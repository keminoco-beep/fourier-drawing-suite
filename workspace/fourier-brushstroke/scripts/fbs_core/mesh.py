"""自适应梯度面片分解（连续色调 / 照片的表示）。

背景
----
本 skill 的默认分解 = 调色板区域填充 + 中轴笔画。它对平涂/线稿/插画有效，
但照片这类**连续色调**图像会被量化成少量颜色 + 大量碎片笔画（Lenna 实测 19.7dB）。

思路（算法自身精度，不做外部量化）
------------------------------
把图像直接表达为"自适应双线性梯度面片"的集合：
* 每个面片对应一个矩形（像素格），内部用 **2×2 颜色场**（四角颜色）双线性插值；
* 拟合误差（面片内残差 RMS）超过容差就一分为四（quadtree），否则保留为叶子；
* 每个叶子的四角颜色不是简单取像素均值，而是**按渲染端真实插值基做最小二乘**
  （渲染端 `fill_polygon` 的 2×2 场在单元格边缘是钳制/线性混合的，直接用这个基函数
  拟合，保证序列化后重建误差最小）；
* 输出仍是 CONTRACT 的 region_fill（显式多边形 + region.color_field），
  逐笔画 / 一笔画 / 视频三条渲染路径无需改动。

这就是"把区域填充 + 颜色场这一表示本身的精度推到极限"：误差判据与细分都来自
图像自身，不依赖调色板量化、不依赖外部海报化预处理。
"""

from __future__ import annotations

import numpy as np

from .config import FBSConfig
from .util import WarningLog

# 面片四角颜色（2×2 场）在渲染端的基函数：单元格边缘 0.5 格内为常量，中间线性过渡。
# 与 render.fill_polygon 的采样公式一致（gx = u*gw - 0.5，钳制到 [0,gw-1]）。
_MESH_GRID = 2
# 最小面片边（像素）：再小就不值得（单像素面片等价于原图存储）。
_MESH_MIN_SIDE_PX = 4
# 细分安全阀：叶子数上限（超过后不再细分，只警告——体积/渲染时间保护）。
_MESH_MAX_CELLS = 12000
# 默认容差（面片内残差 RMS，0..1 色阶）：0.04 ≈ 每通道 10 色阶。
_MESH_DEFAULT_TOL = 0.04


def _ramp_basis(u: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """2 格颜色场的 1D 基：返回 (w0, w1)。

    u ∈ [0,1] 为格内归一化坐标。渲染端：gx = u*2-0.5 钳制到 [0,1]，
    因此 u<0.25 时为常量 w0=1，u>0.75 时 w1=1，中间线性过渡。
    """
    w0 = np.clip(1.0 - np.clip((u - 0.25) / 0.5, 0.0, 1.0), 0.0, 1.0)
    w1 = np.clip((u - 0.25) / 0.5, 0.0, 1.0)
    return w0, w1


def _fit_cell(block: np.ndarray) -> tuple[np.ndarray, float]:
    """对一个 h×w×3 像素块拟合 2×2 颜色场，返回 (四角颜色 (2,2,3) 0..1, 残差 RMS)。"""
    h, w = block.shape[:2]
    yy = (np.arange(h, dtype=np.float64) + 0.5) / float(h)
    xx = (np.arange(w, dtype=np.float64) + 0.5) / float(w)
    wy0, wy1 = _ramp_basis(yy)
    wx0, wx1 = _ramp_basis(xx)
    # 基函数顺序：[[00,01],[10,11]] = c00*wx0*wy0 + c01*wx1*wy0 + c10*wx0*wy1 + c11*wx1*wy1
    b00 = wy0[:, None] * wx0[None, :]
    b01 = wy0[:, None] * wx1[None, :]
    b10 = wy1[:, None] * wx0[None, :]
    b11 = wy1[:, None] * wx1[None, :]
    A = np.stack([b00.ravel(), b01.ravel(), b10.ravel(), b11.ravel()], axis=1)  # (n,4)
    flat = block.reshape(-1, 3)
    try:
        coef, *_ = np.linalg.lstsq(A, flat, rcond=None)  # (4,3)
    except np.linalg.LinAlgError:
        coef = np.zeros((4, 3), dtype=np.float64)
        coef[:, :] = flat.mean(axis=0)
    rec = A @ coef
    res = flat - rec
    rms = float(np.sqrt(np.mean(res ** 2))) if res.size else 0.0
    corners = coef.reshape(2, 2, 3)  # [y][x]
    return np.clip(corners, 0.0, 1.0), rms


def _quadtree(rgb: np.ndarray, tol: float, min_side: int, max_cells: int) -> tuple[list, dict]:
    """误差驱动 quadtree（优先队列版）：每次细分当前误差最大的面片。

    与"栈式 DFS 到预算就停"不同，优先队列保证**预算花在最差区域**：面片数
    从 1 单调增加时重建质量单调变好，达到体积上限时的画质是给定预算下的最优分配。
    返回叶子列表 [(y0,x0,y1,x1,corners,rms)] 与统计。
    """
    import heapq

    H, W = rgb.shape[:2]
    leaves: list = []
    stats = {"cells": 0, "splits": 0, "budget_hit": False, "max_depth": 0}

    def eval_cell(y0: int, x0: int, y1: int, x1: int, depth: int) -> tuple:
        corners, rms = _fit_cell(rgb[y0:y1, x0:x1])
        return (-rms, y0, x0, y1, x1, depth, corners, rms)

    heap = [eval_cell(0, 0, H, W, 0)]
    split_count = 0
    # 预先留出一次分裂的 4 个槽位；超过预算就停止分裂，把剩余所有节点收为叶子
    while heap:
        neg_rms, y0, x0, y1, x1, depth, corners, rms = heapq.heappop(heap)
        h, w = y1 - y0, x1 - x0
        can_split = (
            depth < 12
            and h > min_side
            and w > min_side
            and rms > tol
            and len(leaves) + len(heap) + 3 <= max_cells
        )
        if not can_split:
            if rms > tol and (len(leaves) + len(heap) + 3 > max_cells):
                stats["budget_hit"] = True
            leaves.append((y0, x0, y1, x1, corners, rms))
            stats["cells"] = len(leaves)
            continue
        ym = (y0 + y1) // 2
        xm = (x0 + x1) // 2
        if ym <= y0 or ym >= y1 or xm <= x0 or xm >= x1:
            leaves.append((y0, x0, y1, x1, corners, rms))
            stats["cells"] = len(leaves)
            continue
        split_count += 1
        stats["max_depth"] = max(stats["max_depth"], depth + 1)
        heapq.heappush(heap, eval_cell(y0, x0, ym, xm, depth + 1))
        heapq.heappush(heap, eval_cell(y0, xm, ym, x1, depth + 1))
        heapq.heappush(heap, eval_cell(ym, x0, y1, xm, depth + 1))
        heapq.heappush(heap, eval_cell(ym, xm, y1, x1, depth + 1))
    stats["splits"] = split_count
    # 按空间顺序（便于逐块作画的观察顺序：从左到右、从上到下）
    leaves.sort(key=lambda t: (t[0], t[1]))
    return leaves, stats


def reconstruct(rgb_shape: tuple[int, int, int], leaves: list) -> np.ndarray:
    """按渲染端同一插值基重建图像（单元测试与误差评估用）。"""
    H, W = rgb_shape[0], rgb_shape[1]
    out = np.zeros((H, W, 3), dtype=np.float64)
    for (y0, x0, y1, x1, corners, _rms) in leaves:
        h, w = y1 - y0, x1 - x0
        yy = (np.arange(h, dtype=np.float64) + 0.5) / float(h)
        xx = (np.arange(w, dtype=np.float64) + 0.5) / float(w)
        wy0, wy1 = _ramp_basis(yy)
        wx0, wx1 = _ramp_basis(xx)
        c00, c01 = corners[0, 0], corners[0, 1]
        c10, c11 = corners[1, 0], corners[1, 1]
        rec = (
            wy0[:, None, None] * wx0[None, :, None] * c00[None, None, :]
            + wy0[:, None, None] * wx1[None, :, None] * c01[None, None, :]
            + wy1[:, None, None] * wx0[None, :, None] * c10[None, None, :]
            + wy1[:, None, None] * wx1[None, :, None] * c11[None, None, :]
        )
        out[y0:y1, x0:x1] = rec
    return out


def decompose_mesh(
    rgb: np.ndarray,
    cfg: FBSConfig,
    warn: WarningLog | None = None,
    long_side: int | None = None,
) -> dict:
    """连续色调图像 → 自适应梯度面片 elements/regions（CONTRACT region_fill 表示）。"""
    rgba = np.asarray(rgb, dtype=np.float64)
    if rgba.ndim != 3 or rgba.shape[2] < 3:
        raise ValueError("decompose_mesh 需要 (H,W,3) RGB 数组")
    rgb01 = np.clip(rgba[..., :3], 0.0, 1.0)
    H, W = rgb01.shape[:2]
    L = int(long_side or max(H, W))
    tol = float(getattr(cfg, "mesh_tol", _MESH_DEFAULT_TOL) or _MESH_DEFAULT_TOL)
    tol = float(np.clip(tol, 0.005, 0.5))
    min_side = int(max(2, getattr(cfg, "mesh_min_side", _MESH_MIN_SIDE_PX) or _MESH_MIN_SIDE_PX))
    max_cells = int(max(16, getattr(cfg, "mesh_max_cells", _MESH_MAX_CELLS) or _MESH_MAX_CELLS))

    leaves, qstats = _quadtree(rgb01, tol, min_side, max_cells)
    if not leaves:
        raise ValueError("mesh quadtree 没有产生任何面片")

    elements: list[dict] = []
    regions: list[dict] = []
    for rid, (y0, x0, y1, x1, corners, rms) in enumerate(leaves):
        # 世界坐标：像素索引 i 的中心 = (i+0.5)/L；面片矩形 = 像素格边界 i / L
        x0w, x1w = x0 / float(L), x1 / float(L)
        y0w, y1w = y0 / float(L), y1 / float(L)
        poly_w = np.array(
            [[x0w, y0w], [x1w, y0w], [x1w, y1w], [x0w, y1w]], dtype=np.float64
        )
        block = rgb01[y0:y1, x0:x1]
        mean_col = block.reshape(-1, 3).mean(axis=0)
        region = {
            "id": int(rid),
            "rgb": [int(round(float(v) * 255.0)) for v in np.clip(mean_col, 0.0, 1.0)],
            "area_px": int(block.shape[0] * block.shape[1]),
            "bbox": [float(x0w), float(y0w), float(x1w), float(y1w)],
            "sprawl": None,
            "dt_peak_px": None,
            "color_field": {
                "grid": _MESH_GRID,
                "gw": _MESH_GRID,
                "gh": _MESH_GRID,
                # 与 fill_polygon 采样一致：bbox 用格子边界（世界单位）
                "bbox": [float(x0w), float(y0w), float(x1w), float(y1w)],
                "colors": [
                    [int(round(float(c) * 255.0)) for c in corners[gy, gx]]
                    for gy in range(2)
                    for gx in range(2)
                ],
                "valid": 4,
            },
        }
        regions.append(region)
        n = len(poly_w)
        el = {
            "points": poly_w,
            "s": np.arange(n, dtype=np.float64) / float(n),
            "closed": True,
            "fill_role": "region_fill",
            "widths": np.zeros(n, dtype=np.float64),
            "colors": np.tile(np.clip(mean_col, 0.0, 1.0).astype(np.float32), (n, 1)),
            "alphas": np.ones(n, dtype=np.float64),
            "shapes": None,
            "fill_poly": poly_w,
            "fill_holes": None,
            "region_id": int(rid),
            "layer": 0,
            "meta": {
                "max_halfwidth_px": 0.0,
                "sprawl": None,
                "dt_peak_px": None,
                "source": "mesh_cell",
                "area_px": int(block.shape[0] * block.shape[1]),
                "cell_rms": float(rms),
            },
        }
        elements.append(el)

    rec = reconstruct((H, W, 3), leaves)
    mse = float(np.mean((rec - rgb01) ** 2))
    psnr = 10.0 * np.log10(1.0 / max(mse, 1e-12))
    stats = {
        "method": "adaptive_gradient_mesh",
        "cells": len(leaves),
        "splits": int(qstats.get("splits", 0)),
        "max_depth": int(qstats.get("max_depth", 0)),
        "tol": float(tol),
        "min_side_px": int(min_side),
        "budget_hit": bool(qstats.get("budget_hit", False)),
        "recon_psnr_db": round(float(psnr), 3),
        "grid": _MESH_GRID,
    }
    if warn is not None:
        if bool(qstats.get("budget_hit", False)):
            warn.add(
                "W_MESH_BUDGET",
                f"面片数达到安全阀 {max_cells}，部分高误差区域未继续细分（可用 --mesh-tol 放宽或 --mesh-max-cells 调大）",
                severity="warning",
            )
        warn.add(
            "W_MESH_ACTIVE",
            f"连续色调模式：自适应梯度面片 {len(leaves)} 个（tol={tol:.3f}，自重建 PSNR {psnr:.2f}dB）",
            severity="info",
        )
    return {
        "method": "adaptive_gradient_mesh",
        "elements": elements,
        "regions": regions,
        "stats": stats,
        "accelerators": {},
    }
