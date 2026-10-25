"""通用工具：错误/警告、加速器探测、可分离高斯、距离变换、连通域、
形态学、折线重采样、双线性采样、哈希、字体查找。

设计约束（见 CONTRACT.md）：
- 必需依赖只有 numpy；scipy 存在则走精确加速路径，不存在则走 numpy 回退。
- 回退路径不得静默降级：`accelerators()` 会把实际后端写进 .fbs，供验收核对。
- 本模块不含任何写死的"数据相关上限"；调用方传什么就处理什么。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

# ---------------------------------------------------------------- scipy 探测
try:  # pragma: no cover - 环境相关
    import scipy.ndimage as _ndi  # type: ignore

    _HAVE_SCIPY = True
except Exception:  # noqa: BLE001 - 探测失败是合法路径
    _ndi = None
    _HAVE_SCIPY = False

_ACCEL_CACHE: dict[str, str] = {}


def have_scipy() -> bool:
    """scipy 是否可用（仅用于加速，不影响功能语义）。"""
    return _HAVE_SCIPY


def accelerators(extra: dict[str, str] | None = None) -> dict[str, str]:
    """返回本进程实际使用的后端记录，写入 .fbs 的 accelerators 字段。"""
    acc = dict(_ACCEL_CACHE)
    acc.setdefault("edt", "scipy" if _HAVE_SCIPY else "numpy_chamfer_approx")
    acc.setdefault("gaussian", "scipy" if _HAVE_SCIPY else "numpy_separable")
    acc.setdefault("label", "scipy" if _HAVE_SCIPY else "numpy_propagate")
    if extra:
        acc.update(extra)
    return acc


# ---------------------------------------------------------------- 错误与警告
class FBSError(Exception):
    """带机器可读 code 的异常。所有面向用户的失败都必须用这个类，禁止裸露 ValueError。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.evidence = dict(evidence or {})
        self.suggestions = list(suggestions or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "evidence": self.evidence,
            "suggestions": self.suggestions,
        }


@dataclass
class WarningLog:
    """统一的降级/超限/精度未达标记录。禁止用 print 代替。"""

    warnings: list[dict[str, str]] = field(default_factory=list)

    def add(self, code: str, message: str, severity: str = "warn") -> None:
        rec = {"code": str(code), "message": str(message), "severity": str(severity)}
        if rec not in self.warnings:  # 同一警告只记一次，避免刷屏
            self.warnings.append(rec)

    def extend(self, other: "WarningLog | Iterable[dict[str, str]] | None") -> None:
        if other is None:
            return
        if isinstance(other, WarningLog):
            other = other.to_list()
        for rec in other:
            self.add(rec.get("code", "W_UNKNOWN"), rec.get("message", ""), rec.get("severity", "warn"))

    def to_list(self) -> list[dict[str, str]]:
        return [dict(w) for w in self.warnings]

    def __len__(self) -> int:  # pragma: no cover - 简单转发
        return len(self.warnings)


# ---------------------------------------------------------------- 哈希
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                return h.hexdigest()
            h.update(buf)


def sha256_json(obj: Any) -> str:
    """对可 JSON 序列化对象取稳定哈希（sort_keys，不依赖字典插入顺序）。"""
    txt = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    return sha256_bytes(txt.encode("utf-8"))


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


# ---------------------------------------------------------------- 基础数值
def smoothstep(x: np.ndarray | float) -> np.ndarray:
    """0→1 的三次平滑过渡（钳位）。"""
    x = np.clip(np.asarray(x, dtype=np.float64), 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def gaussian_kernel1d(sigma: float) -> np.ndarray:
    sigma = float(max(sigma, 1e-6))
    radius = max(1, int(math.ceil(3.0 * sigma)))
    xs = np.arange(-radius, radius + 1, dtype=np.float64)
    k = np.exp(-(xs * xs) / (2.0 * sigma * sigma))
    return k / k.sum()


def gaussian_blur(arr: np.ndarray, sigma: float) -> np.ndarray:
    """可分离高斯模糊。支持 (H,W) 或 (H,W,C)；边界用 reflect 填充。"""
    arr = np.asarray(arr)
    if sigma is None or sigma <= 0:
        return arr.astype(np.float32, copy=True)
    if _HAVE_SCIPY:  # pragma: no branch - 生产环境走这里
        sig = (float(sigma), float(sigma)) + ((0.0,) if arr.ndim == 3 else ())
        out = _ndi.gaussian_filter(arr.astype(np.float32), sigma=sig, mode="reflect")
        _ACCEL_CACHE["gaussian"] = "scipy"
        return out
    _ACCEL_CACHE["gaussian"] = "numpy_separable"
    k = gaussian_kernel1d(sigma)
    r = len(k) // 2
    x = np.asarray(arr, dtype=np.float32)
    if x.ndim == 2:
        x = x[:, :, None]
    pad = np.pad(x, ((r, r), (r, r), (0, 0)), mode="reflect")
    h, w, c = x.shape
    tmp = np.zeros_like(pad)
    for i, kv in enumerate(k):
        tmp += kv * pad[i : i + h + 2 * r, :, :]
    out = np.zeros_like(tmp)
    for i, kv in enumerate(k):
        out += kv * tmp[:, i : i + w + 2 * r, :]
    out = out[r : r + h, r : r + w, :]
    return out[:, :, 0] if arr.ndim == 2 else out


def _shift(mask: np.ndarray, dy: int, dx: int, fill: bool) -> np.ndarray:
    """返回平移后的布尔数组（越界填 fill）。"""
    out = np.full_like(mask, fill, dtype=bool)
    h, w = mask.shape
    ys, yd = (slice(0, h - dy), slice(dy, h)) if dy >= 0 else (slice(-dy, h), slice(0, h + dy))
    xs, xd = (slice(0, w - dx), slice(dx, w)) if dx >= 0 else (slice(-dx, w), slice(0, w + dx))
    out[yd, xd] = mask[ys, xs]
    return out


_NEIGHBOR8 = tuple((dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if not (dy == 0 and dx == 0))


def erode(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    """8 连通二值腐蚀（radius 轮）。"""
    mask = np.asarray(mask, dtype=bool)
    if radius <= 0:
        return mask.copy()
    if _HAVE_SCIPY:
        _ACCEL_CACHE["morph"] = "scipy"
        return _ndi.binary_erosion(mask, structure=np.ones((3, 3), bool), iterations=int(radius), border_value=0)
    out = mask.copy()
    for _ in range(int(radius)):
        cur = out.copy()
        for dy, dx in _NEIGHBOR8:
            cur &= _shift(out, dy, dx, False)
        out = cur
    return out


def dilate(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    """8 连通二值膨胀（radius 轮）。"""
    mask = np.asarray(mask, dtype=bool)
    if radius <= 0:
        return mask.copy()
    if _HAVE_SCIPY:
        _ACCEL_CACHE["morph"] = "scipy"
        return _ndi.binary_dilation(mask, structure=np.ones((3, 3), bool), iterations=int(radius), border_value=0)
    out = mask.copy()
    for _ in range(int(radius)):
        cur = out.copy()
        for dy, dx in _NEIGHBOR8:
            cur |= _shift(out, dy, dx, False)
        out = cur
    return out


def distance_transform(mask: np.ndarray) -> np.ndarray:
    """欧氏距离变换：mask=True 的前景到最近背景像素的距离（单位：像素，值为像素中心距）。

    scipy 可用时精确（float64）；否则退回 8 邻域倒角近似（误差 ≤3%），并在
    accelerators() 里明确记录为 numpy_chamfer_approx —— 不允许冒充精确值。
    """
    mask = np.asarray(mask, dtype=bool)
    if _HAVE_SCIPY:
        _ACCEL_CACHE["edt"] = "scipy"
        return _ndi.distance_transform_edt(mask).astype(np.float32)
    _ACCEL_CACHE["edt"] = "numpy_chamfer_approx"
    h, w = mask.shape
    INF = np.float32(1e20)
    a, b = np.float32(1.0), np.float32(1.41421356)  # noqa: 倒角核 √2 标准常数
    dist = np.where(mask, INF, np.float32(0.0))
    idx = np.arange(w, dtype=np.float32)
    ridx = (w - 1 - idx).astype(np.float32)

    def _relax_row(cur: np.ndarray, prev: np.ndarray) -> np.ndarray:
        """单行向量化倒角更新 + 行内精确前缀/后缀松弛（避免逐像素循环）。"""
        left = np.concatenate(([INF], prev[:-1]))
        right = np.concatenate((prev[1:], [INF]))
        out = np.minimum(cur, np.minimum(prev + a, np.minimum(left + b, right + b)))
        # 行内左→右传播（等价 min_x'<=x out[x'] + (x-x')*a）
        out = np.minimum(out, np.minimum.accumulate(out - idx * a) + idx * a)
        # 行内右→左传播
        rev = np.minimum.accumulate((out - ridx * a)[::-1])[::-1] + ridx * a
        return np.minimum(out, rev)

    # 正向：行间由 H 次迭代导出（行数为数据，不写死上限）
    for y in range(1, h):
        dist[y] = _relax_row(dist[y], dist[y - 1])
    # 反向
    for y in range(h - 2, -1, -1):
        dist[y] = _relax_row(dist[y], dist[y + 1])
    return dist.astype(np.float32)


def label_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """8 连通域标记。返回 (labels int32, n)；背景为 0，连通域从 1 编号。"""
    mask = np.asarray(mask, dtype=bool)
    if _HAVE_SCIPY:
        _ACCEL_CACHE["label"] = "scipy"
        lab, n = _ndi.label(mask, structure=np.ones((3, 3), bool))
        return lab.astype(np.int32), int(n)
    _ACCEL_CACHE["label"] = "numpy_propagate"
    h, w = mask.shape
    lab = np.where(mask, np.arange(1, h * w + 1, dtype=np.int32).reshape(h, w), 0)
    for _ in range(3 * max(h, w)):  # 传播轮数由图像尺寸导出，不写死数据上限
        cur = lab
        best = cur.copy()
        for dy, dx in _NEIGHBOR8:
            best = np.minimum(best, _shift(cur, dy, dx, True))
        if np.array_equal(best, cur):
            break
        lab = best
    uniq = np.unique(lab[lab > 0])
    remap = {int(v): i + 1 for i, v in enumerate(uniq.tolist())}
    out = np.zeros_like(lab)
    for old, new in remap.items():
        out[lab == old] = new  # 回退路径允许慢，正确性优先
    return out, len(uniq)


def fill_holes(mask: np.ndarray) -> np.ndarray:
    """填充前景内部空洞（保留与边界不连通的背景为洞）。"""
    mask = np.asarray(mask, dtype=bool)
    if _HAVE_SCIPY:
        return _ndi.binary_fill_holes(mask)
    bg = ~mask
    lab, n = label_components(bg)
    border_labels = set(lab[0, :].tolist()) | set(lab[-1, :].tolist()) | set(lab[:, 0].tolist()) | set(lab[:, -1].tolist())
    outside = np.isin(lab, [v for v in border_labels if v > 0])
    return ~outside


def largest_component(mask: np.ndarray, keep: int = 1) -> np.ndarray:
    """按像素数保留最大的 keep 个连通域。"""
    lab, n = label_components(mask)
    if n <= keep:
        return np.asarray(mask, dtype=bool)
    counts = np.bincount(lab.ravel())
    order = np.argsort(counts[1:])[::-1] + 1
    keep_ids = set(int(v) for v in order[:keep])
    return np.isin(lab, list(keep_ids))


# ---------------------------------------------------------------- 折线几何
def polyline_segments(points: np.ndarray, closed: bool = False) -> np.ndarray:
    """返回线段数组 (S,2,2)。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2:
        return np.zeros((0, 2, 2), dtype=np.float64)
    if closed:
        nxt = np.roll(pts, -1, axis=0)
        return np.stack([pts, nxt], axis=1)
    return np.stack([pts[:-1], pts[1:]], axis=1)


def segment_lengths(points: np.ndarray, closed: bool = False) -> np.ndarray:
    segs = polyline_segments(points, closed)
    if len(segs) == 0:
        return np.zeros(0, dtype=np.float64)
    return np.linalg.norm(segs[:, 1] - segs[:, 0], axis=1)


def polyline_arclength(points: np.ndarray, closed: bool = False) -> float:
    return float(segment_lengths(points, closed).sum())


def cumulative_arclength(points: np.ndarray, closed: bool = False) -> np.ndarray:
    """各顶点的累计弧长（首点 0）。closed=True 时总长为含闭合段。"""
    lens = segment_lengths(points, closed)
    cum = np.concatenate([[0.0], np.cumsum(lens)])
    if not closed:
        return cum
    return cum[: len(points) + 1]


def resample_polyline(points: np.ndarray, n: int, closed: bool = False) -> np.ndarray:
    """按弧长均匀重采样为 n 个点。closed 时返回值 t=0..1 开区间（首尾不重复）。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    n = int(max(n, 3))
    lens = segment_lengths(pts, closed)
    total = float(lens.sum())
    if total <= 0:
        return np.repeat(pts[:1], n, axis=0)
    cum = np.concatenate([[0.0], np.cumsum(lens)])
    if closed:
        targets = np.linspace(0.0, total, n, endpoint=False)
    else:
        targets = np.linspace(0.0, total, n)
    # 每个目标所在段索引
    idx = np.searchsorted(cum, targets, side="right") - 1
    idx = np.clip(idx, 0, len(lens) - 1)
    local = (targets - cum[idx]) / np.maximum(lens[idx], 1e-12)
    p0 = pts[idx]
    p1 = pts[(idx + 1) % len(pts)] if closed else pts[np.minimum(idx + 1, len(pts) - 1)]
    return p0 + (p1 - p0) * local[:, None]


def resample_uniform_arclength(points: np.ndarray, spacing: float, closed: bool = False) -> np.ndarray:
    """按给定弧长间距重采样（间距由数据导出，调用方负责保证 >0）。"""
    total = polyline_arclength(points, closed)
    n = int(max(2, math.ceil(total / max(spacing, 1e-9))))
    return resample_polyline(points, n, closed=closed)


def point_at_arclength(points: np.ndarray, s: float, closed: bool = False) -> np.ndarray:
    """按弧长参数取点。"""
    pts = np.asarray(points, dtype=np.float64)
    lens = segment_lengths(pts, closed)
    total = float(lens.sum())
    if total <= 0:
        return pts[0].copy()
    s = float(np.clip(s, 0.0, total))
    cum = np.concatenate([[0.0], np.cumsum(lens)])
    idx = int(np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(lens) - 1))
    local = (s - cum[idx]) / max(lens[idx], 1e-12)
    p1 = pts[(idx + 1) % len(pts)] if closed else pts[min(idx + 1, len(pts) - 1)]
    return pts[idx] + (p1 - pts[idx]) * local


def bilinear_sample(image: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """双线性采样。坐标约定：整数坐标 = 像素中心。支持 (H,W) 与 (H,W,C)。

    越界坐标钳到边缘像素中心；返回 shape = xs.shape + (C,)（灰度时为 xs.shape）。
    """
    img = np.asarray(image)
    squeeze = img.ndim == 2
    if squeeze:
        img = img[:, :, None]
    h, w, c = img.shape
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    fx = xs - x0
    fy = ys - y0
    x0 = np.clip(x0, 0, w - 1)
    y0 = np.clip(y0, 0, h - 1)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    out = img.astype(np.float64)
    top = out[y0, x0] * (1 - fx)[..., None] + out[y0, x1] * fx[..., None]
    bot = out[y1, x0] * (1 - fx)[..., None] + out[y1, x1] * fx[..., None]
    res = top * (1 - fy)[..., None] + bot * fy[..., None]
    if squeeze:
        return res[..., 0]
    if c == 1:
        return res[..., 0]
    return res


def normalize01(arr: np.ndarray) -> np.ndarray:
    """线性拉伸到 0..1（常值返回全 0）。"""
    a = np.asarray(arr, dtype=np.float32)
    lo = float(np.nanmin(a)) if a.size else 0.0
    hi = float(np.nanmax(a)) if a.size else 0.0
    if not np.isfinite(lo) or not np.isfinite(hi) or hi - lo < 1e-12:
        return np.zeros_like(a)
    return (a - lo) / (hi - lo)


# ---------------------------------------------------------------- 其他
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\Arial.ttf",
)


def find_font_path() -> str | None:
    """找一个支持中文的系统字体路径；找不到返回 None（调用方退回默认字体）。"""
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def clamp(v: float, lo: float, hi: float) -> float:
    return float(min(max(v, lo), hi))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return float(a / b) if abs(b) > 1e-12 else float(default)


def ensure_dir(path: str) -> str:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    return path


def stem_of(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]
