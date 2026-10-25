"""Stage0 预处理 + 分类的单元测试（stdlib unittest，无第三方测试框架）。

覆盖：模式归一化（RGB/RGBA/L/LA/P/PA/CMYK/YCbCr/I;16/F/1）、alpha 合成、
像素预算降采样、最小尺寸/损坏/缺失/全透明错误、中文路径、GIF 多帧、
照片拒绝（D5）与"平滑渐变不得判成照片"、lineart/flat_art/illustration 分类。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from fbs_core import preprocess  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import FBSError, WarningLog  # noqa: E402

# 临时文件必须落在工作区内（DSH file sandbox 只允许写 workspace）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 注意：不能用 tempfile.mkdtemp() 建临时目录（沙箱下新目录不可写），
# 统一用 os.makedirs 建一个固定目录，文件名加唯一后缀。
_TMP = os.path.join(_ROOT, "tests", "_tmp")
os.makedirs(_TMP, exist_ok=True)
atexit.register(shutil.rmtree, _TMP, True)
_seq = 0


def _save(img: Image.Image, name: str, mode: str | None = None) -> str:
    """把 PIL 图存到工作区内的测试临时目录并返回路径。"""
    global _seq
    _seq += 1
    stem, ext = os.path.splitext(name)
    path = os.path.join(_TMP, f"{stem}_{_seq}{ext}")
    img.save(path, format=mode)
    return path


def _rgba(h: int = 64, w: int = 64, bg=(255, 255, 255, 255)) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., :] = bg
    return arr


def _png_bytes_from(arr: np.ndarray) -> bytes:
    import io

    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


class TestLoadAndPrepare(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = FBSConfig()

    def _load(self, path: str, cfg: FBSConfig | None = None):
        warn = WarningLog()
        img = preprocess.load_and_prepare(path, cfg or self.cfg, warn)
        return img, warn

    # ---------------- 模式归一化 ----------------
    def test_rgb_and_rgba(self) -> None:
        for mode, ctor in (("RGB", lambda: Image.new("RGB", (48, 40), (10, 200, 30))),
                           ("RGBA", lambda: Image.new("RGBA", (48, 40), (10, 200, 30, 255)))):
            img, _ = self._load(_save(ctor(), f"a_{mode}.png"))
            self.assertEqual(img["rgba"].shape, (40, 48, 4))
            self.assertEqual(str(img["rgba"].dtype), "uint8")
            self.assertEqual(str(img["rgb"].dtype), "float32")
            self.assertEqual(str(img["alpha"].dtype), "float32")
            self.assertEqual(img["source_mode"], mode)
            self.assertAlmostEqual(float(img["rgb"][5, 5, 0]), 10 / 255.0, places=3)
            self.assertAlmostEqual(float(img["rgb"][5, 5, 1]), 200 / 255.0, places=3)
            self.assertEqual(img["width"], 48)
            self.assertEqual(img["height"], 40)
            self.assertEqual(img["long_side"], 48)
            self.assertAlmostEqual(img["scale"], 1.0)

    def test_palette_modes(self) -> None:
        p = Image.new("P", (36, 36))
        pal = [0, 0, 0, 255, 255, 255] + [0] * (768 - 6)
        p.putpalette(pal)
        p.paste(1, (10, 10, 26, 26))
        img, _ = self._load(_save(p, "p.png"))
        self.assertEqual(img["rgba"].shape, (36, 36, 4))
        self.assertGreater(int(img["rgb"][18, 18].min() * 255), 220)  # 中心 = 调色板 1（白）
        self.assertLess(int(img["rgb"][2, 2].max() * 255), 30)        # 角落 = 调色板 0（黑）

    def test_cmyk_and_ycbcr(self) -> None:
        cmyk = Image.new("CMYK", (40, 40), (0, 255, 255, 0))  # 红
        img, _ = self._load(_save(cmyk, "cmyk.jpg", mode="JPEG"))
        r, g, b = (img["rgb"][20, 20] * 255).round().astype(int).tolist()
        self.assertGreater(r, 200)
        self.assertLess(g, 80)
        self.assertLess(b, 80)
        ycbcr = Image.new("YCbCr", (40, 40), (220, 128, 128))
        img2, _ = self._load(_save(ycbcr, "ycc.jpg", mode="JPEG"))
        self.assertGreater(int(img2["rgb"][20, 20].min() * 255), 150)

    def test_16bit_and_float_and_bilevel(self) -> None:
        a16 = np.zeros((32, 32), dtype=np.uint16)
        a16[:, 16:] = 65535
        img, _ = self._load(_save(Image.fromarray(a16, mode="I;16"), "i16.png"))
        self.assertLess(int(img["rgb"][16, 2].max() * 255), 20)
        self.assertGreater(int(img["rgb"][16, 28].min() * 255), 235)
        self.assertEqual(img["meta"].get("bit_depth"), 16)

        f = np.full((32, 32), 0.5, dtype=np.float32)
        img2, _ = self._load(_save(Image.fromarray(f, mode="F"), "f.tif"))
        self.assertTrue(120 <= int(img2["rgb"][16, 16, 0] * 255) <= 135)

        bilevel = Image.new("1", (32, 32), 1)
        img3, _ = self._load(_save(bilevel, "one.png"))
        self.assertGreater(int(img3["rgb"][5, 5].min() * 255), 250)

    def test_la_and_alpha_composite(self) -> None:
        la = Image.new("LA", (32, 32), (0, 255))
        img, _ = self._load(_save(la, "la.png"))
        self.assertLess(int(img["rgb"][16, 16].max() * 255), 10)
        self.assertAlmostEqual(float(img["alpha"][16, 16]), 1.0, places=3)

        # 半透明黑线 + 透明背景 → preprocess 已合成为白底
        arr = np.zeros((32, 32, 4), dtype=np.uint8)
        arr[16, :, 3] = 160
        img2, _ = self._load(_save(Image.fromarray(arr, mode="RGBA"), "semi.png"))
        self.assertAlmostEqual(float(img2["alpha"][16, 5]), 160 / 255.0, places=3)
        # 合成值 ≈ 0.627*0 + 0.373*255 ≈ 95
        self.assertTrue(88 <= int(img2["rgb"][16, 5, 0] * 255) <= 102)
        self.assertGreater(int(img2["rgb"][5, 5, 0] * 255), 250)  # 透明背景 → 白

    def test_max_pixels_downscale(self) -> None:
        arr = _rgba(200, 200)
        arr[::4, :, :3] = 0
        cfg = FBSConfig(max_pixels=10_000)
        img, warn = self._load(_save(Image.fromarray(arr), "big.png"), cfg)
        self.assertLessEqual(img["width"] * img["height"], 12_000)
        self.assertAlmostEqual(img["scale"], 0.5, places=2)
        self.assertTrue(any(w["code"] == "W_DOWNSCALED" for w in warn.to_list()))

    def test_chinese_path(self) -> None:
        arr = _rgba(48, 48)
        arr[20:28, 20:28, :3] = 0
        img, _ = self._load(_save(Image.fromarray(arr), "中文名称测试.png"))
        self.assertEqual((img["width"], img["height"]), (48, 48))

    def test_multiframe_gif_first_frame(self) -> None:
        frames = [Image.new("RGB", (40, 40), c) for c in ((255, 0, 0), (0, 255, 0))]
        path = _save(frames[0], "anim.gif", mode="GIF")
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=50)
        img, warn = self._load(path)
        self.assertEqual(img["multiframe"], 2)
        self.assertEqual((img["width"], img["height"]), (40, 40))
        self.assertTrue(any(w["code"] == "W_MULTIFRAME" for w in warn.to_list()))

    # ---------------- 错误路径 ----------------
    def test_missing_file(self) -> None:
        with self.assertRaises(FBSError) as ctx:
            preprocess.load_and_prepare(os.path.join(tempfile.gettempdir(), "不存在_x.png"), self.cfg, WarningLog())
        self.assertEqual(ctx.exception.code, "E_INPUT_MISSING")

    def test_corrupt_file(self) -> None:
        path = _save(Image.fromarray(_rgba(64, 64)), "broken.png")
        data = _png_bytes_from(_rgba(64, 64))
        with open(path, "wb") as fh:
            fh.write(data[: max(24, len(data) // 3)])
        with self.assertRaises(FBSError) as ctx:
            preprocess.load_and_prepare(path, self.cfg, WarningLog())
        self.assertIn(ctx.exception.code, {"E_INPUT_CORRUPT", "E_INPUT_FORMAT"})

    def test_not_an_image(self) -> None:
        path = _save(Image.fromarray(_rgba(64, 64)), "not_image.png")
        with open(path, "wb") as fh:
            fh.write(b"this is not an image at all" * 4)
        with self.assertRaises(FBSError) as ctx:
            preprocess.load_and_prepare(path, self.cfg, WarningLog())
        self.assertEqual(ctx.exception.code, "E_INPUT_FORMAT")

    def test_too_small(self) -> None:
        img = Image.new("RGB", (16, 40), (0, 0, 0))
        with self.assertRaises(FBSError) as ctx:
            preprocess.load_and_prepare(_save(img, "small.png"), self.cfg, WarningLog())
        self.assertEqual(ctx.exception.code, "E_INPUT_TOO_SMALL")

    def test_fully_transparent(self) -> None:
        with self.assertRaises(FBSError) as ctx:
            preprocess.load_and_prepare(_save(Image.new("RGBA", (40, 40), (0, 0, 0, 0)), "t.png"),
                                        self.cfg, WarningLog())
        self.assertEqual(ctx.exception.code, "E_INPUT_TRANSPARENT")


class TestClassify(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = FBSConfig()

    def _classify(self, rgba: np.ndarray) -> dict:
        h, w = rgba.shape[:2]
        a = rgba[..., 3].astype(np.float32) / 255.0
        rgb = (rgba[..., :3].astype(np.float32) / 255.0 * a[..., None] + (1.0 - a[..., None])).astype(np.float32)
        img = {"rgba": rgba, "rgb": rgb, "alpha": a, "width": w, "height": h,
               "long_side": max(w, h), "path": "mem", "source_mode": "RGBA", "scale": 1.0, "meta": {}}
        return preprocess.classify(img, self.cfg)

    def test_photo_rejected(self) -> None:
        rng = np.random.default_rng(7)
        h = w = 192
        yy, xx = np.mgrid[0:h, 0:w] / float(max(h, w))
        base = np.zeros((h, w, 3))
        for c in range(3):
            base[..., c] = 0.5 + 0.4 * np.sin(6 * xx + 2 * yy + c)
        base = np.clip(base + rng.normal(0, 0.02, base.shape), 0, 1)
        photo = np.clip(base + rng.normal(0, 3.0 / 255.0, base.shape), 0, 1)
        rgba = np.concatenate([(photo * 255).astype(np.uint8),
                               np.full((h, w, 1), 255, np.uint8)], axis=2)
        res = self._classify(rgba)
        self.assertEqual(res["klass"], "photo")
        self.assertIn("reject", res)
        self.assertEqual(res["reject"]["code"], "E_PHOTO_DEFERRED")
        self.assertTrue(res["reject"]["suggestions"])
        ev = res["evidence"]
        self.assertGreaterEqual(ev["unique_colors"], 4096)
        self.assertGreaterEqual(ev["entropy_bits"], 6.0)
        self.assertGreaterEqual(ev["noise_level"], 1.5)

    def test_photo_experimental_opt_in(self) -> None:
        """显式开启 --allow-photo-experimental 后：照片按插画处理（默认仍拒绝，合同不变）。"""
        rng = np.random.default_rng(7)
        h = w = 192
        yy, xx = np.mgrid[0:h, 0:w] / float(max(h, w))
        base = np.zeros((h, w, 3))
        for c in range(3):
            base[..., c] = 0.5 + 0.4 * np.sin(6 * xx + 2 * yy + c)
        base = np.clip(base + rng.normal(0, 0.02, base.shape), 0, 1)
        photo = np.clip(base + rng.normal(0, 3.0 / 255.0, base.shape), 0, 1)
        rgba = np.concatenate([(photo * 255).astype(np.uint8),
                               np.full((h, w, 1), 255, np.uint8)], axis=2)
        cfg = FBSConfig(allow_photo_experimental=True)
        hh, ww = rgba.shape[:2]
        a = rgba[..., 3].astype(np.float32) / 255.0
        rgb = (rgba[..., :3].astype(np.float32) / 255.0 * a[..., None] + (1.0 - a[..., None])).astype(np.float32)
        img = {"rgba": rgba, "rgb": rgb, "alpha": a, "width": ww, "height": hh,
               "long_side": max(ww, hh), "path": "mem", "source_mode": "RGBA", "scale": 1.0, "meta": {}}
        res = preprocess.classify(img, cfg)
        self.assertEqual(res["klass"], "illustration")
        self.assertNotIn("reject", res)
        self.assertTrue(res.get("photo_experimental"))
        # 默认配置仍是拒绝：合同不变
        self.assertIn("reject", preprocess.classify(img, FBSConfig()))

    def test_smooth_gradient_is_not_photo(self) -> None:
        h = w = 192
        yy, xx = np.mgrid[0:h, 0:w] / float(max(h, w))
        grad = np.stack([xx, yy, (xx + yy) / 2.0], axis=-1)
        rgba = np.concatenate([(np.clip(grad, 0, 1) * 255).astype(np.uint8),
                               np.full((h, w, 1), 255, np.uint8)], axis=2)
        res = self._classify(rgba)
        self.assertNotEqual(res["klass"], "photo")
        self.assertNotIn("reject", res)
        self.assertEqual(res["klass"], "gradient")

    def test_lineart_and_flat_art(self) -> None:
        h = w = 128
        rgba = _rgba(h, w)
        rgba[:, ::16, :3] = 0
        rgba[::16, :, :3] = 0
        self.assertEqual(self._classify(rgba)["klass"], "lineart")

        flat = _rgba(h, w)
        flat[20:70, 20:70, :3] = (230, 80, 60)
        flat[80:110, 40:120, :3] = (40, 90, 210)
        self.assertEqual(self._classify(flat)["klass"], "flat_art")

    def test_sparse_signature_lineart(self) -> None:
        """T1 类稀疏签名：低饱和 + 极细黑线 + 白底，必须判 lineart（不能落到 flat_art）。"""
        import numpy as np
        from PIL import Image, ImageDraw

        im = Image.new("RGB", (256, 256), (255, 255, 255))
        dr = ImageDraw.Draw(im)
        # 一条平滑曲线 + 少量花体笔画（占画面比例很小，硬边像素占比 ~1%）
        dr.line([(30, 190), (70, 120), (120, 90), (170, 130), (210, 180)], fill=(0, 0, 0), width=3)
        dr.line([(90, 150), (130, 175), (170, 150)], fill=(0, 0, 0), width=2)
        arr = np.asarray(im.convert("RGBA"))
        # 轻微抗锯齿灰阶（模拟真实签名：唯一色数 >512 但饱和度为 0）
        rng = np.random.default_rng(5)
        arr = arr.astype(np.int16)
        arr[..., :3] += rng.integers(-2, 3, arr[..., :3].shape, dtype=np.int16)
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        res = self._classify(arr)
        self.assertEqual(res["klass"], "lineart", f"签名类被误判: {res['klass']} {res['evidence']}")
        self.assertLess(res["evidence"]["mean_saturation"], 0.05)

    def test_illustration_evidence_keys(self) -> None:
        h = w = 160
        rgba = _rgba(h, w)
        yy, xx = np.mgrid[0:h, 0:w] / float(max(h, w))
        for c, (fx, fy) in enumerate(((5.0, 3.0), (3.0, 7.0), (7.0, 2.0))):
            rgba[..., c] = np.clip(128 + 120 * np.sin(fx * xx + fy * yy + c), 0, 255).astype(np.uint8)
        rgba[::19, :, :3] = 0
        rgba[:, ::23, :3] = 0
        res = self._classify(rgba)
        self.assertIn(res["klass"], {"illustration", "flat_art", "lineart", "gradient"})
        for key in ("unique_colors", "entropy_bits", "edge_density", "hard_edge_frac",
                    "mean_saturation", "colorful_frac", "noise_level", "flat_frac"):
            self.assertIn(key, res["evidence"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
