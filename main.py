#!/usr/bin/env python3
import argparse, time, threading, sys
from PyQt5.QtWidgets import QApplication, QMainWindow, QAction
from PyQt5.QtOpenGL import QGLWidget
from PyQt5.QtCore import Qt, QTimer
from OpenGL import GL

from lgmp_profile  import (
    IDX_OFF_DEFAULT, FLAG_OFF_DEFAULT, FLAG_MASK_DEFAULT,
    ACK_RANGES_DEFAULT, ACK_FALLBACK_DEFAULT,
)
from lgmp_preflight import warm_boot_and_find_ack
from lgmp_ring     import LGMPv6
from input_vnc     import VNCInputProxy

from lg_signal_monitor import SignalMonitor4, PredEq, PredNZ, PredOneOf

from gl_viewer import (
    _build_program, _make_quad, _create_tex, _upload_bgr,
    VERT_SRC_TEX, FRAG_SRC_TEX
)

def on_ui_action(action, payload=None):
    print(f"[ui] action={action} payload={payload or {}}", flush=True)

def parse_size(s):
    try:
        w, h = map(int, s.lower().split("x"))
    except Exception:
        w, h = 1920, 1080
    return w, h

# -------- Qt -> X11/VNC keysym map  --------
QT2X11 = {
    Qt.Key_A: 0x0061, Qt.Key_B: 0x0062, Qt.Key_C: 0x0063, Qt.Key_D: 0x0064,
    Qt.Key_E: 0x0065, Qt.Key_F: 0x0066, Qt.Key_G: 0x0067, Qt.Key_H: 0x0068,
    Qt.Key_I: 0x0069, Qt.Key_J: 0x006A, Qt.Key_K: 0x006B, Qt.Key_L: 0x006C,
    Qt.Key_M: 0x006D, Qt.Key_N: 0x006E, Qt.Key_O: 0x006F, Qt.Key_P: 0x0070,
    Qt.Key_Q: 0x0071, Qt.Key_R: 0x0072, Qt.Key_S: 0x0073, Qt.Key_T: 0x0074,
    Qt.Key_U: 0x0075, Qt.Key_V: 0x0076, Qt.Key_W: 0x0077, Qt.Key_X: 0x0078,
    Qt.Key_Y: 0x0079, Qt.Key_Z: 0x007A,
    Qt.Key_0: 0x0030, Qt.Key_1: 0x0031, Qt.Key_2: 0x0032, Qt.Key_3: 0x0033,
    Qt.Key_4: 0x0034, Qt.Key_5: 0x0035, Qt.Key_6: 0x0036, Qt.Key_7: 0x0037,
    Qt.Key_8: 0x0038, Qt.Key_9: 0x0039,
    Qt.Key_Space: 0x0020, Qt.Key_Return: 0xFF0D, Qt.Key_Escape: 0xFF1B,
    Qt.Key_Backspace: 0xFF08, Qt.Key_Tab: 0xFF09,
    Qt.Key_Left: 0xFF51, Qt.Key_Up: 0xFF52, Qt.Key_Right: 0xFF53, Qt.Key_Down: 0xFF54,
    Qt.Key_F1: 0xFFBE, Qt.Key_F2: 0xFFBF, Qt.Key_F3: 0xFFC0, Qt.Key_F4: 0xFFC1,
    Qt.Key_F5: 0xFFC2, Qt.Key_F6: 0xFFC3, Qt.Key_F7: 0xFFC4, Qt.Key_F8: 0xFFC5,
    Qt.Key_F9: 0xFFC6, Qt.Key_F10: 0xFFC7, Qt.Key_F11: 0xFFC8, Qt.Key_F12: 0xFFC9,
}
def map_keysym(qt_key):
    return QT2X11.get(qt_key, qt_key)

# --- Health monitor ---
class HealthThread:
    def __init__(self, shm, idx_off, flag_off, flag_mask, on_transition=None,
                 fps_ok=30.0, fps_dead=0.5, relaxed=True):
        preds = {
            int(0x138): PredEq(0xEBEEEBAF),
            int(0x1C4): PredNZ(),
            int(0x63C): PredNZ(),
            int(0x648): PredNZ(),
            int(0x640): PredOneOf([0x1, 0x2]),
            int(0x4A8): PredOneOf([0x0, 0x14]),
        }
        self.on_transition = on_transition
        self.relaxed = bool(relaxed)
        self.fps_ok = float(fps_ok)
        self.mon = SignalMonitor4(shm, idx_off, flag_off, flag_mask, preds,
                                  poll_ms=10, out_file="/dev/null",
                                  verbose=False, fps_ok=fps_ok, fps_dead=fps_dead, fps_horizon=1.0)
        self._last_status = None
        self.started = False

    def start(self):
        if not self.started:
            self.mon.open()
            self.t_poll = threading.Thread(target=self.mon.poll_loop, daemon=True)
            self.t_poll.start()
            self.t = threading.Thread(target=self._loop, daemon=True)
            self.t.start()
            self.started = True

    def _loop(self):
        while True:
            time.sleep(0.2)
            status, reason = self.mon._classify(time.time())
            fps = self.mon.fps.rate()
            if self.relaxed and status != "dead" and fps >= self.fps_ok * 0.9:
                status = "ok"
            if self._last_status is not None and status != self._last_status:
                try:
                    if callable(self.on_transition):
                        self.on_transition(self._last_status, status)
                except Exception as e:
                    print(f"[health] transition callback error: {e}", flush=True)
            self._last_status = status

    def status(self):
        status, reason = self.mon._classify(time.time())
        fps = self.mon.fps.rate()
        if self.relaxed and status != "dead" and fps >= self.fps_ok * 0.9:
            return "ok"
        return status

# ---- Qt OpenGL widget with direct VNC proxy ----
class ViewerGL(QGLWidget):
    def __init__(self, lg, vnc=None, parent=None):
        super().__init__(parent)
        self.lg = lg
        self.vnc = vnc
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.prog = None
        self.vao = None
        self.tex = None
        self.locTex = None

    def _to_guest(self, x, y):
        w = max(1, self.width())
        h = max(1, self.height())
        gx = int(x * self.lg.fb_w / w)
        gy = int(y * self.lg.fb_h / h)
        return gx, gy

    def _qt_mods(self, ev):
        mods = 0
        if ev.modifiers() & Qt.ShiftModifier: mods |= 0x1
        if ev.modifiers() & Qt.ControlModifier: mods |= 0x2
        if ev.modifiers() & Qt.AltModifier: mods |= 0x4
        return mods

    # ---- Input forwarding ----
    def mouseMoveEvent(self, ev):
        if self.vnc:
            gx, gy = self._to_guest(ev.x(), ev.y())
            self.vnc.on_cursor_pos(gx, gy, self)

    def mousePressEvent(self, ev):
        if not self.vnc: return
        if ev.button() == Qt.LeftButton: btn = 0
        elif ev.button() == Qt.RightButton: btn = 1
        elif ev.button() == Qt.MiddleButton: btn = 2
        else: return
        gx, gy = self._to_guest(ev.x(), ev.y())
        self.vnc.on_cursor_pos(gx, gy, self)
        self.vnc.on_mouse_button(btn, 1, self._qt_mods(ev), self)

    def mouseReleaseEvent(self, ev):
        if not self.vnc: return
        if ev.button() == Qt.LeftButton: btn = 0
        elif ev.button() == Qt.RightButton: btn = 1
        elif ev.button() == Qt.MiddleButton: btn = 2
        else: return
        gx, gy = self._to_guest(ev.x(), ev.y())
        self.vnc.on_cursor_pos(gx, gy, self)
        self.vnc.on_mouse_button(btn, 0, self._qt_mods(ev), self)

    def wheelEvent(self, ev):
        if self.vnc:
            dx = ev.angleDelta().x()
            dy = ev.angleDelta().y()
            self.vnc.on_scroll(dx, dy, self)

    def keyPressEvent(self, ev):
        if self.vnc:
            keysym = map_keysym(ev.key())
            self.vnc.on_key(keysym, 0, 1, self._qt_mods(ev), self)

    def keyReleaseEvent(self, ev):
        if self.vnc:
            keysym = map_keysym(ev.key())
            self.vnc.on_key(keysym, 0, 0, self._qt_mods(ev), self)

    def initializeGL(self):
        self.prog = _build_program(VERT_SRC_TEX, FRAG_SRC_TEX)
        self.vao, _, _ = _make_quad(self.prog, flip_y=True)
        self.tex = _create_tex()
        self.locTex = GL.glGetUniformLocation(self.prog, "uTex")
        GL.glClearColor(0, 0, 0, 1)

    def paintGL(self):
        try:
            slot = self.lg.current_slot()
            data = self.lg.read_frame_tight(slot)
            if data:
                _upload_bgr(self.tex, self.lg.fb_w, self.lg.fb_h, data, self.lg.bpp)
        except Exception:
            pass

        GL.glViewport(0, 0, self.width(), self.height())
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glUseProgram(self.prog)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glUniform1i(self.locTex, 0)
        GL.glBindVertexArray(self.vao)
        GL.glDrawElements(GL.GL_TRIANGLES, 6, GL.GL_UNSIGNED_INT, None)
        GL.glBindVertexArray(0)

        self.update()

# ---- Main Qt window ----
class MainWindow(QMainWindow):
    def __init__(self, args, lg, vnc, health):
        super().__init__()
        self.lg = lg
        self.vnc = vnc
        self.health = health

        w, h = parse_size(args.win)
        self.setWindowTitle("LGMP Client")
        self.resize(w, h)

        menubar = self.menuBar()
        fileMenu = menubar.addMenu("Machine")
        fileMenu.addAction(QAction("Start", self, triggered=lambda: on_ui_action("start")))
        fileMenu.addAction(QAction("Restart", self, triggered=lambda: on_ui_action("restart")))
        fileMenu.addAction(QAction("Shutdown", self, triggered=lambda: on_ui_action("shutdown")))
        viewMenu = menubar.addMenu("View")
        viewMenu.addAction(QAction("Fullscreen", self, triggered=lambda: on_ui_action("fullscreen_toggle")))

        self.viewer = ViewerGL(lg, vnc)
        self.setCentralWidget(self.viewer)

        self._sb = self.statusBar()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_statusbar)
        self._timer.start(500)

    def _tick_statusbar(self):
        try:
            status = self.health.status()
            fps = getattr(self.health.mon.fps, "rate", lambda: 0.0)()
            self._sb.showMessage(f"Health: {status} | FPS: {fps:.1f}")
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser(description="LGMP v6 Qt client with VNC input")
    ap.add_argument("--shm", default="/dev/shm/looking-glass")
    ap.add_argument("--bpp", type=int, choices=[3,4], default=3)
    ap.add_argument("--width",  type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--pitch",  type=int, default=5888)
    ap.add_argument("--offset", type=int, default=3169789)
    ap.add_argument("--slot",   type=int, default=0)
    ap.add_argument("--win",    default="1920x1080")
    ap.add_argument("--no-preflight", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--idx-off",  type=lambda x:int(x,0), default=IDX_OFF_DEFAULT)
    ap.add_argument("--flag-off", type=lambda x:int(x,0), default=FLAG_OFF_DEFAULT)
    ap.add_argument("--flag-mask",type=lambda x:int(x,0), default=FLAG_MASK_DEFAULT)
    ap.add_argument("--health-fps-ok", type=float, default=30.0)
    ap.add_argument("--health-fps-dead", type=float, default=0.5)
    ap.add_argument("--health-relaxed", action="store_true")
    ap.add_argument("--vnc-host", default=None)
    ap.add_argument("--vnc-port", type=int, default=5901)
    ap.add_argument("--vnc-offset-x", type=int, default=0)
    ap.add_argument("--vnc-offset-y", type=int, default=0)
    ap.add_argument("--vnc-scale-x", type=float, default=1.0)
    ap.add_argument("--vnc-scale-y", type=float, default=1.0)
    ap.add_argument("--no-input", action="store_true")

    args = ap.parse_args()

    if not args.no_preflight:
        print("[auto] running preflight (warm boot + ACK scan)", flush=True)
        warm_boot_and_find_ack(
            shm=args.shm,
            idx_off=args.idx_off,
            flag_off=args.flag_off, flag_mask=args.flag_mask,
            ranges=ACK_RANGES_DEFAULT,
            fallback=ACK_FALLBACK_DEFAULT,
            margin=2,
            pump_seconds=2.0,
            interval=0.02,
            verbose=True,
        )

    lg = LGMPv6(
        shm=args.shm,
        width=args.width, height=args.height, pitch=args.pitch, bpp=args.bpp,
        idx_off=args.idx_off, force_offset=args.offset, force_slot=0, nbuf=1
    )

    vnc = None
    if args.vnc_host and not args.no_input:
        print(f"[main] starting VNC input proxy to {args.vnc_host}:{args.vnc_port}", flush=True)
        vnc = VNCInputProxy(host=args.vnc_host, port=args.vnc_port, verbose=args.verbose,
                            offset_x=args.vnc_offset_x, offset_y=args.vnc_offset_y,
                            scale_x=args.vnc_scale_x, scale_y=args.vnc_scale_y)
        vnc.start()

    def on_transition(prev, curr):
        print(f"[health] transition {prev} -> {curr}", flush=True)
        if prev in ("dead","problematic") and curr == "ok":
            try: warm_boot_and_find_ack(
                shm=args.shm,
                idx_off=args.idx_off,
                flag_off=args.flag_off, flag_mask=args.flag_mask,
                ranges=ACK_RANGES_DEFAULT,
                fallback=ACK_FALLBACK_DEFAULT,
                margin=2,
                pump_seconds=2.0,
                interval=0.02,
                verbose=True,
            )
            except Exception as e:
                print(f"[auto] preflight error: {e}", flush=True)
            if vnc: vnc.start()

    health = HealthThread(args.shm, args.idx_off, args.flag_off, args.flag_mask,
                          on_transition=on_transition,
                          fps_ok=args.health_fps_ok, fps_dead=args.health_fps_dead,
                          relaxed=args.health_relaxed or True)
    health.start()

    app = QApplication(sys.argv)
    win = MainWindow(args, lg, vnc, health)
    win.show()
    try:
        sys.exit(app.exec_())
    finally:
        try: lg.close()
        except Exception: pass
        if vnc: vnc.stop()

if __name__ == "__main__":
    raise SystemExit(main())
