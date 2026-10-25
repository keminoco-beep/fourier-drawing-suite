"""`.fdraw` single-file container (``fdraw/1``) — stdlib only.

A ``.fdraw`` file is a deterministic ZIP container::

    meta.json            manifest: schema/version/capabilities/root_sha256/sizes
    render.json          rendering authority: strict superset of ``.fbs`` v2
    source.fdsl.json     optional editable FDSL program
    fonts/<name>         optional passthrough font payload (no font semantics yet)

Design rules implemented here (see ``analysis/10_FDRAW容器_字体_编辑协议设计.md``):

* ``render.json`` is the rendering authority and must pass a structural check
  equivalent to the skill's ``fbs_core.io_format.validate``.  The bytes are kept
  *verbatim* (canonical JSON), so a plain ``.fbs`` round-trips unchanged.
* Determinism: fixed entry order, fixed ZIP timestamp (1980-01-01 or
  ``SOURCE_DATE_EPOCH``), fixed deflate level, canonical JSON.  Packing the same
  input twice yields **byte-identical** output.
* ``root_sha256`` = sha256 of the canonical ``render.json`` bytes.  ``inspect``
  recomputes it; ``unpack``/``validate`` reject a mismatch.
* Safety: a strict path whitelist, no ``..``/absolute paths/backslashes/symlinks,
  entry count/size/ratio/total limits, strict JSON (no NaN/Inf), no code
  execution, no network.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterable

FDRAW_VERSION = "fdraw/1"
FDRAW_SPEC_VERSION = "1.0"
RENDER_SCHEMA = "fdraw-render/1"
FDRAW_MIME = "application/vnd.fdraw+zip;v=1"

META_PATH = "meta.json"
RENDER_PATH = "render.json"
SOURCE_PATH = "source.fdsl.json"
FONTS_PREFIX = "fonts/"

DEFAULT_ZIP_DATE = (1980, 1, 1, 0, 0, 0)

#: Product limits.  Limits are data (not scattered ``if`` statements) so they
#: can be tightened per customer/tests.  Sizes are uncompressed bytes.
LIMITS: dict[str, Any] = {
    "max_entries": 64,
    # 2026-10 high-precision mode: 4 GiB container cap (render authority is the
    # largest entry, so it needs the same 4 GiB ceiling as the total).
    "max_entry_bytes": 4096 * 1024 * 1024,
    "max_font_entry_bytes": 32 * 1024 * 1024,
    "max_fonts_bytes": 32 * 1024 * 1024,
    "max_total_bytes": 4096 * 1024 * 1024,
    "max_container_bytes": 4096 * 1024 * 1024,
    "max_meta_bytes": 4 * 1024 * 1024,
    "max_render_bytes": 4096 * 1024 * 1024,
    "max_source_bytes": 4 * 1024 * 1024,
    "max_compression_ratio": 500.0,
    "compression_ratio_min_bytes": 1024,
    "max_json_depth": 32,
    "max_elements": 100000,
    "max_texts": 5000,
}

#: Capability tokens this reader understands.  Unknown *required* capability
#: means "this reader cannot honour the file" -> ``FD_E_CAPABILITY``.
KNOWN_CAPABILITIES = frozenset(
    {
        "fourier/stroke",
        "fill/polygon",
        "text/outline",
        "text/glyph",
        "timeline/1",
        "source/fdsl-2",
        "fonts/passthrough",
    }
)

_ALLOWED_JSON = {META_PATH, RENDER_PATH, SOURCE_PATH}
_FONT_EXTS = frozenset({".ttf", ".otf", ".woff", ".woff2", ".ttc", ".txt"})
_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_FONT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SCHEMA_RE = re.compile(r"^fdraw/1(\.\d+)?$")

_FILL_ROLES = ("medial_branch", "region_fill")
_MODE_PER_STROKE = "per_stroke"
_MODE_ONE_STROKE = "one_stroke"
_FBS_FORMAT = "fourier-brushstroke"


class FDrawError(Exception):
    """Container error with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str, evidence: dict | None = None):
        self.code = str(code)
        self.message = str(message)
        self.evidence = dict(evidence or {})
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.evidence:
            out["evidence"] = self.evidence
        return out


# ---------------------------------------------------------------------------
# canonical JSON / strict parsing
# ---------------------------------------------------------------------------
def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8, no NaN/Inf."""
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # non-JSON types, NaN/Inf, mixed keys
        raise FDrawError("FD_E_JSON", f"无法序列化为严格 JSON: {exc}") from exc
    return text.encode("utf-8")


def _reject_constant(token: str) -> None:
    raise FDrawError("FD_E_JSON", f"JSON 含非法常量 {token}（不允许 NaN/Infinity）")


def _json_loads(data: bytes | bytearray | memoryview, where: str) -> Any:
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FDrawError("FD_E_JSON", f"{where} 不是 UTF-8: {exc}") from exc
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except FDrawError:
        raise
    except json.JSONDecodeError as exc:
        raise FDrawError("FD_E_JSON", f"{where} JSON 解析失败: {exc}") from exc
    _check_json_depth(value, where)
    return value


def _check_json_depth(value: Any, where: str) -> None:
    max_depth = int(LIMITS["max_json_depth"])
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            raise FDrawError("FD_E_JSON", f"{where} JSON 嵌套超过 {max_depth} 层")
        if isinstance(current, dict):
            for child in current.values():
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            for child in current:
                stack.append((child, depth + 1))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# paths / zip entries
# ---------------------------------------------------------------------------
def _validate_rel_path(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise FDrawError("FD_E_PATH", f"非法条目名: {name!r}")
    if "\x00" in name:
        raise FDrawError("FD_E_PATH", "条目名含 NUL")
    if "\\" in name:
        raise FDrawError("FD_E_PATH", f"条目名含反斜杠: {name!r}")
    if name.startswith("/") or _DRIVE_RE.match(name):
        raise FDrawError("FD_E_PATH", f"条目名是绝对路径: {name!r}")
    if name.endswith("/"):
        raise FDrawError("FD_E_PATH", f"条目名以 / 结尾（目录）: {name!r}")
    if not _PATH_RE.match(name):
        raise FDrawError("FD_E_PATH", f"条目名含非法字符: {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise FDrawError("FD_E_PATH", f"条目名含空段/./..: {name!r}")
    return name


def _is_font_path(name: str) -> bool:
    if not name.startswith(FONTS_PREFIX):
        return False
    rel = name[len(FONTS_PREFIX) :]
    if "/" in rel or not _FONT_NAME_RE.match(rel):
        return False
    return os.path.splitext(rel)[1].lower() in _FONT_EXTS


def _entry_role(name: str) -> str:
    if name == META_PATH:
        return "meta"
    if name == RENDER_PATH:
        return "render"
    if name == SOURCE_PATH:
        return "source"
    if _is_font_path(name):
        return "font"
    return "unknown"


def _check_allowed(name: str) -> str:
    role = _entry_role(name)
    if role == "unknown":
        raise FDrawError("FD_E_UNKNOWN_ENTRY", f"未知/不允许的容器条目: {name!r}")
    return role


def _entry_limit(name: str) -> int:
    role = _entry_role(name)
    if role == "meta":
        return int(LIMITS["max_meta_bytes"])
    if role == "render":
        return int(LIMITS["max_render_bytes"])
    if role == "source":
        return int(LIMITS["max_source_bytes"])
    if role == "font":
        return int(LIMITS["max_font_entry_bytes"])
    return int(LIMITS["max_entry_bytes"])


def _zip_date_time() -> tuple[int, int, int, int, int, int]:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw:
        try:
            epoch = int(raw)
        except (TypeError, ValueError):
            epoch = 0
        # ZIP cannot represent dates before 1980.
        if epoch >= 315532800:
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            if dt.year > 2107:  # ZIP upper bound
                dt = datetime(2107, 12, 31, 23, 59, 58, tzinfo=timezone.utc)
            return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    return DEFAULT_ZIP_DATE


def _write_zip(files: Iterable[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    date_time = _zip_date_time()
    with zipfile.ZipFile(
        buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zf:
        for name, data in files:
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, deterministic across hosts
            info.external_attr = 0o100644 << 16
            info.internal_attr = 0
            zf.writestr(info, data)
    return buf.getvalue()


def _as_bytes(data_or_path: Any) -> tuple[bytes, str | None]:
    if isinstance(data_or_path, (bytes, bytearray, memoryview)):
        data = bytes(data_or_path)
        if len(data) > int(LIMITS["max_container_bytes"]):
            raise FDrawError(
                "FD_E_ENTRY_TOO_BIG",
                f"容器字节 {len(data)} 超过上限 {LIMITS['max_container_bytes']}",
            )
        return data, None
    if isinstance(data_or_path, (str, os.PathLike)):
        path = os.fspath(data_or_path)
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise FDrawError("FD_E_INPUT", f"无法读取容器文件: {path}") from exc
        if size > int(LIMITS["max_container_bytes"]):
            raise FDrawError(
                "FD_E_ENTRY_TOO_BIG",
                f"容器文件 {size} 字节超过上限 {LIMITS['max_container_bytes']}",
            )
        try:
            with open(path, "rb") as fh:
                return fh.read(), str(path)
        except OSError as exc:
            raise FDrawError("FD_E_INPUT", f"无法读取容器文件: {path}") from exc
    raise FDrawError("FD_E_INPUT", f"需要 bytes 或路径，收到 {type(data_or_path).__name__}")


def _check_zip_info(info: zipfile.ZipInfo) -> None:
    name = info.filename
    _validate_rel_path(name)
    _check_allowed(name)
    if info.is_dir():
        raise FDrawError("FD_E_ENTRY_TYPE", f"容器含目录条目: {name!r}")
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    file_type = mode & 0o170000
    if file_type not in (0, 0o100000):
        raise FDrawError(
            "FD_E_ENTRY_TYPE",
            f"容器条目不是普通文件（mode={oct(mode)}）: {name!r}",
        )
    if int(info.flag_bits) & 0x1:
        raise FDrawError("FD_E_ZIP_MODE", f"容器条目被加密: {name!r}")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise FDrawError(
            "FD_E_ZIP_MODE",
            f"不支持的压缩方法 {info.compress_type}: {name!r}",
        )
    limit = _entry_limit(name)
    if int(info.file_size) > limit:
        raise FDrawError(
            "FD_E_ENTRY_TOO_BIG",
            f"{name!r} 解压后 {info.file_size} 字节超过上限 {limit}",
        )
    min_bytes = int(LIMITS["compression_ratio_min_bytes"])
    if info.file_size >= min_bytes and info.compress_size > 0:
        ratio = float(info.file_size) / float(info.compress_size)
        if ratio > float(LIMITS["max_compression_ratio"]):
            raise FDrawError(
                "FD_E_BOMB",
                f"{name!r} 压缩比 {ratio:.1f}:1 超过上限 "
                f"{LIMITS['max_compression_ratio']}:1",
            )


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    limit = _entry_limit(info.filename)
    try:
        with zf.open(info) as fh:
            data = fh.read(limit + 1)
    except FDrawError:
        raise
    except (RuntimeError, zipfile.BadZipFile, EOFError, OSError) as exc:
        raise FDrawError("FD_E_ZIP", f"解压条目失败 {info.filename!r}: {exc}") from exc
    if len(data) > limit:
        raise FDrawError(
            "FD_E_ENTRY_TOO_BIG",
            f"{info.filename!r} 解压后超过上限 {limit}",
        )
    if len(data) != int(info.file_size):
        raise FDrawError(
            "FD_E_ZIP",
            f"{info.filename!r} 实际大小 {len(data)} 与声明 {info.file_size} 不符",
        )
    return data


# ---------------------------------------------------------------------------
# .fbs structural validation (mirror of skill ``io_format.validate``)
# ---------------------------------------------------------------------------
def _render_errors(doc: Any) -> list[str]:
    """Structural validation of a ``render.json`` / ``.fbs`` v2 document.

    This is intentionally a faithful subset of ``fbs_core.io_format.validate``:
    the container must not depend on the skill at runtime, but a document that
    passes here is structurally a valid per-stroke ``.fbs`` v2 document (the
    skill's own validator remains the final authority in tests).
    """
    errs: list[str] = []

    def err(msg: str) -> None:
        errs.append(msg)

    if not isinstance(doc, dict):
        return ["render.json 顶层必须是 JSON 对象"]
    if doc.get("format") != _FBS_FORMAT:
        err(f"format != {_FBS_FORMAT}: {doc.get('format')!r}")
    if str(doc.get("version", "")).split(".")[0] != "2":
        err(f"version 不是 2.x: {doc.get('version')!r}")
    mode = doc.get("mode")
    if mode not in (_MODE_PER_STROKE, _MODE_ONE_STROKE):
        err(f"mode 非法: {mode!r}")
    canvas = doc.get("canvas")
    if not isinstance(canvas, dict):
        err("canvas 缺失/非对象")
        canvas = {}
    for key in ("width", "height", "long_side_units"):
        if key not in canvas:
            err(f"canvas 缺少 {key}")
    for key in ("width", "height"):
        if key in canvas:
            try:
                value = float(canvas[key])
            except (TypeError, ValueError):
                err(f"canvas.{key} 不是数字")
                continue
            if not math.isfinite(value) or value <= 0:
                err(f"canvas.{key} 非正/非有限: {canvas[key]!r}")
    contract = doc.get("render_contract")
    if not isinstance(contract, dict):
        err("render_contract 缺失/非对象")
        contract = {}
    for key in ("target_resolution", "epsilon_px"):
        if key not in contract:
            err(f"render_contract 缺少 {key}")
    if "width_scale" in contract:
        try:
            ws = float(contract["width_scale"])
        except (TypeError, ValueError):
            err(f"render_contract.width_scale 不是数字: {contract['width_scale']!r}")
        else:
            if not math.isfinite(ws) or ws < 0:
                err(f"render_contract.width_scale 非法: {contract['width_scale']!r}")
    if "fdraw" in doc and not isinstance(doc.get("fdraw"), dict):
        err("顶层 fdraw 必须是对象")

    styles = doc.get("brush_styles")
    if not isinstance(styles, list):
        err("brush_styles 必须是列表")
        styles = []
    strokes = doc.get("strokes")
    if not isinstance(strokes, list):
        err("strokes 必须是列表")
        strokes = []
    if len(strokes) > int(LIMITS["max_elements"]):
        err(f"strokes 数量 {len(strokes)} 超过上限 {LIMITS['max_elements']}")
    if not strokes:
        codes = {
            w.get("code")
            for w in doc.get("warnings", [])
            if isinstance(w, dict)
        }
        if "W_SOLID_COLOR" not in codes:
            err("strokes 为空且没有 W_SOLID_COLOR 警告")

    ids: set[int] = set()
    for index, stroke in enumerate(strokes):
        tag = f"strokes[{index}]"
        if not isinstance(stroke, dict):
            err(f"{tag} 不是对象")
            continue
        sid = stroke.get("id")
        if not isinstance(sid, int) or isinstance(sid, bool):
            err(f"{tag}.id 必须是 int")
        elif sid in ids:
            err(f"{tag}.id 重复: {sid}")
        else:
            ids.add(sid)
        if not isinstance(stroke.get("closed"), bool):
            err(f"{tag}.closed 必须是 bool")
        if stroke.get("fill_role") not in _FILL_ROLES:
            err(f"{tag}.fill_role 非法: {stroke.get('fill_role')!r}")
        if stroke.get("repr", "fourier") != "fourier":
            err(f"{tag}.repr 必须为 fourier")
        coef = stroke.get("coef") or []
        if not coef:
            err(f"{tag}.coef 为空")
        elif not isinstance(coef, list):
            err(f"{tag}.coef 必须是列表")
        else:
            ks: list[int] = []
            for row in coef:
                if not (isinstance(row, (list, tuple)) and len(row) == 3):
                    err(f"{tag}.coef 行格式错误（应为 [k,re,im]）")
                    break
                try:
                    k = int(row[0])
                    re_v = float(row[1])
                    im_v = float(row[2])
                except (TypeError, ValueError):
                    err(f"{tag}.coef 行含非数字")
                    break
                ks.append(k)
                if not (math.isfinite(re_v) and math.isfinite(im_v)):
                    err(f"{tag}.coef 含非有限数")
                    break
            if ks:
                n = stroke.get("N")
                expected = max(abs(k) for k in ks)
                if not isinstance(n, int) or isinstance(n, bool) or n != expected:
                    err(f"{tag}.N 与 coef 最大 |k| 不一致: N={n}, max|k|={expected}")
                if any(abs(ks[i]) > abs(ks[i + 1]) for i in range(len(ks) - 1)):
                    err(f"{tag}.coef 未按 |k| 升序")
                if len(set(ks)) != len(ks):
                    err(f"{tag}.coef 含重复 k")
        closure = stroke.get("closure")
        if not isinstance(closure, dict):
            err(f"{tag}.closure 缺失/非对象")
            closure = {}
        kind = closure.get("kind")
        if stroke.get("closed") is True:
            if kind not in ("none", None):
                err(f"{tag}.closure.kind 对闭合曲线应为 none: {kind!r}")
        elif stroke.get("closed") is False:
            if kind != "hermite_c1":
                err(f"{tag} 开曲线 closure.kind 应为 hermite_c1: {kind!r}")
            t_end = closure.get("t_end")
            if not isinstance(t_end, (int, float)) or isinstance(t_end, bool) or not (
                0.0 < float(t_end) <= 1.0
            ):
                err(f"{tag}.closure.t_end 非法: {t_end!r}")
        brush = stroke.get("brush")
        if not isinstance(brush, dict):
            err(f"{tag}.brush 缺失/非对象")
            brush = {}
        style = brush.get("style")
        if not isinstance(style, int) or isinstance(style, bool):
            err(f"{tag}.brush.style 必须是 int")
        elif styles and not (0 <= style < len(styles)):
            err(f"{tag}.brush.style 越界: {style} / {len(styles)}")
        if not isinstance(brush.get("width"), dict):
            err(f"{tag}.brush.width 缺失")
        if not isinstance(stroke.get("layer"), int) or isinstance(
            stroke.get("layer"), bool
        ):
            err(f"{tag}.layer 必须是 int")
        if "order" in stroke and (
            not isinstance(stroke.get("order"), int)
            or isinstance(stroke.get("order"), bool)
        ):
            err(f"{tag}.order 必须是 int")
        if stroke.get("fill_role") == "region_fill":
            poly = stroke.get("poly")
            if not poly:
                err(f"{tag} region_fill 缺少 explicit poly")
            elif not isinstance(poly, list) or len(poly) < 3:
                err(f"{tag}.poly 至少 3 点")
            else:
                for point in poly:
                    if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                        err(f"{tag}.poly 点格式错误")
                        break
                    try:
                        x, y = float(point[0]), float(point[1])
                    except (TypeError, ValueError):
                        err(f"{tag}.poly 点含非数字")
                        break
                    if not (math.isfinite(x) and math.isfinite(y)):
                        err(f"{tag}.poly 点含非有限数")
                        break
            holes = stroke.get("fill_holes")
            if holes is not None:
                if not isinstance(holes, list):
                    err(f"{tag}.fill_holes 必须是列表")
                else:
                    for hi, hole in enumerate(holes):
                        if not isinstance(hole, list) or len(hole) < 3:
                            err(f"{tag}.fill_holes[{hi}] 至少 3 点")
        for key in ("arc_length",):
            if key in stroke and not (
                isinstance(stroke[key], (int, float))
                and not isinstance(stroke[key], bool)
                and float(stroke[key]) > 0
            ):
                err(f"{tag}.{key} 非法: {stroke.get(key)!r}")
        if "max_err" in stroke and not (
            isinstance(stroke["max_err"], (int, float))
            and not isinstance(stroke["max_err"], bool)
            and float(stroke["max_err"]) >= 0
        ):
            err(f"{tag}.max_err 非法: {stroke.get('max_err')!r}")

    for index, style in enumerate(styles):
        if not isinstance(style, dict):
            err(f"brush_styles[{index}] 不是对象")
            continue
        for key in ("color", "alpha", "shape"):
            if not isinstance(style.get(key), dict):
                err(f"brush_styles[{index}].{key} 缺失")

    texts = doc.get("texts")
    if texts is not None:
        if not isinstance(texts, list):
            err("texts 必须是列表")
        else:
            if len(texts) > int(LIMITS["max_texts"]):
                err(f"texts 数量 {len(texts)} 超过上限 {LIMITS['max_texts']}")
            for index, text in enumerate(texts):
                if not isinstance(text, dict):
                    err(f"texts[{index}] 不是对象")
                    continue
                if text.get("as") not in ("outline", "glyph"):
                    err(f"texts[{index}].as 非法: {text.get('as')!r}")

    if mode == _MODE_ONE_STROKE:
        if len(strokes) != 1:
            err(f"one_stroke 模式必须恰好 1 条笔画，实际 {len(strokes)}")
        elif strokes[0].get("closed") is not True:
            err("one_stroke 笔画必须 closed=True")
        if not isinstance(doc.get("one_stroke_fills"), list):
            err("one_stroke 模式缺少 one_stroke_fills 列表")

    fingerprint = doc.get("fingerprint")
    if not isinstance(fingerprint, dict) or not fingerprint.get("params_sha256"):
        err("缺少 fingerprint.params_sha256")
    return errs


def _require_valid_render(doc: Any) -> dict:
    errs = _render_errors(doc)
    if errs:
        raise FDrawError(
            "FD_E_RENDER",
            f"render.json 不是合法 .fbs v2（{len(errs)} 项）",
            {"errors": errs[:12]},
        )
    return doc


# ---------------------------------------------------------------------------
# meta / entries helpers
# ---------------------------------------------------------------------------
def _counts(render: dict, font_count: int) -> dict:
    strokes = render.get("strokes") or []
    fills = sum(1 for s in strokes if isinstance(s, dict) and s.get("fill_role") == "region_fill")
    texts = render.get("texts") or []
    return {
        "elements": len(strokes),
        "strokes": len(strokes) - fills,
        "fills": fills,
        "texts": len(texts) if isinstance(texts, list) else 0,
        "fonts": int(font_count),
    }


def _layers(render: dict) -> list[int]:
    layers: set[int] = set()
    for stroke in render.get("strokes") or []:
        if isinstance(stroke, dict) and isinstance(stroke.get("layer"), int):
            layers.add(int(stroke["layer"]))
    for text in render.get("texts") or []:
        if isinstance(text, dict) and isinstance(text.get("layer"), int):
            layers.add(int(text["layer"]))
    return sorted(layers)


def _text_modes(render: dict) -> set[str]:
    modes: set[str] = set()
    for text in render.get("texts") or []:
        if isinstance(text, dict) and text.get("as") in ("outline", "glyph"):
            modes.add(str(text["as"]))
    return modes


def _entries_root_sha256(entries: list[dict]) -> str:
    """Root over (path, sha256, bytes) sorted by path — independent of ZIP bytes."""
    h = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: str(item.get("path", ""))):
        h.update(str(entry.get("path", "")).encode("utf-8"))
        h.update(b"\0")
        h.update(str(entry.get("sha256", "")).encode("utf-8"))
        h.update(b"\0")
        h.update(str(entry.get("bytes", 0)).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _manifest_sha256(meta: dict) -> str:
    copy_meta = copy.deepcopy(meta)
    integrity = copy_meta.get("integrity")
    if isinstance(integrity, dict):
        integrity.pop("manifest_sha256", None)
    return _sha256(canonical_json_bytes(copy_meta))


def _producer() -> dict:
    version = "0.1.0"
    try:  # pragma: no cover - trivial package metadata lookup
        from . import __version__ as package_version

        version = str(package_version)
    except Exception:  # pragma: no cover
        pass
    return {
        "name": "fourier-prompt",
        "version": version,
        "container": FDRAW_VERSION,
        "codegen": "fbs-v2",
    }


def _normalize_fonts(fonts: Any) -> list[tuple[str, bytes]]:
    if fonts is None:
        return []
    if isinstance(fonts, dict):
        fonts = list(fonts.items())
    if not isinstance(fonts, (list, tuple)):
        raise FDrawError("FD_E_FONT", "fonts 必须是 [(name, bytes), ...]")
    out: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for item in fonts:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise FDrawError("FD_E_FONT", "fonts 元素必须是 (name, bytes)")
        name, data = item
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, (bytes,)):
            raise FDrawError("FD_E_FONT", f"字体 {name!r} 内容必须是 bytes")
        if not name or not isinstance(name, str):
            raise FDrawError("FD_E_FONT", f"非法字体名: {name!r}")
        path = name if name.startswith(FONTS_PREFIX) else FONTS_PREFIX + name
        _validate_rel_path(path)
        if not _is_font_path(path):
            raise FDrawError(
                "FD_E_CONTENT_TYPE",
                f"字体路径必须是 fonts/<name> 且扩展名在 {sorted(_FONT_EXTS)}: {path!r}",
            )
        rel = path[len(FONTS_PREFIX) :]
        if "/" in rel or not _FONT_NAME_RE.match(rel):
            raise FDrawError("FD_E_PATH", f"字体名不合法: {path!r}")
        key = path.casefold()
        if key in seen:
            raise FDrawError("FD_E_DUPLICATE", f"字体路径重复: {path!r}")
        seen.add(key)
        if len(data) > int(LIMITS["max_font_entry_bytes"]):
            raise FDrawError(
                "FD_E_ENTRY_TOO_BIG",
                f"字体 {path!r} 大小 {len(data)} 超过上限 "
                f"{LIMITS['max_font_entry_bytes']}",
            )
        out.append((path, data))
    out.sort(key=lambda item: item[0])
    total = sum(len(data) for _name, data in out)
    if total > int(LIMITS["max_fonts_bytes"]):
        raise FDrawError(
            "FD_E_ENTRY_TOO_BIG",
            f"字体总大小 {total} 超过上限 {LIMITS['max_fonts_bytes']}",
        )
    return out


def _media_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".ttf", ".otf", ".ttc"):
        return "font/ttf" if ext != ".otf" else "font/otf"
    if ext == ".woff":
        return "font/woff"
    if ext == ".woff2":
        return "font/woff2"
    return "text/plain"


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------
def pack(
    fbs: dict,
    *,
    source_fdsl: dict | None = None,
    meta: dict | None = None,
    fonts: list[tuple[str, bytes]] | None = None,
    out: str | None = None,
) -> bytes:
    """Pack a ``.fbs`` document into a deterministic ``.fdraw`` container.

    ``fbs`` is kept verbatim (canonicalized JSON) as ``render.json``; it is not
    quantized or mutated.  ``meta`` may carry free-form user metadata (``title``,
    ``id``, ``created`` ...); reserved computed fields are overwritten.
    """
    if not isinstance(fbs, dict):
        raise FDrawError(
            "FD_E_RENDER", f"pack 需要 .fbs dict，收到 {type(fbs).__name__}"
        )
    render = _require_valid_render(fbs)
    render_bytes = canonical_json_bytes(render)
    if len(render_bytes) > int(LIMITS["max_render_bytes"]):
        raise FDrawError(
            "FD_E_ENTRY_TOO_BIG",
            f"render.json {len(render_bytes)} 字节超过上限 "
            f"{LIMITS['max_render_bytes']}",
        )

    source_bytes: bytes | None = None
    source_obj: dict | None = None
    source_schema: str | None = None
    if source_fdsl is not None:
        if not isinstance(source_fdsl, dict):
            raise FDrawError(
                "FD_E_SOURCE",
                f"source_fdsl 需要 dict，收到 {type(source_fdsl).__name__}",
            )
        source_obj = source_fdsl
        raw_schema = source_obj.get("schema")
        source_schema = raw_schema if isinstance(raw_schema, str) else None
        source_bytes = canonical_json_bytes(source_obj)
        if len(source_bytes) > int(LIMITS["max_source_bytes"]):
            raise FDrawError(
                "FD_E_ENTRY_TOO_BIG",
                f"source.fdsl.json {len(source_bytes)} 字节超过上限 "
                f"{LIMITS['max_source_bytes']}",
            )

    font_entries = _normalize_fonts(fonts)

    if meta is not None and not isinstance(meta, dict):
        raise FDrawError("FD_E_INPUT", "meta 必须是 dict")
    user_meta = copy.deepcopy(meta) if meta else {}
    created = user_meta.get("created")
    if created is not None and not isinstance(created, str):
        raise FDrawError("FD_E_INPUT", "meta.created 必须是 ISO 时间字符串")

    entries: list[dict] = [
        {
            "path": RENDER_PATH,
            "role": "render",
            "bytes": len(render_bytes),
            "sha256": _sha256(render_bytes),
            "media": "application/json",
            "required": True,
        }
    ]
    files: list[tuple[str, bytes]] = []
    if source_bytes is not None:
        entries.append(
            {
                "path": SOURCE_PATH,
                "role": "source",
                "bytes": len(source_bytes),
                "sha256": _sha256(source_bytes),
                "media": "application/json",
                "required": False,
            }
        )
    for path, data in font_entries:
        entries.append(
            {
                "path": path,
                "role": "font",
                "bytes": len(data),
                "sha256": _sha256(data),
                "media": _media_for(path),
                "required": False,
            }
        )

    counts = _counts(render, len(font_entries))
    text_modes = _text_modes(render)
    render_contract = dict(render.get("render_contract") or {})
    required_caps = ["fourier/stroke"]
    if counts["fills"]:
        required_caps.append("fill/polygon")
    if "text/outline" in text_modes:
        required_caps.append("text/outline")
    if "text/glyph" in text_modes:
        required_caps.append("text/glyph")
    if source_bytes is None:
        optional_caps = ["edit/restricted"]
    elif source_schema is None or source_schema.startswith("fdsl/2"):
        optional_caps = ["source/fdsl-2"]
    else:
        optional_caps = ["source/other"]
    if font_entries:
        optional_caps.append("fonts/passthrough")
    if "width_scale" not in render_contract:
        optional_caps.append("render/width_scale-default")
    caps = user_meta.get("capabilities")
    if isinstance(caps, dict):
        for extra in caps.get("optional") or []:
            if isinstance(extra, str) and extra not in optional_caps:
                optional_caps.append(extra)
        for extra in caps.get("required") or []:
            if isinstance(extra, str) and extra not in required_caps:
                required_caps.append(extra)
    unknown_required = [cap for cap in required_caps if cap not in KNOWN_CAPABILITIES]
    if unknown_required:
        raise FDrawError(
            "FD_E_CAPABILITY",
            f"未知的必需能力: {unknown_required}（可选能力见 KNOWN_CAPABILITIES）",
        )
    source_capability = None
    if source_bytes is not None:
        source_capability = source_schema or "fdsl/2"

    sizes = {
        "render_bytes": len(render_bytes),
        "source_bytes": len(source_bytes) if source_bytes is not None else 0,
        "fonts_bytes": sum(len(data) for _path, data in font_entries),
        "total_uncompressed": len(render_bytes)
        + (len(source_bytes) if source_bytes is not None else 0)
        + sum(len(data) for _path, data in font_entries),
        "entry_count": len(entries),
    }
    root_sha256 = _sha256(render_bytes)

    computed = {
        "schema": FDRAW_VERSION,
        "fdraw": FDRAW_VERSION,
        "spec_version": FDRAW_SPEC_VERSION,
        "capabilities": {
            "required": required_caps,
            "optional": optional_caps,
            "source": source_capability,
            "editing": "full" if source_bytes is not None else "restricted",
        },
        "profiles": ["core"] + (["text"] if "text/glyph" in text_modes else []),
        "root_sha256": root_sha256,
        "sizes": sizes,
        "counts": counts,
        "layers": _layers(render),
        "canvas": {
            "width": (render.get("canvas") or {}).get("width"),
            "height": (render.get("canvas") or {}).get("height"),
        },
        "render_contract": dict(render_contract),
        "entries": entries,
        "fonts": [
            {"path": path, "bytes": len(data), "sha256": _sha256(data)}
            for path, data in font_entries
        ],
        "integrity": {
            "algorithm": "sha256",
            "root_sha256": root_sha256,
            "root_scope": RENDER_PATH,
            "entries_root_sha256": _entries_root_sha256(entries),
        },
    }
    computed["render_contract"].setdefault("width_scale", None)
    computed["render_contract"]["width_scale_pinned"] = (
        "width_scale" in (render.get("render_contract") or {})
    )

    merged = dict(user_meta)
    merged.update(computed)
    if "created" in user_meta:
        merged["created"] = created
    else:
        merged.pop("created", None)
    producer = _producer()
    user_producer = user_meta.get("producer")
    if isinstance(user_producer, dict):
        producer.update(user_producer)
        producer["name"] = str(user_producer.get("name") or producer["name"])
    merged["producer"] = producer
    merged["integrity"]["entries_root_sha256"] = _entries_root_sha256(entries)
    merged["integrity"]["manifest_sha256"] = _manifest_sha256(merged)

    meta_bytes = canonical_json_bytes(merged)
    if len(meta_bytes) > int(LIMITS["max_meta_bytes"]):
        raise FDrawError(
            "FD_E_ENTRY_TOO_BIG",
            f"meta.json {len(meta_bytes)} 字节超过上限 {LIMITS['max_meta_bytes']}",
        )

    files: list[tuple[str, bytes]] = [(META_PATH, meta_bytes), (RENDER_PATH, render_bytes)]
    if source_bytes is not None:
        files.append((SOURCE_PATH, source_bytes))
    files.extend(font_entries)
    if len(files) > int(LIMITS["max_entries"]):
        raise FDrawError(
            "FD_E_TOO_MANY_ENTRIES",
            f"容器条目 {len(files)} 超过上限 {LIMITS['max_entries']}",
        )
    total = sum(len(data) for _name, data in files)
    if total > int(LIMITS["max_total_bytes"]):
        raise FDrawError(
            "FD_E_BOMB",
            f"容器解压总大小 {total} 超过上限 {LIMITS['max_total_bytes']}",
        )

    blob = _write_zip(files)
    if out is not None:
        path = os.fspath(out)
        parent = os.path.dirname(os.path.abspath(path))
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, path)
        except OSError as exc:
            raise FDrawError("FD_E_IO", f"写入容器失败 {path}: {exc}") from exc
    return blob


# ---------------------------------------------------------------------------
# read / validate
# ---------------------------------------------------------------------------
def _read_container(data_or_path: Any, *, verify: bool = True) -> dict:
    data, path = _as_bytes(data_or_path)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise FDrawError("FD_E_ZIP", f"不是合法 ZIP: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > int(LIMITS["max_entries"]):
            raise FDrawError(
                "FD_E_TOO_MANY_ENTRIES",
                f"容器条目 {len(infos)} 超过上限 {LIMITS['max_entries']}",
            )
        seen: dict[str, str] = {}
        for info in infos:
            _check_zip_info(info)
            key = info.filename.casefold()
            if key in seen:
                raise FDrawError(
                    "FD_E_DUPLICATE",
                    f"条目名大小写不敏感重复: {info.filename!r} / {seen[key]!r}",
                )
            seen[key] = info.filename
        total_declared = sum(int(info.file_size) for info in infos)
        if total_declared > int(LIMITS["max_total_bytes"]):
            raise FDrawError(
                "FD_E_BOMB",
                f"容器声明解压总大小 {total_declared} 超过上限 "
                f"{LIMITS['max_total_bytes']}",
            )
        names = {info.filename for info in infos}
        if META_PATH not in names:
            raise FDrawError("FD_E_MANIFEST", "容器缺少 meta.json")
        if RENDER_PATH not in names:
            raise FDrawError("FD_E_RENDER", "容器缺少 render.json")

        files: dict[str, bytes] = {}
        entry_reports: list[dict] = []
        for info in infos:
            raw = _read_entry(zf, info)
            files[info.filename] = raw
            entry_reports.append(
                {
                    "path": info.filename,
                    "role": _entry_role(info.filename),
                    "bytes": len(raw),
                    "compress_bytes": int(info.compress_size),
                    "sha256": _sha256(raw),
                }
            )

    meta = _json_loads(files[META_PATH], META_PATH)
    if not isinstance(meta, dict):
        raise FDrawError("FD_E_MANIFEST", "meta.json 顶层必须是对象")
    schema = str(meta.get("schema") or meta.get("fdraw") or "")
    if not _SCHEMA_RE.match(schema):
        raise FDrawError(
            "FD_E_VERSION", f"不支持的容器版本: {schema!r}（需要 fdraw/1.x）"
        )
    caps = meta.get("capabilities")
    if not isinstance(caps, dict):
        raise FDrawError("FD_E_MANIFEST", "meta.capabilities 缺失/非对象")
    required = caps.get("required")
    if not isinstance(required, list):
        raise FDrawError("FD_E_MANIFEST", "meta.capabilities.required 必须是列表")
    unknown = [item for item in required if item not in KNOWN_CAPABILITIES]
    if unknown:
        raise FDrawError(
            "FD_E_CAPABILITY",
            f"reader 不支持必需能力: {unknown}",
            {"required": required},
        )

    render = _json_loads(files[RENDER_PATH], RENDER_PATH)
    render_errors = _render_errors(render)
    if render_errors:
        raise FDrawError(
            "FD_E_RENDER",
            f"render.json 不是合法 .fbs v2（{len(render_errors)} 项）",
            {"errors": render_errors[:12]},
        )

    source = None
    if SOURCE_PATH in files:
        source = _json_loads(files[SOURCE_PATH], SOURCE_PATH)
        if not isinstance(source, dict):
            raise FDrawError("FD_E_SOURCE", "source.fdsl.json 顶层必须是对象")
    fonts = {name: raw for name, raw in files.items() if _is_font_path(name)}

    manifest = meta.get("entries")
    manifest_errors: list[str] = []
    if not isinstance(manifest, list):
        manifest_errors.append("meta.entries 缺失/非列表")
        manifest = []
    listed: dict[str, dict] = {}
    for index, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            manifest_errors.append(f"meta.entries[{index}] 不是对象")
            continue
        entry_path = entry.get("path")
        try:
            _validate_rel_path(entry_path)  # type: ignore[arg-type]
            _check_allowed(entry_path)  # type: ignore[arg-type]
        except FDrawError as exc:
            manifest_errors.append(f"meta.entries[{index}]: {exc.code} {exc.message}")
            continue
        if entry_path == META_PATH:
            manifest_errors.append("meta.entries 不应包含 meta.json（避免自引用）")
            continue
        if entry_path in listed:
            manifest_errors.append(f"meta.entries 路径重复: {entry_path}")
            continue
        listed[entry_path] = entry
    for name in files:
        if name == META_PATH:
            continue
        if name not in listed:
            manifest_errors.append(f"容器条目未在 meta.entries 登记: {name}")
    for entry_path, entry in listed.items():
        if entry_path not in files:
            manifest_errors.append(f"meta.entries 登记的条目不存在: {entry_path}")
            continue
        raw = files[entry_path]
        if entry.get("bytes") != len(raw):
            manifest_errors.append(
                f"{entry_path} bytes 不符: meta={entry.get('bytes')} actual={len(raw)}"
            )
        if entry.get("sha256") != _sha256(raw):
            manifest_errors.append(f"{entry_path} sha256 不符")
    if verify and manifest_errors:
        raise FDrawError(
            "FD_E_MANIFEST",
            f"meta.entries 与容器内容不一致（{len(manifest_errors)} 项）",
            {"errors": manifest_errors[:12]},
        )

    root_sha256 = _sha256(canonical_json_bytes(render))
    integrity = meta.get("integrity") if isinstance(meta.get("integrity"), dict) else {}
    recorded = meta.get("root_sha256") or integrity.get("root_sha256")
    root_ok = isinstance(recorded, str) and recorded == root_sha256
    if verify and not root_ok:
        raise FDrawError(
            "FD_E_HASH",
            "root_sha256 与 render.json 规范化字节不一致",
            {"recorded": recorded, "recomputed": root_sha256},
        )
    entries_root = _entries_root_sha256(
        [
            {
                "path": name,
                "sha256": _sha256(raw),
                "bytes": len(raw),
            }
            for name, raw in files.items()
            if name != META_PATH
        ]
    )
    entries_root_ok = (
        integrity.get("entries_root_sha256") == entries_root
        if integrity.get("entries_root_sha256") is not None
        else None
    )
    manifest_sha = _manifest_sha256(meta)
    manifest_sha_ok = (
        integrity.get("manifest_sha256") == manifest_sha
        if integrity.get("manifest_sha256") is not None
        else None
    )

    return {
        "path": path,
        "zip_bytes": len(data),
        "meta": meta,
        "render": render,
        "source_fdsl": source,
        "fonts": fonts,
        "files": files,
        "entries": entry_reports,
        "entry_count": len(files),
        "uncompressed_bytes": sum(len(raw) for raw in files.values()),
        "compressed_bytes": sum(report["compress_bytes"] for report in entry_reports),
        "root_sha256": root_sha256,
        "root_sha256_recorded": recorded,
        "root_sha256_ok": root_ok,
        "entries_root_sha256": entries_root,
        "entries_root_sha256_ok": entries_root_ok,
        "manifest_sha256": manifest_sha,
        "manifest_sha256_ok": manifest_sha_ok,
        "manifest_errors": manifest_errors,
    }


def _safe_extract_path(out_dir: str, rel_path: str) -> str:
    base = os.path.realpath(os.fspath(out_dir))
    target = os.path.realpath(os.path.join(base, rel_path.replace("/", os.sep)))
    if target != base and not target.startswith(base + os.sep):
        raise FDrawError("FD_E_PATH", f"解包路径逃逸出输出目录: {rel_path!r}")
    return target


def _extract(container: dict, out_dir: str) -> list[str]:
    base = os.fspath(out_dir)
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as exc:
        raise FDrawError("FD_E_IO", f"无法创建输出目录 {base}: {exc}") from exc
    written: list[str] = []
    for name in (META_PATH, RENDER_PATH, SOURCE_PATH):
        raw = container["files"].get(name)
        if raw is None:
            continue
        target = _safe_extract_path(base, name)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(raw)
        written.append(target)
    for name, raw in sorted(container["fonts"].items()):
        target = _safe_extract_path(base, name)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(raw)
        written.append(target)
    return written


def unpack(data_or_path: Any, *, out_dir: str | None = None) -> dict:
    """Unpack and verify a ``.fdraw`` container.

    Returns ``{meta, render, source_fdsl, fonts, ...}``.  ``fonts`` maps the
    container path (``fonts/<name>``) to raw bytes (passthrough only).
    Bad containers raise :class:`FDrawError` with a stable ``FD_E_*`` code.
    """
    container = _read_container(data_or_path, verify=True)
    meta = container["meta"]
    result = {
        "path": container["path"],
        "zip_bytes": container["zip_bytes"],
        "meta": meta,
        "render": container["render"],
        "source_fdsl": container["source_fdsl"],
        "fonts": container["fonts"],
        "entries": meta.get("entries") or [],
        "root_sha256": container["root_sha256"],
        "root_sha256_recorded": container["root_sha256_recorded"],
        "root_sha256_ok": True,
        "capabilities": meta.get("capabilities"),
        "counts": meta.get("counts"),
        "sizes": meta.get("sizes"),
    }
    if out_dir is not None:
        result["out_dir"] = os.fspath(out_dir)
        result["written"] = _extract(container, out_dir)
    return result


def validate(data_or_path: Any) -> list[str]:
    """Validate a container; returns a list of error strings (empty = valid)."""
    try:
        _read_container(data_or_path, verify=True)
        return []
    except FDrawError as exc:
        errors = exc.evidence.get("errors") if isinstance(exc.evidence, dict) else None
        if exc.code in ("FD_E_RENDER", "FD_E_MANIFEST") and isinstance(errors, list):
            return [f"{exc.code}: {message}" for message in errors] or [
                f"{exc.code}: {exc.message}"
            ]
        return [f"{exc.code}: {exc.message}"]
    except Exception as exc:  # pragma: no cover - defensive
        return [f"FD_E_INTERNAL: {type(exc).__name__}: {exc}"]


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------
def inspect(path_or_bytes: Any) -> dict:
    """Inspect a container without rendering.

    Returns version/layers/elements/canvas/sizes/hash/entry reports.  Hash
    mismatches are reported (``*_ok``) instead of raised, so the command is
    useful for diagnosing damaged files; ``unpack``/``validate`` stay strict.
    """
    container = _read_container(path_or_bytes, verify=False)
    meta = container["meta"]
    render = container["render"]
    strokes = render.get("strokes") or []
    texts = render.get("texts") or []
    fills = sum(
        1
        for stroke in strokes
        if isinstance(stroke, dict) and stroke.get("fill_role") == "region_fill"
    )
    canvas = render.get("canvas") or {}
    contract = render.get("render_contract") or {}
    capabilities = meta.get("capabilities") or {}
    source_schema = None
    if isinstance(container["source_fdsl"], dict):
        source_schema = container["source_fdsl"].get("schema")
    return {
        "schema": meta.get("schema"),
        "spec_version": meta.get("spec_version"),
        "zip_bytes": container["zip_bytes"],
        "uncompressed_bytes": container["uncompressed_bytes"],
        "compressed_bytes": container["compressed_bytes"],
        "entry_count": container["entry_count"],
        "entries": container["entries"],
        "root_sha256": container["root_sha256"],
        "root_sha256_recorded": container["root_sha256_recorded"],
        "root_sha256_ok": container["root_sha256_ok"],
        "entries_root_sha256_ok": container["entries_root_sha256_ok"],
        "manifest_sha256_ok": container["manifest_sha256_ok"],
        "render": {
            "format": render.get("format"),
            "version": render.get("version"),
            "mode": render.get("mode"),
            "canvas": {
                "width": canvas.get("width"),
                "height": canvas.get("height"),
                "long_side_units": canvas.get("long_side_units"),
            },
            "target_resolution": contract.get("target_resolution"),
            "epsilon_px": contract.get("epsilon_px"),
            "width_scale": contract.get("width_scale"),
            "width_scale_pinned": "width_scale" in contract,
            "element_count": len(strokes),
            "stroke_count": len(strokes) - fills,
            "fill_count": fills,
            "text_count": len(texts) if isinstance(texts, list) else 0,
            "brush_style_count": len(render.get("brush_styles") or []),
            "layer_count": len(_layers(render)),
            "layers": _layers(render),
            "has_texts": bool(texts),
        },
        "counts": meta.get("counts") or _counts(render, len(container["fonts"])),
        "fonts": [
            {"path": name, "bytes": len(raw), "sha256": _sha256(raw)}
            for name, raw in sorted(container["fonts"].items())
        ],
        "font_count": len(container["fonts"]),
        "has_source": isinstance(container["source_fdsl"], dict),
        "source_schema": source_schema,
        "capabilities": capabilities,
        "profiles": meta.get("profiles") or [],
        "sizes": meta.get("sizes"),
    }


__all__ = [
    "FDRAW_VERSION",
    "FDRAW_SPEC_VERSION",
    "RENDER_SCHEMA",
    "FDRAW_MIME",
    "LIMITS",
    "KNOWN_CAPABILITIES",
    "FDrawError",
    "pack",
    "unpack",
    "inspect",
    "validate",
    "canonical_json_bytes",
]
