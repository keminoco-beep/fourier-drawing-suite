"""Optional real-world SVG conversion (OmniSVG output or complex_eval export).

Disabled by default because the sample is multi-megabyte.  Enable with::

    $env:FDSL_SVG_REAL = "1"
    python -m unittest discover -s tests/svg -v

If WP-R has produced OmniSVG output under ``poc/omnisvg`` this test prefers it;
otherwise it uses the existing ``poc/complex_eval/out_ref0/ref0.svg`` stress
sample (48k paths, 8.3 MB).  Both are read-only inputs.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import REPO_ROOT  # noqa: E402

from fourier_prompt.svg_import import svg_to_fdsl  # noqa: E402

_ENABLED = os.environ.get("FDSL_SVG_REAL") == "1"


def _candidate_svgs():
    candidates = sorted((REPO_ROOT / "poc" / "omnisvg").glob("**/*.svg"))
    fallback = REPO_ROOT / "poc" / "complex_eval" / "out_ref0" / "ref0.svg"
    if fallback.exists():
        candidates.append(fallback)
    return [path for path in candidates if path.exists()]


@unittest.skipUnless(_ENABLED, "set FDSL_SVG_REAL=1 to run the multi-megabyte real SVG case")
class RealSvgCase(unittest.TestCase):
    def test_real_svg_converts_and_renders(self):
        import numpy as np

        from fourier_prompt.compile import compile_plan
        from fourier_prompt.compat import skill_api

        files = _candidate_svgs()
        if not files:
            self.skipTest("no real SVG sample available (WP-R output not ready)")
        path = files[0]
        plan, warnings, stats = svg_to_fdsl(
            path, max_paths=32, max_total_points=60000, on_overflow="trim"
        )
        self.assertLessEqual(len(plan["elements"]), 32)
        self.assertGreater(stats["shapes_seen"], 10)
        # Trimming a many-path document must be reported, never silent.
        if stats["paths_dropped"]:
            self.assertIn("SI_W_PATH_LIMIT", [item["code"] for item in warnings])
        result = compile_plan(plan, resolution=96)
        image = skill_api.render(result["fbs"], resolution=96, supersample=2)
        diff = np.abs(image.astype(int) - np.array([255, 255, 255])).sum(axis=2)
        self.assertGreater(int((diff > 8).mean() * 96 * 96), 96 * 96 * 0.05)
        print(
            f"\n[real svg] {path.name}: {stats['shapes_seen']} shapes, "
            f"{stats['elements']} elements, {stats['points']} points, "
            f"import {stats['seconds']}s, compile+render ok"
        )


if __name__ == "__main__":
    unittest.main()
