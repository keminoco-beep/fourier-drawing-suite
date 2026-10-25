"""结构笔触 → v2 `.fbs` 文档（复用 Fourier 拟合 / 笔刷属性 / 渲染契约）。

与 `structpaint.py` 分工：structpaint 负责"结构分析 + 策略实验 + 生成笔触"，
本模块只负责把像素坐标笔触转成可渲染文档（世界坐标、逐点宽度/颜色/alpha/笔形）。
"""
from __future__ import annotations

import numpy as np

from . import brushlib
from .config import FBSConfig
from .util import WarningLog


def strokes_to_elements(strokes: list[dict], L: int) -> list[dict]:
    out: list[dict] = []
    for i, st in enumerate(strokes):
        pts_px = np.asarray(st["points_px"], dtype=np.float64)
        n = len(pts_px)
        if n < 2:
            continue
        pts_w = np.stack(
            [np.clip(pts_px[:, 0], 0.0, L) / L, np.clip(pts_px[:, 1], 0.0, L) / L], axis=1
        )
        widths_px = np.asarray(st["widths_px"], dtype=np.float64).reshape(-1)
        if len(widths_px) == 1:
            widths_px = np.full(n, float(widths_px[0]))
        widths_px = np.maximum(0.35, np.interp(np.linspace(0, len(widths_px) - 1, n),
                                              np.arange(len(widths_px)), widths_px))
        widths_w = widths_px / float(L)
        col = np.clip(np.asarray(st["color"], dtype=np.float64).reshape(3), 0.0, 1.0)
        if st.get("colors") is not None:
            colors = np.asarray(st["colors"], dtype=np.float64).reshape(-1, 3)
            if len(colors) != n:
                # resample along normalized path length
                grid = np.linspace(0.0, 1.0, len(colors))
                tgt = np.linspace(0.0, 1.0, n)
                colors = np.stack([np.interp(tgt, grid, colors[:, ch]) for ch in range(3)], axis=1)
            colors = np.clip(colors, 0.0, 1.0).astype(np.float32)
        else:
            colors = np.tile(col.astype(np.float32), (n, 1))
        shape = str(st.get("shape") or brushlib.BRUSHES[str(st["preset"])]["shape"])
        out.append(
            {
                "points": pts_w,
                "s": np.linspace(0.0, 1.0, n),
                "closed": False,
                "fill_role": "medial_branch",
                "widths": widths_w,
                "colors": colors,
                "gradient": st.get("gradient") or st.get("color_gradient"),
                "alphas": np.full(n, float(st["alpha"]), dtype=np.float64),
                "shapes": np.array([shape] * n, dtype=object),
                "fill_poly": None,
                "fill_holes": None,
                "region_id": -1,
                "layer": int(st.get("layer", 0)),
                "meta": {
                    "max_halfwidth_px": float(widths_px.max()),
                    "sprawl": None,
                    "dt_peak_px": None,
                    "source": str(st.get("source", "structure")),
                    "preset": str(st["preset"]),
                    "structure_class": str(st.get("class", "")),
                    "order": int(i),
                },
            }
        )
    return out


def build_doc_from_strokes(
    strokes: list[dict],
    img: dict,
    image_path: str,
    cfg: FBSConfig,
    warn: WarningLog,
    method: str = "structure_brush_painting",
) -> dict:
    from . import brush as brush_mod
    from . import document as document_mod
    from . import fourier as fourier_mod
    from . import io_format

    L = int(img["long_side"])
    elements = strokes_to_elements(strokes, L)
    contract = cfg.contract()
    styles: list[dict] = []
    style_index: dict[str, int] = {}
    records: list[dict] = []
    for idx, el in enumerate(elements):
        fit = fourier_mod.fit_curve(el["points"], False, contract, cfg, warn, name=f"stroke#{idx}")
        style, brush = brush_mod.build_brush(el, contract, cfg, warn)
        key = document_mod._style_key(style)
        if key not in style_index:
            style_index[key] = len(styles)
            styles.append(style)
        brush["style"] = style_index[key]
        records.append(document_mod._stroke_record(idx, el, fit, brush, cfg))
    src = document_mod._source_info(image_path, img)
    doc = io_format.new_document(
        mode=io_format.MODE_PER_STROKE,
        source=src,
        image_class={"klass": "illustration", "evidence": {}},
        canvas=document_mod._canvas(img),
        render_contract=contract,
        decomposition={"method": method, "params": cfg.to_dict(), "region_count": 0},
        base_color=[255, 255, 255, 255],
        warnings=warn,
        accelerators={},
    )
    doc["brush_styles"] = styles
    doc["strokes"] = records
    doc["stats"]["fft_passes"] = len(records)
    io_format.finalize_document(doc, image_sha256=src.get("sha256", ""))
    return doc
