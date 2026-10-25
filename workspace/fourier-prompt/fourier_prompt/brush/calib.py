"""笔刷校准表（Brush Calibration v1）：尺寸×压力 → 沉积宽/α 的实测响应。

数据来源：``poc/brushcalib/out/brush_calib_measured.json``（本仓库自己的渲染器实测，
见 ``analysis/13a_笔刷实测与校准数据.md``），由
``python -m fourier_prompt.brush._build`` 转换为 ``assets/brush/brush_calib.json``。

实测口径（关键，别忽略）：
- 宽度 = α 归一化几何宽 ``width_alpha_px``（墨量/列 ÷ 峰值 α），已扣掉笔形"有效宽"损失；
- α = 峰值 alpha，且是**把 ``color.stops[].a`` 归一到 1.0 之后**测的
  （默认 ``build_brush`` 会把 alphas 同时写进 color.a 与 style.alpha，渲染相乘 → 实测≈请求²）；
- 压力→宽度是 1:1 线性（20 预设 R²≥0.9999）；尺寸 4→120px 线性（斜率 0.999–1.002）。

本模块只用 stdlib。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .catalog import UnknownPresetError

CALIB_VERSION = "brush-calib/1"

# 压力轴（实测点）；插值/外推在两端夹紧
PRESSURE_POINTS: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)

DEFAULT_CALIB_PATH = Path(__file__).resolve().parents[2] / "assets" / "brush" / "brush_calib.json"


class BrushCalibError(ValueError):
    """校准表结构非法或使用方式错误（超出可表达范围等）。"""


def _interp(points: list[tuple[float, float]], x: float) -> tuple[float, bool]:
    """分段线性插值；返回 (values, extrapolated)。空表 → 报错（fail-loud）。"""
    if not points:
        raise BrushCalibError("压力曲线为空，无法求值")
    pts = sorted((float(p), float(v)) for p, v in points)
    if x <= pts[0][0]:
        return pts[0][1], x < pts[0][0]
    if x >= pts[-1][0]:
        return pts[-1][1], x > pts[-1][0]
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if p0 <= x <= p1:
            if p1 - p0 <= 1e-12:
                return v1, False
            t = (x - p0) / (p1 - p0)
            return v0 + (v1 - v0) * t, False
    return pts[-1][1], False  # pragma: no cover


class BrushCalib:
    """校准表只读视图 + 沉积响应求值。

    典型用法::

        cal = BrushCalib.load()
        r = cal.response("ink_gpen", 12, 0.8)   # {'width_px':..., 'alpha':..., 'width_eff':...}
        cal.width_eff("basic_airbrush")        # 0.333（名义宽 12px 只沉积 4px）
    """

    def __init__(self, data: dict, *, source: str | None = None) -> None:
        if not isinstance(data, dict):
            raise BrushCalibError(f"校准表必须是对象，收到 {type(data).__name__}")
        presets = data.get("presets")
        if not isinstance(presets, dict) or not presets:
            raise BrushCalibError("校准表缺少非空 presets 对象")
        self._data = data
        self._presets: dict[str, dict] = presets
        self.source = source or "<memory>"

    # ---------------------------------------------------------------- 载入
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "BrushCalib":
        p = Path(path) if path is not None else DEFAULT_CALIB_PATH
        if not p.is_file():
            raise FileNotFoundError(
                f"笔刷校准表不存在: {p}（默认路径 {DEFAULT_CALIB_PATH}）；"
                "请确认 assets/brush/brush_calib.json 已随包安装"
            )
        with open(p, "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:  # pragma: no cover - 防御性
                raise BrushCalibError(f"校准表 JSON 解析失败: {p}: {exc}") from exc
        return cls(data, source=str(p))

    @classmethod
    def from_dict(cls, data: dict) -> "BrushCalib":
        return cls(copy.deepcopy(data), source="<dict>")

    # ---------------------------------------------------------------- 读取
    @property
    def version(self) -> str:
        return str(self._data.get("schema") or "")

    def __len__(self) -> int:
        return len(self._presets)

    def __contains__(self, preset_id: object) -> bool:
        return isinstance(preset_id, str) and preset_id in self._presets

    def presets(self) -> list[str]:
        return list(self._presets.keys())

    def _entry(self, preset_id: str) -> dict:
        if not isinstance(preset_id, str):
            raise UnknownPresetError(str(preset_id), len(self._presets))
        entry = self._presets.get(preset_id)
        if entry is None:
            raise UnknownPresetError(preset_id, len(self._presets))
        return entry

    def width_eff(self, preset_id: str) -> float:
        """名义宽 → 实际沉积宽因子（round/ink≈1.0、soft=0.749、airbrush=0.333）。"""
        return float(self._entry(preset_id).get("width_eff"))

    def traits(self, preset_id: str) -> list[str]:
        """实测性格标签：``W+``/``W~``/``W-``、``A+``/``A~``/``A-`` 与 ``grain``/``rim``/``aniso`` 等。"""
        return list(self._entry(preset_id).get("traits") or [])

    def sizes(self, preset_id: str) -> list[float]:
        """该预设实测覆盖的 nominal size（px）。"""
        sizes = self._entry(preset_id).get("sizes") or {}
        return sorted(float(k) for k in sizes)

    def anisotropy(self, preset_id: str) -> float:
        """竖笔/横笔宽度比（1.0=各向同性；marker 家族实测 1.599–1.600）。"""
        entry = self._entry(preset_id)
        return float((entry.get("anisotropy") or {}).get("ratio", 1.0))

    def response(self, preset_id: str, size_px: float, pressure: float) -> dict:
        """返回给定尺寸/压力下的沉积响应（确定性；无随机）。

        契约签名：``response(preset_id, size_px, pressure)``。返回 dict：

        - ``width_px``：实测口径的沉积宽（α 归一化几何宽）；尺寸按名义宽线性缩放；
        - ``width_nominal_px``：名义宽 = ``size × size_mult × (1 − sens·(1−p))``；
        - ``alpha``：峰值 α（color.a 归一后的实测值口径）；
        - ``width_eff`` / ``traits`` / ``size_bucket`` / ``interpolated`` / ``extrapolated``。

        未知 preset → ``UnknownPresetError``；``size_px<=0`` → ``BrushCalibError``；
        ``pressure`` 超出 [0,1] → 夹紧并在返回里记 ``pressure_clamped=True``（不静默改写输入）。
        """
        entry = self._entry(preset_id)
        if not isinstance(size_px, (int, float)) or not (float(size_px) > 0):
            raise BrushCalibError(f"size_px 必须是正数，收到 {size_px!r}")
        p_raw = float(pressure)
        p = min(max(p_raw, 0.0), 1.0)
        size = float(size_px)

        bucket_key, bucket = self._pick_bucket(entry, size)
        w_pts = [(float(k), float(v["width_px"])) for k, v in bucket["pressure"].items()]
        a_pts = [(float(k), float(v["alpha"])) for k, v in bucket["pressure"].items()]
        width_at_bucket, extrap_w = _interp(w_pts, p)
        alpha, extrap_a = _interp(a_pts, p)

        size_mult = float(entry.get("size_mult", 1.0))
        sens = float(entry.get("pressure_sensitivity", 0.0))

        def nominal(px: float) -> float:
            return float(px) * size_mult * (1.0 - sens * (1.0 - p))

        nominal_at_size = nominal(size)
        nominal_at_bucket = nominal(bucket["size_px"])
        width_px = width_at_bucket * (nominal_at_size / max(nominal_at_bucket, 1e-9))
        exact = abs(size - float(bucket["size_px"])) < 1e-6 and any(
            abs(p - q) < 1e-9 for q in (float(k) for k in bucket["pressure"])
        )
        return {
            "preset_id": preset_id,
            "size_px": size,
            "pressure": p,
            "pressure_input": p_raw,
            "pressure_clamped": bool(abs(p - p_raw) > 1e-12),
            "size_bucket": float(bucket["size_px"]),
            "width_px": float(width_px),
            "width_nominal_px": float(nominal_at_size),
            "width_eff": float(entry.get("width_eff")),
            "alpha": float(alpha),
            "traits": list(entry.get("traits") or ()),
            "interpolated": not exact,
            "extrapolated": bool(extrap_w or extrap_a),
            "measured": exact,
            "source": str(entry.get("width_eff_source", "")),
        }

    def _pick_bucket(self, entry: dict, size: float) -> tuple[str, dict]:
        """选最合适的尺寸档：优先有 ≥2 个压力点的档，其次按名义宽比例最接近。"""
        sizes = entry.get("sizes") or {}
        cands: list[tuple[float, str, dict]] = []
        for key, bucket in sizes.items():
            pts = bucket.get("pressure") or {}
            if len(pts) >= 2:
                cands.append((float(bucket.get("size_px", key)), key, bucket))
        if not cands:
            raise BrushCalibError(f"预设 {entry.get('id')!r} 没有任何尺寸档带 ≥2 个压力点")
        # 名义宽线性 → 用 log 距离选档更稳（4 vs 12 与 120 vs 128 的差异口径一致）
        import math

        best = min(cands, key=lambda c: (abs(math.log(size / max(c[0], 1e-9))), c[0]))
        return best[1], best[2]

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    # ---------------------------------------------------------------- 校验
    def validate(self, *, catalog=None) -> list[str]:
        """返回错误列表（空=通过）。``catalog`` 可选：同时校验"目录↔校准"覆盖一致性。"""
        errs: list[str] = []
        if self._data.get("schema") != CALIB_VERSION:
            errs.append(f"schema 应为 {CALIB_VERSION!r}，实际 {self._data.get('schema')!r}")
        if not self._data.get("generated_from"):
            errs.append("缺少 generated_from（数据来源必须可追溯）")
        if not self._data.get("generated_by"):
            errs.append("缺少 generated_by（生成命令必须可复现）")
        active_ids: list[str] = []
        if catalog is not None:
            active_ids = catalog.ids()
            for pid in active_ids:
                if pid not in self._presets:
                    errs.append(f"目录 active 预设 {pid!r} 没有校准数据")
            for pid in self._presets:
                if pid not in catalog:
                    errs.append(f"校准表里的 {pid!r} 不在目录内")
        for pid, entry in self._presets.items():
            tag = f"presets[{pid}]"
            if entry.get("id") != pid:
                errs.append(f"{tag}.id 与键名不一致: {entry.get('id')!r}")
            weff = entry.get("width_eff")
            if not isinstance(weff, (int, float)) or not (0.05 <= float(weff) <= 1.05):
                errs.append(f"{tag}.width_eff 非法: {weff!r}")
            traits = entry.get("traits")
            if not isinstance(traits, list) or not traits:
                errs.append(f"{tag}.traits 必须是非空列表")
            sizes = entry.get("sizes")
            if not isinstance(sizes, dict) or not sizes:
                errs.append(f"{tag}.sizes 必须是非空对象")
                continue
            for skey, bucket in sizes.items():
                btag = f"{tag}.sizes[{skey}]"
                try:
                    float(skey)
                except (TypeError, ValueError):
                    errs.append(f"{btag} 尺寸键必须是数字字符串")
                pts = bucket.get("pressure")
                if not isinstance(pts, dict) or not pts:
                    errs.append(f"{btag}.pressure 必须是非空对象")
                    continue
                for pkey, vals in pts.items():
                    try:
                        pv = float(pkey)
                    except (TypeError, ValueError):
                        errs.append(f"{btag}.pressure 键必须是数字字符串: {pkey!r}")
                        continue
                    if not (0.0 <= pv <= 1.0):
                        errs.append(f"{btag}.pressure 压力越界: {pkey!r}")
                    for field in ("width_px", "alpha"):
                        v = vals.get(field)
                        if not isinstance(v, (int, float)):
                            errs.append(f"{btag}.pressure[{pkey}].{field} 必须是数字")
                        elif field == "alpha" and not (0.0 <= float(v) <= 1.0):
                            errs.append(f"{btag}.pressure[{pkey}].alpha 越界: {v!r}")
                        elif field == "width_px" and float(v) <= 0:
                            errs.append(f"{btag}.pressure[{pkey}].width_px 必须 >0: {v!r}")
        return errs

    def __repr__(self) -> str:  # pragma: no cover
        return f"BrushCalib(source={self.source!r}, presets={len(self._presets)})"
