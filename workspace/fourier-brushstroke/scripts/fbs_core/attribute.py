"""AttributeCurve 求值：width / alpha（数值）、color（色标）、shape（分类）。

所有模块只允许通过本模块求值，禁止各自重写插值语义（旧版 R4：多处实现漂移）。
任何越界/空曲线都必须显式走 default 或抛错，不得静默返回 0。
"""
from __future__ import annotations

import numpy as np

from .shapelib import VALID_SHAPES  # 笔形注册表（round/soft/marker/bristle/chalk/...）
from .util import FBSError

_PRESETS = {
    "taper-in": lambda t: t,
    "taper-out": lambda t: 1.0 - t,
    "taper-both": lambda t: np.sin(np.pi * np.clip(t, 0.0, 1.0)),
    "ease-in": lambda t: t * t,
    "ease-out": lambda t: 1.0 - (1.0 - t) * (1.0 - t),
    "sigmoid": lambda t: 1.0 / (1.0 + np.exp(-8.0 * (t - 0.5))),
}


def _as_t(t) -> np.ndarray:
    return np.atleast_1d(np.asarray(t, dtype=np.float64))


def _keys(curve: dict) -> tuple[np.ndarray, np.ndarray]:
    keys = curve.get("keys") or []
    if not keys:
        return np.zeros(0), np.zeros(0)
    arr = np.asarray(keys, dtype=np.float64)
    ts, vs = arr[:, 0].copy(), arr[:, 1].copy()
    order = np.argsort(ts, kind="stable")
    return ts[order], vs[order]


def _catmull_rom(ts: np.ndarray, vs: np.ndarray, t: np.ndarray) -> np.ndarray:
    """过关键点的 Catmull-Rom 三次样条（端点用镜像虚拟点）。"""
    if len(ts) == 1:
        return np.full_like(t, vs[0])
    t = np.clip(t, ts[0], ts[-1])
    idx = np.searchsorted(ts, t, side="right") - 1
    idx = np.clip(idx, 0, len(ts) - 2)
    t0, t1 = ts[idx], ts[idx + 1]
    dt = np.maximum(t1 - t0, 1e-12)
    u = (t - t0) / dt
    v0, v1 = vs[idx], vs[idx + 1]
    vm = vs[np.maximum(idx - 1, 0)]
    vp = vs[np.minimum(idx + 2, len(vs) - 1)]
    # 均匀 Catmull-Rom（非心参数化；对单调关键点足够稳定）
    m0 = 0.5 * (v1 - vm)
    m1 = 0.5 * (vp - v0)
    u2, u3 = u * u, u * u * u
    return (2 * u3 - 3 * u2 + 1) * v0 + (u3 - 2 * u2 + u) * m0 + (-2 * u3 + 3 * u2) * v1 + (u3 - u2) * m1


def _eval_preset(curve: dict, t: np.ndarray) -> np.ndarray:
    name = str(curve.get("preset", "taper-out"))
    if name not in _PRESETS:
        raise FBSError("E_CURVE", f"未知 preset: {name}", suggestions=[f"可用: {sorted(_PRESETS)}"])
    strength = float(curve.get("strength", 1.0))
    scale = curve.get("scale")
    if scale is None:
        raise FBSError("E_CURVE", "preset 曲线缺少 scale", evidence={"preset": name})
    scale = float(scale)
    g = _PRESETS[name](t)
    return scale * (1.0 - strength + strength * g)


def eval_numeric(curve: dict | None, t, default: float | None = None) -> np.ndarray:
    """数值曲线求值。t 任意 shape；返回同 shape float64。"""
    t = _as_t(t)
    if not curve:
        if default is None:
            raise FBSError("E_CURVE", "空曲线且未给 default")
        return np.full(t.shape, float(default), dtype=np.float64)
    interp = str(curve.get("interp", "constant"))
    if interp == "constant":
        keys = curve.get("keys") or []
        value = float(keys[0][1]) if keys else default
        if value is None:
            raise FBSError("E_CURVE", "constant 曲线无 keys 且未给 default")
        return np.full(t.shape, float(value), dtype=np.float64)
    if interp in ("linear", "spline", "step"):
        ts, vs = _keys(curve)
        if len(ts) == 0:
            if default is None:
                raise FBSError("E_CURVE", f"{interp} 曲线无 keys 且未给 default")
            return np.full(t.shape, float(default), dtype=np.float64)
        if interp == "step":
            idx = np.searchsorted(ts, t, side="right") - 1
            idx = np.clip(idx, 0, len(vs) - 1)
            return vs[idx]
        if interp == "spline":
            return _catmull_rom(ts, vs, t)
        clamp = bool(curve.get("clamp", True))
        if clamp:
            return np.interp(t, ts, vs)
        out = np.interp(t, ts, vs)
        if len(ts) >= 2:  # 允许外推（仅显式 clamp=False 时）
            slope0 = (vs[1] - vs[0]) / max(ts[1] - ts[0], 1e-12)
            slope1 = (vs[-1] - vs[-2]) / max(ts[-1] - ts[-2], 1e-12)
            lo = t < ts[0]
            hi = t > ts[-1]
            out[lo] = vs[0] + (t[lo] - ts[0]) * slope0
            out[hi] = vs[-1] + (t[hi] - ts[-1]) * slope1
        return out
    if interp == "fourier":
        coefs = np.asarray(curve.get("coef") or [], dtype=np.float64)
        if len(coefs) == 0:
            if default is None:
                raise FBSError("E_CURVE", "fourier 曲线无 coef")
            return np.full(t.shape, float(default))
        out = np.zeros_like(t)
        for k, re, im in coefs:
            kk = float(k)
            out += re * np.cos(2.0 * np.pi * kk * t) - im * np.sin(2.0 * np.pi * kk * t)
        return out
    if interp == "preset":
        return _eval_preset(curve, t)
    raise FBSError("E_CURVE", f"未知 interp: {interp}")


def eval_color(curve: dict | None, t) -> tuple[np.ndarray, np.ndarray]:
    """颜色曲线求值。返回 rgb (...,3) float 0..1 与 alpha (...)。"""
    t = _as_t(t)
    if not curve:
        raise FBSError("E_CURVE", "颜色曲线为空")
    interp = str(curve.get("interp", "constant"))
    stops = curve.get("stops") or []
    if not stops:
        raise FBSError("E_CURVE", "颜色曲线没有 stops")
    ts = np.asarray([float(s["t"]) for s in stops], dtype=np.float64)
    rgb = np.asarray([s["rgb"] for s in stops], dtype=np.float64) / 255.0
    alpha = np.asarray([float(s.get("a", 1.0)) for s in stops], dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    ts, rgb, alpha = ts[order], rgb[order], alpha[order]
    if interp == "constant" or len(ts) == 1:
        idx = np.searchsorted(ts, t, side="right") - 1
        idx = np.clip(idx, 0, len(ts) - 1)
        return rgb[idx], alpha[idx]
    if interp == "linear":
        tq = np.clip(t, ts[0], ts[-1])
        out_rgb = np.stack([np.interp(tq, ts, rgb[:, c]) for c in range(3)], axis=-1)
        out_a = np.interp(tq, ts, alpha)
        return out_rgb, out_a
    if interp == "spline":
        out_rgb = np.stack([_catmull_rom(ts, rgb[:, c], t) for c in range(3)], axis=-1)
        out_a = _catmull_rom(ts, alpha, t)
        return np.clip(out_rgb, 0.0, 1.0), np.clip(out_a, 0.0, 1.0)
    raise FBSError("E_CURVE", f"未知 color interp: {interp}")


def eval_shape(curve: dict | None, t) -> list[str]:
    """分类曲线（step）求值。"""
    t = _as_t(t)
    if not curve:
        return ["round"] * len(t)
    keys = curve.get("keys") or []
    if not keys:
        return ["round"] * len(t)
    ts = np.asarray([float(k[0]) for k in keys], dtype=np.float64)
    vals = [str(k[1]) for k in keys]
    order = np.argsort(ts, kind="stable")
    ts, vals = ts[order], [vals[i] for i in order]
    idx = np.searchsorted(ts, t, side="right") - 1
    idx = np.clip(idx, 0, len(vals) - 1)
    out = [vals[i] for i in idx]
    for v in out:
        if v not in VALID_SHAPES:
            raise FBSError("E_CURVE", f"未知笔形: {v}", suggestions=[f"可用: {list(VALID_SHAPES)}"])
    return out


def curve_is_constant(curve: dict | None) -> bool:
    """曲线是否常量（体积纪律：常量必须压成 constant）。"""
    if not curve:
        return True
    interp = str(curve.get("interp", "constant"))
    if interp == "constant":
        return True
    if interp in ("linear", "spline", "step"):
        keys = curve.get("keys") or []
        if len(keys) <= 1:
            return True
        vals = np.asarray([float(k[1]) for k in keys])
        return bool(np.allclose(vals, vals[0], rtol=0.0, atol=1e-12))
    if interp == "fourier":
        coefs = np.asarray(curve.get("coef") or [], dtype=np.float64)
        if len(coefs) <= 1:
            return True
        return bool(np.all(np.abs(coefs[1:, 1:]) < 1e-12))
    return False
