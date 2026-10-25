"""``status`` tool: environment / skill / dependency self-check (WP-H)."""

from __future__ import annotations

import platform
import shutil
import sys

from .common import TOOL_VERSION, ToolError, jsonable, load_brush_catalog

__all__ = ["status", "TOOL_NAME", "DESCRIPTION", "INPUT_SCHEMA"]

TOOL_NAME = "status"
DESCRIPTION = (
    "Report tool/skill/dependency status: skill path and version, numpy/Pillow "
    "versions, font support, ffmpeg availability, brush catalog counts, .fdraw "
    "version and a skill self-test summary."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "selftest": {
            "type": "boolean",
            "description": "run the skill self-test (one assemble + one 64px render)",
            "default": True,
        }
    },
    "additionalProperties": False,
}


def _skill_status(selftest: bool) -> dict:
    from ..compat import skill_api

    info = {"module": "compat.skill_api", "found": False, "path": None, "version": None}
    try:
        info["path"] = skill_api.skill_dir()
        api = skill_api.import_skill()
        info["found"] = True
        info["version"] = api.version or None
        info["scripts"] = api.scripts
    except skill_api.SkillApiError as exc:
        info["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - status must never fail overall
        info["error"] = f"{type(exc).__name__}: {exc}"

    if not info["found"]:
        info["selftest"] = {"ran": False, "ok": False, "error": "skill not importable"}
        return info
    if not selftest:
        info["selftest"] = {"ran": False, "ok": None}
        return info
    try:
        messages = skill_api.selftest()
    except Exception as exc:  # noqa: BLE001
        info["selftest"] = {"ran": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        info["selftest"] = {"ran": True, "ok": True, "checks": list(messages)}
    return info


def status(*, selftest: bool = True) -> dict:
    """Return a JSON-serializable status report; never raises for missing deps."""
    if not isinstance(selftest, bool):
        raise ToolError(
            "TOOL_E_ARG", f"selftest must be a boolean (got {selftest!r})", {"selftest": selftest}
        )

    try:
        import numpy

        numpy_version = numpy.__version__
    except Exception as exc:  # noqa: BLE001 - numpy is a hard dependency; report honestly
        numpy_version = None
        numpy_error = f"{type(exc).__name__}: {exc}"
    else:
        numpy_error = None

    try:
        import PIL

        pillow_version = getattr(PIL, "__version__", None)
    except Exception as exc:  # noqa: BLE001
        pillow_version = None
        pillow_error = f"{type(exc).__name__}: {exc}"
    else:
        pillow_error = None

    from .. import fdraw

    brush = {"path": None, "active": None, "planned": None, "total": None}
    try:
        catalog = load_brush_catalog()
    except ToolError as exc:
        brush["error"] = exc.to_dict()
    else:
        presets = catalog["presets"]
        counts = catalog.get("counts") or {}
        active = sum(1 for item in presets.values() if item.get("status") == "active")
        planned = sum(1 for item in presets.values() if item.get("status") != "active")
        brush.update(
            {
                "path": catalog.get("_path"),
                "active": int(counts.get("active", active)),
                "planned": int(counts.get("planned", planned)),
                "total": int(counts.get("total", len(presets))),
                "catalog_version": catalog.get("catalog_version") or catalog.get("schema"),
            }
        )

    ffmpeg_path = shutil.which("ffmpeg")
    render_fast_info = {"available": False, "reason": None}
    try:
        from .. import render_fast as fast_module
    except Exception as exc:  # noqa: BLE001 - WP-F optional at this stage
        render_fast_info["reason"] = f"{type(exc).__name__}: {exc}"
    else:
        render_fast_info["available"] = hasattr(fast_module, "render_auto")
        if not render_fast_info["available"]:
            render_fast_info["reason"] = "render_fast has no render_auto()"

    skill = _skill_status(selftest)

    report = {
        "ok": True,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "package": {"version": _package_version(), "python": sys.version.split()[0]},
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "skill": skill,
        "dependencies": {
            "numpy": {"version": numpy_version, "error": numpy_error},
            "pillow": {"version": pillow_version, "error": pillow_error},
        },
        "fonts": {
            "available": False,
            "text_rendering": False,
            "note": "font/text subsystem is not implemented (batch 5); .fbs texts are counted but not rendered",
        },
        "ffmpeg": {
            "available": bool(ffmpeg_path),
            "path": ffmpeg_path,
            "note": "video output is a later batch; ffmpeg is detected only",
        },
        "brush_catalog": brush,
        "fdraw": {"version": fdraw.FDRAW_VERSION, "spec_version": fdraw.FDRAW_SPEC_VERSION},
        "render_backends": {
            "render_fast": render_fast_info,
            "skill_exact": {"available": bool(skill["found"])},
        },
    }
    return jsonable(report)


def _package_version() -> str:
    from .. import __version__

    return __version__
