"""GPU 快速评估渲染（PyTorch/CUDA）：训练打分用，不是最终交付渲染。

背景：训练每轮瓶颈是 CPU 上逐笔光栅化（~2000 笔 × 变宽 capsule + 纹理核，20–35s/次）。
GPU 版把笔触光栅化搬到 CUDA：每笔在局部 bbox 内用向量化 capsule 覆盖率（圆盘并集语义，
近似 CPU 的 union-max + 一次合成），顺序合成到画布；只用于"选笔打分"，
最终交付图仍由 CPU renderer 全质量渲染。

约定：
- 只支持 per_stroke 且不含 region_fill 的文档（structpaint 训练文档满足）；
  其他文档抛 GpuEvalUnsupported，由调用方回退 CPU。
- 笔形纹理（bristle/chalk/...）在 GPU 评估里近似为圆头（实测纹理对 768 图 PSNR 影响 ~0.02dB）。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

_GpuUnsupportedMsg = "GPU 评估渲染不支持该文档（需要 per_stroke 且无 region_fill）"


class GpuEvalUnsupported(RuntimeError):
    pass


def gpu_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _as_float01(arr: Any) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float64).reshape(-1)
    if a.size < 3:
        a = np.resize(a, 3)
    return a[:3]


def render_gpu(
    doc: dict,
    resolution: int,
    supersample: int = 2,
    device: str = "cuda",
) -> np.ndarray:
    """GPU 渲染 → (H,W,3) uint8；语义为"近似 CPU renderer 的打分渲染"。"""
    import torch
    from .render import _output_size, sample_stroke  # 延迟导入避免循环
    from .util import WarningLog

    if not isinstance(doc, dict):
        raise GpuEvalUnsupported(_GpuUnsupportedMsg)
    if doc.get("mode") == "one_stroke":
        raise GpuEvalUnsupported(_GpuUnsupportedMsg)
    for st in doc.get("strokes") or []:
        if isinstance(st, dict) and st.get("fill_role") == "region_fill":
            raise GpuEvalUnsupported(_GpuUnsupportedMsg)

    W, H, R = _output_size(doc, resolution)
    S = max(1, int(supersample))
    Ws, Hs = W * S, H * S
    bg = _as_float01(doc.get("base_color") if doc.get("base_color") is not None else [1.0, 1.0, 1.0])
    canvas = torch.empty((Hs, Ws, 3), dtype=torch.float32, device=device)
    canvas[...] = torch.tensor(bg, dtype=torch.float32, device=device)
    order = sorted(
        range(len(doc.get("strokes") or [])),
        key=lambda i: (int((doc["strokes"][i] or {}).get("layer", 0) or 0),
                       0 if (doc["strokes"][i] or {}).get("fill_role") == "region_fill" else 1, i),
    )
    warn = WarningLog()
    for idx in order:
        st = doc["strokes"][idx]
        if not isinstance(st, dict) or not st.get("coef"):
            continue
        smp = sample_stroke(doc, st, R, 1.0, warn)
        pts = np.asarray(smp["points_world"], dtype=np.float64) * R * S
        radii = np.asarray(smp["radii_world"], dtype=np.float64) * R * S
        rgb = np.asarray(smp["rgb"], dtype=np.float64)
        alpha = np.asarray(smp["alpha"], dtype=np.float64)
        if pts.shape[0] < 2:
            continue
        if smp.get("closed") and len(pts) > 0:
            pts = np.vstack([pts, pts[:1]])
            radii = np.concatenate([radii, radii[:1]])
            rgb = np.vstack([rgb, rgb[:1]])
            alpha = np.concatenate([alpha, alpha[:1]])
        rmax = float(np.max(radii))
        if rmax <= 0.0 or not math.isfinite(rmax):
            continue
        x0 = max(0, int(math.floor(float(pts[:, 0].min()) - rmax)) - 1)
        x1 = min(Ws, int(math.ceil(float(pts[:, 0].max()) + rmax)) + 2)
        y0 = max(0, int(math.floor(float(pts[:, 1].min()) - rmax)) - 1)
        y1 = min(Hs, int(math.ceil(float(pts[:, 1].max()) + rmax)) + 2)
        if x1 <= x0 or y1 <= y0:
            continue
        xs = (torch.arange(x0, x1, dtype=torch.float32, device=device) + 0.5)
        ys = (torch.arange(y0, y1, dtype=torch.float32, device=device) + 0.5)
        cov = torch.zeros((y1 - y0, x1 - x0), dtype=torch.float32, device=device)
        for k in range(len(pts) - 1):
            ra, rb = float(radii[k]), float(radii[k + 1])
            if ra <= 0.0 and rb <= 0.0:
                continue
            ax, ay = float(pts[k, 0]), float(pts[k, 1])
            bx, by = float(pts[k + 1, 0]), float(pts[k + 1, 1])
            seg_rmax = max(ra, rb)
            sx0 = max(0, int(math.floor(min(ax, bx) - seg_rmax)) - x0 - 1)
            sx1 = min(x1 - x0, int(math.ceil(max(ax, bx) + seg_rmax)) - x0 + 2)
            sy0 = max(0, int(math.floor(min(ay, by) - seg_rmax)) - y0 - 1)
            sy1 = min(y1 - y0, int(math.ceil(max(ay, by) + seg_rmax)) - y0 + 2)
            if sx1 <= sx0 or sy1 <= sy0:
                continue
            X = xs[sx0:sx1][None, :] - ax
            Y = ys[sy0:sy1][:, None] - ay
            dx, dy = bx - ax, by - ay
            seg_len2 = dx * dx + dy * dy
            if seg_len2 <= 1e-12:
                dist = torch.sqrt(X * X + Y * Y)
                cov_seg = torch.clamp((seg_rmax + 0.5) - dist, min=0.0)
            else:
                t = torch.clamp((X * dx + Y * dy) / seg_len2, 0.0, 1.0)
                px = X - t * dx
                py = Y - t * dy
                dist = torch.sqrt(px * px + py * py)
                rr = ra + (rb - ra) * t
                cov_seg = torch.clamp((rr + 0.5) - dist, min=0.0)
            cov[sy0:sy1, sx0:sx1] = torch.maximum(cov[sy0:sy1, sx0:sx1], cov_seg)
        cov = torch.clamp(cov, 0.0, 1.0)
        a = float(np.clip(float(np.mean(alpha)), 0.0, 1.0))
        if a <= 0.0:
            continue
        color = torch.tensor(rgb.mean(axis=0), dtype=torch.float32, device=device)
        eff = (cov * a)[..., None]
        sub = canvas[y0:y1, x0:x1]
        sub.mul_(1.0 - eff)
        sub.add_(color[None, None, :] * eff)
    if S > 1:
        canvas = canvas.view(H, S, W, S, 3).mean(dim=(1, 3))
    out = torch.clamp(canvas * 255.0, 0.0, 255.0).round().to(torch.uint8)
    return out.detach().cpu().numpy()
