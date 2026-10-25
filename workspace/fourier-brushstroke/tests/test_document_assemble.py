"""WP-B：文档组装公共 API 的单元测试（`assemble_document` / `stroke_record` / `StyleTable`）。

覆盖（对应 KICKOFF v1 §2.2 的 DoD）：
- `StyleTable` 精确去重与首见顺序（style id 稳定）；
- `stroke_record` 与原 `_stroke_record` 字段一致 + `bbox` / `layer` / `region_id` 注入
  （FDSL 的 gen 元素没有 `element["points"]`）；旧私有名兼容；
- `assemble_document` 的 `record_ids` / `fft_passes` / `fill_soft_px` / `method` /
  `catch_brush_errors` 语义与 warnings 顺序；
- `build_from_image` 只调用一次 `assemble_document`，且 auto fill_soft/method 走原分支。
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from fbs_core import document, io_format, render  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import FBSError, WarningLog, sha256_json  # noqa: E402

TESTCASES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "testcases")


def _fit_stub(arc_length=0.5):
    return {
        "coef": [[0, 0.0, 0.0]],
        "closure": {"kind": "hermite_c1", "t_end": 0.5},
        "closed": False,
        "N": 0,
        "arc_length": float(arc_length),
        "max_err": 0.01,
    }


def _brush_stub():
    style = {
        "color": {"interp": "constant", "value": [0.1, 0.1, 0.1]},
        "alpha": {"interp": "constant", "value": 1.0},
        "shape": {"interp": "constant", "value": "round"},
    }
    return style, {"width": {"interp": "constant", "value": 2.0}}


def _element(points=((0.1, 0.1), (0.5, 0.5))):
    return {"points": np.asarray(points, dtype=np.float64), "fill_role": "medial_branch",
            "layer": 1, "region_id": 2, "closed": False}


def _assemble_kwargs():
    return {
        "cfg": FBSConfig(target_resolution=256),
        "warn": WarningLog(),
        "source": {"sha256": "deadbeef"},
        "image_class": {"klass": "lineart", "evidence": {}},
        "canvas": {"width": 1.0, "height": 1.0, "long_side_units": 1.0,
                   "origin": "top-left", "y_axis": "down"},
        "decomposition": {"method": "hybrid_medial_contour", "params": {}, "region_count": 0},
        "base_color": [255, 255, 255, 255],
        "contract": FBSConfig(target_resolution=256).contract(),
        "accelerators": {},
        "regions": [],
    }


class TestStyleTable(unittest.TestCase):
    def test_exact_dedup_keeps_first_seen_order(self):
        table = document.StyleTable()
        a = {"color": {"interp": "constant", "value": [0.1, 0.2, 0.3]}}
        b = {"color": {"interp": "constant", "value": [0.4, 0.5, 0.6]}}
        self.assertEqual(table.add(a), 0)
        self.assertEqual(table.add(b), 1)
        self.assertEqual(table.add(json.loads(json.dumps(a))), 0)
        self.assertEqual(len(table), 2)
        self.assertEqual(table.styles, [a, b])

    def test_near_equal_styles_do_not_merge(self):
        table = document.StyleTable()
        table.add({"alpha": {"interp": "constant", "value": 0.5}})
        table.add({"alpha": {"interp": "constant", "value": 0.50001}})
        self.assertEqual(len(table), 2)


class TestStrokeRecord(unittest.TestCase):
    def test_matches_legacy_fields_and_alias(self):
        self.assertIs(document._stroke_record, document.stroke_record)
        el = _element()
        rec = document.stroke_record(3, el, _fit_stub(), _brush_stub()[1], FBSConfig())
        self.assertEqual(rec["id"], 3)
        self.assertEqual(rec["layer"], 1)
        self.assertEqual(rec["region_id"], 2)
        self.assertEqual(rec["bbox"], [0.1, 0.1, 0.5, 0.5])
        self.assertEqual(rec["brush"]["width"], {"interp": "constant", "value": 2.0})
        self.assertEqual(rec["closure"], {"kind": "hermite_c1", "t_end": 0.5})

    def test_bbox_injection_without_points(self):
        el = {"fill_role": "region_fill", "layer": 4, "region_id": 5}
        rec = document.stroke_record(0, el, _fit_stub(1.0), _brush_stub()[1],
                                     bbox=[0.0, 0.25, 0.75, 1.0])
        self.assertEqual(rec["bbox"], [0.0, 0.25, 0.75, 1.0])

    def test_layer_region_override(self):
        rec = document.stroke_record(0, _element(), _fit_stub(), _brush_stub()[1],
                                     layer=7, region_id=9)
        self.assertEqual(rec["layer"], 7)
        self.assertEqual(rec["region_id"], 9)

    def test_poly_and_holes(self):
        el = _element()
        el["fill_poly"] = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
        el["fill_holes"] = [[[0.2, 0.2], [0.4, 0.2], [0.4, 0.4]], [[0.0, 0.0]]]
        rec = document.stroke_record(0, el, _fit_stub(), _brush_stub()[1])
        self.assertEqual(rec["poly"], [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]])
        self.assertEqual(len(rec["fill_holes"]), 1)  # 少于 3 点的洞环被丢弃（=无洞）


class TestAssembleDocument(unittest.TestCase):
    def _run(self, elements, **overrides):
        kwargs = _assemble_kwargs()
        kwargs.update(overrides)
        with mock.patch.object(document.fourier_mod, "fit_curve",
                               side_effect=lambda *a, **k: _fit_stub()), \
                mock.patch.object(document.brush_mod, "build_brush",
                                  side_effect=lambda *a, **k: _brush_stub()):
            return document.assemble_document(elements, **kwargs)

    def test_defaults_and_stats(self):
        doc, info = self._run([_element(), _element()])
        self.assertEqual([s["id"] for s in doc["strokes"]], [0, 1])
        self.assertEqual(len(doc["brush_styles"]), 1)
        self.assertEqual(doc["stats"]["stroke_count"], 2)
        self.assertEqual(doc["stats"]["fft_passes"], 2)
        self.assertEqual(info["strokes"], 2)
        self.assertEqual(info["styles"], 1)
        self.assertEqual(info["skipped"], [])
        self.assertEqual(io_format.validate(doc), [])

    def test_explicit_fft_passes_fill_soft_and_method(self):
        doc, info = self._run([_element()], fft_passes=0, fill_soft_px=1.5,
                             method="plan_brush_strokes")
        self.assertEqual(doc["stats"]["fft_passes"], 0)
        self.assertEqual(info["fft_passes"], 0)
        self.assertEqual(doc["decomposition"]["params"]["fill_soft_px"], 1.5)
        self.assertEqual(doc["decomposition"]["method"], "plan_brush_strokes")

    def test_none_overrides_keep_document_values(self):
        doc, _info = self._run([_element()])
        self.assertEqual(doc["decomposition"]["method"], "hybrid_medial_contour")
        self.assertNotIn("fill_soft_px", doc["decomposition"]["params"])

    def test_sequential_ids_and_skip_warning_order(self):
        elements = [_element(), _element(), _element()]
        fit_calls = {"n": 0}

        def fit_side_effect(*args, **kwargs):
            fit_calls["n"] += 1
            if fit_calls["n"] == 2:
                raise FBSError("E_GEOMETRY_DEGENERATE", "退化")
            return _fit_stub()

        kwargs = _assemble_kwargs()
        with mock.patch.object(document.fourier_mod, "fit_curve", side_effect=fit_side_effect), \
                mock.patch.object(document.brush_mod, "build_brush",
                                  side_effect=lambda *a, **k: _brush_stub()):
            doc, info = document.assemble_document(elements, record_ids="sequential", **kwargs)
        self.assertEqual([s["id"] for s in doc["strokes"]], [0, 1])
        self.assertEqual([w["code"] for w in doc["warnings"]], ["W_GEOMETRY_SKIP"])
        self.assertEqual(doc["warnings"][0]["severity"], "degrade")
        self.assertEqual(info["skipped"], [{"index": 1, "name": "medial_branch#1",
                                            "code": "W_GEOMETRY_SKIP", "stage": "fit"}])

    def test_brush_errors_bubble_by_default(self):
        def brush_raise(*_a, **_k):
            raise FBSError("E_BRUSH", "笔刷失败")

        kwargs = _assemble_kwargs()
        with mock.patch.object(document.fourier_mod, "fit_curve",
                               side_effect=lambda *a, **k: _fit_stub()), \
                mock.patch.object(document.brush_mod, "build_brush", side_effect=brush_raise):
            with self.assertRaises(FBSError):
                document.assemble_document([_element()], **kwargs)

    def test_catch_brush_errors_degrades(self):
        def brush_raise(*_a, **_k):
            raise FBSError("E_BRUSH", "笔刷失败")

        kwargs = _assemble_kwargs()
        with mock.patch.object(document.fourier_mod, "fit_curve",
                               side_effect=lambda *a, **k: _fit_stub()), \
                mock.patch.object(document.brush_mod, "build_brush", side_effect=brush_raise):
            doc, info = document.assemble_document([_element()], catch_brush_errors=True, **kwargs)
        self.assertEqual(doc["strokes"], [])
        self.assertEqual(doc["stats"]["fft_passes"], 0)
        self.assertEqual(info["skipped"][0]["stage"], "brush")
        self.assertIn("W_GEOMETRY_SKIP", [w["code"] for w in doc["warnings"]])

    def test_bad_record_ids_rejected(self):
        with self.assertRaises(FBSError) as ctx:
            self._run([_element()], record_ids="dense")
        self.assertEqual(ctx.exception.code, "E_CONFIG")


class TestBuildFromImageWiring(unittest.TestCase):
    def test_build_calls_assemble_once_with_auto_fill_soft(self):
        image = os.path.join(TESTCASES, "T1_signature.png")
        cfg = FBSConfig(target_resolution=256)
        real = document.assemble_document
        calls = []

        def wrapper(*args, **kwargs):
            calls.append(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(document, "assemble_document", side_effect=wrapper):
            doc, info = document.build_from_image(image, cfg, WarningLog())

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["fill_soft_px"], 0.5)          # lineart auto
        self.assertEqual(call["method"], doc["decomposition"]["method"])
        self.assertNotIn("fft_passes", call)                 # None → len(strokes)
        self.assertNotIn("record_ids", call)                 # 默认 index（build 行为）
        self.assertEqual(doc["stats"]["fft_passes"], len(doc["strokes"]))
        self.assertEqual(io_format.validate(doc), [])

        png = render.render(doc, resolution=256, supersample=cfg.supersample, mode=cfg.render_mode)
        self.assertEqual(tuple(png.shape), (256, 256, 3))


class TestBaselineEquivalence(unittest.TestCase):
    """build 输出与冻结基线一致（与 tests/_baseline/freeze_baseline.py 同口径）。"""

    def test_t1_doc_png_bytes_match_frozen_baseline(self):
        base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "_baseline", "baseline.json")
        self.assertTrue(os.path.exists(base_path), f"缺少冻结基线: {base_path}")
        with open(base_path, encoding="utf-8") as fh:
            baseline = json.load(fh)
        entry = next(r for r in baseline["cases"] if r["name"] == "T1_signature@R256")

        path = os.path.join(TESTCASES, "T1_signature.png")
        cfg = FBSConfig(target_resolution=256)
        doc, _info = document.build_from_image(path, cfg, WarningLog())
        normalized = json.loads(json.dumps(doc, ensure_ascii=False, default=str))
        normalized.pop("created", None)
        normalized["source"].pop("path", None)
        self.assertEqual(sha256_json(normalized), entry["doc_sha256"])

        png = render.render(doc, resolution=256, supersample=cfg.supersample, mode=cfg.render_mode)
        self.assertEqual(hashlib.sha256(np.ascontiguousarray(png).tobytes()).hexdigest(),
                         entry["png_sha256"])
        with tempfile.TemporaryDirectory(prefix="fbs_unit_") as outdir:
            written = io_format.write_fbs(doc, os.path.join(outdir, "t1.fbs.json"))
        self.assertEqual(written["bytes"], entry["fbs_bytes"])


if __name__ == "__main__":
    unittest.main()
