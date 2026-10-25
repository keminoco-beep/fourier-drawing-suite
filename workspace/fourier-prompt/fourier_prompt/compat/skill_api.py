"""Single adaptation layer for the ``fourier-brushstroke`` skill (WP-D).

Everything in ``fourier-prompt`` that needs the skill goes through this module:
path resolution, ``sys.path`` injection, the public API re-exports and a
self-test.  The skill checkout is **read only** for this package.

``skill_dir()`` resolves in this order:

1. the ``FBS_SKILL_DIR`` environment variable (the skill root *or* its
   ``scripts/`` directory);
2. the workspace sibling ``<repo>/fourier-brushstroke`` (derived from this
   file's location, independent of the current working directory);
3. ``./fourier-brushstroke`` and ``../fourier-brushstroke`` (run-from-workspace
   fallbacks).

A candidate is accepted only when ``scripts/fbs_core/__init__.py`` exists, so a
wrong path fails loudly instead of importing an unrelated package.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

__all__ = [
    "SkillApiError",
    "skill_dir",
    "scripts_dir",
    "import_skill",
    "assemble_document",
    "stroke_record",
    "StyleTable",
    "render",
    "write_fbs",
    "selftest",
    "fbs_config",
    "warning_log",
    "preset_shape",
    "brush_presets",
    "is_skill_error",
]

_ENV_VAR = "FBS_SKILL_DIR"
_CACHE: dict[str, types.SimpleNamespace] = {}


class SkillApiError(RuntimeError):
    """Raised when the skill checkout cannot be located or imported."""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _skill_scripts(candidate: Path) -> Path | None:
    """Return ``candidate/scripts`` when it holds ``fbs_core``, else ``None``."""
    candidate = candidate.expanduser().resolve()
    if (candidate / "scripts" / "fbs_core" / "__init__.py").is_file():
        return candidate / "scripts"
    # ``FBS_SKILL_DIR`` may point straight at ``scripts/``.
    if (candidate / "fbs_core" / "__init__.py").is_file():
        return candidate
    return None


def _candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get(_ENV_VAR)
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    # <repo>/fourier-prompt/fourier_prompt/compat/skill_api.py
    if len(here.parents) >= 4:
        candidates.append(here.parents[3] / "fourier-brushstroke")
    cwd = Path.cwd()
    candidates.extend([cwd / "fourier-brushstroke", cwd.parent / "fourier-brushstroke"])
    return candidates


def scripts_dir() -> str:
    """Absolute path of the skill's ``scripts/`` directory."""
    for candidate in _candidates():
        found = _skill_scripts(candidate)
        if found is not None:
            return str(found)
    checked = ", ".join(str(p) for p in _candidates())
    raise SkillApiError(
        f"cannot locate fourier-brushstroke scripts/ (set {_ENV_VAR}); checked: {checked}"
    )


def skill_dir() -> str:
    """Absolute path of the skill root (the directory that owns ``scripts/``)."""
    return str(Path(scripts_dir()).parent)


# ---------------------------------------------------------------------------
# Import cache
# ---------------------------------------------------------------------------


def import_skill() -> types.SimpleNamespace:
    """Inject ``scripts/`` into ``sys.path`` once and import ``fbs_core`` modules.

    Returns a namespace with the modules this package is allowed to use:
    ``document``, ``io_format``, ``render``, ``brush``, ``brushlib``,
    ``fourier``, ``config`` and ``util``.
    """
    scripts = scripts_dir()
    cached = _CACHE.get(scripts)
    if cached is not None:
        return cached
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        from fbs_core import brush, brushlib, config, document, fourier, io_format, render, util
    except Exception as exc:  # pragma: no cover - broken checkout
        raise SkillApiError(f"cannot import fbs_core from {scripts}: {exc}") from exc
    namespace = types.SimpleNamespace(
        scripts=scripts,
        document=document,
        io_format=io_format,
        render=render,
        brush=brush,
        brushlib=brushlib,
        fourier=fourier,
        config=config,
        util=util,
    )
    module = sys.modules.get("fbs_core")
    namespace.version = str(getattr(module, "__version__", "") or "")
    _CACHE[scripts] = namespace
    return namespace


# ---------------------------------------------------------------------------
# Public API re-exports
# ---------------------------------------------------------------------------


def assemble_document(*args, **kwargs):
    """Delegate to ``fbs_core.document.assemble_document`` (see WP-B contract)."""
    return import_skill().document.assemble_document(*args, **kwargs)


def stroke_record(*args, **kwargs):
    """Delegate to ``fbs_core.document.stroke_record``."""
    return import_skill().document.stroke_record(*args, **kwargs)


def StyleTable():
    """Return a fresh ``fbs_core.document.StyleTable`` instance."""
    return import_skill().document.StyleTable()


def render(
    doc: dict,
    *,
    resolution: int,
    supersample: int = 2,
    mode: str = "sweep",
    width_scale: float | None = None,
):
    """Render ``doc`` to an ``(H, W, 3)`` uint8 RGB array via the skill.

    If the document pins ``render_contract.width_scale`` (compiled FDSL does: 1.0),
    honor it unless the caller passes an explicit override. Documents without a
    pinned value keep the skill's legacy auto behavior.
    """
    kwargs = {
        "resolution": int(resolution),
        "supersample": int(supersample),
        "mode": str(mode),
    }
    if width_scale is not None:
        kwargs["width_scale"] = float(width_scale)
    else:
        contract = doc.get("render_contract") or {}
        if "width_scale" in contract:
            kwargs["width_scale"] = float(contract["width_scale"])
    return import_skill().render.render(doc, **kwargs)


def write_fbs(doc: dict, path: str | None = None) -> bytes:
    """Quantize + validate ``doc`` through ``io_format.write_fbs`` and return bytes.

    With ``path`` the file is written there; otherwise a temporary file is used
    and removed.  Validation errors propagate as the skill's ``FBSError``.
    """
    api = import_skill()
    if path:
        api.io_format.write_fbs(doc, os.fspath(path))
        with open(path, "rb") as handle:
            return handle.read()
    handle, tmp = tempfile.mkstemp(prefix="fourier-prompt-", suffix=".fbs.json")
    os.close(handle)
    try:
        api.io_format.write_fbs(doc, tmp)
        with open(tmp, "rb") as stream:
            return stream.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:  # pragma: no cover - best effort cleanup
            pass


def fbs_config(target_resolution: int = 256, epsilon_px: float = 0.5):
    """Return an ``FBSConfig`` bound to the render contract used by the compiler."""
    return import_skill().config.FBSConfig(
        target_resolution=int(target_resolution), epsilon_px=float(epsilon_px)
    )


def warning_log():
    """Return an empty skill ``WarningLog``."""
    return import_skill().util.WarningLog()


def brush_presets() -> tuple[str, ...]:
    """The skill's built-in brush preset ids (20 active presets)."""
    presets = import_skill().brushlib.BRUSHES
    return tuple(sorted(presets))


def preset_shape(preset: str) -> str:
    """Resolve a brush preset id to its shape kernel id."""
    presets = import_skill().brushlib.BRUSHES
    try:
        return str(presets[str(preset)]["shape"])
    except KeyError as exc:
        raise SkillApiError(f"unknown brush preset {preset!r}") from exc


def is_skill_error(exc: BaseException) -> bool:
    """True when ``exc`` is one of the skill's structured ``FBSError`` values."""
    try:
        api = import_skill()
    except SkillApiError:  # pragma: no cover - path broken while unwinding
        return False
    error_type = getattr(api.util, "FBSError", None)
    if error_type is None:  # pragma: no cover - older checkout
        return False
    return isinstance(exc, error_type)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def selftest() -> list[str]:
    """Path/version/one minimal assembly/one render check.

    Returns human-readable ``ok`` lines; raises :class:`SkillApiError` on the
    first failure (``selftest`` must never pretend to pass).
    """
    import numpy as np

    api = import_skill()
    messages = [f"skill_dir: {skill_dir()}", f"fbs_core: {api.version or 'unknown'}"]
    points = np.asarray([[0.1, 0.1], [0.5, 0.2], [0.9, 0.8]], dtype=np.float64)
    element = {
        "points": points,
        "s": np.asarray([0.0, 0.5, 1.0]),
        "closed": False,
        "widths": np.full(len(points), 4.0 / 256.0),
        "colors": np.tile(np.asarray([0.1, 0.1, 0.2]).reshape(1, 3), (len(points), 1)),
        "alphas": np.ones(len(points)),
        "shapes": ["round"] * len(points),
        "fill_role": "medial_branch",
        "fill_poly": None,
        "region_id": -1,
        "layer": 0,
    }
    try:
        doc, info = api.document.assemble_document(
            [element],
            cfg=fbs_config(64, 0.5),
            warn=warning_log(),
            source={"path": "", "sha256": "", "width": 64, "height": 64},
            image_class={"klass": "text_plan", "evidence": {}},
            canvas={
                "width": 1.0,
                "height": 1.0,
                "long_side_units": 1.0,
                "origin": "top-left",
                "y_axis": "down",
            },
            decomposition={"method": "skill_selftest", "params": {"fill_soft_px": 0.5}},
            base_color=[255, 255, 255],
            fill_soft_px=0.5,
            record_ids="sequential",
        )
    except Exception as exc:  # noqa: BLE001 - report the real failure
        raise SkillApiError(f"assemble_document selftest failed: {type(exc).__name__}: {exc}") from exc
    messages.append(f"assemble_document: ok ({info['strokes']} stroke)")

    data = write_fbs(doc)
    messages.append(f"write_fbs: ok ({len(data)} bytes)")

    handle, tmp = tempfile.mkstemp(prefix="fourier-prompt-selftest-", suffix=".fbs.json")
    os.close(handle)
    try:
        write_fbs(doc, tmp)
        reloaded = api.io_format.read_fbs(tmp)
        rgb = render(reloaded, resolution=64, supersample=1)
    finally:
        try:
            os.unlink(tmp)
        except OSError:  # pragma: no cover
            pass
    if getattr(rgb, "shape", None) != (64, 64, 3):
        raise SkillApiError(f"render selftest returned shape {getattr(rgb, 'shape', None)!r}")
    messages.append("render: ok (64x64x3)")
    return messages
