"""纯笔触绘画分解（painterly brush-stroke fitting）。

目标（用户定义的产品形态）
------------------------
绘画过程的**唯一表示** = 一串傅里叶曲线（每条笔画的路径）+ 一串随时间 t 定义的
画笔属性（宽度/颜色/alpha/笔形）。不允许用"区域填充/颜色场/面片"去复刻像素。

算法（沿结构走向的分层画师笔触，Hertzmann 式）
----------------------------------------------
1. **分层**由粗到细（笔刷半径 12→7→4→2.5 @512）：每层先对目标做与笔刷同尺度的模糊
   （画师看到的是"概括后的形"），在该尺度上落笔；
2. **笔触方向沿结构走向**（梯度切向）积分成弯曲笔触——画师沿边缘/头发走向运笔，
   而不是乱涂；平坦区（梯度小）随机方向；
3. 只在"当前画布与该层参考差得明显"的位置落笔（误差门限随层收紧），已画好的区域跳过；
4. 笔触有**压力曲线**（两端细、中段满宽）与软毛笔边；颜色取该层模糊参考色
   （不是逐像素抄色），alpha 1→0.95 产生叠加层次；
5. 输出逐点宽度/颜色/alpha 的曲线元素，交给 Fourier 拟合 + 画笔属性曲线 + 时间轴，
   **没有任何填充/面片元素**。
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image

from . import brushlib
from .config import FBSConfig
from .util import WarningLog, gaussian_blur

# (笔刷半径, 每层笔触上限, 是否铺底) —— @work 长边 512；其他分辨率按比例缩放。
# 第 1 层是**铺底**（underpainting）：抖动网格扫满画布，画师先定大色调与明暗；
# 之后各层只在误差明显处落笔，半径递减、逐步收拾细节。
_LAYERS = (
    (22.0, 900, True),
    (14.0, 700, False),
    (9.0, 850, False),
    (5.5, 1000, False),
    (3.0, 1150, False),
)
_PRESSURE = np.array([0.45, 0.85, 1.0, 0.85, 0.45], dtype=np.float64)
_WORK_RES = 512
_BRUSH_STROKES = 3000
# 落笔误差门限（每通道，0..1）：粗层宽松、细层收紧（画师先概括后细节）
_ERR_THRESH_BASE = 0.012


def _resize_rgb(rgb: np.ndarray, w: int, h: int) -> np.ndarray:
    img = Image.fromarray(np.clip(rgb * 255.0, 0, 255).astype(np.uint8))
    return np.asarray(img.resize((w, h), Image.LANCZOS), dtype=np.float64) / 255.0


def _stroke_mask(
    H: int, W: int,
    pts: np.ndarray,
    halfw: np.ndarray,
    soft_px: float,
) -> tuple[int, int, int, int, np.ndarray]:
    """局部 bbox 内光栅化软边笔触：沿折线取最近距离，半宽沿折线插值，边缘线性过渡。"""
    x0 = int(max(0, np.floor(pts[:, 0].min() - halfw.max() - soft_px - 2)))
    x1 = int(min(W, np.ceil(pts[:, 0].max() + halfw.max() + soft_px + 3)))
    y0 = int(max(0, np.floor(pts[:, 1].min() - halfw.max() - soft_px - 2)))
    y1 = int(min(H, np.ceil(pts[:, 1].max() + halfw.max() + soft_px + 3)))
    if x1 <= x0 or y1 <= y0:
        return x0, y0, x1, y1, np.zeros((0, 0), np.float64)
    xs = np.arange(x0, x1, dtype=np.float64)[None, :] + 0.5
    ys = np.arange(y0, y1, dtype=np.float64)[:, None] + 0.5
    n_seg = len(pts) - 1
    best_d = np.full((y1 - y0, x1 - x0), 1e9, dtype=np.float64)
    best_t = np.zeros_like(best_d)
    best_i = np.zeros_like(best_d)
    for i in range(n_seg):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        vx, vy = bx - ax, by - ay
        L2 = vx * vx + vy * vy
        if L2 <= 1e-12:
            continue
        t = ((xs - ax) * vx + (ys - ay) * vy) / L2
        t = np.clip(t, 0.0, 1.0)
        d = np.hypot(xs - (ax + t * vx), ys - (ay + t * vy))
        upd = d < best_d
        best_d = np.where(upd, d, best_d)
        best_t = np.where(upd, t, best_t)
        best_i = np.where(upd, float(i), best_i)
    hw = np.zeros_like(best_d)
    for i in range(n_seg):
        m = best_i == i
        if not np.any(m):
            continue
        hw = np.where(m, halfw[i] * (1.0 - best_t) + halfw[i + 1] * best_t, hw)
    soft = max(float(soft_px), 0.25)
    cov = np.clip((hw + soft - best_d) / (2.0 * soft), 0.0, 1.0)
    return x0, y0, x1, y1, cov


def decompose_painterly(
    rgb: np.ndarray,
    cfg: FBSConfig,
    warn: WarningLog | None = None,
    long_side: int | None = None,
) -> dict:
    """图像 → 纯笔触绘画元素（Fourier 路径 + 画笔属性），无任何填充/面片元素。"""
    arr = np.asarray(rgb, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError("decompose_painterly 需要 (H,W,3) RGB")
    src = np.clip(arr[..., :3], 0.0, 1.0)
    H, W = src.shape[:2]
    L = int(long_side or max(H, W))
    work = int(getattr(cfg, "brush_work_res", _WORK_RES) or _WORK_RES)
    work = int(np.clip(work, 128, 1536))
    scale = work / float(max(H, W))
    Ww = max(8, int(round(W * scale)))
    Hw = max(8, int(round(H * scale)))
    target = _resize_rgb(src, Ww, Hw) if (Ww, Hw) != (W, H) else src.copy()
    budget = int(getattr(cfg, "brush_strokes", _BRUSH_STROKES) or _BRUSH_STROKES)
    budget = int(np.clip(budget, 32, 20000))
    sres = Ww / 512.0

    rng = np.random.default_rng(20261018)
    canvas = np.ones((Hw, Ww, 3), dtype=np.float64)
    strokes: list[dict] = []

    # 预算按层比例分配（保证每层都有笔触）
    total_cap = sum(cap for _r, cap, _u in _LAYERS)
    layer_caps = [max(8, int(round(budget * cap / total_cap))) for _r, cap, _u in _LAYERS]
    layer_caps[-1] += budget - sum(layer_caps)

    for li, (radius0, cap0, under) in enumerate(_LAYERS):
        radius = radius0 * sres
        cap = layer_caps[li]
        layer_brushes = brushlib.presets_for_layer(li)
        sigma = max(0.55, radius * 0.5)
        ref = gaussian_blur(target.astype(np.float32), sigma).astype(np.float64)
        lum = ref @ np.array([0.2126, 0.7152, 0.0722])
        gy, gx = np.gradient(gaussian_blur(lum.astype(np.float32), max(0.6, radius * 0.25)).astype(np.float64))
        mag = np.hypot(gx, gy)
        thr = _ERR_THRESH_BASE * (0.55 + 0.45 * (radius0 / 22.0))
        soft_px = max(0.7, radius * 0.18)
        if under:
            # 铺底：抖动网格扫满画布（保证无白色空洞），笔触长且宽
            step = max(2.0, radius * 1.05)
        else:
            step = max(1.5, radius * 1.15)
        ys = np.arange(radius * 0.6, Hw - radius * 0.6, step)
        xs = np.arange(radius * 0.6, Ww - radius * 0.6, step)
        if len(xs) == 0 or len(ys) == 0:
            continue
        cand = [(float(x + rng.uniform(-0.5, 0.5) * step), float(y + rng.uniform(-0.5, 0.5) * step))
                for y in ys for x in xs]
        rng.shuffle(cand)
        painted = 0
        for (cx, cy) in cand:
            if painted >= cap:
                break
            iy = int(np.clip(round(cy), 0, Hw - 1))
            ix = int(np.clip(round(cx), 0, Ww - 1))
            if not under and float(np.abs(canvas[iy, ix] - ref[iy, ix]).sum()) / 3.0 < thr:
                continue
            # 选择笔刷预设（现代软件笔刷库里该层允许的笔刷；越靠前权重越高）
            first_p = 0.45 if li < 2 else 0.62
            if len(layer_brushes) == 1 or rng.random() < first_p:
                bid = layer_brushes[0]
            else:
                bid = layer_brushes[int(rng.integers(1, len(layer_brushes)))]
            preset = brushlib.BRUSHES[bid]
            # 运笔长度：按预设 length_mult 缩放
            seg_len = max(1.0, radius * 0.85 * float(preset["length_mult"]))
            # 沿结构走向积分出弯曲笔触（梯度切向；平坦区随机）
            n_nodes = 5
            path = []
            px, py = float(cx), float(cy)
            ang = None
            for k in range(n_nodes):
                iy = int(np.clip(round(py), 0, Hw - 1))
                ix = int(np.clip(round(px), 0, Ww - 1))
                gmag = float(mag[iy, ix])
                if gmag > 0.004:
                    a = math.atan2(float(gy[iy, ix]), float(gx[iy, ix])) + math.pi * 0.5
                    if ang is not None and math.cos(a - ang) < 0.0:
                        a += math.pi
                    ang = a
                elif ang is None:
                    ang = rng.uniform(0.0, 2.0 * math.pi)
                path.append((px, py))
                px += math.cos(ang) * seg_len
                py += math.sin(ang) * seg_len
                px = float(np.clip(px, 1.0, Ww - 2.0))
                py = float(np.clip(py, 1.0, Hw - 2.0))
            pts = np.asarray(path, dtype=np.float64)
            if np.hypot(*(pts[-1] - pts[0])) < 1e-6:
                continue
            # 压力曲线（预设的 pressure 控制锥度强弱）+ 尺寸动态
            pressure = float(np.clip(float(preset["pressure"]), 0.0, 1.0))
            profile = 1.0 - pressure * (1.0 - _PRESSURE)
            wj = rng.uniform(1.0 - float(preset["width_jitter"]), 1.0 + float(preset["width_jitter"]))
            halfw = radius * float(preset["size_mult"]) * profile * wj
            # 颜色取笔触中点处的**该层模糊参考色**（概括色，不是逐像素抄色）+ 颜色动态
            mid = pts[len(pts) // 2]
            miy, mix = int(np.clip(round(mid[1]), 0, Hw - 1)), int(np.clip(round(mid[0]), 0, Ww - 1))
            col = ref[miy, mix].copy()
            cj = float(preset["color_jitter"])
            if cj > 0.0:
                col = np.clip(col + rng.normal(0.0, cj, 3), 0.0, 1.0)
            alpha = float(np.clip(rng.uniform(*preset["alpha"]), 0.05, 1.0))
            # 覆盖力下限：半透明/纹理笔刷在固定笔数预算下必须叠得出来，否则画面发虚
            floor = 0.90 if li == 0 else (0.82 if li < 3 else 0.86)
            alpha = max(alpha, floor)
            x0, y0, x1, y1, cov = _stroke_mask(Hw, Ww, pts, halfw, soft_px)
            if cov.size == 0:
                continue
            a = (alpha * cov)[..., None]
            canvas[y0:y1, x0:x1] = canvas[y0:y1, x0:x1] * (1.0 - a) + col[None, None, :] * a
            Lw = float(max(Ww, Hw))
            pts_w = np.stack([(pts[:, 0] + 0.5) / Lw, (pts[:, 1] + 0.5) / Lw], axis=1)
            strokes.append(
                {
                    "points": pts_w,
                    "widths": (halfw * 2.0) / Lw,
                    "color": np.clip(col, 0.0, 1.0),
                    "alpha": alpha,
                    "soft_px": float(soft_px),
                    "phase": li,
                    "shape": str(preset["shape"]),
                    "preset": bid,
                }
            )
            painted += 1
        if warn is not None:
            warn.add("W_PAINTERLY_LAYER", f"第 {li + 1} 层（半径 {radius0:.1f}px）落笔 {painted} 支", severity="info")

    if not strokes:
        raise ValueError("painterly 分解没有产生任何笔触")

    elements: list[dict] = []
    for si, st in enumerate(strokes):
        n = len(st["points"])
        elements.append(
            {
                "points": st["points"],
                "s": np.linspace(0.0, 1.0, n),
                "closed": False,
                "fill_role": "medial_branch",
                "widths": st["widths"].astype(np.float64),
                "colors": np.tile(st["color"].astype(np.float32), (n, 1)),
                "alphas": np.full(n, float(st["alpha"]), dtype=np.float64),
                "shapes": np.array([str(st["shape"])] * n, dtype=object),
                "fill_poly": None,
                "fill_holes": None,
                "region_id": -1,
                "layer": 1,
                "meta": {
                    "max_halfwidth_px": float(st["widths"].max()),
                    "sprawl": None,
                    "dt_peak_px": None,
                    "source": "painterly_stroke",
                    "phase": int(st["phase"]),
                    "order": int(si),
                    "preset": str(st["preset"]),
                    "shape": str(st["shape"]),
                },
            }
        )

    mse = float(np.mean((canvas - target) ** 2))
    psnr = 10.0 * math.log10(1.0 / max(mse, 1e-12))
    stats = {
        "method": "painterly_brush_strokes",
        "strokes": len(elements),
        "layers": [int(c) for c in layer_caps],
        "work_res": [Ww, Hw],
        "canvas_psnr_db": round(float(psnr), 3),
        "canvas_rmse": round(float(math.sqrt(mse)), 5),
    }
    if warn is not None:
        warn.add(
            "W_PAINTERLY_ACTIVE",
            f"纯笔触绘画：{len(elements)} 笔（工作分辨率 {Ww}x{Hw}，自检 PSNR {psnr:.2f}dB）",
            severity="info",
        )
    return {
        "method": "painterly_brush_strokes",
        "elements": elements,
        "regions": [],
        "stats": stats,
        "accelerators": {},
        "debug_canvas": canvas,
    }
