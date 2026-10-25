#!/usr/bin/env python
"""gpu_render.py — OpenGL fragment-shader rasteriser for Fourier stroke documents.

Paints ``.fdraw`` / ``.fbs`` / raw ``layered_base`` stroke JSON on the GPU instead
of the CPU sweep rasteriser: stroke sampling is reused from
``fbs_core.render.sample_stroke`` (no re-implementation), every sampled segment
is uploaded as one instanced quad, and the fragment shader evaluates the exact
CPU band coverage formula

    cov = clip(min(0.5, d + rr) - max(-0.5, d - rr), 0, 1)

where ``rr = 0.5*(r_a + r_b)`` is the segment half width in supersampled pixels.
Fragments are alpha-blended in stroke order into an ``RGBA16F`` FBO at
``supersample`` x, read back with ``glReadPixels``, box-downsampled and saved.

Exit codes: 0 success, 2 input error, 3 no PyOpenGL/GL context (caller should
fall back to the CPU renderer), 4 unsupported document (e.g. region fills).

Only existing dependencies are used: PyOpenGL + glfw + numpy + PIL.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import sys
import time
import zipfile

import numpy as np

# Windows has no EGL runtime.  Some hosts (e.g. ComfyUI node packs) export
# PYOPENGL_PLATFORM=egl, which PyOpenGL would inherit and fail to import in our
# subprocess ("Unable to load EGL library") -> exit 3 -> silent CPU fallback.
# Force the default desktop platform (WGL) on Windows before any OpenGL import.
if os.name == "nt":
    os.environ.pop("PYOPENGL_PLATFORM", None)

# --------------------------------------------------------------------------- paths
_HERE = os.path.dirname(os.path.abspath(__file__))
# <ws>/skills/fourier-multistroke-training/scripts/gpu_render.py
_WS = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
for _p in (os.path.join(_WS, "fourier-prompt"),
           os.path.join(_WS, "fourier-brushstroke", "scripts")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_NO_GL = 3
EXIT_UNSUPPORTED = 4
EXIT_ALPHA = 5  # document/stream has alpha < 1 strokes: GPU band alpha blend
                # is per-segment, the CPU rasteriser paints a stroke band once;
                # such documents must use --device cpu (node falls back)

VERT_SRC = """#version 330 core
layout(location = 0) in vec2 corner;
layout(location = 1) in vec2 seg_a;
layout(location = 2) in vec2 seg_b;
layout(location = 3) in float seg_r;
layout(location = 4) in vec3 col_a;
layout(location = 5) in vec3 col_b;
layout(location = 6) in float alpha_a;
layout(location = 7) in float alpha_b;
uniform vec2 u_size;   // supersampled framebuffer size in pixels
out vec2 v_a;
out vec2 v_b;
out float v_r;
out vec3 v_c0;
out vec3 v_c1;
out float v_a0;
out float v_a1;
void main() {
    float m = seg_r + 1.5;
    vec2 lo = min(seg_a, seg_b) - m;
    vec2 hi = max(seg_a, seg_b) + m;
    vec2 px = mix(lo, hi, corner);
    v_a = seg_a; v_b = seg_b; v_r = seg_r;
    v_c0 = col_a; v_c1 = col_b; v_a0 = alpha_a; v_a1 = alpha_b;
    // world y axis points down; GL NDC y points up
    gl_Position = vec4(px.x / u_size.x * 2.0 - 1.0, 1.0 - px.y / u_size.y * 2.0,
                       0.0, 1.0);
}
"""

FRAG_SRC = """#version 330 core
in vec2 v_a;
in vec2 v_b;
in float v_r;
in vec3 v_c0;
in vec3 v_c1;
in float v_a0;
in float v_a1;
uniform vec2 u_size;
out vec4 out_color;
void main() {
    // gl_FragCoord is bottom-left origin; convert to world top-left pixel coords
    vec2 p = vec2(gl_FragCoord.x, u_size.y - gl_FragCoord.y);
    vec2 ab = v_b - v_a;
    float l2 = dot(ab, ab);
    float t = (l2 > 1e-12) ? clamp(dot(p - v_a, ab) / l2, 0.0, 1.0) : 0.0;
    vec2 q = v_a + t * ab;
    float d = length(p - q);
    float rr = v_r;
    float cov = clamp(min(0.5, d + rr) - max(-0.5, d - rr), 0.0, 1.0);
    vec3 col = mix(v_c0, v_c1, t);
    float al = mix(v_a0, v_a1, t);
    out_color = vec4(col, cov * al);
}
"""


class UnsupportedDoc(Exception):
    pass


class AlphaUnsupported(Exception):
    pass


def _log(msg: str) -> None:
    print(f"[gpu_render] {msg}", file=sys.stderr, flush=True)


def _parse_background(spec: str | None) -> tuple[float, float, float]:
    if spec is None:
        return (1.0, 1.0, 1.0)
    s = str(spec).strip().lower()
    if s in ("white", "#fff", "#ffffff"):
        return (1.0, 1.0, 1.0)
    if s.startswith("#") and len(s) == 7:
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (1, 3, 5))  # type: ignore
    parts = [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]
    if len(parts) < 3:
        raise ValueError(f"--background 需要 R,G,B 或 #RRGGBB: {spec!r}")
    vals = []
    for p in parts[:3]:
        v = float(p)
        vals.append(v / 255.0 if v > 1.0 else v)
    return tuple(float(np.clip(v, 0.0, 1.0)) for v in vals)  # type: ignore


# --------------------------------------------------------------------------- loading
def _read_fdraw_render_json(path: str) -> dict:
    """Fast path: read render.json straight out of the .fdraw zip container."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        member = None
        for name in names:
            if name.lower().endswith("render.json"):
                member = name
                break
        if member is None:
            from fourier_prompt.fdraw import unpack  # type: ignore
            doc = unpack(path).get("render")
            if not isinstance(doc, dict):
                raise ValueError(f"{path}: 缺少 render.json")
            return doc
        data = zf.read(member)
    doc = json.loads(data)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: render.json 顶层不是对象")
    return doc


def _load_input(args) -> tuple[str, dict | None, list | None]:
    if args.fdraw:
        return "fdraw", _read_fdraw_render_json(args.fdraw), None
    if args.fbs:
        doc = json.load(open(args.fbs, encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError(f"{args.fbs}: 顶层不是 JSON 对象")
        return "fbs", doc, None
    if args.render_json:
        doc = json.load(open(args.render_json, encoding="utf-8"))
        return "render-json", doc, None
    if args.strokes:
        data = json.load(open(args.strokes, encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("strokes") or []
        if not isinstance(data, list):
            raise ValueError(f"{args.strokes}: 需要 strokes 列表")
        return "strokes", None, data
    raise ValueError("必须给出 --fdraw / --fbs / --render-json / --strokes 之一")


# --------------------------------------------------------------------------- sampling
def _sampled_from_doc(doc: dict, resolution: int, width_scale: float | None):
    """Reuse the existing CPU sampler: returns ordered list of sampled strokes."""
    from fbs_core.render import sample_stroke, _output_size  # type: ignore

    W, H, R = _output_size(doc, resolution)
    strokes = doc.get("strokes") or []
    n_fill = sum(1 for s in strokes
                 if isinstance(s, dict) and s.get("fill_role") == "region_fill")
    if n_fill:
        raise UnsupportedDoc(
            f"文档包含 {n_fill} 条 region_fill 填充，GPU 光栅器暂不支持（请用 CPU 渲染）")
    order = sorted(
        range(len(strokes)),
        key=lambda i: (int((strokes[i] or {}).get("layer", 0) or 0), i),
    )
    ws = 1.0 if width_scale is None else float(width_scale)
    to_float = lambda v: float(v)  # noqa: E731
    sampled = []
    shapes_seen = set()
    for i in order:
        st = strokes[i]
        if not isinstance(st, dict) or not st.get("coef"):
            continue
        smp = sample_stroke(doc, st, R, ws)
        pts = np.asarray(smp["points_world"], dtype=np.float64) * R
        radii = np.asarray(smp["radii_world"], dtype=np.float64) * R
        rgb = np.asarray(smp["rgb"], dtype=np.float64)
        alpha = np.asarray(smp["alpha"], dtype=np.float64)
        if smp["closed"] and len(pts) > 0:
            pts = np.vstack([pts, pts[:1]])
            radii = np.concatenate([radii, radii[:1]])
            rgb = np.vstack([rgb, rgb[:1]])
            alpha = np.concatenate([alpha, alpha[:1]])
        for sh in smp["shapes"]:
            shapes_seen.add(str(sh))
        if len(alpha) and float(np.min(alpha)) < 0.999:
            raise AlphaUnsupported()
        if len(pts) >= 1:
            sampled.append((pts, np.maximum(radii, 0.0), rgb, alpha))
    return W, H, R, sampled, sorted(shapes_seen)


def _sampled_from_strokes(strokes: list, resolution: int):
    """Raw layered_base stroke JSON -> per-stroke sampled geometry."""
    sampled = []
    R = int(resolution)
    for st in strokes:
        if not isinstance(st, dict):
            continue
        pts = np.asarray(st.get("points") or [], dtype=np.float64).reshape(-1, 2)
        if len(pts) == 0:
            continue
        pts = pts * R
        widths = np.asarray(st.get("widths") or [], dtype=np.float64).reshape(-1)
        if len(widths) == 1:
            widths = np.full(len(pts), widths[0])
        widths = np.maximum(widths * R, 0.0)
        n = len(pts)
        grad = st.get("gradient") or st.get("color_gradient")
        if grad:
            c0 = np.asarray(grad["start"], dtype=np.float64)
            c1 = np.asarray(grad["end"], dtype=np.float64)
            seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
            cum = np.concatenate([[0.0], np.cumsum(seg)])
            u = (cum / max(float(cum[-1]), 1e-9))[:, None]
            rgb = c0[None, :] * (1.0 - u) + c1[None, :] * u
        else:
            col = np.asarray(st.get("color") or [0.5, 0.5, 0.5], dtype=np.float64).reshape(-1)[:3]
            if float(np.max(np.abs(col))) > 1.5:
                col = col / 255.0
            rgb = np.broadcast_to(np.clip(col, 0.0, 1.0), (n, 3)).copy()
        a = float(st.get("alpha", 1.0))
        if a < 0.999:
            raise AlphaUnsupported()
        alpha = np.full(n, np.clip(a, 0.0, 1.0))
        sampled.append((pts, widths * 0.5, rgb, alpha))
    return R, R, R, sampled, ["round"]


def _segment_arrays(sampled: list, supersample: int):
    """Flatten sampled strokes into per-segment instance attributes (SS pixels)."""
    lengths = np.asarray([len(s[0]) for s in sampled], dtype=np.int64)
    if len(lengths) == 0 or int(lengths.sum()) < 2:
        raise ValueError("没有可渲染的笔画")
    P = np.concatenate([s[0] for s in sampled]) * float(supersample)
    RAD = np.concatenate([s[1] for s in sampled]) * float(supersample)
    RGB = np.concatenate([s[2] for s in sampled])
    AL = np.concatenate([s[3] for s in sampled])
    sid = np.repeat(np.arange(len(sampled)), lengths)
    same = sid[:-1] == sid[1:]
    a = P[:-1][same]
    b = P[1:][same]
    ra = RAD[:-1][same]
    rb = RAD[1:][same]
    c0 = RGB[:-1][same]
    c1 = RGB[1:][same]
    a0 = AL[:-1][same]
    a1 = AL[1:][same]
    rr = 0.5 * (ra + rb)
    keep = rr > 0.0
    a, b, rr = a[keep], b[keep], rr[keep]
    c0, c1, a0, a1 = c0[keep], c1[keep], a0[keep], a1[keep]
    # single-point strokes: paint them as a zero-length segment (a == b)
    sing = np.nonzero(lengths == 1)[0]
    if len(sing):
        sp = P[np.searchsorted(np.cumsum(lengths), sing)]
        sr = RAD[np.searchsorted(np.cumsum(lengths), sing)]
        sc0 = RGB[np.searchsorted(np.cumsum(lengths), sing)]
        sa = AL[np.searchsorted(np.cumsum(lengths), sing)]
        keep2 = sr > 0.0
        if bool(keep2.any()):
            a = np.vstack([a, sp[keep2]])
            b = np.vstack([b, sp[keep2]])
            rr = np.concatenate([rr, sr[keep2]])
            c0 = np.vstack([c0, sc0[keep2]])
            c1 = np.vstack([c1, sc0[keep2]])
            a0 = np.concatenate([a0, sa[keep2]])
            a1 = np.concatenate([a1, sa[keep2]])
    inst = np.empty((len(rr), 11), dtype=np.float32)
    inst[:, 0:2] = a
    inst[:, 2:4] = b
    inst[:, 4] = rr.astype(np.float32)
    inst[:, 5:8] = c0
    inst[:, 8:11] = c1
    alpha = np.empty((len(rr), 2), dtype=np.float32)
    alpha[:, 0] = a0
    alpha[:, 1] = a1
    return inst, alpha


# --------------------------------------------------------------------------- OpenGL
class GLRenderer:
    def __init__(self, width: int, height: int, supersample: int,
                 background: tuple[float, float, float]):
        import glfw  # type: ignore
        from OpenGL import GL  # type: ignore

        self.GL = GL
        self.glfw = glfw
        self.Wss = int(width) * int(supersample)
        self.Hss = int(height) * int(supersample)
        if not glfw.init():
            raise RuntimeError("glfw.init() 失败（无 GLFW 运行时/显示）")
        glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        self.win = glfw.create_window(64, 64, "gpu_render", None, None)
        if not self.win:
            glfw.terminate()
            raise RuntimeError("无法创建隐藏 GL 窗口（需要桌面 OpenGL 上下文）")
        glfw.make_context_current(self.win)
        GL.glViewport(0, 0, self.Wss, self.Hss)
        # float framebuffer for quiet blending (fallback to RGBA8 if unsupported)
        self.tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        self.float_fbo = False
        try:
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA16F, self.Wss, self.Hss,
                            0, GL.GL_RGBA, GL.GL_FLOAT, None)
            self.float_fbo = True
        except Exception:
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, self.Wss, self.Hss,
                            0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        self.fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                  GL.GL_TEXTURE_2D, self.tex, 0)
        status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
        if status != GL.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError(f"FBO 不完整: {status}")
        GL.glClearColor(background[0], background[1], background[2], 1.0)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        self.prog = self._build_program(VERT_SRC, FRAG_SRC)
        self.u_size = GL.glGetUniformLocation(self.prog, "u_size")
        # quad corners (triangle strip)
        self.tri_vbo = GL.glGenBuffers(1)
        corners = np.asarray([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
                             dtype=np.float32)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.tri_vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, corners.nbytes, corners, GL.GL_STATIC_DRAW)
        self.inst_vbo = GL.glGenBuffers(1)
        self.alpha_vbo = GL.glGenBuffers(1)
        self.vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self.vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.tri_vbo)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.inst_vbo)
        stride = 11 * 4
        for loc, size, off in ((1, 2, 0), (2, 2, 8), (3, 1, 16), (4, 3, 20),
                               (5, 3, 32)):
            GL.glEnableVertexAttribArray(loc)
            GL.glVertexAttribPointer(loc, size, GL.GL_FLOAT, GL.GL_FALSE, stride,
                                     ctypes.c_void_p(off))
            GL.glVertexAttribDivisor(loc, 1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.alpha_vbo)
        for loc, off in ((6, 0), (7, 4)):
            GL.glEnableVertexAttribArray(loc)
            GL.glVertexAttribPointer(loc, 1, GL.GL_FLOAT, GL.GL_FALSE, 8,
                                     ctypes.c_void_p(off))
            GL.glVertexAttribDivisor(loc, 1)
        GL.glBindVertexArray(0)

    def _build_program(self, vert_src: str, frag_src: str):
        GL = self.GL
        from OpenGL import GL as _GL  # noqa: F401
        vs = GL.glCreateShader(GL.GL_VERTEX_SHADER)
        GL.glShaderSource(vs, vert_src)
        GL.glCompileShader(vs)
        if not GL.glGetShaderiv(vs, GL.GL_COMPILE_STATUS):
            raise RuntimeError("vertex shader 编译失败: " + GL.glGetShaderInfoLog(vs).decode())
        fs = GL.glCreateShader(GL.GL_FRAGMENT_SHADER)
        GL.glShaderSource(fs, frag_src)
        GL.glCompileShader(fs)
        if not GL.glGetShaderiv(fs, GL.GL_COMPILE_STATUS):
            raise RuntimeError("fragment shader 编译失败: " + GL.glGetShaderInfoLog(fs).decode())
        prog = GL.glCreateProgram()
        GL.glAttachShader(prog, vs)
        GL.glAttachShader(prog, fs)
        GL.glLinkProgram(prog)
        if not GL.glGetProgramiv(prog, GL.GL_LINK_STATUS):
            raise RuntimeError("shader link 失败: " + GL.glGetProgramInfoLog(prog).decode())
        return prog

    def draw(self, inst: np.ndarray, alpha: np.ndarray) -> np.ndarray:
        GL = self.GL
        n = int(inst.shape[0])
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glViewport(0, 0, self.Wss, self.Hss)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glUseProgram(self.prog)
        GL.glUniform2f(self.u_size, float(self.Wss), float(self.Hss))
        GL.glBindVertexArray(self.vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.inst_vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, inst.nbytes, inst, GL.GL_STREAM_DRAW)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.alpha_vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, alpha.nbytes, alpha, GL.GL_STREAM_DRAW)
        GL.glDrawArraysInstanced(GL.GL_TRIANGLE_STRIP, 0, 4, n)
        GL.glFinish()
        if self.float_fbo:
            buf = GL.glReadPixels(0, 0, self.Wss, self.Hss, GL.GL_RGBA, GL.GL_FLOAT)
            arr = np.frombuffer(buf, dtype=np.float32).reshape(self.Hss, self.Wss, 4)
        else:
            buf = GL.glReadPixels(0, 0, self.Wss, self.Hss, GL.GL_RGBA,
                                  GL.GL_UNSIGNED_BYTE)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(self.Hss, self.Wss, 4)
            arr = arr.astype(np.float32) / 255.0
        GL.glBindVertexArray(0)
        return arr

    def close(self):
        try:
            self.glfw.terminate()
        except Exception:
            pass


def _downsample_and_save(arr: np.ndarray, width: int, height: int,
                         supersample: int, out_png: str) -> tuple[int, int]:
    img = np.flipud(arr)[..., :3].astype(np.float64)
    S = int(supersample)
    if S > 1:
        img = img.reshape(height, S, width, S, 3).mean(axis=(1, 3))
    img = np.clip(img, 0.0, 1.0)
    q = np.clip(img * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    from PIL import Image
    Image.fromarray(q).save(out_png)
    return int(q.shape[1]), int(q.shape[0])


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="OpenGL GPU rasteriser for .fdraw/.fbs/strokes")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fdraw", help="input .fdraw container")
    src.add_argument("--fbs", help="input .fbs JSON")
    src.add_argument("--render-json", help="input render.json")
    src.add_argument("--strokes", help="raw layered_base strokes JSON")
    ap.add_argument("--out", required=True, help="output PNG")
    ap.add_argument("--resolution", type=int, default=1024, help="long side in px")
    ap.add_argument("--supersample", type=int, default=2, choices=(1, 2, 3))
    ap.add_argument("--device", choices=("gl", "cpu"), default="gl")
    ap.add_argument("--background", default=None, help="R,G,B / #RRGGBB / white")
    ap.add_argument("--width-scale", type=float, default=None)
    ap.add_argument("--timing-json", default=None,
                    help="optional timing JSON path; fields: load_s, sample_s, "
                         "geometry_s, gl_init_s, gpu_s, readback_s, total_s "
                         "(+ strokes/segments/out_size/device/float_fbo)")
    ap.epilog = ("timing JSON field names are stable: load_s / sample_s / "
                 "geometry_s / gl_init_s / gpu_s / readback_s / total_s.  "
                 "Exit codes: 0 ok, 2 input error, 3 no GL (caller should fall "
                 "back to CPU), 4 unsupported document (region fills), "
                 "5 alpha<1 strokes (GPU covers alpha=1 documents only; "
                 "use --device cpu).")
    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    t0 = time.perf_counter()
    timing: dict[str, float | int | str | bool | None] = {}

    # ---- load ------------------------------------------------------------
    try:
        kind, doc, strokes = _load_input(args)
        bg = _parse_background(args.background)
    except UnsupportedDoc as exc:
        _log(str(exc))
        return EXIT_UNSUPPORTED
    except AlphaUnsupported:
        _log("alpha<1 not supported by GPU renderer; use --device cpu "
             "(nodes fall back to the CPU exact renderer automatically)")
        return EXIT_ALPHA
    except Exception as exc:
        _log(f"输入错误: {exc}")
        return EXIT_INPUT
    t_load = time.perf_counter() - t0
    timing["load_s"] = round(t_load, 3)
    timing["input"] = kind

    # ---- cpu mode (delegates to the existing exact CPU renderer) ---------
    if args.device == "cpu":
        try:
            t = time.perf_counter()
            if doc is not None:
                from fbs_core.render import render as cpu_render  # type: ignore
                bg255 = tuple(int(round(v * 255.0)) for v in bg)
                img = cpu_render(doc, resolution=args.resolution,
                                 supersample=args.supersample,
                                 width_scale=args.width_scale, background=bg255)
            else:
                # raw layered_base stroke JSON: reuse the skill's CPU renderer
                import hatch_gen as hgr  # type: ignore
                R = int(args.resolution)
                conv = []
                for st in strokes:
                    if not isinstance(st, dict):
                        continue
                    d = dict(st)
                    d["points"] = np.asarray(d.get("points") or [], dtype=np.float64)
                    if len(d["points"]) == 0:
                        continue
                    wd = np.asarray(d.get("widths") or [], dtype=np.float64).reshape(-1)
                    if len(wd) == 1:
                        wd = np.full(len(d["points"]), wd[0])
                    d["widths"] = wd
                    d["color"] = np.asarray(d.get("color") or [0.5, 0.5, 0.5],
                                            dtype=np.float64).reshape(-1)[:3]
                    if float(np.max(np.abs(d["color"]))) > 1.5:
                        d["color"] = d["color"] / 255.0
                    d["layer"] = int(d.get("layer", 0) or 0)
                    d["alpha"] = float(d.get("alpha", 1.0))
                    conv.append(d)
                if not conv:
                    raise ValueError("--strokes 文件里没有可渲染的笔画")
                rgb_f = hgr._render_strokes(conv, R, R, ss=int(args.supersample))
                img = np.clip(np.asarray(rgb_f) * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
            from PIL import Image
            Image.fromarray(img).save(args.out)
            timing.update({"render_s": round(time.perf_counter() - t, 3),
                           "device": "cpu"})
        except UnsupportedDoc as exc:
            _log(str(exc))
            return EXIT_UNSUPPORTED
        except Exception as exc:
            _log(f"CPU 渲染失败: {exc}")
            return EXIT_INPUT
        timing["total_s"] = round(time.perf_counter() - t0, 3)
        print(json.dumps({"ok": True, "out": os.path.abspath(args.out),
                          "timing": timing}, ensure_ascii=False, sort_keys=True))
        if args.timing_json:
            json.dump(timing, open(args.timing_json, "w", encoding="utf-8"), indent=1)
        return EXIT_OK

    # ---- GL availability -------------------------------------------------
    # Defensive retry: if the module-level cleaning did not apply (non-Windows
    # host, or PYOPENGL_PLATFORM set after import), drop a hostile platform
    # hint once and retry.  Only a real second failure yields exit 3.
    def _import_gl():
        import glfw  # noqa: F401
        from OpenGL import GL  # noqa: F401
        return glfw, GL

    try:
        _import_gl()
    except Exception as exc:  # noqa: BLE001
        first_error = exc
        platform_hint = os.environ.get("PYOPENGL_PLATFORM")
        if platform_hint:
            os.environ.pop("PYOPENGL_PLATFORM", None)
            try:
                _import_gl()
            except Exception as exc2:  # noqa: BLE001
                _log("GPU 渲染不可用（PyOpenGL/glfw 导入失败；已尝试清除 "
                     f"PYOPENGL_PLATFORM={platform_hint!r}）："
                     f"first={first_error!r}; retry={exc2!r}；"
                     "请用 --device cpu 或回退 CPU")
                return EXIT_NO_GL
        else:
            _log(f"GPU 渲染不可用（PyOpenGL/glfw 缺失: {first_error}）；"
                 "请用 --device cpu 或回退 CPU")
            return EXIT_NO_GL

    # ---- sample (reuse the CPU sampler) ----------------------------------
    try:
        t = time.perf_counter()
        if doc is not None:
            W, H, R, sampled, shapes = _sampled_from_doc(doc, args.resolution,
                                                         args.width_scale)
        else:
            W, H, R, sampled, shapes = _sampled_from_strokes(strokes, args.resolution)
        t_sample = time.perf_counter() - t
        timing["sample_s"] = round(t_sample, 3)
        timing["strokes"] = len(sampled)
        timing["resolution"] = [W, H]
        timing["non_round_shapes"] = [s for s in shapes if s not in ("round",)]
    except UnsupportedDoc as exc:
        _log(str(exc))
        return EXIT_UNSUPPORTED
    except AlphaUnsupported:
        _log("alpha<1 not supported by GPU renderer; use --device cpu")
        return EXIT_ALPHA
    except Exception as exc:
        _log(f"采样失败: {exc}")
        return EXIT_INPUT

    # ---- geometry (vectorised segment arrays) ----------------------------
    try:
        t = time.perf_counter()
        inst, alpha = _segment_arrays(sampled, args.supersample)
        timing["segments"] = int(inst.shape[0])
        timing["geometry_s"] = round(time.perf_counter() - t, 3)
    except Exception as exc:
        _log(f"几何构造失败: {exc}")
        return EXIT_INPUT

    # ---- GPU --------------------------------------------------------------
    renderer = None
    try:
        t = time.perf_counter()
        try:
            renderer = GLRenderer(W, H, args.supersample, bg)
        except Exception as exc:
            _log(f"GL 初始化失败: {exc}")
            return EXIT_NO_GL
        t_init = time.perf_counter() - t
        t = time.perf_counter()
        arr = renderer.draw(inst, alpha)
        t_gpu = time.perf_counter() - t
        timing["gl_init_s"] = round(t_init, 3)
        timing["gpu_s"] = round(t_gpu, 3)
        timing["float_fbo"] = bool(renderer.float_fbo)
    finally:
        if renderer is not None:
            renderer.close()

    # ---- readback / downsample / save ------------------------------------
    t = time.perf_counter()
    out_w, out_h = _downsample_and_save(arr, W, H, args.supersample, args.out)
    timing["readback_s"] = round(time.perf_counter() - t, 3)
    timing["out_size"] = [out_w, out_h]
    timing["supersample"] = int(args.supersample)
    timing["device"] = "gl"
    timing["total_s"] = round(time.perf_counter() - t0, 3)
    print(json.dumps({"ok": True, "out": os.path.abspath(args.out),
                      "timing": timing}, ensure_ascii=False, sort_keys=True))
    if args.timing_json:
        json.dump(timing, open(args.timing_json, "w", encoding="utf-8"), indent=1)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
