"""End-to-end text → .fbs → PNG tests: CLI, determinism, font-free render (WP-K)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _fixtures import (
    FIXTURES,
    PACKAGE_ROOT,
    pixel_hash,
    render_fbs,
    run_cli,
    text_plan,
    write_json,
)

from fourier_prompt.compat import skill_api
from fourier_prompt.compile import compile_plan, normalized_fbs

_RESOLUTION = 256


def _all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key).lower()
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


class RenderTextCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = compile_plan(text_plan(), resolution=_RESOLUTION)
        cls.fbs = cls.result["fbs"]

    def test_renders_visible_text_pixels(self):
        rgb = render_fbs(self.fbs, _RESOLUTION)
        # canvas 384x256 -> long side 384 -> output 256x171
        self.assertEqual(rgb.shape, (171, 256, 3))
        ink = int(((rgb < 200).any(axis=2)).sum())
        self.assertGreater(ink, 500, f"text looks invisible ({ink} dark pixels)")
        self.assertLess(ink, rgb.shape[0] * rgb.shape[1] * 0.5, "text covers too much of the canvas")

    def test_normalized_hash_and_pixels_are_deterministic(self):
        again = compile_plan(text_plan(), resolution=_RESOLUTION)
        self.assertEqual(normalized_fbs(self.fbs), normalized_fbs(again["fbs"]))
        self.assertEqual(
            pixel_hash(render_fbs(self.fbs, _RESOLUTION)),
            pixel_hash(render_fbs(again["fbs"], _RESOLUTION)),
        )

    def test_document_has_no_font_dependency(self):
        serialized = json.dumps(self.fbs, ensure_ascii=False).lower()
        for needle in ("fonttools", "font-file", "fontfile", "font_family", "font-family"):
            self.assertNotIn(needle, serialized)
        keys = set(_all_keys(self.fbs))
        self.assertNotIn("font", keys)
        self.assertTrue(all(stroke.get("fill_role") == "region_fill" for stroke in self.fbs["strokes"]))

    def test_holes_survive_fbs_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "text.fbs")
            skill_api.write_fbs(self.fbs, path)
            reloaded = skill_api.import_skill().io_format.read_fbs(path)
        holes = [hole for stroke in reloaded["strokes"] for hole in (stroke.get("fill_holes") or [])]
        self.assertGreater(len(holes), 0)
        self.assertEqual(reloaded["canvas"]["width"], self.fbs["canvas"]["width"])

    def test_render_works_with_fonttools_blocked(self):
        """A fresh process renders the .fbs while any fontTools import is fatal."""
        script = """
import json, sys
class BlockFontTools:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fontTools" or fullname.startswith("fontTools."):
            raise ImportError("fontTools is unavailable in the render profile")
        return None
sys.meta_path.insert(0, BlockFontTools())
fbs_path, scripts = sys.argv[1], sys.argv[2]
sys.path.insert(0, scripts)
from fbs_core import io_format, render
doc = io_format.read_fbs(fbs_path)
rgb = render.render(doc, resolution=128, supersample=1)
print(json.dumps({"shape": list(rgb.shape), "ink": int((rgb < 200).any(axis=2).sum()),
                  "fontTools_loaded": "fontTools" in sys.modules}))
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "text.fbs")
            skill_api.write_fbs(self.fbs, path)
            proc = subprocess.run(
                [sys.executable, "-c", script, path, skill_api.scripts_dir()],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(PACKAGE_ROOT),
            )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["shape"], [85, 128, 3])
        self.assertGreater(payload["ink"], 100)
        self.assertFalse(payload["fontTools_loaded"])

    def test_compiling_text_does_not_import_fonttools_in_process(self):
        script = (
            "import sys;"
            "sys.path.insert(0, sys.argv[1]);"
            "import fourier_prompt.compile;"
            "print('fontTools' in sys.modules)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script, str(PACKAGE_ROOT)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")


class CliTextCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan2fbs_and_draw_produce_text_output(self):
        plan_path = write_json(self.tmp, "text_plan.json", text_plan())
        fbs_path = self.tmp / "text.fbs"
        code, stdout, stderr = run_cli(
            "plan2fbs", "--plan", plan_path, "--out", str(fbs_path), "--resolution", str(_RESOLUTION)
        )
        self.assertEqual(code, 0, stderr or stdout)
        self.assertTrue(fbs_path.is_file())
        self.assertTrue(fbs_path.read_bytes().startswith(b"{"))

        png_path = self.tmp / "text.png"
        code, stdout, stderr = run_cli(
            "draw", "--plan", plan_path, "--out", str(png_path), "--resolution", str(_RESOLUTION)
        )
        self.assertEqual(code, 0, stderr or stdout)
        from PIL import Image

        with Image.open(png_path) as image:
            self.assertEqual(image.size[0], _RESOLUTION)
            import numpy as np

            pixels = np.asarray(image.convert("RGB"))
        dark = int((pixels.min(axis=2) < 200).sum())
        self.assertGreater(dark, 500)

    def test_fixture_plan_file_is_part_of_the_suite(self):
        self.assertTrue((FIXTURES / "text_plan.json").is_file())


if __name__ == "__main__":
    unittest.main()
