"""四维度量 :: fourier-brushstroke v2 的**唯一权威比较口径**（Stage 6）。

本模块存在的理由（历史事故，见 references/engineering_guardrails.md §1）
-------------------------------------------------------------------
项目上一版的多数返工是**度量错了**，不是算法错了：

· 用「渲染色 == 底色」判漏画 → 报覆盖率 99.1%，真实漏画 8.20%；
· 重建图与参考图分辨率不同时上采样再比 → 细线被判成漏画；
· 按连通域**面积**分箱归因 → 「200px 长 × 1.5px 宽的缺失发丝」被算进"大块"；
· 墨迹掩码只分「墨/背景」→ **把灰画成黑完全看不见**（掩码指标一片漂亮）；
· 覆盖判定阈值降到 0.10 → 极淡笔画也算"已覆盖"；
· 拿不同代码代/不同契约的产物当基线 → 假回归，白排查 17 分钟。

因此本模块强制三件事（由 API 形状保证，绕过就写不出来）：

1. **同分辨率**：``compare`` 两侧尺寸不同直接抛 ``E_METRIC_RESOLUTION``，
   绝不提供"上采样后再比"的路径。
2. **四维缺一不可**：一次返回 结构 / 厚度谱 / 色调 / 线宽 四维 + 契约自证；
   只取一项就下结论的用法没有 API 支持。
3. **契约/指纹自证**：``doc`` 参与时必须同代码代（codegen=fbs-v2）、同 v2 契约，
   否则把错误写进 ``contract["errors"]``（CLI ``eval`` 据此返回非 0），
   并把 ``fingerprint`` 一并带进结果，供任何 A/B 比较先核对。

口径速查
--------
* ``psnr`` / ``ssim`` 在 0..1 浮点域计算；``rmse`` / ``mae`` 单位为 8-bit 灰度级（0..255）。
* 墨迹掩码：与背景色的欧氏距离 > ``tol``（默认 **0.5**；低于 0.5 不得算"已覆盖"）。
* 背景色：四角 band 的中位色（默认 band=8），也可显式传入 ``bg``。
* 厚度谱：按连通域 **EDT 峰值（=最大内切半径）** 分箱，不是面积分箱。
* 色调：主色调量化 → 色调混淆矩阵 + CIEDE76 平均色差（能抓到"灰画成黑"）。
* 线宽：连通域 EDT 峰值 ×2 作为笔画全宽（直径），比较中位数与比值。
* 体积：``linewidth["volume"]`` 给墨量/面积比；``volume_report`` 给 .fbs/PNG 体积比，
  **必须同时给 R 与分子分母绝对值**（比值随 R 漂移，旧版曾因此误判"表示效率"）。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import numpy as np

from .io_format import CODEGEN, fingerprint as _io_fingerprint, same_generation as _io_same_generation
from .util import FBSError, distance_transform, label_components

METRICS_VERSION = "2.0"

#: 墨迹/覆盖判定默认阈值。低于 0.5 会把极淡笔画也算作"已覆盖"（事故见模块 docstring）。
DEFAULT_INK_TOL = 0.5
#: 背景色估计所用角带宽度（像素）。
BG_BAND = 8
#: 厚度谱默认分箱（EDT 峰值半径，单位：比较分辨率下的像素）。
DEFAULT_THICKNESS_BINS = (0.0, 1.01, 1.76, 2.51, 5.01, float("inf"))
_THICKNESS_LABELS = ("<1.0px（极细）", "1.0-1.75px（细）", "1.75-2.5px（中）", "2.5-5px（粗）", ">5px（块面）")
#: PSNR 上界（完全相同时刻意截断，保持 JSON 可序列化且不冒充数学上的 +inf）。
PSNR_CAP_DB = 120.0

__all__ = [
    "METRICS_VERSION",
    "DEFAULT_INK_TOL",
    "PSNR_CAP_DB",
    "as_float01",
    "load_rgb",
    "load_rgb01",
    "gaussian_window",
    "psnr",
    "ssim",
    "rmse",
    "mae",
    "bg_color",
    "ink_distance",
    "ink_mask",
    "thickness_spectrum",
    "component_peaks",
    "structure",
    "reference_tones",
    "tone_confusion",
    "srgb_to_lab",
    "delta_e76",
    "linewidth",
    "check_contract",
    "fingerprint",
    "same_generation",
    "assert_same_generation",
    "compare",
    "compare_paths",
    "volume_report",
    "format_report",
]


# ===================================================================== 基础转换
def as_float01(img: Any) -> np.ndarray:
    """把图像转成 float64 0..1（接受 uint8/uint16/float 0..255/float 0..1）。"""
    arr = np.asarray(img)
    if arr.dtype == np.uint8:
        return arr.astype(np.float64) / 255.0
    if np.issubdtype(arr.dtype, np.integer):
        info = np.iinfo(arr.dtype)
        return arr.astype(np.float64) / float(info.max if info.max > 0 else 1)
    out = arr.astype(np.float64)
    if out.size and float(np.nanmax(out)) > 1.0 + 1e-6:
        out = out / 255.0
    return out


def _to_rgb01(img: Any) -> np.ndarray:
    """统一为 (H,W,3) float64 0..1；RGBA 按白底合成，灰度复制三通道。"""
    a = as_float01(img)
    if a.ndim == 2:
        return np.repeat(a[:, :, None], 3, axis=2)
    if a.ndim != 3:
        raise FBSError("E_METRIC_INPUT", f"图像维度非法: shape={a.shape}")
    if a.shape[2] == 4:
        rgb = a[:, :, :3]
        alpha = a[:, :, 3:4]
        return np.clip(rgb * alpha + (1.0 - alpha), 0.0, 1.0)
    if a.shape[2] == 3:
        return np.clip(a, 0.0, 1.0)
    if a.shape[2] == 1:
        return np.repeat(a, 3, axis=2)
    raise FBSError("E_METRIC_INPUT", f"通道数非法: {a.shape[2]}（只支持 1/3/4）")


def load_rgb(path: str) -> np.ndarray:
    """读图片为 (H,W,3) uint8（RGBA 白底合成；不做任何尺寸缩放）。"""
    from PIL import Image

    with Image.open(path) as im:
        rgba = np.asarray(im.convert("RGBA"), dtype=np.float64) / 255.0
    rgb = rgba[:, :, :3] * rgba[:, :, 3:4] + (1.0 - rgba[:, :, 3:4])
    return np.clip(np.round(rgb * 255.0), 0, 255).astype(np.uint8)


def load_rgb01(path: str) -> np.ndarray:
    """读图片为 (H,W,3) float64 0..1。"""
    return load_rgb(path).astype(np.float64) / 255.0


# ===================================================================== 全参考指标
def _check_same_shape(a: np.ndarray, b: np.ndarray, what: str = "图像") -> None:
    if a.shape != b.shape:
        raise FBSError(
            "E_METRIC_RESOLUTION",
            f"{what}分辨率不一致: {a.shape[:2][::-1]} vs {b.shape[:2][::-1]}；"
            "禁止上采样后比较，请把 .fbs 按参考图原生分辨率重新渲染",
            evidence={"shape_a": list(a.shape), "shape_b": list(b.shape)},
            suggestions=["render(doc, resolution=max(H,W)) 后与原始参考图比较"],
        )


def psnr(a: Any, b: Any) -> float:
    """峰值信噪比（0..1 数据域，data_range=1）。完全相同返回 +inf。"""
    x, y = as_float01(a), as_float01(b)
    _check_same_shape(x, y)
    mse = float(np.mean((x - y) ** 2))
    if mse <= 0.0:
        return float("inf")
    return float(10.0 * math.log10(1.0 / mse))


def rmse(a: Any, b: Any) -> float:
    """均方根误差，单位 8-bit 灰度级（0..255）。"""
    x, y = as_float01(a), as_float01(b)
    _check_same_shape(x, y)
    return float(math.sqrt(float(np.mean((x - y) ** 2))) * 255.0)


def mae(a: Any, b: Any) -> float:
    """平均绝对误差，单位 8-bit 灰度级（0..255）。"""
    x, y = as_float01(a), as_float01(b)
    _check_same_shape(x, y)
    return float(np.mean(np.abs(x - y)) * 255.0)


def gaussian_window(win: int = 11, sigma: float = 1.5) -> np.ndarray:
    """归一化一维高斯窗（SSIM 的 11×11 / σ=1.5 窗可分离为两个 11-tap 卷积）。"""
    if win < 1 or win % 2 == 0:
        raise FBSError("E_METRIC_INPUT", f"高斯窗 win 必须为正奇数: {win}")
    r = win // 2
    xs = np.arange(-r, r + 1, dtype=np.float64)
    k = np.exp(-(xs * xs) / (2.0 * float(sigma) ** 2))
    return k / k.sum()


def _sep_filter(arr: np.ndarray, k: np.ndarray) -> np.ndarray:
    """可分离卷积（reflect 边界）。arr: (H,W) float64；k: 一维核。"""
    r = len(k) // 2
    if r == 0:
        return arr.copy()
    h, w = arr.shape
    pad_mode = "reflect" if (h > r and w > r) else "edge"
    p = np.pad(arr, ((0, 0), (r, r)), mode=pad_mode)
    tmp = np.zeros_like(arr)
    for i, kv in enumerate(k):
        tmp += kv * p[:, i : i + w]
    p2 = np.pad(tmp, ((r, r), (0, 0)), mode=pad_mode)
    out = np.zeros_like(arr)
    for i, kv in enumerate(k):
        out += kv * p2[i : i + h, :]
    return out


def _ssim_channel(x: np.ndarray, y: np.ndarray, k: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    mu_x = _sep_filter(x, k)
    mu_y = _sep_filter(y, k)
    s_xx = _sep_filter(x * x, k) - mu_x * mu_x
    s_yy = _sep_filter(y * y, k) - mu_y * mu_y
    s_xy = _sep_filter(x * y, k) - mu_x * mu_y
    c1 = 0.01 ** 2
    c2 = 0.03 ** 2
    num = (2.0 * mu_x * mu_y + c1) * (2.0 * s_xy + c2)
    den = (mu_x * mu_x + mu_y * mu_y + c1) * (s_xx + s_yy + c2)
    ssim_map = num / den
    r = len(k) // 2
    h, w = ssim_map.shape
    if h > 2 * r and w > 2 * r:
        ssim_map = ssim_map[r : h - r, r : w - r]
    return float(np.mean(ssim_map))


def ssim(a: Any, b: Any, win: int = 11, sigma: float = 1.5) -> float:
    """结构相似度（自实现：11×11 高斯窗 σ=1.5，多通道取均值，范围 [-1,1]）。

    边界用 reflect 填充；统计量在窗口内用高斯加权（Wang et al. 原始定义）。
    相同图像返回 1.0；不做任何 resize —— 尺寸不同直接抛 E_METRIC_RESOLUTION。
    """
    x, y = as_float01(a), as_float01(b)
    _check_same_shape(x, y)
    k = gaussian_window(win, sigma)
    if x.ndim == 2:
        return float(np.clip(_ssim_channel(x, y, k), -1.0, 1.0))
    vals = [_ssim_channel(x[:, :, c], y[:, :, c], k) for c in range(x.shape[2])]
    return float(np.clip(float(np.mean(vals)), -1.0, 1.0))


# ===================================================================== 背景/墨迹
def bg_color(rgb: Any, band: int = BG_BAND) -> np.ndarray:
    """四角 band 区域的中位色，单位 0..255（背景 = 画布底色）。"""
    a = as_float01(rgb)
    if a.ndim == 3:
        a = a[:, :, :3]
    h, w = a.shape[:2]
    band = int(min(max(band, 1), max(1, min(h, w) // 4)))
    corners = np.concatenate(
        [
            a[:band, :band].reshape(-1, a.shape[2]),
            a[:band, -band:].reshape(-1, a.shape[2]),
            a[-band:, :band].reshape(-1, a.shape[2]),
            a[-band:, -band:].reshape(-1, a.shape[2]),
        ]
    )
    return np.median(corners, axis=0) * 255.0


def ink_distance(rgb: Any, bg: Any) -> np.ndarray:
    """归一化墨距：0 = 纯背景，越大越有墨（欧氏距离，0..1.73 量级）。"""
    a = _to_rgb01(rgb)
    bg01 = _bg01(bg, a)
    return np.linalg.norm(a - bg01[None, None, :], axis=2)


def _bg01(bg: Any, ref01: np.ndarray) -> np.ndarray:
    if bg is None:
        return bg_color(ref01) / 255.0
    b = np.asarray(bg, dtype=np.float64).reshape(-1)[:3]
    if float(np.max(b)) > 1.5:
        b = b / 255.0
    return b


def ink_mask(rgb: Any, tol: float = DEFAULT_INK_TOL, bg: Any = None) -> np.ndarray:
    """「有没有墨」二值掩码：与背景色距离 > tol（默认 0.5）。

    ⚠️ 它**分不清灰和黑** —— 这正是必须同时看 ``tone`` 维度的原因。
    """
    return ink_distance(rgb, bg) > float(tol)


# ===================================================================== 厚度谱
def _bin_edges(thickness_bins: Sequence[float] | None) -> tuple[tuple[float, ...], tuple[str, ...]]:
    if thickness_bins is None:
        edges = tuple(float(v) for v in DEFAULT_THICKNESS_BINS)
        return edges, _THICKNESS_LABELS
    edges = tuple(float(v) for v in thickness_bins)
    if len(edges) < 2:
        raise FBSError("E_METRIC_INPUT", "thickness_bins 至少需要 2 个边界")
    labels = tuple(f"{edges[i]:g}-{edges[i + 1]:g}px" for i in range(len(edges) - 1))
    return edges, labels


def component_peaks(mask: np.ndarray) -> np.ndarray:
    """每个 8 连通域的 EDT 峰值（最大内切半径，像素），按值升序返回。"""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return np.zeros(0, dtype=np.float64)
    lab, n = label_components(m)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    dist = distance_transform(m).astype(np.float64)
    flat_lab = lab.reshape(-1)
    flat_dist = dist.reshape(-1)
    nz = flat_lab > 0
    peak = np.zeros(n + 1, dtype=np.float64)
    np.maximum.at(peak, flat_lab[nz], flat_dist[nz])
    return np.sort(peak[1:])


def thickness_spectrum(
    mask: np.ndarray,
    thickness_bins: Sequence[float] | None = None,
) -> list[dict[str, Any]]:
    """按连通域 **EDT 峰值半径**分箱 → [{bin, radius_lo_px, radius_hi_px, count, pixels}]。

    为什么不按面积分箱：一条「200px 长 × 1.5px 宽」的缺失发丝面积 300px，
    会被面积分箱算进"大块"，从而把归因引到错误方向（真实事故）。
    """
    edges, labels = _bin_edges(thickness_bins)
    m = np.asarray(mask, dtype=bool)
    out: list[dict[str, Any]] = []
    if m.any():
        lab, n = label_components(m)
        if n > 0:
            dist = distance_transform(m).astype(np.float64)
            flat_lab = lab.reshape(-1)
            flat_dist = dist.reshape(-1)
            nz = flat_lab > 0
            peak = np.zeros(n + 1, dtype=np.float64)
            np.maximum.at(peak, flat_lab[nz], flat_dist[nz])
            areas = np.bincount(lab.reshape(-1), minlength=n + 1)
            peaks, comp_areas = peak[1:], areas[1:]
        else:
            peaks = comp_areas = np.zeros(0, dtype=np.float64)
    else:
        peaks = comp_areas = np.zeros(0, dtype=np.float64)
    for i, label in enumerate(labels):
        lo, hi = edges[i], edges[i + 1]
        sel = (peaks > lo) & (peaks <= hi)
        out.append(
            {
                "bin": label,
                "radius_lo_px": None if math.isinf(lo) else float(lo),
                "radius_hi_px": None if math.isinf(hi) else float(hi),
                "count": int(sel.sum()),
                "pixels": int(comp_areas[sel].sum()),
            }
        )
    return out


# ===================================================================== 结构
def structure(
    ref01: np.ndarray,
    rec01: np.ndarray,
    *,
    tol: float = DEFAULT_INK_TOL,
    bg: Any = None,
    thickness_bins: Sequence[float] | None = None,
) -> dict[str, Any]:
    """「有没有墨」层的结构比对（同分辨率；同分辨率闸门已在 compare 里强校验）。"""
    ref = _to_rgb01(ref01)
    rec = _to_rgb01(rec01)
    aref = ink_mask(ref, tol, bg)
    arec = ink_mask(rec, tol, bg)
    ref_px = int(aref.sum())
    rec_px = int(arec.sum())
    inter = int((aref & arec).sum())
    union = int((aref | arec).sum())
    missing = aref & ~arec
    extra = arec & ~aref
    missing_px = int(missing.sum())
    extra_px = int(extra.sum())

    def _pct(num: int, den: int) -> float:
        if den <= 0:
            return 0.0 if num == 0 else 100.0
        return float(min(999.0, 100.0 * num / den))

    if ref_px == 0 and rec_px == 0:
        recall = precision = iou = 1.0
    else:
        recall = float(inter / ref_px) if ref_px else 0.0
        precision = float(inter / rec_px) if rec_px else 0.0
        iou = float(inter / union) if union else 1.0
    return {
        "ink_threshold": float(tol),
        "ref_px": ref_px,
        "rec_px": rec_px,
        "missing_px": missing_px,
        "extra_px": extra_px,
        "recall": round(recall, 6),  # noqa: 度量显示精度，非序列化精度
        "precision": round(precision, 6),  # noqa: 度量显示精度，非序列化精度
        "iou": round(iou, 6),  # noqa: 度量显示精度，非序列化精度
        "missing_pct": round(_pct(missing_px, ref_px), 4),
        "extra_pct": round(_pct(extra_px, ref_px), 4),
        "extra_pct_of_rec": round(_pct(extra_px, rec_px), 4),
        "missing_thickness": thickness_spectrum(missing, thickness_bins),
        "extra_thickness": thickness_spectrum(extra, thickness_bins),
    }


# ===================================================================== 色调
def reference_tones(rgb: Any, max_k: int = 6, min_share: float = 0.005) -> np.ndarray:
    """从原图提取主色调参考色（含背景），单位 0..255，形状 (K,3)。

    做法：量化到 8 级 → 按频次取峰 → 最小间距 24 合并。足够稳且无需重跑分割。
    """
    a = _to_rgb01(rgb) * 255.0
    q = (a / 8.0).astype(np.int32)
    flat = q[..., 0] * 64 * 64 + q[..., 1] * 64 + q[..., 2]
    vals, cnts = np.unique(flat.reshape(-1), return_counts=True)
    order = np.argsort(-cnts)
    refs: list[np.ndarray] = []
    total = int(flat.size)
    for i in order:
        if cnts[i] < max(1, int(min_share * total)):
            break
        code = int(vals[i])
        c = np.array(
            [
                (code // (64 * 64)) * 8 + 4,
                ((code // 64) % 64) * 8 + 4,
                (code % 64) * 8 + 4,
            ],
            dtype=np.float64,
        )
        if all(float(np.linalg.norm(c - r)) > 24.0 for r in refs):
            refs.append(c)
        if len(refs) >= int(max_k):
            break
    if not refs:
        refs = [bg_color(a)]
    return np.stack(refs, axis=0)


def tone_labels(rgb: Any, refs: np.ndarray) -> np.ndarray:
    """每个像素归到最近的参考色调（返回 (H,W) int32）。"""
    a = _to_rgb01(rgb) * 255.0
    out = np.zeros(a.shape[:2], dtype=np.int32)
    best = np.full(a.shape[:2], np.inf, dtype=np.float64)
    for i in range(len(refs)):
        d = ((a - refs[i][None, None, :]) ** 2).sum(axis=2)
        upd = d < best
        best = np.where(upd, d, best)
        out[upd] = i
    return out


def tone_confusion(
    ref_rgb: Any,
    rec_rgb: Any,
    refs: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """色调混淆矩阵 | 原图色调 × 重建色调。返回 (K×K 计数, 参考色 0..255)。

    矩阵**非对角元**就是色调错误量 —— 这是"灰画成黑"唯一能看见的地方。
    """
    ref = _to_rgb01(ref_rgb)
    if refs is None:
        refs = reference_tones(ref)
    la = tone_labels(ref, refs)
    lb = tone_labels(rec_rgb, refs)
    k = len(refs)
    idx = la.astype(np.int64) * k + lb.astype(np.int64)
    conf = np.bincount(idx.reshape(-1), minlength=k * k).reshape(k, k)
    return conf, refs


def srgb_to_lab(rgb01: np.ndarray) -> np.ndarray:
    """sRGB 0..1 → CIELAB（D65 白点）。支持 (...,3)；返回同形状。"""
    c = np.clip(np.asarray(rgb01, dtype=np.float64), 0.0, 1.0)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    # noqa: 标准 sRGB→XYZ(D65) 矩阵系数（IEC 61966-2-1），不是手写序列化精度
    m = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float64,
    )
    white = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)
    xyz = lin.reshape(-1, 3) @ m.T / white[None, :]
    eps = (6.0 / 29.0) ** 3
    f = np.where(xyz > eps, np.cbrt(xyz), xyz / (3.0 * (6.0 / 29.0) ** 2) + 4.0 / 29.0)
    lab = np.stack(
        [116.0 * f[:, 1] - 16.0, 500.0 * (f[:, 0] - f[:, 1]), 200.0 * (f[:, 1] - f[:, 2])],
        axis=1,
    )
    return lab.reshape(np.asarray(rgb01).shape)


def delta_e76(rgb_a: Any, rgb_b: Any) -> np.ndarray:
    """CIE76 色差 ΔE（0..~100 量级），输入 0..1 RGB，返回 (H,W)。"""
    a = srgb_to_lab(_to_rgb01(rgb_a))
    b = srgb_to_lab(_to_rgb01(rgb_b))
    return np.sqrt(((a - b) ** 2).sum(axis=2))


def _tone_metrics(
    ref01: np.ndarray,
    rec01: np.ndarray,
    ref_mask: np.ndarray,
    rec_mask: np.ndarray,
) -> dict[str, Any]:
    conf, refs = tone_confusion(ref01, rec01)
    k = len(refs)
    total = int(conf.sum())
    diag = int(np.trace(conf))
    wrong_pct = 0.0 if total == 0 else 100.0 * (total - diag) / total
    names = [f"T{i}({int(r[0])},{int(r[1])},{int(r[2])})" for i, r in enumerate(refs)]

    union = ref_mask | rec_mask
    de = delta_e76(ref01, rec01)
    if union.any():
        ink_de = de[union]
        mean_de = float(ink_de.mean())
        p95_de = float(np.percentile(ink_de, 95))
        extra_de = float(de[rec_mask & ~ref_mask].mean()) if (rec_mask & ~ref_mask).any() else 0.0
        # 墨迹像素上的色调混淆（背景不参与，避免大面积正确背景稀释错误率）
        idx = np.bincount(
            (tone_labels(ref01, refs)[union].astype(np.int64) * k
             + tone_labels(rec01, refs)[union].astype(np.int64)),
            minlength=k * k,
        ).reshape(k, k)
        ink_total = int(idx.sum())
        ink_wrong = 100.0 * (ink_total - int(np.trace(idx))) / ink_total if ink_total else 0.0
    else:
        mean_de = p95_de = extra_de = 0.0
        ink_wrong = 0.0
    return {
        "mean_delta_e": round(mean_de, 4),
        "p95_delta_e": round(p95_de, 4),
        "extra_ink_delta_e": round(extra_de, 4),
        "wrong_pct": round(wrong_pct, 4),
        "wrong_ink_pct": round(ink_wrong, 4),
        "confusion": [[int(v) for v in row] for row in conf],
        "refs": [[int(v) for v in r] for r in refs],
        "names": names,
        "note": "confusion 行=原图色调, 列=重建色调；非对角元即色调错误（结构指标对其盲）",
    }


# ===================================================================== 线宽 / 体积
def _width_stats(peaks: np.ndarray) -> dict[str, Any]:
    widths = 2.0 * peaks  # 峰值半径 → 全宽（直径）
    if widths.size == 0:
        return {"median_px": 0.0, "p10_px": 0.0, "p90_px": 0.0, "components": 0}
    return {
        "median_px": round(float(np.median(widths)), 4),
        "p10_px": round(float(np.percentile(widths, 10)), 4),
        "p90_px": round(float(np.percentile(widths, 90)), 4),
        "components": int(widths.size),
    }


def linewidth(
    ref01: np.ndarray,
    rec01: np.ndarray,
    *,
    tol: float = DEFAULT_INK_TOL,
    bg: Any = None,
    ref_mask: np.ndarray | None = None,
    rec_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """笔画宽度（厚度）分布对比：连通域 EDT 峰值 ×2 = 全宽（直径，像素）。"""
    ref = _to_rgb01(ref01)
    rec = _to_rgb01(rec01)
    aref = ink_mask(ref, tol, bg) if ref_mask is None else np.asarray(ref_mask, dtype=bool)
    arec = ink_mask(rec, tol, bg) if rec_mask is None else np.asarray(rec_mask, dtype=bool)
    sref = _width_stats(component_peaks(aref))
    srec = _width_stats(component_peaks(arec))
    ratio = None
    if sref["median_px"] > 0:
        ratio = round(float(srec["median_px"] / sref["median_px"]), 4)
    ref_px = int(aref.sum())
    rec_px = int(arec.sum())
    d_ref = ink_distance(ref, bg)
    d_rec = ink_distance(rec, bg)
    mass_ref = float(np.clip(d_ref[aref], 0.0, 1.0).sum()) if ref_px else 0.0
    mass_rec = float(np.clip(d_rec[arec], 0.0, 1.0).sum()) if rec_px else 0.0
    volume = {
        "ref_ink_px": ref_px,
        "rec_ink_px": rec_px,
        "area_ratio": round(rec_px / ref_px, 4) if ref_px else None,
        "ref_ink_mass": round(mass_ref, 2),
        "rec_ink_mass": round(mass_rec, 2),
        "mass_ratio": round(mass_rec / mass_ref, 4) if mass_ref > 0 else None,
        "note": "mass=墨距(0..1)之和，等价于墨量积分；比值需与面积比、线宽比一起看",
    }
    return {
        "method": "component_EDT_peak_x2",
        "ref_median_px": sref["median_px"],
        "rec_median_px": srec["median_px"],
        "ratio": ratio,
        "ratio_abs_error_pct": round(abs(ratio - 1.0) * 100.0, 2) if ratio is not None else None,
        "ref_p10_px": sref["p10_px"],
        "ref_p90_px": sref["p90_px"],
        "rec_p10_px": srec["p10_px"],
        "rec_p90_px": srec["p90_px"],
        "ref_components": sref["components"],
        "rec_components": srec["components"],
        "volume": volume,
    }


# ===================================================================== 指纹 / 契约闸门
def fingerprint(doc: dict) -> dict:
    """文档指纹（image/params/contract sha + codegen）；不同代禁止比较。"""
    return _io_fingerprint(doc)


def same_generation(a: dict, b: dict) -> bool:
    """两份文档是否同代码代 + 同参数 + 同渲染契约（可互相比较的前提）。"""
    return bool(_io_same_generation(a, b))


def assert_same_generation(a: dict, b: dict) -> None:
    """不可比直接抛 E_FBS_INVALID（返回数字前必须先证明可比）。"""
    if same_generation(a, b):
        return
    fa, fb = fingerprint(a), fingerprint(b)
    keys = ("codegen", "params_sha256", "contract_sha256")
    diff = {k: [fa.get(k), fb.get(k)] for k in keys if fa.get(k) != fb.get(k)}
    raise FBSError(
        "E_FBS_INVALID",
        "两份 .fbs 不同代码代/不同契约/不同参数，禁止互相比较（先证明可比）",
        evidence={"diff": diff},
        suggestions=["用同一代码代、同一 FBSConfig 重新生成后再 A/B"],
    )


def _aspect_ratio(shape: Sequence[int]) -> float:
    h, w = int(shape[0]), int(shape[1])
    return float(w) / float(h) if h else 1.0


def check_contract(doc: dict | None, resolution: Sequence[int] | None = None) -> dict[str, Any]:
    """校验 doc 是否允许参与比较；问题写入 ``errors``（CLI eval 据此返回非 0）。"""
    info: dict[str, Any] = {
        "target_resolution": None,
        "epsilon_px": None,
        "fingerprint": None,
        "codegen": None,
        "resolution": [int(resolution[0]), int(resolution[1])] if resolution is not None else None,
        "resolution_matches_target": None,
        "comparable": True,
        "errors": [],
    }
    if doc is None:
        return info
    errors: list[str] = info["errors"]
    fmt = doc.get("format")
    if fmt != "fourier-brushstroke":
        errors.append(f"format 非法: {fmt!r}（应为 'fourier-brushstroke'）")
    ver = str(doc.get("version", "0"))
    if ver.split(".")[0] != "2":
        errors.append(f"version 非 2.x: {ver!r}（v1 产物禁止参与 v2 比较）")
    contract = doc.get("render_contract") or {}
    for key in ("target_resolution", "epsilon_px"):
        if key not in contract:
            errors.append(f"render_contract 缺少 {key}")
    info["target_resolution"] = contract.get("target_resolution")
    info["epsilon_px"] = contract.get("epsilon_px")
    try:
        fp = fingerprint(doc)
    except FBSError as exc:
        errors.append(f"fingerprint 计算失败: {exc.message}")
        fp = {}
    if fp:
        info["fingerprint"] = {
            k: fp.get(k) for k in ("image_sha256", "params_sha256", "codegen", "contract_sha256")
        }
        info["codegen"] = fp.get("codegen")
        if fp.get("codegen") != CODEGEN:
            errors.append(f"不同代码代: codegen={fp.get('codegen')!r}（要求 {CODEGEN!r}）")
    if resolution is not None and contract.get("target_resolution") is not None:
        info["resolution_matches_target"] = int(max(int(resolution[0]), int(resolution[1]))) == int(
            contract["target_resolution"]
        )
    canvas = doc.get("canvas") or {}
    if resolution is not None and canvas.get("width") and canvas.get("height"):
        doc_aspect = float(canvas["width"]) / float(canvas["height"])
        # resolution 是 [W, H]（numpy shape / compare 口径），_aspect_ratio 约定入参为 (H, W)
        ref_aspect = _aspect_ratio([resolution[1], resolution[0]])
        if doc_aspect > 0 and abs(ref_aspect / doc_aspect - 1.0) > 0.01:
            errors.append(
                f"参考图与文档 canvas 宽高比不一致（{ref_aspect:.4f} vs {doc_aspect:.4f}），可能拿错参考图"
            )
    info["comparable"] = not errors
    return info


# ===================================================================== 四维汇总
def compare(
    render_rgb: Any,
    reference_rgb: Any,
    *,
    doc: dict | None = None,
    bg: Any = None,
    thickness_bins: Sequence[float] | None = None,
) -> dict[str, Any]:
    """一次输出四维比较 + 契约自证（**唯一权威口径**，禁止绕过）。

    render_rgb / reference_rgb：同分辨率图像（H,W,3；接受 uint8 或 0..1 float）。
    尺寸不同 → 抛 ``E_METRIC_RESOLUTION``，绝不 resize 后比较。
    doc 传入时校验代码代/契约/画布宽高比，问题进 ``contract["errors"]``。
    """
    render = _to_rgb01(render_rgb)
    reference = _to_rgb01(reference_rgb)
    _check_same_shape(render, reference, "渲染图与参考图")
    h, w = reference.shape[:2]
    resolution = [int(w), int(h)]
    contract = check_contract(doc, resolution=resolution)

    bg01 = _bg01(bg, reference)
    ref_mask = ink_mask(reference, DEFAULT_INK_TOL, bg01)
    rec_mask = ink_mask(render, DEFAULT_INK_TOL, bg01)

    raw_psnr = psnr(render, reference)
    psnr_db = PSNR_CAP_DB if math.isinf(raw_psnr) else float(raw_psnr)
    struct = structure(
        reference, render, tol=DEFAULT_INK_TOL, bg=bg01, thickness_bins=thickness_bins
    )
    result: dict[str, Any] = {
        "metrics_version": METRICS_VERSION,
        "resolution": resolution,
        "psnr": round(psnr_db, 4),
        "psnr_capped": bool(math.isinf(raw_psnr)),
        "ssim": round(ssim(render, reference), 6),  # noqa: 度量显示精度，非序列化精度
        "rmse": round(rmse(render, reference), 4),
        "mae": round(mae(render, reference), 4),
        "structure": struct,
        "thickness": {
            "bins": [None if math.isinf(float(v)) else float(v) for v in (
                thickness_bins if thickness_bins is not None else DEFAULT_THICKNESS_BINS
            )],
            "hist_ref": thickness_spectrum(ref_mask, thickness_bins),
            "hist_rec": thickness_spectrum(rec_mask, thickness_bins),
            "missing": struct["missing_thickness"],
            "extra": struct["extra_thickness"],
            "ref_peak_median_px": _width_stats(component_peaks(ref_mask))["median_px"],
            "rec_peak_median_px": _width_stats(component_peaks(rec_mask))["median_px"],
            "note": "按连通域 EDT 峰值（最大内切半径）分箱；分箱单位=比较分辨率像素",
        },
        "tone": _tone_metrics(reference, render, ref_mask, rec_mask),
        "linewidth": linewidth(
            reference, render, tol=DEFAULT_INK_TOL, bg=bg01, ref_mask=ref_mask, rec_mask=rec_mask
        ),
        "contract": contract,
        "units": {
            "psnr_dB": "dB @ data_range=1（相同图截断为 120）",
            "ssim": "无量纲 [-1,1]",
            "rmse_mae": "8-bit 灰度级（0..255）",
            "distances_px": f"像素（比较分辨率长边 R={max(h, w)}）",
        },
    }
    return result


def compare_paths(
    render_path: str,
    reference_path: str,
    *,
    doc: dict | None = None,
    bg: Any = None,
    thickness_bins: Sequence[float] | None = None,
) -> dict[str, Any]:
    """从两个 PNG 路径做四维比较（尺寸不同会抛 E_METRIC_RESOLUTION）。"""
    return compare(
        load_rgb(render_path),
        load_rgb(reference_path),
        doc=doc,
        bg=bg,
        thickness_bins=thickness_bins,
    )


def volume_report(
    doc: dict | None,
    fbs_bytes: int,
    png_bytes: int,
    *,
    resolution: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    """.fbs / PNG 体积比 —— **必须同时给 R 与分子分母绝对值**（比值随 R 漂移）。"""
    fbs_b = int(fbs_bytes)
    png_b = int(png_bytes)
    contract = (doc or {}).get("render_contract") or {}
    res = int(resolution if resolution is not None else contract.get("target_resolution", 0) or 0)
    ratio = (fbs_b / png_b) if png_b > 0 else None
    return {
        "resolution_R": res,
        "epsilon_px": contract.get("epsilon_px"),
        "fbs_bytes": fbs_b,
        "png_bytes": png_b,
        "fbs_kib": round(fbs_b / 1024.0, 2),
        "png_kib": round(png_b / 1024.0, 2),
        "ratio_fbs_over_png": round(ratio, 4) if ratio is not None else None,
        "note": note
        or "比值随 R 漂移（同一表示 R=512 时 5.49×、R=1024 时 3.12×），"
        "任何结论必须同时报 R、分子分母绝对值；不要当'表示效率'",
    }


def format_report(result: dict[str, Any]) -> str:
    """把 compare() 结果排成可读文本（控制台 / report.md 用）。"""
    s = result["structure"]
    t = result["tone"]
    lw = result["linewidth"]
    contract = result.get("contract") or {}
    lines = [
        f"分辨率 {result['resolution'][0]}×{result['resolution'][1]}   "
        f"PSNR {result['psnr']:.3f} dB   SSIM {result['ssim']:.6f}   "
        f"RMSE {result['rmse']:.3f}   MAE {result['mae']:.3f}",
        f"[结构] recall {s['recall']:.4f}  precision {s['precision']:.4f}  IoU {s['iou']:.4f}  "
        f"漏画 {s['missing_pct']:.2f}%  多画 {s['extra_pct']:.2f}%",
    ]
    missing_bins = [b for b in s["missing_thickness"] if b["count"] > 0]
    if missing_bins:
        lines.append("       漏画厚度谱: " + "  ".join(f"{b['bin']}×{b['count']}({b['pixels']}px)" for b in missing_bins))
    lines.append(
        f"[色调] 平均 ΔE {t['mean_delta_e']:.2f}  P95 ΔE {t['p95_delta_e']:.2f}  "
        f"墨迹色调错误 {t['wrong_ink_pct']:.2f}%  全图错误 {t['wrong_pct']:.2f}%"
    )
    conf = np.asarray(t["confusion"])
    if conf.size:
        lines.append("       色调混淆矩阵（行=原图, 列=重建）:")
        for i, name in enumerate(t["names"]):
            row = "  ".join(f"{int(v):>8}" for v in conf[i])
            lines.append(f"         {name:<20} {row}")
    lines.append(
        f"[线宽] 参考中位 {lw['ref_median_px']:.2f}px  重建中位 {lw['rec_median_px']:.2f}px  "
        f"比值 {lw['ratio']}  墨量比 {lw['volume']['mass_ratio']}  面积比 {lw['volume']['area_ratio']}"
    )
    if contract:
        if contract.get("errors"):
            lines.append(f"[契约] ⚠️ 不可比: {contract['errors']}")
        else:
            lines.append(
                f"[契约] target_R={contract.get('target_resolution')} eps={contract.get('epsilon_px')} "
                f"codegen={contract.get('codegen')} 比较R={result['resolution'][0]}×{result['resolution'][1]}"
                f"(与 target 一致={contract.get('resolution_matches_target')})"
            )
    return "\n".join(lines)

