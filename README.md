# pyLgClient (Qt single-window viewer + VNC input)

A lightweight Qt-based frontend that embeds an OpenGL viewer, reads frames from a Looking Glass–style shared memory buffer, and injects **mouse/keyboard** into the guest via **VNC**.

## What it does

- Renders the guest framebuffer from `/dev/shm/looking-glass` (configurable).
- Native Qt window with menu + status bar (health & FPS).
- Sends mouse & keyboard to a VNC server (e.g., QEMU’s built-in VNC on `:1` → port **5901**).
- Auto preflight + health monitor + auto-reconnect behavior (mirrors the original main).

## Requirements

- Python 3.9+ (works with 3.8 too)
- System packages: OpenGL drivers; on Debian/Ubuntu: `sudo apt install python3-pyqt5 python3-opengl`
- Python packages (if not already present):
  - `PyQt5`
  - `PyOpenGL`


> **Note on shared memory**  
> The app reads from `/dev/shm/looking-glass`. Ensure your host/guest Looking Glass setup (or equivalent producer) populates this SHM region, or adjust `--shm` to the correct path.

## Quick start

```bash
python3 main.py   --shm /dev/shm/looking-glass   --bpp 3   --width 1920 --height 1080 --pitch 5888   --offset 3169789   --slot 0   --win 1920x1080   --vnc-host 127.0.0.1
```

- `--vnc-host 127.0.0.1` assumes QEMU is serving VNC on **port 5901** (`:1`).
- Health info updates live in the status bar.
- Menubar contains basic actions you can wire up to QMP/libvirt later.

### Useful flags

- `--no-preflight` to skip the warm boot/ACK scan.
- `--vnc-offset-x / --vnc-offset-y` and `--vnc-scale-x / --vnc-scale-y` to fix input alignment if needed.
- `--health-fps-ok`, `--health-fps-dead`, `--health-relaxed` tune health classification.

## Minimal QEMU example (VNC on 5901)

Below are two options. Both expose VNC on `127.0.0.1:5901` (display `:1`) so this app can inject input.

### 1) Simple VNC-only display (software GPU)
```bash
qemu-system-x86_64   -enable-kvm   -machine q35,accel=kvm   -cpu host   -smp 4 -m 8G   -drive file=./disk.qcow2,if=virtio   -nic user,model=virtio-net-pci   -display vnc=127.0.0.1:1,lossy=on   -k en-us   -device virtio-tablet   -device virtio-keyboard
```

- `-display vnc=127.0.0.1:1`  VNC on **5901**.
- `-device virtio-tablet` gives absolute pointer (nicer mouse behavior over VNC).

### 2) With GPU passthrough (real monitor) **and** lightweight VNC for input
If you’re doing VFIO passthrough but still want VNC for input injection:

```bash
qemu-system-x86_64   -enable-kvm   -machine q35,accel=kvm   -cpu host   -smp 8 -m 16G   -drive file=./disk.qcow2,if=virtio   -nic user,model=virtio-net-pci     # Your GPU passthrough (example, adjust IDs)
  -device vfio-pci,host=0000:01:00.0,multifunction=on   -device vfio-pci,host=0000:01:00.1     # Keep a VNC server for input only
  -display vnc=127.0.0.1:1,lossy=on   -k en-us   -device virtio-tablet   -device virtio-keyboard
```

> We can also use `-display none` + `-vnc 127.0.0.1:1`
> ```
> -display none -vnc 127.0.0.1:1
> ```

## Tips / Troubleshooting

- **Mouse alignment off?**  
  Try `--vnc-offset-x / --vnc-offset-y` or scaling with `--vnc-scale-x / --vnc-scale-y`.

- **High CPU usage?**  
  The viewer currently repaints continuously. Later I will switch to a `QTimer` (e.g., 60 Hz) to reduce CPU, and/or restore `nbuf=2`.

- **No input in the guest?**  
  Ensure QEMU’s VNC is listening on `127.0.0.1:5901` and not password-protected, and that you launched `main.py` with `--vnc-host 127.0.0.1`.


## Roadmap

- Sidebars/panels (QMP controls, disk snapshots, USB attach/detach).
- Input profile manager (macros, per-game tweaks).
- Smarter frame pacing and adaptive refresh.
- Optional GL overlay reintroduced as a Qt overlay widget.

---

**License:** MIT
