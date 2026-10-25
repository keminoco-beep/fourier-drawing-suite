"""Layer table / filtering / per-layer rendering tests (WP-I)."""

from __future__ import annotations

import copy
import json
import sys
import unittest

from _fixtures import (
    PACKAGE_ROOT,
    doc_without_layers,
    prepare_01,
    prepare_06,
    sample_01,
    small_doc,
    tempdir,
)

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt import layers  # noqa: E402
from fourier_prompt.fdraw import unpack  # noqa: E402


class LayerTableCase(unittest.TestCase):
    def test_doc_layers_synthesised_from_strokes(self):
        doc = prepare_01()
        table = layers.doc_layers(doc)
        self.assertEqual([entry["index"] for entry in table], [0, 1, 2, 3, 4, 5])
        self.assertEqual([entry["name"] for entry in table], [f"L{i}" for i in range(6)])
        total = sum(entry["stroke_count"] + entry["fill_count"] for entry in table)
        self.assertEqual(total, len(doc["strokes"]))
        self.assertEqual(total, 51)
        self.assertEqual(table[0]["index"], 0)  # bottom layer keeps index 0
        for entry in table:
            self.assertTrue(entry["visible"])
            self.assertEqual(entry["opacity"], 1.0)
            self.assertIn("bbox", entry)

    def test_doc_layers_prefers_plan_metadata(self):
        doc = small_doc(
            layers_spec=[
                {"id": 0, "name": "base", "visible": True, "opacity": 1.0},
                {"id": 1, "name": "ink", "visible": False, "opacity": 0.5},
            ]
        )
        table = layers.doc_layers(doc)
        self.assertEqual([entry["name"] for entry in table], ["base", "ink"])
        self.assertEqual([entry["source"] for entry in table], ["metadata", "metadata"])
        self.assertTrue(table[0]["visible"])
        self.assertFalse(table[1]["visible"])
        self.assertAlmostEqual(table[1]["opacity"], 0.5)
        self.assertEqual([entry["order"] for entry in table], [0, 1])

    def test_add_layer_metadata_is_additive(self):
        doc = doc_without_layers()
        before = copy.deepcopy(doc)
        original_strokes = json.dumps(doc["strokes"], sort_keys=True)
        returned = layers.add_layer_metadata(doc, None)
        self.assertIs(returned, doc)
        self.assertIn("layers", returned)
        self.assertEqual(json.dumps(returned["strokes"], sort_keys=True), original_strokes)
        added = set(returned) - set(before)
        self.assertEqual(added, {"layers"})
        for key in before:
            self.assertEqual(returned[key], before[key], key)

    def test_add_layer_metadata_from_plan_rejects_bad_opacity(self):
        doc = doc_without_layers()
        plan = {"layers": [{"id": 0, "name": "bad", "opacity": 2.0}]}
        with self.assertRaises(layers.LayerError) as ctx:
            layers.add_layer_metadata(doc, plan)
        self.assertEqual(ctx.exception.code, "FP_E_LAYER_OPACITY")


class FilterCase(unittest.TestCase):
    def test_filter_doc_only_target_layer(self):
        doc = small_doc()
        before = copy.deepcopy(doc)
        only_ink = layers.filter_doc(doc, 1)
        self.assertEqual({stroke["layer"] for stroke in only_ink["strokes"]}, {1})
        self.assertEqual(len(only_ink["strokes"]), 2)
        self.assertEqual(len(doc["strokes"]), 4)  # input untouched
        self.assertEqual(doc, before)
        self.assertIsNot(only_ink["strokes"], doc["strokes"])

    def test_filter_doc_include_lower_layers(self):
        doc = small_doc()
        bottom = layers.filter_doc(doc, 0, include_above=True)
        self.assertEqual(len(bottom["strokes"]), 2)
        top = layers.filter_doc(doc, 1, include_above=True)
        self.assertEqual(len(top["strokes"]), 4)

    def test_filter_doc_unknown_layer_fails(self):
        doc = small_doc()
        with self.assertRaises(layers.LayerError) as ctx:
            layers.filter_doc(doc, 7)
        self.assertEqual(ctx.exception.code, "FP_E_LAYER_INDEX")

    def test_filter_doc_does_not_mutate_layers_metadata(self):
        doc = small_doc()
        snapshot = json.dumps(doc["layers"], sort_keys=True)
        layers.filter_doc(doc, 0, include_above=True)
        self.assertEqual(json.dumps(doc["layers"], sort_keys=True), snapshot)


class RenderCase(unittest.TestCase):
    def test_render_layer_pngs_writes_rgba_files(self):
        from PIL import Image

        doc = small_doc()
        with tempdir() as directory:
            manifest = layers.render_layer_pngs(
                doc, resolution=32, out_dir=str(directory), quality="fast"
            )
            self.assertEqual(len(manifest), 2)
            self.assertTrue((directory / "layers.json").is_file())
            for entry in manifest:
                self.assertTrue(entry["path"].endswith(".png"))
                with Image.open(entry["path"]) as image:
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.size, layers.canvas_size(doc, 32))
                self.assertGreater(entry["sha256"], "")
            payload = json.loads((directory / "layers.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "fdraw-layers/1")
            self.assertEqual([item["file"] for item in payload["layers"]],
                             [entry["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for entry in manifest])

    def test_layer_opacity_is_baked_into_alpha(self):
        opaque = small_doc(
            layers_spec=[{"id": 0, "name": "base"}, {"id": 1, "name": "ink"}]
        )
        faded = copy.deepcopy(opaque)
        faded["layers"][1]["opacity"] = 0.5
        a = layers.render_layers(opaque, resolution=32, quality="fast")[1]["rgba"][..., 3]
        b = layers.render_layers(faded, resolution=32, quality="fast")[1]["rgba"][..., 3]
        self.assertGreater(int(a.max()), 0)
        mask = a > 0
        ratio = b[mask].astype(float) / a[mask].astype(float)
        self.assertAlmostEqual(float(ratio.mean()), 0.5, delta=0.02)

    def test_render_layers_validates_resolution_and_quality(self):
        doc = small_doc()
        with self.assertRaises(layers.LayerError) as ctx:
            layers.render_layers(doc, resolution=1024, quality="fast")
        self.assertEqual(ctx.exception.code, "FP_E_LAYER_RESOLUTION")
        with self.assertRaises(layers.LayerError) as ctx:
            layers.render_layers(doc, resolution=32, quality="ultra")
        self.assertEqual(ctx.exception.code, "FP_E_LAYER_QUALITY")

    def test_canvas_size_matches_renderer(self):
        from fourier_prompt.compat import skill_api

        doc = sample_01()
        for resolution in (32, 64, 128):
            width, height = layers.canvas_size(doc, resolution)
            expected = skill_api.import_skill().render._output_size(doc, resolution)
            self.assertEqual((width, height), (expected[0], expected[1]))

    def test_06_pencil_layer_composite_matches_exact_layer_render(self):
        import numpy as np

        from fourier_prompt import render_fast

        doc = prepare_06()
        images = layers.render_layers(doc, resolution=32, quality="fast")
        self.assertEqual(len(images), 9)  # 06 uses layers 0..7 and 9
        pencil = next(image for image in images if image["index"] == 5)
        self.assertEqual(pencil["quality"], "exact")
        self.assertTrue(pencil["fallback_reason"])
        subset = layers.filter_doc(doc, 5)
        direct = render_fast.render_auto(
            subset,
            resolution=32,
            quality="exact",
            background=layers._base_rgb(doc),
        )
        rebuilt = layers.composite_over([pencil], layers._base_rgb(doc))
        diff = np.abs(rebuilt.astype(int) - direct.astype(int))
        self.assertLessEqual(int(diff.max()), 2, f"max diff {diff.max()}")

    def test_composite_over_matches_full_render(self):
        import numpy as np

        from fourier_prompt import render_fast

        doc = prepare_01()
        images = layers.render_layers(doc, resolution=32, quality="fast")
        rebuilt = layers.composite_over(images, layers._base_rgb(doc))
        direct = render_fast.render_auto(
            doc, resolution=32, quality="fast", background=layers._base_rgb(doc)
        )
        diff = np.abs(rebuilt.astype(int) - direct.astype(int))
        # Independent straight-alpha recomposition compounds 8-bit alpha
        # quantisation across layers; the authoritative PSD/KRA composite is
        # the direct full render (tested separately). Fractional mean is the
        # meaningful gate here.
        self.assertLessEqual(float(diff.mean()), 1.0, f"mean diff {diff.mean()}")
        self.assertLessEqual(int(diff.max()), 20, f"max diff {diff.max()}")


if __name__ == "__main__":
    unittest.main()
