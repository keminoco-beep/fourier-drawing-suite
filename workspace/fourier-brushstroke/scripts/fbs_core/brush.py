"""Stage3 笔刷属性曲线：element 的逐点采样 → 契约规定的 AttributeCurve。

接口契约见 CONTRACT.md §2/§5，格式规范见 references/fbs_format_spec.md §4。

设计决策（原因都写在代码旁）：
1. **属性是 t 上的连续函数，不是区间常量**（旧版 R4/R6 教训）：width/alpha 用
   constant/linear/spline/step/preset 中最简且保真的表达，constant 必须压成
   `{"interp":"constant","keys":[[0,v]]}`（体积纪律：共享表才能精确去重）。
2. **容差语义（与 Lead 确认过的契约修正）**：cfg.curve_tol 是**相对容差**。
   - width：tol_abs = curve_tol × max(width)。宽度是世界单位（典型 0.002~0.01，
     2~10px@R1024），若按绝对值 0.02 世界单位做 RDP，所有锥形/宽度变化都会被压成
     constant —— 旧版实测 0.02 是"归一化 profile 的相对容差"，即 2% 最大宽度
     （≈ 0.5px 抖动抑制），这也是旧版 A7 宽度误差的实测平衡点。
   - alpha：tol_abs = curve_tol（0..1 绝对量纲）。
   - color：按 8bit 色阶（±4/255 合并同色、>24/255 判硬跳变），不平均墨色。
3. **颜色不停留在"平均色"**：同色段合并时取离中位色最近的**真实样本**（medoid），
   渐变用 RDP 通道断点 + 线性 stops；硬跳变用相邻同 t 的一对 stop 表达
   （attribute.eval_color 已支持同 t 重复 stop，语义为左段→右段硬切换）。
4. **形状曲线**：v2 起 shape 是分类曲线（step keys，取值 round/soft/marker），
   修复旧版 D4"一笔画只能一个笔形"。显式 shapes 优先；None 时走 select_shape
   的数据可测规则（半透明墨→soft；均匀宽度+硬边→marker；其余→round）。
5. 三条属性曲线相互独立，全部按弧长归一化参数 t 求值（element["s"]）。
"""
from __future__ import annotations

import numpy as np

from . import attribute
from .shapelib import VALID_SHAPES
from .util import FBSError, WarningLog

# 颜色通道判据（8bit 0..255）：JPEG/抗锯齿噪声量级约 ±2~4，硬边界通常 >24
_COLOR_MERGE_TOL = 4
_COLOR_JUMP_TOL = 24
# 关键点数 ≤ 该值时 linear 已足够紧凑，不再尝试 spline
_LINEAR_KEY_PREF = 6
# 材质/硬度判据（旧版实测：硬边矢量图上只有 0.5%~3.2% 采样检出柔边/半透明，
# 所以 mean(alpha) ≥ 0.9 视为硬边；宽度 p95/p5 < 1.35 视为等宽）
_HARD_ALPHA = 0.9
_UNIFORM_WIDTH_RATIO = 1.35
_VALID_SHAPES = VALID_SHAPES  # 由 shapelib 注册（round/soft/marker/bristle/chalk/...）


# ------------------------------------------------------------------ 通用小工具
def _as_1d(values, name: str) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64).reshape(-1)
    if not np.all(np.isfinite(arr)):
        raise FBSError("E_CURVE", f"{name} 含非有限值", evidence={"count": int(len(arr))})
    return arr


def _collapse_samples(s: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """按 t 排序/去重（同 t 取均值），并补齐 0、1 端点，保证属性曲线覆盖整段弧长。"""
    s = np.asarray(s, dtype=np.float64).reshape(-1)
    vals = np.asarray(values, dtype=np.float64)
    n = len(s)
    if vals.shape[0] != n:
        raise FBSError("E_CURVE", "s 与属性采样长度不一致", evidence={"s": n, "values": int(vals.shape[0])})
    if n == 0:
        raise FBSError("E_CURVE", "空的属性采样")
    order = np.argsort(s, kind="stable")
    s = np.clip(s[order], 0.0, 1.0)
    vals = vals[order]
    # 同 t 合并（数值取均值；颜色在调用方先转 int，这里同样按均值兜底）
    keep = np.concatenate([[True], np.diff(s) > 1e-12])
    if not np.all(keep):
        starts = np.nonzero(keep)[0]
        counts = np.diff(np.concatenate([starts, [n]]))
        s_u = s[starts]
        vals_u = np.add.reduceat(vals, starts, axis=0) / counts.reshape((-1,) + (1,) * (vals.ndim - 1))
        s, vals = s_u, vals_u
    # 端点补齐（s 理论上是 [0,1]，缺失时用端点值延拓，避免属性曲线在 t 边界外推）
    if s[0] > 1e-12:
        s = np.concatenate([[0.0], s])
        vals = np.concatenate([vals[:1], vals], axis=0)
    if s[-1] < 1.0 - 1e-12:
        s = np.concatenate([s, [1.0]])
        vals = np.concatenate([vals, vals[-1:]], axis=0)
    return s, vals


def _simplify_vertical(s: np.ndarray, v: np.ndarray, tol: float) -> list[int]:
    """垂直距离 Douglas-Peucker：返回保留的关键点索引（首尾必留）。

    用**垂直偏差**而不是点-线段欧氏距离：契约判据是 |w_approx(t)-w(t)| ≤ tol，
    垂直距离恰好直接约束这个量。
    """
    n = len(s)
    if n <= 2:
        return list(range(n))
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack: list[tuple[int, int]] = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        span = float(s[b] - s[a])
        if span <= 0.0:
            continue
        frac = (s[a:b + 1] - s[a]) / span
        lin = v[a] + (v[b] - v[a]) * frac
        resid = np.abs(v[a:b + 1] - lin)
        j = int(np.argmax(resid))
        if resid[j] > tol:
            mid = a + j
            keep[mid] = True
            stack.append((a, mid))
            stack.append((mid, b))
    return np.nonzero(keep)[0].tolist()


def _curve_error(curve: dict, s: np.ndarray, v: np.ndarray) -> float:
    """用唯一求值口径 attribute.eval_numeric 复核拟合误差（世界单位）。"""
    est = attribute.eval_numeric(curve, s)
    return float(np.max(np.abs(est - v))) if len(v) else 0.0


def _constant_curve(value: float) -> dict:
    """常量数值曲线（体积纪律：常数必须压成 constant）。"""
    return {"interp": "constant", "keys": [[0.0, float(value)]]}


def _linear_curve(s: np.ndarray, v: np.ndarray, idx: list[int]) -> dict:
    keys = [[float(s[i]), float(v[i])] for i in idx]
    return {"interp": "linear", "keys": keys, "clamp": True}


def _spline_curve(s: np.ndarray, v: np.ndarray, idx: list[int]) -> dict:
    keys = [[float(s[i]), float(v[i])] for i in idx]
    return {"interp": "spline", "keys": keys, "clamp": True}


def _step_candidate(s: np.ndarray, v: np.ndarray, abs_tol: float) -> dict | None:
    """分段常量数据 → 显式 step 曲线（区间语义必须用 step 表达，不能用常量近似代替）。

    以相邻样本差 > abs_tol 为跳变切成若干"平台"，平台值取中位数，再用 attribute
    求值复核整体最大偏差；只有确属分段常量才接受。
    """
    diff = np.abs(np.diff(v))
    jumps = np.nonzero(diff > abs_tol)[0] + 1
    if len(jumps) == 0:
        return None
    bounds = [0] + jumps.tolist() + [len(v)]
    keys = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b <= a:
            continue
        keys.append([float(s[a]) if a > 0 else 0.0, float(np.median(v[a:b]))])
    if len(keys) < 2:
        return None
    # 合并相邻等值平台
    merged = [keys[0]]
    for t, val in keys[1:]:
        if abs(val - merged[-1][1]) <= abs_tol * 0.5:
            continue
        merged.append([t, val])
    curve = {"interp": "step", "keys": merged, "clamp": True}
    if _curve_error(curve, s, v) <= abs_tol:
        return curve
    return None


def _preset_candidates(s: np.ndarray, v: np.ndarray, abs_tol: float) -> list[dict]:
    """用 affine-in-g(t) 拟合 6 种命名预设，返回保真（≤ abs_tol）的候选曲线。

    preset 语义 f(t)=scale·(1−strength+strength·g(t)) = a + b·g(t)，
    a=scale·(1−strength), b=scale·strength ⇒ scale=a+b, strength=b/(a+b)。
    g(t) 直接由 attribute.eval_numeric 对原型曲线求值得到（唯一求值口径）。
    """
    out: list[dict] = []
    for name in ("taper-in", "taper-out", "taper-both", "ease-in", "ease-out", "sigmoid"):
        proto = {"interp": "preset", "preset": name, "scale": 1.0, "strength": 1.0}
        try:
            g = attribute.eval_numeric(proto, s)
        except FBSError:
            continue
        design = np.stack([np.ones_like(g), g], axis=1)
        try:
            coef, *_ = np.linalg.lstsq(design, v, rcond=None)
        except np.linalg.LinAlgError:
            continue
        a, b = float(coef[0]), float(coef[1])
        scale = a + b
        if not np.isfinite(scale) or scale <= 0.0:
            continue
        strength = b / scale
        if strength < -1e-9 or strength > 1.0 + 1e-9:
            continue
        curve = {
            "interp": "preset",
            "preset": name,
            "scale": float(scale),
            "strength": float(min(max(strength, 0.0), 1.0)),
        }
        if _curve_error(curve, s, v) <= abs_tol:
            out.append(curve)
    return out


def _fit_numeric(s: np.ndarray, v: np.ndarray, abs_tol: float) -> dict:
    """把沿弧长的逐点数值 profile 拟合成"最简且保真"的 AttributeCurve。

    候选：constant / step / preset / linear / spline（fourier 语义见格式规范，
    但非周期 profile 用傅里叶展开会端点振铃，故构建器不产出 fourier）。
    选择规则：保真（max|eval−v| ≤ abs_tol）前提下序列化体积最小。
    """
    s, v = _collapse_samples(s, v)
    lo, hi = float(v.min()), float(v.max())
    # 1) 常量：minimax 中点使最大偏差最小
    if hi - lo <= 2.0 * abs_tol:
        return _constant_curve(0.5 * (lo + hi))

    abs_tol = float(max(abs_tol, 0.0))
    candidates: list[tuple[float, int, dict]] = []  # (体积代价, 语义优先级, 曲线)

    # 2) step：显式区间语义
    step_curve = _step_candidate(s, v, abs_tol)
    if step_curve is not None:
        candidates.append((2.0 * len(step_curve["keys"]), 1, step_curve))

    # 3) preset：常见笔锋的最紧凑表达
    for curve in _preset_candidates(s, v, abs_tol):
        candidates.append((3.0, 2, curve))

    # 4) linear：RDP 垂直容差的关键点
    idx = _simplify_vertical(s, v, abs_tol)
    linear = _linear_curve(s, v, idx)
    candidates.append((2.0 * len(idx), 3, linear))

    # 5) spline：关键点较多时尝试减半关键点（过点三次样条更平滑，便于压缩体积）
    if len(idx) > _LINEAR_KEY_PREF:
        best_spline: dict | None = None
        m = len(idx)
        cur = m
        while cur > 3:
            cur = max(3, cur // 2)
            sel = np.unique(np.round(np.linspace(0, m - 1, cur)).astype(int))
            spline = _spline_curve(s, v, [idx[i] for i in sel])
            if _curve_error(spline, s, v) <= abs_tol:
                best_spline = spline
            else:
                break
        if best_spline is not None:
            candidates.append((2.0 * len(best_spline["keys"]), 4, best_spline))

    if not candidates:  # 理论不可达：linear 候选一定保真
        return linear
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


# ------------------------------------------------------------------ 宽度曲线
def build_width_curve(s, widths, tol: float = 0.02) -> dict:
    """宽度曲线（width 是世界单位下的笔画**全宽**，不是半径）。

    容差是相对语义：tol_abs = tol × max(width)（与 Lead 确认的契约修正一致，
    旧版实测 0.02 是归一化 profile 的相对容差，即 2% 最大宽度）。
    """
    sv = _as_1d(s, "s")
    w = _as_1d(widths, "widths")
    if len(w) == 0:
        raise FBSError("E_CURVE", "widths 为空")
    scale_ref = float(np.max(np.abs(w)))
    abs_tol = float(tol) * scale_ref if scale_ref > 0.0 else float(tol)
    return _fit_numeric(sv, np.maximum(w, 0.0), abs_tol)


# ------------------------------------------------------------------ alpha 曲线
def build_alpha_curve(s, alpha, tol: float = 0.02) -> dict:
    """透明度曲线（0..1 绝对量纲，独立于 width/color）。"""
    sv = _as_1d(s, "s")
    a = _as_1d(alpha, "alpha")
    if len(a) == 0:
        raise FBSError("E_CURVE", "alpha 为空")
    return _fit_numeric(sv, np.clip(a, 0.0, 1.0), float(tol))


# ------------------------------------------------------------------ 颜色曲线
def _rgb8(rgb: np.ndarray) -> np.ndarray:
    """0..1 float RGB → 0..255 int（唯一量化点；不在这里做平均）。"""
    return np.clip(np.rint(np.asarray(rgb, dtype=np.float64) * 255.0), 0, 255).astype(np.int64)


def _medoid_sample_index(rgb8: np.ndarray) -> int:
    """离逐通道中位色最近的**真实样本**下标（medoid，不做通道平均，避免墨色被拉淡）。"""
    if len(rgb8) == 1:
        return 0
    med = np.median(rgb8.astype(np.float64), axis=0)
    dist = np.abs(rgb8.astype(np.float64) - med).sum(axis=1)
    return int(np.argmin(dist))


def _simplify_color_stops(stops: list[dict], tol: float) -> list[dict]:
    """删除冗余 stop（局部"帽函数"判据），保留硬跳变的同 t 对。

    对三元组 (prev, cur, next)：若 cur 的颜色与 prev→next 线性插值在 t_cur 处的
    值相差 ≤ 合并容差（alpha 同理），删掉 cur 对求值的最大影响就是一个高度 =
    该色差的帽函数 —— 因此是无损近似，且完全不需要调用求值器（热路径）。
    next 与 prev 同 t（硬跳变对）时跳过：那正是"左段颜色→右段颜色"的语义锚点。
    """
    if len(stops) <= 2:
        return stops
    work = list(stops)
    for _ in range(4):  # 删除后邻居变化，最多几轮即稳定（每轮 O(k)）
        changed = False
        i = 1
        while i < len(work) - 1:
            prev, cur, nxt = work[i - 1], work[i], work[i + 1]
            span = float(nxt["t"]) - float(prev["t"])
            if span <= 1e-12:
                i += 1
                continue
            frac = (float(cur["t"]) - float(prev["t"])) / span
            prev_rgb = np.asarray(prev["rgb"], dtype=np.float64)
            next_rgb = np.asarray(nxt["rgb"], dtype=np.float64)
            lin_rgb = prev_rgb + (next_rgb - prev_rgb) * frac
            cur_rgb = np.asarray(cur["rgb"], dtype=np.float64)
            lin_a = float(prev["a"]) + (float(nxt["a"]) - float(prev["a"])) * frac
            if float(np.max(np.abs(cur_rgb - lin_rgb))) <= _COLOR_MERGE_TOL \
                    and abs(float(cur["a"]) - lin_a) <= float(tol):
                work.pop(i)
                changed = True
            else:
                i += 1
        if not changed:
            break
    return work


def build_color_curve(s, rgb, alpha, tol: float = 0.02) -> dict:
    """颜色 + 逐 stop alpha 曲线（颜色渐变是一等公民；不得平均墨色）。

    - 同色连续段合并：逐通道 RDP（±4/255 容差）的断点并集 + alpha 断点；
    - 硬跳变（相邻样本通道差 > 24/255）：在跳变时刻放一对同 t 的 stop
      （attribute.eval_color 的线性插值语义 = 左段颜色 → 右段颜色硬切换）；
    - 每个 stop 的颜色取**最近的真实样本**（medoid 语义），不做通道平均。
    """
    sv = _as_1d(s, "s")
    c = np.asarray(rgb, dtype=np.float64)
    if c.ndim != 2 or c.shape[1] != 3:
        raise FBSError("E_CURVE", "rgb 形状必须是 (M,3)", evidence={"shape": list(c.shape)})
    a = _as_1d(alpha, "alpha")
    if len(c) != len(sv) or len(a) != len(sv):
        raise FBSError("E_CURVE", "s/rgb/alpha 长度不一致",
                       evidence={"s": len(sv), "rgb": len(c), "alpha": len(a)})
    if len(sv) == 0:
        raise FBSError("E_CURVE", "空颜色采样")
    sv, idx = np.unique(np.clip(sv, 0.0, 1.0), return_index=True)
    rgb8 = _rgb8(c[idx])
    a = np.clip(a[idx], 0.0, 1.0)
    # 补端点（颜色的端点延拓：取端点样本本身，不插值）
    if sv[0] > 1e-12:
        sv = np.concatenate([[0.0], sv])
        rgb8 = np.concatenate([rgb8[:1], rgb8], axis=0)
        a = np.concatenate([[a[0]], a])
    if sv[-1] < 1.0 - 1e-12:
        sv = np.concatenate([sv, [1.0]])
        rgb8 = np.concatenate([rgb8, rgb8[-1:]], axis=0)
        a = np.concatenate([a, [a[-1]]])

    span = rgb8.max(axis=0) - rgb8.min(axis=0)
    alpha_range = float(a.max() - a.min())
    if float(span.max()) <= _COLOR_MERGE_TOL and alpha_range <= float(tol):
        # 常数颜色：取 medoid 真实样本，绝不取均值（平均会把墨色拉淡）
        mi = _medoid_sample_index(rgb8)
        return {
            "interp": "constant",
            "stops": [{"t": 0.0, "rgb": [int(v) for v in rgb8[mi]],
                       "a": float(np.clip(np.median(a), 0.0, 1.0))}],
        }

    # 断点：颜色通道 RDP 并集 + alpha RDP
    times: set[float] = {0.0, 1.0}
    for ch in range(3):
        v = rgb8[:, ch].astype(np.float64)
        if float(v.max() - v.min()) <= _COLOR_MERGE_TOL:
            continue
        for i in _simplify_vertical(sv, v, float(_COLOR_MERGE_TOL)):
            times.add(float(sv[i]))
    if alpha_range > 0.0:
        for i in _simplify_vertical(sv, a, float(tol)):
            times.add(float(sv[i]))

    # 硬跳变时刻（左段 → 右段）
    jump_times: set[float] = set()
    dmax = np.max(np.abs(np.diff(rgb8, axis=0)), axis=1) if len(rgb8) > 1 else np.zeros(0)
    for i in np.nonzero(dmax > _COLOR_JUMP_TOL)[0]:
        jump_times.add(float(sv[i + 1]))

    def _nearest(t: float) -> int:
        i = int(np.searchsorted(sv, t, side="left"))
        i = min(max(i, 0), len(sv) - 1)
        if i > 0 and abs(sv[i - 1] - t) <= abs(sv[i] - t):
            return i - 1
        return i

    stops: list[dict] = []
    for t in sorted(times | jump_times):
        if t in jump_times:
            right = int(np.searchsorted(sv, t, side="left"))
            right = min(max(right, 1), len(sv) - 1)
            left = right - 1
            stops.append({"t": float(t), "rgb": [int(v) for v in rgb8[left]],
                          "a": float(a[left])})
            stops.append({"t": float(t), "rgb": [int(v) for v in rgb8[right]],
                          "a": float(a[right])})
        else:
            i = _nearest(t)
            stops.append({"t": float(t), "rgb": [int(v) for v in rgb8[i]], "a": float(a[i])})

    stops = _simplify_color_stops(stops, float(tol))
    if len(stops) <= 1:
        s0 = stops[0] if stops else {"rgb": [0, 0, 0], "a": 1.0}
        return {"interp": "constant",
                "stops": [{"t": 0.0, "rgb": list(s0["rgb"]), "a": float(s0["a"])}]}
    return {"interp": "linear", "stops": stops}


# ------------------------------------------------------------------ 形状曲线
def build_shape_curve(s, shapes) -> dict:
    """形状为分类 step 曲线（v2 修复旧版 D4：一笔画可沿 t 变笔形）。

    shapes: None / 单值 / 长度 M 的列表；None 返回默认 round。
    """
    if shapes is None:
        return {"interp": "step", "keys": [[0.0, "round"]]}
    if isinstance(shapes, str):
        values = [shapes]
    else:
        values = [str(x) for x in list(shapes)]
    if len(values) == 0:
        return {"interp": "step", "keys": [[0.0, "round"]]}
    for v in values:
        if v not in _VALID_SHAPES:
            raise FBSError("E_CURVE", f"未知笔形: {v}", suggestions=[f"可用: {list(_VALID_SHAPES)}"])
    if len(values) == 1:
        return {"interp": "step", "keys": [[0.0, values[0]]]}
    sv = _as_1d(s, "s")
    if len(sv) != len(values):
        raise FBSError("E_CURVE", "shapes 长度必须为 1 或与 s 一致",
                       evidence={"shapes": len(values), "s": len(sv)})
    keys: list[list] = [[0.0, values[0]]]
    for i in range(1, len(values)):
        if values[i] != values[i - 1]:
            keys.append([float(np.clip(sv[i], 0.0, 1.0)), values[i]])
    # 同 t 相邻键合并（s 可能非严格递增）
    merged: list[list] = [keys[0]]
    for t, val in keys[1:]:
        if t <= merged[-1][0] + 1e-12:
            merged[-1][1] = val
        else:
            merged.append([t, val])
    return {"interp": "step", "keys": merged}


def select_shape(element: dict, cfg) -> str:
    """自动选择笔形（数据可测规则；cfg.shape_mode 非 auto 时强制）。

    判据（旧版实测结论的可测代理）：
    - region_fill / 零宽度 → round（填充不用笔刷几何）；
    - mean(alpha) < 0.9 → soft（半透明墨 = 柔边笔刷的证据；硬边矢量图基本检不出）；
    - 硬边且宽度均匀（p95/p5 < 1.35×）→ marker（扁平笔头 + 恒定宽度）；
    - 其余（硬边但宽度变化）→ round。
    """
    mode = str(getattr(cfg, "shape_mode", "auto"))
    if mode in _VALID_SHAPES:
        return mode
    widths = np.asarray(element.get("widths", []), dtype=np.float64).reshape(-1)
    alphas = np.asarray(element.get("alphas", []), dtype=np.float64).reshape(-1)
    if str(element.get("fill_role", "medial_branch")) == "region_fill":
        return "round"
    positive = widths[widths > 0.0]
    if positive.size == 0:
        return "round"
    hard = True if alphas.size == 0 else float(np.mean(np.clip(alphas, 0.0, 1.0))) >= _HARD_ALPHA
    if not hard:
        return "soft"
    p_lo, p_hi = np.percentile(positive, [5.0, 95.0])
    ratio = float(p_hi / max(float(p_lo), 1e-300))
    if ratio < _UNIFORM_WIDTH_RATIO:
        return "marker"
    return "round"


# ------------------------------------------------------------------ 组装入口
def _recover_lengths(element: dict, warn: WarningLog | None) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, object]:
    """校验/恢复 element 的等长采样数组（防御 Stage1 接口异常，不静默）。"""
    s = np.asarray(element.get("s", []), dtype=np.float64).reshape(-1)
    widths = np.asarray(element.get("widths", []), dtype=np.float64).reshape(-1)
    colors = np.asarray(element.get("colors", []), dtype=np.float64)
    alphas = np.asarray(element.get("alphas", []), dtype=np.float64).reshape(-1)
    m = len(s)
    if m == 0:
        raise FBSError("E_CURVE", "element.s 为空，无法生成笔刷属性")
    if colors.ndim != 2 or colors.shape[1] != 3:
        try:
            colors = colors.reshape(-1, 3)
        except ValueError:
            colors = np.zeros((m, 3), dtype=np.float64)
        if warn is not None:
            warn.add("W_BRUSH_COLOR_SHAPE",
                     f"element.colors 形状非法（应为 (M,3)），已按 {list(colors.shape)} 恢复", severity="degrade")
    shapes = element.get("shapes")

    def _fix(name: str, arr: np.ndarray, fill: float) -> np.ndarray:
        if len(arr) == m:
            return arr
        if warn is not None:
            warn.add("W_BRUSH_LENGTH_MISMATCH",
                     f"element.{name} 长度 {len(arr)} 与 s 长度 {m} 不一致，按 s 网格线性重采样恢复")
        if len(arr) == 0:
            return np.full(m, fill, dtype=np.float64)
        grid = np.linspace(0.0, 1.0, len(arr))
        return np.interp(np.linspace(0.0, 1.0, m), grid, arr)

    widths = _fix("widths", widths, 0.0)
    alphas = _fix("alphas", alphas, 1.0)
    if len(colors) != m:
        if warn is not None and colors.shape[0] != 0:
            warn.add("W_BRUSH_LENGTH_MISMATCH", "element.colors 长度与 s 不一致，已重采样恢复")
        if colors.shape[0] == 0:
            colors = np.zeros((m, 3), dtype=np.float64)
        else:
            grid = np.linspace(0.0, 1.0, colors.shape[0])
            tgt = np.linspace(0.0, 1.0, m)
            colors = np.stack([np.interp(tgt, grid, colors[:, ch]) for ch in range(3)], axis=1)
    return s, widths, alphas, colors, shapes


def build_brush(element: dict, contract: dict, cfg, warn: WarningLog | None = None) -> tuple[dict, dict]:
    """Stage3 主入口：element → (style, brush)。

    style = {"color": 颜色曲线, "alpha": 数值曲线, "shape": 分类曲线, "jitter": float}
    brush = {"style": 0 占位（document.py 建表后回填）, "width": 数值曲线}
    三条属性曲线相互独立；常数属性压成 constant；jitter 默认 cfg.brush_jitter。
    """
    s, widths, alphas, colors, shapes = _recover_lengths(element, warn)
    tol = float(getattr(cfg, "curve_tol", 0.02))
    width_curve = build_width_curve(s, widths, tol)
    alpha_curve = build_alpha_curve(s, alphas, tol)
    grad = element.get("gradient")
    if isinstance(grad, dict) and grad.get("start") is not None and grad.get("end") is not None:
        # First-class two-endpoint smooth gradient: do not run the jump/RDP
        # detector, which would misread a large endpoint delta as a hard cut.
        c0 = np.clip(np.asarray(grad["start"], dtype=np.float64).reshape(3), 0.0, 1.0)
        c1 = np.clip(np.asarray(grad["end"], dtype=np.float64).reshape(3), 0.0, 1.0)
        r0 = [int(round(v * 255.0)) for v in c0]
        r1 = [int(round(v * 255.0)) for v in c1]
        a = float(np.clip(np.mean(alphas), 0.0, 1.0))
        color_curve = {
            "interp": "linear",
            "stops": [
                {"t": 0.0, "rgb": r0, "a": a},
                {"t": 1.0, "rgb": r1, "a": a},
            ],
        }
    else:
        color_curve = build_color_curve(s, colors, alphas, tol)
    if shapes is None:
        shape_value = select_shape(element, cfg)
        shape_curve = {"interp": "step", "keys": [[0.0, shape_value]]}
    else:
        shape_curve = build_shape_curve(s, shapes)
    style = {
        "color": color_curve,
        "alpha": alpha_curve,
        "shape": shape_curve,
        "jitter": float(getattr(cfg, "brush_jitter", 0.0)),
    }
    brush = {"style": 0, "width": width_curve}
    return style, brush
