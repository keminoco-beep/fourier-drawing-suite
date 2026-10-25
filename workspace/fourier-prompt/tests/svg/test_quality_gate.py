"""WP-W: svg_quality gate + six agent-authored example SVGs (stdlib unittest)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import PACKAGE_ROOT, REPO_ROOT  # noqa: E402

from fourier_prompt.svg_quality import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    QualityThresholds,
    evaluate_svg,
)

EXAMPLE_DIR = REPO_ROOT / "poc" / "agent_svg" / "svg"
EXAMPLE_NAMES = [
    "01_portrait",
    "02_landscape",
    "03_fox",
    "04_mech",
    "05_still_life",
    "06_cathedral",
]
STRICT = replace(
    DEFAULT_THRESHOLDS, min_paths=30, min_shapes=30, min_points=400, max_unsupported=0
)

_CIRCLES = "".join(
    f'<circle cx="{60 + (index % 4) * 60}" cy="{60 + (index // 4) * 60}" r="24"/>'
    for index in range(12)
)
GOOD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" '
    'viewBox="0 0 300 300">'
    '<rect x="0" y="0" width="300" height="300" fill="#ffffff"/>'
    '<g fill="none" stroke="#1a1a1a" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round">'
    f"{_CIRCLES}</g></svg>"
)

_SMALL_CIRCLES = "".join(
    f'<circle cx="{290 + (index % 5) * 6}" cy="{290 + (index // 5) * 6}" r="3"/>'
    for index in range(20)
)
SMALL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" '
    'viewBox="0 0 600 600">'
    '<g fill="none" stroke="#1a1a1a" stroke-width="1">'
    f"{_SMALL_CIRCLES}</g></svg>"
)


def gate(document, **overrides):
    thresholds = replace(DEFAULT_THRESHOLDS, **overrides) if overrides else DEFAULT_THRESHOLDS
    return evaluate_svg(document, thresholds=thresholds)


def failed_ids(report):
    return {check["id"] for check in report["checks"] if not check["ok"]}


class QualityGateCase(unittest.TestCase):
    def test_good_svg_passes_defaults(self):
        report = gate(GOOD_SVG)
        self.assertTrue(report["ok"], report["errors"])
        self.assertGreaterEqual(report["metrics"]["shapes"], 12)
        self.assertGreaterEqual(report["metrics"]["paths"], 12)
        self.assertGreaterEqual(report["metrics"]["points"], 150)
        self.assertGreater(report["metrics"]["ink_ratio"], 0.01)
        self.assertGreater(report["metrics"]["bbox_ratio"], 0.3)
        self.assertGreaterEqual(report["score"], 50.0)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["checks"][-1]["id"], "valid_import")

    def test_too_few_paths_fails(self):
        sparse = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" '
            'viewBox="0 0 200 200">'
            '<rect x="10" y="10" width="180" height="180" fill="none" stroke="#000"/>'
            "</svg>"
        )
        report = gate(sparse)
        self.assertFalse(report["ok"])
        self.assertIn("min_paths", failed_ids(report))
        self.assertTrue(any(code["code"] == "SQ_E_MIN_PATHS" for code in report["errors"]))

    def test_low_ink_fails(self):
        report = gate(SMALL_SVG, min_bbox=0.0, min_points=0, min_shapes=0, min_ink=0.01)
        self.assertFalse(report["ok"])
        self.assertIn("min_ink", failed_ids(report))
        self.assertLess(report["metrics"]["ink_ratio"], 0.01)

    def test_small_bbox_fails(self):
        report = gate(SMALL_SVG, min_ink=0.0, min_points=0, min_shapes=0, min_bbox=0.30)
        self.assertFalse(report["ok"])
        self.assertIn("min_bbox", failed_ids(report))
        self.assertLess(report["metrics"]["bbox_ratio"], 0.30)

    def test_unsupported_features_fail(self):
        body = _CIRCLES + (
            '<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/>'
            '<stop offset="1" stop-color="#000"/></linearGradient></defs>'
            '<text x="20" y="20">hello</text>'
            '<image x="0" y="0" width="10" height="10" href="x.png"/>'
            '<rect x="0" y="0" width="10" height="10" filter="url(#blur)"/>'
            '<clipPath id="c"><rect x="0" y="0" width="5" height="5"/></clipPath>'
        )
        document = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" '
            f'viewBox="0 0 300 300"><g fill="none" stroke="#1a1a1a">{body}</g></svg>'
        )
        report = gate(document)
        self.assertFalse(report["ok"])
        self.assertIn("max_unsupported", failed_ids(report))
        self.assertGreaterEqual(report["metrics"]["unsupported_total"], 4)
        self.assertIn("gradient", report["metrics"]["unsupported"])
        self.assertIn("text", report["metrics"]["unsupported"])

    def test_threshold_override_can_relax(self):
        relaxed = gate(GOOD_SVG, min_shapes=5, min_points=50, min_ink=0.005, min_bbox=0.2)
        self.assertTrue(relaxed["ok"], relaxed["errors"])
        strict = gate(GOOD_SVG, min_shapes=999)
        self.assertFalse(strict["ok"])
        self.assertIn("min_shapes", failed_ids(strict))

    def test_empty_svg_reports_empty(self):
        empty = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" '
            'viewBox="0 0 200 200">'
            '<rect x="0" y="0" width="10" height="10" fill="none"/></svg>'
        )
        report = evaluate_svg(empty)
        self.assertFalse(report["ok"])
        self.assertTrue(report["metrics"]["empty"])
        self.assertTrue(report["metrics"]["degenerate"])
        self.assertEqual(report["errors"][0]["code"], "SI_E_EMPTY")

    def test_invalid_xml_reports_error(self):
        report = evaluate_svg("<svg><path d='M0 0 L10 10'></svg>")
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "SQ_E_XML")

    def test_report_is_deterministic(self):
        first = gate(GOOD_SVG)
        second = gate(GOOD_SVG)
        self.assertEqual(first["score"], second["score"])
        # Wall-clock timing is intentionally excluded: it is not deterministic.
        first_metrics = dict(first["metrics"])
        second_metrics = dict(second["metrics"])
        first_metrics.pop("import_seconds", None)
        second_metrics.pop("import_seconds", None)
        self.assertEqual(first_metrics, second_metrics)
        self.assertEqual(
            [check["ok"] for check in first["checks"]],
            [check["ok"] for check in second["checks"]],
        )

    def test_thresholds_validation(self):
        with self.assertRaises(ValueError):
            QualityThresholds(min_ink=0.5, max_ink=0.1)
        with self.assertRaises(ValueError):
            QualityThresholds(raster=8)
        with self.assertRaises(ValueError):
            evaluate_svg(GOOD_SVG, thresholds={"unknown_knob": 1})

    def test_wp_u_model_samples_are_rejected(self):
        """Regression: the gate must reject WP-U's two bad model SVGs."""
        p1 = REPO_ROOT / "poc" / "online_gen" / "p1_girl_horns.svg"
        p2 = REPO_ROOT / "poc" / "online_gen" / "p2_cottage_moon.svg"
        if not (p1.exists() and p2.exists()):
            self.skipTest("WP-U model samples not present")
        first = evaluate_svg(p1)
        self.assertFalse(first["ok"])
        self.assertIn("min_paths", failed_ids(first))
        second = evaluate_svg(p2)
        self.assertFalse(second["ok"])
        self.assertIn("min_bbox", failed_ids(second))
        self.assertLess(second["metrics"]["bbox_ratio"], 0.05)
        self.assertLess(second["metrics"]["ink_ratio"], 0.01)


class QualityCliCase(unittest.TestCase):
    def _run(self, path, *extra):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "fourier_prompt.svg_quality",
                "--svg",
                str(path),
                "--json",
                *extra,
            ],
            cwd=str(PACKAGE_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_cli_pass_and_fail_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.svg"
            good.write_text(GOOD_SVG, encoding="utf-8")
            bad = Path(directory) / "bad.svg"
            bad.write_text(SMALL_SVG, encoding="utf-8")
            passed = self._run(good)
            self.assertEqual(passed.returncode, 0, passed.stderr)
            payload = json.loads(passed.stdout)
            self.assertTrue(payload["ok"])
            failed = self._run(bad)
            self.assertEqual(failed.returncode, 1, failed.stderr)
            self.assertFalse(json.loads(failed.stdout)["ok"])

    def test_cli_strict_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.svg"
            good.write_text(GOOD_SVG, encoding="utf-8")
            strict = self._run(good, "--strict")
            self.assertEqual(strict.returncode, 1, strict.stderr)  # <30 shapes
            config = Path(directory) / "gate.json"
            config.write_text(
                json.dumps({"min_shapes": 5, "min_points": 50}), encoding="utf-8"
            )
            relaxed = self._run(good, "--config", str(config))
            self.assertEqual(relaxed.returncode, 0, relaxed.stderr)
            self.assertTrue(json.loads(relaxed.stdout)["ok"])


class ExampleSvgCase(unittest.TestCase):
    def test_six_examples_exist_and_are_marked_agent_authored(self):
        self.assertEqual(len(EXAMPLE_NAMES), 6)
        for name in EXAMPLE_NAMES:
            path = EXAMPLE_DIR / f"{name}.svg"
            self.assertTrue(path.exists(), path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("agent-authored, not model output", text, name)
            lowered = text.lower()
            for forbidden in (
                "<text",
                "<image",
                "<filter",
                "<lineargradient",
                "<radialgradient",
                "<clippath",
                "<mask",
                "<pattern",
                "<use",
            ):
                self.assertNotIn(forbidden, lowered, f"{name}: {forbidden}")

    def test_six_examples_pass_strict_quality(self):
        scores = {}
        for name in EXAMPLE_NAMES:
            report = evaluate_svg(EXAMPLE_DIR / f"{name}.svg", thresholds=STRICT)
            self.assertTrue(report["ok"], f"{name}: {report['errors']}")
            metrics = report["metrics"]
            self.assertGreaterEqual(metrics["shapes"], 30, name)
            self.assertGreaterEqual(metrics["paths"], 30, name)
            self.assertGreaterEqual(metrics["points"], 400, name)
            self.assertEqual(metrics["unsupported_total"], 0, name)
            self.assertFalse(metrics["empty"], name)
            self.assertEqual(metrics["warnings"], 0, name)
            self.assertGreaterEqual(report["score"], 85.0, name)
            scores[name] = report["score"]
        self.assertEqual(len(scores), 6)
        self.assertGreaterEqual(sum(scores.values()) / 6.0, 90.0)

    def test_six_examples_convert_and_render(self):
        import numpy as np

        from fourier_prompt.compile import compile_plan
        from fourier_prompt.compat import skill_api
        from fourier_prompt.svg_import import svg_to_fdsl

        for name in EXAMPLE_NAMES:
            plan, warnings, stats = svg_to_fdsl(EXAMPLE_DIR / f"{name}.svg", target_size=384)
            self.assertEqual(warnings, [], name)
            self.assertGreaterEqual(stats["elements"], 30, name)
            self.assertGreaterEqual(stats["points"], 400, name)
            self.assertEqual(stats["paths_dropped"], 0, name)
            result = compile_plan(plan, resolution=96)
            image = skill_api.render(result["fbs"], resolution=96, supersample=2)
            self.assertEqual(image.shape[2], 3, name)
            ink = (np.abs(image.astype(int) - 255).max(axis=2) > 8).mean()
            self.assertGreater(ink, 0.02, f"{name}: ink {ink:.3f}")


if __name__ == "__main__":
    unittest.main()
