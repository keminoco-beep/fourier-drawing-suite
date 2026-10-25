"""生成 T1–T13 合成测试集（设计文档 v2.1 §7.4），确定性（固定 seed，无时间戳）。

为什么必须存在（references/engineering_guardrails.md §8 第 5 条）
-------------------------------------------------------------
「能用 256×256 合成图复现的，绝不在 200 万像素真图上摸索」。
合成图的每一像素都由已知几何 + 已知宽度/颜色曲线生成，因此**可以反演验真**：
- T4/T11：已知 w(t) → 验 A7/A13/A14（宽度反演 / 三种 taper 表达一致）；
- T12：已知颜色 stops → 验颜色渐变反演；
- T13：已知 step 区间 → 验 A15 区间语义；
- T5：已知尖角多边形 → 验 A8（stamp vs line 形状误差）；
- T7：合成高熵图 → 验照片拒绝（D5）；
- T8/T9/T10：边界与异常输入 → 验 A5 零崩溃且原因明确。

绘制口径（与 MAT 重建语义对齐）
----------------------------
笔画用「沿弧长采样的圆盘并集 / 变宽条带」绘制：逐段用契约的精确带覆盖公式
``coverage = min(0.5, d+r) − max(−0.5, d−r)``（d=到中轴距离，r=半宽）在局部包围盒累加，
相当于 ``∪ disc(c(t), r(t))`` 的亚像素近似；填充多边形用 4× 超采样求覆盖。
宽度 truth 存的是**全宽（直径）**、``t`` 为**弧长归一化**参数（与 .fbs 语义一致）。

每个用例附 ``<case>_spec.json``（真值 + 预期处理），并汇总到 ``manifest.json``
（含每文件 sha256，``--check`` 可验证确定性与陈旧性）。

用法：
    python tests/gen_fixtures.py            # 生成（幂等重建全部用例）
    python tests/gen_fixtures.py --check    # 只校验 manifest 的 sha256，不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from typing import Any, Callable, Sequence

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "assets", "testcases")

SCHEMA = "fbs-fixture/2"
MANIFEST = "manifest.json"
SEED = 20261018
# 合成图尺寸控制在 256–512：足够覆盖形状类别，又不拖慢验收。
DEFAULT_SIZE = 384


# ===================================================================== 画布
class Canvas:
    """浮点 RGB 画布：笔画以圆盘并集（Minkowski 和）亚像素覆盖绘制，填充多边形 4× 超采样。"""

    def __init__(self, width: int, height: int, bg: Sequence[float] = (255, 255, 255)) -> None:
        self.w = int(width)
        self.h = int(height)
        self.rgb = np.empty((self.h, self.w, 3), dtype=np.float64)
        self.rgb[:] = np.asarray(bg, dtype=np.float64) / 255.0
        self._cov = np.zeros((self.h, self.w), dtype=np.float64)
        self._colsum = np.zeros((self.h, self.w, 3), dtype=np.float64)
        self._wsum = np.zeros((self.h, self.w), dtype=np.float64)

    # ---------------------------------------------------------------- 内部
    def _composite(self) -> None:
        cov = np.clip(self._cov, 0.0, 1.0)
        safe = np.maximum(self._wsum, 1e-12)
        color = self._colsum / safe[:, :, None]
        m = cov[:, :, None]
        self.rgb = np.clip(self.rgb * (1.0 - m) + color * m, 0.0, 1.0)
        self._cov = np.zeros((self.h, self.w), dtype=np.float64)
        self._colsum = np.zeros((self.h, self.w, 3), dtype=np.float64)
        self._wsum = np.zeros((self.h, self.w), dtype=np.float64)

    @staticmethod
    def _resample_path(path: np.ndarray, spacing: float) -> tuple[np.ndarray, np.ndarray, float]:
        """按弧长等距重采样折线；返回 (points, t 弧长归一化 0..1, 总弧长)。"""
        pts = np.asarray(path, dtype=np.float64).reshape(-1, 2)
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = float(cum[-1])
        if total <= 0:
            return pts[:1], np.zeros(1), 0.0
        n = int(max(2, math.ceil(total / max(spacing, 1e-6)) + 1))
        targets = np.linspace(0.0, total, n)
        xs = np.interp(targets, cum, pts[:, 0])
        ys = np.interp(targets, cum, pts[:, 1])
        return np.stack([xs, ys], axis=1), targets / total, total

    def _add_band(self, p0: np.ndarray, p1: np.ndarray, r: float, color: np.ndarray) -> None:
        """一段等宽条带的亚像素精确覆盖（圆盘并集的离散近似）。"""
        if r <= 1e-6:
            return  # 宽度 0 = 抬笔，不落墨（契约 §5）
        x0 = int(math.floor(min(p0[0], p1[0]) - r - 1))
        x1 = int(math.ceil(max(p0[0], p1[0]) + r + 1))
        y0 = int(math.floor(min(p0[1], p1[1]) - r - 1))
        y1 = int(math.ceil(max(p0[1], p1[1]) + r + 1))
        x0, x1 = max(0, x0), min(self.w, x1)
        y0, y1 = max(0, y0), min(self.h, y1)
        if x0 >= x1 or y0 >= y1:
            return
        ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float64)
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        l2 = dx * dx + dy * dy
        if l2 <= 1e-12:
            d = np.hypot(xs - p0[0], ys - p0[1])
        else:
            u = np.clip(((xs - p0[0]) * dx + (ys - p0[1]) * dy) / l2, 0.0, 1.0)
            d = np.hypot(xs - (p0[0] + u * dx), ys - (p0[1] + u * dy))
        cov = np.clip(np.minimum(0.5, d + r) - np.maximum(-0.5, d - r), 0.0, 1.0)
        self._cov[y0:y1, x0:x1] = np.maximum(self._cov[y0:y1, x0:x1], cov)
        self._colsum[y0:y1, x0:x1] += cov[:, :, None] * color[None, None, :]
        self._wsum[y0:y1, x0:x1] += cov

    # ---------------------------------------------------------------- 公开
    def add_stroke(
        self,
        path: np.ndarray,
        width_fn: Callable[[np.ndarray], np.ndarray],
        color_fn: Callable[[np.ndarray], np.ndarray],
        *,
        spacing_px: float = 0.5,
    ) -> dict[str, Any]:
        """画变宽笔画。width_fn/color_fn 的参数是**弧长归一化 t**（0..1）。"""
        pts, ts, total = self._resample_path(path, spacing_px)
        for i in range(len(pts) - 1):
            tm = 0.5 * (ts[i] + ts[i + 1])
            r = float(0.5 * float(np.asarray(width_fn(np.array([tm]))).reshape(-1)[0]))  # 全宽→半径
            col = np.asarray(color_fn(np.array([tm])), dtype=np.float64).reshape(-1, 3)[0] / 255.0
            self._add_band(pts[i], pts[i + 1], r, col)
        self._composite()
        return {"arc_length_px": total, "width_keys": None}

    def add_polygon(self, points: Sequence[Sequence[float]], color: Sequence[float],
                    alpha: float = 1.0, supersample: int = 4) -> None:
        """4× 超采样多边形覆盖（直角/尖角保持锐利，不做形态学圆化）。"""
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        x0 = max(0, int(math.floor(pts[:, 0].min())) - 1)
        x1 = min(self.w, int(math.ceil(pts[:, 0].max())) + 1)
        y0 = max(0, int(math.floor(pts[:, 1].min())) - 1)
        y1 = min(self.h, int(math.ceil(pts[:, 1].max())) + 1)
        if x0 >= x1 or y0 >= y1:
            return
        bw, bh = x1 - x0, y1 - y0
        ss = int(supersample)
        mask = Image.new("L", (bw * ss, bh * ss), 0)
        draw = ImageDraw.Draw(mask)
        local = [(float((p[0] - x0) * ss), float((p[1] - y0) * ss)) for p in pts]
        draw.polygon(local, fill=255)
        small = mask.resize((bw, bh), Image.BOX)
        cov = np.asarray(small, dtype=np.float64) / 255.0 * float(np.clip(alpha, 0.0, 1.0))
        col = np.asarray(color, dtype=np.float64) / 255.0
        region = self.rgb[y0:y1, x0:x1]
        m = cov[:, :, None]
        self.rgb[y0:y1, x0:x1] = np.clip(region * (1.0 - m) + col[None, None, :] * m, 0.0, 1.0)

    def add_ellipse(self, cx: float, cy: float, rx: float, ry: float,
                    color: Sequence[float], segments: int = 256) -> None:
        a = np.linspace(0.0, 2.0 * math.pi, segments, endpoint=False)
        pts = np.stack([cx + rx * np.cos(a), cy + ry * np.sin(a)], axis=1)
        self.add_polygon(pts, color)

    def add_rect(self, x0: float, y0: float, x1: float, y1: float, color: Sequence[float],
                 alpha: float = 1.0) -> None:
        self.add_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], color, alpha=alpha)

    def to_array(self) -> np.ndarray:
        return np.clip(np.round(self.rgb * 255.0), 0, 255).astype(np.uint8)


# ===================================================================== 路径
def bezier(p0, p1, p2, p3, n: int = 240) -> np.ndarray:
    """三次贝塞尔采样折线（n 只影响折线光滑度，重采样在 add_stroke 内按弧长做）。"""
    t = np.linspace(0.0, 1.0, int(n))[:, None]
    p0, p1, p2, p3 = (np.asarray(p, dtype=np.float64) for p in (p0, p1, p2, p3))
    return (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3


def centerline_px(path: np.ndarray, spacing: float = 1.0) -> list[list[float]]:
    """按弧长等距重采样折线，返回 px 坐标点列（供验收把恢复的笔画投影回真值参数 t）。"""
    pts = np.asarray(path, dtype=np.float64).reshape(-1, 2)
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    n = int(max(2, math.ceil(total / max(spacing, 1e-6)) + 1))
    targets = np.linspace(0.0, total, n)
    xs = np.interp(targets, cum, pts[:, 0])
    ys = np.interp(targets, cum, pts[:, 1])
    return [[round(float(x), 2), round(float(y), 2)] for x, y in zip(xs, ys)]


def key_curve(keys: Sequence[Sequence[float]]) -> Callable[[np.ndarray], np.ndarray]:
    """线性关键点曲线 t→v（truth 用；与 .fbs linear interp 同语义）。"""
    ts = np.array([k[0] for k in keys], dtype=np.float64)
    vs = np.array([k[1] for k in keys], dtype=np.float64)

    def fn(t: np.ndarray) -> np.ndarray:
        return np.interp(np.asarray(t, dtype=np.float64), ts, vs)

    return fn


def step_curve(keys: Sequence[Sequence[float]]) -> Callable[[np.ndarray], np.ndarray]:
    """step（左闭右开区间保持）曲线 t→v。"""
    ts = np.array([k[0] for k in keys], dtype=np.float64)
    vs = np.array([k[1] for k in keys], dtype=np.float64)

    def fn(t: np.ndarray) -> np.ndarray:
        tt = np.asarray(t, dtype=np.float64)
        idx = np.clip(np.searchsorted(ts, tt, side="right") - 1, 0, len(vs) - 1)
        return vs[idx]

    return fn


def const_color(rgb: Sequence[float]) -> Callable[[np.ndarray], np.ndarray]:
    c = np.asarray(rgb, dtype=np.float64)

    def fn(t: np.ndarray) -> np.ndarray:
        return np.repeat(c[None, :], len(np.atleast_1d(t)), axis=0) if np.ndim(t) else c

    return fn


def color_stops_fn(stops: Sequence[dict]) -> Callable[[np.ndarray], np.ndarray]:
    """颜色 stops  linearly 插值 truth（rgb 0..255）。"""
    ts = np.array([s["t"] for s in stops], dtype=np.float64)
    rgbs = np.array([s["rgb"] for s in stops], dtype=np.float64)

    def fn(t: np.ndarray) -> np.ndarray:
        tt = np.atleast_1d(np.asarray(t, dtype=np.float64))
        return np.stack([np.interp(tt, ts, rgbs[:, c]) for c in range(3)], axis=1)

    return fn


# ===================================================================== 落盘工具
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save_rgb(arr: np.ndarray, name: str) -> str:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    Image.fromarray(np.ascontiguousarray(arr.astype(np.uint8)), mode="RGB").save(path)
    return path


def save_rgba(rgba: np.ndarray, name: str) -> str:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    Image.fromarray(np.ascontiguousarray(rgba.astype(np.uint8)), mode="RGBA").save(path)
    return path


def write_spec(case: str, file: str, spec: dict[str, Any]) -> str:
    data = {"schema": SCHEMA, "case": case, "file": file}
    data.update(spec)
    path = os.path.join(OUT, f"{case}_spec.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    return path


def write_bytes(data: bytes, name: str) -> str:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# ===================================================================== 用例定义

# ---------------------------------------------------------------- T1 签名线稿
def gen_t1() -> list[str]:
    size = DEFAULT_SIZE
    keys = [[0.0, 3.5], [0.3, 6.5], [0.65, 4.0], [1.0, 7.0]]
    path = bezier((50, 320), (150, 60), (250, 360), (340, 80))
    c = Canvas(size, size)
    info = c.add_stroke(path, key_curve(keys), const_color((20, 20, 26)))
    f = "T1_signature.png"
    save_rgb(c.to_array(), f)
    spec = {
        "purpose": "A1/A2：线稿类 PSNR≥30dB、SSIM≥0.90 的最佳场景",
        "class_expected": "lineart",
        "size": [size, size],
        "stroke": {
            "width_px": {"mode": "linear_keys", "keys": keys,
                         "note": "全宽（直径，像素）；t=弧长归一化"},
            "color_rgb": [20, 20, 26],
            "arc_length_px": round(info["arc_length_px"], 2),
        },
        "checks": {"A1": "psnr>=30@R1024", "A2": "ssim>=0.90@R1024"},
    }
    write_spec("T1_signature", f, spec)
    return [f]


# ---------------------------------------------------------------- T2 图标
def gen_t2() -> list[str]:
    size = DEFAULT_SIZE
    c = Canvas(size, size, bg=(250, 250, 250))
    palette = [[30, 60, 120], [200, 60, 50], [30, 30, 30], [40, 140, 90]]
    c.add_rect(30, 30, 150, 150, palette[0])                    # 直角矩形
    c.add_polygon([(210, 30), (330, 30), (270, 150)], palette[1])  # 尖角三角形
    c.add_rect(30, 210, 150, 330, palette[2])
    c.add_ellipse(270, 270, 60, 60, palette[3])                 # 曲线圆形
    path = bezier((40, 185), (140, 160), (240, 215), (344, 190), n=160)
    c.add_stroke(path, key_curve([[0.0, 3.0], [1.0, 3.0]]), const_color((90, 90, 90)))
    f = "T2_icon.png"
    save_rgb(c.to_array(), f)
    spec = {
        "purpose": "T2：图标（直角+曲线）—— 尖角分支保留、区域填充；A8 尖角形状误差的载体之一",
        "class_expected": "flat_art",
        "size": [size, size],
        "palette_rgb": palette,
        "shapes": [
            {"kind": "rect", "box": [30, 30, 150, 150], "color": palette[0], "corners": [[30, 30], [150, 30], [150, 150], [30, 150]]},
            {"kind": "triangle", "points": [[210, 30], [330, 30], [270, 150]], "color": palette[1]},
            {"kind": "rect", "box": [30, 210, 150, 330], "color": palette[2]},
            {"kind": "circle", "center": [270, 270], "radius": 60, "color": palette[3]},
        ],
        "checks": {"T2": "build 成功 + fill_count>0", "A5": "无崩溃"},
    }
    write_spec("T2_icon", f, spec)
    return [f]


# ---------------------------------------------------------------- T3 扁平插画
def gen_t3() -> list[str]:
    size = DEFAULT_SIZE
    c = Canvas(size, size, bg=(245, 240, 230))
    palette = [
        [220, 80, 70], [240, 170, 60], [90, 170, 90], [70, 130, 200],
        [150, 90, 190], [60, 60, 70], [235, 120, 160], [120, 200, 200],
    ]
    boxes = [
        (24, 24, 136, 136), (150, 24, 256, 112), (272, 24, 360, 150),
        (24, 150, 112, 256), (128, 128, 240, 240), (256, 166, 360, 256),
        (24, 272, 150, 360), (166, 256, 360, 360),
    ]
    for (box, col) in zip(boxes, palette):
        c.add_rect(*box, col)
    f = "T3_flat_illustration.png"
    save_rgb(c.to_array(), f)
    spec = {
        "purpose": "A9/A11/A10：扁平插画（8 色块）多区域中轴 + 分层顺序 + 体积比",
        "class_expected": "illustration",
        "size": [size, size],
        "palette_rgb": palette,
        "regions_expected": len(palette),
        "region_boxes": [[int(v) for v in box] for box in boxes],
        "checks": {"A9": "psnr>=26@R1024", "A11": "fbs/png<=3x（记录 R+绝对值）",
                   "R7": "fill 层 < 分支层；同层 footprint 降序", "R4": "fill_count>0"},
    }
    write_spec("T3_flat_illustration", f, spec)
    return [f]


# ---------------------------------------------------------------- T4 锥形笔画
def gen_t4() -> list[str]:
    size = DEFAULT_SIZE
    keys = [[0.0, 22.0], [1.0, 2.5]]
    path = bezier((60, 300), (150, 140), (250, 360), (330, 180))
    c = Canvas(size, size)
    info = c.add_stroke(path, key_curve(keys), const_color((25, 25, 30)))
    f = "T4_taper_thick_to_thin.png"
    save_rgb(c.to_array(), f)
    spec = {
        "purpose": "A7/A13（宽度反演精度）、A3（N–R 契约）、A10（分支数）：已知 w(t) 的锥形笔画",
        "class_expected": "lineart",
        "size": [size, size],
        "stroke": {
            "width_px": {"mode": "linear_keys", "keys": keys,
                         "note": "全宽（直径，像素）；t=弧长归一化；22→2.5px"},
            "color_rgb": [25, 25, 30],
            "arc_length_px": round(info["arc_length_px"], 2),
            "centerline_px": centerline_px(path),
        },
        "truth": {"w_t0_px": 22.0, "w_t1_px": 2.5, "width_profile": "linear"},
        "checks": {
            "A7": "宽度反演中位相对误差<5%",
            "A13": "沿 t 取 32 点，最大宽度相对误差<5%",
            "A3": "max_err*R<=eps 且 max_err*R>=0.5eps 且 N 随 R 单调增",
            "A10": "中轴分支数 <= 1.5×预期(1)",
        },
    }
    write_spec("T4_taper_thick_to_thin", f, spec)
    return [f]


# ---------------------------------------------------------------- T5 尖角
def gen_t5() -> list[str]:
    size = DEFAULT_SIZE
    c = Canvas(size, size)
    cx, cy, R, r = 192.0, 200.0, 150.0, 58.0
    pts = []
    n = 5
    for i in range(2 * n):
        rad = R if i % 2 == 0 else r
        a = -math.pi / 2 + i * math.pi / n
        pts.append([cx + rad * math.cos(a), cy + rad * math.sin(a)])
    c.add_polygon(pts, (35, 35, 45))
    # L 形直角（第二个尖角载体）
    c.add_polygon([[40, 240], [70, 240], [70, 350], [40, 350]], (60, 90, 160))
    f = "T5_sharp_corners.png"
    save_rgb(c.to_array(), f)
    spec = {
        "purpose": "A8：stamp 渲染 vs 变宽线段渲染的形状误差（尖角必须保持）；尖角多边形真值",
        "class_expected": "flat_art",
        "size": [size, size],
        "shapes": [{"kind": "star", "points": [[round(x, 2), round(y, 2)] for x, y in pts],
                    "center": [cx, cy], "outer_r": R, "inner_r": r}],
        "checks": {"A8": "shape_err(stamp) <= 0.5 * shape_err(line)"},
    }
    write_spec("T5_sharp_corners", f, spec)
    return [f]


# ---------------------------------------------------------------- T5b 尖角折返笔画（A8 修正判据载体）
def gen_t5b() -> list[str]:
    """带尖锐折返的**变宽笔画**（不是区域填充）：stamp / sweep / line 三种渲染基元的

    A8（修正后）要求在同一个 doc 上比较两种渲染基元与真实参考的形状误差 —— 载体必须
    是 medial_branch（region_fill 的渲染与 mode 无关，测不出差异）。这里画一个 V 形 +
    Z 形折返的粗笔画（尖角 60°），用圆盘并集语义的真值绘制。
    """
    size = DEFAULT_SIZE
    width = 26.0
    path = np.array(
        [
            [60.0, 80.0], [150.0, 300.0], [240.0, 80.0],  # V 形（两处尖角）
            [300.0, 80.0], [300.0, 300.0], [355.0, 300.0],  # Z 形折返
        ],
        dtype=np.float64,
    )
    # 用折线插值保持尖角：逐段直线
    seg_pts = []
    for i in range(len(path) - 1):
        seg_pts.append(np.linspace(path[i], path[i + 1], 80, endpoint=(i == len(path) - 2)))
    polyline = np.concatenate(seg_pts, axis=0)
    c = Canvas(size, size)
    info = c.add_stroke(polyline, key_curve([[0.0, width], [1.0, width]]), const_color((30, 30, 38)))
    f = "T5b_sharp_fold_stroke.png"
    save_rgb(c.to_array(), f)
    write_spec("T5b_sharp_fold_stroke", f, {
        "purpose": "A8（修正判据）载体：带尖锐折返的变宽笔画真值 → 同一 doc 比较 stamp vs line 的"
                   "形状误差等价性（差比∈[0.85,1.15] 且各自绝对边界偏差≤1px@R1024）",
        "class_expected": "lineart",
        "size": [size, size],
        "stroke": {
            "width_px": {"mode": "constant", "value": width,
                         "note": "全宽（直径，像素）恒定；t=弧长归一化"},
            "color_rgb": [30, 30, 38],
            "centerline_px": [[round(float(x), 2), round(float(y), 2)] for x, y in path],
            "corners_px": [[round(float(x), 2), round(float(y), 2)] for x, y in path[1:4]],
            "sharp_angle_deg": 60.0,
            "arc_length_px": round(info["arc_length_px"], 2),
        },
        "checks": {"A8": "stamp/line 形状误差等价（差比∈[0.85,1.15]）且各自边界偏差 p99≤1.0px"},
    })
    return [f]


# ---------------------------------------------------------------- T6 渐变
def gen_t6() -> list[str]:
    size = 256
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    u = xx / (size - 1)
    v = yy / (size - 1)
    r = 40 + 180 * u
    g = 60 + 150 * (0.5 * u + 0.5 * v)
    b = 80 + 120 * (0.5 + 0.5 * np.sin(2.0 * math.pi * (0.7 * u + 0.3 * v)))
    rgb = np.clip(np.stack([r, g, b], axis=2), 0, 255).astype(np.uint8)
    f = "T6_gradient.png"
    save_rgb(rgb, f)
    spec = {
        "purpose": "T6：渐变图（无硬边 → 中轴退化）—— 必须明确降级/告警而非崩溃或静默",
        "class_expected": "gradient",
        "size": [size, size],
        "gradient": {"u_axis": "x/(W-1)", "v_axis": "y/(H-1)",
                     "note": "平滑色场，无硬边界；期望 W_COLOR_FIELD_OFF / W_SOLID_COLOR 等降级告警之一"},
        "checks": {"T6": "不崩溃；有降级 warning 或明确分类；metrics 四维照常输出"},
    }
    write_spec("T6_gradient", f, spec)
    return [f]


# ---------------------------------------------------------------- T7 伪照片
def gen_t7() -> list[str]:
    size = DEFAULT_SIZE
    rng = np.random.default_rng(7)

    def upsample(low: np.ndarray, w: int, h: int) -> np.ndarray:
        chans = []
        for c in range(low.shape[2]):
            im = Image.fromarray(np.clip(low[:, :, c] * 255, 0, 255).astype(np.uint8))
            chans.append(np.asarray(im.resize((w, h), Image.BICUBIC), dtype=np.float64) / 255.0)
        return np.stack(chans, axis=2)

    # 多倍频自然纹理：低频色块 + 中频结构 + 高频颗粒 + 暗角 + 噪声
    low = rng.random((size // 48 + 2, size // 48 + 2, 3))
    mid = rng.random((size // 12 + 2, size // 12 + 2, 3))
    base = 0.55 * upsample(low, size, size) + 0.45 * upsample(mid, size, size)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    vign = 1.0 - 0.45 * (((xx - size / 2) / (size / 2)) ** 2 + ((yy - size / 2) / (size / 2)) ** 2)
    img = base * np.clip(vign, 0.2, 1.0)[:, :, None]
    img = img + rng.normal(0, 0.035, (size, size, 3))
    img = np.clip(img, 0, 1)
    # 压色度、保留明度纹理：更像真实照片（低饱和、连续色调），而不是纯彩色噪声
    luma = img @ np.array([0.299, 0.587, 0.114])
    img = np.clip(0.74 * luma[:, :, None] + 0.26 * img, 0, 1)
    # 轻微暖偏色
    img[..., 0] = np.clip(img[..., 0] * 1.04 + 0.015, 0, 1)
    img[..., 2] = np.clip(img[..., 2] * 0.97, 0, 1)
    rgb = np.round(img * 255).astype(np.uint8)

    flat = rgb.reshape(-1, 3)
    uniq = len(np.unique(flat, axis=0))
    hist, _ = np.histogram(flat.mean(axis=1), bins=256, range=(0, 255))
    p = hist[hist > 0].astype(np.float64)
    p = p / p.sum()
    entropy = float(-(p * np.log2(p)).sum())
    f = "T7_photo_like.png"
    save_rgb(rgb, f)
    spec = {
        "purpose": "A5/D5：合成高熵照片类输入 —— 必须明确拒绝（E_PHOTO_DEFERRED），错误信息可读且含实测依据",
        "class_expected": "photo",
        "expected_error": "E_PHOTO_DEFERRED",
        "size": [size, size],
        "photo_evidence": {
            "unique_colors": int(uniq),
            "entropy_bits": round(entropy, 3),
            "noise_sigma_8bit": round(0.035 * 255, 2),
            "note": "低频色块+多倍频+暗角+颗粒，确定性 seed=7",
        },
        "checks": {"A5": "拒绝码=E_PHOTO_DEFERRED，附证据与替代建议"},
    }
    write_spec("T7_photo_like", f, spec)
    return [f]


# ---------------------------------------------------------------- T8 边界
def gen_t8() -> list[str]:
    size = 256
    files = []
    black = np.zeros((size, size, 3), np.uint8)
    files.append(save_rgb(black, "T8a_solid_black.png"))
    write_spec("T8a_solid_black", "T8a_solid_black.png", {
        "purpose": "T8/A5：纯黑图（纯色/无中轴）—— 必须走专门路径 + W_SOLID_COLOR 告警，不崩溃",
        "class_expected": "solid", "size": [size, size],
        "expected": {"handling": "warn_or_build", "warning_codes": ["W_SOLID_COLOR"],
                     "must_not": "crash_or_silent_empty"},
        "checks": {"A5": "不崩溃且给出 W_SOLID_COLOR 或明确拒绝"},
    })
    white = np.full((size, size, 3), 255, np.uint8)
    files.append(save_rgb(white, "T8b_solid_white.png"))
    write_spec("T8b_solid_white", "T8b_solid_white.png", {
        "purpose": "T8/A5：纯白图（无墨迹）—— 必须输出空笔画 + W_SOLID_COLOR 告警，不崩溃",
        "class_expected": "solid", "size": [size, size],
        "expected": {"handling": "warn_or_build", "warning_codes": ["W_SOLID_COLOR"],
                     "strokes": 0},
        "checks": {"A5": "不崩溃且给出 W_SOLID_COLOR/空笔画"},
    })
    transparent = np.zeros((size, size, 4), np.uint8)
    files.append(save_rgba(transparent, "T8c_fully_transparent.png"))
    write_spec("T8c_fully_transparent", "T8c_fully_transparent.png", {
        "purpose": "T8/A5：全透明图 —— 必须拒绝 E_INPUT_TRANSPARENT（图像无可见内容）",
        "class_expected": "empty", "size": [size, size],
        "expected_error": "E_INPUT_TRANSPARENT",
        "checks": {"A5": "拒绝码=E_INPUT_TRANSPARENT"},
    })
    return files


# ---------------------------------------------------------------- T9 模式转换
def gen_t9() -> list[str]:
    base = np.asarray(Image.open(os.path.join(OUT, "T2_icon.png")).convert("RGB"))
    base = np.asarray(Image.fromarray(base).resize((256, 256), Image.LANCZOS))
    files: list[str] = []

    gray = np.asarray(Image.fromarray(base).convert("L"), dtype=np.uint16)
    gray16 = (gray * 257).astype("<u2")
    p = os.path.join(OUT, "T9a_16bit.png")
    im16 = Image.frombytes("I;16", (gray16.shape[1], gray16.shape[0]), gray16.tobytes())
    im16.save(p)
    files.append("T9a_16bit.png")
    write_spec("T9a_16bit", "T9a_16bit.png", {
        "purpose": "T9/A5：16bit 灰度 PNG —— 自动转换为 8bit，不崩溃",
        "class_expected": "any", "size": [256, 256],
        "expected": {"convert": True, "source_mode_recorded": True},
        "checks": {"A5": "build 成功或明确拒绝；不崩溃"},
    })

    p = os.path.join(OUT, "T9b_cmyk.jpg")
    Image.fromarray(base).convert("CMYK").save(p, quality=95)
    files.append("T9b_cmyk.jpg")
    write_spec("T9b_cmyk", "T9b_cmyk.jpg", {
        "purpose": "T9/A5：CMYK JPEG —— 自动转换为 RGB，不崩溃",
        "class_expected": "any", "size": [256, 256],
        "expected": {"convert": True},
        "checks": {"A5": "build 成功或明确拒绝；不崩溃"},
    })

    h, w = base.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    rad = np.hypot((xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2))
    alpha = np.clip(255 * (1.0 - 0.55 * rad), 60, 255).astype(np.uint8)
    rgba = np.dstack([base, alpha])
    files.append(save_rgba(rgba, "T9c_alpha.png"))
    write_spec("T9c_alpha", "T9c_alpha.png", {
        "purpose": "T9/A5：带 alpha 的 PNG —— 按 alpha 阈值合成，不崩溃；source.mode 记录 RGBA",
        "class_expected": "any", "size": [256, 256],
        "expected": {"convert": True, "alpha_threshold": 128},
        "checks": {"A5": "build 成功或明确拒绝；不崩溃"},
    })

    p = os.path.join(OUT, "T9d_palette.png")
    Image.fromarray(base).convert("P", palette=Image.ADAPTIVE, colors=16).save(p)
    files.append("T9d_palette.png")
    write_spec("T9d_palette", "T9d_palette.png", {
        "purpose": "T9/A5：调色板模式 PNG —— 自动转 RGBA8",
        "class_expected": "any", "size": [256, 256],
        "expected": {"convert": True},
        "checks": {"A5": "build 成功或明确拒绝；不崩溃"},
    })
    return files


# ---------------------------------------------------------------- T10 中文名/损坏
def gen_t10() -> list[str]:
    size = 512
    c = Canvas(size, size)
    keys = [[0.0, 10.0], [1.0, 3.0]]
    path = bezier((60, 400), (180, 100), (340, 480), (470, 160))
    c.add_stroke(path, key_curve(keys), const_color((18, 18, 24)))
    f = "T10a_中文名_大图.png"
    save_rgb(c.to_array(), f)
    write_spec("T10a_中文名_大图", f, {
        "purpose": "T10/A5：非 ASCII 文件名 + 512 大图 —— 路径必须支持中文；build 成功",
        "class_expected": "lineart", "size": [size, size],
        "stroke": {"width_px": {"mode": "linear_keys", "keys": keys},
                   "color_rgb": [18, 18, 24], "note": "全宽（直径）；t=弧长归一化"},
        "expected": {"handling": "build_ok", "non_ascii_path": True},
        "checks": {"A5": "中文名可读可写；不崩溃"},
    })

    write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40 + b"this-is-not-a-real-png", "T10b_损坏文件.png")
    write_spec("T10b_损坏文件", "T10b_损坏文件.png", {
        "purpose": "T10/A5：损坏 PNG —— 必须拒绝且原因明确（E_INPUT_CORRUPT / E_INPUT_FORMAT）",
        "class_expected": "corrupt", "size": None,
        "expected_error": ["E_INPUT_CORRUPT", "E_INPUT_FORMAT"],
        "checks": {"A5": "拒绝码属于 {E_INPUT_CORRUPT,E_INPUT_FORMAT}"},
    })

    tiny = np.full((8, 8, 3), 255, np.uint8)
    tiny[2:5, 2:5] = 0
    files = [f, "T10b_损坏文件.png"]
    files.append(save_rgb(tiny, "T10c_太小_8x8.png"))
    write_spec("T10c_太小_8x8", "T10c_太小_8x8.png", {
        "purpose": "A5：过小图像（<32×32）—— 必须拒绝 E_INPUT_TOO_SMALL",
        "class_expected": "too_small", "size": [8, 8],
        "expected_error": "E_INPUT_TOO_SMALL",
        "checks": {"A5": "拒绝码=E_INPUT_TOO_SMALL"},
    })

    write_bytes(b"not an image at all\n", "T10d_不是图片.txt")
    files.append("T10d_不是图片.txt")
    write_spec("T10d_不是图片", "T10d_不是图片.txt", {
        "purpose": "A5：不支持的格式 —— 必须拒绝 E_INPUT_FORMAT/E_INPUT_MODE 并列出支持格式",
        "class_expected": "invalid_format", "size": None,
        "expected_error": ["E_INPUT_FORMAT", "E_INPUT_MODE"],
        "checks": {"A5": "拒绝且原因明确"},
    })
    return files


# ---------------------------------------------------------------- T11 taper 三表达
def gen_t11() -> list[str]:
    size = DEFAULT_SIZE
    keys = [[0.0, 20.0], [1.0, 3.0]]
    path = bezier((50, 330), (160, 120), (260, 300), (350, 60))
    c = Canvas(size, size)
    info = c.add_stroke(path, key_curve(keys), const_color((40, 30, 40)))
    f = "T11_taper_three_expressions.png"
    save_rgb(c.to_array(), f)
    spec = {
        "purpose": "A14：同一 taper（20→3px 线性）的 linear / spline / preset 三种表达必须渲染一致（两两差异<1%）",
        "class_expected": "lineart",
        "size": [size, size],
        "stroke": {
            "width_px": {"mode": "linear_keys", "keys": keys,
                         "note": "全宽（直径，像素）；t=弧长归一化"},
            "color_rgb": [40, 30, 40],
            "arc_length_px": round(info["arc_length_px"], 2),
            "centerline_px": centerline_px(path),
        },
        "equivalent_expressions_px": {
            "linear": {"interp": "linear", "keys": [[0.0, 20.0], [1.0, 3.0]]},
            "spline": {"interp": "spline", "keys": [[0.0, 20.0], [0.5, 11.5], [1.0, 3.0]]},
            "preset": {"interp": "preset", "preset": "taper-out", "scale": 20.0, "strength": 0.85},
        },
        "checks": {"A14": "两两渲染平均差异 < 1%（相对 255）", "A13": "宽度反演 32 点最大相对误差<5%"},
    }
    write_spec("T11_taper_three_expressions", f, spec)
    return [f]


# ---------------------------------------------------------------- T12 颜色渐变
def gen_t12() -> list[str]:
    size = DEFAULT_SIZE
    stops = [
        {"t": 0.0, "rgb": [220, 60, 70]},
        {"t": 0.5, "rgb": [140, 90, 150]},
        {"t": 1.0, "rgb": [40, 130, 220]},
    ]
    path = bezier((60, 290), (180, 150), (280, 360), (350, 190))
    c = Canvas(size, size)
    info = c.add_stroke(path, key_curve([[0.0, 14.0], [1.0, 14.0]]), color_stops_fn(stops))
    f = "T12_color_gradient.png"
    save_rgb(c.to_array(), f)
    spec = {
        "purpose": "T12：颜色沿笔画渐变（已知 stops）—— 验颜色曲线反演（色调维度），能抓到单调变暗/变淡",
        "class_expected": "lineart",
        "size": [size, size],
        "stroke": {
            "width_px": {"mode": "constant", "value": 14.0, "note": "全宽（直径，像素）"},
            "color_stops": stops,
            "color_interp": "linear",
            "arc_length_px": round(info["arc_length_px"], 2),
            "centerline_px": centerline_px(path),
        },
        "checks": {"T12": "恢复的颜色曲线沿 t 与真值 stops 的平均 ΔE 记录并判定（SHOULD）"},
    }
    write_spec("T12_color_gradient", f, spec)
    return [f]


# ---------------------------------------------------------------- T13 step 区间
def gen_t13() -> list[str]:
    size = DEFAULT_SIZE
    keys = [[0.0, 20.0], [0.5, 6.0]]
    path = bezier((60, 220), (160, 170), (260, 290), (350, 210))
    c = Canvas(size, size)
    info = c.add_stroke(path, step_curve(keys), const_color((30, 30, 40)))
    f = "T13_step_intervals.png"
    save_rgb(c.to_array(), f)
    spec = {
        "purpose": "A15：分段区间笔触（step）—— 宽度在 t=0.5 处从 20px 切到 6px，不得被常量/线性近似掉",
        "class_expected": "lineart",
        "size": [size, size],
        "stroke": {
            "width_px": {"mode": "step", "keys": keys,
                         "note": "全宽（直径，像素）；左闭右开区间，t=0.5 为边界"},
            "color_rgb": [30, 30, 40],
            "arc_length_px": round(info["arc_length_px"], 2),
            "centerline_px": centerline_px(path),
        },
        "step_truth": {"before_t": 0.25, "before_width_px": 20.0, "after_t": 0.75,
                       "after_width_px": 6.0, "boundary_t": 0.5},
        "checks": {"A15": "恢复的 width 曲线：w(0.25)≈20、w(0.75)≈6、边界 t∈[0.4,0.6]；interp 优先 step"},
    }
    write_spec("T13_step_intervals", f, spec)
    return [f]


# ===================================================================== 主流程
_GENERATORS: list[Callable[[], list[str]]] = [
    gen_t1, gen_t2, gen_t3, gen_t4, gen_t5, gen_t5b, gen_t6, gen_t7, gen_t8,
    gen_t9, gen_t10, gen_t11, gen_t12, gen_t13,
]


def generate(out_dir: str | None = None, *, verbose: bool = True) -> dict[str, Any]:
    """重建全部 T1–T13 用例 + spec + manifest；返回 manifest dict（幂等、确定性）。"""
    global OUT
    OUT = os.path.abspath(out_dir or OUT)
    os.makedirs(OUT, exist_ok=True)
    files: list[str] = []
    for gen in _GENERATORS:
        files.extend(gen())

    entries: list[dict[str, Any]] = []
    spec_names = sorted(
        f[: -len("_spec.json")]
        for f in os.listdir(OUT)
        if f.endswith("_spec.json")
    )
    for name in spec_names:
        spec_path = os.path.join(OUT, f"{name}_spec.json")
        with open(spec_path, "r", encoding="utf-8") as fh:
            spec = json.load(fh)
        fpath = os.path.join(OUT, spec["file"])
        entries.append({
            "case": name,
            "file": spec["file"],
            "spec": f"{name}_spec.json",
            "class_expected": spec.get("class_expected"),
            "purpose": spec.get("purpose", ""),
            "size": spec.get("size"),
            "sha256": _sha256(fpath) if os.path.exists(fpath) else None,
        })
    manifest = {
        "schema": SCHEMA,
        "generator": "tests/gen_fixtures.py",
        "seed": SEED,
        "root": "assets/testcases",
        "cases": entries,
        "count": len(entries),
        "files": sorted(set(files) | {f"{n}_spec.json" for n in spec_names}),
    }
    mpath = os.path.join(OUT, MANIFEST)
    with open(mpath, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    _write_readme(entries)
    if verbose:
        print(f"[gen_fixtures] {len(entries)} 个用例 → {OUT}")
        for e in entries:
            print(f"  {e['case']:<30} {e['file']:<36} {e['size']}  class={e['class_expected']}")
    return manifest


def _write_readme(entries: list[dict[str, Any]]) -> None:
    lines = [
        "# 合成测试用例（T1–T13）",
        "",
        "> 由 `tests/gen_fixtures.py` 确定性生成（seed=%d），不要手改图片。" % SEED,
        "> 每张图附 `<case>_spec.json` 真值（已知宽度曲线 / 颜色 stops / 预期错误码），",
        "> 供 `scripts/run_acceptance.py` 做反演精度验收。`manifest.json` 记录 sha256。",
        "",
        "| 用例 | 文件 | 尺寸 | 预期类别 | 用途 |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        size = "×".join(str(v) for v in e["size"]) if e["size"] else "—"
        purpose = (e["purpose"] or "").split("：")[0]
        purpose = purpose.replace("|", "/")
        lines.append(f"| {e['case']} | {e['file']} | {size} | {e['class_expected']} | {purpose} |")
    lines.append("")
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def check_manifest(out_dir: str | None = None) -> list[str]:
    """只读校验：manifest 与磁盘文件 sha256 是否一致；返回问题列表（空=一致）。"""
    root = os.path.abspath(out_dir or OUT)
    mpath = os.path.join(root, MANIFEST)
    if not os.path.exists(mpath):
        return [f"缺少 manifest: {mpath}"]
    with open(mpath, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    problems: list[str] = []
    for e in manifest.get("cases", []):
        fpath = os.path.join(root, e["file"])
        if not os.path.exists(fpath):
            problems.append(f"缺文件: {e['file']}")
            continue
        if e.get("sha256") and _sha256(fpath) != e["sha256"]:
            problems.append(f"内容与 manifest 不一致（陈旧/被手改）: {e['file']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 / 校验 fourier-brushstroke 合成测试集")
    ap.add_argument("--out", default=None, help=f"输出目录（默认 {OUT}）")
    ap.add_argument("--check", action="store_true", help="只校验 manifest sha256，不写文件")
    args = ap.parse_args(argv)
    if args.check:
        problems = check_manifest(args.out)
        if problems:
            print("[gen_fixtures] 校验失败：")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("[gen_fixtures] manifest 校验通过（文件与 sha256 一致）")
        return 0
    generate(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())






