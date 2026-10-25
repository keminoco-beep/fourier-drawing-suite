"""video.py 单元测试：3 秒短片 smoke（帧数/时长/尺寸/文件非空）、帧预算/提升 fps、
增量作画（末帧墨量 > 首帧）、probe_video 校验、错误码。
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fbs_core import video as video_mod  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.onestroke import build_one_stroke  # noqa: E402
from fbs_core.util import FBSError, WarningLog  # noqa: E402

from test_onestroke import tiny_doc  # noqa: E402

ASSETS_OUT = os.path.join(os.path.dirname(HERE), "assets", "out")


def _one_doc(R: int = 256, harmonics: int = 1024, max_fft: int = 1 << 14) -> dict:
    cfg = FBSConfig(target_resolution=R, harmonics=harmonics, max_fft=max_fft)
    return build_one_stroke(tiny_doc(R=R), cfg, WarningLog())


def _out(name: str) -> str:
    os.makedirs(ASSETS_OUT, exist_ok=True)
    return os.path.join(ASSETS_OUT, name)


class TestLayout(unittest.TestCase):
    """默认尺寸与作画区自适应（竖版 720×1080 / 横图 1080×720）。"""

    def test_pick_size_defaults(self):
        self.assertEqual(video_mod._pick_size((0.7, 1.0), None), (720, 1080))
        self.assertEqual(video_mod._pick_size((1.0, 0.7), None), (1080, 720))
        self.assertEqual(video_mod._pick_size((1.0, 0.9786), None), (720, 1080))  # 近方形仍竖版
        self.assertEqual(video_mod._pick_size((1.0, 1.0), (320, 480)), (320, 480))

    def test_layout_fits_art_box(self):
        layout = video_mod._layout((0.7093, 1.0), 720, 1080)
        self.assertLessEqual(layout["art_w"], 720 - 2 * layout["margin_x"])
        self.assertLessEqual(layout["art_h"], 1080 - layout["header"] - layout["footer"])
        self.assertGreater(layout["R_art"], 100)
        self.assertGreaterEqual(layout["x0"], 0)
        self.assertGreaterEqual(layout["y0"], 0)


class TestFrameBudget(unittest.TestCase):
    """帧预算：每步至少 min_frames；不足时提高 fps 并 warn（R3 不省略步骤）。"""

    def test_enough_frames_keeps_fps(self):
        warn = WarningLog()
        n, fps = video_mod._frame_budget(3.0, 30, 10, 1, warn)
        self.assertEqual(n, 90)
        self.assertEqual(fps, 30)
        self.assertEqual(warn.to_list(), [])

    def test_raises_fps_when_steps_too_many(self):
        warn = WarningLog()
        n, fps = video_mod._frame_budget(1.0, 10, 200, 1, warn)
        self.assertGreaterEqual(n, 201)
        self.assertGreater(fps, 10)
        codes = {w["code"] for w in warn.to_list()}
        self.assertIn("W_VIDEO_FPS_RAISED", codes)
        self.assertLessEqual(abs(n / fps - 1.0), 0.05)

    def test_invalid_args_raise(self):
        with self.assertRaises(FBSError):
            video_mod._frame_budget(0.0, 30, 10, 1, WarningLog())
        with self.assertRaises(FBSError):
            video_mod._frame_budget(3.0, 0, 10, 1, WarningLog())


class TestMakeVideo(unittest.TestCase):
    """3 秒短片 smoke：帧数/时长/尺寸/文件非空/增量作画/probe 一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.one = _one_doc(R=256)
        cls.path = _out("render_test_video_3s.mp4")
        cls.warn = WarningLog()
        cls.info = video_mod.make_video(
            cls.one,
            cls.path,
            duration=3.0,
            fps=30,
            size=(270, 480),
            circles=24,
            mode="sweep",
            warn=cls.warn,
        )
        cls.probe = video_mod.probe_video(cls.path)

    def test_return_contract(self):
        for key in ("out", "frames", "fps", "duration_s", "size", "circles", "bytes", "timeline"):
            self.assertIn(key, self.info)
        self.assertEqual(self.info["out"], self.path)
        self.assertEqual(self.info["size"], [270, 480])
        self.assertEqual(self.info["circles"], 24)
        self.assertEqual(self.info["timeline"], "arc")
        self.assertGreater(self.info["bytes"], 1000)
        self.assertTrue(os.path.exists(self.path))

    def test_probe_matches(self):
        self.assertEqual(self.probe["frames"], self.info["frames"])
        self.assertAlmostEqual(float(self.probe["fps"]), float(self.info["fps"]), delta=0.6)
        self.assertEqual(self.probe["size"], [270, 480])
        self.assertLessEqual(abs(float(self.probe["duration_s"]) - 3.0), 0.15)
        self.assertLessEqual(abs(float(self.info["duration_s"]) - 3.0), 0.15)

    def test_frames_cover_all_steps(self):
        steps = int(self.info["steps"])
        self.assertGreaterEqual(self.info["frames"], 1 + steps * 1)
        self.assertGreaterEqual(steps, 2)

    def test_incremental_drawing(self):
        # 首帧应基本空白，末帧应有完整笔画（证明逐帧增量作画，不是一次性贴图）
        import imageio

        reader = imageio.get_reader(self.path)
        try:
            first = np.asarray(reader.get_data(0), dtype=np.float64)
            last = np.asarray(reader.get_data(self.probe["frames"] - 1), dtype=np.float64)
        finally:
            reader.close()
        art0 = first[60:400, 20:250]
        art1 = last[60:400, 20:250]
        ink0 = float(np.mean(art0 < 200.0))
        ink1 = float(np.mean(art1 < 200.0))
        self.assertGreater(ink1, ink0 + 0.01, f"末帧墨量 {ink1:.3f} 应明显大于首帧 {ink0:.3f}")

    def test_unknown_mode_raises(self):
        with self.assertRaises(FBSError) as ctx:
            video_mod.make_video(_one_doc(R=64), _out("render_test_video_bad.mp4"), duration=1.0, fps=10,
                                 size=(128, 128), circles=4, mode="bogus")
        self.assertEqual(ctx.exception.code, "E_RENDER_MODE")

    def test_rejects_per_stroke_doc(self):
        with self.assertRaises(FBSError) as ctx:
            video_mod.make_video(tiny_doc(R=128), _out("render_test_video_bad2.mp4"), duration=1.0, fps=10,
                                 size=(128, 128), circles=4)
        self.assertEqual(ctx.exception.code, "E_INPUT_MODE")


class TestMultiStepVideo(unittest.TestCase):
    """步骤数 > 目标帧数时必须提高 fps，且时长保持 ±5%。"""

    def test_many_steps_raise_fps(self):
        one = _one_doc(R=64, harmonics=256, max_fft=1 << 12)
        # 人为造 120 个步骤（模拟 1014 笔画路径的 step_spans 规模）
        one["strokes"][0]["step_spans"] = [[i / 120.0, (i + 1) / 120.0, "stroke"] for i in range(120)]
        path = _out("render_test_video_steps.mp4")
        warn = WarningLog()
        info = video_mod.make_video(one, path, duration=1.0, fps=10, size=(128, 160), circles=8, warn=warn)
        codes = {w["code"] for w in warn.to_list()}
        self.assertIn("W_VIDEO_FPS_RAISED", codes)
        self.assertGreater(info["fps"], 10)
        self.assertGreaterEqual(info["frames"], 121)
        probe = video_mod.probe_video(path)
        self.assertLessEqual(abs(float(probe["duration_s"]) - 1.0), 0.05)



class TestVideoHoles(unittest.TestCase):
    """带洞填充的一笔画视频：末帧洞=文档底色、填充环=填充色（与静态一笔画渲染一致）。"""

    @classmethod
    def setUpClass(cls) -> None:
        from test_render import _hole_fill_doc

        doc = _hole_fill_doc(256, holes=[[[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]])
        cfg = FBSConfig(target_resolution=256, harmonics=1024, max_fft=1 << 14)
        cls.one = build_one_stroke(doc, cfg, WarningLog())
        cls.size = (192, 160)
        cls.path = _out("render_test_video_holes.mp4")
        cls.info = video_mod.make_video(
            cls.one, cls.path, duration=1.0, fps=20, size=cls.size, circles=0, mode="sweep", warn=WarningLog()
        )
        cls.layout = video_mod._layout((1.0, 1.0), *cls.size)

    def test_probe_and_contract(self):
        probe = video_mod.probe_video(self.path)
        self.assertEqual(probe["frames"], self.info["frames"])
        self.assertEqual(probe["size"], list(self.size))
        self.assertLessEqual(abs(float(probe["duration_s"]) - 1.0), 0.05)

    def test_last_frame_hole_is_base_color(self):
        import imageio

        reader = imageio.get_reader(self.path)
        try:
            frame = np.asarray(reader.get_data(int(self.info["frames"]) - 1), dtype=np.float64)
        finally:
            reader.close()
        x0, y0, R_art = self.layout["x0"], self.layout["y0"], self.layout["R_art"]
        cy, cx = y0 + int(0.5 * R_art), x0 + int(0.5 * R_art)
        ry, rx = y0 + int(0.5 * R_art), x0 + int(0.3 * R_art)
        self.assertTrue(np.allclose(frame[cy, cx], [255, 255, 255], atol=6),
                        f"末帧洞中心应为背景色，实测 {frame[cy, cx]}")
        self.assertGreater(frame[ry, rx][0], 170.0, f"末帧填充环应为填充色，实测 {frame[ry, rx]}")
        self.assertLess(frame[ry, rx][1], 90.0)


class TestProbeErrors(unittest.TestCase):
    """probe_video 对缺失/空文件必须显式报错。"""

    def test_missing_file(self):
        with self.assertRaises(FBSError) as ctx:
            video_mod.probe_video(_out("render_test_not_exist.mp4"))
        self.assertEqual(ctx.exception.code, "E_INPUT_MISSING")

    def test_empty_file(self):
        p = _out("render_test_empty.mp4")
        with open(p, "wb") as fh:
            fh.write(b"")
        try:
            with self.assertRaises(FBSError) as ctx:
                video_mod.probe_video(p)
            self.assertEqual(ctx.exception.code, "E_VIDEO_ENCODE")
        finally:
            os.remove(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
