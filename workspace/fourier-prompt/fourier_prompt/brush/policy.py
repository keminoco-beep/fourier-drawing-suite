"""用笔策略（Brush Policy）：角色 → 笔刷池 → 具体 preset id，并按实测性格兜底。

依据：``analysis/13_...`` §3.5 的角色表 + ``analysis/13a_...`` §3.1 的**实测分组**
（W+/W~/W− 提按强度、A+/A− 浓淡能力、soft/rim/aniso 边缘性格）与 §3 的压力档建议。

契约（KICKOFF §2.3）::

    ROLES = ("bed","wash","line_hairline","line_taper","line_broad",
             "detail","texture","effect","marker","sketch")
    choose_brush(role, *, style=None, width_px=None, traits=None) -> str
    pressure_for(role) -> (float, float)

fail-loud：未知 role / style / trait 键 / 空池 → KeyError/ValueError（带可用值）；
``status="planned"`` 的笔永远不进入候选池。
"""
from __future__ import annotations

import json
from pathlib import Path

from .calib import BrushCalib
from .catalog import ROLES, BrushCatalog, UnknownPresetError

__all__ = [
    "ROLES",
    "ROLE_POOLS",
    "ROLE_PRESSURE",
    "ROLE_BIAS",
    "TRAIT_KEYS",
    "DEFAULT_STYLE_PACKS_PATH",
    "UnknownRoleError",
    "UnknownStyleError",
    "choose_brush",
    "pressure_for",
    "pool_for",
    "explain_choice",
    "validate_policy",
    "load_style_packs",
]

# 角色 → 笔刷池（顺序即默认优先级；第一个是"无额外约束时的默认笔"）。
# 池内每一支都必须：在目录里 status=active，且声明了该 role（validate_policy 会查）。
ROLE_POOLS: dict[str, tuple[str, ...]] = {
    "bed": (
        "basic_round_hard",
        "basic_round_soft",
        "dry_flat",
        "bristle_oil",
        "palette_knife",
        "marker_chisel",
        "marker_alcohol",
        "watercolor_wash",
        "sumi_e",
        "wet_oil",
    ),
    "wash": (
        "basic_round_soft",
        "watercolor_wash",
        "wet_oil",
        "bristle_oil",
        "marker_alcohol",
        "basic_airbrush",
        "sumi_e",
    ),
    "line_hairline": ("ink_fineliner", "ink_gpen"),
    "line_taper": ("ink_gpen", "ink_brush_rough", "sumi_e", "charcoal"),
    "line_broad": ("sumi_e", "ink_brush_rough", "bristle_oil"),
    "detail": ("ink_fineliner", "pencil_hb", "pencil_2b", "ink_gpen"),
    "texture": (
        "chalk",
        "charcoal",
        "pastel",
        "dry_flat",
        "palette_knife",
        "spatter_brush",
        "bristle_oil",
        "ink_brush_rough",
        "wet_oil",
        "pencil_2b",
    ),
    "effect": ("basic_airbrush", "spatter_brush", "watercolor_wash", "basic_round_soft"),
    "marker": ("marker_chisel", "marker_alcohol", "dry_flat", "basic_round_hard", "palette_knife"),
    "sketch": ("pencil_hb", "pencil_2b", "charcoal", "chalk", "pastel"),
}

# 推荐压力档（13a §3.1 结论：线稿 0.55–0.85 / 铺底 0.7–1.0 / 细节 0.2–0.7；
# 水彩·马克只做轻微调制；提按类需要更大动态范围）
ROLE_PRESSURE: dict[str, tuple[float, float]] = {
    "bed": (0.70, 1.00),
    "wash": (0.55, 0.90),
    "line_hairline": (0.60, 0.85),
    "line_taper": (0.30, 0.90),
    "line_broad": (0.55, 1.00),
    "detail": (0.20, 0.70),
    "texture": (0.40, 0.90),
    "effect": (0.30, 0.80),
    "marker": (0.60, 0.95),
    "sketch": (0.20, 0.65),
}

# 角色对"实测性格"的偏好（给 choose_brush 打分的弱约束，不是硬过滤）：
#   width_gain: "high" 需要 W+（提按强） | "low" 需要 W−/W~（等宽线）
#   alpha_dynamic: True 需要 A+（压力→浓淡）| False 需要 A−/A~
#   textured: True 偏好颗粒/纹理内核
#   soft_edge: True 偏好柔边/雾化
#   aniso: True 偏好平头（各向异性）| False 避免平头
#   max_alpha_hi: 偏好 alpha 上限 ≤ 该值（wash/effect 要低 α 叠加）
ROLE_BIAS: dict[str, dict] = {
    "bed": {"width_gain": "high", "textured": False, "max_alpha_hi": 1.0},
    "wash": {"width_gain": None, "max_alpha_hi": 0.85, "textured": None},
    "line_hairline": {"width_gain": "low", "alpha_dynamic": False},
    "line_taper": {"width_gain": "high", "alpha_dynamic": True},
    "line_broad": {"width_gain": "high"},
    "detail": {"width_gain": "low", "alpha_dynamic": True},
    "texture": {"textured": True, "width_gain": "high"},
    "effect": {"soft_edge": True, "max_alpha_hi": 0.85},
    "marker": {"aniso": True},
    "sketch": {"textured": None, "max_alpha_hi": 1.0},
}

# 轨迹可调的 trait 约束键（未知键 → ValueError）
TRAIT_KEYS: tuple[str, ...] = ("width_gain", "alpha_dynamic", "textured", "soft_edge", "aniso")

TEXTURED_SHAPES = frozenset({"bristle", "chalk", "spatter", "pencil", "watercolor"})
SOFT_SHAPES = frozenset({"airbrush", "soft", "spatter", "watercolor"})
ANISO_SHAPES = frozenset({"marker"})

DEFAULT_STYLE_PACKS_PATH = Path(__file__).resolve().parents[2] / "assets" / "brush" / "style_packs.json"

STYLE_PACKS_SCHEMA = "brush-style-packs/1"


class UnknownRoleError(KeyError):
    """未知用笔角色（fail-loud）。"""

    def __init__(self, role: object) -> None:
        super().__init__(role)
        self.role = role

    def __str__(self) -> str:
        return f"未知用笔角色 {self.role!r}（可用: {list(ROLES)}）"


class UnknownStyleError(KeyError):
    """未知风格包（fail-loud）。"""

    def __init__(self, style: object, known: list[str]) -> None:
        super().__init__(style)
        self.style = style
        self.known = list(known)

    def __str__(self) -> str:
        return f"未知风格包 {self.style!r}（可用: {self.known}）"


# ------------------------------------------------------------------ 数据载入
def load_style_packs(path: str | Path | None = None) -> dict:
    """读取 ``style_packs.json``（水墨/铅笔/马克笔/油画/漫画/水彩）。"""
    p = Path(path) if path is not None else DEFAULT_STYLE_PACKS_PATH
    if not p.is_file():
        raise FileNotFoundError(f"风格包文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("schema") != STYLE_PACKS_SCHEMA:
        raise ValueError(f"风格包 schema 应为 {STYLE_PACKS_SCHEMA!r}，实际 {data.get('schema')!r}")
    if not isinstance(data.get("packs"), dict) or not data["packs"]:
        raise ValueError("风格包缺少非空 packs 对象")
    return data


_CATALOG_CACHE: dict[str, BrushCatalog] = {}
_CALIB_CACHE: dict[str, BrushCalib] = {}
_PACKS_CACHE: dict[str, dict] = {}


def _default_catalog() -> BrushCatalog:
    if "default" not in _CATALOG_CACHE:
        _CATALOG_CACHE["default"] = BrushCatalog.load()
    return _CATALOG_CACHE["default"]


def _default_calib() -> BrushCalib:
    if "default" not in _CALIB_CACHE:
        _CALIB_CACHE["default"] = BrushCalib.load()
    return _CALIB_CACHE["default"]


def _default_packs() -> dict:
    if "default" not in _PACKS_CACHE:
        _PACKS_CACHE["default"] = load_style_packs()
    return _PACKS_CACHE["default"]


def _pack_index(packs: dict) -> dict[str, str]:
    """别名/中文名 → 规范包 id（大小写不敏感）。"""
    index: dict[str, str] = {}
    for pid, pack in (packs.get("packs") or {}).items():
        index[pid.lower()] = pid
        for alias in pack.get("aliases") or ():
            index[str(alias).lower()] = pid
        for key in ("name_zh", "name_en"):
            if pack.get(key):
                index[str(pack[key]).lower()] = pid
    return index


def _resolve_style(style: str, packs: dict) -> str:
    if not isinstance(style, str) or not style:
        raise UnknownStyleError(style, sorted((packs.get("packs") or {}).keys()))
    canon = _pack_index(packs).get(style.lower())
    if canon is None:
        raise UnknownStyleError(style, sorted((packs.get("packs") or {}).keys()))
    return canon


# ------------------------------------------------------------------ 池/打分
def pool_for(
    role: str,
    *,
    style: str | None = None,
    catalog: BrushCatalog | None = None,
    packs: dict | None = None,
) -> list[str]:
    """返回该角色（可选风格）下的候选 id 列表（顺序=优先级；只含可渲染笔）。

    - 未知 role → ``UnknownRoleError``；未知 style → ``UnknownStyleError``；
    - 风格包给该 role 指定了池 → 用它，否则回退到 ``ROLE_POOLS``；
    - 候选池里 planned / 不在目录 / renderable=False 的 id 被剔除；
      若过滤后为空 → ``LookupError``（不静默给默认笔）。
    """
    if role not in ROLE_POOLS:
        raise UnknownRoleError(role)
    cat = catalog if catalog is not None else _default_catalog()
    ordered: list[str] = []
    if style is not None:
        pack_data = packs if packs is not None else _default_packs()
        canon = _resolve_style(style, pack_data)
        pack = pack_data["packs"][canon]
        pool = (pack.get("pools") or {}).get(role) or []
        ordered = [str(pid) for pid in pool]
    if not ordered:
        ordered = list(ROLE_POOLS[role])
    out: list[str] = []
    for pid in ordered:
        if pid not in cat:
            continue  # 池里写了目录没有的笔：由 validate_policy 报错，这里跳过以免运行期崩
        try:
            if not cat.is_renderable(pid):
                continue
        except UnknownPresetError:  # pragma: no cover - 已被 `in` 拦住
            continue
        out.append(pid)
    if not out:
        raise LookupError(f"角色 {role!r}（style={style!r}）没有任何可渲染的笔刷候选")
    return out


def _entry_traits(meta: dict, cal: BrushCalib | None) -> set[str]:
    """合并目录/实测标签（实测优先）。"""
    tags: set[str] = set(str(t) for t in (meta.get("traits") or ()))
    pid = meta.get("id")
    if cal is not None and isinstance(pid, str):
        try:
            tags.update(cal.traits(pid))
        except UnknownPresetError:
            pass
    return tags


def _width_gain_class(tags: set[str]) -> str | None:
    for tag, cls in (("W+", "high"), ("W~", "mid"), ("W-", "low")):
        if tag in tags:
            return cls
    return None


def _alpha_dynamic(tags: set[str]) -> bool | None:
    if "A+" in tags:
        return True
    if "A-" in tags:
        return False
    return None


def _check_trait_keys(traits: dict, where: str) -> None:
    unknown = [k for k in traits if k not in TRAIT_KEYS]
    if unknown:
        raise ValueError(f"{where} 含未知 trait 键 {unknown}（可用: {list(TRAIT_KEYS)}）")
    for key, value in traits.items():
        if key in ("width_gain",) and value not in (None, "high", "mid", "low"):
            raise ValueError(f"trait {key!r} 取值非法: {value!r}（可用 high/mid/low）")
        if key in ("alpha_dynamic", "textured", "soft_edge", "aniso") and not isinstance(value, bool):
            raise ValueError(f"trait {key!r} 必须是 bool，收到 {value!r}")


def _score_candidate(
    pid: str,
    *,
    role: str,
    idx: int,
    n: int,
    width_px: float | None,
    traits: dict | None,
    meta: dict,
    cal: BrushCalib | None,
) -> tuple[float, list[str]]:
    """确定性打分；返回 (score, reasons)。同分时调用方按 idx/id 兜底。"""
    reasons: list[str] = []
    base = 6.0 * (1.0 - (idx / max(n - 1, 1)))
    score = base
    reasons.append(f"pool#{idx}{'' if n == 1 else f'/{n - 1}'} += {base:.2f}")

    tags = _entry_traits(meta, cal)
    width_gain = _width_gain_class(tags)
    alpha_dyn = _alpha_dynamic(tags)
    shape = str(meta.get("shape") or "")
    textured = shape in TEXTURED_SHAPES
    soft_edge = shape in SOFT_SHAPES
    aniso = shape in ANISO_SHAPES or "aniso" in tags
    alpha_hi = None
    alpha_pts = (meta.get("pressure") or {}).get("alpha") or []
    if alpha_pts:
        alpha_hi = float(alpha_pts[-1][1])

    def apply(key: str, wanted, actual, weight: float, label: str) -> None:
        nonlocal score
        if wanted is None:
            return
        if actual is None:
            return
        if wanted == actual:
            score += weight
            reasons.append(f"{key}={label} match +{weight:.1f}")
        else:
            score -= weight
            reasons.append(f"{key}={label} miss -{weight:.1f}")

    bias = ROLE_BIAS.get(role, {})
    # 角色弱约束（±1.5）
    apply("width_gain", bias.get("width_gain"), width_gain, 1.0, "role")
    apply("alpha_dynamic", bias.get("alpha_dynamic"), alpha_dyn, 1.0, "role")
    apply("textured", bias.get("textured"), textured, 1.0, "role")
    apply("soft_edge", bias.get("soft_edge"), soft_edge, 1.0, "role")
    apply("aniso", bias.get("aniso"), aniso, 1.0, "role")
    max_alpha_hi = bias.get("max_alpha_hi")
    if max_alpha_hi is not None and alpha_hi is not None:
        if alpha_hi <= float(max_alpha_hi) + 1e-9:
            score += 1.0
            reasons.append(f"alpha_hi={alpha_hi:.2f}<= {max_alpha_hi} +1.0")
        else:
            score -= 1.0
            reasons.append(f"alpha_hi={alpha_hi:.2f}> {max_alpha_hi} -1.0")

    # 显式 traits 约束（±8，权重高到能压过池顺序）
    if traits:
        _check_trait_keys(traits, f"choose_brush(role={role!r}).traits")
        if "width_gain" in traits:
            wanted = traits["width_gain"]
            ok = (
                wanted is None
                or width_gain is None
                or (wanted == "high" and width_gain == "high")
                or (wanted == "mid" and width_gain == "mid")
                or (wanted == "low" and width_gain == "low")
            )
            score += 8.0 if ok else -8.0
            reasons.append(f"explicit width_gain={wanted} {'hit' if ok else 'miss'}")
        for key, actual in (("alpha_dynamic", alpha_dyn), ("textured", textured),
                            ("soft_edge", soft_edge), ("aniso", aniso)):
            if key in traits:
                ok = actual is None or actual == bool(traits[key])
                score += 8.0 if ok else -8.0
                reasons.append(f"explicit {key}={traits[key]} {'hit' if ok else 'miss'}")

    # 宽度约束（±6）：以"沉积宽"为准，先按 width_eff 反推名义宽再查目录 range
    if width_px is not None:
        weff = None
        if cal is not None:
            try:
                weff = cal.width_eff(pid)
            except UnknownPresetError:
                weff = None
        if weff is None:
            weff = float(meta.get("width_eff") or 1.0)
        nominal = float(width_px) / max(float(weff), 1e-6)
        lo, hi = (meta.get("width_range_px") or [1.0, 200.0])[:2]
        inside = (float(lo) <= float(width_px) <= float(hi)) and 0.5 <= nominal <= 400.0
        if inside:
            score += 6.0
            reasons.append(f"width {width_px}px in [{lo},{hi}] (nominal {nominal:.2f}) +6.0")
        else:
            score -= 6.0
            reasons.append(f"width {width_px}px out of [{lo},{hi}] (nominal {nominal:.2f}) -6.0")
    return score, reasons


def choose_brush(
    role: str,
    *,
    style: str | None = None,
    width_px: float | None = None,
    traits: dict | None = None,
    catalog: BrushCatalog | None = None,
    calib: BrushCalib | None = None,
    packs: dict | None = None,
) -> str:
    """按角色（可选风格/宽度/性格约束）返回**目录内可渲染**的 preset id。

    - 纯规则 + 实测标签打分，确定性（无随机、无时间）；
    - ``status="planned"`` 的笔永不入选；
    - 未知 role/style/trait 键 → 报错；候选池为空 → ``LookupError``。
    """
    cat = catalog if catalog is not None else _default_catalog()
    cal = calib if calib is not None else _default_calib()
    pool = pool_for(role, style=style, catalog=cat, packs=packs)
    if width_px is not None:
        if not isinstance(width_px, (int, float)) or not (float(width_px) > 0):
            raise ValueError(f"width_px 必须是正数，收到 {width_px!r}")
    best: tuple[float, int, str] | None = None
    for idx, pid in enumerate(pool):
        meta = cat.get(pid)
        score, _reasons = _score_candidate(
            pid,
            role=role,
            idx=idx,
            n=len(pool),
            width_px=width_px,
            traits=traits,
            meta=meta,
            cal=cal,
        )
        key = (-score, idx, pid)
        if best is None or key < best:
            best = key
    if best is None:  # pragma: no cover - pool_for 已保证非空
        raise LookupError(f"角色 {role!r} 没有候选笔")
    return best[2]


def explain_choice(
    role: str,
    *,
    style: str | None = None,
    width_px: float | None = None,
    traits: dict | None = None,
    catalog: BrushCatalog | None = None,
    calib: BrushCalib | None = None,
    packs: dict | None = None,
) -> dict:
    """调试/报告用：返回每个候选的分数、选择理由与最终选择（不走缓存）。"""
    cat = catalog if catalog is not None else _default_catalog()
    cal = calib if calib is not None else _default_calib()
    pool = pool_for(role, style=style, catalog=cat, packs=packs)
    rows: list[dict] = []
    for idx, pid in enumerate(pool):
        score, reasons = _score_candidate(
            pid,
            role=role,
            idx=idx,
            n=len(pool),
            width_px=width_px,
            traits=traits,
            meta=cat.get(pid),
            cal=cal,
        )
        rows.append({"id": pid, "score": round(score, 4), "reasons": reasons})
    chosen = choose_brush(
        role,
        style=style,
        width_px=width_px,
        traits=traits,
        catalog=cat,
        calib=cal,
        packs=packs,
    )
    return {
        "role": role,
        "style": style,
        "width_px": width_px,
        "traits": traits,
        "chosen": chosen,
        "pressure": list(pressure_for(role)),
        "candidates": rows,
    }


def pressure_for(role: str) -> tuple[float, float]:
    """该角色推荐的压力档 ``(lo, hi)``（0..1）。未知角色 → ``UnknownRoleError``。"""
    if role not in ROLE_PRESSURE:
        raise UnknownRoleError(role)
    lo, hi = ROLE_PRESSURE[role]
    return (float(lo), float(hi))


# ------------------------------------------------------------------ 校验
def validate_policy(
    *,
    catalog: BrushCatalog | None = None,
    packs: dict | None = None,
) -> list[str]:
    """策略自检：池 id 存在/active/声明了该 role、无 planned、每角色 ≥2 支、风格包合法。"""
    errs: list[str] = []
    cat = catalog if catalog is not None else _default_catalog()
    pack_data = packs if packs is not None else _default_packs()
    if set(ROLE_POOLS) != set(ROLES):
        errs.append(f"ROLE_POOLS 键与 ROLES 不一致: {sorted(set(ROLE_POOLS) ^ set(ROLES))}")
    if set(ROLE_PRESSURE) != set(ROLES):
        errs.append(f"ROLE_PRESSURE 键与 ROLES 不一致: {sorted(set(ROLE_PRESSURE) ^ set(ROLES))}")
    planned = set(cat.planned_ids())
    for role, pool in ROLE_POOLS.items():
        if len(pool) < 2:
            errs.append(f"角色 {role!r} 的笔刷池少于 2 支（无法兜底）")
        for pid in pool:
            if pid in planned:
                errs.append(f"角色 {role!r} 的池里混入了 planned 笔 {pid!r}")
                continue
            if pid not in cat:
                errs.append(f"角色 {role!r} 的池引用了目录外的笔 {pid!r}")
                continue
            meta = cat.get(pid)
            if not cat.is_renderable(pid):
                errs.append(f"角色 {role!r} 的池引用了不可渲染的笔 {pid!r}（status={meta.get('status')}）")
            if role not in (meta.get("roles") or ()):
                errs.append(f"角色 {role!r} 的池里 {pid!r} 未在目录声明该 role")
    for role, (lo, hi) in ROLE_PRESSURE.items():
        if not (0.0 <= lo < hi <= 1.0):
            errs.append(f"角色 {role!r} 的压力档非法: {(lo, hi)}")
    for pid, pack in (pack_data.get("packs") or {}).items():
        pools = pack.get("pools")
        if not isinstance(pools, dict) or not pools:
            errs.append(f"风格包 {pid!r} 缺少 pools")
            continue
        for role, ids in pools.items():
            if role not in ROLES:
                errs.append(f"风格包 {pid!r} 含未知角色 {role!r}")
            for bid in ids or ():
                if bid in planned:
                    errs.append(f"风格包 {pid!r} 的 {role} 池混入 planned 笔 {bid!r}")
                elif bid not in cat:
                    errs.append(f"风格包 {pid!r} 的 {role} 池引用目录外的笔 {bid!r}")
    return errs
