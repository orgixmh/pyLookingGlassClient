#!/usr/bin/env python3
import socket, struct, threading, queue

try:
    import glfw
except Exception:
    glfw = None

class VNCInputProxy(threading.Thread):
    def __init__(self, host="127.0.0.1", port=5901, verbose=False,
                 offset_x=0, offset_y=0, scale_x=1.0, scale_y=1.0, **_compat):
        super().__init__(daemon=True)
        self.host, self.port, self.verbose = host, port, verbose
        self.offset_x, self.offset_y = int(offset_x), int(offset_y)
        self.scale_x, self.scale_y  = float(scale_x), float(scale_y)

        self._stop = threading.Event()
        self._q    = queue.Queue()
        self._sock = None
        self._win  = None
        self.remote_w = None
        self.remote_h = None

        self._last_x = 0.0
        self._last_y = 0.0

        # Track button state (RFB mask bits): 1=left, 2=middle, 4=right, 8/16=wheel up/down (edge only)
        self._btn_mask = 0

    # ---------- public API from viewer ----------
    def on_window_created(self, win):
        self._win = win
        if self.verbose and glfw and win:
            try:
                ww, wh = glfw.get_window_size(win)
                print(f"[vnc] window size: {ww}x{wh}", flush=True)
            except Exception:
                pass

    def on_cursor_pos(self, x, y, w):
        self._last_x, self._last_y = float(x), float(y)
        xr, yr = self._current_window_xy()
        # Send move with current button mask so drag is preserved
        self._send_pointer(xr, yr, self._btn_mask)

    def on_mouse_button(self, button, action, mods, w):
        # GLFW: 0=left, 1=right, 2=middle
        bit = 0
        if button == 0:   bit = 1   # left
        elif button == 2: bit = 2   # middle
        elif button == 1: bit = 4   # right

        if action != 0:   # press
            self._btn_mask |= bit
        else:             # release
            self._btn_mask &= ~bit

        xr, yr = self._current_window_xy()
        self._send_pointer(xr, yr, self._btn_mask)

    def on_scroll(self, dx, dy, w):
        xr, yr = self._current_window_xy()
        # Emit wheel as short presses (do not latch into _btn_mask)
        if dy > 0:
            self._send_pointer(xr, yr, self._btn_mask | 8)   # wheel up
            self._send_pointer(xr, yr, self._btn_mask)
        elif dy < 0:
            self._send_pointer(xr, yr, self._btn_mask | 16)  # wheel down
            self._send_pointer(xr, yr, self._btn_mask)
        if dx > 0:
            self._send_pointer(xr, yr, self._btn_mask | 32)  # wheel right
            self._send_pointer(xr, yr, self._btn_mask)
        elif dx < 0:
            self._send_pointer(xr, yr, self._btn_mask | 64)  # wheel left
            self._send_pointer(xr, yr, self._btn_mask)

    def on_key(self, keysym, sc, action, mods, w):
        down = 1 if action != 0 else 0
        self._send_key(int(keysym), down)

    # ---------- thread loop ----------
    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def run(self):
        try:
            self._sock = self._connect_and_handshake()
            if not self._sock:
                return
            while not self._stop.is_set():
                try:
                    kind, args = self._q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "key":
                    ks, down = args
                    self._send_key(ks, down)
                elif kind == "ptr":
                    x, y, mask = args
                    self._send_pointer(int(x), int(y), int(mask))
        except Exception as e:
            if self.verbose:
                print(f"[vnc] exit: {e}", flush=True)
        finally:
            try:
                if self._sock:
                    self._sock.close()
            except Exception:
                pass

    # ---------- internals ----------
    def _log(self, msg):
        if self.verbose:
            print(f"[vnc] {msg}", flush=True)

    def _connect_and_handshake(self):
        s = socket.create_connection((self.host, self.port), timeout=5.0)

        # ProtocolVersion
        srv_ver = s.recv(12)
        if not srv_ver or not srv_ver.startswith(b"RFB "):
            raise RuntimeError("invalid server greeting")
        s.sendall(b"RFB 003.008\n")

        # Security types
        sec_count = s.recv(1)
        if not sec_count:
            raise RuntimeError("no security types")
        n = sec_count[0]
        sec_types = s.recv(n)
        if 1 not in sec_types:
            raise RuntimeError(f"server doesn't offer None security: {sec_types}")
        s.sendall(b"\x01")  # None

        # SecurityResult
        res = s.recv(4)
        if len(res) != 4 or struct.unpack("!I", res)[0] != 0:
            raise RuntimeError("security failed")

        # ClientInit (share desktop)
        s.sendall(b"\x01")

        # ServerInit
        header = self._recvn(s, 24)
        w, h = struct.unpack("!HH", header[:4])
        self.remote_w, self.remote_h = int(w), int(h)
        name_len = struct.unpack("!I", header[20:24])[0]
        _name = self._recvn(s, name_len)

        print(f"[vnc] connected to {self.host}:{self.port} — remote {self.remote_w}x{self.remote_h}", flush=True)

        # SetEncodings with empty list
        s.sendall(struct.pack(">BBH", 2, 0, 0))

        self._sock = s
        return s

    def _recvn(self, s, n):
        data = b""
        while len(data) < n:
            chunk = s.recv(n - len(data))
            if not chunk:
                raise ConnectionError("unexpected EOF")
            data += chunk
        return data

    def _current_window_xy(self):
        # GLFW window coords are top-left origin — same as RFB
        if self._win is None or not glfw:
            xr, yr = int(self._last_x), int(self._last_y)
        else:
            ww, wh = glfw.get_window_size(self._win)
            rw = max(1, self.remote_w or ww)
            rh = max(1, self.remote_h or wh)
            xr = int(float(self._last_x) * rw / max(1, ww))
            yr = int(float(self._last_y) * rh / max(1, wh))

        # apply calibration
        xr = int(xr * self.scale_x + self.offset_x)
        yr = int(yr * self.scale_y + self.offset_y)

        # clamp
        if self.remote_w: xr = max(0, min(self.remote_w - 1, xr))
        if self.remote_h: yr = max(0, min(self.remote_h - 1, yr))

        self._log(f"ptr x={xr} y={yr} mask=0x{self._btn_mask:02X}")
        return xr, yr

    # ------- RFB encoders -------
    def _send_key(self, keysym, down):
        if not self._sock: return
        msg = struct.pack("!BBH I", 4, 1 if down else 0, 0, int(keysym))
        self._sock.sendall(msg)
        self._log(f"key 0x{int(keysym):04X} {'down' if down else 'up'}")

    def _send_pointer(self, x, y, mask):
        if not self._sock: return
        msg = struct.pack("!BBHH", 5, int(mask) & 0xFF, int(x) & 0xFFFF, int(y) & 0xFFFF)
        self._sock.sendall(msg)
        self._log(f"ptr x={x} y={y} mask=0x{int(mask)&0xFF:02X}")
