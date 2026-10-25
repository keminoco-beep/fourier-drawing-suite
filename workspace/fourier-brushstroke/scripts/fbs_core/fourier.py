"""Stage2 傅里叶参数化：任意折线 → 复平面傅里叶系数 + 误差驱动自适应 N。

本模块只负责"几何"，笔刷属性在 brush.py（Stage3）。接口契约见 CONTRACT.md §4。

关键决策（均为旧版实测结论或本轮实测，不是自创）：
1. **开曲线先做 C1/C2 闭环再周期 FFT**。傅里叶级数天然周期，开曲线直接展开会在首尾
   之间出现跳变（重建出多余斜线）；镜像延拓只有 C0，折返处切向反转形成尖点、
   系数按 O(1/k²) 慢收敛。闭包段不参与绘制，只用于把 drawable 段周期化。
2. **闭环用"五次 Hermite + 中部凸起项"**（本轮实测）：
   - 五次 Hermite 匹配两端位置/切向，并把端点曲率取 0 —— 开曲线折线在接缝处的
     相邻段是直线（几何曲率 0），匹配 0 才能得到 C2 接缝，端点附近的截断振铃
     从 O(1/N) 降到 O(1/N²) 量级（实测近直线笔画 N：349 → 20）；
   - 两端切向平行时五次解退化为共线往返（180° 折返尖点），叠加一个在两端 0~2 阶
     导数全为 0 的凸起项 `64·s³(1-s)³·h`，得到真正的 2D 闭环而不破坏端点约束。
3. **闭环尺度按曲线自身相对量取**（弦长/弧长），禁止世界坐标下的绝对像素下限
   —— 世界坐标长边恒为 1，写死绝对下限在大小图上行为不一致。
4. **N 由渲染契约反推**：thr_world = epsilon_px / target_resolution；在 drawable 参数
   区间用密集采样点算截断重建的 max 偏差，粗扫（倍增）+ 二分取"满足契约的最小 N"。
   这避免了"每档分辨率必须 2×"这类单标量判据（旧版 A3 因此长期假失败）。
5. **Nyquist 钳制 N ≤ M/2-2**，越界绝不回绕重复取系数（旧版真 bug：N=4096 而 M=2048
   时 z[k % M] 回绕重复，PSNR 从 30.7 崩到 5.5）。
6. 候选 N 的评估用**零填充 ifft**：一次 ifft 得到 drawable 区间上任意密度的重建，
   成本与 N 无关，1000+ 曲线 @R1024 的 N 搜索可控（见 CONTRACT §12 性能预算）。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .util import FBSError, WarningLog, polyline_arclength, resample_polyline, segment_lengths

# energy 模式保留的频谱能量占比（设计文档 v2.1 §4.1：energy = 能量 99.5%）
_ENERGY_RATIO = 0.995
# 开曲线闭环：控制点/凸起尺度相对曲线自身尺度（弦长优先；弦长相对弧长过小时用弧长）的比例
_CHORD_ARC_FLOOR = 0.25
_BULGE_RATIO = 0.35
# 桥接多项式自适应采样的初始细化段数与数值安全上限（正常曲线 6~9 轮即收敛；
# 达到上限会体现为弦高误差变大，不会静默丢几何）
_BRIDGE_INIT_SEGMENTS = 8
_BRIDGE_MAX_SEGMENTS = 1 << 12
# 评估网格：drawable 区间目标采样数 = 2×resample_points；总长度最多 4×resample_points
# （两者都是 resample_points 的相对倍数，由数据/配置导出，非绝对像素上限；上限保证
# 1000 曲线 @R1024 的 N 搜索满足 CONTRACT §12 的 60s 预算）
_EVAL_DRAWABLE_FACTOR = 2.0
_EVAL_LENGTH_FACTOR = 4.0
# eval_coef 相位矩阵分块的内存预算（按复数 16 byte 估）；热路径保护，与数据无关
_PHASE_BLOCK_BYTES = 64 << 20
# 五次 Hermite 的约束矩阵（参数 s∈[0,1]：位置 / 一阶 / 二阶 × 两端）
_QUINTIC_MATRIX = np.array(
    [
        [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 2.0, 0.0, 0.0, 0.0],
        [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
        [0.0, 0.0, 2.0, 6.0, 12.0, 20.0],
    ],
    dtype=np.float64,
)


# ------------------------------------------------------------------ 基础小工具
def _next_pow2(value: int) -> int:
    """不小于 value 的最小 2 的幂（value ≥ 1）。"""
    p = 1
    while p < int(value):
        p <<= 1
    return p


def _resample_size(cfg: Any) -> int:
    """M = 弧长均匀重采样点数：取 cfg.resample_points 向上对齐到 2 的幂（FFT 友好）。"""
    m = int(cfg.resample_points)
    if m < 16:
        raise FBSError("E_CONFIG", "resample_points 必须 ≥ 16", evidence={"value": m})
    return _next_pow2(m)


def _dedupe_points(points: np.ndarray, closed: bool) -> np.ndarray:
    """去掉相邻重复点（零长度段）；闭合曲线首尾重合时去掉重复的尾点。"""
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(p) <= 1:
        return p
    scale = max(float(polyline_arclength(p, closed=closed)), 1e-300)
    eps = scale * 1e-12
    keep = np.ones(len(p), dtype=bool)
    keep[1:] = np.linalg.norm(np.diff(p, axis=0), axis=1) > eps
    out = p[keep]
    if closed and len(out) > 1 and np.linalg.norm(out[0] - out[-1]) <= eps:
        out = out[:-1]
    return out


def _endpoint_tangent(points: np.ndarray, start: bool) -> np.ndarray:
    """端点单位切向：必须与相邻段方向严格一致，否则闭环接缝会出现 C0 角点。

    相邻点重合时沿序列找第一个非重合点做退化回退（Stage1 理论上已去掉重复点，
    这里是为外部调用方兜底）。
    """
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    n = len(p)
    if n < 2:
        raise FBSError("E_CURVE", "点数不足，无法求端点切向", evidence={"count": n})
    if start:
        for i in range(1, n):
            v = p[i] - p[0]
            nv = float(np.linalg.norm(v))
            if nv > 0.0:
                return v / nv
    else:
        for i in range(n - 2, -1, -1):
            v = p[-1] - p[i]
            nv = float(np.linalg.norm(v))
            if nv > 0.0:
                return v / nv
    raise FBSError("E_CURVE", "端点切向退化（全等点）")


def _poly_eval(coef: np.ndarray, s) -> np.ndarray:
    """按系数 [c0,c1,...] 求值 z(s)=Σ c_i·s^i（Horner，向量化）。"""
    ss = np.atleast_1d(np.asarray(s, dtype=np.float64))
    out = np.zeros((len(ss), 2), dtype=np.float64)
    for c in coef[::-1]:
        out = out * ss[:, None] + c
    return out


def _bridge_coef(p_from: np.ndarray, p_to: np.ndarray,
                 t_from: np.ndarray, t_to: np.ndarray,
                 scale: float) -> np.ndarray:
    """开曲线闭环桥接多项式系数 (7,2)（次数 6，参数 s∈[0,1]）。

    五次 Hermite 匹配两端：位置 / 单位切向 / 曲率=0（C2 接缝）；
    再叠加在两端的 0、一阶、二阶导数均为 0 的凸起项 64·s³(1-s)³·h，
    h 垂直于弦方向、长度 = _BULGE_RATIO·scale —— 保证两端切向平行（直线笔画）
    时不是退化共线往返，而是真正的 2D 闭环。
    """
    rhs = np.zeros((6, 2), dtype=np.float64)
    rhs[0] = p_from
    rhs[1] = t_from
    rhs[3] = p_to
    rhs[4] = t_to
    coef = np.zeros((7, 2), dtype=np.float64)
    coef[:6] = np.linalg.solve(_QUINTIC_MATRIX, rhs)

    chord = np.asarray(p_to, dtype=np.float64) - np.asarray(p_from, dtype=np.float64)
    chord_len = float(np.linalg.norm(chord))
    if chord_len > 0.0:
        normal = np.array([-chord[1], chord[0]], dtype=np.float64) / chord_len
    else:  # 端点几乎重合：用起点切向的垂线定凸起方向
        normal = np.array([-t_from[1], t_from[0]], dtype=np.float64)
    bulge = normal * (_BULGE_RATIO * float(scale))
    # 64·s³(1-s)³ = 64s³ - 192s⁴ + 192s⁵ - 64s⁶
    coef[3] += 64.0 * bulge
    coef[4] -= 192.0 * bulge
    coef[5] += 192.0 * bulge
    coef[6] -= 64.0 * bulge
    return coef


def _sample_bridge(coef: np.ndarray, tol: float) -> np.ndarray:
    """自适应采样桥接多项式，返回首尾含端点的折线（弦高误差 ≤ tol）。

    判据：每段 4 个细分点中 3 个内部点到该段端点弦的最大距离（向量化整段检查）。
    不用固定段数：控制点尺度是相对量，绝对段数在大小曲线上行为不一致。
    """
    tol = max(float(tol), 0.0)
    segments = _BRIDGE_INIT_SEGMENTS
    while True:
        count = 4 * segments + 1
        ss = np.linspace(0.0, 1.0, count)
        pts = _poly_eval(coef, ss)
        idx = np.arange(1, count - 1)
        seg = idx // 4
        frac = (idx - 4 * seg) / 4.0
        lin = pts[4 * seg] * (1.0 - frac)[:, None] + pts[4 * seg + 4] * frac[:, None]
        err = float(np.max(np.linalg.norm(pts[idx] - lin, axis=1))) if len(idx) else 0.0
        if err <= tol or segments >= _BRIDGE_MAX_SEGMENTS:
            return pts
        segments *= 2


def _hermite_closed(points: np.ndarray, arc: float, bridge_tol: float) -> tuple[np.ndarray, float]:
    """开曲线 → C1（几何上 C2）闭环折线；返回 (闭环点列（首尾不重复）, t_end)。

    桥接段从终点沿终点切向离开、绕回起点并沿起点切向到达，两端曲率匹配 0；
    尺度 = 弦长（弦长相对弧长过小时退化为 0.25×弧长），不写死绝对像素下限。
    """
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    chord = float(np.linalg.norm(p[-1] - p[0]))
    scale = chord if chord >= _CHORD_ARC_FLOOR * arc else _CHORD_ARC_FLOOR * arc
    t_start = _endpoint_tangent(p, start=True)
    t_end_dir = _endpoint_tangent(p, start=False)
    coef = _bridge_coef(p[-1], p[0], t_end_dir, t_start, scale)
    bridge = _sample_bridge(coef, bridge_tol)
    # 桥接段首尾就是 p[-1] / p[0]，闭环折线里去掉重复端点（闭合段由 closed=True 补上）
    closed_pts = np.vstack([p, bridge[1:-1]])
    forward = float(polyline_arclength(p, closed=False))
    total = float(polyline_arclength(closed_pts, closed=True))
    if total <= 0.0:
        raise FBSError("E_CURVE", "闭环弧长退化", evidence={"arc": arc})
    t_end = forward / total
    if not (0.0 < t_end < 1.0):
        raise FBSError("E_CURVE", "闭环映射比例退化", evidence={"t_end": t_end, "arc": arc})
    return closed_pts, float(t_end)


def _sample_polyline(points: np.ndarray, t, closed: bool) -> np.ndarray:
    """按弧长归一化参数 t∈[0,1] 在折线上取点（向量化，t 任意 shape）。"""
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    tt = np.asarray(t, dtype=np.float64).reshape(-1)
    if len(p) == 0:
        raise FBSError("E_CURVE", "空折线无法采样")
    if len(p) == 1:
        return np.repeat(p, len(tt), axis=0)
    lens = segment_lengths(p, closed=closed)
    total = float(lens.sum())
    if total <= 0.0:
        return np.repeat(p[:1], len(tt), axis=0)
    cum = np.concatenate([[0.0], np.cumsum(lens)])
    s = np.clip(tt, 0.0, 1.0) * total
    seg = np.searchsorted(cum, s, side="right") - 1
    seg = np.clip(seg, 0, len(lens) - 1)
    local = (s - cum[seg]) / np.maximum(lens[seg], 1e-300)
    a = p[seg]
    if closed:
        b = p[(seg + 1) % len(p)]
    else:
        b = p[np.minimum(seg + 1, len(p) - 1)]
    return a + (b - a) * local[:, None]


# ------------------------------------------------------------------ 频谱与误差评估
def _spectrum(resampled: np.ndarray, m: int) -> np.ndarray:
    """Z = fft(z)/M，z = x + i·y（单位：世界坐标）。"""
    arr = np.asarray(resampled, dtype=np.float64)
    z = arr[:, 0] + 1j * arr[:, 1]
    return np.fft.fft(z) / float(m)


def _eval_length(m: int, t_end: float) -> int:
    """零填充 ifft 的网格长度 L：drawable 目标 2M 个评估点，上限 4M（性能预算）。"""
    target = _EVAL_DRAWABLE_FACTOR * m / max(float(t_end), 1e-9)
    limit = int(_EVAL_LENGTH_FACTOR * m)
    length = 2 * m
    while length < target and length < limit:
        length *= 2
    return int(length)


class _ErrorEvaluator:
    """在 drawable 参数区间上评估任意截断 N 的重建最大偏差（世界单位）。

    一次准备、多次候选：参考点只在初始化时采样一次；每个候选 N 只做一次零填充 ifft。
    """

    def __init__(self, spec: np.ndarray, m: int, t_end: float, closed: bool,
                 reference_polyline: np.ndarray) -> None:
        self.M = int(m)
        self.t_end = float(t_end)
        self.closed = bool(closed)
        self.L = _eval_length(self.M, self.t_end)
        ref = np.asarray(reference_polyline, dtype=np.float64).reshape(-1, 2)
        self.ref_polyline = ref

        # 全频谱（长度 M）→ 长度 L 的零填充频谱（L ≥ 2M，不会重叠）
        k_m = np.fft.fftfreq(self.M, d=1.0 / self.M).astype(np.int64)
        self._k_m_arr = k_m
        self._abs_k_m_arr = np.abs(k_m)
        self._c_m = np.asarray(spec, dtype=np.complex128)
        self._spec_l = np.zeros(self.L, dtype=np.complex128)
        self._spec_l[k_m % self.L] = self._c_m
        self._abs_k_l = np.abs(np.fft.fftfreq(self.L, d=1.0 / self.L))

        # 按 |k| 升序的系数（用于精确端点求值）：|k| ≤ n 恰好是最前面 2n+1 个
        order = np.argsort(self._abs_k_m_arr, kind="stable")
        self._k_sorted = k_m[order].astype(np.float64)
        self._c_sorted = self._c_m[order]

        if self.closed:
            self._idx = np.arange(self.L, dtype=np.int64)
            t_draw = self._idx / float(self.L)
            ref_pts = _sample_polyline(ref, t_draw, closed=True)
            self._ref_c = ref_pts[:, 0] + 1j * ref_pts[:, 1]
            self._end_ref_c: complex | None = None
        else:
            n_draw = int(math.floor(self.t_end * self.L))
            self._idx = np.arange(n_draw + 1, dtype=np.int64)
            t_closed = self._idx / float(self.L)
            t_draw = np.clip(t_closed / self.t_end, 0.0, 1.0)
            ref_pts = _sample_polyline(ref, t_draw, closed=False)
            self._ref_c = ref_pts[:, 0] + 1j * ref_pts[:, 1]
            # t_closed = t_end 通常不落在 L 网格上，端点单独精确求值
            self._end_ref_c = complex(float(ref[-1][0]), float(ref[-1][1]))

    def _eval_at(self, n: int, t: float) -> complex:
        """截断到 |k| ≤ n 后在单个 t 处求值（用于开曲线端点）。"""
        count = min(2 * int(n) + 1, len(self._k_sorted))
        phase = np.exp(2j * np.pi * self._k_sorted[:count] * float(t))
        return complex(np.dot(self._c_sorted[:count], phase))

    def error(self, n: int) -> float:
        """截断到 |k| ≤ n 时 drawable 区间上的最大重建偏差（世界单位）。"""
        n = int(n)
        spec = self._spec_l.copy()
        spec[self._abs_k_l > n] = 0.0
        rec = np.fft.ifft(spec) * float(self.L)
        err = float(np.max(np.abs(rec[self._idx] - self._ref_c)))
        if self._end_ref_c is not None:
            err = max(err, abs(self._eval_at(n, self.t_end) - self._end_ref_c))
        return err

    def energy_n(self, n_cap: int, ratio: float = _ENERGY_RATIO) -> int:
        """energy 模式：保留累计能量 ≥ ratio 的最小 |k|。"""
        mag2 = np.abs(self._c_m) ** 2
        total = float(mag2.sum())
        if total <= 0.0:
            return 0
        order = np.argsort(self._abs_k_m_arr, kind="stable")
        cum = np.cumsum(mag2[order])
        idx = int(np.searchsorted(cum, float(ratio) * total, side="left"))
        idx = min(max(idx, 0), len(order) - 1)
        n = int(self._abs_k_m_arr[order[idx]])
        return int(min(max(n, 0), int(n_cap)))


def _pack_coef(spec: np.ndarray, m: int, n: int) -> np.ndarray:
    """|k| ≤ n 的系数打包成 (K,3) float64：每行 [k, re, im]，按 |k| 升序。"""
    n = int(max(n, 0))
    ks = np.arange(-n, n + 1, dtype=np.int64)
    order = np.argsort(np.abs(ks), kind="stable")
    ks = ks[order]
    c = spec[ks % int(m)]
    return np.column_stack([ks.astype(np.float64), c.real, c.imag]).astype(np.float64)


# ------------------------------------------------------------------ N 选择
def _choose_n(evaluator: _ErrorEvaluator, n_cap: int, thr: float, cfg: Any,
              warn: WarningLog | None, name: str) -> tuple[int, float]:
    """按 cfg.N_mode 选择 N，返回 (N, max_err)。

    error_driven：粗扫倍增定位量级 + 二分收敛到"满足契约的最小 N"。
    energy：累计频谱能量 ≥ 99.5% 的最小 |k|。
    fixed：用户固定 cfg.N_max（仍受 Nyquist 钳制）。
    """
    mode = str(getattr(cfg, "N_mode", "error_driven"))
    tag = f"[{name}] " if name else ""

    if mode == "fixed":
        requested = int(getattr(cfg, "N_max", n_cap))
        n = int(min(max(requested, 0), n_cap))
        if requested > n_cap and warn is not None:
            warn.add(
                "W_N_CLIPPED",
                f"{tag}fixed 模式 N_max={requested} 超过 Nyquist 钳制 {n_cap}，已钳制",
            )
        return n, evaluator.error(n)

    if mode == "energy":
        n = evaluator.energy_n(n_cap)
        err = evaluator.error(n)
        if err > thr and warn is not None:
            warn.add(
                "W_N_CLIPPED",
                f"{tag}energy 模式 N={n} 未达渲染契约（max_err={err:.6g} > thr={thr:.6g}）",
            )
        return n, err

    if mode != "error_driven":
        raise FBSError("E_CONFIG", f"未知 N_mode: {mode}")

    err0 = evaluator.error(0)
    if err0 <= thr:
        return 0, err0

    # ---- 粗扫：N = 2,4,8,... 直到满足契约或触顶 ----
    fail_lo = 0
    n = 2
    while True:
        err = evaluator.error(n)
        if err <= thr:
            pass_n = n
            pass_err = err
            break
        fail_lo = n
        if n >= n_cap:
            # 到顶（Nyquist 钳制或 N_max）仍不达标：保留最大可用 N，显式告警
            msg = (f"{tag}曲线在 N={n_cap}（Nyquist/N_max 钳制）仍不满足渲染契约："
                   f"max_err={err:.6g} > thr={thr:.6g}，已标记 W_N_CLIPPED")
            if warn is not None:
                warn.add("W_N_CLIPPED", msg)
            return n_cap, err
        n = min(n * 2, n_cap)

    # ---- 二分 (fail_lo, pass_n]：求满足契约的最小 N ----
    lo, hi = int(fail_lo), int(pass_n)
    err_hi = float(pass_err)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        err_mid = evaluator.error(mid)
        if err_mid <= thr:
            hi = mid
            err_hi = err_mid
        else:
            lo = mid
    return hi, err_hi


# ------------------------------------------------------------------ 公开 API
def eval_coef(coef: Any, t_closed: Any) -> np.ndarray:
    """按 coef 求值 z(t)=Σ c_k·e^{i2πkt}，t_closed 为周期参数（任意 shape）；返回 (N,2)。

    t_closed 允许任意实数（周期 1）；块状相位矩阵，避免大 N×大采样数的内存尖峰。
    """
    arr = np.asarray(coef, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != 3 or arr.shape[0] == 0:
        raise FBSError("E_CURVE", "coef 形状非法（应为 (K,3)）", evidence={"shape": list(arr.shape)})
    tt = np.atleast_1d(np.asarray(t_closed, dtype=np.float64)).reshape(-1)
    ks = arr[:, 0].astype(np.float64)
    cs = arr[:, 1] + 1j * arr[:, 2]
    out = np.zeros(len(tt), dtype=np.complex128)
    k_count = len(ks)
    if k_count <= 4 or len(tt) <= 4:
        for k, c in zip(ks, cs):
            if c != 0.0:
                out += c * np.exp(2j * np.pi * k * tt)
    else:
        block = max(1, int(_PHASE_BLOCK_BYTES // (k_count * 16)))
        for start in range(0, len(tt), block):
            chunk = tt[start:start + block]
            phase = np.exp(2j * np.pi * np.multiply.outer(chunk, ks))
            out[start:start + block] = phase @ cs
    return np.stack([out.real, out.imag], axis=1)


def eval_curve(coef: Any, closure: dict | None, t_draw: Any) -> np.ndarray:
    """按 drawable 参数 t_draw∈[0,1] 求值（开曲线映射 t_closed = t_draw·t_end）。"""
    tt = np.atleast_1d(np.asarray(t_draw, dtype=np.float64)).reshape(-1)
    kind = "none" if closure is None else str(closure.get("kind", "none"))
    if kind == "none":
        return eval_coef(coef, tt)
    if kind == "hermite_c1":
        t_end = float(closure.get("t_end", 1.0))
        return eval_coef(coef, tt * t_end)
    raise FBSError("E_CURVE", f"未知 closure.kind: {kind}")


def reconstruct_max_error(coef: Any, closure: dict | None, points: Any) -> float:
    """用 eval_curve 密集采样复核 drawable 区间上的最大偏差（世界单位，测试/调试用）。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    kind = "none" if closure is None else str(closure.get("kind", "none"))
    closed = kind == "none"
    if len(pts) < 2:
        raise FBSError("E_CURVE", "参考折线点数不足", evidence={"count": len(pts)})
    n = max(256, 2 * len(pts))
    t = np.linspace(0.0, 1.0, n)
    rec = eval_curve(coef, closure, t)
    ref = _sample_polyline(pts, t, closed=closed)
    return float(np.max(np.linalg.norm(rec - ref, axis=1)))


def fit_curve(points: np.ndarray, closed: bool, contract: dict, cfg: Any,
              warn: WarningLog | None = None, name: str = "") -> dict:
    """Stage2 主入口：折线 → 契约要求的傅里叶表示。

    返回 dict（CONTRACT.md §4）：
      coef (K,3) float64 [k,re,im]（|k| 升序，max|k|==N）、N、max_err（世界单位）、
      arc_length（drawable 世界弧长）、closed、closure、resampled（(M,2) 闭环均匀重采样点）。
    点数不足 / 全等点等退化几何抛 FBSError("E_CURVE")，由调用方跳过并记录。
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2:
        raise FBSError("E_CURVE", "点数不足（<2），无法参数化", evidence={"count": len(pts)})
    if not np.all(np.isfinite(pts)):
        raise FBSError("E_CURVE", "曲线含非有限坐标", evidence={"count": len(pts)})
    closed = bool(closed)
    if closed and len(pts) < 3:
        raise FBSError("E_CURVE", "闭合轮廓点数不足（<3）", evidence={"count": len(pts)})
    pts = _dedupe_points(pts, closed=closed)
    if len(pts) < (3 if closed else 2):
        raise FBSError("E_CURVE", "去重后点数不足，几何退化", evidence={"count": len(pts)})
    arc = float(polyline_arclength(pts, closed=closed))
    if not np.isfinite(arc) or arc <= 0.0:
        raise FBSError("E_CURVE", "曲线弧长退化（全等点）", evidence={"arc_length": arc})

    target_resolution = int(contract["target_resolution"])
    epsilon_px = float(contract["epsilon_px"])
    if target_resolution <= 0 or not np.isfinite(epsilon_px) or epsilon_px <= 0.0:
        raise FBSError("E_CONFIG", "渲染契约非法",
                       evidence={"target_resolution": target_resolution, "epsilon_px": epsilon_px})
    thr = epsilon_px / float(target_resolution)

    m = _resample_size(cfg)
    n_clamp = m // 2 - 2
    if n_clamp < 1:
        raise FBSError("E_CONFIG", "resample_points 过小，Nyquist 上限 < 1",
                       evidence={"resample_points": int(cfg.resample_points), "M": m})
    n_cap = int(min(max(int(getattr(cfg, "N_max", 0)), 0), n_clamp))
    if n_cap < 1:
        n_cap = 1

    if closed:
        resampled = resample_polyline(pts, m, closed=True)
        t_end = 1.0
        closure: dict[str, Any] = {"kind": "none"}
    else:
        closure_kind = str(getattr(cfg, "closure", "hermite_c1"))
        if closure_kind != "hermite_c1":
            raise FBSError("E_CONFIG", f"不支持的闭环方式: {closure_kind}")
        # 桥接多项式采样容差取渲染阈值的 1/8：桥接误差不参与绘制，但会污染高频系数
        bridge_tol = thr * 0.125
        closed_pts, t_end = _hermite_closed(pts, arc, bridge_tol)
        resampled = resample_polyline(closed_pts, m, closed=True)
        closure = {"kind": "hermite_c1", "t_end": float(t_end)}

    spec = _spectrum(resampled, m)
    evaluator = _ErrorEvaluator(spec, m, t_end, closed, pts)
    n_use, max_err = _choose_n(evaluator, n_cap, thr, cfg, warn, name)
    coef = _pack_coef(spec, m, n_use)
    return {
        "coef": coef,
        "N": int(n_use),
        "max_err": float(max_err),
        "arc_length": float(arc),
        "closed": closed,
        "closure": closure,
        "resampled": np.asarray(resampled, dtype=np.float64),
    }
