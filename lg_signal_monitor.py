#!/usr/bin/env python3
import sys, os, mmap, struct, time, threading, termios, tty
from datetime import datetime
from collections import deque

def u32(mm, off):
    return struct.unpack_from("<I", mm, off)[0]

class Ring3:
    def __init__(self):
        self.buf=[]
    def push(self, item):
        self.buf.append(item)
        if len(self.buf)>3:
            self.buf.pop(0)
    def last3(self): return list(self.buf)

class RateMeter:
    def __init__(self, horizon=1.0):
        self.horizon = float(horizon)
        self.samples = deque()
    def push(self, t, v):
        self.samples.append((t, v))
        cutoff = t - self.horizon
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
    def rate(self):
        if len(self.samples) < 2: return 0.0
        t0, v0 = self.samples[0]
        t1, v1 = self.samples[-1]
        dt = max(1e-6, t1 - t0)
        return max(0.0, (v1 - v0) / dt)

# --- Predicates ---
class PredBase:
    def describe(self): return ""
    def check(self, cur, history): return True

class PredEq(PredBase):
    def __init__(self, val): self.val = int(val)
    def describe(self): return f"==0x{self.val:08X}"
    def check(self, cur, history):
        if cur is None: cur = 0
        return int(cur) == self.val

class PredNZ(PredBase):
    def describe(self): return "!=0"
    def check(self, cur, history):
        if cur is None: cur = 0
        return int(cur) != 0

class PredOneOf(PredBase):
    def __init__(self, vals): self.vals = set(int(x) for x in vals)
    def describe(self): return "oneof{" + ",".join(f"0x{v:08X}" for v in sorted(self.vals)) + "}"
    def check(self, cur, history):
        if cur is None: cur = 0
        return int(cur) in self.vals

class PredRecentEq(PredBase):
    def __init__(self, val, window_ms=1000):
        self.val = int(val); self.win = float(window_ms)/1000.0
    def describe(self): return f"recent==0x{self.val:08X} in {int(self.win*1000)}ms"
    def check(self, cur, history):
        now = time.time()
        it = history if history is not None else []
        for t,v in reversed(it):
            if now - t > self.win: break
            if int(v) == self.val: return True
        return False

# --- Monitor ---
class SignalMonitor4:
    def __init__(self, shm, idx_off, flag_off, flag_mask, preds:dict,
                 poll_ms=10, out_file="signal_snapshots.txt", verbose=True,
                 fps_ok=30.0, fps_dead=0.5, fps_horizon=1.0):
        self.shm_path = shm
        self.idx_off  = idx_off
        self.flag_off = flag_off
        self.flag_mask= flag_mask
        self.preds    = dict(preds or {})
        self.poll_s   = max(0.001, poll_ms/1000.0)
        self.out_file = out_file
        self.verbose  = verbose
        self.fps_ok   = float(fps_ok)
        self.fps_dead = float(fps_dead)
        self.stop_ev  = threading.Event()
        self.fd       = None
        self.mm       = None

        watch_addrs = [idx_off, flag_off] + sorted(self.preds.keys())
        seen = set()
        self.watch_addrs = []
        for a in watch_addrs:
            if a not in seen:
                self.watch_addrs.append(a); seen.add(a)

        self.hist     = {a: Ring3() for a in self.watch_addrs}
        self.last_val = {a: 0        for a in self.watch_addrs}
        self.fps      = RateMeter(horizon=fps_horizon)
        self.last_print = 0.0

    def open(self):
        self.fd = os.open(self.shm_path, os.O_RDONLY)
        st = os.fstat(self.fd)
        self.mm = mmap.mmap(self.fd, st.st_size, mmap.MAP_SHARED, mmap.PROT_READ)
        if self.verbose:
            print(f"[mon4] opened {self.shm_path} ({st.st_size} bytes); watching {len(self.watch_addrs)} addresses")
        for a in self.watch_addrs:
            try:
                v = u32(self.mm,a)
                self.last_val[a] = v
                if self.verbose:
                    print(f"[mon4]   0x{a:08X} = 0x{v:08X}")
            except Exception as e:
                self.last_val[a] = 0
                if self.verbose:
                    print(f"[mon4]   0x{a:08X} = <err {e}>")

    def close(self):
        try:
            if self.mm: self.mm.close()
        finally:
            if self.fd: os.close(self.fd)

    def _classify(self, now):
        fps = self.fps.rate()
        flagv = self.last_val.get(self.flag_off, 0) or 0
        masked = (flagv & self.flag_mask) != 0 if self.flag_mask else True

        preds_ok = True
        for addr, pred in self.preds.items():
            cur = self.last_val.get(addr, 0) or 0
            history = self.hist.get(addr).buf if addr in self.hist else []
            if not pred.check(cur, history):
                preds_ok = False
                break

        if fps <= self.fps_dead:
            return "dead", f"fps={fps:.1f}, idx stalled"
        if fps >= self.fps_ok and masked and preds_ok:
            return "ok", f"fps={fps:.1f}, mask bit on, predicates pass"
        reasons = []
        if fps < self.fps_ok: reasons.append(f"low fps={fps:.1f}")
        if not masked: reasons.append("mask bit off")
        if not preds_ok: reasons.append("predicates failed")
        return "problematic", ", ".join(reasons) if reasons else "unknown"

    def poll_loop(self):
        try:
            while not self.stop_ev.is_set():
                now = time.time()
                try:
                    idxv = u32(self.mm, self.idx_off)
                except Exception:
                    idxv = self.last_val.get(self.idx_off, 0) or 0
                self.fps.push(now, idxv)
                self.last_val[self.idx_off] = idxv

                for a in self.watch_addrs:
                    try:
                        v = u32(self.mm, a)
                    except Exception:
                        v = self.last_val.get(a, 0) or 0
                    if v != self.last_val.get(a, 0):
                        self.last_val[a] = v
                        self.hist[a].push((now, v))
                        if self.verbose and a == self.idx_off:
                            ts = datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3]
                            print(f"[mon4] idx 0x{v:08X} @ {ts} (fps {self.fps.rate():.1f})")

                if self.verbose and (now - self.last_print) > 1.0:
                    self.last_print = now
                    status, reason = self._classify(now)
                    flagv = self.last_val.get(self.flag_off, 0) or 0
                    masked = (flagv & self.flag_mask) != 0 if self.flag_mask else True
                    print(f"[mon4] status={status} ({reason}); mask={'1' if masked else '0'}; fps={self.fps.rate():.1f}")
                time.sleep(self.poll_s)
        except Exception as e:
            if self.verbose:
                print(f"[mon4] poll exit: {e}")

    def snapshot(self, label=None):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status, reason = self._classify(time.time())
        lines = []
        lines.append(f"=== SNAPSHOT {ts} {f'[{label}]' if label else ''} ===")
        lines.append(f"status={status} ({reason}); fps={self.fps.rate():.1f}")
        flagv = self.last_val.get(self.flag_off, 0) or 0
        lines.append(f"flag 0x{self.flag_off:08X} & 0x{self.flag_mask:08X} => 0x{flagv & self.flag_mask:08X} (raw=0x{flagv:08X})")
        for a,p in self.preds.items():
            cur = self.last_val.get(a, 0) or 0
            ok = p.check(cur, self.hist.get(a).buf if a in self.hist else [])
            lines.append(f"pred  0x{a:08X}: cur=0x{cur:08X}, require {p.describe()} -> {'OK' if ok else 'FAIL'}")
        for a in self.watch_addrs:
            cur = self.last_val.get(a, 0) or 0
            hist = self.hist[a].last3()
            lines.append(f"addr 0x{a:08X}: current=0x{cur:08X}")
            for i,(t,v) in enumerate(hist, start=1):
                tstr = datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]
                lines.append(f"  -#{i} 0x{v:08X} @ {tstr}")
        lines.append("")
        with open(self.out_file, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[mon4] wrote snapshot to {self.out_file} ({status})")

# Optional spacebar loop (for standalone use)
def read_space_loop(on_space, stop_ev):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    label = None
    try:
        tty.setraw(fd)
        while not stop_ev.is_set():
            ch = sys.stdin.read(1)
            if ch == ' ':
                on_space(label)
            elif ch in ('s','S'):
                label = 'OK'; print("[mon4] label=[OK]")
            elif ch in ('d','D'):
                label = 'DEAD'; print("[mon4] label=[DEAD]")
            elif ch in ('p','P'):
                label = 'PROBLEM'; print("[mon4] label=[PROBLEM]")
            elif ch in ('\x03', '\x04', '\x1b'):
                stop_ev.set(); break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

if __name__ == "__main__":
    # not meant to be run standalone
    pass
