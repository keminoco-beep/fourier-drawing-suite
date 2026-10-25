"""End-to-end tests: 6 drafts + 2 canonical plans → .fbs → PNG (WP-D)."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from _fixtures import EXAMPLES, PACKAGE_ROOT, canonical_plan_curves, canonical_plan_shapes

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt.compile import compile_plan, normalized_fbs  # noqa: E402
from fourier_prompt.compat import skill_api  # noqa: E402
from fourier_prompt.legacy import from_legacy  # noqa: E402
from fourier_prompt.schema import validate_plan  # noqa: E402

RESOLUTION = 128


def pixel_hash(rgb) -> str:
    return hashlib.sha256(rgb.tobytes()).hexdigest()


def write_png(rgb, path: Path) -> None:
    from PIL import Image

    Image.fromarray(rgb, mode="RGB").save(path, format="PNG")


def load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class SixDraftEndToEndCase(unittest.TestCase):
    def test_six_drafts_compile_and_render(self):
        files = sorted(EXAMPLES.glob("*.json"))
        self.assertEqual(len(files), 6)
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            for path in files:
                with self.subTest(name=path.name):
                    draft = load_draft(path)
                    plan21, legacy_warnings = from_legacy(draft)
                    self.assertEqual(validate_plan(plan21), [])
                    result = compile_plan(draft, resolution=RESOLUTION)
                    self.assertGreaterEqual(result["stats"]["records"], 1)
                    self.assertGreater(result["stats"]["fbs_bytes"], 0)
                    self.assertGreaterEqual(len(legacy_warnings), 1)
                    data = skill_api.write_fbs(result["fbs"], str(outdir / f"{path.stem}.fbs"))
                    self.assertTrue(data.startswith(b"{"))
                    rgb = skill_api.render(result["fbs"], resolution=RESOLUTION, supersample=1)
                    self.assertEqual(rgb.shape, (RESOLUTION, RESOLUTION, 3))
                    # 不是空白图：至少 1% 像素偏离背景
                    background = result["fbs"]["base_color"][:3]
                    import numpy as np

                    diff = np.abs(rgb.astype(int) - np.asarray(background, dtype=int)).sum(axis=2)
                    self.assertGreater(int((diff > 8).sum()), RESOLUTION * RESOLUTION * 0.01)
                    png = outdir / f"{path.stem}.png"
                    write_png(rgb, png)
                    self.assertGreater(png.stat().st_size, 1000)

    def test_canonical_plans_compile_and_render(self):
        for label, plan in (
            ("shapes", canonical_plan_shapes()),
            ("curves", canonical_plan_curves()),
        ):
            with self.subTest(plan=label):
                result = compile_plan(plan, resolution=RESOLUTION)
                rgb = skill_api.render(result["fbs"], resolution=RESOLUTION, supersample=1)
                self.assertEqual(max(rgb.shape[:2]), RESOLUTION)
                self.assertGreater(int(rgb.sum()), 0)
                self.assertGreaterEqual(result["stats"]["records"], 2)


class DeterminismCase(unittest.TestCase):
    def test_document_hash_and_pixels_are_stable(self):
        draft = load_draft(EXAMPLES / "01_rose_medallion.json")
        first = compile_plan(draft, resolution=RESOLUTION)
        second = compile_plan(draft, resolution=RESOLUTION)
        self.assertEqual(normalized_fbs(first["fbs"]), normalized_fbs(second["fbs"]))
        self.assertEqual(first["stats"]["coef_rows"], second["stats"]["coef_rows"])
        rgb_a = skill_api.render(first["fbs"], resolution=RESOLUTION, supersample=1)
        rgb_b = skill_api.render(second["fbs"], resolution=RESOLUTION, supersample=1)
        self.assertEqual(pixel_hash(rgb_a), pixel_hash(rgb_b))

    def test_written_fbs_normalized_hash_is_stable(self):
        draft = load_draft(EXAMPLES / "04_ocean_waves.json")
        with tempfile.TemporaryDirectory() as tmp:
            hashes = []
            for index in range(2):
                result = compile_plan(draft, resolution=RESOLUTION)
                path = Path(tmp) / f"run{index}.fbs"
                skill_api.write_fbs(result["fbs"], str(path))
                reloaded = skill_api.import_skill().io_format.read_fbs(str(path))
                hashes.append(hashlib.sha256(normalized_fbs(reloaded).encode("utf-8")).hexdigest())
        self.assertEqual(hashes[0], hashes[1])


class TextureCase(unittest.TestCase):
    def test_fill_and_stroke_layers_render_differently(self):
        result = compile_plan(canonical_plan_shapes(), resolution=RESOLUTION)
        rgb = skill_api.render(result["fbs"], resolution=RESOLUTION, supersample=1)
        background = result["fbs"]["base_color"][:3]
        import numpy as np

        diff = np.abs(rgb.astype(int) - np.asarray(background, dtype=int)).sum(axis=2)
        # 至少两种颜色族：红色底 + 深色线（简单用唯一色数下界）
        unique = np.unique(rgb.reshape(-1, 3), axis=0)
        self.assertGreater(len(unique), 5)


if __name__ == "__main__":
    unittest.main()
