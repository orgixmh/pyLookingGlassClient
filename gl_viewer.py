#!/usr/bin/env python3
# gl_viewer.py — Looking Glass python viewer 
#
#
# - Draws LG frame using a textured quad shader (RGB/BGRA upload).
# - If health_fn is provided and returns anything other than "ok",
#   it draws a semi-transparent black full-screen overlay with text:
#       -- waiting for signal --
# - If ui is provided, ui.draw(fbw, fbh) is called every frame
#   and UI receives mouse input before the VNC input sink.

import ctypes
import time
import glfw
import numpy as np
from OpenGL import GL

# ----------------------------------------------------------
# Shaders
# ----------------------------------------------------------
VERT_SRC_TEX = """
#version 130
in vec2 aPos; in vec2 aUV; out vec2 vUV;
void main(){ vUV=aUV; gl_Position=vec4(aPos,0.0,1.0); }
"""
FRAG_SRC_TEX = """
#version 130
in vec2 vUV; out vec4 FragColor; uniform sampler2D uTex;
void main(){ FragColor = texture(uTex, vUV); }
"""

VERT_SRC_SOLID = """
#version 130
in vec2 aPos; uniform vec4 uColor; out vec4 vCol;
void main(){ vCol = uColor; gl_Position = vec4(aPos,0.0,1.0); }
"""
FRAG_SRC_SOLID = """
#version 130
in vec4 vCol; out vec4 FragColor;
void main(){ FragColor = vCol; }
"""

def _compile_shader(kind, src):
    s = GL.glCreateShader(kind)
    GL.glShaderSource(s, src)
    GL.glCompileShader(s)
    if GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS) != GL.GL_TRUE:
        raise RuntimeError(GL.glGetShaderInfoLog(s).decode("utf-8", "ignore"))
    return s

def _build_program(vs_src, fs_src):
    vs = _compile_shader(GL.GL_VERTEX_SHADER, vs_src)
    fs = _compile_shader(GL.GL_FRAGMENT_SHADER, fs_src)
    p  = GL.glCreateProgram()
    GL.glAttachShader(p, vs); GL.glAttachShader(p, fs); GL.glLinkProgram(p)
    if GL.glGetProgramiv(p, GL.GL_LINK_STATUS) != GL.GL_TRUE:
        raise RuntimeError(GL.glGetProgramInfoLog(p).decode("utf-8", "ignore"))
    GL.glDeleteShader(vs); GL.glDeleteShader(fs)
    return p

# ----------------------------------------------------------
# Geometry helpers 
# ----------------------------------------------------------
def _make_quad(prog, flip_y=True):
    """Full-screen quad with positions + UV """
    v0y, v1y = (1.0, 0.0) if flip_y else (0.0, 1.0)
    verts = np.array([
        -1,-1, 0.0, v0y,
         1,-1, 1.0, v0y,
         1, 1, 1.0, v1y,
        -1, 1, 0.0, v1y,
    ], dtype=np.float32)
    idx = np.array([0,1,2, 0,2,3], dtype=np.uint32)

    vao = GL.glGenVertexArrays(1)
    vbo = GL.glGenBuffers(1)
    ebo = GL.glGenBuffers(1)
    GL.glBindVertexArray(vao)

    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
    GL.glBufferData(GL.GL_ARRAY_BUFFER, verts.nbytes, verts, GL.GL_STATIC_DRAW)

    GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ebo)
    GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, GL.GL_STATIC_DRAW)

    stride = 4 * ctypes.sizeof(ctypes.c_float)
    loc_pos = GL.glGetAttribLocation(prog, "aPos")
    if loc_pos != -1:
        GL.glEnableVertexAttribArray(loc_pos)
        GL.glVertexAttribPointer(loc_pos, 2, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(0))

    loc_uv  = GL.glGetAttribLocation(prog, "aUV")
    if loc_uv != -1:
        GL.glEnableVertexAttribArray(loc_uv)
        GL.glVertexAttribPointer(loc_uv, 2, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(8))

    GL.glBindVertexArray(0)
    return vao, vbo, ebo

# ----------------------------------------------------------
# Texture helpers
# ----------------------------------------------------------
def _create_tex():
    t = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, t)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
    GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
    return t

def _upload_bgr(tex, w, h, data, bpp):
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
    GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
    if bpp == 3:
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB, w, h, 0, GL.GL_BGR,  GL.GL_UNSIGNED_BYTE, data)
    else:
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, w, h, 0, GL.GL_BGRA, GL.GL_UNSIGNED_BYTE, data)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_SWIZZLE_A, GL.GL_ONE)
    GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

# ----------------------------------------------------------
# Tiny bitmap text for overlay message (5x7)
# ----------------------------------------------------------
_FONT = {
 ' ': [0,0,0,0,0], '-':[0,0,0x1F,0,0],
 'A':[0x0E,0x11,0x1F,0x11,0x11],'D':[0x1C,0x12,0x11,0x12,0x1C],
 'E':[0x1F,0x10,0x1E,0x10,0x1F],'F':[0x1F,0x10,0x1E,0x10,0x10],
 'G':[0x0F,0x10,0x17,0x11,0x0E],'I':[0x0E,0x04,0x04,0x04,0x0E],
 'L':[0x10,0x10,0x10,0x10,0x1F],'N':[0x11,0x19,0x15,0x13,0x11],
 'O':[0x0E,0x11,0x11,0x11,0x0E],'R':[0x1E,0x11,0x1E,0x12,0x11],
 'S':[0x0F,0x10,0x0E,0x01,0x1E],'T':[0x1F,0x04,0x04,0x04,0x04],
 'U':[0x11,0x11,0x11,0x11,0x0F],'W':[0x11,0x11,0x15,0x1B,0x11],
}

def _text_to_tex(msg, scale=3):
    # uppercase + monospace
    msg = msg.upper()
    cols = 0
    for ch in msg:
        cols += 6  # 5px glyph + 1px space
    cols -= 1
    w = cols*scale + 8
    h = 7*scale + 8
    img = np.zeros((h, w, 4), dtype=np.uint8)
    x = 4
    for ch in msg:
        glyph = _FONT.get(ch, _FONT[' '])
        for gx, col in enumerate(glyph):
            for gy in range(7):
                if (col >> gy) & 1:
                    xs, xe = x + gx*scale, x + (gx+1)*scale
                    ys, ye = 4 + gy*scale, 4 + (gy+1)*scale
                    img[ys:ye, xs:xe, :3] = 255
                    img[ys:ye, xs:xe,  3] = 255
        x += 6*scale
    t = _create_tex()
    GL.glBindTexture(GL.GL_TEXTURE_2D, t)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, img)
    GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
    return t, w, h

# ----------------------------------------------------------
# Main viewer
# ----------------------------------------------------------
def run_viewer(lg, win_w=1920, win_h=1080, title="LGMP v6 Client",
               input_sink=None, health_fn=None, ui=None):

    # minimal GLFW->X11 keysym mapper for arrows/enter/esc/backspace/tab
    import glfw as _glfw
    _KS = {
        _glfw.KEY_LEFT:  0xFF51,
        _glfw.KEY_UP:    0xFF52,
        _glfw.KEY_RIGHT: 0xFF53,
        _glfw.KEY_DOWN:  0xFF54,
        _glfw.KEY_ENTER: 0xFF0D,
        _glfw.KEY_ESCAPE:0xFF1B,
        _glfw.KEY_BACKSPACE:0xFF08,
        _glfw.KEY_TAB:   0xFF09,
    }
    def _keysym_from_glfw(key, mods):
        if key in _KS: return _KS[key]
        if 32 <= key <= 126: return key
        return None

    if not glfw.init(): raise SystemExit(1)
    try:
        glfw.window_hint(glfw.RESIZABLE, True)
        win = glfw.create_window(win_w, win_h, title, None, None)
        if not win: raise SystemExit(1)
        glfw.make_context_current(win); glfw.swap_interval(1)

        # build programs/geometry/textures
        prog_tex   = _build_program(VERT_SRC_TEX,   FRAG_SRC_TEX)
        prog_solid = _build_program(VERT_SRC_SOLID, FRAG_SRC_SOLID)

        vao_tex, vbo_tex, ebo_tex = _make_quad(prog_tex,   flip_y=True)
        vao_blk, vbo_blk, ebo_blk = _make_quad(prog_solid, flip_y=True)  # aUV safely ignored

        tex_frame = _create_tex()
        loc_uTex  = GL.glGetUniformLocation(prog_tex, "uTex")
        loc_uCol  = GL.glGetUniformLocation(prog_solid, "uColor")

        # pre-bake overlay text texture
        overlay_text = "-- waiting for signal --"
        tex_wait, tw, th = _text_to_tex(overlay_text, scale=3)

        # Input plumbing (UI gets first dibs)
        if input_sink is not None:
            try:
                input_sink.on_window_created(win)
                print('[viewer] input forwarding active (mouse+wheel+basic keys)', flush=True)
            except Exception as e:
                print(f'[viewer] input sink setup failed: {e}', flush=True)

        def _cb_cursor(w, x, y):
            consumed = False
            if ui is not None:
                try:
                    consumed = ui.on_mouse(x, y, *glfw.get_window_size(w), pressed=False)
                except Exception:
                    consumed = False
            if not consumed and input_sink is not None:
                try: input_sink.on_cursor_pos(x, y, w)
                except Exception: pass

        def _cb_button(w, button, action, mods):
            consumed = False
            if ui is not None and action in (glfw.PRESS, glfw.REPEAT):
                try:
                    consumed = ui.on_mouse(*glfw.get_cursor_pos(w), *glfw.get_window_size(w), pressed=True)
                except Exception:
                    consumed = False
            if not consumed and input_sink is not None:
                try: input_sink.on_mouse_button(button, action, mods, w)
                except Exception: pass

        def _cb_scroll(w, dx, dy):
            # let UI ignore scroll for now; pass to sink
            if input_sink is not None:
                try: input_sink.on_scroll(dx, dy, w)
                except Exception: pass

        def _cb_key(w, key, sc, action, mods):
            ks = _keysym_from_glfw(key, mods)
            if ks is not None and input_sink is not None:
                try: input_sink.on_key(ks, sc, action, mods, w)
                except Exception: pass

        glfw.set_cursor_pos_callback(win, _cb_cursor)
        glfw.set_mouse_button_callback(win, _cb_button)
        glfw.set_scroll_callback(win, _cb_scroll)
        glfw.set_key_callback(win, _cb_key)

        # main loop
        while not glfw.window_should_close(win):
            glfw.poll_events()

            # Update LG frame texture if new data available
            try:
                slot = lg.current_slot()
                data = lg.read_frame_tight(slot)
                if data:
                    _upload_bgr(tex_frame, lg.fb_w, lg.fb_h, data, lg.bpp)
            except Exception:
                pass

            fbw, fbh = glfw.get_framebuffer_size(win)
            GL.glViewport(0, 0, fbw, fbh)
            GL.glClearColor(0, 0, 0, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)

            # Draw LG frame
            GL.glUseProgram(prog_tex)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex_frame)
            GL.glUniform1i(loc_uTex, 0)
            GL.glBindVertexArray(vao_tex)
            GL.glDrawElements(GL.GL_TRIANGLES, 6, GL.GL_UNSIGNED_INT, None)
            GL.glBindVertexArray(0)

            # Health overlay (draw BEFORE UI so UI stays visible)
            unhealthy = False
            if health_fn is not None:
                try:
                    unhealthy = (health_fn() != "ok")
                except Exception:
                    unhealthy = False
            if unhealthy:
                # semi-transparent black
                GL.glEnable(GL.GL_BLEND)
                GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
                GL.glUseProgram(prog_solid)
                GL.glUniform4f(loc_uCol, 0.0, 0.0, 0.0, 0.6)
                GL.glBindVertexArray(vao_blk)
                GL.glDrawElements(GL.GL_TRIANGLES, 6, GL.GL_UNSIGNED_INT, None)
                GL.glBindVertexArray(0)

                # centered text
                GL.glUseProgram(prog_tex)
                GL.glActiveTexture(GL.GL_TEXTURE0)
                GL.glBindTexture(GL.GL_TEXTURE_2D, tex_wait)
                GL.glUniform1i(loc_uTex, 0)

                # compute centered rect in NDC with some scaling
                # draw at ~1/3 width of the screen
                scale_px = max(1, fbw // 3)
                # keep original text size (tw,th) and center it
                w_px = min(tw, fbw - 40)
                h_px = th
                x0 = (fbw - w_px) // 2
                y0 = (fbh - h_px) // 2
                # convert to NDC
                X0 = (x0 / fbw) * 2.0 - 1.0
                X1 = ((x0 + w_px) / fbw) * 2.0 - 1.0
                Y0 = 1.0 - 2.0 * ((y0 + h_px) / fbh)
                Y1 = 1.0 - 2.0 * (y0 / fbh)

                # build a small temp quad for the text
                verts = np.array([
                    [X0, Y0, 0.0, 0.0],
                    [X1, Y0, 1.0, 0.0],
                    [X1, Y1, 1.0, 1.0],
                    [X0, Y1, 0.0, 1.0],
                ], dtype=np.float32)
                idx = np.array([0,1,2, 0,2,3], dtype=np.uint32)
                vao = GL.glGenVertexArrays(1)
                vbo = GL.glGenBuffers(1)
                ebo = GL.glGenBuffers(1)
                GL.glBindVertexArray(vao)
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
                GL.glBufferData(GL.GL_ARRAY_BUFFER, verts.nbytes, verts, GL.GL_STREAM_DRAW)
                GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ebo)
                GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, GL.GL_STREAM_DRAW)
                loc_pos = GL.glGetAttribLocation(prog_tex, "aPos")
                loc_uv  = GL.glGetAttribLocation(prog_tex, "aUV")
                GL.glEnableVertexAttribArray(loc_pos)
                GL.glVertexAttribPointer(loc_pos, 2, GL.GL_FLOAT, GL.GL_FALSE, 16, ctypes.c_void_p(0))
                GL.glEnableVertexAttribArray(loc_uv)
                GL.glVertexAttribPointer(loc_uv,  2, GL.GL_FLOAT, GL.GL_FALSE, 16, ctypes.c_void_p(8))
                GL.glDrawElements(GL.GL_TRIANGLES, 6, GL.GL_UNSIGNED_INT, None)
                GL.glBindVertexArray(0)
                GL.glDeleteBuffers(1, [vbo]); GL.glDeleteBuffers(1, [ebo]); GL.glDeleteVertexArrays(1, [vao])

            # Draw UI last so it stays on top of everything
            if ui is not None:
                try:
                    ui.draw(fbw, fbh)
                except Exception:
                    pass

            glfw.swap_buffers(win)

    finally:
        glfw.terminate()
