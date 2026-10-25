#!/usr/bin/env python
"""v1.38 GPU-render tests (GPU cases skipped unless RUN_GPU=1 + GL).

v1.38 GPU rendering: ``gpu_render.py`` (PyOpenGL + glfw offscreen GLSL capsule
rasterisation) renders .fdraw / .fbs / layered_base strokes JSON to PNG.

GPU cases are SKIPPED by default (the unit-test python has no PyOpenGL); they run
only with ``RUN_GPU=1`` under a GL-capable python (ComfyUI python).  CPU-only
paths (``--device cpu``, no-GL exit code, interface, node package contract) run
in the default suite.

Frozen interface (Lead/fixer 2026-10-25):
  inputs (mutually exclusive): --fdraw / --fbs / --render-json / --strokes
  --out PNG (required), --resolution INT (default 1024, long side),
  --supersample {1,2,3} (default 2), --device {gl,cpu} (default gl),
  --background, --width-scale, --timing-json PATH
  exit codes: 0 ok, 2 input error, 3 no PyOpenGL/GL context, 4 unsupported doc
  timing JSON: load_s/sample_s/geometry_s/gpu_s/readback_s/total_s
               (+ gl_init_s/segments/out_size/device/float_fbo)
  node: PACKAGE_VERSION 3.x (>=3.0.1, semantic check), GPU first then CPU
  fallback, FDP_GPU_RENDER=0 CPU
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

try:  # unittest discover -s tests
    import _support as S
except ImportError:  # tests.test_gpu_render
    from tests import _support as S

GPU_RENDER = os.path.join(S.SCRIPTS_DIR, "gpu_render.py")
BUILD_FBS = os.path.join(S.SCRIPTS_DIR, "build_strokes_fbs.py")
RUN_GPU = os.environ.get("RUN_GPU") == "1"
WORKSPACE_ROOT = S.WORKSPACE_ROOT

#: Frozen CLI defaults (Lead/fixer confirmed 2026-10-25).
FROZEN_GPU_DEFAULTS = {
    "--resolution": 1024,
    "--supersample": 2,
    "--device": "gl",
    "--background": None,
    "--width-scale": None,
}
FROZEN_GPU_CHOICES = {
    "--supersample": [1, 2, 3],
    "--device": ["gl", "cpu"],
}

#: GPU vs CPU quality gate on the synthetic gradient/alpha document.
#: 32 dB / 0.98 come from the task contract; the measured probe values are
#: recorded in work/tests6/baseline_result_gpu.md (do not relax).
GPU_MIN_PSNR = 32.0
GPU_MIN_SSIM = 0.98

TIMING_STAGES = ("load", "sample", "geometry", "gpu", "readback", "total")

#: Node package compatibility: any 3.x with minor.patch >= 0.1 is accepted
#: (v1.38 shipped 3.0.1, the portable release is 3.1.0, later 3.y.z is fine);
#: older 3.0.0 / 2.x and a future 4.x require re-validation.
NODE_VERSION_MAJOR = 3
NODE_VERSION_MIN = (3, 0, 1)


def _parse_version(text):
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(text).strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _version_compatible(version):
    parsed = _parse_version(version)
    return (parsed is not None and parsed[0] == NODE_VERSION_MAJOR
            and parsed[1:] >= NODE_VERSION_MIN[1:])

NODE_CANDIDATES = []
if os.environ.get("FDP_NODES_PATH"):
    NODE_CANDIDATES.append(os.environ["FDP_NODES_PATH"])
NODE_CANDIDATES.append(os.path.join(
    WORKSPACE_ROOT, "fourier-prompt", "comfyui", "ComfyUI-FDP-T2I", "nodes.py"))

TINY_FDRAW_CANDIDATES = [
    os.path.join(WORKSPACE_ROOT, "work", "auditfix", "fixtures", "B_with_fdraw",
                 "qa_purestroke.fdraw"),
    os.path.join(WORKSPACE_ROOT, "work", "verify", "qa03", "B_with_fdraw",
                 "qa_probe.fdraw"),
]

#: GPU-vs-CPU equivalence document: alpha=1 (the skill pipeline writes alpha=1;
#: translucent documents are CPU-fallback by contract, see ALPHA_DOC below).
SYNTH_STROKES = [
    {"points": [[0.12, 0.18], [0.30, 0.30], [0.55, 0.38]],
     "widths": [0.030, 0.030, 0.030], "color": [0.12, 0.35, 0.70],
     "alpha": 1.0, "layer": 2, "kind": "base_pass3", "closed": False},
    {"points": [[0.20, 0.72], [0.45, 0.62], [0.72, 0.55]],
     "widths": [0.040, 0.040, 0.040], "color": [0.65, 0.20, 0.15],
     "gradient": {"start": [0.70, 0.22, 0.16], "end": [0.18, 0.28, 0.72]},
     "alpha": 1.0, "layer": 2, "kind": "base_pass3", "closed": False},
    {"points": [[0.62, 0.18], [0.78, 0.34], [0.88, 0.52]],
     "widths": [0.026, 0.026, 0.026], "color": [0.15, 0.62, 0.28],
     "alpha": 1.0, "layer": 3, "kind": "base_pass4", "closed": False},
    {"points": [[0.05, 0.90], [0.35, 0.86], [0.60, 0.92]],
     "widths": [0.010, 0.010, 0.010], "color": [0.05, 0.05, 0.05],
     "alpha": 1.0, "layer": 3, "kind": "base_pass4", "closed": False},
    {"points": [[0.30, 0.50], [0.48, 0.46]],
     "widths": [0.022, 0.022], "color": [0.85, 0.62, 0.10],
     "alpha": 1.0, "layer": 3, "kind": "base_pass4", "closed": False},
]

#: Translucent document: gpu_render must refuse (exit 5) and the caller falls
#: back to the CPU exact renderer (Lead decision for v1.38).
ALPHA_DOC = [
    {"points": [[0.20, 0.25], [0.80, 0.70]],
     "widths": [0.050, 0.050], "color": [0.15, 0.62, 0.28],
     "alpha": 0.55, "layer": 3, "kind": "base_pass4", "closed": False},
]
GPU_NO_ALPHA_EXIT = 5


# --------------------------------------------------------------- detection
def _gl_available() -> bool:
    try:
        import OpenGL  # noqa: F401
        import glfw  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


HAS_GL = _gl_available()
GPU_SKIP = not (RUN_GPU and HAS_GL and os.path.isfile(GPU_RENDER))
GPU_SKIP_REASON = (
    "GPU render tests need RUN_GPU=1 and a GL-capable python "
    f"(OpenGL importable: {HAS_GL}, gpu_render.py present: "
    f"{os.path.isfile(GPU_RENDER)})")


def _tiny_fdraw():
    for path in TINY_FDRAW_CANDIDATES:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def _nodes_path(require_compatible=False):
    """Best available nodes.py (highest compatible PACKAGE_VERSION).

    ``require_compatible=True`` keeps only versions passing the semantic
    compatibility check (major==3, >=3.0.1); otherwise the highest version
    found is returned so the contract test can report an incompatible one.
    """
    best = None
    for path in NODE_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            content = open(path, encoding="utf-8").read()
        except OSError:
            continue
        match = re.search(r'PACKAGE_VERSION\s*=\s*["\']([^"\']+)["\']',
                          content)
        version = match.group(1) if match else "0"
        if require_compatible and not _version_compatible(version):
            continue
        parsed = _parse_version(version) or (0, 0, 0)
        if best is None or parsed > best[2]:
            best = (path, version, parsed)
    if best is None:
        return None
    return best[0], best[1]


# --------------------------------------------------------------- helpers
def _write_strokes(path, strokes=None):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(strokes or SYNTH_STROKES, fh)
    return path


def _build_synthetic_fbs(root, strokes=None, name="synth"):
    """strokes JSON -> .fbs doc via build_strokes_fbs.py (CPU renderer doc)."""
    strokes_path = os.path.join(root, "strokes.json")
    with open(strokes_path, "w", encoding="utf-8") as fh:
        json.dump(strokes or SYNTH_STROKES, fh)
    white = os.path.join(root, "white.png")
    Image.new("RGB", (256, 256), (255, 255, 255)).save(white)
    fbs = os.path.join(root, f"{name}.fbs")
    proc = S.run_cli([BUILD_FBS, "--strokes", strokes_path, "--input", white,
                      "--out", fbs, "--resolution", "256"], timeout=300)
    if proc.returncode != 0:
        raise unittest.SkipTest(
            f"build_strokes_fbs.py unavailable ({proc.returncode}): "
            f"{proc.stderr[-1000:]}")
    return {"strokes": strokes_path, "fbs": fbs, "white": white}


def _run_render(args, timeout=600):
    return S.run_cli([GPU_RENDER] + list(args), timeout=timeout)


def _image(path):
    return np.asarray(Image.open(path).convert("RGB"),
                      dtype=np.float64) / 255.0


def _psnr(a, b):
    mse = float(np.mean((np.asarray(a) - np.asarray(b)) ** 2))
    if mse <= 1e-12:
        return 99.0
    return 10.0 * math.log10(1.0 / mse)


def _ssim(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        raise AssertionError(f"shape mismatch {a.shape} vs {b.shape}")
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    mu_a = a.mean(axis=(0, 1))
    mu_b = b.mean(axis=(0, 1))
    var_a = a.var(axis=(0, 1))
    var_b = b.var(axis=(0, 1))
    cov = ((a - mu_a) * (b - mu_b)).mean(axis=(0, 1))
    return float((((2 * mu_a * mu_b + c1) * (2 * cov + c2)) /
                  ((mu_a ** 2 + mu_b ** 2 + c1) *
                   (var_a + var_b + c2))).mean())


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pack_fdraw(fbs_path, out_path):
    """Pack an .fbs JSON into an .fdraw container (needs fourier_prompt)."""
    prompt_dir = os.path.join(WORKSPACE_ROOT, "fourier-prompt")
    env = dict(os.environ)
    env["PYTHONPATH"] = prompt_dir + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "fourier_prompt.fdraw_cli", "pack",
         "--fbs", fbs_path, "--out", out_path],
        capture_output=True, text=True, timeout=300, cwd=prompt_dir, env=env)
    return proc


def _timing_keys(data):
    """Stage presence check: frozen names are load_s/sample_s/.../total_s."""
    missing = []
    for stage in TIMING_STAGES:
        if not any(key == stage or key.startswith(stage + "_") or
                   key.startswith(stage) for key in data):
            missing.append(stage)
    return missing


class TestGPUInterface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(GPU_RENDER):
            raise unittest.SkipTest("gpu_render.py not implemented")
        cls.help_text = S.script_help(GPU_RENDER)
        cls.defaults = S.argparse_defaults(GPU_RENDER)
        cls.choices = S.argparse_choices(GPU_RENDER)

    def test_help_lists_inputs_and_options(self):
        for option in ("--fdraw", "--fbs", "--render-json", "--strokes",
                       "--out", "--resolution", "--supersample", "--device",
                       "--background", "--width-scale", "--timing-json"):
            self.assertIn(option, self.help_text,
                          f"{option} missing from gpu_render.py --help")

    def test_frozen_gpu_defaults(self):
        problems = []
        for option, expected in FROZEN_GPU_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: {self.defaults[option]!r} != "
                                f"{expected!r}")
        for option, expected in FROZEN_GPU_CHOICES.items():
            if self.choices.get(option) != expected:
                problems.append(f"{option}: choices "
                                f"{self.choices.get(option)!r} != {expected}")
        self.assertFalse(problems, "\n".join(problems))

    def test_inputs_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory(prefix="gpu_args_") as td:
            strokes = _write_strokes(os.path.join(td, "strokes.json"))
            proc = _run_render(["--strokes", strokes, "--fbs", strokes,
                                "--out", os.path.join(td, "out.png"),
                                "--resolution", "128"], timeout=120)
        self.assertEqual(proc.returncode, 2,
                         f"two inputs must be rejected with exit 2, got "
                         f"{proc.returncode}\n{proc.stderr[-1500:]}")

    def test_no_gl_exit_code_and_clear_message(self):
        """Without PyOpenGL/GL: render exits 3, --help still works."""
        if HAS_GL:
            self.skipTest("GL is available in this interpreter")
        help_proc = S.run_cli([GPU_RENDER, "--help"], timeout=120)
        self.assertEqual(help_proc.returncode, 0,
                         "--help must not require a GL context")
        with tempfile.TemporaryDirectory(prefix="gpu_nogl_") as td:
            strokes = _write_strokes(os.path.join(td, "strokes.json"))
            out = os.path.join(td, "out.png")
            proc = _run_render(["--strokes", strokes, "--out", out,
                                "--resolution", "128", "--device", "gl"],
                               timeout=180)
            self.assertEqual(proc.returncode, 3,
                             f"gpu_render.py without GL must exit 3, got "
                             f"{proc.returncode}\nstdout:\n"
                             f"{proc.stdout[-1500:]}\nstderr:\n"
                             f"{proc.stderr[-3000:]}")
            self.assertFalse(os.path.isfile(out),
                             "no PNG should be produced when GL is unavailable")
        message = (proc.stdout + "\n" + proc.stderr).lower()
        self.assertTrue(any(word in message for word in
                            ("opengl", "pyopengl", "glfw", "gl ")),
                        f"no-GL message should name the GL dependency:\n"
                        f"{proc.stderr[-2000:]}")


class TestCPUDevice(unittest.TestCase):
    """--device cpu is pure CPU and runs in the default suite (no GPU needed)."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(GPU_RENDER):
            raise unittest.SkipTest("gpu_render.py not implemented")
        cls._tmp = tempfile.TemporaryDirectory(prefix="gpu_cpu_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.strokes = _write_strokes(os.path.join(cls._tmp.name,
                                                  "strokes.json"))
        cls.png = os.path.join(cls._tmp.name, "cpu.png")
        cls.timing = os.path.join(cls._tmp.name, "cpu_timing.json")
        cls.proc = _run_render(["--strokes", cls.strokes, "--out", cls.png,
                                "--resolution", "256", "--device", "cpu",
                                "--supersample", "2",
                                "--timing-json", cls.timing], timeout=600)

    def test_cpu_render_ok(self):
        self.assertEqual(self.proc.returncode, 0,
                         f"CPU render failed\n{self.proc.stderr[-2500:]}")
        self.assertTrue(os.path.isfile(self.png))
        with Image.open(self.png) as im:
            self.assertEqual(max(im.size), 256)

    def test_cpu_timing_json(self):
        self.assertTrue(os.path.isfile(self.timing))
        data = json.load(open(self.timing, encoding="utf-8"))
        # The CPU device path is one delegated exact render call, so only the
        # CPU stages are required; the full GPU stage set is asserted by
        # TestGPURenderQuality.test_gpu_timing_json.
        for key in ("load_s", "render_s", "total_s"):
            self.assertIn(key, data, f"CPU timing JSON missing {key}")
        self.assertEqual(data.get("device"), "cpu")
        self.assertGreater(float(data["total_s"]), 0.0)


@unittest.skipIf(GPU_SKIP, GPU_SKIP_REASON)
class TestGPURenderQuality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="gpu_quality_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.strokes = _write_strokes(os.path.join(cls._tmp.name,
                                                  "strokes.json"))
        cls.png_gpu = os.path.join(cls._tmp.name, "gpu.png")
        cls.png_cpu = os.path.join(cls._tmp.name, "cpu.png")
        cls.timing = os.path.join(cls._tmp.name, "gpu_timing.json")
        cls.gpu = _run_render(["--strokes", cls.strokes, "--out",
                               cls.png_gpu, "--resolution", "256",
                               "--device", "gl", "--supersample", "2",
                               "--timing-json", cls.timing], timeout=900)
        cls.cpu = _run_render(["--strokes", cls.strokes, "--out",
                               cls.png_cpu, "--resolution", "256",
                               "--device", "cpu", "--supersample", "2"],
                              timeout=900)

    def test_gpu_matches_cpu(self):
        self.assertEqual(self.gpu.returncode, 0,
                         f"GPU render failed\n{self.gpu.stderr[-2500:]}")
        self.assertEqual(self.cpu.returncode, 0,
                         f"CPU render failed\n{self.cpu.stderr[-2500:]}")
        a = _image(self.png_gpu)
        b = _image(self.png_cpu)
        self.assertEqual(a.shape, b.shape)
        psnr = _psnr(a, b)
        ssim = _ssim(a, b)
        print(f"[gpu] synthetic 256px GPU vs CPU PSNR={psnr:.2f} "
              f"SSIM={ssim:.4f}")
        self.assertGreaterEqual(psnr, GPU_MIN_PSNR,
                                "GPU render deviates from the CPU render")
        self.assertGreaterEqual(ssim, GPU_MIN_SSIM,
                                "GPU render structure differs from the CPU render")
        for y, x in ((2, 2), (2, a.shape[1] - 3), (a.shape[0] - 3, 2)):
            self.assertGreaterEqual(float(a[y, x].min()), 0.98,
                                    "background must stay white at the corners")

    def test_gpu_timing_json(self):
        self.assertTrue(os.path.isfile(self.timing))
        data = json.load(open(self.timing, encoding="utf-8"))
        missing = _timing_keys(data)
        self.assertFalse(missing, f"GPU timing JSON missing stages: {missing}")
        self.assertEqual(data.get("device"), "gl")
        self.assertGreater(float(data["total_s"]), 0.0)
        self.assertGreaterEqual(int(data.get("segments", 0)), 1)
        self.assertEqual(int(data.get("supersample", 0)), 2)

    def test_supersample_and_resolution(self):
        for ss, res in ((1, 192), (2, 320), (3, 256)):
            out = os.path.join(self._tmp.name, f"ss{ss}_{res}.png")
            proc = _run_render(["--strokes", self.strokes, "--out", out,
                                "--resolution", str(res), "--device", "gl",
                                "--supersample", str(ss)], timeout=900)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            with Image.open(out) as im:
                self.assertEqual(max(im.size), res)

    def test_strokes_input_path(self):
        out = os.path.join(self._tmp.name, "strokes.png")
        proc = _run_render(["--strokes", self.strokes, "--out", out,
                            "--resolution", "256", "--device", "gl"],
                           timeout=900)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertTrue(os.path.isfile(out))
        with Image.open(out) as im:
            self.assertEqual(max(im.size), 256)


@unittest.skipIf(GPU_SKIP, GPU_SKIP_REASON)
class TestFdrawInputPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fdraw = _tiny_fdraw()
        if cls.fdraw is None:
            raise unittest.SkipTest("no small .fdraw fixture available")
        cls._tmp = tempfile.TemporaryDirectory(prefix="gpu_fdraw_")
        cls.addClassCleanup(cls._tmp.cleanup)

    def test_fdraw_renders(self):
        out = os.path.join(self._tmp.name, "fdraw.png")
        proc = _run_render(["--fdraw", self.fdraw, "--out", out,
                            "--resolution", "256", "--device", "gl"],
                           timeout=900)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2500:])
        with Image.open(out) as im:
            self.assertEqual(max(im.size), 256)


@unittest.skipIf(GPU_SKIP, GPU_SKIP_REASON)
class TestGPURefusesTranslucentDocs(unittest.TestCase):
    """alpha<1 documents must be refused with exit 5 (CPU fallback by caller).

    Lead decision for v1.38: the GPU rasteriser blends per segment, which does
    not reproduce the CPU's whole-stroke alpha compositing, so translucent
    documents are CPU-exact only.  The skill pipeline emits alpha=1 strokes, so
    real layered_base output is unaffected.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="gpu_alpha_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.strokes = _write_strokes(os.path.join(cls._tmp.name,
                                                  "alpha.json"), ALPHA_DOC)
        cls.gpu_png = os.path.join(cls._tmp.name, "alpha_gpu.png")
        cls.cpu_png = os.path.join(cls._tmp.name, "alpha_cpu.png")
        cls.gpu = _run_render(["--strokes", cls.strokes, "--out",
                               cls.gpu_png, "--resolution", "256",
                               "--device", "gl"], timeout=600)
        cls.cpu = _run_render(["--strokes", cls.strokes, "--out",
                               cls.cpu_png, "--resolution", "256",
                               "--device", "cpu"], timeout=600)

    def test_alpha_doc_exits_5_with_clear_message(self):
        self.assertEqual(self.gpu.returncode, GPU_NO_ALPHA_EXIT,
                         f"translucent document must exit {GPU_NO_ALPHA_EXIT} "
                         f"(CPU fallback), got {self.gpu.returncode}\n"
                         f"stdout:\n{self.gpu.stdout[-1500:]}\n"
                         f"stderr:\n{self.gpu.stderr[-2500:]}")
        message = (self.gpu.stdout + "\n" + self.gpu.stderr).lower()
        self.assertTrue(re.search(r"alpha|translucent|透明|回退|fallback", message),
                        f"message should explain the alpha fallback:\n"
                        f"{self.gpu.stderr[-2000:]}")
        self.assertFalse(os.path.isfile(self.gpu_png),
                         "no GPU PNG should be written for a refused document")

    def test_cpu_still_renders_alpha_doc(self):
        self.assertEqual(self.cpu.returncode, 0, self.cpu.stderr[-2500:])
        self.assertTrue(os.path.isfile(self.cpu_png))
        with Image.open(self.cpu_png) as im:
            self.assertEqual(max(im.size), 256)


class TestNodePackageContract(unittest.TestCase):
    """PACKAGE_VERSION 3.x (>=3.0.1) + RenderFdraw still has ONE widget."""

    @classmethod
    def setUpClass(cls):
        found = _nodes_path()
        if found is None:
            raise unittest.SkipTest("ComfyUI-FDP-T2I nodes.py not found")
        cls.nodes = found[0]
        text = open(cls.nodes, encoding="utf-8").read()
        match = re.search(r'PACKAGE_VERSION\s*=\s*["\']([^"\']+)["\']', text)
        cls.version = match.group(1) if match else "0"

    def test_package_version_compatible(self):
        parsed = _parse_version(self.version)
        self.assertIsNotNone(parsed,
                             f"unparsable PACKAGE_VERSION {self.version!r} "
                             f"({self.nodes})")
        self.assertTrue(
            _version_compatible(self.version),
            f"PACKAGE_VERSION must be 3.x with minor.patch >= 0.1 "
            f"(v1.38 shipped 3.0.1, release is 3.1.x); got {self.version!r} "
            f"({self.nodes})")

    def test_render_widget_count_still_one(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fdp_nodes_contract",
                                                      self.nodes)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        req = mod.FDPT2IRenderFdraw.INPUT_TYPES()["required"]
        widgets = [name for name, spec_ in req.items()
                   if isinstance(spec_[0], list)
                   or spec_[0] in ("INT", "FLOAT", "STRING", "BOOLEAN")]
        self.assertEqual(widgets, ["resolution"],
                         f"RenderFdraw widget inputs changed: {widgets}")

    def test_workflows_render_widgets_unchanged(self):
        wfdir = os.path.join(os.path.dirname(self.nodes), "workflows")
        if not os.path.isdir(wfdir):
            self.skipTest("node package ships no workflows/ directory")
        checked = 0
        for name in sorted(os.listdir(wfdir)):
            if not name.endswith(".json"):
                continue
            data = json.load(open(os.path.join(wfdir, name), encoding="utf-8"))
            for node in data.get("nodes") or []:
                if node.get("type") == "FDPT2IRenderFdraw":
                    checked += 1
                    self.assertEqual(len(node.get("widgets_values") or []), 1,
                                     f"{name}: RenderFdraw widgets changed: "
                                     f"{node.get('widgets_values')}")
        self.assertGreater(checked, 0,
                           "no UI workflow with FDPT2IRenderFdraw found")


@unittest.skipIf(GPU_SKIP, GPU_SKIP_REASON)
class TestNodeGpuFallback(unittest.TestCase):
    """RenderFdraw uses GPU first and falls back to CPU via FDP_GPU_RENDER=0."""

    @classmethod
    def setUpClass(cls):
        cls.fdraw = _tiny_fdraw()
        if cls.fdraw is None:
            raise unittest.SkipTest("no small .fdraw fixture available")
        nodes = _nodes_path(require_compatible=True)
        if nodes is None:
            raise unittest.SkipTest("no compatible ComfyUI-FDP-T2I nodes.py "
                                    "(needs 3.x with minor.patch >= 0.1)")
        import importlib.util
        spec = importlib.util.spec_from_file_location("fdp_nodes_gpu", nodes[0])
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        if not _version_compatible(cls.mod.PACKAGE_VERSION):
            raise unittest.SkipTest(
                f"node package is {cls.mod.PACKAGE_VERSION}, needs 3.x "
                f"with minor.patch >= 0.1 (>=3.0.1)")

    def _run_captured(self, **kwargs):
        fdraw = kwargs.pop("fdraw_path", self.fdraw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self.mod.FDPT2IRenderFdraw()._run(fdraw, **kwargs)
        return result, buf.getvalue()

    def test_gpu_path_succeeds(self):
        with tempfile.TemporaryDirectory(prefix="gpu_node_") as td:
            (img,), out = self._run_captured(run_dir=td, resolution=256)
        self.assertEqual(tuple(img.shape), (1, 256, 256, 3))
        self.assertTrue(np.isfinite(np.asarray(img)).all())
        self.assertIn("gpu", out.lower(),
                      f"GPU path should log gpu usage, got:\n{out[-1500:]}")

    def test_translucent_fdraw_falls_back_to_cpu(self):
        """A .fdraw with alpha<1 makes GPU render exit 5 -> node falls back."""
        with tempfile.TemporaryDirectory(prefix="gpu_node_alpha_") as td:
            doc = _build_synthetic_fbs(td, strokes=ALPHA_DOC, name="alpha")
            fdraw = os.path.join(td, "alpha.fdraw")
            pack = _pack_fdraw(doc["fbs"], fdraw)
            if pack.returncode != 0 or not os.path.isfile(fdraw):
                self.skipTest(f"fourier_prompt pack unavailable: "
                              f"{pack.stderr[-800:]}")
            result, out = self._run_captured(run_dir=td, resolution=256,
                                             fdraw_path=fdraw)
        (img,) = result
        self.assertEqual(tuple(img.shape), (1, 256, 256, 3))
        self.assertNotIn("gpu render ok", out.lower(),
                         f"translucent doc must not stay on GPU:\n{out[-1500:]}")
        self.assertTrue(re.search(r"回退|fallback|cpu", out.lower()),
                        f"fallback should be logged:\n{out[-1500:]}")

    def test_forced_cpu_fallback(self):
        old = os.environ.get("FDP_GPU_RENDER")
        os.environ["FDP_GPU_RENDER"] = "0"
        try:
            with tempfile.TemporaryDirectory(prefix="gpu_node_cpu_") as td:
                result, out = self._run_captured(run_dir=td, resolution=256)
        finally:
            if old is None:
                os.environ.pop("FDP_GPU_RENDER", None)
            else:
                os.environ["FDP_GPU_RENDER"] = old
        (img,) = result
        self.assertEqual(tuple(img.shape), (1, 256, 256, 3))
        self.assertNotIn("gpu render ok", out.lower(),
                         f"FDP_GPU_RENDER=0 must not use the GPU:\n{out[-1500:]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
