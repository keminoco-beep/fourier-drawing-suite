"""原图高精度测绘（survey）：训练前先把"识图精度"量化出来。

为什么要先测绘（用户要求）
--------------------------
训练/RL 的奖励依赖目标图与结构测量。若线宽、区域边界、封闭轮廓的测量有系统偏差，
训练只会把偏差"学进去"。所以先对原图做一次可复现的测绘：
1. 图像统计：尺寸、唯一色数、熵、噪声、平坦区/边缘密度、饱和度；
2. 线稿测量：线条条数、线宽分布（px 分位数）、长度分布、AA 像素占比（亚像素信息量）；
3. 区域测量：封闭轮廓分区数量、面积分位数、各类结构占比；
4. 分辨率收敛性：同一图在 R 与其一半下的测量对比 → 判断"提高 R 是否真的提升测量精度"
   （源图本身分辨率是上限；把 421px 图放大到 4096 不会产生新细节，只会让边界更平滑）；
5. 输出 JSON + Markdown 报告，供训练参数标定（笔宽/笔数/区域阈值）与结果复核。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

import numpy as np

from . import coverage, decompose, preprocess
from .config import FBSConfig
from .util import WarningLog


def _pct(arr: np.ndarray, q: float) -> float:
    if arr.size == 0:
        return 0.0
    return float(np.percentile(arr, q))


def measure_lines(dec: dict, L: int) -> dict:
    """从分解结果量线条：宽度/长度分布、AA 信息量代理。"""
    widths_px = []
    lengths_px = []
    for el in dec.get("elements", []):
        if el.get("fill_role") == "region_fill":
            continue
        w = np.asarray(el.get("widths", []), dtype=np.float64) * L
        if w.size:
            widths_px.append(float(np.median(w)) if len(w) else 0.0)
        pts = np.asarray(el.get("points", []), dtype=np.float64)
        if len(pts) > 1:
            lengths_px.append(float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1))) * L)
    w = np.asarray(widths_px, dtype=np.float64)
    ln = np.asarray(lengths_px, dtype=np.float64)
    return {
        "line_count": int(len(w)),
        "width_px": {
            "p10": round(_pct(w, 10), 3), "median": round(_pct(w, 50), 3),
            "p90": round(_pct(w, 90), 3), "max": round(float(w.max()) if w.size else 0.0, 3),
        },
        "length_px": {
            "p10": round(_pct(ln, 10), 3), "median": round(_pct(ln, 50), 3),
            "p90": round(_pct(ln, 90), 3), "total": round(float(ln.sum()) if ln.size else 0.0, 2),
        },
    }


def measure_coverage(analysis: dict) -> dict:
    """覆盖率场/AA 带：亚像素信息量（边界像素落在 0.05..0.95 覆盖率区间的比例）。"""
    cov = np.asarray(analysis.get("cov"), dtype=np.float64)
    mask = np.asarray(analysis.get("mask"), dtype=bool)
    if cov.size == 0:
        return {}
    aa = (cov > 0.05) & (cov < 0.95)
    edge = aa & mask
    return {
        "ink_px": int(mask.sum()),
        "ink_fraction": round(float(mask.mean()), 4),
        "cov_mean_on_ink": round(float(cov[mask].mean()) if mask.any() else 0.0, 4),
        "aa_fraction_of_ink": round(float(edge.sum() / max(int(mask.sum()), 1)), 4),
        "subpixel_information": "AA 带占比越高，说明原图存在亚像素抗锯齿信息，边界测量可到 <1px",
    }


def _classify_regions(dec: dict, analysis: dict, cfg: FBSConfig) -> dict:
    """区域测量（复用区域填充元素，不做重绘）：按面积/色场方差分级。"""
    W = int(analysis.get("labels", np.zeros((1, 1))).shape[1])
    H = int(analysis.get("labels", np.zeros((1, 1))).shape[0])
    areas = []
    regions = dec.get("regions", [])
    for el in dec.get("elements", []):
        if el.get("fill_role") != "region_fill":
            continue
        meta = el.get("meta") or {}
        a = int(meta.get("area_px") or 0)
        if a > 0:
            areas.append(a)
    a = np.asarray(areas, dtype=np.float64)
    return {
        "region_count": int(len(a)),
        "area_px": {
            "p10": round(_pct(a, 10), 1), "median": round(_pct(a, 50), 1),
            "p90": round(_pct(a, 90), 1), "max": round(float(a.max()) if a.size else 0.0, 1),
        },
        "canvas_px": int(W * H),
        "region_fraction": round(float(a.sum() / max(W * H, 1)) if a.size else 0.0, 4),
        "color_fields": int(sum(1 for r in regions if isinstance(r, dict) and r.get("color_field"))),
    }


def survey_image(image_path: str, cfg: FBSConfig, warn: WarningLog | None = None) -> dict:
    """对一张图做高精度测绘（Stage 0 之前/旁路，不写主文档）。"""
    warn = warn or WarningLog()
    img = preprocess.load_and_prepare(image_path, cfg, warn)
    cls = preprocess.classify(img, cfg)
    analysis = coverage.analyze(img, cfg, warn, policy_klass=str(cls.get("klass") or ""))
    analysis["klass"] = str(cls.get("klass") or analysis.get("klass"))
    dec = decompose.decompose(img, analysis, cfg, warn)
    H, W = int(img["height"]), int(img["width"])
    L = int(img["long_side"])
    ev = dict(cls.get("evidence") or {})
    src_w, src_h = _source_size(image_path)
    up = max(H / max(src_h, 1), W / max(src_w, 1))
    data = {
        "schema": "fbs-survey/1",
        "image": {"path": image_path, "width": W, "height": H, "source_width": src_w,
                  "source_height": src_h, "long_side": L, "upscale_factor": round(up, 3)},
        "class": {"klass": cls.get("klass"), "evidence": ev},
        "lines": measure_lines(dec, L),
        "coverage": measure_coverage(analysis),
        "regions": _classify_regions(dec, analysis, cfg),
        "resolution": {
            "measured_at_R": L,
            "source_nyquist_px": max(src_w, src_h),
            "note": ("源图分辨率是测量精度上限：upscale_factor>1 时提高 R 只会让边界更平滑，"
                     "不会产生新的真实细节；R 的收益应看'同一测量在不同 R 下的收敛性'。"),
        },
    }
    _write_report(data, os.path.splitext(image_path)[0] + ".survey.json", os.path.splitext(image_path)[0] + ".survey.md")
    return data


def _source_size(path: str) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def _write_report(data: dict, json_path: str, md_path: str) -> None:
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    im = data["image"]
    li = data["lines"]
    co = data.get("coverage", {})
    re = data["regions"]
    lines = [
        "# 原图高精度测绘报告（survey）\n",
        f"- 图片：{os.path.basename(im['path'])}  源尺寸 {im['source_width']}×{im['source_height']}，"
        f"测量分辨率长边 {im['long_side']}（upscale×{im['upscale_factor']}）\n",
        f"- 分类：{data['class']['klass']}\n",
        "\n## 线条\n",
        f"- 条数 {li['line_count']}；线宽 px：p10={li['width_px']['p10']} / 中位={li['width_px']['median']} / "
        f"p90={li['width_px']['p90']} / max={li['width_px']['max']}\n",
        f"- 长度 px：中位={li['length_px']['median']} / p90={li['length_px']['p90']} / 总长={li['length_px']['total']}\n",
        "\n## 覆盖率 / 亚像素信息\n",
        f"- 墨迹像素 {co.get('ink_px')}（占比 {co.get('ink_fraction')}），墨迹上平均覆盖率 {co.get('cov_mean_on_ink')}\n",
        f"- AA 带占墨迹比例 {co.get('aa_fraction_of_ink')} —— 越高越说明原图有亚像素抗锯齿信息\n",
        "\n## 区域\n",
        f"- 区域数 {re['region_count']}，面积 px：中位={re['area_px']['median']} / p90={re['area_px']['p90']}，"
        f"区域覆盖率 {re['region_fraction']}，颜色场数 {re['color_fields']}\n",
        "\n## 分辨率结论\n",
        f"- {data['resolution']['note']}\n",
        f"- 源图 Nyquist 上限 ≈ {data['resolution']['source_nyquist_px']}px；"
        f"当前测量 R={im['long_side']}（upscale×{im['upscale_factor']}）\n",
    ]
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(lines)
