"""R2 一笔画合成：把逐笔画文档串成**一条**闭合路径，只做**一次** FFT。

职责
----
1. `build_one_stroke(doc, cfg, warn, harmonics, max_fft)`：逐笔画 `.fbs` → one_stroke 文档；
2. `build_path(one_doc, resolution)`：一次 ifft 预计算路径采样/半径/颜色/alpha/笔形，
   供静态渲染与视频逐帧增量绘制共用（视频不得逐帧遍历系数，见 R3 旧坑 6）。

关键决策与原因
--------------
- **排序**：按 `layer` 升序；层内 region_fill 先于 medial_branch（同层填充在底），
  两组内各自做最近邻 + 受限 2-opt（允许反向），把抬笔移动压到最小 —— 抬笔段也要被
  傅里叶级数表示，越短越不破坏其余部分。
- **全部段都进路径**：墨线段（width>0）、抬笔 travel（width=0）、region_fill 轮廓
  （width=0）都在同一条路径上；travel/填充段半径 0，渲染时跳过（不设半径下限）。
- **恰好一次 FFT**：整条路径按 `M = next_pow2(≈4·总弧长_px)`（上限 `max_fft`）重采样后
  做一次 `fft`；`N = min(harmonics, M//2-2)` 显式记录在 `stats.fft_passes == 1`。
- **显式填充侧表**：`one_stroke_fills = [t0, t1, [r,g,b,a], 显式poly, color_field|None, holes|None]`
  （旧 4 元素 / 5 元素记录仍被渲染端兼容；holes = 世界坐标闭合环列表，渲染时用底色挖洞），
  填充边界用原始多边形，不用"按弧段有损反推"（旧版坑 3：反推多边形不闭合会填出直边碎片）；
  第 5 元素（可选）是区域颜色场，渲染端据此双线性采样，避免渐变色块被涂成单一平色。
- **路径偏差**：用原逐笔画采样点逐点求到「合成路径连续折线」的距离（不是最近顶点），
  @R 目标 ≤0.05px；只做局部窗口搜索（原采样点已知全局弧长参数），不写死全图 KNN。
- **宽度/颜色/alpha/笔形逐段曲线**：属性沿全局路径参数 t（弧长归一化）采样后自适应
  简化（宽度按契约量化步长、颜色按 2.5/255 L1 色差、shape 用 step 关键点），
  宽度 0 的 travel/填充区间显式落在宽度曲线上。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import io_format
from .attribute import eval_color, eval_numeric, eval_shape
from .config import FBSConfig
from .render import _fill_rgba, _resolve_style, sample_stroke
from .util import FBSError, WarningLog, fill_holes

_SAMPLES_PER_PX = 4.0          # 与渲染器同一采样密度约定（CONTRACT §7 n ≈ 4·arc_px）
_TWO_OPT_PASSES = 4            # 「受限 2-opt」：固定轮数上限，不追全局最优
_PATH_DEVIATION_TARGET_PX = 0.05


# ------------------------------------------------------------------ 几何工具
def _next_pow2(n: int) -> int:
    """返回 ≥ n 的最小 2 的幂（n≥1）。"""
    n = int(max(1, n))
    return 1 << (n - 1).bit_length()


def _polyline_cum(points: np.ndarray, closed: bool = False) -> np.ndarray:
    """各顶点累计弧长（closed 时末尾追加闭合段长度）。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2:
        return np.zeros(len(pts), dtype=np.float64)
    nxt = np.roll(pts, -1, axis=0) if closed else pts[1:]
    seg = np.linalg.norm(nxt - pts[: len(nxt)], axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    return cum[: len(pts)] if not closed else cum


def _resample_closed(points: np.ndarray, n: int) -> np.ndarray:
    """按弧长均匀重采样闭合折线（首尾不重复）。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    n = int(max(2, n))
    if len(pts) < 2:
        return np.repeat(pts[:1], n, axis=0)
    nxt = np.roll(pts, -1, axis=0)
    seg = np.linalg.norm(nxt - pts, axis=1)
    total = float(seg.sum())
    if total <= 1e-15:
        return np.repeat(pts[:1], n, axis=0)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    targets = np.linspace(0.0, total, n, endpoint=False)
    idx = np.clip(np.searchsorted(cum, targets, side="right") - 1, 0, len(seg) - 1)
    frac = (targets - cum[idx]) / np.maximum(seg[idx], 1e-15)
    return pts[idx] + (nxt[idx] - pts[idx]) * frac[:, None]


def _resample_path_attrs(
    points: np.ndarray,
    widths: np.ndarray,
    rgb: np.ndarray,
    alpha: np.ndarray,
    n: int,
    closed: bool = True,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """按弧长均匀重采样折线，并线性插值宽度/颜色/alpha（closed 首尾不重复）。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    n = int(max(2, n))
    if closed:
        nxt = np.roll(pts, -1, axis=0)
    else:
        nxt = pts[1:]
    seg = np.linalg.norm(nxt - pts[: len(nxt)], axis=1)
    total = float(seg.sum())
    if total <= 1e-15:
        return (
            np.repeat(pts[:1], n, axis=0),
            np.repeat(np.asarray(widths, dtype=np.float64)[:1], n),
            np.repeat(np.asarray(rgb, dtype=np.float64)[:1], n, axis=0),
            np.repeat(np.asarray(alpha, dtype=np.float64)[:1], n),
        )
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    if closed:
        targets = np.linspace(0.0, total, n, endpoint=False)
    else:
        targets = np.linspace(0.0, total, n)
    idx = np.clip(np.searchsorted(cum, targets, side="right") - 1, 0, len(seg) - 1)
    frac = (targets - cum[idx]) / np.maximum(seg[idx], 1e-15)
    if closed:
        nxt_pts = pts[(idx + 1) % len(pts)]
        nxt_w = np.asarray(widths, dtype=np.float64)[(idx + 1) % len(widths)]
        nxt_c = np.asarray(rgb, dtype=np.float64)[(idx + 1) % len(rgb)]
        nxt_a = np.asarray(alpha, dtype=np.float64)[(idx + 1) % len(alpha)]
    else:
        j = np.minimum(idx + 1, len(pts) - 1)
        nxt_pts = pts[j]
        nxt_w = np.asarray(widths, dtype=np.float64)[j]
        nxt_c = np.asarray(rgb, dtype=np.float64)[j]
        nxt_a = np.asarray(alpha, dtype=np.float64)[j]
    out_pts = pts[idx] + (nxt_pts - pts[idx]) * frac[:, None]
    out_w = np.asarray(widths, dtype=np.float64)[idx] + (nxt_w - np.asarray(widths, dtype=np.float64)[idx]) * frac
    out_c = np.asarray(rgb, dtype=np.float64)[idx] + (nxt_c - np.asarray(rgb, dtype=np.float64)[idx]) * frac[:, None]
    out_a = np.asarray(alpha, dtype=np.float64)[idx] + (nxt_a - np.asarray(alpha, dtype=np.float64)[idx]) * frac
    return out_pts, out_w, out_c, out_a


# ------------------------------------------------------------------ 排序
def _nn_order(entries: list[dict], indices: list[int]) -> list[tuple[int, bool]]:
    """最近邻贪心排序（允许反向），返回 [(entry_index, reversed)]。"""
    if not indices:
        return []
    n = len(indices)
    first = int(np.argmax([entries[i]["arc_length"] for i in indices]))  # 最长者起步
    starts = np.stack([entries[indices[k]]["points"][0] for k in range(n)])
    ends = np.stack([entries[indices[k]]["points"][-1] for k in range(n)])
    used = np.zeros(n, dtype=bool)
    order: list[tuple[int, bool]] = []
    cur = first
    used[cur] = True
    order.append((indices[cur], False))
    tail = ends[cur]
    for _ in range(n - 1):
        d0 = np.linalg.norm(starts - tail, axis=1)
        d1 = np.linalg.norm(ends - tail, axis=1)
        d0[used] = np.inf
        d1[used] = np.inf
        best0 = int(np.argmin(d0))
        best1 = int(np.argmin(d1))
        if d0[best0] <= d1[best1]:
            cur = best0
            rev = False
            tail = ends[cur]
        else:
            cur = best1
            rev = True
            tail = starts[cur]
        used[cur] = True
        order.append((indices[cur], rev))
    return order


def _oriented_endpoints(entries: list[dict], order: list[tuple[int, bool]]) -> tuple[np.ndarray, np.ndarray]:
    """按当前顺序/方向返回各段起止点 (n,2)。"""
    starts = np.stack([entries[i]["points"][-1] if rev else entries[i]["points"][0] for i, rev in order])
    ends = np.stack([entries[i]["points"][0] if rev else entries[i]["points"][-1] for i, rev in order])
    return starts, ends


def _two_opt(entries: list[dict], order: list[tuple[int, bool]], passes: int = _TWO_OPT_PASSES) -> list[tuple[int, bool]]:
    """受限 2-opt：每轮只算受影响的两条边，块反转时同步翻转段方向。"""
    n = len(order)
    if n < 4:
        return order
    starts, ends = _oriented_endpoints(entries, order)
    order = list(order)
    for _ in range(int(passes)):
        improved = False
        for i in range(1, n - 1):
            j = np.arange(i, n - 1)
            if len(j) == 0:
                continue
            a_old = float(np.linalg.norm(ends[i - 1] - starts[i]))
            b_old = np.linalg.norm(ends[j] - starts[j + 1], axis=1)
            a_new = np.linalg.norm(ends[i - 1] - ends[j], axis=1)
            b_new = np.linalg.norm(starts[i] - starts[j + 1], axis=1)
            gains = (a_old + b_old) - (a_new + b_new)
            k = int(np.argmax(gains))
            if float(gains[k]) > 1e-9:
                jj = int(j[k])
                order[i : jj + 1] = [(idx, not rev) for idx, rev in reversed(order[i : jj + 1])]
                starts, ends = _oriented_endpoints(entries, order)
                improved = True
        if not improved:
            break
    return order


def _entry_centroid(entries: list[dict], idxs: list[int]) -> tuple[float, float]:
    """一组段的大致中心（取各段首/中/尾采样点的均值，够排序用）。"""
    pts = []
    for i in idxs:
        p = entries[i]["points"]
        pts.append(p[0])
        pts.append(p[len(p) // 2])
        pts.append(p[-1])
    arr = np.asarray(pts, dtype=np.float64)
    m = arr.mean(axis=0)
    return float(m[0]), float(m[1])


def _order_fill_groups(entries: list[dict], fill_idxs: list[int]) -> list[int]:
    """填充段按"区域组"连续画：同一区域的蛇形扫描线挨着画完，再按最近邻切到下一区域。

    若不分组，最近邻会把两个区域的行交错着画，画笔在区域之间来回飞，抬笔行程暴涨。
    """
    groups: dict[int, list[int]] = {}
    for i in fill_idxs:
        rid = int(entries[i].get("region_id", -1) or -1)
        groups.setdefault(rid, []).append(i)
    if not groups:
        return []
    keys = list(groups.keys())
    cents = {k: _entry_centroid(entries, groups[k]) for k in keys}
    remaining = set(keys)
    cur = np.asarray(cents[keys[0]], dtype=np.float64) if keys else np.zeros(2)
    ordered: list[int] = []
    while remaining:
        nxt = min(remaining, key=lambda k: (cents[k][0] - cur[0]) ** 2 + (cents[k][1] - cur[1]) ** 2)
        remaining.discard(nxt)
        ordered.extend(groups[nxt])          # 组内保持原始蛇形顺序
        cur = np.asarray(cents[nxt], dtype=np.float64)
    return ordered


def _order_spatial(entries: list[dict]) -> list[tuple[int, bool]]:
    """按区域逐块画（贪心最近邻选下一块）：每块先铺该块的填充、再画该块的线条。

    这样画面是一块一块"长出来"的（脸→头发→衣服…），不是先全铺色再全勾线；
    块间用贪心最近邻而不是固定 y/x 排序，避免画笔在画布上大跨度来回飞。
    """
    groups: dict[object, list[int]] = {}
    for i, e in enumerate(entries):
        rid = int(e.get("region_id", -1) or -1)
        if rid >= 0:
            key: object = ("r", rid)
        else:
            px, py = _entry_centroid(entries, [i])
            key = ("cell", int(py * 16.0), int(px * 16.0))
        groups.setdefault(key, []).append(i)
    cents = {k: _entry_centroid(entries, idxs) for k, idxs in groups.items()}
    remaining = set(groups.keys())
    first = min(remaining, key=lambda k: (cents[k][1], cents[k][0]))
    remaining.discard(first)
    cur = np.asarray(cents[first], dtype=np.float64)
    ordered: list[int] = []
    seq_groups = [first] + []
    while remaining:
        nxt = min(remaining, key=lambda k: (cents[k][0] - cur[0]) ** 2 + (cents[k][1] - cur[1]) ** 2)
        remaining.discard(nxt)
        seq_groups.append(nxt)
        cur = np.asarray(cents[nxt], dtype=np.float64)
    order: list[tuple[int, bool]] = []
    last_end: np.ndarray | None = None
    for key in seq_groups:
        idxs = groups[key]
        fills = [i for i in idxs if entries[i]["is_fill"]]
        medials = [i for i in idxs if not entries[i]["is_fill"]]
        if fills:
            for i in _order_fill_groups(entries, fills):
                order.append((i, False))
                last_end = np.asarray(entries[i]["points"][-1], dtype=np.float64)
        if medials:
            if last_end is not None:
                pt = last_end
                medials = sorted(
                    medials,
                    key=lambda i: float(np.min(np.linalg.norm(entries[i]["points"] - pt[None, :], axis=1))),
                )
            for i, rev in _two_opt(entries, _nn_order(entries, medials)):
                order.append((i, rev))
                pts = entries[i]["points"]
                last_end = np.asarray(pts[0] if rev else pts[-1], dtype=np.float64)
    return order


def _order_reading(entries: list[dict]) -> list[tuple[int, bool]]:
    """从左到右（阅读顺序）作画：适合 logo / 文字 / 签名，笔序像"写"出来一样。

    填充按区域整组占位（组内保持蛇形顺序），中轴笔画按最左端 x 排序；两者按 x 归并，
    使笔在画布上基本只向右推进，而不是最近邻那样来回跳。
    """
    fills = [i for i, e in enumerate(entries) if e["is_fill"]]
    medials = [i for i, e in enumerate(entries) if not e["is_fill"]]

    def key_of(i: int) -> float:
        p = entries[i]["points"]
        return float(np.min(p[:, 0]))

    fill_groups: dict[int, list[int]] = {}
    for i in fills:
        fill_groups.setdefault(int(entries[i].get("region_id", -1) or -1), []).append(i)
    fill_items: list[tuple[float, str, list[int]]] = []
    for rid, idxs in fill_groups.items():
        seq = _order_fill_groups(entries, idxs)
        fill_items.append((min(key_of(i) for i in idxs), "fill", seq))
    medial_items: list[tuple[float, str, list[int]]] = [
        (key_of(i), "medial", [i]) for i in medials
    ]
    merged = sorted(fill_items + medial_items, key=lambda t: (t[0], 0 if t[1] == "fill" else 1))
    order: list[tuple[int, bool]] = []
    last_end: np.ndarray | None = None
    for _x, _kind, seq in merged:
        for i in seq:
            pts = entries[i]["points"]
            rev = False
            if last_end is not None and entries[i]["is_fill"] is False:
                d0 = float(np.linalg.norm(pts[0] - last_end))
                d1 = float(np.linalg.norm(pts[-1] - last_end))
                rev = d1 < d0
            order.append((i, rev))
            last_end = np.asarray(pts[0] if rev else pts[-1], dtype=np.float64)
    return order


def _order_entries(entries: list[dict], warn: WarningLog, cfg: FBSConfig | None = None) -> list[tuple[int, bool]]:
    """排序一笔画的作画顺序。

    默认 ``layer``：按 layer 升序；层内 region_fill 先于 medial_branch，各自 NN + 受限 2-opt。
    ``path_order="spatial"``：按区域空间顺序（上→下、左→右）逐块画——同一块的填充先铺、
    细节后画，再移到下一块；画面会像画师一样一块一块"长出来"，而不是先全铺色再全勾线。
    """
    path_order = str(getattr(cfg, "path_order", "layer") if cfg is not None else "layer")
    if path_order == "spatial":
        return _order_spatial(entries)
    if path_order == "reading":
        return _order_reading(entries)
    if path_order == "lines_first":
        # 像人画画：先把线稿画出来（画面立刻具象），最后再用宽笔把黑/色块涂上去。
        # 填充是后画的，会盖住区域内部的线——参考图里那些区域本来就是实色，观感正确。
        medials = [i for i, e in enumerate(entries) if not e["is_fill"]]
        fills = [i for i, e in enumerate(entries) if e["is_fill"]]
        order: list[tuple[int, bool]] = []
        layers = sorted({int(entries[i]["layer"]) for i in medials})
        for layer in layers:
            grp = [i for i in medials if int(entries[i]["layer"]) == layer]
            if grp:
                order.extend(_two_opt(entries, _nn_order(entries, grp)))
        if fills:
            order.extend((i, False) for i in _order_fill_groups(entries, fills))
        return order
    layers = sorted({int(e["layer"]) for e in entries})
    order = []
    for layer in layers:
        idxs = [i for i, e in enumerate(entries) if int(e["layer"]) == layer]
        fills = [i for i in idxs if entries[i]["is_fill"]]
        medials = [i for i in idxs if not entries[i]["is_fill"]]
        if fills:
            order.extend((i, False) for i in _order_fill_groups(entries, fills))
        if medials:
            order.extend(_two_opt(entries, _nn_order(entries, medials)))
    return order


# ------------------------------------------------------------------ 曲线压缩
def _dp_simplify(x: np.ndarray, v: np.ndarray, tol: float) -> list[int]:
    """Douglas-Peucker 一维简化：返回保留点索引（首尾必留），最大垂直偏差 ≤ tol。"""
    n = len(x)
    if n <= 2:
        return list(range(n))
    keep = {0, n - 1}
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a <= 1:
            continue
        xa, xb = float(x[a]), float(x[b])
        span = xb - xa
        if span <= 1e-15:
            continue
        va, vb = float(v[a]), float(v[b])
        pred = va + (vb - va) * (x[a + 1 : b] - xa) / span
        dev = np.abs(np.asarray(v[a + 1 : b], dtype=np.float64) - pred)
        k = int(np.argmax(dev))
        if float(dev[k]) > tol:
            m = a + 1 + k
            keep.add(m)
            stack.append((a, m))
            stack.append((m, b))
    return sorted(keep)


def _width_curve(
    t: np.ndarray,
    width: np.ndarray,
    width_tol: float,
    zero_spans: list[tuple[float, float]] | None = None,
    ramp: float = 2e-4,
) -> dict:
    """由逐点宽度生成 `linear` 数值曲线：显式保留零宽（抬笔/填充）区间边界。

    zero_spans 给出精确的零宽 t 区间（来自 step_spans）：在这些区间两侧各插入
    一段 ramp 长度的线性过渡、区间内强制 0。ramp 取 ≥2 个 t 量化步长，
    保证写盘量化后零宽区间仍然成立（旧版把抬笔画成横丝的防护）。
    """
    w = np.asarray(width, dtype=np.float64)
    n = len(w)
    keys: list[list[float]] = []

    def push(tk: float, vk: float) -> None:
        if keys and abs(keys[-1][0] - tk) < 1e-12:
            keys[-1][1] = float(vk)
        else:
            keys.append([float(tk), float(vk)])

    i = 0
    while i < n:
        if w[i] <= 0.0:
            j = i
            while j < n and w[j] <= 0.0:
                j += 1
            push(float(t[i]), 0.0)
            if j - 1 > i:
                push(float(t[j - 1]), 0.0)
            i = j
        else:
            j = i
            while j < n and w[j] > 0.0:
                j += 1
            idx = _dp_simplify(t[i:j], w[i:j], width_tol)
            for k in idx:
                push(float(t[i + k]), float(w[i + k]))
            i = j
    if not keys:
        keys = [[0.0, 0.0]]
    # 精确零宽区间：插值取值 + 注入 0 值边界键（量化后仍成立）
    spans = [(float(a), float(b)) for a, b in (zero_spans or []) if float(b) > float(a) + 1e-9]
    if spans:
        arr_t = np.asarray([kk[0] for kk in keys], dtype=np.float64)
        arr_v = np.asarray([kk[1] for kk in keys], dtype=np.float64)
        extra: list[list[float]] = []
        step = float(max(ramp, 1e-6))
        for a, b in spans:
            if a > 0.0:
                ta = max(0.0, a - step)
                extra.append([ta, float(np.interp(ta, arr_t, arr_v))])
                extra.append([a, 0.0])
            else:
                extra.append([0.0, 0.0])
            extra.append([b, 0.0])
            if b < 1.0:
                tb = min(1.0, b + step)
                extra.append([b + step if b + step < 1.0 else 1.0, float(np.interp(min(tb, 1.0), arr_t, arr_v))])
        keys = keys + extra
        keys.sort(key=lambda kk: kk[0])  # stable：同 t 时后注入的 0 值覆盖旧值
    # 收口：首尾 key 必须覆盖 [0,1]
    if keys and keys[0][0] > 0.0:
        keys.insert(0, [0.0, keys[0][1]])
    if keys and keys[-1][0] < 1.0:
        keys.append([1.0, keys[0][1]])
    return {"interp": "linear", "keys": keys, "clamp": True}


def _alpha_curve(t: np.ndarray, alpha: np.ndarray, tol: float = 0.02) -> dict:
    """由逐点 alpha 生成 `linear` 数值曲线（DP 简化，最大偏差 ≤ tol）。"""
    a = np.asarray(alpha, dtype=np.float64)
    if len(a) == 0:
        return {"interp": "constant", "keys": [[0.0, 1.0]], "clamp": True}
    if float(a.max() - a.min()) <= tol:
        return {"interp": "constant", "keys": [[0.0, float(a[0])]], "clamp": True}
    idx = _dp_simplify(np.asarray(t, dtype=np.float64), a, tol)
    keys = [[float(t[k]), float(np.clip(a[k], 0.0, 1.0))] for k in idx]
    if keys[0][0] > 0.0:
        keys.insert(0, [0.0, keys[0][1]])
    if keys[-1][0] < 1.0:
        keys.append([1.0, keys[-1][1]])
    return {"interp": "linear", "keys": keys, "clamp": True}


def _color_curve(t: np.ndarray, rgb: np.ndarray) -> dict:
    """由逐点颜色生成 color 曲线（stops，自适应：块间 RGB L1 色差 ≤ 2.5/255）。

    alpha 不在这里编码（a 固定 1.0）：透明度由独立的 alpha 曲线承担，避免把平滑
    alpha 变化展开成几万个颜色停靠点（体积纪律）；渲染时 final_alpha = a_stop × alpha_curve。
    """
    tol = 2.5 / 255.0
    n = len(rgb)
    if n == 0:
        return {"interp": "constant", "stops": [{"t": 0.0, "rgb": [20, 20, 22], "a": 1.0}]}
    rgb = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)

    def stop(k: int) -> dict:
        return {
            "t": float(t[k]),
            "rgb": [int(round(float(rgb[k, c]) * 255.0)) for c in range(3)],
            "a": 1.0,
        }

    stops: list[dict] = [stop(0)]
    last = 0
    for i in range(1, n):
        if float(np.abs(rgb[i] - rgb[last]).sum()) > tol:
            stops.append(stop(i))
            last = i
    if stops[-1]["t"] < 1.0:
        final = stop(n - 1)
        final["t"] = 1.0
        stops.append(final)
    if stops[0]["t"] > 0.0:
        first = dict(stops[0])
        first["t"] = 0.0
        stops.insert(0, first)
    return {"interp": "linear", "stops": stops}


def _t_decimals(cfg: FBSConfig, arc_length: float) -> int:
    """曲线参数 t 的写入小数位：由最长弧长像素反推（与 io_format 量化契约同一公式）。

    写盘时 io_format 会按同一公式计算 tnd（max(snd, ceil(log10(arc_px/(ε/2))))，上限 9）；
    这里提前算出，用于零宽区间两侧 ramp 的宽度（ramp ≥ 2 个量化步长才不会被量化抹掉）。
    """
    eps = float(cfg.epsilon_px)
    R = int(cfg.target_resolution)
    snd = io_format.decimals_for_scalar(eps, R)
    arc_px = max(float(arc_length), 0.0) * R
    if arc_px <= 0.0 or eps <= 0.0:
        return int(snd)
    nd = int(math.ceil(math.log10(arc_px / max(eps * 0.5, 1e-12))))
    return int(max(snd, min(nd, 9)))


def _shape_curve(shapes: list[str], t: np.ndarray) -> dict:
    """由逐点笔形生成分类 step 曲线（相邻相同值合并，D4：一笔画也允许笔形变化）。"""
    if not shapes:
        return {"interp": "step", "keys": [[0.0, "round"]]}
    keys: list[list] = []
    prev = None
    for k, sh in enumerate(shapes):
        val = str(sh)
        if val != prev:
            keys.append([float(t[k]), val])
            prev = val
    if not keys:
        keys = [[0.0, "round"]]
    if keys[0][0] > 0.0:
        keys.insert(0, [0.0, keys[0][1]])
    return {"interp": "step", "keys": keys}


# ------------------------------------------------------------------ 路径偏差
def _dist_to_segments(pts: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """点到一组线段的距离（pts (P,2)，a/b (P,2) 或 (1,2) 广播）。"""
    d = b - a
    l2 = np.sum(d * d, axis=1)
    l2 = np.where(l2 < 1e-18, 1e-18, l2)
    ap = pts - a
    tt = np.clip(np.sum(ap * d, axis=1) / l2, 0.0, 1.0)
    proj = a + d * tt[:, None]
    return np.linalg.norm(pts - proj, axis=1)


def _nearest_distances_to_path(points: np.ndarray, recon: np.ndarray, cell: float) -> np.ndarray:
    """逐点求到闭合折线的**精确最近距离**（空间网格 + 环形扩展，不做全量 KNN）。

    网格只影响候选集，环形扩展带下界剪枝，因此结果是精确值而不是近似值；
    自交/闭合重合点也能正确取到更近的另一段（局部窗口法会漏）。
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    M = len(recon)
    nxt = np.roll(recon, -1, axis=0)
    mids = 0.5 * (recon + nxt)
    seg_len = np.linalg.norm(nxt - recon, axis=1)
    half = float(seg_len.max()) * 0.5 if M else 0.0
    cell = float(max(cell, 1e-12))
    grid: dict[tuple[int, int], list[int]] = {}
    for j in range(M):
        key = (int(math.floor(float(mids[j, 0]) / cell)), int(math.floor(float(mids[j, 1]) / cell)))
        grid.setdefault(key, []).append(j)
    out = np.empty(len(pts), dtype=np.float64)
    for i in range(len(pts)):
        px, py = float(pts[i, 0]), float(pts[i, 1])
        qx, qy = int(math.floor(px / cell)), int(math.floor(py / cell))
        best = np.inf
        radius = 0
        max_radius = max(int(math.ceil(2.0 / cell)) + 4, 8)
        while radius <= max_radius:
            cand: list[int] = []
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if radius > 0 and max(abs(dx), abs(dy)) != radius:
                        continue
                    got = grid.get((qx + dx, qy + dy))
                    if got:
                        cand.extend(got)
            if cand:
                ci = np.asarray(cand, dtype=np.int64)
                d = _dist_to_segments(np.repeat(pts[i][None, :], len(ci), axis=0), recon[ci], nxt[ci])
                best = min(best, float(d.min()))
            if math.isfinite(best) and best <= max(0.0, radius * cell - half):
                break
            radius += 1
        out[i] = best if math.isfinite(best) else float(np.min(np.linalg.norm(recon - pts[i], axis=1)))
    return out


def _path_deviation_stats(
    orig_points: np.ndarray,
    orig_t: np.ndarray,
    recon: np.ndarray,
    target_resolution: float,
) -> dict:
    """原采样点到「合成路径连续折线」的距离统计（px @target_resolution）。

    先用已知全局弧长参数做局部窗口搜索（快），窗口距离 > 0.1px 的少数点
    （尖角/自交/闭合重合）再用空间网格精确复核（准确，覆盖全路径）。
    返回 rms / p99.5 / max 三个统计量：rms 作为 path_deviation_px 代表值，
    max 单独记录（尖叫切角允许到亚像素级）。
    """
    pts = np.asarray(orig_points, dtype=np.float64).reshape(-1, 2)
    if len(pts) == 0 or len(recon) < 2:
        return {"rms_px": 0.0, "p995_px": 0.0, "max_px": 0.0}
    M = len(recon)
    tq = np.asarray(orig_t, dtype=np.float64)
    base = np.floor(tq * M).astype(np.int64) % M
    half = 64
    offs = np.arange(-half, half + 1, dtype=np.int64)
    dist = np.empty(len(pts), dtype=np.float64)
    chunk = 8192
    for start in range(0, len(pts), chunk):
        p = pts[start : start + chunk]
        b = base[start : start + chunk]
        idx = (b[:, None] + offs[None, :]) % M
        d = np.linalg.norm(recon[idx] - p[:, None, :], axis=2)
        amin = np.argmin(d, axis=1)
        best = (b + offs[amin]) % M
        nxt = (best + 1) % M
        prv = (best - 1) % M
        d1 = _dist_to_segments(p, recon[prv], recon[best])
        d2 = _dist_to_segments(p, recon[best], recon[nxt])
        dist[start : start + chunk] = np.minimum(d1, d2)
    # 精确复核：窗口法可能因自交/闭合重合点或窗口错位高估
    recheck = dist > (0.1 / max(float(target_resolution), 1.0))
    if np.any(recheck):
        cell = 2.0 / max(float(target_resolution), 1.0)
        dist[recheck] = _nearest_distances_to_path(pts[recheck], recon, cell)
    d_px = dist * float(target_resolution)
    rms = float(np.sqrt(np.mean(d_px * d_px)))
    return {
        "rms_px": rms,
        "p995_px": float(np.percentile(d_px, 99.5)),
        "max_px": float(np.max(d_px)),
    }


# ------------------------------------------------------------------ 主入口：build_one_stroke
def _sample_field_rgb(color_field: dict, x: float, y: float) -> np.ndarray | None:
    """区域颜色场双线性采样（世界坐标；无效返回 None，由调用方回退代表色）。"""
    try:
        gw = int(color_field.get("gw") or color_field.get("grid") or 0)
        gh = int(color_field.get("gh") or color_field.get("grid") or 0)
        colors = color_field.get("colors") or []
        bbox = [float(v) for v in (color_field.get("bbox") or [0.0, 0.0, 1.0, 1.0])]
        if gw < 1 or gh < 1 or len(colors) < gw * gh or len(bbox) != 4:
            return None
        bx0, by0, bx1, by1 = bbox
        fx = (float(x) - bx0) / max(bx1 - bx0, 1e-12) * (gw - 1)
        fy = (float(y) - by0) / max(by1 - by0, 1e-12) * (gh - 1)
        fx = float(np.clip(fx, 0.0, gw - 1))
        fy = float(np.clip(fy, 0.0, gh - 1))
        ix, iy = int(math.floor(fx)), int(math.floor(fy))
        ix1, iy1 = min(ix + 1, gw - 1), min(iy + 1, gh - 1)
        tx, ty = fx - ix, fy - iy
        acc = np.zeros(3, dtype=np.float64)
        for yy, wy in ((iy, 1.0 - ty), (iy1, ty)):
            for xx, wx in ((ix, 1.0 - tx), (ix1, tx)):
                acc += np.asarray(colors[yy * gw + xx], dtype=np.float64) * (wx * wy)
        return np.clip(acc / 255.0, 0.0, 1.0)
    except (TypeError, ValueError, IndexError):
        return None


def _region_color_field(doc: dict, region_id: int) -> dict | None:
    for r in doc.get("regions") or []:
        if isinstance(r, dict):
            try:
                if int(r.get("id", -1)) == int(region_id):
                    cf = r.get("color_field")
                    return cf if isinstance(cf, dict) else None
            except (TypeError, ValueError):
                continue
    return None


def _normalize_fill_holes(value: Any) -> list[list[list[float]]] | None:
    """把 stroke["fill_holes"]（世界坐标闭合环）规范化为可 JSON 的环列表；无/非法返回 None。

    容错：非 list/tuple、点数 <3、非有限数、退化环跳过；首尾重复时去掉重复点。
    """
    if not isinstance(value, (list, tuple)):
        return None
    out: list[list[list[float]]] = []
    for ring in value:
        if not isinstance(ring, (list, tuple, np.ndarray)):
            continue
        try:
            arr = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
        except (TypeError, ValueError):
            continue
        if len(arr) < 3 or not bool(np.all(np.isfinite(arr))):
            continue
        if bool(np.allclose(arr[0], arr[-1])):
            arr = arr[:-1]
        if len(arr) < 3:
            continue
        out.append([[float(x), float(y)] for x, y in arr])
    return out or None


def _brush_fill_entries(
    doc: dict,
    stroke: dict,
    poly: np.ndarray,
    rgb: np.ndarray,
    alpha: float,
    R: int,
    cfg: FBSConfig,
    warn: WarningLog,
    holes: list[np.ndarray] | None = None,
) -> list[dict]:
    """把 region_fill 变成**宽笔扫掠**：水平蛇形扫描线，每条扫描线是一段宽笔画。

    这样视频里填充区不是"整块淡入"，而是笔一路扫过、颜色随笔画出现（与线条同一套笔触）。
    扫描线间距由区域尺寸导出（笔宽 = clamp(bbox 对角线/60, 3e-3, 1.2e-2) 世界单位），
    相邻行反向（蛇形），行内靠外的两端各外扩半个笔宽保证边界覆盖。
    """
    poly = np.asarray(poly, dtype=np.float64).reshape(-1, 2)
    if len(poly) < 3:
        return []
    x0, y0 = float(poly[:, 0].min()), float(poly[:, 1].min())
    x1, y1 = float(poly[:, 0].max()), float(poly[:, 1].max())
    diag = float(math.hypot(x1 - x0, y1 - y0))
    width = float(np.clip(diag / 40.0, 4e-3, 1.8e-2))           # 笔刷全宽（世界单位）
    spacing = max(width * 0.65, 1.5 / float(max(R, 1)))          # 行距：相邻行重叠 35%
    region_id = int(stroke.get("region_id", -1) or -1)
    color_field = _region_color_field(doc, region_id)
    layer = int(stroke.get("layer", 0) or 0)
    rgb = np.clip(np.asarray(rgb, dtype=np.float64).reshape(3), 0.0, 1.0)
    alpha = float(np.clip(alpha, 0.0, 1.0))

    # 用"轮廓栅格化 + 填洞"的掩码做行扫描，而不是解析求交：
    # 轮廓是追踪+简化得到的多边形，可能带自交/重复边，解析奇偶求交会在内部切出假的白色缝隙；
    # 栅格化后 fill_holes 正好对应"填充外轮廓（含洞）"的语义，行扫描只会得到实心区间。
    from PIL import Image, ImageDraw

    canvas = doc.get("canvas") or {}
    cw = int(math.ceil(float(canvas.get("width", 1.0)) * R))
    chh = int(math.ceil(float(canvas.get("height", 1.0)) * R))
    mask_img = Image.new("L", (max(cw, 2), max(chh, 2)), 0)
    ImageDraw.Draw(mask_img).polygon([(float(px) * R, float(py) * R) for px, py in poly], fill=255)
    mask = np.asarray(mask_img, dtype=np.uint8) > 127
    if mask.any():
        mask = fill_holes(mask)
    # 带洞区域：外轮廓实心掩码里挖掉洞（洞内不落填充笔，露出背景/下层）
    if mask.any() and holes:
        for hole in holes:
            hole_img = Image.new("L", mask_img.size, 0)
            ImageDraw.Draw(hole_img).polygon([(float(px) * R, float(py) * R) for px, py in hole], fill=255)
            hole_mask = np.asarray(hole_img, dtype=np.uint8) > 127
            if hole_mask.any():
                mask &= ~fill_holes(hole_mask)
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return []
    ry0, ry1 = int(ys.min()), int(ys.max())
    rx0, rx1 = int(xs.min()), int(xs.max())
    row_step_px = max(1, int(round(spacing * R)))
    half_ext = width * 0.35   # 行两端各外扩 35% 半宽，压住轮廓边缘的锯齿，外溢很小

    entries: list[dict] = []
    y_world = (ry0 + 0.5) / R
    row = 0
    while y_world < (ry1 + 0.5) / R:
        iy = min(max(int(round(y_world * R - 0.5)), 0), mask.shape[0] - 1)
        line = mask[iy]
        if line.any():
            padded = np.concatenate(([False], line, [False]))
            d = np.diff(padded.astype(np.int8))
            starts = np.nonzero(d == 1)[0]
            ends = np.nonzero(d == -1)[0]
            runs = [((float(s)) / R, (float(e)) / R) for s, e in zip(starts, ends) if e - s >= 2]
            if row % 2 == 1:
                runs = [(b, a) for (a, b) in reversed(runs)]
            for a, b in runs:
                aa = (a - half_ext) if a <= b else (a + half_ext)
                bb = (b + half_ext) if a <= b else (b - half_ext)
                col = rgb
                if color_field is not None:
                    sampled = _sample_field_rgb(color_field, (a + b) * 0.5, y_world)
                    if sampled is not None:
                        col = sampled
                pts = np.asarray([[aa, y_world], [bb, y_world]], dtype=np.float64)
                entries.append(
                    {
                        "points": pts,
                        "widths": np.full(2, width, dtype=np.float64),
                        "rgb": np.repeat(col.reshape(1, 3), 2, axis=0),
                        "alpha": np.full(2, alpha, dtype=np.float64),
                        "shapes": ["round", "round"],
                        "arc_length": float(abs(bb - aa)),
                        "layer": layer,
                        "region_id": region_id,
                        "is_fill": True,
                        "poly": None,          # 不再走整块多边形填充
                        "fill_rgba": None,
                    }
                )
        y_world += spacing
        row += 1
    if not entries:
        warn.add("W_FILL_SKIPPED", f"region_fill stroke.id={stroke.get('id')!r} 扫掠填充没有生成线段", severity="degrade")
    return entries


def _sample_entries(
    doc: dict, stroke: dict, R: int, cfg: FBSConfig, warn: WarningLog
) -> list[dict]:
    """把一条逐笔画 stroke 采样成一笔画的候选段列表（世界坐标 + 逐点属性）。"""
    is_fill = stroke.get("fill_role") == "region_fill"
    if is_fill:
        poly = stroke.get("poly")
        if poly is None or len(np.asarray(poly, dtype=np.float64).reshape(-1, 2)) < 3:
            warn.add("W_FILL_SKIPPED", f"region_fill stroke.id={stroke.get('id')!r} 缺少显式 poly，未参与一笔画", severity="degrade")
            return []
        poly_arr = np.asarray(poly, dtype=np.float64).reshape(-1, 2)
        region_id = int(stroke.get("region_id", -1) or -1)
        rgb, alpha = _fill_rgba(doc, stroke, warn)
        holes_norm = _normalize_fill_holes(stroke.get("fill_holes"))
        holes_arr = [np.asarray(ring, dtype=np.float64).reshape(-1, 2) for ring in (holes_norm or [])]
        if cfg.fill_mode == "brush":
            return _brush_fill_entries(doc, stroke, poly_arr, rgb, alpha, R, cfg, warn, holes=holes_arr)
        perimeter = float(np.linalg.norm(np.roll(poly_arr, -1, axis=0) - poly_arr, axis=1).sum())
        n = max(3, int(math.ceil(_SAMPLES_PER_PX * perimeter * R)))
        pts = _resample_closed(poly_arr, n)
        pts = np.vstack([pts, pts[:1]])  # 闭合轮廓：终点回到起点
        return [
            {
                "points": pts,
                "widths": np.zeros(len(pts), dtype=np.float64),
                "rgb": np.repeat(np.asarray(rgb, dtype=np.float64).reshape(1, 3), len(pts), axis=0),
                "alpha": np.full(len(pts), float(alpha), dtype=np.float64),
                "shapes": ["round"] * len(pts),
                "arc_length": float(perimeter),
                "layer": int(stroke.get("layer", 0) or 0),
                "region_id": region_id,
                "is_fill": True,
                "poly": poly_arr,
                "fill_rgba": (rgb, float(alpha)),
                # 带洞区域：洞环随 entry 透传到 one_stroke_fills（渲染端用底色挖洞）
                "fill_holes": holes_norm,
                # 区域颜色场：一笔画填充侧表必须带它，否则 fill 会被涂成单一平色
                # （柔光插画/渐变块面会整片"拍平"，与逐笔画渲染差距可达 4dB+）
                "color_field": _region_color_field(doc, region_id),
            }
        ]
    if not stroke.get("coef"):
        return []
    sampled = sample_stroke(doc, stroke, R, warn=warn)
    pts = np.asarray(sampled["points_world"], dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2:
        return []
    widths = np.asarray(sampled["widths_world"], dtype=np.float64).reshape(-1)
    rgb = np.asarray(sampled["rgb"], dtype=np.float64).reshape(-1, 3)
    alpha = np.asarray(sampled["alpha"], dtype=np.float64).reshape(-1)
    if sampled["closed"]:
        # 闭合笔画把首点补到末尾，成一笔画里真正画完整的闭环
        pts = np.vstack([pts, pts[:1]])
        widths = np.concatenate([widths, widths[:1]])
        rgb = np.vstack([rgb, rgb[:1]])
        alpha = np.concatenate([alpha, alpha[:1]])
    arc = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
    shapes = list(sampled["shapes"])
    if sampled["closed"]:
        shapes = shapes + shapes[:1]
    return [
        {
            "points": pts,
            "widths": np.maximum(widths, 0.0),
            "rgb": rgb,
            "alpha": alpha,
            "shapes": shapes,
            "arc_length": arc,
            "layer": int(stroke.get("layer", 0) or 0),
            "region_id": int(stroke.get("region_id", -1) or -1),
            "is_fill": False,
            "poly": None,
            "fill_rgba": None,
        }
    ]


def build_one_stroke(
    doc: dict,
    cfg: FBSConfig,
    warn: WarningLog | None = None,
    harmonics: int | None = None,
    max_fft: int | None = None,
) -> dict:
    """逐笔画 `.fbs` → one_stroke 文档（整条路径恰好一次 FFT）。"""
    warn = warn if warn is not None else WarningLog()
    if not isinstance(doc, dict):
        raise FBSError("E_INPUT_MODE", f"build_one_stroke 需要 dict 文档，收到 {type(doc).__name__}")
    if doc.get("mode") != "per_stroke":
        raise FBSError(
            "E_INPUT_MODE",
            f"一笔画合成需要 per_stroke 文档（当前 mode={doc.get('mode')!r}）",
            suggestions=["先跑 fbs.py build 生成逐笔画 .fbs，再跑 fbs.py onestroke"],
        )
    strokes = doc.get("strokes") or []
    if not strokes:
        raise FBSError("E_INPUT_MODE", "逐笔画文档没有 strokes，无法合成一笔画")
    R = int(cfg.target_resolution)
    harmonics = int(cfg.harmonics if harmonics is None else harmonics)
    max_fft = int(cfg.max_fft if max_fft is None else max_fft)
    if harmonics < 1:
        raise FBSError("E_CONFIG", f"harmonics 必须 ≥ 1: {harmonics}")
    if max_fft < 16:
        raise FBSError("E_CONFIG", f"max_fft 必须 ≥ 16: {max_fft}")

    # ---- 1) 逐笔画采样（4·arc·R 密度，数据导出）----
    entries: list[dict] = []
    for st in strokes:
        if not isinstance(st, dict):
            continue
        try:
            sampled_entries = _sample_entries(doc, st, R, cfg, warn)
        except FBSError as exc:
            warn.add("W_GEOMETRY_SKIP", f"stroke.id={st.get('id')!r} 采样失败被跳过: {exc.message}", severity="degrade")
            continue
        for e in sampled_entries:
            if e is not None and len(e["points"]) >= 2:
                entries.append(e)
    if not entries:
        raise FBSError("E_INPUT_MODE", "没有任何可用的笔画采样点，无法合成一笔画")

    # ---- 2) 排序（layer 升序 / spatial 空间顺序 + 组内最近邻 + 受限 2-opt）----
    order = _order_entries(entries, warn, cfg)

    # ---- 3) 拼接成一条闭合路径：墨线段 + 抬笔直线段，记录 span 弧长区间 ----
    pts_parts: list[np.ndarray] = []
    w_parts: list[np.ndarray] = []
    c_parts: list[np.ndarray] = []
    a_parts: list[np.ndarray] = []
    shp_parts: list[str] = []
    spans: list[dict] = []
    orig_pts: list[np.ndarray] = []
    orig_t: list[np.ndarray] = []
    arc = 0.0
    travel = 0.0
    prev_end: np.ndarray | None = None
    first_start: np.ndarray | None = None
    last_color = np.asarray([0.0, 0.0, 0.0], dtype=np.float64)
    last_alpha = 1.0

    def append_travel(p0: np.ndarray, p1: np.ndarray) -> None:
        nonlocal arc, travel
        d = float(np.linalg.norm(p1 - p0))
        if d <= 1e-12:
            return
        pts_parts.append(np.stack([p0, p1]))
        w_parts.append(np.zeros(2))
        c_parts.append(np.repeat(last_color.reshape(1, 3), 2, axis=0))
        a_parts.append(np.full(2, last_alpha))
        shp_parts.extend(["round", "round"])
        spans.append({"t0": arc, "t1": arc + d, "kind": "travel"})
        arc += d
        travel += d

    for (idx, rev) in order:
        e = entries[idx]
        p = e["points"][::-1] if rev else e["points"]
        w = e["widths"][::-1] if rev else e["widths"]
        c = e["rgb"][::-1] if rev else e["rgb"]
        a = e["alpha"][::-1] if rev else e["alpha"]
        shp = list(reversed(e["shapes"])) if rev else list(e["shapes"])
        if prev_end is None:
            first_start = p[0].copy()
        else:
            append_travel(prev_end, p[0])
        s0 = arc
        local = _polyline_cum(p, closed=False)
        arc += float(local[-1])
        pts_parts.append(p)
        w_parts.append(w)
        c_parts.append(c)
        a_parts.append(a)
        shp_parts.extend(shp)
        spans.append({"t0": s0, "t1": arc, "kind": "fill" if e["is_fill"] else "stroke", "entry": idx})
        orig_pts.append(p)
        orig_t.append(s0 + local)  # 记录全局弧长（最后统一除以总弧长）
        prev_end = p[-1].copy()
        last_color = np.asarray(c[-1], dtype=np.float64)
        last_alpha = float(a[-1])
    if first_start is not None and prev_end is not None:
        append_travel(prev_end, first_start)  # 闭合整条路径
    if arc <= 1e-12:
        raise FBSError("E_INPUT_MODE", "合成路径总弧长为 0，无法做一笔画")
    # 归一化所有 span 与原始点的全局 t
    for sp in spans:
        sp["t0"] = float(sp["t0"] / arc)
        sp["t1"] = float(sp["t1"] / arc)
    all_pts = np.vstack(pts_parts)
    all_w = np.concatenate(w_parts)
    all_c = np.vstack(c_parts)
    all_a = np.concatenate(a_parts)
    orig_pt_arr = np.vstack(orig_pts)
    orig_t_arr = np.concatenate(orig_t) / max(arc, 1e-15)

    # ---- 4) 一次 FFT：M 由亚像素契约反推 ----
    # 起始 M ≈ 4·总 arc_px（CONTRACT §7「路径采样按 arc·R 自适应」）；若重采样切角使路径
    # 偏差超过 0.05px 目标，则 M 逐次翻倍直到达标（上限 max_fft）。
    # 目标优先于固定密度，且绝不放宽指标、绝不静默。
    m_base = _next_pow2(max(16, int(math.ceil(_SAMPLES_PER_PX * arc * R))))
    m_cap = 1 << (int(max(1, max_fft)).bit_length() - 1)  # ≤ max_fft 的最大 2 的幂
    if m_base > m_cap:
        warn.add("W_ONE_STROKE_N", f"FFT 采样数被 max_fft 钳到 {m_cap}（需要 {m_base}）")
    M = max(16, min(m_base, m_cap))
    shape_ids = {"round": 0, "soft": 1, "marker": 2}
    all_shp = np.asarray([shape_ids.get(s, 0) for s in shp_parts], dtype=np.int64)
    # 形状按最近原始顶点取样（step 语义，不做平均）
    seg_all = np.linalg.norm(np.roll(all_pts, -1, axis=0) - all_pts, axis=1)
    cum_all = np.concatenate([[0.0], np.cumsum(seg_all)])
    shape_names = ["round", "soft", "marker"]

    def _fft_at(size_m: int):
        """在 size_m 个均匀路径采样上做一次 FFT，返回重建与系数（无逐帧循环）。"""
        res_pts, res_w, res_c, res_a = _resample_path_attrs(all_pts, all_w, all_c, all_a, size_m, closed=True)
        tt = np.linspace(0.0, float(cum_all[-1]), size_m, endpoint=False)
        near = np.clip(np.searchsorted(cum_all, tt, side="right") - 1, 0, len(all_pts) - 1)
        res_s = [shape_names[int(v)] for v in all_shp[near]]
        z = res_pts[:, 0] + 1j * res_pts[:, 1]
        zf = np.fft.fft(z) / size_m
        nn = int(min(harmonics, max(1, size_m // 2 - 2)))
        freq = np.zeros(size_m, dtype=np.complex128)
        ks = np.arange(-nn, nn + 1, dtype=np.int64)
        freq[ks % size_m] = zf[ks % size_m]
        recon = np.fft.ifft(freq) * size_m
        recon_pts = np.stack([recon.real, recon.imag], axis=1)
        maxerr = float(np.max(np.linalg.norm(recon_pts - res_pts, axis=1)))
        coef_list = [[int(k), float(zf[k % size_m].real), float(zf[k % size_m].imag)] for k in ks]
        coef_list.sort(key=lambda r: (abs(r[0]), r[0]))
        return res_pts, res_w, res_c, res_a, res_s, nn, recon_pts, maxerr, coef_list

    res_pts, res_w, res_c, res_a, res_s, N, recon_pts, max_err, coef = _fft_at(M)
    dev = _path_deviation_stats(orig_pt_arr, orig_t_arr, recon_pts, float(R))
    while dev["rms_px"] > _PATH_DEVIATION_TARGET_PX and M < m_cap:
        M = min(M * 2, m_cap)
        res_pts, res_w, res_c, res_a, res_s, N, recon_pts, max_err, coef = _fft_at(M)
        dev = _path_deviation_stats(orig_pt_arr, orig_t_arr, recon_pts, float(R))
        warn.add(
            "W_ONE_STROKE_N",
            f"路径偏差 RMS {dev['rms_px']:.4f}px 超目标 {_PATH_DEVIATION_TARGET_PX}px，"
            f"FFT 采样数提高到 M={M}",
        )
    if N < harmonics:
        warn.add("W_N_CLIPPED", f"谐波数被 Nyquist/上限钳到 N={N}（请求 {harmonics}，M={M}）")
    if dev["rms_px"] > _PATH_DEVIATION_TARGET_PX:
        warn.add(
            "W_ONE_STROKE_N",
            f"路径偏差 RMS {dev['rms_px']:.4f}px @R={R}，M={M} 已达上限仍超目标 {_PATH_DEVIATION_TARGET_PX}px",
        )

    # ---- 6) 属性曲线（宽度/颜色/alpha/笔形逐段）----
    t_grid = np.arange(M, dtype=np.float64) / float(M)
    nd = io_format.decimals_for_scalar(float(cfg.epsilon_px), int(cfg.target_resolution))
    tnd = _t_decimals(cfg, arc)
    width_tol = max(1e-7, 2.0 * (10.0 ** -nd))
    # 零宽区间 = 抬笔 + **仍走整块多边形填充**的 region_fill（brush 扫掠填充本身是宽笔画，不能置零）
    zero_spans: list[tuple[float, float]] = []
    for sp in spans:
        if sp["kind"] == "travel":
            zero_spans.append((sp["t0"], sp["t1"]))
        elif sp["kind"] == "fill":
            e = entries[int(sp.get("entry", -1))]
            if e.get("poly") is not None:
                zero_spans.append((sp["t0"], sp["t1"]))
    width_curve = _width_curve(t_grid, res_w, width_tol, zero_spans, ramp=2.0 * (10.0 ** -tnd))
    color_curve = _color_curve(t_grid, res_c)
    alpha_curve = _alpha_curve(t_grid, res_a, tol=0.01)
    shape_curve = _shape_curve(res_s, t_grid)

    # ---- 7) 填充侧表（显式多边形 + 全局 t 区间）----
    fills: list[list] = []
    for sp in spans:
        if sp["kind"] != "fill":
            continue
        e = entries[int(sp["entry"])]
        if e.get("poly") is None or e.get("fill_rgba") is None:
            continue
        rgb, alpha = e["fill_rgba"]
        poly = [[float(x), float(y)] for x, y in np.asarray(e["poly"], dtype=np.float64).reshape(-1, 2)]
        rec: list = [
            float(sp["t0"]),
            float(sp["t1"]),
            [
                int(round(float(np.clip(rgb[0], 0.0, 1.0)) * 255.0)),
                int(round(float(np.clip(rgb[1], 0.0, 1.0)) * 255.0)),
                int(round(float(np.clip(rgb[2], 0.0, 1.0)) * 255.0)),
                float(np.clip(alpha, 0.0, 1.0)),
            ],
            poly,
        ]
        cf = e.get("color_field")
        # 统一 6 元素：第 5 位 color_field（无则 None），第 6 位 holes（无洞写 None）。
        # 旧文档可能只有 4/5 元素，渲染端按"rec[4] 仅 dict 算 color_field、rec[5] 仅 list/tuple
        # 算 holes"兼容解析。
        rec.append(cf if isinstance(cf, dict) and cf.get("colors") else None)
        holes = e.get("fill_holes")
        rec.append(holes if holes else None)
        fills.append(rec)
    step_spans = [
        [float(sp["t0"]), float(sp["t1"]), str(sp["kind"])] for sp in spans
    ]

    # ---- 8) 组装 one_stroke 文档 ----
    bbox = [
        float(all_pts[:, 0].min()),
        float(all_pts[:, 1].min()),
        float(all_pts[:, 0].max()),
        float(all_pts[:, 1].max()),
    ]
    stroke = {
        "id": 0,
        "closed": True,
        "fill_role": "medial_branch",
        "repr": "fourier",
        "N": int(N),
        "arc_length": float(arc),
        "max_err": float(max_err),
        "path_deviation_px": float(dev["rms_px"]),
        "path_deviation_max_px": float(dev["max_px"]),
        "path_deviation_p995_px": float(dev["p995_px"]),
        "travel_length": float(travel),
        "layer": 0,
        "region_id": -1,
        "bbox": bbox,
        "coef": coef,
        "closure": {"kind": "none"},
        "brush": {"style": 0, "width": width_curve},
        "step_spans": step_spans,
    }
    src = doc.get("source") or {}
    source_fbs = {
        "path": str(src.get("path", "")),
        "sha256": str(src.get("sha256", "")),
        "stroke_count": int(len(strokes)),
    }
    warn_list = warn.to_list()
    one = io_format.new_document(
        mode=io_format.MODE_ONE_STROKE,
        source=dict(src, representation="one_stroke_single_fft"),
        image_class=dict(doc.get("image_class") or {}, note="单一傅里叶变换 / 一笔画表示"),
        canvas=dict(doc.get("canvas") or {}),
        render_contract=dict(doc.get("render_contract") or cfg.contract()),
        decomposition={
            "method": "one_stroke_stitch",
            "params": cfg.to_dict(),
            "region_count": len(doc.get("regions") or []),
        },
        base_color=list(doc.get("base_color") or [255, 255, 255, 255]),
        warnings=warn_list,
        accelerators=dict(doc.get("accelerators") or {}),
        regions=[],
    )
    one["source_fbs"] = source_fbs
    one["brush_styles"] = [
        {"color": color_curve, "alpha": alpha_curve, "shape": shape_curve, "jitter": 0.0}
    ]
    one["strokes"] = [stroke]
    one["one_stroke_fills"] = fills
    one["stats"] = {
        "stroke_count": 1,
        "coef_count": int(len(coef)),
        "fill_count": len(fills),
        "region_count": len(doc.get("regions") or []),
        "fft_passes": 1,
        "source_stroke_count": int(len(strokes)),
        "path_samples": int(M),
        "path_arc_length": float(arc),
        "travel_length": float(travel),
        "path_deviation_px": float(dev["rms_px"]),
        "path_deviation_max_px": float(dev["max_px"]),
        "path_deviation_p995_px": float(dev["p995_px"]),
        "max_err": float(max_err),
        "t_decimals": int(tnd),
    }
    io_format.finalize_document(one, image_sha256=str(src.get("sha256", "")))
    # finalize 会重算 stats（保留 fft_passes），把诊断字段补回去
    one["stats"].update(
        {
            "source_stroke_count": int(len(strokes)),
            "path_samples": int(M),
            "path_arc_length": float(arc),
            "travel_length": float(travel),
            "path_deviation_px": float(dev["rms_px"]),
            "path_deviation_max_px": float(dev["max_px"]),
            "path_deviation_p995_px": float(dev["p995_px"]),
            "max_err": float(max_err),
            "t_decimals": int(tnd),
        }
    )
    return one


# ------------------------------------------------------------------ build_path（视频/渲染共用）
def build_path(
    one_doc: dict,
    resolution: int,
    max_vertices: int | None = None,
    warn: WarningLog | None = None,
) -> dict:
    """一次 ifft 预计算一笔画路径采样（世界坐标）与逐点属性，供渲染/视频切片使用。"""
    if not isinstance(one_doc, dict):
        raise FBSError("E_INPUT_MODE", f"build_path 需要 dict 文档，收到 {type(one_doc).__name__}")
    if one_doc.get("mode") != "one_stroke":
        raise FBSError("E_INPUT_MODE", f"build_path 需要 one_stroke 文档（当前 mode={one_doc.get('mode')!r}）")
    strokes = one_doc.get("strokes") or []
    if len(strokes) != 1:
        raise FBSError("E_FBS_INVALID", f"one_stroke 文档必须恰好 1 条笔画，实际 {len(strokes)}")
    stroke = strokes[0]
    if not stroke.get("closed"):
        raise FBSError("E_FBS_INVALID", "one_stroke 笔画必须 closed=True")
    coef = stroke.get("coef") or []
    if not coef:
        raise FBSError("E_FBS_INVALID", "one_stroke 笔画缺少 coef")
    k, c = io_format.coef_to_arrays(coef)
    N = int(stroke.get("N", int(np.max(np.abs(k))) if len(k) else 0))
    arc = float(stroke.get("arc_length") or 0.0)
    R = int(resolution)
    if R < 1:
        raise FBSError("E_RENDER_RESOLUTION", f"resolution 非法: {resolution!r}")
    V = max(8, int(math.ceil(_SAMPLES_PER_PX * max(arc, 1e-9) * R)))
    if max_vertices is not None:
        V = max(8, min(V, int(max_vertices)))
    L = _next_pow2(max(V, 2 * N + 4, 8))
    freq = np.zeros(L, dtype=np.complex128)
    freq[np.mod(k, L)] = c
    dense = np.fft.ifft(freq) * L
    dense_pts = np.stack([dense.real, dense.imag], axis=1)
    t_dense = np.arange(L, dtype=np.float64) / float(L)
    # 按弧长均匀重采样（t_dense 与真实弧长参数在 one_stroke 中一致；这里同步插值参数）
    seg = np.linalg.norm(np.roll(dense_pts, -1, axis=0) - dense_pts, axis=1)
    total = float(seg.sum())
    if total <= 1e-15:
        raise FBSError("E_FBS_INVALID", "一笔画路径弧长为 0")
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    targets = np.linspace(0.0, total, V, endpoint=False)
    idx = np.clip(np.searchsorted(cum, targets, side="right") - 1, 0, L - 1)
    frac = (targets - cum[idx]) / np.maximum(seg[idx], 1e-15)
    pts = dense_pts[idx] + (dense_pts[(idx + 1) % L] - dense_pts[idx]) * frac[:, None]
    # 路径参数 t：用重建折线自身累计弧长归一化（与填充侧表 t 同一口径）
    step = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    total_closed = float(step.sum() + np.linalg.norm(pts[0] - pts[-1]))
    if total_closed <= 1e-15:
        t = np.arange(V, dtype=np.float64) / float(V)
        arc_cum = np.zeros(V, dtype=np.float64)
    else:
        arc_cum = np.concatenate([[0.0], np.cumsum(step)])[:V]
        t = arc_cum / total_closed
    style = _resolve_style(one_doc, stroke, warn)
    brush = stroke.get("brush") or {}
    widths = np.clip(np.asarray(eval_numeric(brush.get("width"), t, default=0.0), dtype=np.float64).reshape(-1), 0.0, None)
    rgb, color_a = eval_color(style["color"], t)
    alpha_curve = np.clip(np.asarray(eval_numeric(style["alpha"], t, default=1.0), dtype=np.float64).reshape(-1), 0.0, 1.0)
    alpha = np.clip(np.asarray(color_a, dtype=np.float64).reshape(-1) * alpha_curve, 0.0, 1.0)
    shapes = list(eval_shape(style["shape"], t))
    fills = list(one_doc.get("one_stroke_fills") or [])
    return {
        "points_world": np.asarray(pts, dtype=np.float64),
        "widths_world": widths,
        "radii_world": widths * 0.5,
        "rgb": np.clip(np.asarray(rgb, dtype=np.float64).reshape(-1, 3), 0.0, 1.0),
        "alpha": alpha,
        "shapes": shapes,
        "arc_cum": arc_cum,
        "t": t,
        "fills": fills,
        "arc_total": total_closed,
        "fft_size": int(L),
        "vertices": int(V),
    }
