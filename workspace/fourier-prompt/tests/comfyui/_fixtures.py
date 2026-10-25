"""Shared mock harness for the ComfyUI-FDP node tests (WP-L).

The real ComfyUI installation was not found on this machine, so the package is
imported with forged ``folder_paths`` / ``comfy`` modules and (in one test) a
fake ``torch``.  The ``fourier_prompt`` core is imported for real; only the
ComfyUI host API is mocked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]
CUSTOM_NODES = PKG_ROOT / "comfyui" / "ComfyUI-FDP"
for candidate in (str(PKG_ROOT), str(HERE)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fourier_prompt import fdraw  # noqa: E402
from fourier_prompt.compile import compile_plan  # noqa: E402

PLAN_RESOLUTION = 64

ROOT = Path(tempfile.mkdtemp(prefix="fdp-comfyui-"))
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
(INPUT_DIR / "fdraw").mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "fdraw").mkdir(parents=True, exist_ok=True)
FOLDERS = {"input": str(INPUT_DIR), "output": str(OUTPUT_DIR)}
INTERRUPT = {"flag": False}


def mini_plan() -> dict:
    """A small valid FDSL v2.1 plan (1 fill + 3 strokes, 4 elements)."""
    return {
        "schema": "fdsl/2.1",
        "name": "wp-l-fixture",
        "canvas": {"width": 128, "height": 128, "background": [250, 248, 242]},
        "palette": {"ink": "#12203a", "gold": "#e8c37a", "red": "#b23b3b"},
        "layers": [{"id": 0, "name": "F1"}, {"id": 1, "name": "L1"}],
        "elements": [
            {
                "kind": "fill",
                "layer": 0,
                "color": "ink",
                "poly": [[8, 8], [120, 8], [120, 120], [8, 120]],
            },
            {
                "kind": "fourier_gen",
                "gen": "circle",
                "layer": 1,
                "id": "e1",
                "transform": {"center": [64, 64], "scale": 0.6},
                "params": {"r": 1.0},
                "brush": {"preset": "ink_gpen", "width": 2.0, "color": "gold", "alpha": 0.9},
            },
            {
                "kind": "stroke",
                "layer": 1,
                "points": [[16, 120], [120, 16]],
                "brush": {"preset": "ink_fineliner", "width": 1.5, "alpha": 0.8},
            },
            {
                "kind": "stroke",
                "layer": 1,
                "points": [[16, 16], [64, 112], [112, 16]],
                "brush": {"preset": "basic_round_soft", "width": 4.0, "color": "red", "alpha": 1.0},
            },
        ],
        "constraints": {"max_elements": 400, "single_line": False},
    }


def _build_sample() -> dict:
    plan = mini_plan()
    compiled = compile_plan(plan, resolution=PLAN_RESOLUTION)
    sample_path = INPUT_DIR / "fdraw" / "mini.fdraw"
    fdraw.pack(compiled["fbs"], source_fdsl=plan, out=str(sample_path))
    second_path = INPUT_DIR / "fdraw" / "mini_copy.fdraw"
    fdraw.pack(compiled["fbs"], source_fdsl=plan, out=str(second_path))
    bad_path = INPUT_DIR / "fdraw" / "bad.fdraw"
    bad_path.write_bytes(b"this is not a zip container")
    plan_path = ROOT / "mini.plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "plan": plan,
        "fbs": compiled["fbs"],
        "fdraw": sample_path,
        "fdraw2": second_path,
        "bad": bad_path,
        "plan_path": plan_path,
    }


SAMPLE = _build_sample()


def install_mocks() -> None:
    """Forged ComfyUI host modules (idempotent)."""
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.folder_names_and_paths = {}

    def get_input_directory() -> str:
        return FOLDERS["input"]

    def get_output_directory() -> str:
        return FOLDERS["output"]

    def get_filename_list(folder: str) -> list[str]:
        directory = Path(FOLDERS["input"]) / folder
        if not directory.is_dir():
            return []
        return sorted(item.name for item in directory.glob("*.fdraw"))

    def get_full_path(folder: str, name: str) -> str | None:
        path = Path(FOLDERS["input"]) / folder / name
        return str(path) if path.is_file() else None

    folder_paths.get_input_directory = get_input_directory
    folder_paths.get_output_directory = get_output_directory
    folder_paths.get_filename_list = get_filename_list
    folder_paths.get_full_path = get_full_path
    sys.modules["folder_paths"] = folder_paths

    comfy = types.ModuleType("comfy")
    comfy.__path__ = []  # type: ignore[attr-defined]
    model_management = types.ModuleType("comfy.model_management")
    model_management.processing_interrupted = lambda: INTERRUPT["flag"]
    utils = types.ModuleType("comfy.utils")

    class ProgressBar:  # minimal V1-compatible stand-in
        def __init__(self, total: int = 0):
            self.total = total

        def update(self, value: int, total: int | None = None) -> None:
            self.total = total if total is not None else self.total

    utils.ProgressBar = ProgressBar
    comfy.model_management = model_management
    comfy.utils = utils
    sys.modules["comfy"] = comfy
    sys.modules["comfy.model_management"] = model_management
    sys.modules["comfy.utils"] = utils


install_mocks()


def load_package():
    """Import ``ComfyUI-FDP`` under a valid module name (dir has a hyphen)."""
    name = "ComfyUI_FDP"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        str(CUSTOM_NODES / "__init__.py"),
        submodule_search_locations=[str(CUSTOM_NODES)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PACKAGE = load_package()
NODES = PACKAGE.NODE_CLASS_MAPPINGS

__all__ = [
    "HERE",
    "PKG_ROOT",
    "CUSTOM_NODES",
    "INPUT_DIR",
    "OUTPUT_DIR",
    "FOLDERS",
    "INTERRUPT",
    "ROOT",
    "PLAN_RESOLUTION",
    "SAMPLE",
    "PACKAGE",
    "NODES",
    "mini_plan",
    "install_mocks",
    "load_package",
]
