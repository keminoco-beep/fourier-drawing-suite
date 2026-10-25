"""笔刷目录（Brush Catalog v1）：预设元数据的唯一事实来源。

设计依据：``analysis/13_笔刷目录与用笔策略设计.md`` §3.1/§3.2、
``analysis/13a_笔刷实测与校准数据.md`` §2/§3；数据文件 ``assets/brush/brush_catalog.json``
由 ``python -m fourier_prompt.brush._build`` 从 skill ``brushlib`` + 实测校准表生成。

纪律：
- **id 沿用 skill ``brushlib`` 的 preset key，不改名**（兼容既有 ``.fbs``/黄金回归）；
- 目录只做**元数据**，不重复实现渲染；渲染以 skill 的 10 个解析笔形内核为准；
- ``status="planned"`` 的条目**没有 skill 渲染实现**，``renderable`` 恒为 False，策略不得选中；
- 未知 id → ``UnknownPresetError``（KeyError 子类，fail-loud），不猜、不回退默认笔。

本模块只用 stdlib（红线：新包 core 无第三方依赖）。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# 目录格式版本（写入数据文件；不兼容变更才能升 major）
CATALOG_VERSION = "brush-catalog/1"

# 渲染端已注册的解析笔形内核（与 skill ``fbs_core.shapelib.SHAPE_CODES`` 对齐；
# 这里固化成常量，避免本包 import skill —— core 只依赖 stdlib）
ENGINE_SHAPES: tuple[str, ...] = (
    "round",
    "soft",
    "marker",
    "bristle",
    "chalk",
    "spatter",
    "airbrush",
    "pencil",
    "watercolor",
    "ink",
)

# 笔刷分区（沿用 skill brushlib 的 category 字符串，便于回溯）
CATEGORIES: tuple[str, ...] = (
    "basic",
    "pencil",
    "inking",
    "marker",
    "dry_painting",
    "chalk_pastel_charcoal",
    "wet_painting",
    "watercolor",
    "airbrush",
    "spray",
)

# 用笔角色词表（KICKOFF §2.3；policy.ROLES 的原点，避免 catalog↔policy 循环 import）
ROLES: tuple[str, ...] = (
    "bed",
    "wash",
    "line_hairline",
    "line_taper",
    "line_broad",
    "detail",
    "texture",
    "effect",
    "marker",
    "sketch",
)

STATUS_ACTIVE = "active"
STATUS_PLANNED = "planned"
STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_PLANNED)

# 每个条目必须有的字段（KICKOFF §2.3 列出的清单 + 追溯用字段）
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "category",
    "shape",
    "sizes_px",
    "width_eff",
    "pressure",
    "roles",
    "default_opacity",
    "recommended_layers",
    "preview",
    "license",
    "version",
    "status",
)

# 默认数据文件：<repo>/fourier-prompt/assets/brush/brush_catalog.json
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "assets" / "brush" / "brush_catalog.json"


class BrushCatalogError(ValueError):
    """目录结构/取值非法（``validate`` 之外的单点读取错误）。"""


class UnknownPresetError(KeyError):
    """未知 preset id（fail-loud；契约要求 KeyError）。"""

    def __init__(self, preset_id: str, known: int = 0) -> None:
        super().__init__(preset_id)
        self.preset_id = preset_id
        self.known = int(known)

    def __str__(self) -> str:  # KeyError 的默认 repr 会加引号，这里给可读信息
        return (
            f"未知笔刷预设 {self.preset_id!r}（目录内共 {self.known} 条）；"
            "请使用 brush_catalog.json 里的 id，不要自造名称"
        )


def _deep(obj):
    return copy.deepcopy(obj)


class BrushCatalog:
    """笔刷目录的只读视图。

    典型用法::

        cat = BrushCatalog.load()                 # 默认 assets/brush/brush_catalog.json
        meta = cat.get("ink_gpen")                # 未知 id -> UnknownPresetError
        tapered = cat.list(role="line_taper")     # 只列可渲染（active）条目
        errs = cat.validate()                     # [] = 通过
    """

    def __init__(self, data: dict, *, source: str | None = None) -> None:
        if not isinstance(data, dict):
            raise BrushCatalogError(f"目录数据必须是对象，收到 {type(data).__name__}")
        self._data = data
        self.source = source or "<memory>"
        presets = data.get("presets")
        if not isinstance(presets, dict):
            raise BrushCatalogError("目录缺少 presets 对象")
        self._presets: dict[str, dict] = presets
        self._order: list[str] = list(presets.keys())

    # ---------------------------------------------------------------- 载入
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "BrushCatalog":
        """读取目录 JSON；``path=None`` 用包内默认资产。文件缺失直接报错（不静默空目录）。"""
        p = Path(path) if path is not None else DEFAULT_CATALOG_PATH
        if not p.is_file():
            raise FileNotFoundError(
                f"笔刷目录不存在: {p}（默认路径 {DEFAULT_CATALOG_PATH}）；"
                "请确认 assets/brush/brush_catalog.json 已随包安装"
            )
        with open(p, "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:  # pragma: no cover - 防御性
                raise BrushCatalogError(f"目录 JSON 解析失败: {p}: {exc}") from exc
        return cls(data, source=str(p))

    @classmethod
    def from_dict(cls, data: dict) -> "BrushCatalog":
        return cls(_deep(data), source="<dict>")

    # ---------------------------------------------------------------- 读取
    @property
    def version(self) -> str:
        return str(self._data.get("schema") or self._data.get("catalog_version") or "")

    def __len__(self) -> int:
        return len(self._presets)

    def __contains__(self, preset_id: object) -> bool:
        return isinstance(preset_id, str) and preset_id in self._presets

    def ids(self, *, include_planned: bool = False) -> list[str]:
        """按目录顺序返回 id（默认只含可渲染的 active 条目）。"""
        if include_planned:
            return list(self._order)
        return [pid for pid in self._order if self._presets[pid].get("status") == STATUS_ACTIVE]

    def planned_ids(self) -> list[str]:
        return [pid for pid in self._order if self._presets[pid].get("status") == STATUS_PLANNED]

    def is_renderable(self, preset_id: str) -> bool:
        meta = self._presets.get(preset_id)
        if meta is None:
            raise UnknownPresetError(preset_id, len(self._presets))
        return bool(meta.get("renderable", meta.get("status") == STATUS_ACTIVE))

    def get(self, preset_id: str) -> dict:
        """取单条预设元数据（深拷贝）。未知 id → ``UnknownPresetError``（KeyError）。"""
        if not isinstance(preset_id, str):
            raise UnknownPresetError(str(preset_id), len(self._presets))
        meta = self._presets.get(preset_id)
        if meta is None:
            raise UnknownPresetError(preset_id, len(self._presets))
        return _deep(meta)

    def list(
        self,
        *,
        category: str | None = None,
        role: str | None = None,
        include_planned: bool = False,
    ) -> list[dict]:
        """按分区/角色过滤，返回元数据列表（目录顺序，确定性）。

        - ``category`` 必须是 ``CATEGORIES`` 之一，未知 → ``BrushCatalogError``；
        - ``role`` 必须是 ``ROLES`` 之一，未知 → ``BrushCatalogError``（fail-loud）；
        - 默认只返回可渲染（status=active）条目；``include_planned=True`` 才带起草条目。
        """
        if category is not None and category not in CATEGORIES:
            raise BrushCatalogError(f"未知笔刷分区 {category!r}（可用: {list(CATEGORIES)}）")
        if role is not None and role not in ROLES:
            raise BrushCatalogError(f"未知用笔角色 {role!r}（可用: {list(ROLES)}）")
        out: list[dict] = []
        for pid in self._order:
            meta = self._presets[pid]
            if not include_planned and meta.get("status") != STATUS_ACTIVE:
                continue
            if category is not None and meta.get("category") != category:
                continue
            if role is not None and role not in (meta.get("roles") or ()):
                continue
            out.append(_deep(meta))
        return out

    def role_index(self, *, include_planned: bool = False) -> dict[str, list[str]]:
        """角色 → id 列表（目录顺序）；每个 role 键都存在（可能为空列表）。"""
        index: dict[str, list[str]] = {r: [] for r in ROLES}
        for pid in self._order:
            meta = self._presets[pid]
            if not include_planned and meta.get("status") != STATUS_ACTIVE:
                continue
            for role in meta.get("roles") or ():
                index.setdefault(role, []).append(pid)
        return index

    def categories(self) -> list[str]:
        """目录里实际出现的分区（目录顺序、去重）。"""
        seen: list[str] = []
        for pid in self._order:
            cat = self._presets[pid].get("category")
            if cat and cat not in seen:
                seen.append(str(cat))
        return seen

    def presets(self) -> dict[str, dict]:
        """全部条目（深拷贝，含 planned）。"""
        return _deep(self._presets)

    def as_dict(self) -> dict:
        return _deep(self._data)

    # ---------------------------------------------------------------- 校验
    def validate(self) -> list[str]:
        """返回错误列表（空 = 通过）。契约要求：结构/枚举/数值范围/planned 一致性。"""
        errs: list[str] = []
        data = self._data
        schema = data.get("schema")
        if schema != CATALOG_VERSION:
            errs.append(f"schema 应为 {CATALOG_VERSION!r}，实际 {schema!r}")
        if str(data.get("catalog_version", CATALOG_VERSION)) != CATALOG_VERSION:
            errs.append(f"catalog_version 应为 {CATALOG_VERSION!r}，实际 {data.get('catalog_version')!r}")
        presets = data.get("presets")
        if not isinstance(presets, dict) or not presets:
            errs.append("presets 必须是非空对象")
            return errs
        if not any(p.get("status") == STATUS_ACTIVE for p in presets.values()):
            errs.append("目录里没有任何 status=active 的可渲染预设")
        seen_roles: dict[str, int] = {r: 0 for r in ROLES}
        for pid, meta in presets.items():
            tag = f"presets[{pid}]"
            if not isinstance(meta, dict):
                errs.append(f"{tag} 不是对象")
                continue
            if meta.get("id") != pid:
                errs.append(f"{tag}.id 与键名不一致: {meta.get('id')!r}")
            for field in REQUIRED_FIELDS:
                if field not in meta:
                    errs.append(f"{tag} 缺少字段 {field}")
            status = meta.get("status")
            if status not in STATUSES:
                errs.append(f"{tag}.status 非法: {status!r}")
            cat = meta.get("category")
            if cat not in CATEGORIES:
                errs.append(f"{tag}.category 非法: {cat!r}")
            shape = meta.get("shape")
            if not isinstance(shape, str) or not shape:
                errs.append(f"{tag}.shape 必须是非空字符串")
            elif status == STATUS_ACTIVE and shape not in ENGINE_SHAPES:
                errs.append(f"{tag}.shape={shape!r} 不是已注册笔形（active 条目必须在 {list(ENGINE_SHAPES)} 内）")
            # 尺寸
            sizes = meta.get("sizes_px")
            if not isinstance(sizes, list) or not sizes:
                errs.append(f"{tag}.sizes_px 必须是非空列表")
            else:
                if any(not isinstance(v, (int, float)) or v <= 0 for v in sizes):
                    errs.append(f"{tag}.sizes_px 必须是正数列表")
                if list(sizes) != sorted(sizes):
                    errs.append(f"{tag}.sizes_px 必须升序")
            # 有效宽
            weff = meta.get("width_eff")
            if not isinstance(weff, (int, float)) or not (0.05 <= float(weff) <= 1.05):
                errs.append(f"{tag}.width_eff 必须在 (0.05, 1.05]：{weff!r}")
            # 压力曲线
            press = meta.get("pressure")
            if not isinstance(press, dict):
                errs.append(f"{tag}.pressure 必须是对象")
            else:
                for key, lo, hi in (("width", 0.0, 4.0), ("alpha", 0.0, 1.0)):
                    pts = press.get(key)
                    if not isinstance(pts, list) or len(pts) < 2:
                        errs.append(f"{tag}.pressure.{key} 至少 2 个压力点")
                        continue
                    ps: list[float] = []
                    for pair in pts:
                        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                            errs.append(f"{tag}.pressure.{key} 点格式应为 [p, value]")
                            ps = []
                            break
                        p, v = pair
                        if not isinstance(p, (int, float)) or not (0.0 <= float(p) <= 1.0):
                            errs.append(f"{tag}.pressure.{key} 压力越界: {p!r}")
                        if not isinstance(v, (int, float)) or not (lo <= float(v) <= hi):
                            errs.append(f"{tag}.pressure.{key} 取值越界: {v!r}")
                        ps.append(float(p))
                    if ps and ps != sorted(ps):
                        errs.append(f"{tag}.pressure.{key} 压力点必须升序")
            # 角色
            roles = meta.get("roles")
            if not isinstance(roles, list) or not roles:
                errs.append(f"{tag}.roles 必须是非空列表")
            else:
                for role in roles:
                    if role not in ROLES:
                        errs.append(f"{tag}.roles 含未知角色 {role!r}")
                    elif status == STATUS_ACTIVE:
                        seen_roles[role] = seen_roles.get(role, 0) + 1
            # 其他取值
            op = meta.get("default_opacity")
            if not isinstance(op, (int, float)) or not (0.0 < float(op) <= 1.0):
                errs.append(f"{tag}.default_opacity 必须在 (0,1]：{op!r}")
            layers = meta.get("recommended_layers")
            if not isinstance(layers, list) or not layers or any(not isinstance(s, str) for s in layers):
                errs.append(f"{tag}.recommended_layers 必须是非空字符串列表")
            preview = meta.get("preview")
            if not isinstance(preview, str) or not preview.endswith(".png") or Path(preview).is_absolute():
                errs.append(f"{tag}.preview 必须是相对 .png 路径：{preview!r}")
            lic = meta.get("license")
            if not isinstance(lic, str) or not lic:
                errs.append(f"{tag}.license 必须是非空字符串")
            ver = meta.get("version")
            if not isinstance(ver, int) or ver < 1:
                errs.append(f"{tag}.version 必须是 >=1 的整数：{ver!r}")
            # planned 的可渲染性必须显式为 False
            if status == STATUS_PLANNED and meta.get("renderable") is True:
                errs.append(f"{tag} 是 planned，但 renderable=True（planned 不得声称可渲染）")
            if status == STATUS_ACTIVE and meta.get("renderable") is False:
                errs.append(f"{tag} 是 active，但 renderable=False（状态自相矛盾）")
        for role, n in seen_roles.items():
            if n == 0:
                errs.append(f"角色 {role!r} 没有任何 active 预设（策略会无笔可用）")
        return errs

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"BrushCatalog(source={self.source!r}, presets={len(self._presets)}, "
            f"active={len(self.ids())}, planned={len(self.planned_ids())})"
        )
