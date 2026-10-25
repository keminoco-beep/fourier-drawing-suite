"""目录/校准数据生成器（开发期工具，不参与运行时渲染）。

把两份"上游事实"合成 ``assets/brush/`` 下的两个数据文件：

1. ``fourier-brushstroke/scripts/fbs_core/brushlib.py`` 的 20 个预设参数
   （经 ``poc/brushcalib/out/brush_calib_measured.json`` 转存，避免本包 import skill）；
2. 同一份实测 JSON 里的尺寸×压力响应、有效宽、各向异性、α 阶梯。

生成命令（仓库根目录执行）::

    python -m fourier_prompt.brush._build \
        --measured "../poc/brushcalib/out/brush_calib_measured.json" \
        --assets assets/brush

输出（确定性：同输入 → 同字节；``generated`` 取实测文件的日期，不含运行时刻）：
- ``assets/brush/brush_catalog.json``：20 active + 10 planned 预设
- ``assets/brush/brush_calib.json``：preset→sizes→pressure→width/alpha/width_eff/traits

``_build`` 只写这两个文件，绝不改 skill。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
from pathlib import Path

CATALOG_VERSION = "brush-catalog/1"
CALIB_VERSION = "brush-calib/1"

# 每个 active 预设的编辑口径（角色/不透明度/层/尺寸范围/中文名）。
# 数值型参数（size_mult/alpha_range/jitter/pressure/width_eff/实测响应）**不写在这里**，
# 一律从实测 JSON 取，避免手抄漂移。
PRESET_META: dict[str, dict] = {
    "basic_round_hard": {
        "name_zh": "基础硬圆笔",
        "roles": ["bed", "marker"],
        "default_opacity": 0.95,
        "recommended_layers": ["F1", "L26", "L13"],
        "width_range_px": [2, 160],
        "usage": "硬边铺底/平涂；压力主要改宽度，靠叠笔加深",
    },
    "basic_round_soft": {
        "name_zh": "基础柔边圆笔",
        "roles": ["bed", "wash", "effect"],
        "default_opacity": 0.90,
        "recommended_layers": ["F1", "L52", "L26"],
        "width_range_px": [3, 160],
        "usage": "柔和过渡/大色块；有效宽 0.75，名义尺寸要放大 1.33 倍",
    },
    "basic_airbrush": {
        "name_zh": "柔性喷枪",
        "roles": ["effect", "wash"],
        "default_opacity": 0.75,
        "recommended_layers": ["L26", "L13"],
        "width_range_px": [6, 200],
        "usage": "雾化/氛围/柔光；有效宽仅 0.33，禁止当铺底笔用",
    },
    "pencil_2b": {
        "name_zh": "2B 铅笔",
        "roles": ["sketch", "detail", "texture"],
        "default_opacity": 0.75,
        "recommended_layers": ["L5", "L7", "X1F"],
        "width_range_px": [1, 24],
        "usage": "起形/细节/轻纹理；压力同时降宽降浓",
    },
    "pencil_hb": {
        "name_zh": "HB 硬铅笔",
        "roles": ["sketch", "detail"],
        "default_opacity": 0.70,
        "recommended_layers": ["L5", "X1F"],
        "width_range_px": [1, 18],
        "usage": "精细起形；比 2B 更细更淡",
    },
    "ink_fineliner": {
        "name_zh": "针管笔",
        "roles": ["line_hairline", "detail"],
        "default_opacity": 1.0,
        "recommended_layers": ["L5", "L7", "X1F"],
        "width_range_px": [1, 16],
        "usage": "等宽细线/轮廓；压力响应最弱（W−），适合稳定线",
    },
    "ink_gpen": {
        "name_zh": "漫画 G 笔",
        "roles": ["line_taper", "line_hairline", "detail"],
        "default_opacity": 1.0,
        "recommended_layers": ["L7", "L13", "X1F"],
        "width_range_px": [1, 24],
        "usage": "硬边墨线/提按；α 几乎不随压力（A−），别用它做浓淡",
    },
    "ink_brush_rough": {
        "name_zh": "毛笔勾线",
        "roles": ["line_taper", "line_broad", "texture"],
        "default_opacity": 1.0,
        "recommended_layers": ["L7", "L13", "X1M"],
        "width_range_px": [2, 40],
        "usage": "毛笔勾线/飞白；尺寸偏小（4.2px 级），宽笔请换 sumi_e",
    },
    "sumi_e": {
        "name_zh": "写意水墨",
        "roles": ["line_broad", "line_taper", "wash", "bed"],
        "default_opacity": 0.95,
        "recommended_layers": ["L26", "L52", "X1C"],
        "width_range_px": [4, 180],
        "usage": "写意水墨/宽墨块；压力动态范围最大（W+ 60%），一笔出粗细",
    },
    "marker_chisel": {
        "name_zh": "方头马克笔",
        "roles": ["marker", "bed"],
        "default_opacity": 0.90,
        "recommended_layers": ["L13", "L26", "F1"],
        "width_range_px": [4, 96],
        "usage": "块面/平涂；刀口固定世界 X 轴 → 竖笔宽 1.6 倍，只做横/斜扫",
    },
    "marker_alcohol": {
        "name_zh": "酒精马克笔",
        "roles": ["marker", "bed", "wash"],
        "default_opacity": 0.80,
        "recommended_layers": ["L13", "L26", "F1"],
        "width_range_px": [4, 96],
        "usage": "半透明叠色/块面；低 α 叠加出混色感，方向性同 chisel",
    },
    "bristle_oil": {
        "name_zh": "油画鬃毛",
        "roles": ["bed", "wash", "texture", "line_broad"],
        "default_opacity": 0.95,
        "recommended_layers": ["L26", "L52", "F1"],
        "width_range_px": [4, 160],
        "usage": "厚涂/覆盖/条纹笔触；覆盖力强，可做宽笔写意",
    },
    "dry_flat": {
        "name_zh": "干画平头刷",
        "roles": ["bed", "texture", "marker"],
        "default_opacity": 0.95,
        "recommended_layers": ["L13", "L26", "F1"],
        "width_range_px": [4, 120],
        "usage": "干笔铺色/边缘干涩；平头方向性敏感",
    },
    "palette_knife": {
        "name_zh": "刮刀",
        "roles": ["texture", "bed", "marker"],
        "default_opacity": 1.0,
        "recommended_layers": ["L7", "L13", "L26"],
        "width_range_px": [6, 160],
        "usage": "厚涂/长条肌理；硬边 + 高覆盖，压力只轻微改宽",
    },
    "chalk": {
        "name_zh": "粉笔",
        "roles": ["texture", "sketch"],
        "default_opacity": 0.90,
        "recommended_layers": ["L13", "L26", "L7"],
        "width_range_px": [3, 120],
        "usage": "颗粒/毛边；颗粒只在边缘（内部被 max(cov) 抹平），别承诺内部质感",
    },
    "charcoal": {
        "name_zh": "炭笔",
        "roles": ["texture", "sketch", "line_taper"],
        "default_opacity": 0.95,
        "recommended_layers": ["L13", "L26", "L7"],
        "width_range_px": [3, 120],
        "usage": "更黑更粗的炭条/飞白；提按强（W+）",
    },
    "pastel": {
        "name_zh": "粉彩",
        "roles": ["texture", "sketch"],
        "default_opacity": 0.85,
        "recommended_layers": ["L13", "L26"],
        "width_range_px": [3, 120],
        "usage": "柔和颗粒/色彩轻微变化；低 α 适合叠加",
    },
    "wet_oil": {
        "name_zh": "湿画油彩",
        "roles": ["wash", "texture", "bed"],
        "default_opacity": 0.85,
        "recommended_layers": ["L13", "L26"],
        "width_range_px": [3, 120],
        "usage": "半透明湿画/混色感（无真实 smudge）；低 α 叠扫",
    },
    "watercolor_wash": {
        "name_zh": "水彩淡彩",
        "roles": ["wash", "effect", "bed"],
        "default_opacity": 0.90,
        "recommended_layers": ["L52", "L26", "F1"],
        "width_range_px": [4, 160],
        "usage": "淡彩/水洗；峰值 α 仅 0.70，边缘沉积（rim）可见",
    },
    "spatter_brush": {
        "name_zh": "喷点笔",
        "roles": ["effect", "texture"],
        "default_opacity": 0.70,
        "recommended_layers": ["L13", "L26"],
        "width_range_px": [3, 120],
        "usage": "稀疏喷点/肌理点缀；α 增益最高（A+ 0.23），低 α 多次叠加",
    },
}

# 8–12 个 planned 预设（status="planned"，无 skill 渲染实现，policy 永不选中）。
# 数值字段从 base 预设继承（标注 estimated），只写"想要什么"与缺失原因。
PLANNED_PRESETS: list[dict] = [
    {
        "id": "ink_ballpen",
        "name_zh": "圆珠笔",
        "base": "ink_fineliner",
        "shape": "round",
        "roles": ["line_hairline", "detail", "sketch"],
        "default_opacity": 1.0,
        "recommended_layers": ["L5", "X1F"],
        "width_range_px": [1, 12],
        "planned_reason": "需要油墨断续/轻微积墨；现 round 内核过于干净（等宽实心）",
    },
    {
        "id": "pencil_4b",
        "name_zh": "4B 软铅笔",
        "base": "pencil_2b",
        "shape": "pencil",
        "roles": ["sketch", "detail", "texture"],
        "default_opacity": 0.80,
        "recommended_layers": ["L5", "L7", "X1F"],
        "width_range_px": [1, 26],
        "overrides": {"size_mult": 0.62, "alpha_range": [0.62, 0.90], "width_jitter": 0.26},
        "planned_reason": "需要更黑更宽的软铅芯与磨损笔尖；现 pencil 参数固定（2B 黑度）",
    },
    {
        "id": "pencil_tilted",
        "name_zh": "侧锋铅笔",
        "base": "pencil_2b",
        "shape": "pencil_tilt",
        "roles": ["texture", "sketch", "bed"],
        "default_opacity": 0.70,
        "recommended_layers": ["L7", "L13", "L26"],
        "width_range_px": [3, 60],
        "planned_reason": "需要笔尖倾斜 → 宽扁椭圆截面；现 marker 刀口是固定 1.6 的近似",
    },
    {
        "id": "chalk_grainy",
        "name_zh": "颗粒粉笔",
        "base": "chalk",
        "shape": "chalk_grain",
        "roles": ["texture", "effect"],
        "default_opacity": 0.85,
        "recommended_layers": ["L13", "L26"],
        "width_range_px": [3, 120],
        "planned_reason": "需要 per-stamp 颗粒累积；现纹理被 max(cov) 抹平（13a §2.5），内部无颗粒",
    },
    {
        "id": "marker_dry",
        "name_zh": "干涩马克笔",
        "base": "marker_alcohol",
        "shape": "marker",
        "roles": ["marker", "texture"],
        "default_opacity": 0.88,
        "recommended_layers": ["L13", "L26"],
        "width_range_px": [4, 96],
        "planned_reason": "需要边缘断续/干笔尾迹；现 marker 内核为硬边均匀",
    },
    {
        "id": "bristle_detail",
        "name_zh": "细鬃毛",
        "base": "bristle_oil",
        "shape": "bristle",
        "roles": ["texture", "detail", "line_taper"],
        "default_opacity": 0.90,
        "recommended_layers": ["L7", "L13"],
        "width_range_px": [2, 32],
        "overrides": {"size_mult": 0.55, "alpha_range": [0.72, 0.95]},
        "planned_reason": "需要小尺寸仍保留鬃毛纹理；现 bristle_oil 尺寸偏大（≥4px 才有条纹）",
    },
    {
        "id": "wet_paint",
        "name_zh": "湿颜料",
        "base": "wet_oil",
        "shape": "wet_paint",
        "roles": ["wash", "bed", "texture"],
        "default_opacity": 0.85,
        "recommended_layers": ["L13", "L26", "L52"],
        "width_range_px": [4, 140],
        "planned_reason": "需要湿边混色/颜料堆积；现渲染无 smudge（只有半透明叠加）",
    },
    {
        "id": "watercolor_fringe",
        "name_zh": "水彩晕边",
        "base": "watercolor_wash",
        "shape": "watercolor_fringe",
        "roles": ["wash", "effect"],
        "default_opacity": 0.90,
        "recommended_layers": ["L26", "L52"],
        "width_range_px": [6, 160],
        "planned_reason": "需要真实边缘沉积（rim 增强）与二次晕染；现 watercolor 只有 rim_ratio 0.7 级",
    },
    {
        "id": "dry_bristle",
        "name_zh": "干鬃毛",
        "base": "bristle_oil",
        "shape": "bristle",
        "roles": ["texture", "bed", "line_broad"],
        "default_opacity": 0.90,
        "recommended_layers": ["L13", "L26"],
        "width_range_px": [4, 120],
        "overrides": {"alpha_range": [0.55, 0.85], "width_jitter": 0.26},
        "planned_reason": "需要沿笔触方向的鬃毛间隙（飞白）；现 bristle 横向条纹不沿笔画断开",
    },
    {
        "id": "fill_round",
        "name_zh": "填色圆头",
        "base": "basic_round_hard",
        "shape": "round",
        "roles": ["bed", "marker"],
        "default_opacity": 1.0,
        "recommended_layers": ["F1", "L26"],
        "width_range_px": [8, 220],
        "planned_reason": "大尺寸铺底专用：需要 ≥180px 稳定覆盖与低抖动（现宽度抖动 0.10 在大尺寸可见）",
    },
    {
        "id": "airbrush_pressure",
        "name_zh": "压感喷枪",
        "base": "basic_airbrush",
        "shape": "airbrush",
        "roles": ["effect", "wash"],
        "default_opacity": 0.80,
        "recommended_layers": ["L26", "L13"],
        "width_range_px": [6, 200],
        "planned_reason": "需要压力→流量（浓度）映射；现在压力只改宽，峰值 α 永不饱和（0.74 vs 请求 0.80）",
    },
]

LICENSE = "proprietary"
SIZES_PX = [4, 8, 16, 32, 64, 120]
PRESSURE_KEYS = ("0.25", "0.5", "0.75", "1.0")

# planned 条目的英文显示名（id 仍是唯一键；显示层不参与任何查表）
NAME_EN_PLANNED: dict[str, str] = {
    "ink_ballpen": "Ballpoint Pen",
    "pencil_4b": "Pencil 4B",
    "pencil_tilted": "Tilted Pencil",
    "chalk_grainy": "Grainy Chalk",
    "marker_dry": "Dry Marker",
    "bristle_detail": "Detail Bristle",
    "wet_paint": "Wet Paint",
    "watercolor_fringe": "Watercolor Fringe",
    "dry_bristle": "Dry Bristle",
    "fill_round": "Fill Round",
    "airbrush_pressure": "Pressure Airbrush",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _r(v: float, nd: int = 4) -> float:
    return round(float(v), nd)


def _pkey(p: float) -> str:
    """压力键：0.25/0.5/0.75/1.0（保留 ``.0``，便于人读/对照契约）。"""
    return repr(float(p))


def _skey(size: float) -> str:
    """尺寸键：整数就写整数（4/8/12/120），非整数保留小数。"""
    size = float(size)
    return str(int(size)) if abs(size - int(size)) < 1e-9 else repr(size)


def _width_eff(preset: dict, sizes_scaling: dict) -> tuple[float, str]:
    rows = ((sizes_scaling or {}).get(preset["id"]) or {}).get("rows") or []
    if rows:
        return _r(statistics.median(r["measured"]["width_eff_ratio"] for r in rows)), "size_scaling_median"
    vals = [r["measured"]["width_eff_ratio"] for r in preset["size_12"]]
    return _r(statistics.median(vals)), "size_12_median"


def _pressure_curves(preset: dict) -> dict:
    """由实测 size_12 的 4 个压力点生成 width（归一化因子）与 alpha 曲线。"""
    rows = sorted(preset["size_12"], key=lambda r: r["pressure"])
    w1 = float(rows[-1]["measured"]["width_alpha_px"])
    width = [[float(r["pressure"]), _r(r["measured"]["width_alpha_px"] / w1)] for r in rows]
    alpha = [[float(r["pressure"]), _r(r["measured"]["peak_alpha"])] for r in rows]
    return {"width": width, "alpha": alpha}


def _note(pid: str, preset: dict, meta: dict) -> str:
    tags = " ".join(preset.get("tags") or [])
    resp = preset["response"]
    return (
        f"{meta['usage']}；实测标签 {tags}，宽度压力增益 {resp['width_gain_pct'] * 100:.0f}%，"
        f"α 增益 {resp['alpha_gain_abs']:.2f}（p0.25→1.0）"
    )


def build_catalog(measured: dict, measured_path: Path) -> dict:
    sizes_scaling = measured.get("size_scaling") or {}
    presets: dict[str, dict] = {}
    for pid, preset in measured["presets"].items():
        meta = PRESET_META[pid]
        weff, weff_src = _width_eff(preset, sizes_scaling)
        aniso = (measured.get("marker_anisotropy") or {}).get(pid)
        curves = _pressure_curves(preset)
        entry = {
            "id": pid,
            "name": preset["name"],
            "name_en": preset["name"],
            "name_zh": meta["name_zh"],
            "category": preset["category"],
            "shape": preset["shape"],
            "status": "active",
            "renderable": True,
            "sizes_px": list(SIZES_PX),
            "width_eff": weff,
            "width_eff_source": weff_src,
            "width_range_px": list(meta["width_range_px"]),
            "pressure": {
                "model": (
                    "width: 1 - sensitivity*(1-p)（skill 公式；实测 R²>=0.9999）；"
                    "alpha: alpha_lo + (alpha_hi-alpha_lo)*p（实测峰值 α，color.a 已归一到 1.0）"
                ),
                "sensitivity": float(preset["pressure"]),
                "width": curves["width"],
                "alpha": curves["alpha"],
            },
            "size_mult": float(preset["size_mult"]),
            "alpha_range": [float(v) for v in preset["alpha_range"]],
            "width_jitter": float(preset["width_jitter"]),
            "color_jitter": float(preset["color_jitter"]),
            "length_mult": float(preset["length_mult"]),
            "roles": list(meta["roles"]),
            "default_opacity": float(meta["default_opacity"]),
            "recommended_layers": list(meta["recommended_layers"]),
            "preview": f"previews/{pid}.png",
            "license": LICENSE,
            "version": 1,
            "traits": list(preset.get("tags") or []),
            "calib": "measured",
            "notes": _note(pid, preset, meta),
        }
        if aniso:
            entry["anisotropy"] = _r((aniso.get("vertical") or {}).get("width_alpha_px", 0.0)
                                     / max((aniso.get("horizontal") or {}).get("width_alpha_px", 1.0), 1e-9), 3)
        presets[pid] = entry

    # ---- planned：从 base 继承数值，显式标注 estimated / renderable=false
    for spec in PLANNED_PRESETS:
        base = measured["presets"][spec["base"]]
        base_entry = presets[spec["base"]]
        overrides = spec.get("overrides") or {}
        curves = _pressure_curves(base)
        presets[spec["id"]] = {
            "id": spec["id"],
            "name": NAME_EN_PLANNED.get(spec["id"], spec["id"]),
            "name_en": NAME_EN_PLANNED.get(spec["id"], spec["id"]),
            "name_zh": spec["name_zh"],
            "category": base_entry["category"],
            "shape": spec["shape"],
            "status": "planned",
            "renderable": False,
            "sizes_px": list(SIZES_PX),
            "width_eff": _r(overrides.get("width_eff", base_entry["width_eff"])),
            "width_eff_source": f"estimated_from:{spec['base']}",
            "width_range_px": list(spec["width_range_px"]),
            "pressure": {
                "model": f"estimated: 继承 {spec['base']} 的实测压力曲线（本笔尚未实现/未实测）",
                "sensitivity": float(base["pressure"]),
                "width": curves["width"],
                "alpha": curves["alpha"],
            },
            "size_mult": float(overrides.get("size_mult", base["size_mult"])),
            "alpha_range": [float(v) for v in overrides.get("alpha_range", base["alpha_range"])],
            "width_jitter": float(overrides.get("width_jitter", base["width_jitter"])),
            "color_jitter": float(base["color_jitter"]),
            "length_mult": float(base["length_mult"]),
            "roles": list(spec["roles"]),
            "default_opacity": float(spec["default_opacity"]),
            "recommended_layers": list(spec["recommended_layers"]),
            "preview": f"previews/{spec['id']}.png",
            "license": LICENSE,
            "version": 1,
            "traits": [],
            "calib": "estimated",
            "implements_base": spec["base"],
            "shape_registered": spec["shape"] in (
                "round", "soft", "marker", "bristle", "chalk", "spatter", "airbrush", "pencil", "watercolor", "ink"
            ),
            "notes": f"planned 条目：{spec['planned_reason']}",
        }

    active = [pid for pid, e in presets.items() if e["status"] == "active"]
    planned = [pid for pid, e in presets.items() if e["status"] == "planned"]
    return {
        "schema": CATALOG_VERSION,
        "catalog_version": CATALOG_VERSION,
        "generated": str(measured.get("generated", ""))[:10],
        "generated_from": "poc/brushcalib/out/brush_calib_measured.json",
        "generated_from_sha256": _sha256(measured_path),
        "generated_by": (
            "python -m fourier_prompt.brush._build --measured "
            "poc/brushcalib/out/brush_calib_measured.json --assets fourier-prompt/assets/brush"
        ),
        "source_engine": "fourier-brushstroke/scripts/fbs_core (brushlib.py / shapelib.py，只读)",
        "counts": {"active": len(active), "planned": len(planned), "total": len(presets)},
        "engine_shapes": [
            "round", "soft", "marker", "bristle", "chalk",
            "spatter", "airbrush", "pencil", "watercolor", "ink",
        ],
        "roles": [
            "bed", "wash", "line_hairline", "line_taper", "line_broad",
            "detail", "texture", "effect", "marker", "sketch",
        ],
        "pressure_encoding": (
            "pressure.width/alpha = [[p, value], ...]，p 升序；width 为 p=1.0 归一化因子，"
            "alpha 为实测峰值 α（color.a 归一到 1.0 口径）"
        ),
        "notes": [
            "active 20 条的 id 与 skill brushlib 完全一致（不改名，兼容既有 .fbs/黄金回归）。",
            "planned 10 条没有 skill 渲染实现，renderable=false，policy 永不选中。",
            "width_eff = 名义宽 → 实际沉积宽因子（round/ink/marker/watercolor≈1.0、soft=0.749、airbrush=0.333），"
            "来源为 size_scaling 中位数或 size12 中位数（见 width_eff_source）。",
            "marker 家族 anisotropy≈1.6：平头刀口固定世界 X 轴，竖笔会粗 60%。",
        ],
        "presets": presets,
    }


def build_calib(measured: dict, measured_path: Path) -> dict:
    sizes_scaling = measured.get("size_scaling") or {}
    presets: dict[str, dict] = {}
    for pid, preset in measured["presets"].items():
        weff, weff_src = _width_eff(preset, sizes_scaling)
        resp = preset["response"]
        aniso = (measured.get("marker_anisotropy") or {}).get(pid)
        sizes: dict[str, dict] = {}
        size12: dict[str, dict] = {}
        for row in sorted(preset["size_12"], key=lambda r: r["pressure"]):
            key = _pkey(row["pressure"])
            size12[key] = {
                "width_px": _r(row["measured"]["width_alpha_px"]),
                "alpha": _r(row["measured"]["peak_alpha"]),
                "width_eff": _r(row["measured"]["width_eff_ratio"]),
                "nominal_px": _r(row["nominal_px"]),
            }
        sizes["12"] = {
            "size_px": 12.0,
            "source": "size_12（4 个压力点）",
            "nominal": {k: v.pop("nominal_px") for k, v in size12.items()},
            "pressure": size12,
        }
        wide = preset.get("wide_44") or {}
        if wide:
            sizes["44"] = {
                "size_px": 44.0,
                "source": "wide_44（p=1.0 宽笔）",
                "nominal": {"1.0": _r(wide["nominal_px"])},
                "pressure": {
                    "1.0": {
                        "width_px": _r(wide["measured"]["width_alpha_px"]),
                        "alpha": _r(wide["measured"]["peak_alpha"]),
                        "width_eff": _r(wide["measured"]["width_eff_ratio"]),
                    }
                },
            }
        wash = preset.get("wash") or {}
        if wash:
            sizes["48"] = {
                "size_px": 48.0,
                "source": "wash（3 笔 48px、间距 24px 叠加，测叠加后覆盖）",
                "nominal": {"1.0": _r(wash["nominal_px"])},
                "pressure": {
                    "1.0": {
                        "width_px": _r(wash["measured"]["width_alpha_px"]),
                        "alpha": _r(wash["measured"]["peak_alpha"]),
                        "width_eff": _r(wash["measured"]["width_eff_ratio"]),
                    }
                },
            }
        for row in ((sizes_scaling.get(pid) or {}).get("rows") or []):
            sizes[_skey(row["size_px"])] = {
                "size_px": float(row["size_px"]),
                "source": "size_scaling（p=1.0 单点，仅测尺寸线性）",
                "nominal": {"1.0": _r(row["nominal_px"])},
                "pressure": {
                    "1.0": {
                        "width_px": _r(row["measured"]["width_alpha_px"]),
                        "alpha": _r(row["measured"]["peak_alpha"]),
                        "width_eff": _r(row["measured"]["width_eff_ratio"]),
                    }
                },
            }
        presets[pid] = {
            "id": pid,
            "name": preset["name"],
            "category": preset["category"],
            "shape": preset["shape"],
            "size_mult": float(preset["size_mult"]),
            "alpha_range": [float(v) for v in preset["alpha_range"]],
            "pressure_sensitivity": float(preset["pressure"]),
            "width_eff": weff,
            "width_eff_source": weff_src,
            "traits": list(preset.get("tags") or []),
            "traits_derived": {
                "width_gain_pct": _r(resp["width_gain_pct"]),
                "alpha_gain_abs": _r(resp["alpha_gain_abs"]),
                "width_linear_r2": _r(resp["width_linear_r2"]),
                "width_slope_px_per_p": _r(resp["width_slope_px_per_p"]),
                "ink_yield": _r(preset["size_12"][-1]["measured"]["ink_yield"]),
                "grain_cv": _r(preset["size_12"][-1]["measured"]["local_cv"]),
                "grain_cv_edge": _r(preset["size_12"][-1]["measured"]["grain_cv_edge"]),
                "edge_soft_ratio": _r(preset["size_12"][-1]["measured"]["edge_soft_ratio"]),
                "rim_ratio": _r(preset["size_12"][-1]["measured"]["rim_ratio"]),
                "anisotropy_ratio": _r(
                    (aniso.get("vertical") or {}).get("width_alpha_px", 0.0)
                    / max((aniso.get("horizontal") or {}).get("width_alpha_px", 1.0), 1e-9), 3
                ) if aniso else 1.0,
            },
            "sizes": sizes,
        }
        if aniso:
            presets[pid]["anisotropy"] = {
                "nominal_px": _r(aniso["nominal_px"]),
                "horizontal_width_px": _r(aniso["horizontal"]["width_alpha_px"]),
                "vertical_width_px": _r(aniso["vertical"]["width_alpha_px"]),
                "ratio": _r(aniso["vertical"]["width_alpha_px"] / max(aniso["horizontal"]["width_alpha_px"], 1e-9), 3),
            }

    assumptions = measured.get("assumptions") or {}
    keep = (
        "pressure_axis", "width_mapping", "alpha_mapping", "taper",
        "measurement_window", "width_area_px", "peak_alpha", "ink_yield",
    )
    engine = measured.get("engine_truth") or {}
    return {
        "schema": CALIB_VERSION,
        "generated_from": "poc/brushcalib/out/brush_calib_measured.json",
        "generated_from_sha256": _sha256(measured_path),
        "generated_by": (
            "python -m fourier_prompt.brush._build --measured "
            "poc/brushcalib/out/brush_calib_measured.json --assets fourier-prompt/assets/brush"
        ),
        "generated": str(measured.get("generated", ""))[:10],
        "source_schema": measured.get("schema"),
        "source_target": measured.get("target"),
        "measurement": {
            "canvas": measured.get("canvas"),
            "engine": {"mode": engine.get("mode"), "supersample": engine.get("supersample")},
            **{k: assumptions[k] for k in keep if k in assumptions},
        },
        "alpha_normalization": (
            "峰值 α 是在把 style.color.stops[].a 归一到 1.0 后测得；默认 build_brush 会把 α "
            "同时写进 color.a 与 style.alpha，渲染相乘 → 实测≈请求²（见 alpha_ladder）"
        ),
        "presets": presets,
        "alpha_ladder": measured.get("alpha_ladder"),
        "roundtrip_fbs": measured.get("roundtrip_fbs"),
        "notes": [
            "压力→宽度 1:1 线性（20 预设 R²=0.9999–1.0000），故预设在曲线域直接写 1-sens*(1-p)。",
            "尺寸 4→120px 线性（斜率 0.999–1.002）：response() 用名义宽比例把 12px 档外推到任意尺寸。",
            "纹理由 max(cov) 在笔触内部抹平（核心带 CV 0.006–0.041），颗粒主要在边缘。",
            "marker 刀口方向固定世界 X 轴（竖笔 1.6 倍），anisotropy 段记录实测。",
        ],
    }


def _write_json(path: Path, data: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return len(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 brush_catalog.json / brush_calib.json")
    ap.add_argument("--measured", required=True, help="poc/brushcalib/out/brush_calib_measured.json")
    ap.add_argument("--assets", default="assets/brush", help="输出目录（默认 assets/brush）")
    args = ap.parse_args(argv)
    measured_path = Path(args.measured)
    if not measured_path.is_file():
        raise SystemExit(f"实测数据不存在: {measured_path}")
    measured = json.loads(measured_path.read_text(encoding="utf-8"))
    if measured.get("schema") != "brushcalib/1":
        raise SystemExit(f"不认识的实测 schema: {measured.get('schema')!r}")
    missing = [pid for pid in measured["presets"] if pid not in PRESET_META]
    if missing:
        raise SystemExit(f"PRESET_META 缺少实测预设的编辑口径: {missing}")
    out = Path(args.assets)
    cat = build_catalog(measured, measured_path)
    cal = build_calib(measured, measured_path)
    nb1 = _write_json(out / "brush_catalog.json", cat)
    nb2 = _write_json(out / "brush_calib.json", cal)
    print(f"wrote {out / 'brush_catalog.json'} ({nb1} bytes, "
          f"{cat['counts']['active']} active + {cat['counts']['planned']} planned)")
    print(f"wrote {out / 'brush_calib.json'} ({nb2} bytes, {len(cal['presets'])} presets)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
