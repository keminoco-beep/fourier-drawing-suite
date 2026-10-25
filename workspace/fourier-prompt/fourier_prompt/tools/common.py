"""Shared plumbing for the ``fourier_prompt.tools`` surface (WP-H).

The tools are *composition only*: every non-trivial operation delegates to
``schema`` / ``compile`` / ``fdraw`` / ``compat.skill_api`` (and, when present,
``render_fast``).  This module owns the small pieces that would otherwise be
duplicated: structured errors, JSON-safety, input sniffing / loading, PNG I/O
and the render dispatch used by ``render`` / ``preview`` / ``frame``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import time
import zipfile
from pathlib import Path

from .. import __version__ as PACKAGE_VERSION

__all__ = [
    "TOOL_VERSION",
    "PACKAGE_VERSION",
    "ToolError",
    "ERROR_CODES",
    "SKILL_INFO",
    "jsonable",
    "sha256_bytes",
    "sha256_file",
    "resolve_file",
    "read_json",
    "sniff_kind",
    "load_input",
    "load_document",
    "ensure_out_path",
    "write_png",
    "png_to_rgb",
    "rgb_to_png_bytes",
    "encode_png_base64",
    "render_document",
    "slice_document",
    "summarize_document",
    "catalog_path",
    "load_brush_catalog",
    "check_int",
    "check_fraction",
]

TOOL_VERSION = "1.0"

#: Stable tool error codes (mirrored in the MCP error payloads).
ERROR_CODES = {
    "TOOL_E_ARG": "invalid argument",
    "TOOL_E_NOT_FOUND": "input path does not exist",
    "TOOL_E_INPUT": "input is not a supported kind",
    "TOOL_E_LOAD": "input could not be parsed",
    "TOOL_E_RENDER": "render failed",
    "TOOL_E_WRITE": "output could not be written",
    "TOOL_E_COMPARE": "images cannot be compared",
    "TOOL_E_CATALOG": "brush catalog unusable",
    "TOOL_E_INTERNAL": "internal error",
}

#: Public skill metadata resolved once (used by ``status``).
SKILL_INFO = {
    "module": "compat.skill_api",
    "read_only": True,
    "license": "proprietary (fourier-brushstroke skill; read-only dependency)",
}


class ToolError(Exception):
    """Structured tool failure: ``code`` / ``message`` / ``evidence``.

    ``to_dict()`` is JSON serializable and is what the MCP layer forwards.
    """

    def __init__(self, code: str, message: str, evidence=None):
        self.code = str(code) if code in ERROR_CODES else "TOOL_E_INTERNAL"
        self.message = str(message)
        if evidence is None:
            self.evidence = {}
        elif isinstance(evidence, dict):
            self.evidence = evidence
        else:
            self.evidence = {"detail": evidence}
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "evidence": jsonable(self.evidence),
        }


# ---------------------------------------------------------------------------
# JSON safety / hashing
# ---------------------------------------------------------------------------


def jsonable(value):
    """Recursively convert ``value`` into plain JSON-serializable types."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    # numpy scalars / arrays without importing numpy eagerly
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return jsonable(item())
        except Exception:  # noqa: BLE001 - fall through to str()
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return jsonable(tolist())
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def check_int(value, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(
            "TOOL_E_ARG",
            f"{name} must be an integer in [{minimum}, {maximum}] (got {value!r})",
            {name: value},
        )
    if not minimum <= value <= maximum:
        raise ToolError(
            "TOOL_E_ARG",
            f"{name} must be in [{minimum}, {maximum}] (got {value})",
            {name: value},
        )
    return value


def check_fraction(value, *, name: str = "fraction") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(
            "TOOL_E_ARG", f"{name} must be a number in (0, 1] (got {value!r})", {name: value}
        )
    number = float(value)
    if not math.isfinite(number) or not 0.0 < number <= 1.0:
        raise ToolError(
            "TOOL_E_ARG", f"{name} must be in (0, 1] (got {value!r})", {name: value}
        )
    return number


# ---------------------------------------------------------------------------
# Paths / loading
# ---------------------------------------------------------------------------


def resolve_file(path, *, what: str = "input") -> Path:
    try:
        candidate = Path(os.fspath(path)).expanduser()
    except TypeError as exc:
        raise ToolError("TOOL_E_ARG", f"{what} path must be a string (got {path!r})") from exc
    resolved = candidate.resolve()
    if not resolved.exists():
        raise ToolError(
            "TOOL_E_NOT_FOUND",
            f"{what} does not exist: {resolved}",
            {"path": str(resolved), "what": what},
        )
    if not resolved.is_file():
        raise ToolError(
            "TOOL_E_INPUT",
            f"{what} is not a regular file: {resolved}",
            {"path": str(resolved), "what": what},
        )
    return resolved


def read_json(path, *, what: str = "input") -> dict:
    resolved = resolve_file(path, what=what)
    try:
        with open(resolved, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ToolError("TOOL_E_LOAD", f"cannot read {what}: {exc}", {"path": str(resolved)}) from exc
    except json.JSONDecodeError as exc:
        raise ToolError(
            "TOOL_E_LOAD", f"invalid JSON in {what}: {exc}", {"path": str(resolved)}
        ) from exc
    if not isinstance(data, dict):
        raise ToolError(
            "TOOL_E_LOAD",
            f"{what} must contain a JSON object (got {type(data).__name__})",
            {"path": str(resolved)},
        )
    return data


def sniff_kind(path, data: dict | None = None) -> str:
    """Return ``"fdraw"`` / ``"fbs"`` / ``"plan"`` / ``"unknown"``."""
    name = Path(os.fspath(path)).name.lower()
    suffix = Path(name).suffix
    if suffix == ".fdraw":
        return "fdraw"
    if suffix == ".fbs" or name.endswith(".fbs.json"):
        return "fbs"
    if suffix in (".json", ".fdsl"):
        if isinstance(data, dict):
            if data.get("format") == "fourier-brushstroke":
                return "fbs"
            schema = data.get("schema")
            if isinstance(schema, str) and schema.startswith("fdsl/"):
                return "plan"
        try:
            if zipfile.is_zipfile(path):
                return "fdraw"
        except OSError:
            pass
    return "unknown"


def load_input(path, *, what: str = "input") -> dict:
    """Load a ``.fdraw`` / ``.fbs`` / FDSL plan into a uniform envelope.

    Returns ``{"kind", "path", "file_bytes", "sha256", "doc", "raw"}`` where
    ``doc`` is the renderable skill document (fbs/fdraw) or the plan dict.
    """
    resolved = resolve_file(path, what=what)
    file_bytes = resolved.stat().st_size
    digest = sha256_file(resolved)
    kind = sniff_kind(resolved)
    loaded_json = None
    if kind == "unknown" and resolved.suffix.lower() in (".json", ".fdsl"):
        # no extension hint: parse once and sniff the content
        loaded_json = read_json(resolved, what=what)
        kind = sniff_kind(resolved, loaded_json)
    if kind == "fdraw":
        from .. import fdraw

        try:
            unpacked = fdraw.unpack(str(resolved))
        except fdraw.FDrawError as exc:
            raise ToolError(
                exc.code if str(exc.code).startswith("FD_E_") else "TOOL_E_LOAD",
                f"invalid .fdraw container: {exc.message}",
                {"path": str(resolved), **(exc.evidence or {})},
            ) from exc
        return {
            "kind": "fdraw",
            "path": str(resolved),
            "file_bytes": file_bytes,
            "sha256": digest,
            "doc": unpacked["render"],
            "raw": unpacked,
        }
    if kind == "fbs":
        from ..compat import skill_api

        try:
            doc = skill_api.import_skill().io_format.read_fbs(str(resolved))
        except Exception as exc:  # noqa: BLE001 - FBSError or broken checkout
            raise ToolError(
                "TOOL_E_LOAD",
                f"invalid .fbs document: {exc}",
                {"path": str(resolved), "sha256": digest},
            ) from exc
        return {
            "kind": "fbs",
            "path": str(resolved),
            "file_bytes": file_bytes,
            "sha256": digest,
            "doc": doc,
            "raw": doc,
        }
    if kind == "plan":
        from ..schema import FDSchemaError, load_plan

        try:
            plan = loaded_json if loaded_json is not None else load_plan(str(resolved))
        except FDSchemaError as exc:
            raise ToolError(
                "TOOL_E_LOAD",
                f"invalid FDSL plan: {exc.message}",
                {"path": str(resolved), "schema_code": exc.code, "at": exc.path},
            ) from exc
        return {
            "kind": "plan",
            "path": str(resolved),
            "file_bytes": file_bytes,
            "sha256": digest,
            "doc": plan,
            "raw": plan,
        }
    raise ToolError(
        "TOOL_E_INPUT",
        f"unsupported input kind: {resolved.name} (expected .fdraw, .fbs or an FDSL plan)",
        {"path": str(resolved), "suffix": resolved.suffix},
    )


def load_document(path, *, what: str = "input") -> dict:
    """Load a renderable document; plans are compiled first (composition)."""
    envelope = load_input(path, what=what)
    if envelope["kind"] == "plan":
        from ..compile import CompileError, compile_plan

        try:
            compiled = compile_plan(envelope["doc"], resolution=256)
        except CompileError as exc:
            raise ToolError(
                "TOOL_E_INPUT",
                f"FDSL plan does not compile: {exc.message}",
                {"path": envelope["path"], "schema_code": exc.code, "at": exc.path},
            ) from exc
        envelope["source_kind"] = "plan"
        envelope["doc"] = compiled["fbs"]
        envelope["compile_stats"] = compiled["stats"]
        envelope["compile_warnings"] = compiled["warnings"]
        return envelope
    envelope["source_kind"] = envelope["kind"]
    return envelope


# ---------------------------------------------------------------------------
# Output handling
# ---------------------------------------------------------------------------


def ensure_out_path(out, *, input_path=None) -> Path:
    try:
        target = Path(os.fspath(out)).expanduser()
    except TypeError as exc:
        raise ToolError("TOOL_E_ARG", f"out path must be a string (got {out!r})") from exc
    if not str(target).strip():
        raise ToolError("TOOL_E_ARG", "out path must not be empty")
    resolved = target.resolve()
    if input_path is not None and resolved == Path(os.fspath(input_path)).resolve():
        raise ToolError(
            "TOOL_E_ARG",
            "out must not be the input file (tools never modify their input)",
            {"out": str(resolved)},
        )
    if resolved.suffix.lower() != ".png":
        raise ToolError(
            "TOOL_E_ARG", f"out must be a .png path (got {resolved.name!r})", {"out": str(resolved)}
        )
    return resolved


def write_png(rgb, out) -> dict:
    from PIL import Image

    target = Path(out)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb, mode="RGB").save(target, format="PNG")
        size = target.stat().st_size
    except OSError as exc:
        raise ToolError("TOOL_E_WRITE", f"cannot write PNG: {exc}", {"out": str(target)}) from exc
    return {"out": str(target), "png_bytes": int(size)}


def png_to_rgb(path) -> "object":
    import numpy as np
    from PIL import Image

    resolved = resolve_file(path, what="image")
    try:
        with Image.open(resolved) as image:
            return np.asarray(image.convert("RGB"), dtype=np.uint8)
    except (OSError, ValueError) as exc:
        raise ToolError(
            "TOOL_E_LOAD", f"cannot decode PNG: {exc}", {"path": str(resolved)}
        ) from exc


def rgb_to_png_bytes(rgb) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def encode_png_base64(rgb, *, max_long_side: int = 768) -> dict:
    """PNG-encode ``rgb`` and shrink so the long side is ``<= max_long_side``."""
    from PIL import Image

    image = Image.fromarray(rgb, mode="RGB")
    width, height = image.size
    long_side = max(width, height)
    scaled = long_side > max_long_side
    if scaled:
        scale = max_long_side / float(long_side)
        size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        image = image.resize(size, Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {
        "mimeType": "image/png",
        "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "width": int(image.size[0]),
        "height": int(image.size[1]),
        "scaled": bool(scaled),
        "bytes": len(buffer.getvalue()),
        "long_side_limit": int(max_long_side),
    }


# ---------------------------------------------------------------------------
# Render dispatch (WP-F fast backend, skill exact fallback)
# ---------------------------------------------------------------------------


def _import_render_fast():
    """Return ``(module, None)`` or ``(None, reason)`` - never raises."""
    try:
        from .. import render_fast
    except Exception as exc:  # noqa: BLE001 - WP-F may be absent/broken
        return None, f"render_fast unavailable ({type(exc).__name__}: {exc})"
    if not hasattr(render_fast, "render_auto"):
        return None, "render_fast has no render_auto()"
    return render_fast, None


def render_document(
    doc: dict,
    *,
    resolution: int,
    quality: str = "fast",
    supersample: int = 2,
) -> dict:
    """Render ``doc`` with the best available backend.

    ``quality="fast"`` prefers ``render_fast.render_auto``; when WP-F is not
    ready (or raises) the skill's exact renderer is used and the fallback is
    reported explicitly.  ``quality="exact"`` always uses the skill renderer.
    """
    if quality not in ("fast", "exact"):
        raise ToolError(
            "TOOL_E_ARG", f"quality must be 'fast' or 'exact' (got {quality!r})", {"quality": quality}
        )
    check_int(resolution, name="resolution", minimum=16, maximum=1024)
    check_int(supersample, name="supersample", minimum=1, maximum=4)

    from ..compat import skill_api

    started = time.perf_counter()
    backend = "skill_exact"
    fallback_reason = None
    fast_reasons: list = []
    fast_approximations: list = []
    rgb = None
    if quality == "fast":
        fast_module, reason = _import_render_fast()
        if fast_module is None:
            fallback_reason = reason
        else:
            output = None
            try:
                output = fast_module.render_auto(
                    doc,
                    resolution=resolution,
                    quality="fast",
                    supersample=supersample,
                    return_info=True,
                )
            except TypeError:
                # older WP-F signature without return_info
                try:
                    rgb = fast_module.render_auto(
                        doc,
                        resolution=resolution,
                        quality="fast",
                        supersample=supersample,
                    )
                except Exception as exc:  # noqa: BLE001 - fall back, reason kept
                    fallback_reason = (
                        f"render_fast.render_auto failed ({type(exc).__name__}: {exc})"
                    )
                else:
                    backend = "render_fast"
            except Exception as exc:  # noqa: BLE001 - fall back, never hide (reason kept)
                fallback_reason = f"render_fast.render_auto failed ({type(exc).__name__}: {exc})"
            else:
                if isinstance(output, tuple) and len(output) == 2:
                    rgb, fast_info = output
                else:
                    rgb, fast_info = output, None
                backend = "render_fast"
                if isinstance(fast_info, dict):
                    reasons = fast_info.get("fallback_reason") or []
                    fast_reasons = [str(item) for item in reasons] if isinstance(reasons, list) else [str(reasons)]
                    approximations = fast_info.get("approximations") or []
                    fast_approximations = (
                        [str(item) for item in approximations]
                        if isinstance(approximations, list)
                        else [str(approximations)]
                    )
    if rgb is None:
        try:
            rgb = skill_api.render(doc, resolution=resolution, supersample=supersample)
        except Exception as exc:  # noqa: BLE001 - FBSError or broken checkout
            raise ToolError(
                "TOOL_E_RENDER",
                f"render failed ({backend}): {exc}",
                {"resolution": resolution, "quality": quality, "backend": backend},
            ) from exc

    shape = getattr(rgb, "shape", None)
    if not shape or len(shape) != 3 or int(shape[2]) != 3:
        raise ToolError(
            "TOOL_E_RENDER",
            f"renderer returned unusable pixels (shape={shape!r})",
            {"resolution": resolution, "backend": backend},
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    return {
        "rgb": rgb,
        "backend": backend,
        "quality": quality,
        "fallback": backend != "render_fast" and quality == "fast",
        "fallback_reason": fallback_reason,
        "fast_fallback_reason": fast_reasons,
        "fast_approximations": fast_approximations,
        "elapsed_ms": elapsed_ms,
        "width": int(shape[1]),
        "height": int(shape[0]),
        "supersample": supersample,
    }


def slice_document(doc: dict, keep: int) -> tuple[dict, int, int]:
    """Build an in-memory prefix copy of ``doc`` (never touches the input file).

    Element order is array order, so the prefix keeps the first ``keep``
    strokes.  When the prefix is cut short the trailing ``texts`` are dropped
    (they are drawn after the strokes); ``regions`` stay because kept region
    fills reference them.  Returns ``(copy, kept, total)``.
    """
    if not isinstance(doc, dict):
        raise ToolError("TOOL_E_LOAD", "document must be a JSON object")
    strokes = doc.get("strokes")
    if not isinstance(strokes, list):
        raise ToolError("TOOL_E_LOAD", "document has no 'strokes' array to truncate")
    total = len(strokes)
    kept = max(0, min(int(keep), total))
    truncated = dict(doc)
    truncated["strokes"] = list(strokes[:kept])
    if kept < total:
        texts = doc.get("texts")
        if isinstance(texts, list) and texts:
            truncated["texts"] = []
        stats = doc.get("stats")
        if isinstance(stats, dict):
            truncated["stats"] = dict(stats)
    return truncated, kept, total


# ---------------------------------------------------------------------------
# Document summarisation (shared by inspect / validate)
# ---------------------------------------------------------------------------


def summarize_document(doc: dict, *, kind: str) -> dict:
    """Normalize an ``.fbs``/``render.json`` document into a stable summary."""
    if not isinstance(doc, dict):
        raise ToolError("TOOL_E_LOAD", f"document must be a JSON object (got {type(doc).__name__})")
    strokes = doc.get("strokes")
    strokes = strokes if isinstance(strokes, list) else []
    texts = doc.get("texts")
    texts = texts if isinstance(texts, list) else []
    canvas = doc.get("canvas") or {}
    contract = doc.get("render_contract") or {}
    styles = doc.get("brush_styles") or []

    per_layer: dict[int, dict] = {}
    fill_count = 0
    for stroke in strokes:
        if not isinstance(stroke, dict):
            continue
        layer = stroke.get("layer", 0)
        try:
            layer = int(layer)
        except (TypeError, ValueError):
            layer = 0
        bucket = per_layer.setdefault(layer, {"id": layer, "elements": 0, "strokes": 0, "fills": 0})
        bucket["elements"] += 1
        if stroke.get("fill_role") == "region_fill":
            bucket["fills"] += 1
            fill_count += 1
        else:
            bucket["strokes"] += 1
    layers = [per_layer[key] for key in sorted(per_layer)]

    font_refs = set()
    text_modes = set()
    for text in texts:
        if not isinstance(text, dict):
            continue
        if isinstance(text.get("font"), str):
            font_refs.add(text["font"])
        elif isinstance(text.get("font"), dict) and text["font"].get("family"):
            font_refs.add(str(text["font"]["family"]))
        if text.get("mode"):
            text_modes.add(str(text["mode"]))

    return {
        "kind": kind,
        "format": doc.get("format"),
        "version": doc.get("version"),
        "mode": doc.get("mode"),
        "canvas": {
            "width": canvas.get("width"),
            "height": canvas.get("height"),
            "long_side_units": canvas.get("long_side_units"),
            "origin": canvas.get("origin"),
            "y_axis": canvas.get("y_axis"),
        },
        "render_contract": {
            "target_resolution": contract.get("target_resolution"),
            "epsilon_px": contract.get("epsilon_px"),
            "width_scale": contract.get("width_scale"),
            "width_scale_pinned": "width_scale" in contract,
        },
        "counts": {
            "elements": len(strokes),
            "strokes": len(strokes) - fill_count,
            "fills": fill_count,
            "texts": len(texts),
            "layers": len(layers),
            "brush_styles": len(styles),
        },
        "layers": layers,
        "text": {
            "count": len(texts),
            "font_refs": sorted(font_refs),
            "modes": sorted(text_modes),
            "supported": False if texts else None,
        },
        "base_color": doc.get("base_color"),
        "decomposition": doc.get("decomposition"),
        "image_class": doc.get("image_class"),
        "source": doc.get("source"),
        "created": doc.get("created"),
        "quality": doc.get("quality"),
        "warnings": len(doc.get("warnings") or [])
        if isinstance(doc.get("warnings"), list)
        else 0,
        "fingerprint": doc.get("fingerprint"),
        "regions": len(doc.get("regions") or []) if isinstance(doc.get("regions"), list) else 0,
    }


# ---------------------------------------------------------------------------
# Brush catalog
# ---------------------------------------------------------------------------


def catalog_path() -> Path:
    # <repo>/fourier-prompt/fourier_prompt/tools/common.py
    return Path(__file__).resolve().parents[2] / "assets" / "brush" / "brush_catalog.json"


def load_brush_catalog() -> dict:
    path = catalog_path()
    if not path.is_file():
        raise ToolError(
            "TOOL_E_CATALOG",
            f"brush catalog not found: {path}",
            {"path": str(path)},
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            catalog = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            "TOOL_E_CATALOG", f"cannot load brush catalog: {exc}", {"path": str(path)}
        ) from exc
    if not isinstance(catalog, dict) or not isinstance(catalog.get("presets"), dict):
        raise ToolError(
            "TOOL_E_CATALOG",
            "brush catalog has no 'presets' object",
            {"path": str(path)},
        )
    catalog["_path"] = str(path)
    return catalog
