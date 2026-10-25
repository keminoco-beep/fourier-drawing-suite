"""Stage 0.5 :: 调色板 + 覆盖率场 + 局部质量守恒二值掩码。

本模块只做三件事，但每一件都是旧版实测的"胜负手"：

1. **调色板**（顺序无关、确定性、保留细结构颜色）
   - 只能从 `core = erode(fg)` 取色会丢掉亚像素细线的颜色（细线腐蚀后什么都不剩），
     因此额外收集「细结构像素」（局部厚度 ≤1.5px）的颜色候选。
   - 颜色聚类用**加权凝聚合并 + 固定排序**：先按 (−权重, R, G, B) 排序再合并，
     与输入顺序无关；禁止「按频次贪心取中心」（顺序相关，会把两侧真实墨色吸进混合色）。
   - AA 过渡色合并必须带**空间证据**：两个颜色在 RGB 上无法区分（都落在背景→墨色线段上），
     但「真实色调」自身成片（同标签自腐蚀 2 次仍存活），「AA 过渡带」只有 1–2px
     （自腐蚀即消失）。被保护且与目标色差 > 24 的簇拒绝合并 —— 这正是三色平涂图的
     灰色头发不被并进黑色的唯一可靠判据（纯颜色判据 256 个簇会塌成 1 个）。
   - 合并只累加权重、**不平均颜色**：平均会把墨色拉淡。

2. **覆盖率场 cov**（投影式，唯一正确推导见下）
       cov = clamp( ((rgb − bg)·w) / |w|², 0, 1 ),  w = ref_最近墨色 − bg
   线性混合 rgb=(1−t)bg+t·ink 下 cov=t；抗锯齿不改变其积分（墨量守恒），
   厚区内部恒为 1。d_bg（到背景距离）不能当覆盖率；比值式 d_bg/(d_bg+d_ink)
   在区域内部会被"参考色与像素的偏差"系统性压低（旧版实测宽度低估、掉 0.5dB）。

3. **局部质量守恒二值掩码**（禁止全局统一调低阈值）
       逐块解出使 #{cov>τ}≈Σcov 的 τ（块内 cov 的上分位数）。
   - 厚区：局部墨量与 0.5 面本就吻合 → τ≈0.5，边界不动；
   - 亚像素细线：峰值只有 0.3–0.46 → τ 自动降到峰值以下，细线复活；
   - 统一调低阈值实测会让多画从 11% 飙到 34%，所以必须"逐块守恒"。
   * 小连通块按"相邻最大标签"吸收；无邻居的极噪点丢弃并计入 W_SMALL_REGIONS。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import FBSConfig
from .util import (
    WarningLog,
    distance_transform,
    erode,
    label_components,
)

# ---------------------------------------------------------------- 常量
# 覆盖率场/平滑阈值下限：低于此覆盖率视为"已离开本结构"（亚像素线的尾巴可低于 0.1）
_COV_EPS = 1e-6
# 调色板候选的 5bit/通道量化（32768 桶）：比精确色去重快，且 bin 均值精度 ±4/255 足够
_PALETTE_BITS = 5
_PALETTE_BINS = 1 << (3 * _PALETTE_BITS)
# 前景门限：与背景色距离 ≤ 该值的像素视为"背景/噪声"（旧行为保底）；
# 但其中"比背景更接近某个墨色"的像素会被最近色分类救回（见 analyze 里的 restored）：
# 近白的真实内容（肤色、浅色高光、浅蓝衣褶）不再被整体当背景剔除。
_FG_INK_MARGIN = 1.0      # 最近墨色距离需比背景距离小多少才算"内容"（噪声余量）
_FG_RESTORED_COV_MIN = 0.60  # 救回像素的最低覆盖率（保证进入局部质量守恒掩码）
# 调色板保留门限：与背景距离 ≤ 该值的簇视为 AA 尾丢弃。必须很小——浅肤色/浅高光这类
# 真实色调与背景只差 10–24px，旧实现用 fg_tol 会把它们整簇丢掉。
_PALETTE_MIN_DBG = 6.0
# 近背景"浅色内容"补色（B 修复）：全局 k-means 常把浅肤色/浅蓝衣褶的配额挤掉，
# 这些像素只能落到背景或远处色心 → 被判背景（白洞）。对"浅、近中性、比背景亮"的
# 像素单独做一轮粗聚类，补最多 4 个颜色（与已有色心保持距离，避免重复）。
_PALE_BAND_MIN_DBG = 6.0
_PALE_BAND_MIN_LUMA = 226.0
_PALE_BAND_MAX_CHROMA = 34.0
_PALE_BAND_MAX_ADD = 4
_PALE_BAND_EXIST_DIST = 10.0
_PALE_BAND_MIN_PX = 200
# 调色板候选的 5bit/通道量化（32768 桶）：比精确色去重快，且 bin 均值精度 ±4/255 足够
_PALETTE_BITS = 5
_PALETTE_BINS = 1 << (3 * _PALETTE_BITS)
# 聚类时按权重预筛的候选数上限（控制 O(n²) 规模；保留的是重色，不会丢真实墨色）
_CLUSTER_MAX_INPUT = 256
# 颜色聚类阈值（RGB 欧氏距离，0..255）：线稿/平涂用 34，插画用 48（相邻色阶更细）
_CLUSTER_THRESH = 34.0
_CLUSTER_THRESH_ILLUSTRATION = 48.0
# AA 过渡色合并阈值：簇到「背景→更重簇」线段的距离上限
_AA_SEG_TOL = 32.0
# 被空间证据保护的真实色调，与目标色差超过它才拒绝合并（压缩噪声造成的小色差照常合并）
_AA_PROTECT_COLOR_GAP = 24.0
# 自标签腐蚀轮数：真实色调成片 → 2 轮仍存活；AA 过渡带 1–2px → 2 轮必消失
_SAME_LABEL_ERODE_R = 2
# 细结构判据：前景距离变换 ≤ 1.5px 视为"细"（需要单独收集其颜色）
_THIN_DT_PX = 1.5
# 调色板候选的最小像素权重（相对前景面积的千分比 + 绝对下限）
_PALETTE_MIN_REL = 8.0e-4
_PALETTE_MIN_ABS = 3
# 掩码块级阈值上下限：上限不得超过 0.5（否则厚区边界被侵蚀）
_TAU_LO = 0.06
_TAU_HI = 0.50
# 噪点连通块（像素）下限：小于它的孤立块视为噪点，计入 W_SMALL_REGIONS
_NOISE_COMPONENT_PX = 3
# 小连通块吸收的邻域搜索半径（px）：1 环内找不到标签时再外扩一圈
_ABSORB_NEIGHBOUR_R = 2
# 暗墨保护：小块与吸收目标色差 ≥ 24（0..255 欧氏距离）且明显更暗（>8 亮度级）时保留，
# 防止细线/排线被并进浅色区域（填色后线消失，manga 最大可见缺陷来源）。
_ABSORB_DARK_PROTECT_DELTA = 24.0
# 保护对象只限"细而连续"的暗结构（细线/排线）：厚度 DT ≤1.6px 且面积 ≥24px。
# 否则 JPEG 噪点/暗斑也会被保留成笔画，画面出现脏点（manga 实测 PSNR −0.5dB）。
_ABSORB_PROTECT_MAX_DT = 1.6
_ABSORB_PROTECT_MIN_PX = 24
# 只保护"暗墨在**浅底**上"的薄结构：目标邻域亮度 ≥110。暗底（深色头发内部纹理）
# 上再画一遍暗线会双重描线/过黑（manga 实测 PSNR −0.5dB 的主因）。
_ABSORB_PROTECT_MIN_BG_LUMA = 110.0


# ---------------------------------------------------------------- 背景估计
def _quant_key(rgb: np.ndarray, bits: int = 5) -> np.ndarray:
    """按 bits/通道量化成整数键（用于直方图，避免 np.unique 慢排序）。

    输入可以是 0..255 的 uint8 或 float；内部统一取整到 uint8 再量化。
    """
    rgb_u8 = np.clip(np.rint(np.asarray(rgb, dtype=np.float64)), 0, 255).astype(np.uint8)
    shift = 8 - int(bits)
    q = (rgb_u8 >> shift).astype(np.uint32)
    levels = 1 << int(bits)
    return (q[..., 0] * levels + q[..., 1]) * levels + q[..., 2]


def _dominant_color(rgb255: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, float, int]:
    """返回 (主导色均值, 该色占比, 像素数)。量化直方图的众数桶，再取桶内精确均值。"""
    total = int(mask.sum())
    if total <= 0:
        return np.zeros(3, dtype=np.float64), 0.0, 0
    key = _quant_key(rgb255, _PALETTE_BITS)
    vals = key[mask]
    hist = np.bincount(vals.ravel(), minlength=_PALETTE_BINS)
    top = int(np.argmax(hist))
    sel = mask & (key == top)
    mean = rgb255[sel].astype(np.float64).mean(axis=0)
    return mean, float(hist[top]) / float(total), total


def estimate_background(img: dict) -> tuple[np.ndarray, dict[str, Any]]:
    """估计背景色（0..1）。优先图像边界环的众数色，其次全图可见像素众数色。

    **不假设纯白**：浅色纸张、深色底图、透明 PNG（合成为白底）都要能处理。
        透明像素占比 > 50% → 背景取白（preprocess 已把透明区合成为白，语义一致）。
    """
    rgb = np.asarray(img["rgb"], dtype=np.float64)
    rgb255 = rgb * 255.0
    alpha = np.asarray(img["alpha"], dtype=np.float64)
    visible = alpha > 0.5
    h, w = visible.shape
    border = np.zeros((h, w), dtype=bool)
    border[0, :] = True
    border[-1, :] = True
    border[:, 0] = True
    border[:, -1] = True
    ring = border & visible
    info: dict[str, Any] = {"visible_frac": round(float(visible.mean()), 4)}
    if float(visible.mean()) < 0.5:
        # 大面积透明：preprocess 已合成为白底，背景取白
        info.update({"source": "transparent_white", "dominance": 1.0, "ring_px": int(ring.sum())})
        return np.ones(3, dtype=np.float64), info
    bg, dom, n = _dominant_color(rgb255, ring if ring.any() else visible)
    source = "border"
    if dom < 0.15:  # 边界太破碎（内容占满画面）：退回全图众数
        bg, dom, n = _dominant_color(rgb255, visible)
        source = "global"
    # 背景噪声：边界像素到背景色的平均偏差（0..255 口径，后面用于前景阈值）
    if ring.any():
        resid = np.linalg.norm(rgb255[ring] - bg[None, :], axis=1)
        bg_noise = float(np.mean(resid))
    else:
        bg_noise = 0.0
    info.update(
        {
            "source": source,
            "dominance": round(dom, 4),
            "ring_px": int(ring.sum()),
            "bg_noise_px": round(bg_noise, 3),
        }
    )
    return np.clip(bg, 0.0, 255.0) / 255.0, info


# ---------------------------------------------------------------- 调色板
def _bin_means(rgb255: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """按 5bit 桶聚合：返回 (桶均值 RGB 0..255, 桶像素数)。"""
    key = _quant_key(rgb255, _PALETTE_BITS)
    flat_key = key[mask].ravel()
    if flat_key.size == 0:
        return np.zeros((0, 3), dtype=np.float64), np.zeros(0, dtype=np.int64)
    counts = np.bincount(flat_key, minlength=_PALETTE_BINS)
    nz = np.flatnonzero(counts)
    sub = rgb255[mask].astype(np.float64)
    sums = np.stack(
        [np.bincount(flat_key, weights=sub[:, c], minlength=_PALETTE_BINS)[nz]
         for c in range(3)],
        axis=1,
    )
    means = sums / np.maximum(counts[nz, None].astype(np.float64), 1.0)
    return means, counts[nz].astype(np.int64)


def cluster_colors(
    palette: np.ndarray,
    weights: np.ndarray,
    thresh: float = _CLUSTER_THRESH,
    max_k: int = 64,
) -> tuple[np.ndarray, np.ndarray]:
    """加权凝聚聚类（顺序无关、确定性）。

    反复合并「距离最小且 < thresh」的一对簇，直到最小距离 ≥ thresh 或簇数 ≤ max_k。
    先按 (−权重, R, G, B) 排序固定输入顺序，因此结果与调用方传入顺序无关。
    """
    n = len(palette)
    if n == 0:
        return np.zeros((0, 3), dtype=np.float64), np.zeros(0, dtype=np.int64)
    cen = np.asarray(palette, dtype=np.float64).reshape(-1, 3).copy()
    w = np.asarray(weights, dtype=np.float64).reshape(-1).copy()
    order = np.lexsort((cen[:, 2], cen[:, 1], cen[:, 0], -w))
    cen, w = cen[order][:  _CLUSTER_MAX_INPUT], w[order][: _CLUSTER_MAX_INPUT]
    max_k = int(max(1, max_k))
    while len(cen) > 1:
        d2 = ((cen[:, None, :] - cen[None, :, :]) ** 2).sum(axis=2)
        np.fill_diagonal(d2, np.inf)
        flat = int(np.argmin(d2))
        i, j = divmod(flat, len(cen))
        if i > j:  # 规范化到 i<j（argmin 本身确定性，这里只是让合并方向稳定）
            i, j = j, i
        if np.sqrt(d2[i, j]) >= thresh or len(cen) <= max_k:
            break
        tw = w[i] + w[j]
        cen[i] = (cen[i] * w[i] + cen[j] * w[j]) / max(tw, _COV_EPS)
        w[i] = tw
        cen = np.delete(cen, j, axis=0)
        w = np.delete(w, j, axis=0)
    order = np.lexsort((cen[:, 2], cen[:, 1], cen[:, 0], -w))
    return cen[order], w[order].astype(np.int64)


def same_label_erode(lab: np.ndarray, r: int = _SAME_LABEL_ERODE_R) -> np.ndarray:
    """「同标签自腐蚀」：返回 r 轮后仍与 8 邻域标签一致的像素（空间证据）。

    用途：判断某颜色簇自身是否**成片**（真实色调），还是只有 1–2px 厚的过渡带（AA）。
    `lab` 中负数表示无效像素，一律视为不相等。成本 O(8·r·H·W)，与颜色数无关。
    """
    lab = np.asarray(lab)
    h, w = lab.shape
    e = lab >= 0
    for _ in range(int(max(0, r))):
        if not bool(e.any()):
            break
        ne = e.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                sh = np.full((h, w), -1, dtype=lab.dtype)
                ys = slice(max(0, dy), h + min(0, dy))
                xs = slice(max(0, dx), w + min(0, dx))
                yd = slice(max(0, -dy), h - max(0, dy))
                xd = slice(max(0, -dx), w - max(0, dx))
                sh[yd, xd] = lab[ys, xs]
                ne &= (sh == lab) & (sh >= 0)
        e = ne
    return e


def tone_protect_mask(lab_img: np.ndarray, n_centers: int) -> np.ndarray:
    """由自标签腐蚀结果标出「真实色调」簇（这些簇不得被当作 AA 过渡色并掉）。"""
    out = np.zeros(int(n_centers), dtype=bool)
    if n_centers <= 0 or lab_img.size == 0:
        return out
    surv = same_label_erode(lab_img, _SAME_LABEL_ERODE_R)
    if bool(surv.any()):
        ids = np.unique(lab_img[surv])
        ids = ids[(ids >= 0) & (ids < int(n_centers))]
        out[ids] = True
    return out


def merge_aa_blends(
    centers: np.ndarray,
    weights: np.ndarray,
    bg: np.ndarray,
    tol: float = _AA_SEG_TOL,
    protect: np.ndarray | None = None,
    protect_gap: float = _AA_PROTECT_COLOR_GAP,
) -> tuple[np.ndarray, np.ndarray]:
    """把 AA 过渡色并入其所属的实体墨色（只累加权重，**不平均颜色**）。

    判据：簇 i 到「背景 → 某个更重簇 j」线段的距离 < tol 时视为过渡色。
    `protect`（tone_protect_mask）为真的簇，若与目标 j 的色差 > protect_gap 则拒绝合并
    —— 真实灰色调与「白黑过渡灰」在颜色上不可分，必须靠空间证据。
    `tol` 量纲为 RGB 欧氏距离（0..255）。
    """
    n = len(centers)
    if n <= 1:
        return np.asarray(centers, dtype=np.float64).copy(), np.asarray(weights, dtype=np.int64).copy()
    cen = np.asarray(centers, dtype=np.float64).reshape(-1, 3)
    w = np.asarray(weights, dtype=np.float64).reshape(-1)
    prot = (
        np.zeros(n, dtype=bool)
        if protect is None
        else np.asarray(protect, dtype=bool).reshape(-1)[:n]
    )
    order = np.lexsort((cen[:, 2], cen[:, 1], cen[:, 0], w))
    c = cen[order].copy()
    ww = w[order].copy()
    pr = prot[order].copy()
    alive = np.ones(n, dtype=bool)
    bg = np.asarray(bg, dtype=np.float64).reshape(3)

    for i in range(n):
        if not alive[i]:
            continue
        heavier = alive & (ww > ww[i])
        heavier[i] = False
        if not bool(heavier.any()):
            continue
        p = c[i]
        ab = c - bg[None, :]
        l2 = (ab * ab).sum(axis=1)
        t = np.clip(((p - bg) @ ab.T) / np.maximum(l2, _COV_EPS), 0.0, 1.0)
        proj = bg[None, :] + t[:, None] * ab
        d = np.linalg.norm(p[None, :] - proj, axis=1)
        d = np.where(heavier, d, np.inf)
        j = int(np.argmin(d))
        if not np.isfinite(d[j]) or float(d[j]) >= tol:
            continue
        if pr[i] and float(np.linalg.norm(c[i] - c[j])) > protect_gap:
            continue
        ww[j] += ww[i]
        alive[i] = False
    cc = c[alive]
    wout = ww[alive]
    order2 = np.lexsort((cc[:, 2], cc[:, 1], cc[:, 0], -wout))
    return cc[order2], wout[order2].astype(np.int64)


def _auto_max_colors(unique_colors: int) -> int:
    """由唯一色数量级导出调色板簇数上限（数据导出，非常量限制）。"""
    if unique_colors <= 64:
        return 16
    if unique_colors <= 512:
        return 24
    if unique_colors <= 5_000:
        return 32
    if unique_colors <= 50_000:
        return 48
    return 64


def _assign_nearest(points: np.ndarray, centers: np.ndarray) -> np.ndarray:
    """逐点最近中心索引（向量化，K 次距离计算）。"""
    n = len(points)
    best = np.full(n, np.inf, dtype=np.float64)
    lab = np.zeros(n, dtype=np.int32)
    for k in range(len(centers)):
        d = ((points - centers[k][None, :]) ** 2).sum(axis=1)
        upd = d < best
        best = np.where(upd, d, best)
        lab[upd] = k
    return lab


def build_palette(
    rgb: np.ndarray,
    rgb255: np.ndarray,
    visible: np.ndarray,
    bg: np.ndarray,
    cfg: FBSConfig,
    evidence: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """从图像构建墨色调色板（不含背景色）。返回 (centers 0..1, weights, stats)。"""
    h, w = visible.shape
    bg255 = bg * 255.0
    dist_bg = np.linalg.norm(rgb255 - bg255[None, None, :], axis=2)
    bg_noise = float(evidence.get("bg_noise_px", 0.0))
    # 前景阈值：必须高于背景噪声；下限 6/255 保证浅色（如浅肤色 dist≈12）不被判成背景
    fg_tol = float(np.clip(2.0 * bg_noise + 5.0, 6.0, 24.0))
    fg = (dist_bg > fg_tol) & visible
    stats: dict[str, Any] = {
        "fg_tol_px": round(fg_tol, 3),
        "fg_px": int(fg.sum()),
        "fg_frac": round(float(fg.mean()), 4),
    }
    if not bool(fg.any()):
        stats["reason"] = "empty_foreground"
        return (
            np.zeros((0, 3), dtype=np.float64),
            np.zeros(0, dtype=np.int64),
            stats,
        )

    # 腐蚀取芯：细笔画不能用大半径，否则芯为空；再自动按前景厚度选择半径
    dt_fg = distance_transform(fg)
    fg_radius = float(dt_fg.max()) if dt_fg.size else 0.0
    erode_r = 2 if fg_radius >= 5.0 else (1 if fg_radius >= 2.0 else 0)
    core = erode(fg, erode_r) if erode_r > 0 else fg.copy()
    if int(core.sum()) < max(int(cfg.min_region_area), int(0.01 * fg.sum())):
        core = fg.copy()

    # --- 候选色：腐蚀芯色 ∪ 细结构色（细线被腐蚀掉，必须单独收集）---
    cand_means: list[np.ndarray] = []
    cand_weights: list[np.ndarray] = []
    m, c = _bin_means(rgb255, core)
    cand_means.append(m)
    cand_weights.append(c)
    thin = fg & (dt_fg <= _THIN_DT_PX)
    n_thin = int(thin.sum())
    if n_thin > 0:
        m2, c2 = _bin_means(rgb255, thin)
        cand_means.append(m2)
        cand_weights.append(c2)
    pal = np.concatenate(cand_means, axis=0) if cand_means else np.zeros((0, 3))
    wts = np.concatenate(cand_weights, axis=0) if cand_weights else np.zeros(0, dtype=np.int64)
    # 同色桶（芯与细结构重合）权重相加
    if len(pal):
        key = (
            (np.round(pal[:, 0]).astype(np.int64) << 16)
            | (np.round(pal[:, 1]).astype(np.int64) << 8)
            | np.round(pal[:, 2]).astype(np.int64)
        )
        uniq, inv = np.unique(key, return_inverse=True)
        agg = np.zeros((len(uniq), 3), dtype=np.float64)
        aggw = np.zeros(len(uniq), dtype=np.int64)
        for c_i in range(3):
            agg[:, c_i] = np.bincount(inv, weights=pal[:, c_i] * wts, minlength=len(uniq))
        aggw = np.bincount(inv, weights=wts.astype(np.float64), minlength=len(uniq)).astype(np.int64)
        pal = agg / np.maximum(aggw[:, None].astype(np.float64), 1.0)
        wts = aggw
    keep = wts >= max(_PALETTE_MIN_ABS, int(_PALETTE_MIN_REL * float(fg.sum())))
    pal, wts = pal[keep], wts[keep]
    stats["candidate_bins"] = int(len(pal))
    stats["thin_px"] = n_thin

    colors = int(cfg.colors) if int(cfg.colors) > 0 else _auto_max_colors(int(evidence.get("unique_colors", 0)))
    # 插画色阶多、相邻色差小：聚类阈值放宽（48），否则发丝/阴影层会被并成一块；
    # 线稿/平涂用 34，保证 AA 过渡灰能被后续 merge_aa_blends 精确并入墨色。
    thresh = _CLUSTER_THRESH_ILLUSTRATION if evidence.get("klass") == "illustration" else _CLUSTER_THRESH
    centers, weights = cluster_colors(pal, wts, thresh, max_k=colors)

    # --- 空间证据：把芯像素标到簇上，自标签腐蚀得到「真实色调」---
    if len(centers) and bool(core.any()):
        sub = rgb255[core].astype(np.float64)
        lab_core = _assign_nearest(sub, centers)
        lab_img = np.full((h, w), -1, dtype=np.int32)
        lab_img[core] = lab_core
        protect = tone_protect_mask(lab_img, len(centers))
    else:
        protect = np.zeros(len(centers), dtype=bool)
    stats["protected_tones"] = int(protect.sum())
    centers, weights = merge_aa_blends(centers, weights, bg255, _AA_SEG_TOL, protect)
    stats["clusters_after_aa_merge"] = int(len(centers))

    # 与背景几乎同色的残余簇（合并失败时的 AA 尾）直接丢弃。
    # 注意：前景掩码本身已把"比背景更接近背景"的像素排除，所以这里用**很小的** d_bg 门限
    # 即可；若沿用 fg_tol（最大 24）会把浅肤色/浅高光这类真实色调簇整簇丢掉，
    # 导致这些区域只能落到别的墨色上（肤色被涂白/错色）。
    if len(centers):
        d_bg = np.linalg.norm(centers - bg255[None, :], axis=1)
        keep2 = d_bg > _PALETTE_MIN_DBG
        if not bool(keep2.all()):
            stats["dropped_near_bg"] = int((~keep2).sum())
        centers, weights = centers[keep2], weights[keep2]

    # 权重过滤（AA 尾部会留下个位数像素的假墨色）
    if len(centers):
        wmin = max(int(cfg.min_region_area), int(0.005 * float(fg.sum())))
        keep3 = weights >= wmin
        centers, weights = centers[keep3], weights[keep3]
    # 最终颜色精修：用芯像素的**精确均值**替换量化桶均值。
    # 只用与中心足够近的像素（距离 ≤ 0.5×聚类阈值）：AA 过渡色被并进墨色时
    # 只贡献权重、**不得参与颜色平均**（否则黑墨芯 + 过渡灰平均后墨色被拉淡，
    # 实测纯黑线会被精修成 (32,32,32)）。
    if len(centers) and bool(core.any()):
        sub = rgb255[core].astype(np.float64)
        best_d = np.full(len(sub), np.inf, dtype=np.float64)
        best_lab = np.zeros(len(sub), dtype=np.int32)
        for k in range(len(centers)):
            d = np.linalg.norm(sub - centers[k][None, :], axis=1)
            upd = d < best_d
            best_d = np.where(upd, d, best_d)
            best_lab[upd] = k
        refine_tol = 0.5 * float(thresh)
        for k in range(len(centers)):
            sel = (best_lab == k) & (best_d <= refine_tol)
            if int(sel.sum()) > 0:
                centers[k] = sub[sel].mean(axis=0)
    stats["palette_size"] = int(len(centers))
    stats["cluster_thresh"] = float(thresh)
    return (
        np.clip(centers, 0.0, 255.0) / 255.0,
        weights,
        stats,
    )


# ---------------------------------------------------------------- 覆盖率场
def coverage_field(
    rgb: np.ndarray,
    bg: np.ndarray,
    refs: np.ndarray,
    lab_ink: np.ndarray,
    dist_bg: np.ndarray,
    fg_tol: float,
) -> np.ndarray:
    """投影式覆盖率场 cov ∈ [0,1]。

    cov = clamp(((rgb−bg)·w)/|w|², 0, 1)，w = refs[lab_ink] − bg。
    线性混合下等于覆盖率；厚区内部恒为 1；积分（墨量）守恒。
    `lab_ink` 为每像素最近墨色在 refs 中的下标（refs 含背景时从 1 开始）。
    与背景偏差 ≤ fg_tol 的像素强制归零（纯背景/噪声不产生假墨量）。
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    bg = np.asarray(bg, dtype=np.float64).reshape(1, 1, 3)
    wv = np.asarray(refs, dtype=np.float64)[lab_ink] - bg  # (H,W,3)
    uu = (wv * wv).sum(axis=2)
    num = ((rgb - bg) * wv).sum(axis=2)
    cov = np.where(uu > _COV_EPS, num / np.maximum(uu, _COV_EPS), 0.0)
    cov = np.clip(cov, 0.0, 1.0)
    cov = np.where(dist_bg > fg_tol, cov, 0.0)
    return cov.astype(np.float32)


# ---------------------------------------------------------------- 局部质量守恒掩码
def local_mass_tau(
    cov: np.ndarray,
    block: int,
    lo: float = _TAU_LO,
    hi: float = _TAU_HI,
) -> np.ndarray:
    """逐块解出「使二值化面积 = 局部墨量」的阈值 τ（块内 cov 的上分位数）。

    性质：厚区局部墨量与 0.5 面吻合 → τ≈0.5（边界不动）；
    亚像素细线墨量少、峰值低 → τ 降到峰值以下（细线复活）；全图墨量守恒。
    """
    h, w = cov.shape
    bh = max(1, int(block))
    nh = int(np.ceil(h / bh))
    nw = int(np.ceil(w / bh))
    tau = np.full((nh, nw), float(hi), dtype=np.float64)
    for by in range(nh):
        y0, y1 = by * bh, min(h, (by + 1) * bh)
        for bx in range(nw):
            x0, x1 = bx * bh, min(w, (bx + 1) * bh)
            blk = cov[y0:y1, x0:x1].ravel()
            if blk.size == 0:
                continue
            m = float(blk.sum(dtype=np.float64))
            if m <= _TAU_LO:  # 近乎纯背景
                continue
            k = int(round(m))
            if k <= 0:
                continue
            if k >= blk.size:  # 整块皆墨：阈值取下限
                tau[by, bx] = float(lo)
                continue
            part = np.partition(blk, blk.size - k - 1)
            tau[by, bx] = float(np.clip(part[blk.size - k - 1], lo, hi))
    return tau


def upsample_tau(tau: np.ndarray, h: int, w: int, block: int) -> np.ndarray:
    """块级阈值放大到像素级（**最近块**，逐块质量守恒）。

    为什么不是双线性：双线性会把「含细线的块 τ=0.4」与「空白块 τ=0.5」
    在块边界处插成 0.42–0.46，恰好把细线在块边界处切断（实测 2 行 0.4 覆盖率的
    亚像素线整根消失）。也试过「3×3 块邻域取最小」：能救回跨块细线，但会把
    大量低覆盖背景并进掩码（horn 掩码面积 +10%、PSNR −0.33dB），故不采用。
    """
    th, tw = tau.shape
    bh = max(1, int(block))
    if th < 2 or tw < 2:
        return np.full((h, w), float(tau.min()), dtype=np.float64)
    ys = np.minimum(np.arange(h, dtype=np.int64) // bh, th - 1)
    xs = np.minimum(np.arange(w, dtype=np.int64) // bh, tw - 1)
    return tau[ys][:, xs]


def absorb_small_components(
    labels: np.ndarray,
    mask: np.ndarray,
    min_area: int,
    palette255: np.ndarray | None = None,
    protect_delta: float = 0.0,
    protect_max_dt: float | None = None,
    protect_min_bg_luma: float = 0.0,
) -> tuple[np.ndarray, dict[str, int]]:
    """把小连通块按「相邻像素数最多的标签」吸收；无邻居的噪点丢弃。

    规则来自「小连通块按规则吸收而非丢弃」：真实小结构（眼睛高光、细碎笔触）
    属于邻居区域，硬丢会留下未覆盖空白。**必须按标签内连通块判定**：
    整幅墨迹可能是一个大连通块，但其内部某颜色的碎斑仍应被吸收（否则会退化成
    成千上万个点盘，实测 chibi 5150 个区域 / 4418 个点盘）。

    **暗墨保护**（palette255 + protect_delta）：若小块的标签色与吸收目标色差很大
    且明显更暗（细线/排线/瞳孔等暗墨落在浅色底上），保留其自身标签而不是吸收——
    否则细线会被并进浅色区域，填色后线就消失（manga 实测最明显缺陷）。
    面积太小的（< max(8, 0.1*min_area)）仍照旧吸收/丢弃，避免 JPEG 噪点变成笔画。

    性能：逐标签算连通域与包围盒，逐块只在其**包围盒裁切**上做邻域比较
    （旧版逐块对全图 dilate，实测 8000 个碎块要 17s）。
    """
    out = np.asarray(labels, dtype=np.int32).copy()
    mask = np.asarray(mask, dtype=bool)
    min_area = int(max(1, min_area))
    pal = None if palette255 is None else np.asarray(palette255, dtype=np.float64)
    if pal is not None and pal.size and float(np.max(pal)) <= 1.5:
        pal = pal * 255.0
    protect_min_area = max(8, int(0.1 * min_area))
    stats = {"absorbed": 0, "dropped": 0, "protected": 0}
    if not bool(mask.any()):
        return out, stats
    for lab in np.unique(out[out >= 0]).tolist():
        comp_lab, n_comp = label_components(out == lab)
        if n_comp <= 0:
            continue
        areas = np.bincount(comp_lab.ravel(), minlength=n_comp + 1)
        small_ids = np.flatnonzero(areas < min_area)
        small_ids = small_ids[small_ids > 0]
        if small_ids.size == 0:
            continue
        h, w = comp_lab.shape
        ys, xs = np.nonzero(comp_lab)
        ids = comp_lab[ys, xs]
        y0 = np.full(n_comp + 1, h, dtype=np.int64)
        x0 = np.full(n_comp + 1, w, dtype=np.int64)
        y1 = np.full(n_comp + 1, -1, dtype=np.int64)
        x1 = np.full(n_comp + 1, -1, dtype=np.int64)
        np.minimum.at(y0, ids, ys)
        np.minimum.at(x0, ids, xs)
        np.maximum.at(y1, ids, ys)
        np.maximum.at(x1, ids, xs)
        for ci in small_ids:
            yc0, yc1 = int(y0[ci]), int(y1[ci]) + 1
            xc0, xc1 = int(x0[ci]), int(x1[ci]) + 1
            # 邻域查找要在**外扩 r 的窗口**上做（否则 ring 的偏移与 bbox 不对齐，
            # 会把邻居查成错误像素）；吸收半径 2px，尽量把真实小结构吸收而非丢弃。
            r_nb = _ABSORB_NEIGHBOUR_R
            ey0, ey1 = max(0, yc0 - r_nb), min(h, yc1 + r_nb)
            ex0, ex1 = max(0, xc0 - r_nb), min(w, xc1 + r_nb)
            comp_exp = comp_lab[ey0:ey1, ex0:ex1] == ci
            win = out[ey0:ey1, ex0:ex1]
            ring = np.zeros_like(comp_exp)
            padded = np.pad(comp_exp, r_nb, mode="constant", constant_values=False)
            ch, cw = comp_exp.shape
            for dy in range(2 * r_nb + 1):
                for dx in range(2 * r_nb + 1):
                    if dy == r_nb and dx == r_nb:
                        continue
                    ring |= padded[dy:dy + ch, dx:dx + cw]
            ring &= ~comp_exp
            nb = win[ring]
            nb = nb[(nb >= 0) & (nb != lab)]
            if nb.size:
                vals, counts = np.unique(nb, return_counts=True)
                target = int(vals[int(np.argmax(counts))])
                if (
                    pal is not None
                    and protect_delta > 0.0
                    and int(areas[ci]) >= max(protect_min_area, _ABSORB_PROTECT_MIN_PX)
                    and 0 <= lab < len(pal)
                    and 0 <= target < len(pal)
                ):
                    c_src = pal[lab]
                    c_dst = pal[target]
                    dcol = float(np.linalg.norm(c_src - c_dst))
                    l_src = float(0.2126 * c_src[0] + 0.7152 * c_src[1] + 0.0722 * c_src[2])
                    l_dst = float(0.2126 * c_dst[0] + 0.7152 * c_dst[1] + 0.0722 * c_dst[2])
                    if dcol >= protect_delta and l_src < l_dst - 8.0 and l_dst >= protect_min_bg_luma:
                        if protect_max_dt is None:
                            stats["protected"] += 1
                            continue
                        # 只保护细结构（细线/排线），不保护暗噪声斑块
                        sub_m = comp_lab[yc0:yc1, xc0:xc1] == ci
                        dt_max = float(np.max(distance_transform(np.pad(sub_m, 1, mode="constant", constant_values=False))[1:-1, 1:-1])) if bool(sub_m.any()) else 0.0
                        if dt_max <= protect_max_dt:
                            stats["protected"] += 1
                            continue
                sub = out[yc0:yc1, xc0:xc1]
                sub[comp_lab[yc0:yc1, xc0:xc1] == ci] = target
                stats["absorbed"] += 1
            else:
                sub = out[yc0:yc1, xc0:xc1]
                sub[comp_lab[yc0:yc1, xc0:xc1] == ci] = -1
                stats["dropped"] += 1
    return out, stats


def _drop_noise_specks(mask: np.ndarray, min_px: int) -> tuple[np.ndarray, int]:
    """剔除 < min_px 的孤立墨点（JPEG/压缩噪点），返回 (mask, 剔除数)。"""
    lab, n = label_components(mask)
    if n <= 0:
        return mask, 0
    counts = np.bincount(lab.ravel())
    small = np.zeros(n + 1, dtype=bool)
    ids = np.flatnonzero(counts < int(min_px))
    ids = ids[ids > 0]
    small[ids] = True
    if not bool(small.any()):
        return mask, 0
    return mask & ~small[lab], int(small.sum())


# ---------------------------------------------------------------- SLIC 空间正则
# 超像素密度：每多少前景像素一个种子（旧版 seg_div=400 的实测平衡点）
_SLIC_PX_PER_SEGMENT = 400
# SLIC 紧致度与迭代轮数（颜色权重 vs 空间权重；复杂插画 4 轮已收敛）
_SLIC_COMPACTNESS = 14.0
_SLIC_ITERS = 4


def _slic_assignment(
    rgb255: np.ndarray,
    mask: np.ndarray,
    palette255: np.ndarray,
    n_segments: int,
    compactness: float,
    iters: int,
) -> np.ndarray:
    """紧凑超像素分割 + 映射回调色板，返回与 mask 一致的空间连贯标签（-1=背景）。

    为什么需要：逐像素「最近参考色」分配完全无视空间。复杂插画里相邻色阶在
    抗锯齿带上来回跳变，会切出成千上万个 1–10px 的碎片（实测 chibi 9512 个连通块，
    其中 8000+ 是碎片）—— 碎片既没有中轴也没有语义，只能退化成点盘。
    超像素在空间上紧凑，先分块再按色板归类，区域数回到"真实块面"量级。

    实现是 SLIC 的简化版（中心位置固定网格、只迭代颜色），因为这里只需要
    「空间连贯」而不是医学分割精度。纯 numpy 向量化，复杂度 O(9·iters·HW)。
    """
    h, w = mask.shape
    fg_area = int(mask.sum())
    if fg_area <= 0:
        return np.full((h, w), -1, dtype=np.int32)
    n_seg = int(np.clip(n_segments, 16, 6000))
    step = max(4, int(round(math.sqrt(h * w / float(n_seg)))))
    gy = np.arange(step // 2, h, step)
    gx = np.arange(step // 2, w, step)
    gh, gw = max(1, len(gy)), max(1, len(gx))
    if gh * gw < 2:
        return np.full((h, w), -1, dtype=np.int32)
    cy_grid, cx_grid = np.meshgrid(gy, gx, indexing="ij")
    center_col = rgb255[cy_grid, cx_grid].astype(np.float64).reshape(-1, 3)
    # 每个像素所属的网格中心（固定）
    gyi = np.broadcast_to(np.clip(np.arange(h) // step, 0, gh - 1)[:, None], (h, w))
    gxi = np.broadcast_to(np.clip(np.arange(w) // step, 0, gw - 1)[None, :], (h, w))
    color = rgb255.astype(np.float64)
    # 预计算 3×3 邻域候选中心索引与固定空间距离（中心位置不随迭代移动）
    compact = float(compactness) / float(step)
    offsets = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    cand_idx: list[np.ndarray] = []
    cand_dsp: list[np.ndarray] = []
    yy = np.arange(h, dtype=np.float64)[:, None]
    xx = np.arange(w, dtype=np.float64)[None, :]
    for dy, dx in offsets:
        cyi = np.clip(gyi + dy, 0, gh - 1)
        cxi = np.clip(gxi + dx, 0, gw - 1)
        cand_idx.append((cyi * gw + cxi).astype(np.int32))
        py = cy_grid[cyi, cxi].astype(np.float64)
        px = cx_grid[cyi, cxi].astype(np.float64)
        cand_dsp.append(((yy - py) ** 2 + (xx - px) ** 2) * (compact * compact))
    best_d = np.full((h, w), np.inf, dtype=np.float64)
    best_i = np.zeros((h, w), dtype=np.int32)
    flat_mask = mask.ravel()
    for _ in range(max(1, int(iters))):
        best_d.fill(np.inf)
        for oi in range(len(offsets)):
            idx = cand_idx[oi]
            cand_col = center_col[idx]  # (h,w,3)
            dcol = ((color - cand_col) ** 2).sum(axis=2)
            d = dcol + cand_dsp[oi]
            d = np.where(mask, d, np.inf)
            upd = d < best_d
            best_d = np.where(upd, d, best_d)
            best_i = np.where(upd, idx, best_i)
        # 更新各中心的颜色（只统计 mask 内像素；无像素的保持原值）
        sel = flat_mask
        lab = best_i.ravel()[sel]
        cnt = np.bincount(lab, minlength=gh * gw).astype(np.float64)
        sums = np.stack(
            [
                np.bincount(lab, weights=color[..., c].ravel()[sel], minlength=gh * gw)
                for c in range(3)
            ],
            axis=1,
        )
        exists = cnt > 0.0
        center_col[exists] = sums[exists] / cnt[exists, None]
    # 超像素均色 → 最近调色板墨色
    if len(palette255) == 0:
        return np.full((h, w), -1, dtype=np.int32)
    d2 = ((center_col[:, None, :] - palette255[None, :, :]) ** 2).sum(axis=2)
    sp_to_pal = np.argmin(d2, axis=1).astype(np.int32)
    labels = sp_to_pal[best_i]
    labels = np.where(mask, labels, -1).astype(np.int32)
    return labels


# ---------------------------------------------------------------- 主入口
def _count_unique_colors(rgb_u8: np.ndarray) -> int:
    """唯一颜色数（最多抽样 200 万像素）。classify 未调用时的兜底。"""
    flat = np.asarray(rgb_u8).reshape(-1, 3)
    if flat.shape[0] > 2_000_000:
        step = flat.shape[0] // 2_000_000 + 1
        flat = flat[::step]
    packed = (
        (flat[:, 0].astype(np.uint32) << 16)
        | (flat[:, 1].astype(np.uint32) << 8)
        | flat[:, 2].astype(np.uint32)
    )
    return int(np.unique(packed).size)


def _augment_pale_band_palette(
    rgb255: np.ndarray,
    visible: np.ndarray,
    bg255: np.ndarray,
    centers: np.ndarray,
    weights: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, int]:
    """给近背景的"浅色内容"补最多 _PALE_BAND_MAX_ADD 个调色板颜色。

    背景判定/覆盖率都以"最近墨色"为准；浅色内容（浅肤色/浅蓝衣褶/浅灰高光）与背景
    只差 6–24 色阶，若全局调色板没有它们的颜色，就永远落到背景 → 填色出现白洞。
    """
    if rgb255.size == 0:
        return centers, weights, 0
    bg = np.asarray(bg255, dtype=np.float64).reshape(1, 1, 3)
    px = rgb255.astype(np.float64)
    dist_bg = np.linalg.norm(px - bg, axis=2)
    mx = px.max(axis=2)
    mn = px.min(axis=2)
    band = visible & (dist_bg > _PALE_BAND_MIN_DBG) & (mx >= _PALE_BAND_MIN_LUMA) & ((mx - mn) <= _PALE_BAND_MAX_CHROMA)
    # 只保留"封闭"的浅色块（不连画布边界）：白纸/边角噪声与背景连通，补色会把整张纸
    # 变成"墨色内容"（horn 实测 PSNR 21.1→13.5 的反例）。
    if bool(band.any()):
        lab_b, n_b = label_components(band)
        border_ids = set(np.concatenate([lab_b[0, :], lab_b[-1, :], lab_b[:, 0], lab_b[:, -1]]).tolist()) - {0}
        keep = np.zeros(n_b + 1, dtype=bool)
        sizes = np.bincount(lab_b.ravel())
        min_keep = max(64, int(0.0008 * float(visible.sum())))
        for cid in range(1, n_b + 1):
            if cid in border_ids:
                continue
            if int(sizes[cid]) >= min_keep:
                keep[cid] = True
        band = keep[lab_b]
    n_band = int(band.sum())
    if n_band < _PALE_BAND_MIN_PX:
        return centers, weights, 0
    sub = np.clip(px[band], 0.0, 255.0)
    centers255 = np.asarray(centers, dtype=np.float64) * 255.0 if len(centers) else np.zeros((0, 3))
    # 粗量化分桶（16 级/通道），按像素数取最大的几桶；桶均值即候选色
    q = np.clip((sub // 16).astype(np.int64), 0, 15)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    uniq, counts = np.unique(keys, return_counts=True)
    order = np.argsort(-counts)
    add_cols: list[np.ndarray] = []
    add_w: list[float] = []
    for oi in order:
        if len(add_cols) >= _PALE_BAND_MAX_ADD:
            break
        sel = keys == uniq[oi]
        col = sub[sel].mean(axis=0)
        if float(np.linalg.norm(col - bg.reshape(3))) < _PALE_BAND_MIN_DBG:
            continue
        if len(centers255) and float(np.min(np.linalg.norm(centers255 - col[None, :], axis=1))) < _PALE_BAND_EXIST_DIST:
            continue
        if add_cols and float(np.min(np.linalg.norm(np.asarray(add_cols) - col[None, :], axis=1))) < _PALE_BAND_EXIST_DIST:
            continue
        add_cols.append(col)
        add_w.append(float(counts[oi]))
    if not add_cols:
        return centers, weights, 0
    add01 = np.asarray(add_cols, dtype=np.float64) / 255.0
    centers = np.vstack([np.asarray(centers, dtype=np.float64), add01]) if len(centers) else add01
    weights = np.concatenate([np.asarray(weights, dtype=np.float64), np.asarray(add_w, dtype=np.float64)]) if len(weights) else np.asarray(add_w, dtype=np.float64)
    return centers, weights, len(add_cols)


def analyze(img: dict, cfg: FBSConfig, warn: WarningLog, policy_klass: str | None = None) -> dict:
    """Stage0.5 主入口：背景 + 调色板 + 覆盖率场 + 局部质量守恒掩码。

    policy_klass：调用方已知的分类（如 document 的 classify/force-class 结果），
    用于**按类别分档**的颜色策略（近背景浅色补色等）；不传时退回 img 内的 klass。
    注意：插画 SLIC 空间正则仍只看 img 内的 klass（保持既有行为，避免静默改变指标）。

    返回键（下游 decompose 依赖）：
      bg       (3,) float32 0..1
      palette  (K,3) float32 0..1  墨色（不含背景）
      labels   (H,W) int32，-1=背景，>=0 为 palette 下标
      cov      (H,W) float32 0..1  覆盖率场
      mask     (H,W) bool          墨迹二值掩码
    另含 stats / klass 等诊断键。
    """
    rgb = np.asarray(img["rgb"], dtype=np.float64)
    alpha = np.asarray(img["alpha"], dtype=np.float64)
    rgba = np.asarray(img["rgba"], dtype=np.uint8)
    h, w = rgb.shape[:2]
    visible = alpha > float(np.clip(cfg.alpha_threshold, 0, 255)) / 255.0
    rgb255 = rgb * 255.0

    bg, bg_info = estimate_background(img)
    klass = str(img.get("klass", "unknown"))
    klass_policy = str(policy_klass or klass)
    uniq_colors = int(img.get("class_evidence", {}).get("unique_colors", 0))
    if uniq_colors <= 0:
        uniq_colors = _count_unique_colors(rgba[..., :3])
    evidence: dict[str, Any] = {
        "unique_colors": uniq_colors,
        "bg_noise_px": bg_info.get("bg_noise_px", 0.0),
        "klass": klass,
    }
    centers, weights, pstats = build_palette(rgb, rgb255, visible, bg, cfg, evidence)
    # 近背景浅色内容补色（避免浅肤色/浅蓝衣褶被判成背景 → 填色白洞）。
    # 仅用于线稿/扁平类：插画类的浅色渐变本就走颜色场，补色会改变其调色板（pale 实测 −0.2dB）。
    bg255 = (bg * 255.0).reshape(1, 1, 3)
    if klass_policy != "illustration" and bool(getattr(cfg, "pale_band_palette", False)):
        centers, weights, n_pale = _augment_pale_band_palette(rgb255, visible, bg255, centers, weights)
    else:
        n_pale = 0
    if n_pale:
        pstats = dict(pstats)
        pstats["pale_band_added"] = int(n_pale)
        pstats["palette_size"] = int(len(centers))
    evidence.update(pstats)

    stats: dict[str, Any] = {
        "bg_source": bg_info,
        "palette_size": int(len(centers)),
        "palette_rgb255": [
            [int(round(float(c) * 255.0)) for c in col] for col in np.asarray(centers)
        ],
        "palette_weights": [int(v) for v in np.asarray(weights)],
    }
    stats.update({k: v for k, v in pstats.items() if k not in stats})

    if len(centers) == 0:
        # 无前景/纯色图：无墨色 → 空掩码，decompose 走纯色路径
        warn.add("W_SOLID_COLOR", "未检出任何墨色（图像可能为纯色或背景色单一）")
        cov = np.zeros((h, w), dtype=np.float32)
        labels = np.full((h, w), -1, dtype=np.int32)
        mask = np.zeros((h, w), dtype=bool)
        return {
            "bg": bg.astype(np.float32),
            "palette": np.zeros((0, 3), dtype=np.float32),
            "labels": labels,
            "cov": cov,
            "mask": mask,
            "klass": img.get("klass", "unknown"),
            "stats": stats,
        }

    refs = np.vstack([bg[None, :], centers]).astype(np.float64)  # 0=bg, 1..K=墨色
    bg255 = (bg * 255.0).reshape(1, 1, 3)
    dist_bg = np.linalg.norm(rgb255 - bg255, axis=2)
    # 背景/前景判定：
    # 1) 与背景色差 > fg_tol 的像素肯定是内容（旧行为）；
    # 2) 近背景色的像素里，"比背景更接近某个墨色"的像素也是内容（最近色分类）。
    #    这样被线条包围的浅色真实内容（肤色/浅高光/浅蓝衣褶）不会被当成背景剔除，
    #    同时纯背景与 JPEG 晕圈（更接近背景色）仍归零，不引入噪声区域。
    fg_tol = float(pstats.get("fg_tol_px", 6.0))
    stats["fg_tol_px"] = round(float(fg_tol), 2)

    # 逐像素最近墨色（用于 cov 投影方向）
    n_ink = len(centers)
    best = np.full((h, w), np.inf, dtype=np.float64)
    lab_ink = np.ones((h, w), dtype=np.int32)  # 默认 1（至少有一个墨色）
    for k in range(n_ink):
        d = ((rgb255 - centers[k][None, None, :] * 255.0) ** 2).sum(axis=2)
        upd = d < best
        best = np.where(upd, d, best)
        lab_ink[upd] = k + 1
    dist_ink = np.sqrt(np.maximum(best, 0.0))
    restored = dist_ink + _FG_INK_MARGIN < dist_bg
    stats["near_bg_restored_px"] = int(restored.sum())
    cov = coverage_field(rgb, bg, refs, lab_ink, dist_bg, fg_tol)
    if bool(restored.any()):
        cov = np.where(restored, np.maximum(cov, _FG_RESTORED_COV_MIN), cov)

    # --- 局部质量守恒二值化 ---
    tau = local_mass_tau(cov, int(cfg.coverage_block), _TAU_LO, _TAU_HI)
    tau_px = upsample_tau(tau, h, w, int(cfg.coverage_block))
    # 用 >= 而不是 >：τ 取的是「第 k 大 cov 值」，用 > 会恰好少收一个像素；
    # 对覆盖率恰好 0.5 的半透明/细线，> 会让整条线消失（>= 保住它）。
    mask = cov >= tau_px
    # 全透明像素不可能是墨
    mask &= visible
    mask, n_specks = _drop_noise_specks(mask, _NOISE_COMPONENT_PX)

    labels = np.where(mask, lab_ink - 1, -1).astype(np.int32)

    # --- 空间正则：复杂插画的逐像素标签会碎片化（抗锯齿带上来回跳变），
    # 先用超像素把标签做得空间连贯，再做小连通块吸收。线稿/平涂不需要（颜色本身成片）。
    if klass == "illustration" and uniq_colors > 512 and bool(mask.any()):
        n_segments = int(np.clip(int(mask.sum()) / _SLIC_PX_PER_SEGMENT, 16, 6000))
        labels = _slic_assignment(
            rgb255,
            mask,
            np.clip(centers * 255.0, 0.0, 255.0),
            n_segments,
            _SLIC_COMPACTNESS,
            _SLIC_ITERS,
        )
        stats["slic_segments"] = n_segments

    if bool(cfg.absorb_small_components) and bool(mask.any()):
        # 暗墨保护：保留"暗且与邻居色差大"的小块（细线/排线/瞳孔），避免被并进浅色区域。
        # 插画类默认启用（pale +0.47dB）；线稿类启用会改变整体调子（manga PSNR −0.45 /
        # SSIM +0.011，纹理更清楚但像素更"脏"），因此放在 --texture-preserve 实验开关后面。
        _protect = (klass_policy == "illustration") or (
            bool(getattr(cfg, "texture_preserve", False)) and klass_policy != "illustration"
        )
        labels, ab_stats = absorb_small_components(
            labels, mask, int(cfg.min_region_area),
            palette255=np.asarray(centers) * 255.0 if (len(centers) and _protect) else None,
            protect_delta=_ABSORB_DARK_PROTECT_DELTA if _protect else 0.0,
            protect_max_dt=None if klass_policy == "illustration" else _ABSORB_PROTECT_MAX_DT,
            protect_min_bg_luma=0.0 if klass_policy == "illustration" else _ABSORB_PROTECT_MIN_BG_LUMA,
        )
        stats["absorbed_small"] = int(ab_stats["absorbed"])
        stats["dropped_small"] = int(ab_stats["dropped"])
        stats["protected_small_dark"] = int(ab_stats.get("protected", 0))
        mask = labels >= 0
    stats["noise_specks"] = int(n_specks)

    if not bool(mask.any()):
        warn.add("W_SOLID_COLOR", "局部质量守恒阈值为整幅图判为背景，未检出墨迹")
    # 墨量守恒自检：二值面积 / cov 总墨量（越接近 1 越好；<0.5 说明细线被判太狠）
    cov_sum = float(cov.sum(dtype=np.float64))
    area = int(mask.sum())
    if cov_sum > 0.0:
        ratio = area / cov_sum
        stats["mass_conservation"] = round(ratio, 4)
        stats["cov_mass_px"] = round(cov_sum, 2)
        if ratio < 0.5:
            warn.add(
                "W_SMALL_REGIONS",
                f"掩码面积/墨量={ratio:.2f} 偏低：大量亚像素结构未进入掩码（将按细线处理）",
                severity="info",
            )
    stats["mask_frac"] = round(float(mask.mean()), 4)
    stats["palette_size"] = int(n_ink)

    return {
        "bg": bg.astype(np.float32),
        "palette": np.clip(centers, 0.0, 1.0).astype(np.float32),
        "labels": labels,
        "cov": cov,
        "mask": mask,
        "klass": img.get("klass", "unknown"),
        "stats": stats,
    }
