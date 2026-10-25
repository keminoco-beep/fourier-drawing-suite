"""Generator contour tests (WP-D)."""

from __future__ import annotations

import math
import sys
import unittest

from _fixtures import PACKAGE_ROOT

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt import generators  # noqa: E402


def bounds(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


class GeneratorContractCase(unittest.TestCase):
    def test_library_has_ten_generators(self):
        self.assertEqual(len(generators.GEN_NAMES), 10)
        self.assertEqual(set(generators.GEN_NAMES), set(generators.contour.__globals__["_GENS"]))

    def test_circle_radius_and_determinism(self):
        a = generators.contour("circle", {"at": [50, 60], "r": 30}, samples=64)
        b = generators.contour("circle", {"at": [50, 60], "r": 30}, samples=64)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)
        for x, y in a:
            self.assertAlmostEqual(math.hypot(x - 50, y - 60), 30, places=9)

    def test_ellipse_extents(self):
        points = generators.contour(
            "ellipse", {"at": [100, 100], "rx": 40, "ry": 20, "rot": 90}, samples=128
        )
        x0, y0, x1, y1 = bounds(points)
        self.assertAlmostEqual(x0, 80, places=6)
        self.assertAlmostEqual(x1, 120, places=6)
        self.assertAlmostEqual(y0, 60, places=6)
        self.assertAlmostEqual(y1, 140, places=6)

    def test_arc_is_open_with_endpoints(self):
        points = generators.contour(
            "arc", {"at": [100, 100], "r": 50, "from": 0, "to": 90}, samples=32
        )
        self.assertEqual(len(points), 33)
        self.assertAlmostEqual(points[0][0], 150.0, places=9)
        self.assertAlmostEqual(points[0][1], 100.0, places=9)
        self.assertAlmostEqual(points[-1][0], 100.0, places=9)
        self.assertAlmostEqual(points[-1][1], 150.0, places=9)
        self.assertTrue(generators.is_open("arc"))

    def test_regular_polygon_has_exact_vertices(self):
        points = generators.contour(
            "regular_polygon", {"at": [50, 50], "r": 40, "n": 5, "rot": -90}, samples=256
        )
        self.assertEqual(len(points), 5)
        self.assertAlmostEqual(points[0][0], 50.0, places=9)
        self.assertAlmostEqual(points[0][1], 10.0, places=9)

    def test_star_alternates_radii(self):
        points = generators.contour(
            "star", {"at": [0, 0], "r": 100, "ri": 40, "n": 5, "rot": -90}, samples=256
        )
        self.assertEqual(len(points), 10)
        radii = [math.hypot(x, y) for x, y in points]
        for index, radius in enumerate(radii):
            self.assertAlmostEqual(radius, 100.0 if index % 2 == 0 else 40.0, places=9)

    def test_rose_is_closed_and_bounded(self):
        points = generators.contour(
            "rose", {"at": [128, 128], "R": 90, "k": 5, "phi": 0}, samples=512
        )
        self.assertEqual(len(points), 512)
        x0, y0, x1, y1 = bounds(points)
        self.assertLessEqual(x1 - x0, 181.0)
        self.assertLessEqual(y1 - y0, 181.0)
        self.assertAlmostEqual(points[0][0], 218.0, places=6)

    def test_spiral_grows_and_stays_open(self):
        points = generators.contour(
            "spiral", {"at": [100, 100], "r0": 5, "turns": 2.0, "growth": 10}, samples=64
        )
        self.assertEqual(len(points), 65)
        start = math.hypot(points[0][0] - 100, points[0][1] - 100)
        end = math.hypot(points[-1][0] - 100, points[-1][1] - 100)
        self.assertAlmostEqual(start, 5.0, places=9)
        self.assertAlmostEqual(end, 50.0, places=6)
        self.assertTrue(generators.is_open("spiral"))

    def test_wave_open_and_periodic(self):
        points = generators.contour(
            "wave", {"at": [0, 100], "w": 200, "amp": 10, "cycles": 2, "phase": 0}, samples=64
        )
        self.assertEqual(len(points), 65)
        self.assertAlmostEqual(points[0][0], 0.0)
        self.assertAlmostEqual(points[-1][0], 200.0)
        self.assertAlmostEqual(points[0][1], 100.0)
        self.assertAlmostEqual(max(p[1] for p in points), 110.0, places=6)

    def test_lissajous_bounds(self):
        points = generators.contour(
            "lissajous",
            {"at": [0, 0], "A": 30, "B": 20, "a": 3, "b": 2, "delta": 0},
            samples=512,
        )
        self.assertAlmostEqual(max(abs(p[0]) for p in points), 30.0, places=6)
        self.assertAlmostEqual(max(abs(p[1]) for p in points), 20.0, places=6)

    def test_superellipse_axes(self):
        points = generators.contour(
            "superellipse", {"at": [0, 0], "rx": 40, "ry": 25, "n": 4}, samples=256
        )
        self.assertAlmostEqual(max(abs(p[0]) for p in points), 40.0, places=6)
        self.assertAlmostEqual(max(abs(p[1]) for p in points), 25.0, places=6)
        # n=4 时应接近圆角方形：对角方向半径明显大于正圆
        diagonal = math.hypot(points[32][0] / 40, points[32][1] / 25)
        self.assertGreater(diagonal, 0.7)

    def test_unknown_generator_fails(self):
        with self.assertRaises(generators.GenError):
            generators.contour("unicorn", {})

    def test_unknown_parameter_fails(self):
        with self.assertRaises(generators.GenError) as ctx:
            generators.contour("circle", {"r": 10, "radius": 10})
        self.assertIn("radius", str(ctx.exception))

    def test_degenerate_parameters_fail(self):
        for gen, params in (
            ("circle", {"r": 0}),
            ("arc", {"r": 10, "from": 30, "to": 30}),
            ("star", {"r": 10, "ri": 20}),
            ("lissajous", {"a": 0}),
        ):
            with self.subTest(gen=gen):
                with self.assertRaises(generators.GenError):
                    generators.contour(gen, params)


if __name__ == "__main__":
    unittest.main()
