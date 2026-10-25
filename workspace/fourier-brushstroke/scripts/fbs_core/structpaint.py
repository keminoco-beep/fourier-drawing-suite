"""结构感知笔触绘画（structure-aware brush painting）+ 局部策略实验（多臂老虎机）。

用户要求（产品目标）
------------------
不要"错误驱动的随机补丁"式笔触。人类画师按**结构**下笔：
- 线条（轮廓/发丝/内部线）：笔触**沿线条走向**，一笔顺下来，压力两端收；
- 平涂色块（裙子、皮肤、帽子）：宽笔**顺着形体的主方向**铺，先大后小；
- 柔和明暗（阴影/渐变）：软笔/喷枪大笔低透明度叠；
- 尖锐结构/细节（眼睛、高光、缎带结）：小号硬笔/墨线笔点画；
- 作画顺序：先铺大色块 → 中景 → 线条与细节（最后勾线）。

实现分三步（对应"局部实验 → 学习 → 完整图"）：
1. `build_structure()`：用调色板/中轴分解得到**结构元素**（region_fill 块面 + medial 线），
   并把元素分类成 flat / shade / detail / line_fine / line_thick，同时算块面主方向；
2. `run_policy_experiments()`：在图像上按类取局部 patch，对每个候选笔刷策略（预设+宽度/长度
   缩放）实际绘制并评分（PSNR/SSIM），记录 JSON/Markdown；用 **UCB 多臂老虎机**从记录里
   选每类的最优策略（有探索项，不是拍脑袋选笔）；
3. `paint_structure()`：按类策略生成纯笔触元素（傅里叶曲线 + 画笔属性），先铺后勾，
   构建 `.fbs` 文档渲染。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

import numpy as np

from . import brushlib
from .config import FBSConfig
from .util import WarningLog, gaussian_blur

# 结构类别
CLASSES = ("flat", "shade", "detail", "line_fine", "line_thick")

# 每类候选笔刷策略（预设 id, 宽度缩放, 长度缩放, alpha 缩放）
POLICY_CANDIDATES: dict[str, list[dict[str, Any]]] = {
    "flat": [
        {"preset": "bristle_oil", "width_scale": 1.5, "length_scale": 1.2, "alpha_scale": 1.0},
        {"preset": "dry_flat", "width_scale": 1.6, "length_scale": 1.3, "alpha_scale": 1.0},
        {"preset": "marker_chisel", "width_scale": 1.3, "length_scale": 1.1, "alpha_scale": 0.95},
        {"preset": "basic_round_soft", "width_scale": 1.5, "length_scale": 1.0, "alpha_scale": 0.95},
    ],
    "shade": [
        {"preset": "basic_airbrush", "width_scale": 1.8, "length_scale": 1.1, "alpha_scale": 1.0},
        {"preset": "watercolor_wash", "width_scale": 1.6, "length_scale": 1.2, "alpha_scale": 1.0},
        {"preset": "pastel", "width_scale": 1.4, "length_scale": 1.0, "alpha_scale": 1.0},
        {"preset": "basic_round_soft", "width_scale": 1.6, "length_scale": 1.0, "alpha_scale": 1.0},
    ],
    "detail": [
        {"preset": "marker_chisel", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
        {"preset": "ink_gpen", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
        {"preset": "basic_round_hard", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
    ],
    "line_fine": [
        {"preset": "ink_fineliner", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
        {"preset": "ink_gpen", "width_scale": 0.9, "length_scale": 1.0, "alpha_scale": 1.0},
        {"preset": "pencil_2b", "width_scale": 0.9, "length_scale": 1.0, "alpha_scale": 1.0},
    ],
    "line_thick": [
        {"preset": "ink_gpen", "width_scale": 1.05, "length_scale": 1.0, "alpha_scale": 1.0},
        {"preset": "ink_brush_rough", "width_scale": 1.05, "length_scale": 1.0, "alpha_scale": 1.0},
        {"preset": "bristle_oil", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
    ],
}


def _lum(rgb: np.ndarray) -> float:
    return float(0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2])


def _region_pixels(labels: np.ndarray, rid: int) -> tuple[np.ndarray, np.ndarray]:
    ys, xs = np.nonzero(labels == rid)
    return ys, xs


def _region_mask_from_poly(poly_world: np.ndarray, W: int, H: int):
    """块面多边形 → (bbox, mask_crop)；hatch 笔触用它做"是否在区域内"校验。"""
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    poly = np.asarray(poly_world, dtype=np.float64).reshape(-1, 2)
    if len(poly) < 3:
        return None
    img = _Image.new("L", (W, H), 0)
    _ImageDraw.Draw(img).polygon([(float(x) * W, float(y) * H) for x, y in poly], fill=255)
    arr = np.asarray(img) > 127
    ys, xs = np.nonzero(arr)
    if len(ys) == 0:
        return None
    x0, y0 = int(xs.min()), int(ys.min())
    x1, y1 = int(xs.max()) + 1, int(ys.max()) + 1
    return {"bbox": (x0, y0, x1, y1), "mask": arr[y0:y1, x0:x1]}


def _region_pixels_from_poly(poly_world: np.ndarray, W: int, H: int) -> tuple[np.ndarray, np.ndarray]:
    """块面多边形（世界坐标）→ 像素集合（region_id 是顺序号不是 label 值，必须用几何反推）。"""
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    poly = np.asarray(poly_world, dtype=np.float64).reshape(-1, 2)
    if len(poly) < 3:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    img = _Image.new("L", (W, H), 0)
    _ImageDraw.Draw(img).polygon(
        [(float(x) * W, float(y) * H) for x, y in poly], fill=255
    )
    arr = np.asarray(img)
    ys, xs = np.nonzero(arr > 127)
    return ys, xs


def _pca_orientation(ys: np.ndarray, xs: np.ndarray) -> float:
    if len(ys) < 3:
        return 0.0
    x = xs.astype(np.float64)
    y = ys.astype(np.float64)
    x -= x.mean()
    y -= y.mean()
    cov = np.array([[np.mean(x * x), np.mean(x * y)], [np.mean(x * y), np.mean(y * y)]])
    w, v = np.linalg.eigh(cov)
    vec = v[:, int(np.argmax(w))]
    return float(math.atan2(vec[1], vec[0]))


def _tangent_orientation(
    rgb: np.ndarray, ys: np.ndarray, xs: np.ndarray, fallback: float
) -> float:
    """块面的"顺形方向"：区域内结构张量切向的中位角；区域太平时用 PCA 主方向。"""
    if len(ys) < 32:
        return fallback
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    lum_s = gaussian_blur(lum.astype(np.float32), 1.6).astype(np.float64)
    gy, gx = np.gradient(lum_s)
    gxx = gaussian_blur((gx * gx).astype(np.float32), 3.0)
    gyy = gaussian_blur((gy * gy).astype(np.float32), 3.0)
    gxy = gaussian_blur((gx * gy).astype(np.float32), 3.0)
    sel = np.zeros_like(lum, dtype=bool)
    sel[ys, xs] = True
    mag = gxx[sel] + gyy[sel]
    if float(np.median(mag)) < 1e-5:
        return fallback
    ang = 0.5 * np.arctan2(2.0 * gxy[sel], gxx[sel] - gyy[sel]) + math.pi * 0.5
    # mod π 的中位角：用双倍角平均
    c = float(np.mean(np.cos(2.0 * ang)))
    s = float(np.mean(np.sin(2.0 * ang)))
    if abs(c) + abs(s) < 1e-6:
        return fallback
    return float(0.5 * math.atan2(s, c))


def _bridge_line_endpoints(line_mask: np.ndarray, lines: list[dict], L: float, max_gap_px: float = 5.0) -> np.ndarray:
    """把相近的线条端点连起来，修补中轴分解造成的线稿缺口（颜色泄漏的根源）。"""
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    ends: list[tuple[float, float]] = []
    for ln in lines:
        pts = np.asarray(ln.get("points_world", []), dtype=np.float64) * L
        if len(pts) < 2:
            continue
        ends.append((float(pts[0, 0]), float(pts[0, 1])))
        ends.append((float(pts[-1, 0]), float(pts[-1, 1])))
    if len(ends) < 2:
        return line_mask
    H, W = line_mask.shape
    link = _Image.new("L", (W, H), 0)
    dr = _ImageDraw.Draw(link)
    pts_arr = np.asarray(ends, dtype=np.float64)
    used = np.zeros(len(ends), dtype=bool)
    for a in range(len(ends)):
        if used[a]:
            continue
        d = np.hypot(pts_arr[:, 0] - pts_arr[a, 0], pts_arr[:, 1] - pts_arr[a, 1])
        d[a] = 1e9
        j = int(np.argmin(d))
        if d[j] <= max_gap_px and not used[j]:
            dr.line([ends[a], ends[j]], fill=255, width=2)
            used[a] = used[j] = True
    return line_mask | (np.asarray(link) > 127)


def build_contour_regions(
    rgb: np.ndarray,
    lines: list[dict],
    bg_color: np.ndarray,
    W: int,
    H: int,
    L: int,
    warn: WarningLog | None = None,
    analysis: dict | None = None,
    std_threshold: float | None = None,
    detail_area_frac: float | None = None,
) -> list[dict]:
    """线稿封闭轮廓分区：线条作墙，连通域=被围住的色块（比调色板连通块更符合读图）。

    步骤：
    1. 把线条结构光栅化成 line mask（沿骨架的粗线）；
    2. 与背景色接近且**与画布边界连通**的像素 = 背景（画布外的白）；
       被线条围住的白色（蕾丝/高光）不会被当背景剔除；
    3. 对 "非线 & 非背景" 像素做 8 连通域 → 封闭色块；
    4. 每个色块输出：mask 裁剪、边界多边形（世界坐标）、主方向、代表色、类别
       （detail/flat/shade 按面积与颜色标准差）。
    """
    from PIL import Image as _Image, ImageDraw as _ImageDraw
    try:
        from scipy import ndimage as _ndi
    except Exception:  # pragma: no cover - scipy 缺失时退化为调色板分区
        if warn is not None:
            warn.add("W_CONTOUR_SKIP", "scipy 不可用，封闭轮廓分区跳过", severity="degrade")
        return []
    from .decompose import trace_boundary_loops

    line_img = _Image.new("L", (W, H), 0)
    dr = _ImageDraw.Draw(line_img)
    for ln in lines:
        pts = np.asarray(ln.get("points_world", []), dtype=np.float64) * L
        if len(pts) < 2:
            continue
        w = np.asarray(ln.get("widths_world", []), dtype=np.float64) * L
        wpix = int(max(1, round(float(np.median(w)) * 1.35 + 1.0))) if len(w) else 2
        dr.line([(float(x), float(y)) for x, y in pts], fill=255, width=wpix, joint="curve")
        r = max(1.0, wpix / 2.0)
        for (x, y) in pts:
            dr.ellipse([x - r, y - r, x + r, y + r], fill=255)
    line_mask = np.asarray(line_img) > 127
    # 线稿像素："最暗的调色板色" + 低亮度像素（比逐条骨架光栅化更完整，避免漏线导致漏色）
    lum_img = rgb @ np.array([0.2126, 0.7152, 0.0722])
    line_mask = line_mask | (lum_img < 0.22)
    # 颜色陡变（线条/色块边界）也是"墙"：动漫线稿的边界常被抗锯齿冲淡，单靠低亮度会漏线
    img_s = gaussian_blur(rgb.astype(np.float32), 1.2).astype(np.float64)
    gmag = np.zeros((H, W), dtype=np.float64)
    for _c in range(3):
        _gy, _gx = np.gradient(img_s[..., _c])
        gmag = np.maximum(gmag, np.hypot(_gx, _gy))
    line_mask = line_mask | (gmag > 0.085)
    # 改进一（线稿闭合）：
    # 1) 端点桥接：把距离 <= 5px 的线条端点用短线连起来（中轴分解常把一条线拆成几段，
    #    端点处 1~3px 的缺口就是颜色泄漏的通道）；
    # 2) 形态学闭运算：dilate→erode 封住 1~2px 的缝；
    # 3) 最后 dilate 1px 作为"墙"。
    line_mask = _bridge_line_endpoints(line_mask, lines, L)
    line_mask = _ndi.binary_closing(line_mask, structure=np.ones((3, 3), dtype=bool), iterations=1)
    line_mask = _ndi.binary_dilation(line_mask, structure=np.ones((3, 3), dtype=bool), iterations=1)

    bg = np.asarray(bg_color, dtype=np.float64).reshape(3)
    dist = np.linalg.norm(rgb - bg[None, None, :], axis=2)
    near_bg = (dist < 0.06) & ~line_mask
    lab_bg, n_bg = _ndi.label(near_bg, structure=np.ones((3, 3), dtype=int))
    if n_bg > 0:
        border = np.concatenate(
            [lab_bg[0, :], lab_bg[-1, :], lab_bg[:, 0], lab_bg[:, -1]]
        )
        border_ids = set(int(v) for v in np.unique(border) if v != 0)
        bg_mask = np.isin(lab_bg, list(border_ids)) if border_ids else np.zeros((H, W), bool)
    else:
        bg_mask = np.zeros((H, W), bool)
    paint = ~line_mask & ~bg_mask
    lab, n = _ndi.label(paint, structure=np.ones((3, 3), dtype=int))
    if n <= 0:
        return []
    # 改进二（区域生长）：线条像素（以及被墙切掉的边缘像素）按最近邻分配给相邻区域，
    # 让填色覆盖到线条下方（线条最后画在上层），避免"色块之间露白缝"。
    grown = lab.copy()
    grow_zone = (~bg_mask) & (lab == 0)
    if bool(grow_zone.any()):
        _, (iy, ix) = _ndi.distance_transform_edt(lab == 0, return_indices=True)
        grown[grow_zone] = lab[iy[grow_zone], ix[grow_zone]]
    # 小区域按"邻居多数"并入（避免碎点）
    counts = np.bincount(grown.ravel(), minlength=n + 1)
    min_merge = max(12, int(0.00002 * W * H))
    small = [cid for cid in range(1, n + 1) if 0 < int(counts[cid]) < min_merge]
    if small:
        for cid in small:
            m = grown == cid
            if not bool(m.any()):
                continue
            dil = _ndi.binary_dilation(m, structure=np.ones((3, 3), dtype=bool)) & ~m
            nb = grown[dil]
            nb = nb[nb > 0]
            if nb.size:
                target = int(np.bincount(nb).argmax())
                grown[m] = target
        # 重新编号
        uniq = np.unique(grown)
        remap = {int(u): k + 1 for k, u in enumerate(uniq[uniq > 0])}
        grown2 = np.zeros_like(grown)
        for u, k in remap.items():
            grown2[grown == u] = k
        grown = grown2
        n = len(remap)
    min_area = max(6, int(0.00004 * W * H))
    counts = np.bincount(grown.ravel(), minlength=n + 1)
    out: list[dict] = []
    for cid in range(1, n + 1):
        if int(counts[cid]) < min_area:
            continue
        m = grown == cid
        ys, xs = np.nonzero(m)
        if len(ys) < 3:
            continue
        x0, y0 = int(xs.min()), int(ys.min())
        x1, y1 = int(xs.max()) + 1, int(ys.max()) + 1
        sub = m[y0:y1, x0:x1]
        geom = {"bbox": (x0, y0, x1, y1), "mask": sub}
        loops = trace_boundary_loops(m)
        if not loops:
            continue
        outer = max(loops, key=lambda p: abs(float(np.sum(p[:-1, 0] * p[1:, 1] - p[1:, 0] * p[:-1, 1]))) if len(p) > 2 else 0.0)
        if len(outer) < 3:
            continue
        poly_world = outer.astype(np.float64) / float(L)
        col = rgb[m].mean(axis=0)
        col_std = float(np.mean(np.std(rgb[m], axis=0)))
        area = int(counts[cid])
        area_frac = float(detail_area_frac) if detail_area_frac is not None else 0.0012
        flat_thr = float(std_threshold) if std_threshold is not None else 0.06
        if area < area_frac * W * H:
            klass = "detail"
        elif col_std > flat_thr:
            klass = "shade"
        else:
            klass = "flat"
        out.append(
            {
                "kind": "region",
                "class": klass,
                "rid": -1,
                "poly_world": poly_world,
                "ys": ys,
                "xs": xs,
                "area_px": area,
                "orientation": _pca_orientation(ys, xs),
                "color": np.clip(col, 0.0, 1.0),
                "alpha": 1.0,
                "bbox_px": (x0, y0, x1, y1),
                "geom": geom,
            }
        )
    if warn is not None:
        n_flat = sum(1 for st in out if st["class"] == "flat")
        n_shade = sum(1 for st in out if st["class"] == "shade")
        n_detail = sum(1 for st in out if st["class"] == "detail")
        warn.add(
            "W_CONTOUR_REGIONS",
            f"封闭轮廓分区：{len(out)} 个（flat={n_flat} shade={n_shade} detail={n_detail}，"
            f"线稿遮挡 {int(line_mask.sum())}px，背景剔除 {int(bg_mask.sum())}px）",
            severity="info",
        )
    return out


def build_structure(
    img: dict,
    analysis: dict,
    dec: dict,
    cfg: FBSConfig,
    warn: WarningLog,
    segmentation: str = "palette",
    std_threshold: float | None = None,
    detail_area_frac: float | None = None,
) -> dict:
    """分解结果 → 结构元素表（块面/线），带类别、方向、尺度、颜色。"""
    rgb = np.asarray(img["rgb"], dtype=np.float64)
    alpha = np.asarray(img["alpha"], dtype=np.float64)
    labels = np.asarray(analysis["labels"])
    W = int(img["width"])
    H = int(img["height"])
    L = int(img["long_side"])
    fills = [e for e in dec["elements"] if e["fill_role"] == "region_fill"]
    branches = [e for e in dec["elements"] if e["fill_role"] != "region_fill"]

    structures: list[dict[str, Any]] = []
    for el in fills:
        rid = int(el.get("region_id", -1))
        poly_w = np.asarray(el.get("fill_poly"), dtype=np.float64)
        ys, xs = _region_pixels_from_poly(poly_w, W, H)
        area = int(len(ys))
        if area <= 0:
            continue
        geom = _region_mask_from_poly(poly_w, W, H)
        fallback = _pca_orientation(ys, xs)
        ang = _tangent_orientation(rgb, ys, xs, fallback)
        col = np.asarray(el["colors"], dtype=np.float64).reshape(-1, 3)
        alphas = np.asarray(el["alphas"], dtype=np.float64).reshape(-1)
        c = col.mean(axis=0) if len(col) else np.array([0.5, 0.5, 0.5])
        cf = None
        # region 颜色场（渲染端结构：[(grid), colors...]；这里只用于判断是否渐变块）
        cf_std = 0.0
        if rid >= 0 and rid < len(dec.get("regions", [])):
            reg = dec["regions"][rid]
            cf = reg.get("color_field")
            if isinstance(cf, dict) and cf.get("colors"):
                arr = np.asarray(cf["colors"], dtype=np.float64) / 255.0
                if arr.ndim == 2 and arr.shape[0] >= 2:
                    cf_std = float(np.mean(np.std(arr, axis=0)))
        # 结构分级：小块=细节；大块且色场平=平涂；其余=柔和明暗（渐变/阴影）
        area_frac = float(detail_area_frac) if detail_area_frac is not None else 0.0012
        flat_thr = float(std_threshold) if std_threshold is not None else 0.10
        if area < area_frac * W * H:
            klass = "detail"
        elif cf_std <= flat_thr:
            klass = "flat"
        else:
            klass = "shade"
        structures.append(
            {
                "kind": "region",
                "class": klass,
                "rid": rid,
                "poly_world": np.asarray(el["fill_poly"], dtype=np.float64),
                "ys": ys,
                "xs": xs,
                "area_px": area,
                "orientation": ang,
                "color": np.clip(c, 0.0, 1.0),
                "alpha": float(alphas.mean()) if len(alphas) else 1.0,
                "bbox_px": (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                "geom": geom,
            }
        )

    for el in branches:
        pts = np.asarray(el["points"], dtype=np.float64)
        widths = np.asarray(el["widths"], dtype=np.float64)
        cols = np.asarray(el["colors"], dtype=np.float64).reshape(-1, 3)
        alphas = np.asarray(el["alphas"], dtype=np.float64)
        if len(pts) < 2:
            continue
        med_w_px = float(np.median(widths)) * L if len(widths) else 0.0
        c = cols.mean(axis=0) if len(cols) else np.array([0.2, 0.2, 0.2])
        dark = _lum(c) < 0.45
        if med_w_px <= 2.2 or len(pts) <= 6:
            klass = "line_fine" if med_w_px <= 1.6 else "line_thick"
        else:
            klass = "line_thick"
        structures.append(
            {
                "kind": "line",
                "class": klass,
                "points_world": pts,
                "widths_world": widths,
                "colors": cols,
                "alphas": alphas,
                "width_px": med_w_px,
                "length_px": float(np.sum(np.linalg.norm(np.diff(pts * L, axis=0), axis=1))),
                "color": np.clip(c, 0.0, 1.0),
                "dark": bool(dark),
                "bbox_px": (
                    int(pts[:, 0].min() * L), int(pts[:, 1].min() * L),
                    int(pts[:, 0].max() * L) + 1, int(pts[:, 1].max() * L) + 1,
                ),
            }
        )
    if segmentation == "contour":
        # 封闭轮廓分区：线稿当"墙"，把图分成被线条围住的封闭区域（动漫/插画的标准读法）。
        line_structs = [st for st in structures if st["kind"] == "line"]
        contour_regions = build_contour_regions(
            rgb, line_structs, np.asarray(analysis.get("bg", [1.0, 1.0, 1.0]), dtype=np.float64),
            W, H, L, warn, analysis=analysis,
            std_threshold=std_threshold, detail_area_frac=detail_area_frac,
        )
        structures = contour_regions + line_structs
    counts: dict[str, int] = {}
    for s in structures:
        counts[s["class"]] = counts.get(s["class"], 0) + 1
    warn.add("W_STRUCTURE", "结构分类：" + "、".join(f"{k}={v}" for k, v in sorted(counts.items())), severity="info")
    return {"structures": structures, "counts": counts, "W": W, "H": H, "L": L, "rgb": rgb}


# ---------------------------------------------------------------- 策略 → 笔触生成
def _fill_strokes_for_region(
    struct: dict,
    rgb: np.ndarray,
    L: int,
    policy: dict,
    width_cap_px: float | None = None,
) -> list[dict]:
    """块面铺色 = 沿区域**中轴骨架**的宽笔（人体运笔：顺着形状走向一笔铺过去）。

    比上一版"平行排线"更接近人类铺色：
    - 中轴骨架给出区域的"形体走向"，笔触沿骨架走（头发顺发流、裙摆顺布褶）；
    - 笔宽 = 2×距离变换（正好覆盖该处厚度），端点压力略收；
    - 骨架在区域内部（由 mask 约束），不会画出区域外白圈；
    - 骨架覆盖不到的小洞用"点盘"补（半径=距离变换）。
    """
    from .decompose import clean_skeleton, skeleton_graph, skeletonize
    from .util import distance_transform

    ys = struct["ys"]
    xs = struct["xs"]
    if len(ys) == 0:
        return []
    geom = struct.get("geom")
    if not isinstance(geom, dict):
        return _hatch_fallback(struct, rgb, policy)
    bx0, by0, bx1, by1 = geom["bbox"]
    mask = np.asarray(geom["mask"], dtype=bool)
    if mask.size == 0 or mask.shape[0] < 2 or mask.shape[1] < 2:
        return _hatch_fallback(struct, rgb, policy)
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    sk = clean_skeleton(skeletonize(padded, 400))
    dt = distance_transform(padded).astype(np.float64)
    branches = skeleton_graph(sk)
    flat = struct["class"] == "flat"
    sigma = max(0.8, float(np.median(dt[padded])) * 0.8)
    rgb_s = gaussian_blur(rgb.astype(np.float32), sigma).astype(np.float64)
    base_col = np.asarray(struct["color"], dtype=np.float64)
    alpha = 1.0 if flat else 0.9
    width_k = float(policy.get("width_scale", 1.0))
    strokes: list[dict] = []
    covered = np.zeros_like(mask, dtype=bool)

    def _emit(points_local: np.ndarray, widths_local: np.ndarray, base_only: bool = False) -> None:
        if len(points_local) < 2:
            return
        pts = points_local.astype(np.float64).copy()
        pts[:, 0] += bx0 - 1
        pts[:, 1] += by0 - 1
        # 平滑（骨架是像素阶梯，画师一笔是圆顺的）
        for _ in range(3):
            if len(pts) > 2:
                pts[1:-1, 0] = 0.25 * pts[:-2, 0] + 0.5 * pts[1:-1, 0] + 0.25 * pts[2:, 0]
                pts[1:-1, 1] = 0.25 * pts[:-2, 1] + 0.5 * pts[1:-1, 1] + 0.25 * pts[2:, 1]
        n = len(pts)
        ss = np.linspace(0.0, 1.0, n)
        widths = np.interp(ss, np.linspace(0.0, 1.0, len(widths_local)), widths_local) * width_k
        # 重采样成至多 9 个点（Fourier 拟合更稳、文件更小）
        m = min(9, n)
        ss2 = np.linspace(0.0, 1.0, m)
        pts2 = np.stack([np.interp(ss2, ss, pts[:, 0]), np.interp(ss2, ss, pts[:, 1])], axis=1)
        w2 = np.interp(ss2, ss, widths)
        if pts2[-1, 0] == pts2[0, 0] and pts2[-1, 1] == pts2[0, 1]:
            return
        mid = pts2[len(pts2) // 2]
        miy = int(np.clip(round(mid[1] - 0.5), 0, rgb.shape[0] - 1))
        mix = int(np.clip(round(mid[0] - 0.5), 0, rgb.shape[1] - 1))
        col = base_col if flat else rgb_s[miy, mix]
        # 压力：中段满宽、两端 85%（铺色不做过强收锋）
        pres = np.interp(ss2, [0.0, 0.25, 0.5, 0.75, 1.0], [0.85, 0.97, 1.0, 0.97, 0.85])
        # 两遍画法（真实画师也是"先铺满底色、再用肌理笔扫一层"）：
        #   1) 底色笔：round 硬圆笔、alpha=1、宽度=2DT → 保证颜色铺满、无露白；
        #   2) 肌理笔：策略笔刷（鬃毛/干刷/粉笔…）低透明度扫在同一路径上 → 出现笔触肌理。
        # 按结构类别决定画法：
        #   flat   → 只用硬圆底色笔（干净平涂；不加肌理，避免色块发花）
        #   shade  → 底色 + 低透明度肌理（柔光/明暗过渡有笔触感）
        #   detail → 直接用策略笔（小号硬笔/墨线，平实填充细节）
        if struct["class"] == "flat":
            pass_list = (("basic_round_hard", 1.0, 0.86),)
        elif struct["class"] == "shade":
            pass_list = (("basic_round_hard", 1.0, 0.86), (str(policy["preset"]), 0.15, 0.9))
        else:
            pass_list = ((str(policy["preset"]), 1.0, 1.0),)
        active = pass_list[:1] if base_only else pass_list
        for pass_i, (preset_id, alpha_mult, width_mult) in enumerate(active):
            shape = str(brushlib.BRUSHES[preset_id]["shape"]) if preset_id in brushlib.BRUSHES else "round"
            strokes.append(
                {
                    "class": struct["class"],
                    "preset": preset_id,
                    "shape": shape,
                    "points_px": pts2,
                    "widths_px": np.minimum(
                        np.maximum(0.8, w2 * pres * width_mult),
                        float(width_cap_px) if width_cap_px else 1e9,
                    ),
                    "color": np.clip(col, 0.0, 1.0),
                    "alpha": float(np.clip(alpha * policy.get("alpha_scale", 1.0) * alpha_mult, 0.2, 1.0)),
                    "layer": 0,
                    "source": "region_skeleton_underpaint" if pass_i == 0 else "region_skeleton_texture",
                }
            )
        # 记录覆盖（粗略：笔宽范围内都算）
        cx = np.clip(pts[:, 0].astype(int) - bx0, 0, mask.shape[1] - 1)
        cy = np.clip(pts[:, 1].astype(int) - by0, 0, mask.shape[0] - 1)
        covered[cy, cx] = True

    for b in branches:
        if len(b) < 2:
            # 孤立骨架点 → 点盘补洞
            for (yy, xx) in b:
                if yy < 1 or xx < 1 or yy >= padded.shape[0] - 1 or xx >= padded.shape[1] - 1:
                    continue
                r = float(dt[yy, xx])
                if r < 1.0:
                    continue
                _emit(np.array([[xx, yy], [xx + 0.5, yy]], dtype=np.float64),
                      np.array([r * 2.0, r * 2.0]), base_only=True)
            continue
        pts_l = np.array([[xx, yy] for (yy, xx) in b], dtype=np.float64)
        widths_l = np.array([2.0 * float(dt[yy, xx]) for (yy, xx) in b], dtype=np.float64)
        _emit(pts_l, widths_l)

    # 骨架覆盖后仍有小块未覆盖（细分支被 clean 掉）：用点盘补，避免露底
    if not bool(covered.all()):
        un = mask & ~covered
        if int(un.sum()) > max(8, int(0.02 * mask.sum())):
            ys_u, xs_u = np.nonzero(un)
            # 按 6px 网格聚类，每格一个点盘
            key = (ys_u // 6) * 100000 + (xs_u // 6)
            for k in np.unique(key):
                m = key == k
                cy = int(np.clip(np.round(ys_u[m].mean()), 0, mask.shape[0] - 1))
                cx = int(np.clip(np.round(xs_u[m].mean()), 0, mask.shape[1] - 1))
                r = float(np.clip(np.sqrt(max(int(m.sum()), 1) / np.pi), 1.2, 14.0))
                _emit(np.array([[cx, cy], [cx + 0.5, cy]], dtype=np.float64),
                      np.array([r * 2.0, r * 2.0]), base_only=True)
    return strokes


def _hatch_fallback(struct: dict, rgb: np.ndarray, policy: dict) -> list[dict]:
    """极端情况兜底：平行排线（旧实现，保持覆盖）。"""
    ys = struct["ys"]
    xs = struct["xs"]
    if len(ys) == 0:
        return []
    x = xs.astype(np.float64) + 0.5
    y = ys.astype(np.float64) + 0.5
    ang = float(struct["orientation"])
    ca, sa = math.cos(ang), math.sin(ang)
    t = x * ca + y * sa
    n = -x * sa + y * ca
    area = float(struct["area_px"])
    bw = float(np.clip(math.sqrt(area) / 6.0, 2.0, 18.0)) * float(policy["width_scale"])
    spacing = max(1.2, bw * 0.4)
    n0 = float(n.min())
    nbins = int(math.ceil((float(n.max()) - n0) / spacing)) + 1
    bins = np.clip(((n - n0) / spacing).astype(np.int64), 0, nbins - 1)
    col = np.asarray(struct["color"], dtype=np.float64)
    out: list[dict] = []
    for b in range(nbins):
        m = bins == b
        if not np.any(m):
            continue
        tt = np.sort(t[m])
        if len(tt) < 2:
            continue
        n_mid = n0 + (b + 0.5) * spacing
        pts = np.array(
            [
                [tt[0] * ca - n_mid * sa, tt[0] * sa + n_mid * ca],
                [0.5 * (tt[0] + tt[-1]) * ca - n_mid * sa, 0.5 * (tt[0] + tt[-1]) * sa + n_mid * ca],
                [tt[-1] * ca - n_mid * sa, tt[-1] * sa + n_mid * ca],
            ]
        )
        out.append(
            {
                "class": struct["class"],
                "preset": policy["preset"],
                "points_px": pts,
                "widths_px": np.array([bw * 0.8, bw, bw * 0.8]),
                "color": col,
                "alpha": 1.0 if struct["class"] == "flat" else 0.9,
                "layer": 0,
                "source": "region_hatch",
            }
        )
    return out


def _line_strokes_for_struct(struct: dict, L: int, policy: dict, width_cap_px: float | None = None) -> list[dict]:
    """线条 → 沿骨架的墨线笔触（压力两端收，宽度按分解宽度）。"""
    pts = np.asarray(struct["points_world"], dtype=np.float64) * L
    widths = np.asarray(struct["widths_world"], dtype=np.float64) * L
    cols = np.asarray(struct["colors"], dtype=np.float64).reshape(-1, 3)
    if len(pts) < 2:
        return []
    # 重采样成 5 点（顺滑的一笔）
    arc = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))])
    total = float(arc[-1])
    if total < 10.0:
        return []
    n = 7
    ss = np.linspace(0.0, total, n)
    px = np.interp(ss, arc, pts[:, 0])
    py = np.interp(ss, arc, pts[:, 1])
    ww = np.interp(ss, arc, widths if len(widths) == len(pts) else np.full(len(pts), np.median(widths)))
    # 两次 3 点滑动平均：消掉骨架阶梯/桥接造成的抖动（画师一笔是圆顺的）
    for _ in range(3):
        px[1:-1] = 0.25 * px[:-2] + 0.5 * px[1:-1] + 0.25 * px[2:]
        py[1:-1] = 0.25 * py[:-2] + 0.5 * py[1:-1] + 0.25 * py[2:]
    if len(cols):
        lum = cols @ np.array([0.2126, 0.7152, 0.0722])
        col = cols[int(np.argmin(lum))].copy()
        col = 0.85 * col + 0.15 * np.array([0.08, 0.09, 0.14])
    else:
        col = np.array([0.12, 0.13, 0.18])
    # 端帽内缩：沿弧长两端各裁掉 0.4×局部半宽（避免圆头端帽越出轮廓）
    cap = 0.4 * float(np.median(ww)) * float(policy.get("width_scale", 1.0))
    if total > 3.0 * cap and cap > 0.2:
        keep = (ss >= cap) & (ss <= total - cap)
        if int(keep.sum()) >= 2:
            px, py = px[keep], py[keep]
            ss = ss[keep]
            ww = ww[keep]
            n = len(ss)
    # 压力曲线：两端收（真实运笔）
    pressure = np.interp(np.linspace(0.0, 1.0, n), [0.0, 0.25, 0.5, 0.75, 1.0], [0.55, 0.88, 1.0, 0.88, 0.55])
    widths_out = np.maximum(0.6, ww * pressure * float(policy.get("width_scale", 1.0)) * 1.12)
    alpha = 1.0
    alpha *= float(policy.get("alpha_scale", 1.0))
    strokes = [
        {
            "class": struct["class"],
            "preset": policy["preset"],
            "points_px": np.stack([px, py], axis=1),
            "widths_px": widths_out,
            "color": np.clip(col, 0.0, 1.0),
            "alpha": float(np.clip(alpha, 0.2, 1.0)),
            "layer": 1,
            "source": "line_skeleton",
        }
    ]
    return strokes


def merge_line_chains(
    lines: list[dict],
    L: float = 1024.0,
    join_px: float = 2.5,
    angle_cos: float = 0.72,
    max_chain_px: float = 220.0,
) -> list[dict]:
    """把中轴碎片按端点/方向**保守**衔接成线（画师一笔画完，但不过度串联）。

    上一版 bug：join_px=5px + 宽松角度让 474 条线几乎全串成 1 条。
    现在：衔接距离 2.5px、方向 cos≥0.72、单链长度上限 220px，避免把整幅线稿串成巨链。
    """
    if len(lines) <= 1:
        return lines
    items = []
    for ln in lines:
        pts = np.asarray(ln["points_world"], dtype=np.float64)
        if len(pts) < 2:
            continue
        w = np.asarray(ln.get("widths_world", np.full(len(pts), 0.002)), dtype=np.float64)
        if len(w) != len(pts):
            w = np.full(len(pts), float(np.median(w)) if len(w) else 0.002)
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1) * L
        items.append(
            {
                "pts": pts, "w": w,
                "class": str(ln["class"]),
                "color": np.asarray(ln.get("color", [0.1, 0.1, 0.12]), dtype=np.float64),
                "len_px": float(seg.sum()),
                "used": False,
            }
        )
    if not items:
        return []

    def _near(p: np.ndarray) -> list[int]:
        out = []
        for j2, it in enumerate(items):
            if it["used"]:
                continue
            if (np.linalg.norm((it["pts"][0] - p) * L) <= join_px
                    or np.linalg.norm((it["pts"][-1] - p) * L) <= join_px):
                out.append(j2)
        return out

    order = sorted(range(len(items)), key=lambda k: -items[k]["len_px"])
    merged: list[dict] = []
    for start in order:
        if items[start]["used"]:
            continue
        it = items[start]
        it["used"] = True
        chain_pts = [p for p in it["pts"]]
        chain_w = [w for w in it["w"]]
        chain_classes = [it["class"]]
        chain_cols = [it["color"]]
        chain_len = it["len_px"]
        for direction in ("tail", "head"):
            while chain_len < max_chain_px:
                end = chain_pts[-1] if direction == "tail" else chain_pts[0]
                if len(chain_pts) >= 3:
                    prev = chain_pts[-3] if direction == "tail" else chain_pts[2]
                else:
                    prev = end
                cur = end - prev
                nrm = float(np.linalg.norm(cur))
                if nrm < 1e-9:
                    break
                cur = cur / nrm
                best_j, best_score = None, -1e9
                for j2 in _near(end):
                    cand = items[j2]["pts"]
                    for rev in (False, True):
                        p0 = cand[-1] if rev else cand[0]
                        p1 = cand[-2] if rev else cand[1]
                        d = p1 - p0
                        dn = float(np.linalg.norm(d))
                        if dn < 1e-9:
                            continue
                        c = float(np.dot(cur, d / dn))
                        dist = float(np.linalg.norm((p0 - end) * L))
                        if c >= angle_cos and dist <= join_px:
                            score = c - 0.02 * dist
                            if score > best_score:
                                best_score, best_j = score, (j2, rev)
                if best_j is None:
                    break
                j2, rev = best_j
                add_len = items[j2]["len_px"]
                if chain_len + add_len > max_chain_px:
                    break
                items[j2]["used"] = True
                cp = items[j2]["pts"][::-1] if rev else items[j2]["pts"]
                cw = items[j2]["w"][::-1] if rev else items[j2]["w"]
                if direction == "tail":
                    chain_pts.extend(cp[1:])
                    chain_w.extend(cw[1:])
                else:
                    chain_pts = list(cp[:-1]) + chain_pts
                    chain_w = list(cw[:-1]) + chain_w
                chain_classes.append(items[j2]["class"])
                chain_cols.append(items[j2]["color"])
                chain_len += add_len
        pts = np.asarray(chain_pts, dtype=np.float64)
        wid = np.asarray(chain_w, dtype=np.float64)
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1) if len(pts) > 1 else np.zeros(1)
        klass = "line_thick" if chain_classes.count("line_thick") >= max(1, len(chain_classes) // 2) else "line_fine"
        merged.append(
            {
                "kind": "line",
                "class": klass,
                "points_world": pts,
                "widths_world": wid,
                "colors": np.tile(np.mean(chain_cols, axis=0), (len(pts), 1)),
                "alphas": np.ones(len(pts)),
                "width_px": float(np.median(wid) * L),
                "length_px": float(np.sum(seg) * L),
                "color": np.mean(chain_cols, axis=0),
                "dark": True,
                "bbox_px": (
                    int(np.clip(pts[:, 0].min() * L, 0, 1e9)),
                    int(np.clip(pts[:, 1].min() * L, 0, 1e9)),
                    int(np.clip(pts[:, 0].max() * L, 0, 1e9)) + 1,
                    int(np.clip(pts[:, 1].max() * L, 0, 1e9)) + 1,
                ),
            }
        )
    return merged


def _edge_strokes_for_structure(
    structure: dict,
    rgb: np.ndarray,
    L: int,
    step_px: float = 6.0,
    width_px: float = 2.4,
    gradient_pct: float = 75.0,
) -> list[dict]:
    """C：沿区域边界生成窄的"沿边笔"（算法内笔触生成，不是图像后处理）。

    做法：区域多边形边界 → 按 step_px 采样 → 只在图像梯度 >= p75 的边界点落笔
    （即有真实线稿/强边的地方）→ 连成窄笔（2–3px @ 源分辨率），颜色取边界处实际色
    并向暗线色偏移。这样边界由"细笔"定义，而不是靠宽笔平均色。
    """
    if not isinstance(rgb, np.ndarray) or rgb.ndim != 3:
        return []
    H = int(rgb.shape[0])
    W = int(rgb.shape[1])
    gray = rgb.mean(axis=2)
    gy, gx = np.gradient(gray)
    mag = np.hypot(gx, gy)
    thr = float(np.percentile(mag, gradient_pct))
    visited: set[tuple[int, int]] = set()
    out: list[dict] = []
    dark = np.array([0.06, 0.07, 0.10])
    for region in (structure.get("structures") or []):
        if region.get("kind") != "region":
            continue
        poly = np.asarray(region.get("poly_world", []), dtype=np.float64) * float(L)
        if len(poly) < 4:
            continue
        for i in range(len(poly)):
            a = poly[i]
            b = poly[(i + 1) % len(poly)]
            seg = float(np.linalg.norm(b - a))
            if seg <= 0.5:
                continue
            n = max(2, int(seg / step_px))
            pts = a[None, :] + np.linspace(0.0, 1.0, n, endpoint=False)[:, None] * (b - a)[None, :]
            sel = []
            for p in pts:
                xi = int(np.clip(round(float(p[0])), 0, W - 1))
                yi = int(np.clip(round(float(p[1])), 0, H - 1))
                key = (xi // 3, yi // 3)
                if key in visited:
                    continue
                if mag[yi, xi] >= thr:
                    visited.add(key)
                    sel.append((float(xi), float(yi)))
            if len(sel) < 3:
                continue
            arr = np.asarray(sel, dtype=np.float64)
            for k in range(0, len(arr), 8):
                chunk = arr[k:k + 8]
                if len(chunk) < 3:
                    continue
                mid = chunk[len(chunk) // 2].astype(int)
                col = np.asarray(rgb[int(np.clip(mid[1], 0, H - 1)), int(np.clip(mid[0], 0, W - 1))],
                                 dtype=np.float64).copy()
                lum = float(col @ np.array([0.2126, 0.7152, 0.0722]))
                if lum < 0.62:
                    col = np.clip(0.85 * col + 0.15 * dark, 0.0, 1.0)
                out.append(
                    {
                        "class": "line_fine",
                        "preset": "ink_fineliner",
                        "shape": "ink",
                        "points_px": chunk,
                        "widths_px": np.full(len(chunk), float(width_px)),
                        "color": col,
                        "alpha": 0.9,
                    }
                )
    return out


def paint_structure(
    structure: dict,
    policies: dict[str, dict],
    rgb: np.ndarray | None = None,
    width_cap_px: float | None = None,
    edge_strokes: bool = False,
) -> list[dict]:
    """按结构类别选策略，生成完整笔触列表（先铺块面、后画线/细节）。"""
    rgb = structure["rgb"] if rgb is None else rgb
    L = int(structure["L"])
    regions = [s for s in structure["structures"] if s["kind"] == "region"]
    lines = [s for s in structure["structures"] if s["kind"] == "line"]
    # 铺色顺序：大块 → 小块；细节最后
    order = {"flat": 0, "shade": 1, "detail": 2}
    regions.sort(key=lambda s: (order.get(s["class"], 2), -s["area_px"]))
    strokes: list[dict] = []
    for region in regions:
        pol = policies[region["class"]]
        strokes.extend(_fill_strokes_for_region(region, rgb, L, pol, width_cap_px=width_cap_px))
    # 线条：先把中轴碎片合并成整条线（画师一笔画完），再按粗线→细线排序
    lines = merge_line_chains(lines, L=float(structure["L"]))
    lines.sort(key=lambda s: (0 if s["class"] == "line_thick" else 1, -s["length_px"]))
    for line in lines:
        pol = policies[line["class"]]
        strokes.extend(_line_strokes_for_struct(line, L, pol, width_cap_px=width_cap_px))
    if edge_strokes:
        strokes.extend(_edge_strokes_for_structure(structure, rgb, L))
    return strokes


# ---------------------------------------------------------------- 实验评分 + UCB
def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 10.0 * math.log10(1.0 / max(mse, 1e-12))


def _ssim01(a: np.ndarray, b: np.ndarray) -> float:
    a_g = a.mean(axis=2)
    b_g = b.mean(axis=2)
    mu_a = gaussian_blur(a_g.astype(np.float32), 1.5)
    mu_b = gaussian_blur(b_g.astype(np.float32), 1.5)
    va = gaussian_blur((a_g * a_g).astype(np.float32), 1.5) - mu_a ** 2
    vb = gaussian_blur((b_g * b_g).astype(np.float32), 1.5) - mu_b ** 2
    cov = gaussian_blur((a_g * b_g).astype(np.float32), 1.5) - mu_a * mu_b
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    s = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2))
    return float(s.mean())


def run_policy_experiments(
    structure: dict,
    render_patch_fn,
    log_path: str,
    patches_per_class: int = 3,
    patch_px: int = 110,
    rng: np.random.Generator | None = None,
) -> dict:
    """按结构类取局部 patch，对每个候选策略**绘制该 patch 内该类的全部元素**并评分。

    只画单个元素会让 patch 大部分留白、PSNR 被留白主导（不同笔刷的差异淹没在噪声里）；
    这里对 patch 内同类元素整体重绘，才是"这个部位该用什么笔"的可比实验。
    """
    rng = rng or np.random.default_rng(7)
    rgb = structure["rgb"]
    H, W = rgb.shape[:2]
    exp: dict[str, dict[str, list[dict]]] = {}
    for klass in CLASSES:
        elems = [s for s in structure["structures"] if s["class"] == klass]
        if not elems:
            continue
        if klass.startswith("line"):
            elems_sorted = sorted(elems, key=lambda s: -s["length_px"])
        elif klass == "flat":
            elems_sorted = sorted(elems, key=lambda s: -s["area_px"])
        else:
            elems_sorted = sorted(elems, key=lambda s: s.get("area_px", 0))
        picks = elems_sorted[: max(1, min(patches_per_class, len(elems_sorted)))]
        exp[klass] = {}
        for pol in POLICY_CANDIDATES[klass]:
            key = f"{pol['preset']}|w{pol['width_scale']}|l{pol['length_scale']}"
            exp[klass][key] = []
            for el in picks:
                x0, y0, x1, y1 = el["bbox_px"]
                cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
                hw = patch_px // 2
                px0 = int(np.clip(cx - hw, 0, max(0, W - patch_px)))
                py0 = int(np.clip(cy - hw, 0, max(0, H - patch_px)))
                px1, py1 = px0 + patch_px, py0 + patch_px
                out = render_patch_fn(klass, pol, (px0, py0, px1, py1))
                if not out:
                    continue
                rec, mask = out
                ref = rgb[py0:py1, px0:px1]
                if rec is None or rec.shape != ref.shape or mask.shape != ref.shape[:2]:
                    continue
                if int(mask.sum()) < 40:
                    continue
                # 只在该类元素覆盖的像素上评分（否则留白/背景把差异全部淹没）
                rr = rec[mask]
                tt = ref[mask]
                exp[klass][key].append(
                    {
                        "patch": [int(px0), int(py0), int(px1), int(py1)],
                        "mask_px": int(mask.sum()),
                        "psnr": round(_psnr(rr, tt), 3),
                        "ssim": round(_ssim01(rr.reshape(-1, 1, 3), tt.reshape(-1, 1, 3)), 4),
                    }
                )
    learned: dict[str, dict] = {}
    for klass, cands in exp.items():
        stats = {}
        for key, scores in cands.items():
            if not scores:
                continue
            ps = np.asarray([s["psnr"] for s in scores], dtype=np.float64)
            ss = np.asarray([s["ssim"] for s in scores], dtype=np.float64)
            reward = float(np.clip((ps.mean() - 5.0) / 30.0, 0.0, 1.0) * 0.7 + ss.mean() * 0.3)
            stats[key] = {"mean_psnr": round(float(ps.mean()), 3), "mean_ssim": round(float(ss.mean()), 4),
                          "reward": round(reward, 4), "n": int(len(scores))}
        learned[klass] = stats
    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
    payload = {"patches_per_class": patches_per_class, "policy_candidates": POLICY_CANDIDATES,
               "results": exp, "learned": learned}
    with open(log_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    md_path = os.path.splitext(log_path)[0] + ".md"
    lines = ["# 局部结构笔刷策略实验（记录）\n",
             "每个结构类取局部 patch，对候选笔刷策略实际绘制（该 patch 内同类元素的整体重绘），"
             "计算 PSNR/SSIM；用 UCB 多臂老虎机在记录上选择该类策略"
             "（奖励 = 0.7×归一化 PSNR + 0.3×SSIM）。\n"]
    for klass, stats in learned.items():
        lines.append(f"\n## {klass}\n\n| 策略 | mean PSNR | mean SSIM | reward | n |\n|---|---|---|---|---|\n")
        for key, st in sorted(stats.items(), key=lambda kv: -kv[1]["reward"]):
            lines.append(f"| `{key}` | {st['mean_psnr']} | {st['mean_ssim']} | {st['reward']} | {st['n']} |\n")
        best = max(stats.items(), key=lambda kv: kv[1]["reward"]) if stats else None
        if best:
            lines.append(f"\n**UCB 选择**：`{best[0]}`\n")
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(lines)
    return {"experiments": exp, "learned": learned, "json": log_path, "md": md_path}


def ucb_choose(learned: dict[str, dict], klass: str, c_explore: float = 0.35) -> str:
    """UCB1：在离线记录上选该类策略（含探索项）；没有记录时回退第一个候选。"""
    stats = learned.get(klass) or {}
    if not stats:
        return POLICY_CANDIDATES[klass][0]["preset"] + "|w1|w1"  # 不应发生
    total = int(sum(v["n"] for v in stats.values()))
    if total < 1:
        total = 1
    best_key, best_val = None, -1e9
    for key, st in stats.items():
        mean = st["reward"]
        explore = c_explore * math.sqrt(math.log(max(total, 2)) / max(st["n"], 1))
        val = mean + explore
        if val > best_val:
            best_val, best_key = val, key
    return str(best_key)


def policies_from_learned(learned: dict[str, dict]) -> dict[str, dict]:
    """把 UCB 选出的 key 还原成策略 dict（用于整图绘制）。"""
    out: dict[str, dict] = {}
    for klass in CLASSES:
        key = ucb_choose(learned, klass)
        preset, w, l = key.split("|")
        out[klass] = {
            "preset": preset,
            "width_scale": float(w[1:]),
            "length_scale": float(l[1:]),
            "alpha_scale": 1.0,
            "key": key,
        }
    return out
