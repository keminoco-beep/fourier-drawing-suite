"""画笔属性时间轴导出（brush timeline）。

产品定义：整个绘画过程 = 一串傅里叶函数（笔触路径）+ 一串随时间 t 定义的画笔属性。
`.fbs` 已存前者与逐笔属性曲线；本模块把两者**按时间展开**成一份可直接读的时间轴文件：

    {
      "schema": "fbs-brush-timeline/1",
      "duration_s": 60.0,
      "styles": [ {"shape": "soft", "color": {...曲线...}, "alpha": {...曲线...}}, ...],
      "strokes": [
        {"id": 0, "t0": 0.0, "t1": 0.9, "duration_s": 0.9, "start": 0.0, "end": 0.015,
         "fourier": {"N": .., "coef": [[k, re, im], ...], "closed": false, "arc_length": ..},
         "brush": {"style": 0, "width": {...曲线...}}}
      ]
    }

时间分配：每笔时长与曲线弧长成正比（画师运笔有快慢），起始时间按作画顺序均匀铺在
总时长上，相邻笔触允许重叠（同时落笔/湿画法）。属性曲线保留 `.fbs` 原样（时间轴只
做展开，不重新拟合）。
"""
from __future__ import annotations

import json
import os
from typing import Any

import numpy as np

# 时间轴文件的系数精度（世界单位，1e-5 ≈ 1024px 下的 0.01px；不影响可渲染性）
_COEF_DECIMALS = 5
# 时间精度：1µs（视频帧级 33ms，时间轴可编辑性足够）；与渲染契约无关，属时间轴格式约定。
_TIME_DECIMALS = 6


def _q_time(t: float) -> float:
    return round(float(t), _TIME_DECIMALS)


def _arc_len(stroke: dict) -> float:
    try:
        return float(stroke.get("arc_length") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _round_coef(coef: Any) -> list:
    out = []
    for item in coef or []:
        try:
            k, re, im = item
            out.append([int(k), round(float(re), _COEF_DECIMALS), round(float(im), _COEF_DECIMALS)])
        except (TypeError, ValueError):
            continue
    return out


def build_timeline(
    doc: dict,
    duration_s: float = 60.0,
    overlap: float = 2.5,
) -> dict:
    """把逐笔画 `.fbs` 文档展开为画笔属性时间轴。"""
    strokes = [s for s in (doc.get("strokes") or []) if isinstance(s, dict) and s.get("coef")]
    styles_all = doc.get("brush_styles") or []
    n = len(strokes)
    if n == 0:
        raise ValueError("文档里没有可用笔画（需要带 coef 的笔画）")
    duration = float(max(1.0, duration_s))
    arcs = np.asarray([max(_arc_len(s), 1e-6) for s in strokes], dtype=np.float64)
    total_arc = float(arcs.sum())
    # 每笔时长 ∝ 弧长；所有笔触时长之和 ≈ duration * overlap（允许同时落笔）
    durs = duration * overlap * arcs / max(total_arc, 1e-9)
    durs = np.clip(durs, 0.02, max(0.05, duration * 0.25))

    # 样式表按需收集（相同 style 索引只写一次）
    used_style_ids: list[int] = []
    style_map: dict[int, int] = {}
    styles_out: list[dict] = []
    for st in strokes:
        idx = (st.get("brush") or {}).get("style", 0)
        idx = int(idx) if isinstance(idx, int) else 0
        if idx not in style_map:
            src = styles_all[idx] if 0 <= idx < len(styles_all) and isinstance(styles_all[idx], dict) else {}
            style_map[idx] = len(styles_out)
            styles_out.append(
                {
                    "shape": src.get("shape"),
                    "color": src.get("color"),
                    "alpha": src.get("alpha"),
                }
            )
        used_style_ids.append(idx)

    out: list[dict[str, Any]] = []
    for rank, (i, st) in enumerate(zip(range(n), strokes)):
        t0 = (rank / max(n - 1, 1)) * (duration - float(durs[i]) * 0.5)
        t0 = float(np.clip(t0, 0.0, duration))
        t1 = float(np.clip(t0 + float(durs[i]), 0.0, duration))
        out.append(
            {
                "id": int(st.get("id", i)),
                "order": int(rank),
                "t0": _q_time(t0),
                "t1": _q_time(t1),
                "duration_s": _q_time(t1 - t0),
                "start": _q_time(t0 / duration),
                "end": _q_time(t1 / duration),
                "fourier": {
                    "N": int(st.get("N", 0)),
                    "closed": bool(st.get("closed", False)),
                    "arc_length": _q_time(float(_arc_len(st))),
                    "coef": _round_coef(st.get("coef")),
                },
                "brush": {
                    "style": int(style_map[used_style_ids[rank]]),
                    "width": (st.get("brush") or {}).get("width"),
                },
            }
        )
    return {
        "schema": "fbs-brush-timeline/1",
        "duration_s": duration,
        "stroke_count": n,
        "overlap": overlap,
        "brush_model": {
            "representation": "fourier_stroke",
            "attributes": ["width", "color", "alpha", "shape"],
            "timeline": "每笔 [t0,t1] 秒；属性曲线按弧长归一化参数 t∈[0,1] 求值",
            "phases": "由粗到细分层落笔（.fbs decomposition.method=painterly_brush_strokes）",
        },
        "styles": styles_out,
        "strokes": out,
    }


def write_timeline(doc: dict, path: str, duration_s: float = 60.0, overlap: float = 2.5) -> str:
    data = build_timeline(doc, duration_s=duration_s, overlap=overlap)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    return path
