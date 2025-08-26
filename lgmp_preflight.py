#!/usr/bin/env python3
import os, time, mmap, struct

from lgmp_profile import (
    SET_BITS,
    IDX_OFF_DEFAULT, FLAG_OFF_DEFAULT, FLAG_MASK_DEFAULT,
    ACK_RANGES_DEFAULT, ACK_FALLBACK_DEFAULT,
)

def _u32(mm, off): return struct.unpack_from("<I", mm, off)[0]
def _p32(mm, off, val): struct.pack_into("<I", mm, off, val & 0xFFFFFFFF)

def _ensure_connected(mm, flag_off, flag_mask):
    cur = _u32(mm, flag_off)
    if (cur & flag_mask) == 0:
        _p32(mm, flag_off, cur | flag_mask)

def _idx_delta(mm, idx_off, win_ms=60, step_ms=5):
    start = _u32(mm, idx_off); t0 = time.time()
    while (time.time()-t0)*1000.0 < win_ms:
        time.sleep(step_ms/1000.0)
    end = _u32(mm, idx_off)
    return (end - start) & 0xFFFFFFFF

def _pulse_once(mm, off, idx, mode, state):
    # three safe patterns; tiny dwell to avoid thrashing
    if mode == "inc32":
        state = 1 if state is None else (state + 1) & 0xFFFFFFFF
        for v in ((idx*1103515245 + 12345) & 0xFFFFFFFF, idx, (idx+1)&0xFFFFFFFF, state):
            _p32(mm, off, v); time.sleep(0.0015)
        return state
    elif mode == "mirror":
        for v in (idx, (idx+1)&0xFFFFFFFF, idx):
            _p32(mm, off, v); time.sleep(0.0015)
        return state
    else:  # toggle1
        v = 0xAAAAAAAA if (idx & 1)==0 else 0x55555555
        _p32(mm, off, v); time.sleep(0.0015)
        return state

def _score_candidate(mm, off, idx_off, quiet_ms=60, pulse_ms=60):
    dq = _idx_delta(mm, idx_off, quiet_ms, 5)
    best_mode, best_dp = None, -1
    for mode in ("inc32", "mirror", "toggle1"):
        p0 = _u32(mm, idx_off)
        t0 = time.time(); state=None
        while (time.time()-t0)*1000.0 < pulse_ms:
            idx = _u32(mm, idx_off)
            state = _pulse_once(mm, off, idx, mode, state)
        dp = (_u32(mm, idx_off) - p0) & 0xFFFFFFFF
        if dp > best_dp: best_dp, best_mode = dp, mode
    return dq, best_mode, best_dp

def _find_ack(mm, idx_off, ranges, fallback, margin, verbose):
    tried=set()
    def scan(lst, tag):
        if verbose: print(f"[scan] {tag}: {len(lst)} dwords")
        for off in lst:
            if off == idx_off or off in tried: continue
            tried.add(off)
            dq, mode, dp = _score_candidate(mm, off, idx_off, quiet_ms=45, pulse_ms=45)
            ok = (dp >= dq + margin)
            if verbose:
                print(f"[eval] off=0x{off:x} best={mode} Δq={dq} Δp={dp} -> {'OK' if ok else 'nope'}")
            if ok: return off, mode
        return None, None

    small = [o for (s,e) in ranges for o in range(s,e,4)]
    off, mode = scan(small, "ranges")
    if off is not None: return off, mode

    s, e = fallback
    off, mode = scan(list(range(s, e, 4)), "fallback")
    return off, mode

def warm_boot_and_find_ack(
    shm="/dev/shm/looking-glass",
    idx_off=IDX_OFF_DEFAULT,
    flag_off=FLAG_OFF_DEFAULT, flag_mask=FLAG_MASK_DEFAULT,
    ranges=ACK_RANGES_DEFAULT, fallback=ACK_FALLBACK_DEFAULT,
    margin=2, pump_seconds=2.0, interval=0.02, verbose=False,
):
    """Replay stable boot bits, discover ACK by Δidx correlation, warm-pump briefly; returns (ack_off, mode)."""
    if not os.path.exists(shm):
        raise RuntimeError(f"shm not found: {shm}")

    fd = os.open(shm, os.O_RDWR); st = os.fstat(fd)
    mm = mmap.mmap(fd, st.st_size, mmap.MAP_SHARED, mmap.PROT_READ|mmap.PROT_WRITE)
    try:
        if mm[:4] != b"LGMP":
            raise RuntimeError("Not LGMP")
        if verbose: print(f"[preflight] shm={shm} size={st.st_size}")

        # 1) assert connected
        _ensure_connected(mm, flag_off, flag_mask)

        # 2) apply stable profile bits (idempotent)
        applied = 0
        for off, mask in SET_BITS.items():
            if off == idx_off:  # never touch the read-index
                continue
            cur = _u32(mm, off)
            newv = cur | (mask & 0xFFFFFFFF)
            if newv != cur:
                _p32(mm, off, newv); applied += 1
                if verbose: print(f"[preflight] set 0x{off:x} |= 0x{mask:08x} -> 0x{newv:08x}")
        if verbose: print(f"[preflight] applied {applied} set-bit writes")

        # 3) find ACK
        ack_off, mode = _find_ack(mm, idx_off, ranges, fallback, margin, verbose)
        if ack_off is None:
            raise RuntimeError("Could not locate ACK; widen fallback or lower margin")
        if verbose: print(f"[preflight] ACK @ 0x{ack_off:x} mode={mode}")

        # 4) warm-pump for a moment
        state=None; end=time.time()+pump_seconds; beats=0
        while time.time() < end:
            idx = _u32(mm, idx_off)
            state = _pulse_once(mm, ack_off, idx, mode, state)
            beats += 1
            time.sleep(max(0.001, interval))
        if verbose: print(f"[preflight] pumped {beats} ticks")

        return ack_off, mode
    finally:
        try: mm.close()
        except: pass
        os.close(fd)
