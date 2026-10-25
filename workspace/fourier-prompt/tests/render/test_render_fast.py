"""Tests for the WP-F fast raster backend (``fourier_prompt.render_fast``)."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import numpy as np
from _fixtures import (
    FALLBACK_SAMPLES,
    SUPPORTED_SAMPLES,
    circle_stroke,
    sample_path,
    synthetic_doc,
)

from fourier_prompt import render_fast as rf

PKG_ROOT = Path(__file__).resolve().parents[2]


def _arr_equal(a, b) -> bool:
    return a.shape == b.shape and bool(np.array_equal(a, b))


class SupportedCase(unittest.TestCase):
    def test_round_document_is_supported(self):
        ok, reasons = rf.fast_supported(synthetic_doc(shape="round"))
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_one_stroke_mode_needs_exact(self):
        ok, reasons = rf.fast_supported(synthetic_doc(mode="one_stroke"))
        self.assertFalse(ok)
        self.assertTrue(any("one_stroke" in r for r in reasons))

    def test_unsupported_shapes_are_named(self):
        for shape in ("chalk", "spatter", "pencil", "airbrush"):
            with self.subTest(shape=shape):
                ok, reasons = rf.fast_supported(synthetic_doc(shape=shape))
                self.assertFalse(ok)
                self.assertTrue(any(shape in r for r in reasons), reasons)

    def test_mesh_method_needs_exact(self):
        ok, reasons = rf.fast_supported(synthetic_doc(method="adaptive_gradient_mesh"))
        self.assertFalse(ok)
        self.assertTrue(any("mesh" in r for r in reasons))

    def test_color_field_fill_needs_exact(self):
        doc = synthetic_doc()
        doc["strokes"].append(
            {
                "id": 9,
                "fill_role": "region_fill",
                "poly": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4]],
                "brush": {"style": 0},
                "color_field": {"bbox": [0.0, 0.0, 1.0, 1.0], "gw": 2, "gh": 2, "cells": []},
            }
        )
        ok, reasons = rf.fast_supported(doc)
        self.assertFalse(ok)
        self.assertTrue(any("color_field" in r for r in reasons))

    def test_text_layer_needs_exact(self):
        doc = synthetic_doc()
        doc["texts"] = [{"text": "hi"}]
        ok, reasons = rf.fast_supported(doc)
        self.assertFalse(ok)
        self.assertTrue(any("文本" in r for r in reasons))


class RenderCase(unittest.TestCase):
    def test_render_shape_dtype_and_background(self):
        doc = synthetic_doc()
        img = rf.render(doc, resolution=128)
        self.assertEqual(img.shape, (128, 128, 3))
        self.assertEqual(img.dtype, np.uint8)
        # the circle sits in the middle: corners stay on the base colour
        np.testing.assert_array_equal(img[0, 0], np.asarray([255, 255, 255], np.uint8))
        self.assertTrue(np.any(img[64, 40:88] != 255))
        img2 = rf.render(doc, resolution=128, background=[10, 20, 30])
        np.testing.assert_array_equal(img2[0, 0], np.asarray([10, 20, 30], np.uint8))

    def test_render_is_deterministic(self):
        doc = synthetic_doc()
        a = rf.render(doc, resolution=128)
        b = rf.render(doc, resolution=128)
        self.assertTrue(_arr_equal(a, b))
        self.assertEqual(
            hashlib.sha256(a.tobytes()).hexdigest(), hashlib.sha256(b.tobytes()).hexdigest()
        )

    def test_explicit_width_scale_beats_contract(self):
        doc = synthetic_doc(contract_width_scale=0.5)
        contracted = rf.render(doc, resolution=160)
        explicit = rf.render(doc, resolution=160, width_scale=1.0)
        self.assertFalse(_arr_equal(contracted, explicit))
        plain = synthetic_doc()
        self.assertTrue(_arr_equal(rf.render(plain, resolution=160, width_scale=0.5), contracted))

    def test_contract_width_scale_defaults_to_one(self):
        doc = synthetic_doc()
        self.assertEqual(rf._resolve_width_scale(doc, None), 1.0)
        doc["render_contract"]["width_scale"] = 0.25
        self.assertEqual(rf._resolve_width_scale(doc, None), 0.25)
        self.assertEqual(rf._resolve_width_scale(doc, 0.75), 0.75)

    def test_unsupported_document_falls_back_to_exact(self):
        doc = synthetic_doc(shape="pencil")
        img, info = rf.render_with_info(doc, resolution=128)
        self.assertEqual(info["quality"], "exact")
        self.assertTrue(info["fallback_reason"])
        self.assertTrue(any("pencil" in r for r in info["fallback_reason"]))
        exact = rf.render_auto(doc, resolution=128, quality="exact")
        self.assertTrue(_arr_equal(img, exact))

    def test_fast_and_exact_shapes_match(self):
        doc = synthetic_doc(width=0.04)
        fast = rf.render_auto(doc, resolution=256, quality="fast")
        exact = rf.render_auto(doc, resolution=256, quality="exact")
        self.assertEqual(fast.shape, exact.shape)
        # geometry sanity: ink bounding boxes agree within 1 px
        def bbox(img):
            mask = np.any(img != 255, axis=2)
            ys, xs = np.where(mask)
            return xs.min(), xs.max(), ys.min(), ys.max()

        fb, eb = bbox(fast), bbox(exact)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(fb, eb)), 1)

    def test_parity_metrics_single_stroke(self):
        doc = synthetic_doc(width=0.04)
        fast = rf.render_auto(doc, resolution=256, quality="fast")
        exact = rf.render_auto(doc, resolution=256, quality="exact")
        metrics = rf.compare_images(exact, fast)
        self.assertEqual(set(metrics), {
            "max_abs_diff", "mean_abs_diff", "psnr_db", "within1_pct", "within2_pct", "parity_pass"
        })
        # Known limitation: binary supersampling cannot hit max|Delta|<=2 on
        # anti-aliased silhouettes; PSNR/coverage sanity is still enforced here.
        self.assertGreater(metrics["psnr_db"], 40.0)
        self.assertGreater(metrics["within2_pct"], 99.0)
        self.assertFalse(metrics["parity_pass"])  # documents the honest gate failure


class CompareCase(unittest.TestCase):
    def test_identical_images_pass_parity(self):
        img = rf.render(synthetic_doc(), resolution=96)
        metrics = rf.compare_images(img, img.copy())
        self.assertEqual(metrics["max_abs_diff"], 0)
        self.assertEqual(metrics["mean_abs_diff"], 0.0)
        self.assertGreater(metrics["psnr_db"], 90)
        self.assertTrue(metrics["parity_pass"])
        self.assertEqual(metrics["within1_pct"], 100.0)

    def test_perturbed_image_fails_parity(self):
        img = rf.render(synthetic_doc(), resolution=96)
        other = img.copy()
        other[10, 10] = [0, 0, 0]
        other[20:30, 20:30] = 255 - other[20:30, 20:30]
        metrics = rf.compare_images(img, other)
        self.assertGreaterEqual(metrics["max_abs_diff"], 200)
        self.assertFalse(metrics["parity_pass"])
        self.assertLess(metrics["within1_pct"], 100.0)

    def test_mismatched_sizes_raise(self):
        with self.assertRaises(ValueError):
            rf.compare_images(np.zeros((8, 8, 3), np.uint8), np.zeros((8, 9, 3), np.uint8))


class TruncateCase(unittest.TestCase):
    def setUp(self):
        self.two = synthetic_doc(
            strokes=[
                circle_stroke(0, center=(0.25, 0.5), radius=0.1, width=0.03),
                circle_stroke(1, center=(0.75, 0.5), radius=0.1, width=0.03),
            ]
        )

    def test_preview_truncates_prefix_only(self):
        full = rf.render_auto(self.two, resolution=256, quality="fast")
        cut = rf.truncate_doc(self.two, fraction=0.5)
        part = rf.render_auto(cut, resolution=256, quality="fast")
        self.assertEqual(len(cut["strokes"]), 1)
        # left half unchanged, right half back to background only
        self.assertEqual(int(np.abs(full[:, :128].astype(int) - part[:, :128].astype(int)).max()), 0)
        self.assertGreater(int(np.abs(full[:, 128:].astype(int) - part[:, 128:].astype(int)).max()), 100)
        self.assertNotEqual(hashlib.sha256(full.tobytes()).hexdigest(),
                            hashlib.sha256(part.tobytes()).hexdigest())

    def test_frame_zero_is_background(self):
        cut = rf.truncate_doc(self.two, at=0)
        part = rf.render_auto(cut, resolution=128, quality="fast")
        self.assertEqual(len(cut["strokes"]), 0)
        self.assertTrue(np.all(part == 255))

    def test_truncate_does_not_mutate_source(self):
        before = json.dumps(self.two, sort_keys=True)
        rf.truncate_doc(self.two, at=1)
        self.assertEqual(before, json.dumps(self.two, sort_keys=True))

    def test_bad_fraction_raises(self):
        with self.assertRaises(ValueError):
            rf.truncate_doc(self.two, fraction=1.5)


class BenchCase(unittest.TestCase):
    def test_bench_reports_timings(self):
        result = rf.bench(synthetic_doc(), resolution=96, repeats=2)
        self.assertEqual(result["repeats"], 2)
        self.assertEqual(len(result["fast_s"]), 2)
        self.assertGreater(result["fast_p50_s"], 0.0)
        self.assertGreaterEqual(result["peak_mem_mb"], 0.0)
        self.assertEqual(result["quality"], "fast")


class SampleParityCase(unittest.TestCase):
    """Coverage/parity over the six WP-D samples (R256, as shipped)."""

    @classmethod
    def setUpClass(cls):
        cls.docs = {
            name: json.loads(sample_path(name).read_text(encoding="utf-8"))
            for name in (*SUPPORTED_SAMPLES, *FALLBACK_SAMPLES)
        }

    def test_supported_and_fallback_sets(self):
        for name in SUPPORTED_SAMPLES:
            ok, reasons = rf.fast_supported(self.docs[name])
            self.assertTrue(ok, f"{name}: {reasons}")
        for name in FALLBACK_SAMPLES:
            ok, reasons = rf.fast_supported(self.docs[name])
            self.assertFalse(ok)
            self.assertTrue(reasons)

    def test_sample_parity_metrics_are_recorded(self):
        for name in SUPPORTED_SAMPLES:
            with self.subTest(sample=name):
                doc = self.docs[name]
                fast, info = rf.render_with_info(doc, resolution=256)
                exact = rf.render_auto(doc, resolution=256, quality="exact")
                self.assertEqual(info["quality"], "fast")
                metrics = rf.compare_images(exact, fast)
                # sanity floor; the literal parity gate is reported in reports/WP-F.md
                self.assertGreater(metrics["psnr_db"], 25.0, metrics)
                self.assertGreater(metrics["within2_pct"], 70.0, metrics)
        for name in FALLBACK_SAMPLES:
            with self.subTest(sample=name):
                img, info = rf.render_with_info(self.docs[name], resolution=256)
                self.assertEqual(info["quality"], "exact")
                self.assertTrue(info["fallback_reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
