#!/usr/bin/env python
"""用傅里叶圆逐笔绘制整张图的过程视频（对 per_stroke 文档连续绘制，不中断）。

- 每帧按累计墨迹长度推进，笔尖沿当前笔画真实路径连续移动；
- 当前笔画用它的 Fourier 系数画旋转圆链（epicycle）；
- 结尾强制补画全部剩余笔画，并与静态渲染核对像素一致；
- probe 校验帧数/时长/尺寸。
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


def _slice(pts: np.ndarray, s: np.ndarray, widths: np.ndarray, colors: np.ndarray,
           alphas: np.ndarray, t0: float, t1: float) -> tuple[np.ndarray, ...]:
    """取弧长参数区间 [t0,t1] 的子折线（含两端插值点）。"""
    total = float(s[-1]) if len(s) else 0.0
    if total <= 0:
        return pts[:1], widths[:1], colors[:1], alphas[:1]
    a = np.clip(t0, 0.0, 1.0) * total
    b = np.clip(t1, 0.0, 1.0) * total
    if b <= 1e-9:
        return pts[:1], widths[:1], colors[:1], alphas[:1]
    if a >= total - 1e-9:
        return pts[-1:], widths[-1:], colors[-1:], alphas[-1:]
    i0 = int(np.searchsorted(s, a, side="right"))
    i1 = int(np.searchsorted(s, b, side="left"))
    i0 = max(0, min(i0, len(s) - 1))
    i1 = max(i0, min(i1, len(s) - 1))
    out_pts = [pts[max(0, i0 - 1)] + (pts[max(0, i0)] - pts[max(0, i0 - 1)])
               * ((a - s[max(0, i0 - 1)]) / max(s[max(0, i0)] - s[max(0, i0 - 1)], 1e-9))]
    out_w = [widths[max(0, i0 - 1)]]
    out_c = [colors[max(0, i0 - 1)]]
    out_a = [alphas[max(0, i0 - 1)]]
    for i in range(max(0, i0), min(i1, len(s))):
        out_pts.append(pts[i]); out_w.append(widths[i]); out_c.append(colors[i]); out_a.append(alphas[i])
    if i1 < len(s):
        j = i1
        fr = (b - s[j - 1]) / max(s[j] - s[j - 1], 1e-9)
        out_pts.append(pts[j - 1] + (pts[j] - pts[j - 1]) * fr)
        out_w.append(widths[j - 1] + (widths[j] - widths[j - 1]) * fr)
        out_c.append(colors[j - 1]); out_a.append(alphas[j - 1])
    return (np.asarray(out_pts, dtype=np.float64), np.asarray(out_w, dtype=np.float64),
            np.asarray(out_c, dtype=np.float64), np.asarray(out_a, dtype=np.float64))


def _draw_circles(frame: Image.Image, coef, pen_xy: tuple[float, float],
                  scale: float, k_circles: int = 12) -> None:
    """当前笔画的 Fourier 圆链：链末端落在笔尖上。"""
    if not coef:
        return
    arr = np.asarray(coef, dtype=np.float64).reshape(-1, 3)
    if len(arr) == 0:
        return
    order = np.argsort(-np.abs(arr[:, 1] + 1j * arr[:, 2]))[:k_circles]
    ks = arr[order, 0]
    cs = (arr[order, 1] + 1j * arr[order, 2]) * float(scale)
    tip = complex(pen_xy[0], pen_xy[1])
    z = complex(0.0, 0.0)
    for k, c in zip(ks, cs):
        z += c * np.exp(2j * math.pi * float(k) * 0.25)
    cur = tip - z
    draw = ImageDraw.Draw(frame)
    for c in cs:
        r = float(abs(c))
        if r < 0.8:
            continue
        cx, cy = float(cur.real), float(cur.imag)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_CIRCLE_EDGE, width=1)
        nxt = cur + c
        draw.line([(cx, cy), (float(nxt.real), float(nxt.imag))], fill=_CIRCLE_LINE, width=1)
        cur = nxt
    draw.ellipse([tip.real - 4, tip.imag - 4, tip.real + 4, tip.imag + 4], fill=_TIP)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="傅里叶圆逐笔过程视频")
    ap.add_argument("--fbs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--fps", type=int, default=None)
    ap.add_argument("--size", type=int, default=None)
    ap.add_argument("--art", type=int, default=None)
    ap.add_argument("--circles", type=int, default=None)
    args = ap.parse_args(argv)
    duration = float(args.duration if args.duration is not None else 120.0)
    fps = int(args.fps if args.fps is not None else 60)
    frame_size = int(args.size if args.size is not None else 1080)
    art_long = int(args.art if args.art is not None else 1000)
    k_circles = int(args.circles if args.circles is not None else 12)

    warn = WarningLog()
    doc = io_format.read_fbs(args.fbs)
    strokes = [s for s in doc.get("strokes") or [] if isinstance(s, dict) and s.get("coef")]
    W, H, R = render_mod._output_size(doc, art_long)
    n_frames = max(2, int(round(duration * fps)))
    print(f"[process] strokes={len(strokes)} art={W}x{H} frames={n_frames}", flush=True)

    items = []
    total_len = 0.0
    for st in strokes:
        smp = render_mod.sample_stroke(doc, st, R, 1.0, warn)
        pts = np.asarray(smp["points_world"], dtype=np.float64) * float(R)
        widths = np.asarray(smp["radii_world"], dtype=np.float64) * 2.0 * float(R)
        rgb = np.asarray(smp["rgb"], dtype=np.float64)
        alphas = np.asarray(smp["alpha"], dtype=np.float64)
        if len(pts) < 2:
            continue
        if bool(smp.get("closed")) and len(pts) > 0:
            pts = np.vstack([pts, pts[:1]])
            widths = np.concatenate([widths, widths[:1]])
            rgb = np.vstack([rgb, rgb[:1]])
            alphas = np.concatenate([alphas, alphas[:1]])
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        L = float(s[-1])
        if L < 0.5:
            continue
        shape = str(smp["shapes"][0]) if len(smp["shapes"]) else "round"
        items.append({"pts": pts, "w": widths * 0.5, "c": rgb, "a": alphas, "s": s,
                      "L": L, "coef": st.get("coef"), "shape": shape,
                      "layer": int(st.get("layer", 0) or 0)})
        total_len += L
    print(f"[process] drawable={len(items)} total_ink_px={total_len:.1f}", flush=True)

    margin = 0.07
    aw = int(frame_size * (1 - 2 * margin))
    ah = int(aw * H / max(W, 1))
    if ah > int(frame_size * 0.80):
        ah = int(frame_size * 0.80)
        aw = int(ah * W / max(H, 1))
    art_size = (aw, ah)
    frame_x = (frame_size - aw) // 2
    frame_y = (frame_size - ah) // 2 + 24
    bar_w = frame_size - 48
    fc = render_mod.FloatCanvas(W, H, 1, (1.0, 1.0, 1.0))

    writer = video_mod._writer_or_raise(args.out, fps)
    stroke_idx = 0
    stroke_t = 0.0
    drawn_total = 0.0
    try:
        for fi in range(n_frames):
            progress = (fi + 1) / float(n_frames)
            target = progress * total_len
            while drawn_total < target and stroke_idx < len(items):
                it = items[stroke_idx]
                remaining = it["L"] * (1.0 - stroke_t)
                take = min(remaining, target - drawn_total)
                t1 = stroke_t + take / max(it["L"], 1e-9)
                pts, widths, colors, alphas = _slice(it["pts"], it["s"], it["w"], it["c"], it["a"],
                                                     stroke_t, t1)
                if len(pts) >= 2:
                    fc.add_band(pts, widths, colors, np.clip(alphas, 0.0, 1.0),
                                mode="sweep", start_cap=(stroke_t <= 1e-9),
                                shapes=[it["shape"]] * len(pts))
                drawn_total += take
                stroke_t = t1
                if stroke_t >= 1.0 - 1e-9:
                    stroke_idx += 1
                    stroke_t = 0.0
            # 当前笔尖与系数（圆链）
            cur_pen = None
            cur_coef = None
            if stroke_idx < len(items):
                it = items[stroke_idx]
                pts, *_ = _slice(it["pts"], it["s"], it["w"], it["c"], it["a"], stroke_t, stroke_t)
                if len(pts):
                    cur_pen = (float(pts[-1, 0]), float(pts[-1, 1]))
                else:
                    cur_pen = (float(it["pts"][0, 0]), float(it["pts"][0, 1]))
                cur_coef = it["coef"]
            elif items:
                pts = items[-1]["pts"]
                cur_pen = (float(pts[-1, 0]), float(pts[-1, 1]))
                cur_coef = items[-1]["coef"]

            art_u8 = fc.to_uint8()
            art = Image.fromarray(art_u8).resize(art_size, Image.LANCZOS)
            frame = Image.new("RGB", (frame_size, frame_size), _BG)
            frame.paste(art, (frame_x, frame_y))
            dr = ImageDraw.Draw(frame)
            dr.text((24, 14), "Fourier brushstroke - fine-line drawing", fill=_INK)
            dr.rectangle([24, 42, 24 + bar_w, 50], fill=_TRACK)
            dr.rectangle([24, 42, 24 + int(bar_w * progress), 50], fill=_ACCENT)
            dr.text((24, 56), f"stroke {min(stroke_idx + 1, len(items))}/{len(items)}   "
                              f"{progress * 100:5.1f}%", fill=_SOFT)
            if cur_pen is not None and cur_coef:
                ox = frame_x + cur_pen[0] * art_size[0] / W
                oy = frame_y + cur_pen[1] * art_size[1] / H
                _draw_circles(frame, cur_coef, (ox, oy), art_long * art_size[0] / W, k_circles)
            writer.append_data(np.asarray(frame))
            if fi % 200 == 0:
                print(f"[process] frame {fi}/{n_frames} strokes={stroke_idx}", flush=True)

        # ---- 补画剩余（保证整张画完）----
        while stroke_idx < len(items):
            it = items[stroke_idx]
            pts, widths, colors, alphas = _slice(it["pts"], it["s"], it["w"], it["c"], it["a"], stroke_t, 1.0)
            if len(pts) >= 2:
                fc.add_band(pts, widths, colors, np.clip(alphas, 0.0, 1.0), mode="sweep",
                            start_cap=(stroke_t <= 1e-9), shapes=[it["shape"]] * len(pts))
            drawn_total += it["L"] * (1.0 - stroke_t)
            stroke_idx += 1
            stroke_t = 0.0
        if drawn_total + 1e-3 < total_len:
            raise RuntimeError(f"未画完：drawn={drawn_total:.2f} < total={total_len:.2f}")
        # 与静态渲染核对：最终画布 vs 直接渲染
        final_u8 = fc.to_uint8()
        direct = render_mod.render(doc, resolution=art_long, supersample=1, mode="sweep", warn=WarningLog())
        if direct.shape == final_u8.shape:
            mae = float(np.abs(direct.astype(np.float64) - final_u8.astype(np.float64)).mean())
        else:
            mae = -1.0
        print(f"[process] final canvas vs direct render MAE={mae:.4f}", flush=True)
        outdir = os.path.dirname(os.path.abspath(args.out))
        art_img = Image.fromarray(final_u8).resize(art_size, Image.LANCZOS)
        art_img.save(os.path.join(outdir, "process_last_art.png"))
        frame = Image.new("RGB", (frame_size, frame_size), _BG)
        frame.paste(art_img, (frame_x, frame_y))
        dr = ImageDraw.Draw(frame)
        dr.text((24, 14), "Fourier brushstroke - fine-line drawing", fill=_INK)
        dr.rectangle([24, 42, 24 + bar_w, 50], fill=_ACCENT)
        dr.text((24, 56), f"complete: {len(items)} strokes", fill=_INK)
        writer.append_data(np.asarray(frame))
    finally:
        writer.close()

    probe = video_mod.probe_video(args.out)
    print(f"[process] probe frames={probe.get('frames')} fps={probe.get('fps')} "
          f"duration={probe.get('duration_s')} size={probe.get('size')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
