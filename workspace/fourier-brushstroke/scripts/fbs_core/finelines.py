"""细线绘画生成器（fine-lines generator）：精细元素识别 + 纯细线笔触。

与 structpaint 的分工：structpaint 生成"宽笔结构笔触"；本模块生成"细线笔触"：
1. 颜色量化（k-means）+ 8 连通域 → 细粒度色块元素（数百 patch）；
2. 每个 patch 内沿结构张量**切向**追踪曲线细线（非固定方向）；
3. 色块边界按梯度/两侧暗色生成细边界线；
4. 无任何填色笔触。

供训练循环使用：generate_strokes() 产出一轮初始笔触；params 可被进化搜索扰动。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy import ndimage as ndi

from .util import gaussian_blur

DEFAULT_PARAMS: dict[str, Any] = {
    "K": 40,              # 颜色量化簇数
    "step": 1.7,          # 排线种子间距（px @源分辨率）
    "width_lo": 1.4,      # 细线宽范围（px @源分辨率）
    "width_hi": 2.0,
    "boundary_width": 1.3,
    "min_patch": 18,      # 色块最小面积
    "near_white": 0.94,   # 近白（不画线）
    "max_trace": 5,       # 切向追踪步数（每方向）
    "extra_density": 0.0, # 追加随机排线比例（训练进化用）
}


def jitter_params(rng: np.random.Generator) -> dict[str, Any]:
    """给 fresh 轮生成随机的超参数（进化探索）。"""
    p = dict(DEFAULT_PARAMS)
    p["K"] = int(rng.choice([32, 40, 48, 56]))
    p["step"] = float(rng.uniform(1.45, 2.0))
    p["width_lo"] = float(rng.uniform(1.2, 1.6))
    p["width_hi"] = float(rng.uniform(1.8, 2.4))
    p["boundary_width"] = float(rng.uniform(1.1, 1.6))
    p["min_patch"] = int(rng.choice([14, 18, 24, 30]))
    p["max_trace"] = int(rng.choice([4, 5, 6]))
    p["extra_density"] = float(rng.uniform(0.0, 0.25))
    return p


def _quantize(rgb: np.ndarray, alpha: np.ndarray, rng: np.random.Generator, K: int,
              sample_step: int = 3) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    H, W = rgb.shape[:2]
    fg = alpha > 0.5
    pix = rgb[fg]
    if len(pix) == 0:
        return np.full((H, W), -1, dtype=np.int32), np.zeros((0, 3)), fg
    sub = pix[::sample_step] if len(pix) > 12000 else pix
    K = int(min(K, len(sub)))
    centers = sub[rng.choice(len(sub), size=K, replace=False)].copy()
    for _ in range(16):
        d = ((sub[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        lab = d.argmin(axis=1)
        for k in range(K):
            m = lab == k
            if bool(m.any()):
                centers[k] = sub[m].mean(axis=0)
    d_all = ((pix[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
    labels = np.full((H, W), -1, dtype=np.int32)
    labels[fg] = d_all.argmin(axis=1)
    return labels, centers, fg


def _orientation_field(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gy, gx = np.gradient(gray)
    Jxx = gaussian_blur((gx * gx).astype(np.float32), 1.2)
    Jyy = gaussian_blur((gy * gy).astype(np.float32), 1.2)
    Jxy = gaussian_blur((gx * gy).astype(np.float32), 1.2)
    theta = 0.5 * np.arctan2(2 * Jxy, Jxx - Jyy)
    tang = theta + math.pi / 2.0
    return np.cos(tang).astype(np.float64), np.sin(tang).astype(np.float64)


def _make_strokes(img: dict, rng: np.random.Generator, params: dict[str, Any],
                  extra_only: bool = False) -> list[dict]:
    rgb = np.asarray(img["rgb"], dtype=np.float64)
    alpha = np.asarray(img["alpha"], dtype=np.float64) if img.get("alpha") is not None else np.ones(rgb.shape[:2])
    H, W = rgb.shape[:2]
    sm = gaussian_blur(rgb.astype(np.float32), 0.8).astype(np.float64)
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})

    labels, centers, fg = _quantize(sm, alpha, rng, int(p["K"]))
    near_white = fg & (sm.mean(axis=2) > float(p["near_white"]))
    patches = []
    for k in range(len(centers)):
        m = (labels == k) & fg & ~near_white
        if int(m.sum()) < int(p["min_patch"]):
            continue
        lab, n = ndi.label(m, structure=np.ones((3, 3), dtype=int))
        sizes = np.bincount(lab.ravel())
        for cid in range(1, n + 1):
            if sizes[cid] < int(p["min_patch"]):
                continue
            mm = lab == cid
            ys, xs = np.nonzero(mm)
            patches.append({"ys": ys, "xs": xs, "mask": mm, "color": sm[ys, xs].mean(axis=0),
                            "area": int(sizes[cid])})
    cos_t, sin_t = _orientation_field(sm)
    strokes: list[dict] = []
    covered = np.zeros((H, W), dtype=bool)
    step = float(p["step"])
    wlo, whi = float(p["width_lo"]), float(p["width_hi"])
    for patch in patches:
        ys, xs = patch["ys"], patch["xs"]
        mask = patch["mask"]
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        for yy in np.arange(y0, y1, step):
            for xx in np.arange(x0, x1, step):
                px = xx + rng.uniform(-0.6, 0.6)
                py = yy + rng.uniform(-0.6, 0.6)
                xi = int(np.clip(round(px), 0, W - 1))
                yi = int(np.clip(round(py), 0, H - 1))
                if not mask[yi, xi] or covered[yi, xi]:
                    continue
                pts = [(px, py)]
                for sign in (1.0, -1.0):
                    cx, cy = px, py
                    for _ in range(int(p["max_trace"])):
                        ix = int(np.clip(round(cx), 0, W - 1))
                        iy = int(np.clip(round(cy), 0, H - 1))
                        if not mask[iy, ix]:
                            break
                        cx += sign * cos_t[iy, ix] * 1.2
                        cy += sign * sin_t[iy, ix] * 1.2
                        if cx < 0 or cy < 0 or cx >= W or cy >= H:
                            break
                        pts.append((cx, cy))
                if len(pts) < 3:
                    continue
                arr = np.asarray(pts, dtype=np.float64)
                for vx, vy in arr:
                    xi2 = int(np.clip(round(vx), 0, W - 1))
                    yi2 = int(np.clip(round(vy), 0, H - 1))
                    covered[max(0, yi2 - 1):yi2 + 2, max(0, xi2 - 1):xi2 + 2] = True
                if len(arr) > 9:
                    idx = np.linspace(0, len(arr) - 1, 9).astype(int)
                    arr = arr[idx]
                xi3 = np.clip(arr[:, 0].astype(int), 0, W - 1)
                yi3 = np.clip(arr[:, 1].astype(int), 0, H - 1)
                col = np.clip(sm[yi3, xi3].mean(axis=0) * 0.97, 0.0, 1.0)
                if col.mean() > 0.93:
                    continue
                strokes.append({
                    "class": "detail", "preset": "ink_fineliner", "shape": "ink",
                    "points_px": arr,
                    "widths_px": np.full(len(arr), float(rng.uniform(wlo, whi))),
                    "color": col, "alpha": float(rng.uniform(0.85, 1.0)), "layer": 1,
                })

    # 边界细线（色块边界 + 强梯度 → 细线，颜色取邻域暗色）
    gray = sm @ np.array([0.2126, 0.7152, 0.0722])
    gy, gx = np.gradient(gray)
    mag = np.hypot(gx, gy)
    edge = np.zeros((H, W), dtype=bool)
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        b = np.roll(np.roll(labels, -dy, axis=0), -dx, axis=1)
        edge |= labels != b
    edge &= fg & (mag > max(float(np.percentile(mag, 70)), 0.02))
    from .decompose import clean_skeleton, skeleton_graph, skeletonize

    padded = np.pad(edge, 1, mode="constant", constant_values=False)
    sk = clean_skeleton(skeletonize(padded, 6))
    branches = skeleton_graph(sk)
    bw = float(p["boundary_width"])
    for path in branches:
        pts = np.asarray([(float(x), float(y)) for (y, x) in path], dtype=np.float64) - 1.0
        if len(pts) < 3:
            continue
        cols = []
        for x, y in pts:
            xi = int(np.clip(round(x), 0, W - 1))
            yi = int(np.clip(round(y), 0, H - 1))
            nb = sm[max(0, yi - 1):yi + 2, max(0, xi - 1):xi + 2].reshape(-1, 3)
            lum = nb @ np.array([0.2126, 0.7152, 0.0722])
            cols.append(nb[int(np.argmin(lum))])
        col = np.clip(0.85 * np.median(np.asarray(cols), axis=0) + 0.05 * np.array([0.05, 0.05, 0.08]),
                      0.0, 1.0)
        strokes.append({
            "class": "line_fine", "preset": "ink_fineliner", "shape": "ink",
            "points_px": pts,
            "widths_px": np.full(len(pts), bw),
            "color": col, "alpha": 1.0, "layer": 9,
        })
    if extra_only and p.get("extra_density", 0.0) > 0:
        keep = int(len(strokes) * (1.0 - float(p["extra_density"])))
        idx = rng.choice(len(strokes), size=max(1, len(strokes) - keep), replace=False)
        strokes = [strokes[i] for i in idx]
    return strokes


def generate_strokes(img: dict, rng: np.random.Generator,
                     params: dict[str, Any] | None = None) -> list[dict]:
    """生成一轮细线笔触（无填色）。"""
    return _make_strokes(img, rng, params or DEFAULT_PARAMS, extra_only=False)


def generate_extra_strokes(img: dict, rng: np.random.Generator,
                           params: dict[str, Any] | None = None,
                           fraction: float = 0.15) -> list[dict]:
    """为 mutate 轮追加的新鲜细线（从完整生成里随机抽一部分）。"""
    all_s = _make_strokes(img, rng, params or DEFAULT_PARAMS)
    if not all_s or fraction <= 0:
        return []
    k = max(1, int(len(all_s) * float(fraction)))
    idx = rng.choice(len(all_s), size=min(k, len(all_s)), replace=False)
    return [all_s[i] for i in idx]
