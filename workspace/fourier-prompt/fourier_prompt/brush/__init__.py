"""``fourier_prompt.brush`` —— 笔刷目录 / 校准 / 用笔策略 / 验收 swatch（WP-C）。

对外接口（KICKOFF §2.3）::

    from fourier_prompt.brush import BrushCatalog, BrushCalib, choose_brush, pressure_for, render_swatch

    cat = BrushCatalog.load()                  # assets/brush/brush_catalog.json
    cal = BrushCalib.load()                    # assets/brush/brush_calib.json
    bid = choose_brush("line_taper", style="ink_wash", width_px=8)
    lo, hi = pressure_for("line_taper")        # (0.30, 0.90)
    stats = render_swatch("assets/brush/previews/swatch.png", resolution=512)

设计约束：
- 本包 ``core`` 只依赖 stdlib；只有 ``swatch`` 子模块会按需把 skill 的 ``scripts/``
  加进 ``sys.path`` 并只读调用渲染器（不 import ``document.py``，不改 skill 任何文件）。
- 所有查询对未知 id/角色/风格 **fail-loud**（KeyError/ValueError），不静默回退默认笔。
- ``status="planned"`` 的预设没有 skill 渲染实现，``choose_brush`` 永不选中。
"""
from __future__ import annotations

from .calib import (
    CALIB_VERSION,
    DEFAULT_CALIB_PATH,
    PRESSURE_POINTS,
    BrushCalib,
    BrushCalibError,
)
from .catalog import (
    CATALOG_VERSION,
    CATEGORIES,
    DEFAULT_CATALOG_PATH,
    ENGINE_SHAPES,
    ROLES,
    STATUS_ACTIVE,
    STATUS_PLANNED,
    BrushCatalog,
    BrushCatalogError,
    UnknownPresetError,
)
from .policy import (
    DEFAULT_STYLE_PACKS_PATH,
    ROLE_BIAS,
    ROLE_POOLS,
    ROLE_PRESSURE,
    TRAIT_KEYS,
    UnknownRoleError,
    UnknownStyleError,
    choose_brush,
    explain_choice,
    load_style_packs,
    pool_for,
    pressure_for,
    validate_policy,
)

__all__ = [
    # 版本/路径
    "CATALOG_VERSION",
    "CALIB_VERSION",
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_CALIB_PATH",
    "DEFAULT_STYLE_PACKS_PATH",
    "BRUSH_API_VERSION",
    # 词表/常量
    "ROLES",
    "CATEGORIES",
    "ENGINE_SHAPES",
    "STATUS_ACTIVE",
    "STATUS_PLANNED",
    "PRESSURE_POINTS",
    "ROLE_POOLS",
    "ROLE_PRESSURE",
    "ROLE_BIAS",
    "TRAIT_KEYS",
    # 目录/校准
    "BrushCatalog",
    "BrushCalib",
    "BrushCatalogError",
    "BrushCalibError",
    "UnknownPresetError",
    "UnknownRoleError",
    "UnknownStyleError",
    # 策略
    "choose_brush",
    "pressure_for",
    "pool_for",
    "explain_choice",
    "validate_policy",
    "load_style_packs",
    # swatch（延迟导入，避免 import brush 就拉起 numpy/PIL/skill）
    "render_swatch",
]

BRUSH_API_VERSION = "brush-api/1"


def __getattr__(name: str):
    """``render_swatch`` 延迟导入：只有真正渲染时才需要 numpy/Pillow/skill。"""
    if name == "render_swatch":
        from .swatch import render_swatch as _render_swatch

        return _render_swatch
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
