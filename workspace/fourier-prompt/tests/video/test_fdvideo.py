"""Tests for the progressive drawing video pipeline (WP-J)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from _fixtures import ROSE_FDRAW, circle_stroke, decode_frames, multi_doc, synthetic_doc

from fourier_prompt import fdraw, fdvideo, fdvideo_cli


def run_cli(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fdvideo_cli.main(argv)
        except SystemExit as exc:  # argparse usage errors
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class _TmpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
class FramePlanTests(unittest.TestCase):
    def test_basic_plan(self):
        counts = fdvideo.frame_indices(51, 24)
        self.assertEqual(len(counts), 24)
        self.assertEqual(counts[0], 1)
        self.assertEqual(counts[-1], 51)
        self.assertTrue(all(1 <= value <= 51 for value in counts))
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts, list(dict.fromkeys(counts)))  # strictly increasing here

    def test_degenerate_plans(self):
        self.assertEqual(fdvideo.frame_indices(0, 5), [0, 0, 0, 0, 0])
        self.assertEqual(fdvideo.frame_indices(7, 0), [])
        self.assertEqual(fdvideo.frame_indices(7, 1), [7])
        self.assertEqual(fdvideo.frame_indices(1, 4), [1, 1, 1, 1])
        # more frames than elements: the tail holds, never rewinds
        counts = fdvideo.frame_indices(3, 9)
        self.assertEqual(len(counts), 9)
        self.assertEqual(counts[-1], 3)
        self.assertEqual(counts, sorted(counts))
        # fewer frames than elements: commits multiple elements per frame
        counts = fdvideo.frame_indices(100, 4)
        self.assertEqual(counts[0], 1)
        self.assertEqual(counts[-1], 100)
        self.assertEqual(counts, sorted(counts))

    def test_plan_rejects_negative(self):
        for n, frames in ((-1, 4), (3, -2)):
            with self.assertRaises(ValueError):
                fdvideo.frame_indices(n, frames)

    def test_sample_positions(self):
        self.assertEqual(fdvideo.sample_positions(10, 10), list(range(10)))
        positions = fdvideo.sample_positions(100, 5)
        self.assertEqual(positions[0], 0)
        self.assertEqual(positions[-1], 99)
        self.assertEqual(len(positions), 5)
        self.assertEqual(positions, sorted(set(positions)))
        self.assertEqual(fdvideo.sample_positions(3, 99), [0, 1, 2])
        self.assertEqual(fdvideo.sample_positions(0, 4), [])
        with self.assertRaises(ValueError):
            fdvideo.sample_positions(4, -1)

    def test_frame_payload_pads_odd_sizes(self):
        import numpy as np

        img = np.zeros((3, 5, 3), dtype=np.uint8)
        img[:, -1] = 7  # last column marker: must be replicated, not black
        payload, size = fdvideo._frame_payload(img)
        self.assertEqual(size, (6, 4))
        self.assertEqual(len(payload), 6 * 4 * 3)
        arr = np.frombuffer(payload, dtype=np.uint8).reshape(4, 6, 3)
        self.assertTrue((arr[:, 5] == 7).all())
        self.assertTrue((arr[3] == arr[2]).all())  # last row replicated
        even = np.zeros((4, 6, 3), dtype=np.uint8)
        payload2, size2 = fdvideo._frame_payload(even)
        self.assertEqual(size2, (6, 4))
        self.assertEqual(payload2, even.tobytes())


# ---------------------------------------------------------------------------
# rendering / encoding
# ---------------------------------------------------------------------------
class RenderVideoTests(_TmpCase):
    def _video(self, doc, name, **kwargs):
        defaults = dict(
            out=str(self.tmp / name),
            resolution=48,
            duration_s=0.5,
            fps=8,
            quality="fast",
            max_frames=600,
        )
        defaults.update(kwargs)
        return fdvideo.render_video(doc, **defaults)

    def test_empty_document_encodes_blank_frames(self):
        result = self._video(synthetic_doc(strokes=[]), "empty.mp4")
        self.assertEqual(result["elements"], 0)
        self.assertEqual(result["frames"], 4)
        self.assertEqual(result["size"], [48, 48])
        self.assertEqual(len(result["frame_hashes"]), 4)
        self.assertEqual(len(set(result["frame_hashes"])), 1)  # blank every frame
        decoded = decode_frames(result["out"])
        self.assertEqual(decoded["frames"], 4)
        self.assertEqual(decoded["size"], [48, 48])
        self.assertEqual(decoded["first_frame_bytes"], 48 * 48 * 3)

    def test_single_stroke_is_constant_and_deterministic(self):
        doc = synthetic_doc(strokes=[circle_stroke(0)])
        first = self._video(doc, "one_a.mp4", duration_s=0.375, fps=8)  # 3 frames
        second = self._video(doc, "one_b.mp4", duration_s=0.375, fps=8)
        self.assertEqual(first["frames"], 3)
        self.assertEqual(len(set(first["frame_hashes"])), 1)
        self.assertEqual(first["unique_renders"], 1)
        self.assertEqual(first["frame_hashes"], second["frame_hashes"])
        self.assertEqual(first["frame_hashes_sha256"], second["frame_hashes_sha256"])
        self.assertEqual(decode_frames(first["out"])["frames"], 3)

    def test_frame_cap_warns_and_shortens(self):
        result = self._video(
            multi_doc(6), "cap.mp4", duration_s=10.0, fps=30, max_frames=5
        )
        self.assertEqual(result["requested_frames"], 300)
        self.assertEqual(result["frames"], 5)
        self.assertAlmostEqual(result["duration_s"], round(5 / 30, 6), places=6)
        self.assertTrue(any("W_VIDEO_FRAME_CAP" in item for item in result["warnings"]))
        self.assertEqual(decode_frames(result["out"])["frames"], 5)
        # hard ceiling: max_frames above 600 is clamped and reported
        capped = self._video(
            multi_doc(6), "cap600.mp4", duration_s=0.5, fps=8, max_frames=5000
        )
        self.assertEqual(capped["frames"], 4)
        self.assertTrue(any("硬上限" in item for item in capped["warnings"]))

    def test_reveal_progress_and_full_document_last_frame(self):
        import numpy as np

        from fourier_prompt.render_fast import render_auto

        doc = multi_doc(6)
        result = self._video(doc, "reveal.mp4", resolution=64, duration_s=0.75, fps=8)
        self.assertEqual(result["frames"], 6)
        self.assertEqual(result["elements"], 6)
        # 6 elements over 6 frames: every frame commits exactly one new stroke
        self.assertEqual(result["unique_renders"], 6)
        full = render_auto(doc, resolution=64, quality="fast", return_info=True)[0]
        import hashlib

        expected = hashlib.sha256(np.ascontiguousarray(full).tobytes()).hexdigest()
        self.assertEqual(result["frame_hashes"][-1], expected)
        self.assertNotEqual(result["frame_hashes"][0], expected)

    def test_quality_exact_and_background(self):
        doc = synthetic_doc(strokes=[circle_stroke(0)])
        result = self._video(
            doc, "exact.mp4", quality="exact", background=[250, 250, 250],
            duration_s=0.25, fps=8,
        )
        self.assertEqual(result["quality"], "exact")
        self.assertEqual(result["quality_used"], ["exact"])
        self.assertEqual(result["frames"], 2)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(result["probe"]["size"], [48, 48])

    def test_unsupported_shape_falls_back_to_exact(self):
        doc = synthetic_doc(strokes=[circle_stroke(0)], shape="pencil")
        result = self._video(doc, "pencil.mp4", duration_s=0.25, fps=8)
        self.assertEqual(result["quality"], "fast")  # requested
        self.assertEqual(result["quality_used"], ["exact"])  # actually rendered
        self.assertTrue(result["fallbacks"])
        self.assertTrue(any("pencil" in reason for reason in result["fallbacks"]))
        self.assertEqual(result["frames"], 2)
        self.assertEqual(len(result["frame_hashes"]), 2)

    def test_no_ffmpeg_raises_clear_error(self):
        doc = synthetic_doc()
        out = self.tmp / "no_ffmpeg.mp4"
        saved = fdvideo.imageio_ffmpeg
        fdvideo.imageio_ffmpeg = None
        try:
            with self.assertRaises(fdvideo.VideoError) as ctx:
                fdvideo.render_video(doc, out=str(out), resolution=32, duration_s=0.25, fps=4)
        finally:
            fdvideo.imageio_ffmpeg = saved
        self.assertEqual(ctx.exception.code, "VD_E_NO_FFMPEG")
        self.assertTrue(ctx.exception.hints)
        self.assertIn("imageio-ffmpeg", ctx.exception.hints[0])
        self.assertFalse(out.exists())

    def test_invalid_arguments(self):
        doc = synthetic_doc()
        out = str(self.tmp / "bad.mp4")
        cases = [
            dict(fps=0),
            dict(fps=1000),
            dict(duration_s=0),
            dict(duration_s=-1),
            dict(quality="ultra"),
            dict(resolution=1),
            dict(max_frames=0),
            dict(codec=""),
            dict(background=[1, 2]),
            dict(background=[1, 2, 999]),
        ]
        for extra in cases:
            kwargs = dict(out=out, resolution=48, duration_s=0.25, fps=8)
            kwargs.update(extra)
            with self.subTest(**extra), self.assertRaises(fdvideo.VideoError) as ctx:
                fdvideo.render_video(doc, **kwargs)
            self.assertEqual(ctx.exception.code, "VD_E_ARG")

    def test_missing_input_and_bad_doc(self):
        with self.assertRaises(fdvideo.VideoError) as ctx:
            fdvideo.render_video(str(self.tmp / "nope.fdraw"), out=str(self.tmp / "x.mp4"))
        self.assertEqual(ctx.exception.code, "VD_E_INPUT")
        with self.assertRaises(fdvideo.VideoError) as ctx:
            fdvideo.render_video(1234, out=str(self.tmp / "x.mp4"))
        self.assertEqual(ctx.exception.code, "VD_E_ARG")

    def test_two_runs_share_frame_hashes(self):
        doc = multi_doc(5)
        first = self._video(doc, "det_a.mp4", duration_s=0.625, fps=8)
        second = self._video(doc, "det_b.mp4", duration_s=0.625, fps=8)
        self.assertEqual(first["frames"], 5)
        self.assertEqual(first["frame_hashes"], second["frame_hashes"])
        self.assertEqual(first["frame_hashes_sampled"], second["frame_hashes_sampled"])
        self.assertEqual(first["frame_hashes_sha256"], second["frame_hashes_sha256"])
        for index, digest in first["frame_hashes_sampled"].items():
            self.assertEqual(digest, first["frame_hashes"][int(index)])

    def test_odd_size_padded_to_even_for_yuv420p(self):
        doc = synthetic_doc(strokes=[circle_stroke(0)])
        doc["canvas"] = {
            "width": 0.9, "height": 1.0, "long_side_units": 1.0,
            "origin": "top-left", "y_axis": "down",
        }
        result = self._video(doc, "odd.mp4", resolution=33, duration_s=0.25, fps=8)
        self.assertEqual(result["width"] % 2, 0)
        self.assertEqual(result["height"] % 2, 0)
        self.assertEqual((result["width"], result["height"]), (30, 34))
        decoded = decode_frames(result["out"])
        self.assertEqual(decoded["size"], [30, 34])
        self.assertEqual(decoded["first_frame_bytes"], 30 * 34 * 3)
        self.assertEqual(result["probe"]["size"], [30, 34])

    def test_probe_video(self):
        result = self._video(multi_doc(3), "probe.mp4", duration_s=0.5, fps=8)
        probe = fdvideo.probe_video(result["out"])
        self.assertEqual(probe["size"], [48, 48])
        self.assertEqual(probe["frames"], 4)
        self.assertAlmostEqual(probe["fps"], 8.0)
        with self.assertRaises(fdvideo.VideoError):
            fdvideo.probe_video(str(self.tmp / "missing.mp4"))


# ---------------------------------------------------------------------------
# frames (PNG sampling)
# ---------------------------------------------------------------------------
class RenderFramesTests(_TmpCase):
    def test_frames_png_and_repeatability(self):
        doc = multi_doc(6)
        first = fdvideo.render_frames(
            doc, out_dir=str(self.tmp / "frames_a"), count=3, resolution=48,
            duration_s=0.75, fps=8,
        )
        second = fdvideo.render_frames(
            doc, out_dir=str(self.tmp / "frames_b"), count=3, resolution=48,
            duration_s=0.75, fps=8,
        )
        self.assertEqual(first["count"], 3)
        self.assertEqual(first["frames"], 6)
        self.assertEqual([entry["frame"] for entry in first["files"]], [0, 2, 5])
        self.assertEqual(
            [entry["elements"] for entry in first["files"]], [1, 3, 6]
        )
        self.assertEqual(
            [entry["sha256"] for entry in first["files"]],
            [entry["sha256"] for entry in second["files"]],
        )
        for entry in first["files"]:
            path = Path(entry["path"])
            self.assertTrue(path.is_file())
            self.assertEqual(entry["size"], [48, 48])
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class VideoCLITests(_TmpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = multi_doc(6)
        cls.fdraw = cls.tmp / "multi.fdraw"
        fdraw.pack(cls.doc, out=str(cls.fdraw))
        cls.fbs = cls.tmp / "multi.fbs"
        cls.fbs.write_text(json.dumps(cls.doc), encoding="utf-8")

    def test_video_json_success(self):
        out = self.tmp / "cli.mp4"
        code, stdout, stderr = run_cli([
            "video", "--fdraw", str(self.fdraw), "--out", str(out),
            "--resolution", "48", "--duration", "0.5", "--fps", "8", "--json",
        ])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["frames"], 4)
        self.assertEqual(payload["width"], 48)
        self.assertEqual(payload["height"], 48)
        self.assertEqual(payload["source"], f"fdraw:{self.fdraw}")
        self.assertEqual(payload["quality_used"], ["fast"])
        self.assertEqual(payload["fallbacks"], [])
        self.assertEqual(payload["probe"]["frames"], 4)
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 0)
        decoded = decode_frames(out)
        self.assertEqual(decoded["frames"], 4)
        self.assertEqual(decoded["size"], [48, 48])
        self.assertEqual(payload["sha256"], fdvideo._file_sha256(str(out)))

    def test_video_text_output(self):
        out = self.tmp / "cli_text.mp4"
        code, stdout, stderr = run_cli([
            "video", "--fbs", str(self.fbs), "--out", str(out),
            "--resolution", "32", "--duration", "0.25", "--fps", "8",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertIn("video ", stdout)
        self.assertIn("probe:", stdout)

    def test_cli_usage_errors(self):
        code, _stdout, _stderr = run_cli([])
        self.assertEqual(code, 2)
        code, _stdout, _stderr = run_cli(["video", "--out", "x.mp4"])
        self.assertEqual(code, 2)
        code, _stdout, stderr = run_cli([
            "video", "--fdraw", str(self.fdraw), "--out", str(self.tmp / "x.mp4"),
            "--quality", "ultra",
        ])
        self.assertEqual(code, 2)
        self.assertIn("quality", stderr)

    def test_cli_argument_validation_and_missing_input(self):
        out = self.tmp / "bad.mp4"
        code, _stdout, stderr = run_cli([
            "video", "--fdraw", str(self.fdraw), "--out", str(out), "--fps", "0",
        ])
        self.assertEqual(code, 2)
        self.assertIn("VD_E_ARG", stderr)
        self.assertFalse(out.exists())

        code, _stdout, stderr = run_cli([
            "video", "--fdraw", str(self.tmp / "missing.fdraw"), "--out", str(out),
        ])
        self.assertEqual(code, 1)
        self.assertIn("VD_E_INPUT", stderr)

    def test_cli_no_ffmpeg_exit_code(self):
        saved = fdvideo.imageio_ffmpeg
        fdvideo.imageio_ffmpeg = None
        try:
            code, _stdout, stderr = run_cli([
                "video", "--fdraw", str(self.fdraw), "--out", str(self.tmp / "n.mp4"),
                "--resolution", "32", "--duration", "0.25", "--fps", "4",
            ])
        finally:
            fdvideo.imageio_ffmpeg = saved
        self.assertEqual(code, 1)
        self.assertIn("VD_E_NO_FFMPEG", stderr)
        self.assertIn("hint:", stderr)

    def test_cli_frames(self):
        out_dir = self.tmp / "cli_frames"
        code, stdout, stderr = run_cli([
            "frames", "--fdraw", str(self.fdraw), "--out-dir", str(out_dir),
            "--count", "3", "--resolution", "48", "--duration", "0.75", "--fps", "8",
            "--json",
        ])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["elements"], 6)
        self.assertEqual([entry["frame"] for entry in payload["files"]], [0, 2, 5])
        self.assertEqual(len(list(out_dir.glob("frame_*.png"))), 3)


# ---------------------------------------------------------------------------
# end-to-end DoD: 01_rose short video (R256, 1s, 12fps)
# ---------------------------------------------------------------------------
class RoseVideoTests(_TmpCase):
    def test_rose_short_video(self):
        if not ROSE_FDRAW.is_file():  # pragma: no cover - sample missing
            self.skipTest(f"sample missing: {ROSE_FDRAW}")
        out = self.tmp / "01_rose.mp4"
        result = fdvideo.render_video_from_fdraw(
            ROSE_FDRAW,
            out=str(out),
            resolution=256,
            duration_s=1.0,
            fps=12,
            quality="fast",
            max_frames=600,
        )
        self.assertEqual(result["frames"], 12)
        self.assertEqual((result["width"], result["height"]), (256, 256))
        self.assertAlmostEqual(result["duration_s"], 1.0)
        self.assertEqual(result["elements"], 51)
        self.assertEqual(result["fallbacks"], [])
        self.assertEqual(result["quality_used"], ["fast"])
        self.assertLessEqual(result["unique_renders"], 12)
        self.assertTrue(result["source"].endswith("01.fdraw"))
        decoded = decode_frames(out)
        self.assertEqual(decoded["frames"], 12)
        self.assertEqual(decoded["size"], [256, 256])
        self.assertAlmostEqual(decoded["fps"], 12.0)
        self.assertEqual(decoded["first_frame_bytes"], 256 * 256 * 3)
        self.assertEqual(result["probe"]["frames"], 12)
        self.assertEqual(result["probe"]["size"], [256, 256])
        self.assertEqual(len(result["frame_hashes"]), 12)
        # the reveal is progressive: the last frame is not the first frame
        self.assertNotEqual(result["frame_hashes"][0], result["frame_hashes"][-1])

    def test_rose_video_is_deterministic(self):
        if not ROSE_FDRAW.is_file():  # pragma: no cover - sample missing
            self.skipTest(f"sample missing: {ROSE_FDRAW}")
        kwargs = dict(
            out=str(self.tmp / "rose_det.mp4"),
            resolution=128,
            duration_s=0.5,
            fps=8,
        )
        first = fdvideo.render_video_from_fdraw(ROSE_FDRAW, **kwargs)
        kwargs["out"] = str(self.tmp / "rose_det2.mp4")
        second = fdvideo.render_video_from_fdraw(ROSE_FDRAW, **kwargs)
        self.assertEqual(first["frame_hashes"], second["frame_hashes"])
        self.assertEqual(first["frame_hashes_sha256"], second["frame_hashes_sha256"])
        self.assertEqual(first["bytes"] > 0, True)


if __name__ == "__main__":
    unittest.main()
