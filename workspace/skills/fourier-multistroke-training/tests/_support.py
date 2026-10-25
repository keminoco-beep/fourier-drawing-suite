"""Shared helpers for the ``fourier-multistroke-training`` unittest suite.

Everything here is stdlib + numpy/Pillow/scipy (already skill dependencies);
``pytest`` is intentionally not required.  Paths are derived from ``__file__``
so the suite is independent of the current working directory and of the
machine layout.  The Python interpreter used for child processes is
``sys.executable`` (never a hard-coded path).

Optional baseline hook
----------------------
Set ``FOURIER_LAYERED_BASE`` to another ``layered_base.py`` (e.g. the pre-fix
snapshot in ``work/backup/skill_before_v1.33/scripts/layered_base.py``) to make
the behavioural tests run against that file.  The default (unset) always uses
the skill's own ``scripts/layered_base.py``.  Verifier runs need no env var.
"""
from __future__ import annotations

import ast
import functools
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

# ------------------------------------------------------------------ paths
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
REFERENCES_DIR = os.path.join(SKILL_ROOT, "references")
SKILL_MD = os.path.join(SKILL_ROOT, "SKILL.md")
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(SKILL_ROOT))
DEFAULT_LAYERED = os.path.join(SCRIPTS_DIR, "layered_base.py")
PYTHON = sys.executable

#: Frozen v1.33 gradient-gate defaults (Lead-confirmed interface contract).
GATE_DEFAULT_MAX_DELTA = 0.12
GATE_DEFAULT_EDGE_DELTA = 0.10
GATE_DEFAULT_EDGE_PCT = 100.0

#: Per-pass blur sigma used by ``--upper-color-mode source`` in layered_base.py
#: (pass2: upper_blur, pass3: upper_blur*0.7 floored at 1.0,
#:  pass4: upper_blur*0.45 floored at 0.8).  Used to reproduce the frozen
#: endpoint-delta definition from the saved strokes JSON.
PASS_SOURCE_SIGMAS = {"base_pass2": 2.5, "base_pass3": 1.75, "base_pass4": 1.125}

#: Path samples used by the gate for the "does the stroke cross a sharp colour
#: step" check (v1.33 grew this from 5 to 9; read from the module at runtime).
PATH_SAMPLES_FALLBACK = 9


def path_sample_count():
    """Number of path samples used by the gradient gate (module constant)."""
    try:
        module = import_layered_base()
        return int(getattr(module, "_GRADIENT_PATH_SAMPLES", 5))
    except Exception:  # pragma: no cover - defensive
        return PATH_SAMPLES_FALLBACK


def has_step_diagnostics(report) -> bool:
    """True when the report carries the v1.33 path-step diagnostics.

    ``gradient_mean_step``/``gradient_max_step`` are informative only: the
    Lead-decided final contract rejects *every* stroke with
    ``d_end > --gradient-max-delta`` (no smooth-ramp exception).  The fast suite
    guards that rule with a synthetic smooth-ramp unit test.
    """
    return "gradient_mean_step" in report


def layered_script() -> str:
    """Path of the layered_base.py under test (honours FOURIER_LAYERED_BASE)."""
    override = os.environ.get("FOURIER_LAYERED_BASE")
    if override:
        path = override
        if not os.path.isabs(path):
            path = os.path.join(WORKSPACE_ROOT, path)
        return os.path.abspath(path)
    return DEFAULT_LAYERED


# ------------------------------------------------------------------ subprocess
def run_cli(args, timeout=300, cwd=None):
    """Run ``sys.executable <args...>`` and capture text output."""
    argv = [PYTHON] + [str(a) for a in args]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          cwd=str(cwd or SKILL_ROOT))


def run_layered(args, timeout=300):
    """Run the layered_base.py under test (default or FOURIER_LAYERED_BASE)."""
    return run_cli([layered_script()] + list(args), timeout=timeout)


def run_layered_ok(testcase, args, timeout=300, label=""):
    """Run layered_base and fail the test with captured output on non-zero exit."""
    proc = run_layered(args, timeout=timeout)
    if proc.returncode != 0:
        testcase.fail(
            f"layered_base.py {label} exited {proc.returncode}\n"
            f"argv: {layered_script()} {' '.join(str(a) for a in args)}\n"
            f"--- stdout ---\n{proc.stdout[-3000:]}\n--- stderr ---\n{proc.stderr[-4000:]}")
    return proc


@functools.lru_cache(maxsize=8)
def _script_help_cached(script_path: str) -> str:
    proc = run_cli([script_path, "--help"], timeout=60)
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def script_help(script_path=None) -> str:
    """``<script> --help`` text (cached; diagnostics only, rc checked elsewhere)."""
    return _script_help_cached(os.path.abspath(script_path or layered_script()))


def has_cli_arg(name, script_path=None) -> bool:
    """True if the CLI option appears in the script's --help output."""
    return name in script_help(script_path)


def has_gradient_gate_cli() -> bool:
    return has_cli_arg("--gradient-gate")


# ------------------------------------------------------------------ module import
_MODULE_CACHE = {}


def import_layered_base():
    """Import the layered_base.py under test as a throwaway module."""
    path = os.path.abspath(layered_script())
    if path in _MODULE_CACHE:
        return _MODULE_CACHE[path]
    scripts_dir = os.path.dirname(path)
    for p in (scripts_dir, SCRIPTS_DIR):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location("layered_base_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULE_CACHE[path] = module
    return module


def has_gradient_api(module) -> bool:
    """True when the imported module implements the v1.33 gradient gate API."""
    try:
        import inspect
        if not hasattr(module, "_new_gradient_stats"):
            return False
        return "gradient_gate" in inspect.signature(module.tile_pass).parameters
    except (AttributeError, ValueError, TypeError):
        return False


# ------------------------------------------------------------------ json / files
def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ argparse introspection
def argparse_defaults(script_path=None) -> dict:
    """Parse ``add_argument`` option -> default from source (no import needed).

    ``store_true`` flags with no explicit default are reported as ``False``.
    Defaults that are not literals are reported as ``"<non-literal>"``.
    """
    path = os.path.abspath(script_path or layered_script())
    tree = ast.parse(read_text(path))
    defaults = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"):
            continue
        option = None
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value.startswith("--"):
                option = arg.value
                break
        if option is None:
            continue
        default = None
        action = None
        for kw in node.keywords:
            if kw.arg == "default":
                try:
                    default = ast.literal_eval(kw.value)
                except (ValueError, SyntaxError):
                    default = "<non-literal>"
            elif kw.arg == "action" and isinstance(kw.value, ast.Constant):
                action = kw.value.value
        if default is None and action == "store_true":
            default = False
        defaults[option] = default
    return defaults


def argparse_choices(script_path=None) -> dict:
    """Parse ``choices=`` for each option (best effort)."""
    path = os.path.abspath(script_path or layered_script())
    tree = ast.parse(read_text(path))
    choices = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"):
            continue
        option = next((a.value for a in node.args
                       if isinstance(a, ast.Constant) and isinstance(a.value, str)
                       and a.value.startswith("--")), None)
        for kw in node.keywords:
            if kw.arg == "choices":
                try:
                    choices[option] = list(ast.literal_eval(kw.value))
                except (ValueError, SyntaxError):
                    choices[option] = "<non-literal>"
    return choices


# ------------------------------------------------------------------ synthetic scenes
def scene_canvas(W=192, H=144, margin=16):
    """White canvas margin + red/blue colour blocks (CLI smoke scene).

    The white margin exercises the foreground mask (``white_px_in_fg`` must stay
    0); the inner blocks give the layered passes real colour boundaries.
    """
    img = np.ones((H, W, 3), dtype=np.float64)
    x0, x1 = margin, W - margin
    y0, y1 = margin, H - margin
    mid = (x0 + x1) // 2
    img[y0:y1, x0:mid] = (0.85, 0.10, 0.10)
    img[y0:y1, mid:x1] = (0.10, 0.10, 0.85)
    img[y0 + 14:y0 + 36, x0 + 10:x0 + 38] = (0.95, 0.85, 0.10)
    img[y0 + 14:y0 + 36, mid + 10:mid + 38] = (0.04, 0.04, 0.04)
    img[y0 + 50:y1 - 6, mid - 2:mid] = (0.02, 0.02, 0.02)
    return img


def scene_split(W=192, H=144):
    """Full-foreground red|blue vertical split + yellow/black/green blocks.

    Hard 0.75 max-abs colour steps at the split: exactly the situation where a
    naive gradient stroke straddles two colours and smears.
    """
    img = np.empty((H, W, 3), dtype=np.float64)
    mid = W // 2
    img[:, :mid] = (0.85, 0.10, 0.10)
    img[:, mid:] = (0.10, 0.10, 0.85)
    y0, x0 = H // 5, W // 6
    img[y0:y0 + H // 5, x0:x0 + W // 7] = (0.95, 0.85, 0.10)
    img[y0:y0 + H // 5, mid + x0:mid + x0 + W // 7] = (0.04, 0.04, 0.04)
    img[H // 2:H // 2 + H // 8, mid + x0:mid + x0 + W // 7] = (0.10, 0.70, 0.15)
    return img


def scene_checker(H=64, W=64, tile=16):
    """Full-foreground checker of saturated colours (unit tests / worst case)."""
    palette = np.array([
        [0.85, 0.10, 0.10],
        [0.10, 0.10, 0.85],
        [0.95, 0.85, 0.10],
        [0.04, 0.04, 0.04],
        [0.10, 0.70, 0.15],
        [0.80, 0.80, 0.80],
    ])
    yy = np.arange(H)[:, None]
    xx = np.arange(W)[None, :]
    return palette[((xx // tile) + (yy // tile)) % len(palette)]


#: v1.34 texture-flat stroke kind (frozen with fixer-core / Lead).
TEXTURE_FLAT_KIND = "oil_flat_texture"
#: total stroke hard cap enforced by layered_base.py (v1.34).
MAX_TOTAL_STROKES = 100000
#: texture-flat sizes are specified in px at a 1280 long side and scaled by
#: ``rs = max(H, W) / 1280``.
TEXTURE_FLAT_REF_RES = 1280.0
SCENE_FLAT_NOISE_COLOR = (0.35, 0.55, 0.75)


def scene_flat_noise(H=144, W=192, seed=12345):
    """Left half flat colour (locally smooth), right half high-freq noise.

    Used by the texture-flat tests: the flat pass must only paint the locally
    flat side of the *current render*.
    """
    img = np.empty((H, W, 3), dtype=np.float64)
    img[:, : W // 2] = SCENE_FLAT_NOISE_COLOR
    rng = np.random.default_rng(seed)
    img[:, W // 2:] = rng.uniform(0.05, 0.85, size=(H, W - W // 2, 3))
    return img


def flat_render(H=96, W=128):
    """Render mock: flat left half, textured (noisy) right half."""
    ren = scene_flat_noise(H, W)
    ren[:, : W // 2] = SCENE_FLAT_NOISE_COLOR
    return ren


def texture_flat_strokes(strokes):
    """Subset of saved strokes emitted by the v1.34 texture-flat pass."""
    return [s for s in strokes if str(s.get("kind", "")) == TEXTURE_FLAT_KIND]


def expected_texture_width_px(work_res):
    """Default texture width range converted to work-res pixels."""
    rs = float(work_res) / TEXTURE_FLAT_REF_RES
    return 2.5 * rs, 3.5 * rs


def expected_texture_length_px(work_res):
    """Default texture length range converted to work-res pixels."""
    rs = float(work_res) / TEXTURE_FLAT_REF_RES
    return 5.0 * rs, 9.0 * rs


SCENES = {
    "canvas": scene_canvas,
    "split": scene_split,
    "checker": scene_checker,
}


def write_scene(path, rgb):
    """Write a float [0,1] RGB array as an 8-bit PNG."""
    Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8)).save(path)
    return path


# ------------------------------------------------------------------ image analysis
def _hybrid():
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    import hybrid_gen  # noqa: WPS433 (deliberate lazy import)
    return hybrid_gen


def load_rgb(path, work_res):
    """Load an image exactly like layered_base.py does (long side = work_res)."""
    return _hybrid().load_image(str(path), int(work_res))


def fg_mask(rgb):
    """Foreground mask used by layered_base.py (white-ish canvas is background)."""
    return ~np.all(rgb > 0.945, axis=2)


def reconstruct_gradient_deltas(strokes, rgb):
    """Reproduce the gate definitions from the saved strokes JSON.

    Returns one dict per gradient stroke::

        kind       pass kind (base_pass2/3/4)
        d_end      max_c |src(start)_c - src(end)_c|          (gate: delta)
        d_adj      max(inf_norm(start-mid), inf_norm(mid-end)) (legacy edge colour)
        max_step   max adjacent colour step over the gate's path samples
                   (v1.33 smooth-ramp check: reject large d_end only when the
                    profile has a sharp step > 0.5 * edge_delta)
        stored_max max_c |stored start_c - stored end_c|       (jittered, report only)
    """
    hg = _hybrid()
    H, W = rgb.shape[:2]
    scale = float(max(H, W))
    fg = fg_mask(rgb)
    samples = max(2, int(path_sample_count()))
    maps = {kind: hg.gaussian_blur(rgb.astype(np.float32), sigma).astype(np.float64)
            for kind, sigma in PASS_SOURCE_SIGMAS.items()}
    rows = []
    for stroke in strokes:
        gradient = stroke.get("gradient")
        if not gradient:
            continue
        kind = str(stroke.get("kind", ""))
        src = maps.get(kind)
        points = np.asarray(stroke["points"], dtype=np.float64) * scale
        stored_start = np.asarray(gradient.get("start", [0, 0, 0]), dtype=np.float64)
        stored_end = np.asarray(gradient.get("end", [0, 0, 0]), dtype=np.float64)
        row = {
            "kind": kind,
            "points": int(len(points)),
            "stored_max": float(np.max(np.abs(stored_start - stored_end))),
        }
        if src is None:
            row["d_end"] = None
            row["d_adj"] = None
            row["max_step"] = None
            rows.append(row)
            continue
        xi = np.clip(points[:, 0].astype(int), 0, W - 1)
        yi = np.clip(points[:, 1].astype(int), 0, H - 1)
        raw0 = np.asarray(src[yi[0], xi[0]], dtype=np.float64)
        raw1 = np.asarray(src[yi[-1], xi[-1]], dtype=np.float64)
        if len(points) >= 3:
            mid = len(points) // 2
            rawm = np.asarray(src[yi[mid], xi[mid]], dtype=np.float64)
        else:
            # v1.37 silhouette endpoint trimming can leave a 2-point stroke:
            # use the segment midpoint colour as the "middle" sample.
            rawm = 0.5 * (raw0 + raw1)
        row["d_end"] = float(np.max(np.abs(raw0 - raw1)))
        row["d_adj"] = float(max(np.max(np.abs(raw0 - rawm)),
                                 np.max(np.abs(rawm - raw1))))
        # Reproduce the 5->9 point path sampling used by the gate.
        ax, ay = float(points[0, 0]), float(points[0, 1])
        bx, by = float(points[-1, 0]), float(points[-1, 1])
        prev = None
        max_step = 0.0
        for k in range(samples):
            t = k / (samples - 1.0)
            sx = int(ax + t * (bx - ax))
            sy = int(ay + t * (by - ay))
            sx = 0 if sx < 0 else (W - 1 if sx >= W else sx)
            sy = 0 if sy < 0 else (H - 1 if sy >= H else sy)
            if not fg[sy, sx]:
                continue
            current = np.asarray(src[sy, sx], dtype=np.float64)
            if prev is not None:
                max_step = max(max_step, float(np.max(np.abs(current - prev))))
            prev = current
        row["max_step"] = max_step
        rows.append(row)
    return rows


def stray_color_pixels(rgb, png_path, thr=0.25):
    """Recompute the report's ``stray_color_px`` from the saved PNG.

    The script measures the float render before 8-bit quantisation, so the
    recomputed count may differ by a few pixels; callers should allow a small
    tolerance.  Returns ``(stray_px, fg_px)``.
    """
    ren = np.asarray(Image.open(png_path).convert("RGB"), dtype=np.float64) / 255.0
    fg = fg_mask(rgb)
    diff = np.max(np.abs(ren - rgb), axis=2)
    return int(((diff > float(thr)) & fg).sum()), int(fg.sum())


# ------------------------------------------------------------------ underpainting helpers (v1.35)
def _hatch():
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    import hatch_gen  # noqa: WPS433 (deliberate lazy import)
    return hatch_gen


def base_strokes(strokes):
    """Strokes that belong to the underpainting/base layer (``kind`` starts with base)."""
    return [s for s in strokes if str(s.get("kind", "")).startswith("base")]


def stroke_color_stats(strokes):
    """Per-channel colour mean/std and the number of unique 5-decimal colours."""
    if not strokes:
        return {"n": 0, "mean": [0.0, 0.0, 0.0], "std": [0.0, 0.0, 0.0],
                "unique": 0}
    colors = np.asarray([s["color"] for s in strokes], dtype=np.float64)
    unique = len({tuple(np.round(c, 5)) for c in colors})
    return {
        "n": int(colors.shape[0]),
        "mean": [float(v) for v in colors.mean(axis=0)],
        "std": [float(v) for v in colors.std(axis=0)],
        "unique": unique,
    }


def stroke_footprints(strokes, H, W):
    """Per-pixel hit count of strokes, using the skill's own renderer geometry.

    Each stroke is re-rendered alone with a magic colour ``(1, 0, 1)`` (green
    channel 0) on the renderer's white canvas; covered pixels are those whose
    green channel dropped below 0.5, so the footprint is exactly the raster
    area used by ``hatch_gen._render_strokes`` (caps/widths included).
    """
    hgr = _hatch()
    counts = np.zeros((H, W), dtype=np.uint16)
    for stroke in strokes:
        probe = dict(stroke)
        probe["color"] = [1.0, 0.0, 1.0]
        probe["alpha"] = 1.0
        probe.pop("gradient", None)
        probe.pop("color_gradient", None)
        probe.pop("color_start", None)
        probe.pop("color_end", None)
        probe["layer"] = 0
        rendered = hgr._render_strokes([probe], H, W, ss=1)
        counts += (rendered[:, :, 1] < 0.5).astype(np.uint16)
    return counts


def coverage_metrics(counts, fg):
    """Coverage / overlap metrics from a per-pixel hit-count image."""
    covered = (counts > 0) & fg
    n_fg = int(fg.sum())
    n_cov = int(covered.sum())
    overlap = int(((counts > 1) & fg).sum())
    over2 = int(((counts > 2) & fg).sum())
    return {
        "fg_px": n_fg,
        "covered_px": n_cov,
        "coverage": float(n_cov / max(1, n_fg)),
        "overlap_px": overlap,
        "overlap_frac_of_covered": float(overlap / max(1, n_cov)),
        "overlap_frac_of_fg": float(overlap / max(1, n_fg)),
        "over2_px": over2,
        "max_count": int(counts.max()) if counts.size else 0,
    }


def render_strokes(strokes, H, W):
    """Render strokes with their real colours (base-only image for flatness tests)."""
    hgr = _hatch()
    return hgr._render_strokes(strokes, H, W, ss=1)


def foreground_color_std(rendered, fg):
    """Max per-channel std of the rendered colours inside ``fg``."""
    pixels = np.asarray(rendered, dtype=np.float64)[fg]
    if pixels.size == 0:
        return 0.0
    return float(np.max(pixels.std(axis=0)))


def parse_base_color(text):
    """Convert a ``#RRGGBB`` / ``r,g,b`` / ``auto`` CLI value to 0..1 RGB."""
    value = str(text).strip()
    if value.lower() == "auto":
        return None
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) != 6:
            raise ValueError(f"bad #RRGGBB colour: {text!r}")
        return [int(digits[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 3:
        raise ValueError(f"bad r,g,b colour: {text!r}")
    return [float(p) / 255.0 for p in parts]


def lineart_image():
    """Path of the v1.35 line-art demo image if present, else None."""
    path = os.path.join(WORKSPACE_ROOT, "work", "input2", "img_20261016.png")
    return path if os.path.isfile(path) and os.path.getsize(path) > 0 else None

# ------------------------------------------------------------------ drawing-order / direction metrics (v1.36)
#: Base-coat stroke kinds seen in v1.35 (rows) and v1.36 (wash, to be frozen).
BASE_COAT_KINDS = ("base_toned", "base_wash")


def base_coat_strokes(strokes):
    """Underpainting/coat strokes (``base_toned`` / ``base_wash``)."""
    return [s for s in strokes if str(s.get("kind", "")) in BASE_COAT_KINDS]


def formal_strokes(strokes):
    """Formal (block-in/fix/detail) strokes: ``base_pass2/3/4``."""
    return [s for s in strokes if str(s.get("kind", "")).startswith("base_pass")]


def stroke_center_px(strokes, H, W):
    """Pixel-space midpoint of every stroke (normalised by L=max(H,W))."""
    L = float(max(H, W))
    return np.asarray([np.asarray(s["points"], dtype=np.float64)[1] * L
                       for s in strokes], dtype=np.float64)


def stroke_direction_angles(strokes, H, W):
    """Endpoint direction of every stroke in degrees ``[0, 180)`` (mod 180)."""
    L = float(max(H, W))
    angles, lengths = [], []
    for stroke in strokes:
        points = np.asarray(stroke["points"], dtype=np.float64) * L
        delta = points[-1] - points[0]
        length = float(np.hypot(delta[0], delta[1]))
        if length < 1e-9:
            angles.append(0.0)
            lengths.append(0.0)
            continue
        angles.append(float(np.degrees(np.arctan2(delta[1], delta[0]))) % 180.0)
        lengths.append(length)
    return np.asarray(angles, dtype=np.float64), np.asarray(lengths, dtype=np.float64)


def axis_aligned_frac(angles, tol=10.0):
    """Fraction of stroke directions within ``tol`` degrees of horizontal/vertical."""
    angles = np.asarray(angles, dtype=np.float64)
    if angles.size == 0:
        return 0.0
    mod = np.mod(angles, 90.0)
    distance = np.minimum(mod, 90.0 - mod)
    return float((distance <= float(tol)).mean())


def direction_entropy(angles, bins=12):
    """Normalised entropy (0..1) of the stroke-direction histogram (higher = freer)."""
    angles = np.asarray(angles, dtype=np.float64)
    if angles.size == 0:
        return 0.0
    hist, _ = np.histogram(angles, bins=int(bins), range=(0.0, 180.0))
    p = hist / max(1, int(hist.sum()))
    p = p[p > 0]
    return float(-(p * np.log(p)).sum() / np.log(int(bins)))


def _rank(values):
    order = np.argsort(np.argsort(np.asarray(values, dtype=np.float64)))
    return order.astype(np.float64)


def sequence_y_spearman(strokes, H, W):
    """Spearman correlation between stroke order and stroke-centre y (scan-ness)."""
    if len(strokes) < 3:
        return 0.0
    centers = stroke_center_px(strokes, H, W)
    return float(np.corrcoef(_rank(np.arange(len(strokes))), _rank(centers[:, 1]))[0, 1])


def adjacent_center_distance_median(strokes, H, W):
    """Median pixel distance between consecutive stroke centres (spatial grouping)."""
    centers = stroke_center_px(strokes, H, W)
    if len(centers) < 2:
        return 0.0
    return float(np.median(np.linalg.norm(np.diff(centers, axis=0), axis=1)))


def region_run_frac(strokes, H, W, cells=4):
    """Fraction of consecutive strokes that fall in the same coarse spatial cell."""
    centers = stroke_center_px(strokes, H, W)
    if len(centers) < 2:
        return 0.0
    col = np.clip((centers[:, 0] / max(W / float(cells), 1e-9)).astype(int), 0, cells - 1)
    row = np.clip((centers[:, 1] / max(H / float(cells), 1e-9)).astype(int), 0, cells - 1)
    cell = row * cells + col
    return float((cell[1:] == cell[:-1]).mean())


def flow_orientation_frac(strokes, rgb, tol=20.0):
    """Fraction of strokes whose direction aligns (<=tol deg) with the structure tensor."""
    hg = _hybrid()
    jxx, jyy, jxy = hg.orientation_field(rgb)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + np.pi / 2.0
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    diffs = []
    for stroke in strokes:
        points = np.asarray(stroke["points"], dtype=np.float64) * L
        center = points[1]
        x = int(np.clip(round(center[0]), 0, W - 1))
        y = int(np.clip(round(center[1]), 0, H - 1))
        tangent = float(np.degrees(theta[y, x])) % 180.0
        delta = points[-1] - points[0]
        if float(np.hypot(delta[0], delta[1])) < 1e-9:
            continue
        angle = float(np.degrees(np.arctan2(delta[1], delta[0]))) % 180.0
        diff = abs(angle - tangent)
        diffs.append(min(diff, 180.0 - diff))
    if not diffs:
        return 0.0
    return float((np.asarray(diffs) <= float(tol)).mean())
