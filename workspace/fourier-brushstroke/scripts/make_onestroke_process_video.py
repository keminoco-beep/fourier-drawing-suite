#!/usr/bin/env python
"""一笔画（one_stroke）傅里叶圆作画过程视频：绘制段 + 结尾静止展示帧（无旋转圆）。

- 单条傅里叶曲线连续绘制（build_path 一次 ifft），笔尖沿路径连续移动；
- 旋转圆链显示当前笔尖的傅里叶分量（全局系数的前 K 项）；
- 绘制完成后追加若干秒**静止帧**：只展示完整成图，不画旋转圆；
- 结束时与静态渲染核对，probe 校验帧数。
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fbs_core import io_format  # noqa: E402
from fbs_core import onestroke  # noqa: E402
from fbs_core import render as render_mod  # noqa: E402
from fbs_core import video as video_mod  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402

_BG = (250, 250, 252)
_INK = (52, 62, 84)
_SOFT = (150, 162, 186)
_TRACK = (218, 224, 236)
_ACCENT = (74, 120, 196)
_CIRCLE_EDGE = (132, 166, 212)
_CIRCLE_LINE = (168, 192, 224)
_TIP = (30, 38, 56)


def _draw_circles(frame: Image.Image, coef: list, pen_xy: tuple[float, float],
                  scale: float, phase: float, k_circles: int = 16) -> None:
    if not coef:
        return
    arr = np.asarray(coef, dtype=np.float64).reshape(-1, 3)
    if len(arr) == 0:
        return
    order = np.argsort(-np.abs(arr[:, 1] + 1j * arr[:, 2]))[:k_circles]
    ks = arr[order, 0]
    cs = (arr[order, 1] + 1j * arr[order, 2]) * float(scale)
    z = complex(0.0, 0.0)
    for k, c in zip(ks, cs):
        z += c * np.exp(2j * math.pi * float(k) * phase)
    tip = complex(pen_xy[0], pen_xy[1])
    cur = tip - z
    draw = ImageDraw.Draw(frame)
    for c in cs:
        r = float(abs(c))
        if r < 1.0:
            continue
        cx, cy = float(cur.real), float(cur.imag)
        if cx < -2000 or cy < -2000 or cx > 4000 or cy > 4000:
            break
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_CIRCLE_EDGE, width=1)
        nxt = cur + c
        draw.line([(cx, cy), (float(nxt.real), float(nxt.imag))], fill=_CIRCLE_LINE, width=1)
        cur = nxt
    draw.ellipse([tip.real - 4, tip.imag - 4, tip.real + 4, tip.imag + 4], fill=_TIP)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一笔画傅里叶圆过程视频 + 静止收尾")
    ap.add_argument("--fbs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--draw-seconds", type=float, default=None)
    ap.add_argument("--static-seconds", type=float, default=None)
    ap.add_argument("--fps", type=int, default=None)
    ap.add_argument("--size", type=int, default=None)
    ap.add_argument("--art", type=int, default=None)
    ap.add_argument("--circles", type=int, default=None)
    args = ap.parse_args(argv)
    draw_s = float(args.draw_seconds if args.draw_seconds is not None else 150.0)
    static_s = float(args.static_seconds if args.static_seconds is not None else 6.0)
    fps = int(args.fps if args.fps is not None else 60)
    frame_size = int(args.size if args.size is not None else 1080)
    art_long = int(args.art if args.art is not None else 1000)
    k_circles = int(args.circles if args.circles is not None else 16)

    doc = io_format.read_fbs(args.fbs)
    stroke = (doc.get("strokes") or [None])[0]
    if not isinstance(stroke, dict):
        print("需要 one_stroke 文档")
        return 2
    warn = WarningLog()
    path = onestroke.build_path(doc, art_long, warn=warn)
    pts = np.asarray(path["points_world"], dtype=np.float64) * float(art_long)
    widths = np.asarray(path["widths_world"], dtype=np.float64) * float(art_long)
    colors = np.asarray(path["rgb"], dtype=np.float64)
    alphas = np.asarray(path["alpha"], dtype=np.float64)
    shapes = list(path["shapes"])
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(arc[-1]) + float(np.linalg.norm(pts[0] - pts[-1]))
    W = int(doc["canvas"]["width"] * art_long) if doc.get("canvas", {}).get("width", 1.0) <= 1.0 else art_long
    # 画布尺寸：按路径 bbox 与文档 canvas 宽高比
    cw = float((doc.get("canvas") or {}).get("width", 1.0))
    ch = float((doc.get("canvas") or {}).get("height", 1.0))
    if cw > 0 and ch > 0 and abs(cw - 1.0) > 1e-6:
        Wc = int(round(art_long * cw / max(cw, ch)))
        Hc = int(round(art_long * ch / max(cw, ch)))
    else:
        Wc, Hc = art_long, art_long
    fc = render_mod.FloatCanvas(Wc, Hc, 1, (1.0, 1.0, 1.0))
    print(f"[video] path={len(pts)} total={total:.1f}px canvas={Wc}x{Hc}", flush=True)

    margin = 0.07
    aw = int(frame_size * (1 - 2 * margin))
    ah = int(aw * Hc / max(Wc, 1))
    if ah > int(frame_size * 0.80):
        ah = int(frame_size * 0.80)
        aw = int(ah * Wc / max(Hc, 1))
    art_size = (aw, ah)
    fx = (frame_size - aw) // 2
    fy = (frame_size - ah) // 2 + 24
    bar_w = frame_size - 48
    coef = stroke.get("coef") or []

    n_draw = max(2, int(round(draw_s * fps)))
    n_static = max(1, int(round(static_s * fps)))
    writer = video_mod._writer_or_raise(args.out, fps)
    prev_arc = 0.0
    try:
        for fi in range(n_draw):
            progress = (fi + 1) / float(n_draw)
            target = progress * float(arc[-1])
            idx = int(np.searchsorted(arc, target, side="right"))
            i0 = 0 if prev_arc <= 0 else int(np.searchsorted(arc, prev_arc, side="right"))
            if idx <= i0:
                idx = min(i0 + 1, len(pts))
            sub = pts[max(0, i0 - 1):idx]
            rad = widths[max(0, i0 - 1):idx] * 0.5
            col = colors[max(0, i0 - 1):idx]
            alp = alphas[max(0, i0 - 1):idx]
            shp = shapes[max(0, i0 - 1):idx]
            if len(sub) >= 2:
                fc.add_band(sub, rad, col, np.clip(alp, 0.0, 1.0), mode="sweep",
                            start_cap=(prev_arc <= 0), shapes=shp)
            prev_arc = target
            pen = pts[min(idx, len(pts) - 1)]
            phase = min(1.0, target / max(float(arc[-1]), 1e-9))
            art = Image.fromarray(fc.to_uint8()).resize(art_size, Image.LANCZOS)
            frame = Image.new("RGB", (frame_size, frame_size), _BG)
            frame.paste(art, (fx, fy))
            dr = ImageDraw.Draw(frame)
            dr.text((24, 14), "Fourier one-stroke - fine-line drawing", fill=_INK)
            dr.rectangle([24, 42, 24 + bar_w, 50], fill=_TRACK)
            dr.rectangle([24, 42, 24 + int(bar_w * progress), 50], fill=_ACCENT)
            dr.text((24, 56), f"drawing  {progress * 100:5.1f}%   phase {phase:.3f}", fill=_SOFT)
            _draw_circles(frame, coef, (fx + pen[0] * art_size[0] / Wc, fy + pen[1] * art_size[1] / Hc),
                          art_long * art_size[0] / Wc, phase, k_circles)
            writer.append_data(np.asarray(frame))
            if fi % 300 == 0:
                print(f"[video] frame {fi}/{n_draw}", flush=True)

        # 补画剩余路径（确保完整）
        tail_idx = int(np.searchsorted(arc, prev_arc, side="right"))
        if tail_idx < len(pts) - 1:
            fc.add_band(pts[tail_idx - 1:], widths[tail_idx - 1:] * 0.5,
                        colors[tail_idx - 1:], np.clip(alphas[tail_idx - 1:], 0, 1),
                        mode="sweep", start_cap=False, shapes=shapes[tail_idx - 1:])
        final_u8 = fc.to_uint8()
        Image.fromarray(final_u8).save(os.path.join(os.path.dirname(os.path.abspath(args.out)),
                                                    "onestroke_process_last_art.png"))
        art_final = Image.fromarray(final_u8).resize(art_size, Image.LANCZOS)
        # 静止收尾帧：完整成图，不画旋转圆
        for si in range(n_static):
            frame = Image.new("RGB", (frame_size, frame_size), _BG)
            frame.paste(art_final, (fx, fy))
            dr = ImageDraw.Draw(frame)
            dr.text((24, 14), "Fourier one-stroke - fine-line drawing", fill=_INK)
            dr.rectangle([24, 42, 24 + bar_w, 50], fill=_ACCENT)
            dr.text((24, 56), "complete", fill=_INK)
            writer.append_data(np.asarray(frame))
        print("[video] drawn+static frames done", flush=True)
    finally:
        writer.close()

    probe = video_mod.probe_video(args.out)
    print(f"[video] probe frames={probe.get('frames')} fps={probe.get('fps')} "
          f"duration={probe.get('duration_s')} size={probe.get('size')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
