"""笔刷库（brush library）：按现代绘图软件的笔刷分类安装可用的"画笔预设"。

调研来源（笔刷体系的事实依据；仅取分类/行为，不复制任何素材文件）：
- Krita 4 预设包分类：Basic / Pencils / Inking / Markers / Dry Painting /
  Chalk·Pastel·Charcoal / Wet Painting / Watercolors / Blender / Spray 等
  （https://docs.krita.org/en/reference_manual/krita_4_preset_bundle.html）；
- Krita 笔刷引擎：Pixel / Bristle / Chalk / Color Smudge / Spray / Particle 等
  （https://docs.krita.org/en/reference_manual/brushes/brush_engines.html）；
- Photoshop 笔刷预设家族（General/Bristle/Erodible/Airbrush/Wet Media/Special Effect）
  与动态项（Shape/Scatter/Texture/Color/Transfer）；
- Procreate 笔刷库分区（Sketching / Inking / Painting / Artistic / Texture …）；
- Corel Painter 笔刷类别（Acrylics / Oils / Watercolor / Palette Knives / Pastel …）。

本库把"笔刷差异"落到**傅里叶笔触可表达的属性**上：
`shape`（笔形内核）、`size_mult`（相对该层笔刷半径）、`alpha`（不透明度区间）、
`width_jitter`/`color_jitter`（动态抖动）、`pressure`（压力→粗细曲线形状）、
`length_mult`（运笔长度）。没有色块/纹理贴图，一切由曲线+属性表达。
"""
from __future__ import annotations

from .shapelib import VALID_SHAPES

# 笔刷预设：id -> 属性
# category 对应现代软件里的笔刷分区；shape 由 render 的笔形内核解释。
BRUSHES: dict[str, dict] = {
    # ---------------- Basic（基础圆/柔/喷枪）
    "basic_round_hard": {
        "name": "Basic Round Hard", "category": "basic", "shape": "round",
        "size_mult": 1.0, "alpha": (0.92, 1.0), "width_jitter": 0.10,
        "color_jitter": 0.006, "pressure": 0.55, "length_mult": 1.0,
        "notes": "基础硬圆笔；起稿/铺色",
    },
    "basic_round_soft": {
        "name": "Basic Round Soft", "category": "basic", "shape": "soft",
        "size_mult": 1.05, "alpha": (0.78, 0.95), "width_jitter": 0.12,
        "color_jitter": 0.008, "pressure": 0.65, "length_mult": 1.1,
        "notes": "基础柔边圆笔；大色块与过渡",
    },
    "basic_airbrush": {
        "name": "Airbrush Soft", "category": "airbrush", "shape": "airbrush",
        "size_mult": 1.5, "alpha": (0.50, 0.80), "width_jitter": 0.15,
        "color_jitter": 0.010, "pressure": 0.45, "length_mult": 1.3,
        "notes": "喷枪：极柔边、低不透明度；雾化/环境色",
    },
    # ---------------- Pencils（铅笔）
    "pencil_2b": {
        "name": "Pencil 2B", "category": "pencil", "shape": "pencil",
        "size_mult": 0.55, "alpha": (0.70, 0.92), "width_jitter": 0.22,
        "color_jitter": 0.012, "pressure": 0.35, "length_mult": 0.9,
        "notes": "铅笔：细、颗粒、压力敏感；草稿/细节",
    },
    "pencil_hb": {
        "name": "Pencil HB", "category": "pencil", "shape": "pencil",
        "size_mult": 0.42, "alpha": (0.65, 0.88), "width_jitter": 0.18,
        "color_jitter": 0.010, "pressure": 0.30, "length_mult": 0.8,
        "notes": "硬铅笔：更细更淡；起形",
    },
    # ---------------- Inking（勾线/墨）
    "ink_fineliner": {
        "name": "Ink Fineliner", "category": "inking", "shape": "round",
        "size_mult": 0.48, "alpha": (0.85, 1.0), "width_jitter": 0.06,
        "color_jitter": 0.004, "pressure": 0.25, "length_mult": 0.9,
        "notes": "针管笔：等宽细线；轮廓/排线",
    },
    "ink_gpen": {
        "name": "Ink GPen", "category": "inking", "shape": "ink",
        "size_mult": 0.50, "alpha": (0.95, 1.0), "width_jitter": 0.05,
        "color_jitter": 0.004, "pressure": 0.30, "length_mult": 1.0,
        "notes": "G 笔：硬边墨线、粗细随压力",
    },
    "ink_brush_rough": {
        "name": "Ink Brush Rough", "category": "inking", "shape": "bristle",
        "size_mult": 0.35, "alpha": (0.85, 1.0), "width_jitter": 0.12,
        "color_jitter": 0.006, "pressure": 0.45, "length_mult": 1.0,
        "notes": "毛笔勾线：鬃毛纹理、带飞白",
    },
    "sumi_e": {
        "name": "Sumi-e Brush", "category": "inking", "shape": "bristle",
        "size_mult": 1.2, "alpha": (0.70, 0.95), "width_jitter": 0.25,
        "color_jitter": 0.010, "pressure": 0.80, "length_mult": 1.6,
        "notes": "写意水墨：宽笔、强压力变化、鬃毛",
    },
    # ---------------- Markers（马克笔）
    "marker_chisel": {
        "name": "Marker Chisel", "category": "marker", "shape": "marker",
        "size_mult": 0.9, "alpha": (0.75, 0.92), "width_jitter": 0.10,
        "color_jitter": 0.006, "pressure": 0.40, "length_mult": 1.1,
        "notes": "方头马克笔：扁笔头、半透明叠加",
    },
    "marker_alcohol": {
        "name": "Marker Alcohol", "category": "marker", "shape": "marker",
        "size_mult": 1.1, "alpha": (0.55, 0.80), "width_jitter": 0.12,
        "color_jitter": 0.010, "pressure": 0.35, "length_mult": 1.2,
        "notes": "酒精马克笔：更透、叠加混色感",
    },
    # ---------------- Dry painting（干画法/油画/丙烯）
    "bristle_oil": {
        "name": "Bristle Oil", "category": "dry_painting", "shape": "bristle",
        "size_mult": 1.0, "alpha": (0.80, 0.97), "width_jitter": 0.18,
        "color_jitter": 0.014, "pressure": 0.60, "length_mult": 1.2,
        "notes": "油画鬃毛：条纹笔触、覆盖力强",
    },
    "dry_flat": {
        "name": "Dry Flat Brush", "category": "dry_painting", "shape": "marker",
        "size_mult": 1.15, "alpha": (0.82, 0.98), "width_jitter": 0.14,
        "color_jitter": 0.012, "pressure": 0.50, "length_mult": 1.3,
        "notes": "干画法平头刷：宽扁、边缘干涩",
    },
    "palette_knife": {
        "name": "Palette Knife", "category": "dry_painting", "shape": "marker",
        "size_mult": 1.25, "alpha": (0.90, 1.0), "width_jitter": 0.20,
        "color_jitter": 0.010, "pressure": 0.30, "length_mult": 1.5,
        "notes": "刮刀：厚涂、硬边、长条肌理",
    },
    # ---------------- Chalk / Pastel / Charcoal
    "chalk": {
        "name": "Chalk", "category": "chalk_pastel_charcoal", "shape": "chalk",
        "size_mult": 1.05, "alpha": (0.72, 0.95), "width_jitter": 0.28,
        "color_jitter": 0.020, "pressure": 0.55, "length_mult": 0.9,
        "notes": "粉笔：颗粒强、覆盖不均",
    },
    "charcoal": {
        "name": "Charcoal", "category": "chalk_pastel_charcoal", "shape": "chalk",
        "size_mult": 1.15, "alpha": (0.75, 0.98), "width_jitter": 0.30,
        "color_jitter": 0.022, "pressure": 0.60, "length_mult": 1.0,
        "notes": "炭笔：更黑更粗、飞白",
    },
    "pastel": {
        "name": "Pastel", "category": "chalk_pastel_charcoal", "shape": "chalk",
        "size_mult": 0.95, "alpha": (0.65, 0.90), "width_jitter": 0.24,
        "color_jitter": 0.018, "pressure": 0.50, "length_mult": 0.9,
        "notes": "粉彩：柔和颗粒、色彩轻微变化",
    },
    # ---------------- Wet painting / Watercolor
    "wet_oil": {
        "name": "Wet Oil", "category": "wet_painting", "shape": "bristle",
        "size_mult": 0.95, "alpha": (0.60, 0.85), "width_jitter": 0.16,
        "color_jitter": 0.016, "pressure": 0.55, "length_mult": 1.0,
        "notes": "湿画法油彩：半透明、与底层混色感（无真实smudge）",
    },
    "watercolor_wash": {
        "name": "Watercolor Wash", "category": "watercolor", "shape": "watercolor",
        "size_mult": 1.35, "alpha": (0.45, 0.70), "width_jitter": 0.18,
        "color_jitter": 0.014, "pressure": 0.45, "length_mult": 1.4,
        "notes": "水彩淡彩：低不透明度、颜料边缘沉积",
    },
    # ---------------- Texture / Spray
    "spatter_brush": {
        "name": "Spatter Spray", "category": "spray", "shape": "spatter",
        "size_mult": 1.05, "alpha": (0.50, 0.80), "width_jitter": 0.35,
        "color_jitter": 0.020, "pressure": 0.40, "length_mult": 0.7,
        "notes": "喷点：稀疏颗粒，点缀/肌理",
    },
}

# 各绘画层（由粗到细）允许的笔刷；顺序即抽签权重（越靠前越常用）
LAYER_BRUSHES: tuple[tuple[str, ...], ...] = (
    ("basic_round_soft", "bristle_oil", "dry_flat", "watercolor_wash"),
    ("bristle_oil", "dry_flat", "basic_round_soft", "marker_chisel", "wet_oil"),
    ("bristle_oil", "chalk", "dry_flat", "marker_alcohol", "pastel", "ink_gpen"),
    ("ink_gpen", "bristle_oil", "chalk", "pencil_2b", "palette_knife", "pastel"),
    ("ink_gpen", "ink_fineliner", "bristle_oil", "pencil_2b", "chalk", "pastel"),
)

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("basic", "Basic"),
    ("pencil", "Pencils"),
    ("inking", "Inking"),
    ("marker", "Markers"),
    ("dry_painting", "Dry painting"),
    ("chalk_pastel_charcoal", "Chalk / Pastel / Charcoal"),
    ("wet_painting", "Wet painting"),
    ("watercolor", "Watercolors"),
    ("airbrush", "Airbrush / Adjustments"),
    ("spray", "Spray / Texture"),
)


def get_brush(brush_id: str) -> dict:
    if brush_id not in BRUSHES:
        raise KeyError(f"未知笔刷预设: {brush_id!r}")
    return BRUSHES[brush_id]


def validate() -> None:
    """自检：所有预设的 shape 必须是已注册笔形，且分层表引用的预设都存在。"""
    for bid, b in BRUSHES.items():
        if b["shape"] not in VALID_SHAPES:
            raise ValueError(f"预设 {bid} 的 shape={b['shape']!r} 未注册（{VALID_SHAPES}）")
    for layer, ids in enumerate(LAYER_BRUSHES):
        for bid in ids:
            if bid not in BRUSHES:
                raise ValueError(f"第 {layer} 层引用了不存在的笔刷 {bid!r}")


def presets_for_layer(layer: int) -> tuple[str, ...]:
    return LAYER_BRUSHES[min(max(int(layer), 0), len(LAYER_BRUSHES) - 1)]
