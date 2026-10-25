"""Unit tests for the eight ``fourier_prompt.tools`` functions (WP-H).

Covers the normal and error paths of every tool, determinism, JSON safety and
the "tools never modify their inputs" contract.
"""

from __future__ import annotations

import json
import unittest

from _fixtures import Fixture, png_magic, png_size, tools


class ToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture("fpt-tools-unit-")

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    # -- status -----------------------------------------------------------
    def test_status_reports_environment(self):
        report = tools.status()
        self.assertTrue(report["ok"])
        self.assertEqual(report["tool"], "status")
        self.assertTrue(report["skill"]["found"])
        self.assertEqual(report["skill"]["version"], "2.0.0")
        self.assertTrue(report["skill"]["selftest"]["ok"])
        self.assertEqual(report["brush_catalog"]["active"], 20)
        self.assertEqual(report["brush_catalog"]["planned"], 11)
        self.assertEqual(report["fdraw"]["version"], "fdraw/1")
        self.assertFalse(report["fonts"]["available"])
        self.assertIn("ffmpeg", report)
        json.dumps(report, allow_nan=False)

    def test_status_invalid_argument(self):
        with self.assertRaises(tools.ToolError) as caught:
            tools.status(selftest="yes")
        self.assertEqual(caught.exception.code, "TOOL_E_ARG")

    # -- list_brushes -----------------------------------------------------
    def test_list_brushes_active(self):
        result = tools.list_brushes(status="active")
        self.assertTrue(result["ok"])
        self.assertEqual(result["counts"]["active"], 20)
        self.assertEqual(result["counts"]["planned"], 0)
        self.assertEqual(len(result["brushes"]), 20)
        for brush in result["brushes"]:
            self.assertTrue(brush["renderable"], brush["id"])
            self.assertEqual(brush["status"], "active")
            for field in ("id", "name", "category", "roles", "width_eff"):
                self.assertIn(field, brush)

    def test_list_brushes_planned_marked_unrenderable(self):
        result = tools.list_brushes(status="planned")
        self.assertEqual(result["counts"]["planned"], 11)
        self.assertEqual(len(result["brushes"]), 11)
        for brush in result["brushes"]:
            self.assertFalse(brush["renderable"], brush["id"])
            self.assertIn("unrenderable_reason", brush)

    def test_list_brushes_filters_and_bad_status(self):
        filtered = tools.list_brushes(role="bed")
        self.assertTrue(filtered["brushes"])
        for brush in filtered["brushes"]:
            self.assertIn("bed", brush["roles"])
        self.assertEqual(len(tools.list_brushes()["brushes"]), 31)
        with self.assertRaises(tools.ToolError) as caught:
            tools.list_brushes(status="retired")
        self.assertEqual(caught.exception.code, "TOOL_E_ARG")

    # -- inspect ----------------------------------------------------------
    def test_inspect_fdraw(self):
        report = tools.inspect(str(self.fx.fdraw_path))
        self.assertTrue(report["ok"])
        self.assertEqual(report["kind"], "fdraw")
        self.assertEqual(report["document"]["counts"]["elements"], self.fx.stroke_count)
        self.assertGreaterEqual(report["document"]["counts"]["layers"], 2)
        self.assertTrue(report["container"]["root_sha256_ok"])
        self.assertEqual(len(report["container"]["root_sha256"]), 64)
        self.assertEqual(report["file_bytes"], self.fx.fdraw_path.stat().st_size)

    def test_inspect_fbs_and_plan(self):
        fbs_report = tools.inspect(str(self.fx.fbs_path))
        self.assertEqual(fbs_report["kind"], "fbs")
        self.assertTrue(fbs_report["valid"])
        self.assertEqual(fbs_report["document"]["version"], "2.0")
        plan_report = tools.inspect(str(self.fx.plan_path))
        self.assertEqual(plan_report["kind"], "plan")
        self.assertTrue(plan_report["plan"]["valid"])
        self.assertEqual(plan_report["plan"]["counts"]["elements"], 4)
        self.assertEqual(plan_report["plan"]["counts"]["layers"], 2)
        self.assertEqual(plan_report["plan"]["element_kinds"]["fourier_gen"], 1)

    def test_inspect_missing_and_unsupported(self):
        with self.assertRaises(tools.ToolError) as missing:
            tools.inspect(str(self.fx.dir / "does-not-exist.fdraw"))
        self.assertEqual(missing.exception.code, "TOOL_E_NOT_FOUND")
        with self.assertRaises(tools.ToolError) as unsupported:
            tools.inspect(str(self.fx.not_fbs))
        self.assertEqual(unsupported.exception.code, "TOOL_E_INPUT")

    # -- render -----------------------------------------------------------
    def test_render_fdraw_writes_png(self):
        out = self.fx.out("render_fdraw.png")
        result = tools.render(str(self.fx.fdraw_path), out, resolution=96)
        self.assertTrue(result["ok"])
        self.assertEqual((result["width"], result["height"]), (96, 96))
        self.assertEqual(result["quality"], "fast")
        self.assertIn(result["backend"], ("render_fast", "skill_exact"))
        self.assertEqual(result["fallback"], result["backend"] != "render_fast")
        if result["fallback"]:
            self.assertIsNotNone(result["fallback_reason"])
        self.assertEqual(png_size(out), (96, 96))
        self.assertEqual(png_magic(out), b"\x89PNG\r\n\x1a\n")
        self.assertGreater(result["png_bytes"], 0)
        self.assertEqual(len(result["pixel_sha256"]), 64)
        self.assertGreaterEqual(result["elapsed_ms"], 0.0)

    def test_render_fbs_and_plan_inputs(self):
        fbs_out = self.fx.out("render_fbs.png")
        plan_out = self.fx.out("render_plan.png")
        fbs_result = tools.render(str(self.fx.fbs_path), fbs_out, resolution=64, quality="exact")
        plan_result = tools.render(str(self.fx.plan_path), plan_out, resolution=64)
        self.assertEqual(fbs_result["input"]["kind"], "fbs")
        self.assertEqual(fbs_result["backend"], "skill_exact")
        self.assertEqual(plan_result["input"]["kind"], "plan")
        self.assertIn("compiled", plan_result)
        self.assertEqual(png_size(fbs_out), (64, 64))
        self.assertEqual(png_size(plan_out), (64, 64))

    def test_render_rejects_bad_arguments(self):
        out = self.fx.out("bad.png")
        cases = (
            (dict(quality="ultra"), "TOOL_E_ARG"),
            (dict(resolution=8), "TOOL_E_ARG"),
            (dict(resolution=True), "TOOL_E_ARG"),
            (dict(supersample=9, quality="exact"), "TOOL_E_ARG"),
        )
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                kwargs = dict(resolution=32)
                kwargs.update(overrides)
                with self.assertRaises(tools.ToolError) as caught:
                    tools.render(str(self.fx.fbs_path), out, **kwargs)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(tools.ToolError) as overwrite:
            tools.render(str(self.fx.fbs_path), str(self.fx.fbs_path), resolution=32)
        self.assertEqual(overwrite.exception.code, "TOOL_E_ARG")
        with self.assertRaises(tools.ToolError) as wrong_suffix:
            tools.render(str(self.fx.fbs_path), self.fx.out("x.jpg"), resolution=32)
        self.assertEqual(wrong_suffix.exception.code, "TOOL_E_ARG")

    def test_render_is_deterministic(self):
        first = tools.render(str(self.fx.fdraw_path), self.fx.out("det1.png"), resolution=64)
        second = tools.render(str(self.fx.fdraw_path), self.fx.out("det2.png"), resolution=64)
        self.assertEqual(first["pixel_sha256"], second["pixel_sha256"])

    # -- preview ----------------------------------------------------------
    def test_preview_keeps_a_prefix(self):
        out = self.fx.out("preview.png")
        result = tools.preview(str(self.fx.fdraw_path), out, fraction=0.5, resolution=64)
        total = result["total"]
        self.assertEqual(total, self.fx.stroke_count)
        self.assertEqual(result["kept"], min(total, max(1, -(-total // 2))))
        self.assertTrue(result["truncated"])
        self.assertAlmostEqual(result["percent"], 100.0 * result["kept"] / total, places=2)
        self.assertEqual(png_size(out), (64, 64))
        # a 50% prefix must differ from the full drawing
        full = tools.render(str(self.fx.fdraw_path), self.fx.out("preview_full_ref.png"), resolution=64)
        metric = tools.compare(out, full["out"])
        self.assertGreater(metric["metrics"]["max_abs_diff"], 0)
        self.assertFalse(metric["parity_pass"])

    def test_preview_fraction_one_matches_full_render(self):
        preview = tools.preview(
            str(self.fx.fbs_path), self.fx.out("preview_all.png"), fraction=1.0, resolution=64
        )
        full = tools.render(str(self.fx.fbs_path), self.fx.out("preview_all_ref.png"), resolution=64)
        self.assertFalse(preview["truncated"])
        self.assertTrue(preview["full_preview"])
        self.assertEqual(preview["kept"], preview["total"])
        self.assertEqual(preview["pixel_sha256"], full["pixel_sha256"])

    def test_preview_invalid_fraction(self):
        for fraction in (0, -0.25, 1.5, "0.5", None):
            with self.subTest(fraction=fraction):
                with self.assertRaises(tools.ToolError) as caught:
                    tools.preview(
                        str(self.fx.fbs_path), self.fx.out("bad_prev.png"), fraction=fraction
                    )
                self.assertEqual(caught.exception.code, "TOOL_E_ARG")

    # -- frame ------------------------------------------------------------
    def test_frame_prefix_and_clamp(self):
        out = self.fx.out("frame.png")
        result = tools.frame(str(self.fx.fdraw_path), out, at=2, resolution=64)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["at"], 2)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["clamped"])
        self.assertEqual(png_size(out), (64, 64))
        clamped = tools.frame(str(self.fx.fdraw_path), self.fx.out("frame_all.png"), at=9999, resolution=64)
        self.assertEqual(clamped["kept"], clamped["total"])
        self.assertFalse(clamped["truncated"])
        self.assertTrue(clamped["clamped"])
        self.assertIsNotNone(clamped["note"])

    def test_frame_rejects_bad_at(self):
        with self.assertRaises(tools.ToolError) as caught:
            tools.frame(str(self.fx.fbs_path), self.fx.out("f.png"), at=0)
        self.assertEqual(caught.exception.code, "TOOL_E_ARG")

    # -- compare ----------------------------------------------------------
    def test_compare_identical_and_different(self):
        first = tools.render(str(self.fx.fdraw_path), self.fx.out("cmp_a.png"), resolution=64)
        same = tools.compare(first["out"], first["out"])
        self.assertTrue(same["parity_pass"])
        self.assertEqual(same["metrics"]["max_abs_diff"], 0)
        self.assertEqual(same["metrics"]["mean_abs_diff"], 0.0)
        # identical images: either the local backend reports None (infinite dB)
        # or render_fast reports its 99 dB sentinel - both must pass the gate
        self.assertTrue(same["metrics"]["psnr_db"] is None or same["metrics"]["psnr_db"] >= 42.0)
        self.assertTrue(same["metrics"]["identical"])
        self.assertEqual(same["metrics"]["within1_pct"], 100.0)
        self.assertEqual(same["metrics"]["within2_pct"], 100.0)
        self.assertEqual(same["shape"], [64, 64])
        self.assertIn(same["backend"], ("local", "render_fast"))

        preview = tools.preview(
            str(self.fx.fdraw_path), self.fx.out("cmp_b.png"), fraction=0.5, resolution=64
        )
        different = tools.compare(first["out"], preview["out"])
        self.assertFalse(different["parity_pass"])
        self.assertGreater(different["metrics"]["max_abs_diff"], 2)
        self.assertGreater(different["metrics"]["mean_abs_diff"], 0.0)
        self.assertLess(different["metrics"]["within2_pct"], 100.0)
        self.assertIsNotNone(different["metrics"]["psnr_db"])

    def test_compare_region_and_errors(self):
        image = tools.render(str(self.fx.fdraw_path), self.fx.out("cmp_r.png"), resolution=64)
        region = {"x": 0, "y": 0, "width": 16, "height": 16}
        cropped = tools.compare(image["out"], image["out"], region=region)
        self.assertEqual(cropped["region"], region)
        self.assertTrue(cropped["parity_pass"])
        self.assertEqual(tools.compare(image["out"], image["out"], region=[0, 0, 8, 8])["region"]["width"], 8)

        small = tools.render(str(self.fx.fdraw_path), self.fx.out("cmp_small.png"), resolution=32)
        with self.assertRaises(tools.ToolError) as mismatch:
            tools.compare(image["out"], small["out"])
        self.assertEqual(mismatch.exception.code, "TOOL_E_COMPARE")
        with self.assertRaises(tools.ToolError) as bad_region:
            tools.compare(
                image["out"], image["out"], region={"x": 0, "y": 0, "width": 9999, "height": 8}
            )
        self.assertEqual(bad_region.exception.code, "TOOL_E_ARG")

    # -- validate ---------------------------------------------------------
    def test_validate_three_kinds(self):
        fdraw_report = tools.validate(str(self.fx.fdraw_path))
        self.assertTrue(fdraw_report["ok"])
        self.assertEqual(fdraw_report["kind"], "fdraw")
        self.assertEqual(fdraw_report["errors"], [])
        self.assertTrue(fdraw_report["checks"]["root_sha256_ok"])

        fbs_report = tools.validate(str(self.fx.fbs_path))
        self.assertTrue(fbs_report["ok"])
        self.assertEqual(fbs_report["kind"], "fbs")
        self.assertTrue(fbs_report["checks"]["format"])

        plan_report = tools.validate(str(self.fx.plan_path))
        self.assertTrue(plan_report["ok"])
        self.assertEqual(plan_report["kind"], "plan")
        self.assertEqual(plan_report["checks"]["counts"]["elements"], 4)

    def test_validate_error_paths(self):
        bad_plan = tools.validate(str(self.fx.bad_plan_path))
        self.assertFalse(bad_plan["ok"])
        self.assertTrue(bad_plan["errors"])
        self.assertIn("code", bad_plan["errors"][0])

        corrupt = tools.validate(str(self.fx.corrupt_fdraw_path))
        self.assertFalse(corrupt["ok"])
        self.assertTrue(corrupt["errors"])

        with self.assertRaises(tools.ToolError) as unsupported:
            tools.validate(str(self.fx.not_fbs))
        self.assertEqual(unsupported.exception.code, "TOOL_E_INPUT")

    # -- cross-cutting ----------------------------------------------------
    def test_tools_do_not_modify_inputs(self):
        from fourier_prompt.tools.common import sha256_file

        inputs = (self.fx.fdraw_path, self.fx.fbs_path, self.fx.plan_path)
        before = {str(path): sha256_file(path) for path in inputs}
        tools.inspect(str(self.fx.fdraw_path))
        tools.validate(str(self.fx.fdraw_path))
        tools.validate(str(self.fx.fbs_path))
        tools.validate(str(self.fx.plan_path))
        tools.render(str(self.fx.fdraw_path), self.fx.out("ro_render.png"), resolution=48)
        tools.preview(str(self.fx.fdraw_path), self.fx.out("ro_preview.png"), fraction=0.5, resolution=48)
        tools.frame(str(self.fx.fdraw_path), self.fx.out("ro_frame.png"), at=1, resolution=48)
        after = {str(path): sha256_file(path) for path in inputs}
        self.assertEqual(before, after)

    def test_call_tool_registry_and_json_safety(self):
        names = tools.tool_names()
        self.assertEqual(len(names), 8)
        listed = tools.list_tools()
        self.assertEqual([item["name"] for item in listed], list(names))
        for item in listed:
            self.assertEqual(item["inputSchema"]["type"], "object")
            self.assertIn("description", item)
        self.assertEqual(tools.call_tool("fdraw_status", {"selftest": False})["tool"], "status")
        with self.assertRaises(tools.ToolError) as unknown:
            tools.call_tool("draw_everything", {})
        self.assertEqual(unknown.exception.code, "TOOL_E_ARG")
        with self.assertRaises(tools.ToolError) as extra:
            tools.call_tool("status", {"selftest": False, "verbose": True})
        self.assertEqual(extra.exception.code, "TOOL_E_ARG")
        with self.assertRaises(tools.ToolError) as missing:
            tools.call_tool("inspect", {})
        self.assertEqual(missing.exception.code, "TOOL_E_ARG")

        payloads = [
            tools.status(selftest=False),
            tools.list_brushes(status="active"),
            tools.inspect(str(self.fx.fdraw_path)),
            tools.render(str(self.fx.fdraw_path), self.fx.out("json.png"), resolution=32),
            tools.preview(str(self.fx.fdraw_path), self.fx.out("json_p.png"), fraction=0.5, resolution=32),
            tools.frame(str(self.fx.fdraw_path), self.fx.out("json_f.png"), at=1, resolution=32),
            tools.compare(self.fx.out("json.png"), self.fx.out("json.png")),
            tools.validate(str(self.fx.fdraw_path)),
        ]
        for payload in payloads:
            json.dumps(payload, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
