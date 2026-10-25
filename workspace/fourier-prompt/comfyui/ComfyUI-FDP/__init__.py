"""ComfyUI-FDP — Fourier Drawing Program nodes for ComfyUI (V1 API only).

Install: copy/symlink this directory into ``ComfyUI/custom_nodes/ComfyUI-FDP``
and make the ``fourier_prompt`` package importable in the ComfyUI environment
(``pip install -e <repo>/fourier-prompt`` or set ``FDP_CORE_PATH`` to the
directory that *contains* ``fourier_prompt/``).  No third-party custom nodes
and no extra pip dependencies are required; video encoding uses the
``imageio-ffmpeg`` already bundled with ComfyUI distributions.

The package deliberately ships no web extension: ``WEB_DIRECTORY = None``.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _bootstrap_core() -> None:
    """Make ``fourier_prompt`` importable from the repo layout / env var."""
    candidates = [os.environ.get("FDP_CORE_PATH")]
    # repo layout: fourier-prompt/comfyui/ComfyUI-FDP -> fourier-prompt
    candidates.append(os.path.abspath(os.path.join(_HERE, "..", "..")))
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.abspath(candidate)
        if os.path.isdir(os.path.join(candidate, "fourier_prompt")) and candidate not in sys.path:
            sys.path.insert(0, candidate)


_bootstrap_core()


def _register_fdraw_folders() -> None:
    """Register ``input/fdraw`` + ``output/fdraw`` once (idempotent)."""
    try:
        import folder_paths  # type: ignore
    except Exception:
        return
    try:
        directories: list[str] = []
        for getter in ("get_input_directory", "get_output_directory"):
            fn = getattr(folder_paths, getter, None)
            if not callable(fn):
                continue
            try:
                base = os.fspath(fn())
            except Exception:
                continue
            if not base:
                continue
            target = os.path.join(base, "fdraw")
            try:
                os.makedirs(target, exist_ok=True)
            except OSError:
                pass
            directories.append(target)
            directories.append(base)
        registry = getattr(folder_paths, "folder_names_and_paths", None)
        if isinstance(registry, dict):
            unique = list(dict.fromkeys(directories))
            registry["fdraw"] = (unique, {".fdraw"})
    except Exception as exc:  # never break ComfyUI start-up for a folder hint
        import logging

        logging.getLogger("fourier_prompt.comfyui").warning("FDP folder registration skipped: %s", exc)


_register_fdraw_folders()

try:
    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - install-time guidance
    if exc.name and (exc.name == "fourier_prompt" or exc.name.startswith("fourier_prompt.")):
        raise ImportError(
            "ComfyUI-FDP 需要 fourier_prompt 包：在 ComfyUI 的 Python 环境里执行 "
            "`pip install -e <repo>/fourier-prompt`，或设置环境变量 FDP_CORE_PATH "
            "指向包含 fourier_prompt/ 的目录（例如 G:\\...\\fourier-prompt）。"
        ) from exc
    raise

WEB_DIRECTORY = None

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
