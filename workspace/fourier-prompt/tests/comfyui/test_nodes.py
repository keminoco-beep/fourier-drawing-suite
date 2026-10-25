"""Execution tests for the eight ComfyUI-FDP nodes under mocked host modules.

No real ComfyUI and no torch: IMAGE values fall back to numpy, except in one
test that installs a fake ``torch`` to exercise the real tensor path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
import unittest

import numpy as np

from _fixtures import INTERRUPT, NODES, OUTPUT_DIR, SAMPLE


def run(coro):
    return asyncio.run(coro)


def load_sample(**kwargs):
    return run(NODES["FDPLoadFdraw"]().load("mini.fdraw", **kwargs))


class TestLoadSave(unittest.TestCase):
    def test_load_returns_fdraw_and_summary(self):
        value, summary = load_sample()
        self.assertEqual(type(value).__name__, "FDrawValue")
        self.assertIsInstance(value.render, dict)
        self.assertTrue(value.root_sha256)
        self.assertEqual(value.root_sha256, value.cache_key)
        self.assertEqual(summary.count("elements"), 1)
        self.assertIn("4 elements", summary)
        self.assertIn("2 layers", summary)
        self.assertTrue(value.path.endswith("mini.fdraw"))
        self.assertIsInstance(value.source_fdsl, dict)
        self.assertEqual(value.source_fdsl.get("schema"), "fdsl/2.1")
        self.assertTrue(value.inspection.get("root_sha256_ok"))

    def test_load_bad_container_and_missing_file(self):
        node = NODES["FDPLoadFdraw"]()
        with self.assertRaises(RuntimeError) as bad:
            run(node.load("bad.fdraw"))
        self.assertIn("FD_E_ZIP", str(bad.exception))
        with self.assertRaises(RuntimeError) as missing:
            run(node.load("nope.fdraw"))
        self.assertIn("FD_E_FILE_NOT_FOUND", str(missing.exception))

    def test_load_path_override(self):
        node = NODES["FDPLoadFdraw"]()
        value, summary = run(node.load("<empty>", path_override=str(SAMPLE["fdraw"])))
        self.assertTrue(value.root_sha256)
        self.assertIn("mini.fdraw", summary)

    def test_save_roundtrip_is_byte_identical(self):
        value, _summary = load_sample()
        relative = OUTPUT_DIR / "fdraw" / "roundtrip_00001.fdraw"
        result = run(NODES["FDPSaveFdraw"]().save(value, "fdraw/roundtrip"))
        self.assertIsInstance(result, dict)
        self.assertIn("ui", result)
        self.assertEqual(result["result"], ("fdraw/roundtrip_00001.fdraw",))
        self.assertTrue(relative.is_file())
        saved_bytes = relative.read_bytes()
        self.assertEqual(
            hashlib.sha256(saved_bytes).hexdigest(),
            hashlib.sha256(SAMPLE["fdraw"].read_bytes()).hexdigest(),
        )
        reloaded, _ = run(NODES["FDPLoadFdraw"]().load("roundtrip_00001.fdraw"))
        self.assertEqual(reloaded.root_sha256, value.root_sha256)


class TestCompile(unittest.TestCase):
    def test_compile_fdsl_json(self):
        payload = json.dumps(SAMPLE["plan"])
        node = NODES["FDPCompileFDSL"]()
        value, report_json = run(node.compile(payload, 64, True, False))
        self.assertEqual(len(value.render.get("strokes") or []), 4)
        self.assertTrue(value.root_sha256)
        self.assertGreater(value.zip_bytes, 0)
        report = json.loads(report_json)
        self.assertEqual(report["resolution"], 64)
        self.assertEqual(report["elements"], 4)
        self.assertEqual(report["root_sha256"], value.root_sha256)
        self.assertEqual(report["cache_key"], value.cache_key)

    def test_compile_accepts_fdsl_type(self):
        node = NODES["FDPCompileFDSL"]()
        value, _report = run(node.compile("", 64, True, False, fdsl=SAMPLE["plan"]))
        self.assertEqual(len(value.render.get("strokes") or []), 4)

    def test_compile_bad_json_and_bad_plan(self):
        node = NODES["FDPCompileFDSL"]()
        with self.assertRaises(RuntimeError) as bad_json:
            run(node.compile("{not json", 64, True, False))
        self.assertIn("FD_E_FDSL_JSON", str(bad_json.exception))
        with self.assertRaises(RuntimeError) as bad_plan:
            run(node.compile(json.dumps({"schema": "fdsl/9", "canvas": {}, "elements": []}), 64, False, False))
        self.assertIn("E_", str(bad_plan.exception))

    def test_compile_strict_turns_warnings_into_error(self):
        plan = json.loads(json.dumps(SAMPLE["plan"]))
        plan["layers"][0]["visible"] = False
        node = NODES["FDPCompileFDSL"]()
        value, report_json = run(node.compile(json.dumps(plan), 64, False, False))
        self.assertGreater(json.loads(report_json)["warning_count"], 0)
        with self.assertRaises(RuntimeError) as strict:
            run(node.compile(json.dumps(plan), 64, False, True))
        self.assertIn("FD_E_SCHEMA", str(strict.exception))

    def test_compile_rejects_empty(self):
        with self.assertRaises(RuntimeError) as empty:
            run(NODES["FDPCompileFDSL"]().compile("  ", 64, True, False))
        self.assertIn("FD_E_FDSL_EMPTY", str(empty.exception))


class TestRenderPreview(unittest.TestCase):
    def test_render_numpy_fallback(self):
        value, _ = load_sample()
        before = json.dumps(value.render, sort_keys=True)
        result = run(
            NODES["FDPRenderFdraw"]().render(
                value, resolution=48, quality="fast", background="#ffffff",
                supersample=1, width_scale=0.0,
            )
        )
        self.assertEqual(before, json.dumps(value.render, sort_keys=True), "render must not mutate FDRAW")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 1)
        image = result[0]
        self.assertIsInstance(image, np.ndarray)
        self.assertEqual(image.shape, (1, 48, 48, 3))
        self.assertEqual(image.dtype, np.float32)
        self.assertGreaterEqual(float(image.min()), 0.0)
        self.assertLessEqual(float(image.max()), 1.0)
        self.assertGreater(int(image.max() * 255), 0)

    def test_render_uses_file_target_resolution_when_zero(self):
        value, _ = load_sample()
        image = run(NODES["FDPRenderFdraw"]().render(value, 0, "fast", "auto", 1, 0.0))[0]
        self.assertEqual(image.shape[1], 64)
        self.assertEqual(image.shape[2], 64)

    def test_render_fake_torch_tensor(self):
        class FakeTensor:
            def __init__(self, array):
                self.arr = np.array(array, copy=True)

            def float(self):
                self.arr = self.arr.astype(np.float32)
                return self

            def div_(self, value):
                self.arr = self.arr / value
                return self

            def unsqueeze(self, dim):
                self.arr = np.expand_dims(self.arr, dim)
                return self

        fake = types.ModuleType("torch")
        fake.from_numpy = lambda array: FakeTensor(array)
        sys.modules["torch"] = fake
        try:
            value, _ = load_sample()
            image = run(NODES["FDPRenderFdraw"]().render(value, 32, "fast", "auto", 1, 0.0))[0]
        finally:
            sys.modules.pop("torch", None)
        self.assertIsInstance(image, FakeTensor)
        self.assertEqual(image.arr.shape, (1, 32, 32, 3))
        self.assertEqual(image.arr.dtype, np.float32)
        self.assertLessEqual(float(image.arr.max()), 1.0)

    def test_render_background_and_quality_errors(self):
        value, _ = load_sample()
        node = NODES["FDPRenderFdraw"]()
        with self.assertRaises(RuntimeError) as background:
            run(node.render(value, 32, "fast", "chartreuse", 1, 0.0))
        self.assertIn("FD_E_BACKGROUND", str(background.exception))
        with self.assertRaises(RuntimeError) as quality:
            run(node.render(value, 32, "ultra", "auto", 1, 0.0))
        self.assertIn("FD_E_QUALITY", str(quality.exception))

    def test_preview_prefix_and_fraction_validation(self):
        value, _ = load_sample()
        node = NODES["FDPPreviewFdraw"]()
        image = run(node.preview(value, 0.5, 48, "fast", "auto", 1))[0]
        self.assertEqual(image.shape, (1, 48, 48, 3))
        full = run(node.preview(value, 1.0, 48, "fast", "auto", 1))[0]
        self.assertEqual(full.shape, image.shape)
        self.assertFalse(np.array_equal(full, image), "50% prefix must differ from the full drawing")
        with self.assertRaises(RuntimeError) as fraction:
            run(node.preview(value, 0.0, 48, "fast", "auto", 1))
        self.assertIn("FD_E_PREVIEW_FRACTION", str(fraction.exception))

    def test_render_is_interruptible(self):
        value, _ = load_sample()
        INTERRUPT["flag"] = True
        try:
            with self.assertRaises(InterruptedError) as stopped:
                run(NODES["FDPRenderFdraw"]().render(value, 32, "fast", "auto", 1, 0.0))
        finally:
            INTERRUPT["flag"] = False
        self.assertIn("FD_E_INTERRUPTED", str(stopped.exception))


class TestInspectBatch(unittest.TestCase):
    def test_inspect_json(self):
        value, _ = load_sample()
        payload = json.loads(run(NODES["FDPInspectFdraw"]().inspect(value))[0])
        self.assertTrue(payload["root_sha256_ok"])
        self.assertEqual(payload["element_count"], 4)
        self.assertEqual(payload["layer_count"], 2)
        self.assertEqual(payload["root_sha256"], value.root_sha256)
        self.assertEqual(payload["font_count"], 0)
        self.assertTrue(payload["has_source"])
        self.assertIn("canvas", payload)

    def test_batch_returns_aligned_lists(self):
        node = NODES["FDPBatchFdraw"]()
        values, reports = run(node.batch("mini.fdraw\nmini_copy.fdraw", True, False))
        self.assertIsInstance(values, list)
        self.assertIsInstance(reports, list)
        self.assertEqual(len(values), 2)
        self.assertEqual(len(reports), 2)
        for item in reports:
            parsed = json.loads(item)
            self.assertEqual(parsed["root_sha256"], values[0].root_sha256)
            self.assertIn("inspect", parsed)
            self.assertEqual(parsed["elements"], 4)

    def test_batch_missing_and_skip(self):
        node = NODES["FDPBatchFdraw"]()
        with self.assertRaises(RuntimeError) as missing:
            run(node.batch("mini.fdraw\nmissing.fdraw", False, False))
        self.assertIn("FD_E_FILE_NOT_FOUND", str(missing.exception))
        values, reports = run(node.batch("mini.fdraw\nmissing.fdraw", False, True))
        self.assertEqual(len(values), 1)
        self.assertEqual(len(reports), 1)
        with self.assertRaises(RuntimeError) as empty:
            run(node.batch("# only comments\n\n", False, False))
        self.assertIn("FD_E_BATCH_EMPTY", str(empty.exception))


class TestVideo(unittest.TestCase):
    def test_video_outputs_mp4(self):
        try:
            import imageio_ffmpeg  # noqa: F401
        except ImportError:
            self.skipTest("imageio-ffmpeg not installed")
        value, _ = load_sample()
        path, info = run(
            NODES["FDPVideoFdraw"]().video(
                value, 1.0, 4, 48, "fast", 16, "auto", "fdp/video_test"
            )
        )
        self.assertTrue(path.endswith(".mp4"))
        self.assertTrue(path.startswith(str(OUTPUT_DIR)))
        self.assertTrue(__import__("os").path.isfile(path))
        self.assertGreater(__import__("os").path.getsize(path), 0)
        self.assertIn("frames=4", info)
        self.assertIn("48x48", info)


if __name__ == "__main__":
    unittest.main()
