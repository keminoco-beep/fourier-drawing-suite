"""并行评估：把一轮里的多个剪枝候选/渲染评估放到独立进程（multiprocessing spawn）。

实测单轮瓶颈是渲染（1024 @ SS2、约 2500 笔 ≈130s），而 doc 构建/指标只占十几秒。
所以把 3 个候选的 (build_doc + render + metrics) 并行到 3 个进程，单轮从 350s 量级降到 ~50-140s。

工作进程把 fbs/png 写到临时文件，只回传指标与小体积路径，避免大数组跨进程序列化。
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import tempfile
import traceback
from typing import Any

import numpy as np
from PIL import Image

from . import io_format, render as render_mod, structdoc
from .config import FBSConfig
from .util import WarningLog


def _psnr_ssim(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    import math

    mse = float(np.mean((a - b) ** 2))
    psnr = 10.0 * math.log10(1.0 / max(mse, 1e-12))
    from .util import gaussian_blur

    ag, bg = a.mean(axis=2), b.mean(axis=2)
    mu_a = gaussian_blur(ag.astype(np.float32), 1.5)
    mu_b = gaussian_blur(bg.astype(np.float32), 1.5)
    va = gaussian_blur((ag * ag).astype(np.float32), 1.5) - mu_a ** 2
    vb = gaussian_blur((bg * bg).astype(np.float32), 1.5) - mu_b ** 2
    cov = gaussian_blur((ag * bg).astype(np.float32), 1.5) - mu_a * mu_b
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    s = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2))
    return psnr, float(s.mean())


def _worker(payload: dict) -> dict:
    try:
        cfg_args = payload["cfg"]
        cfg = FBSConfig(**cfg_args)
        img = payload["img"]
        inp = payload["input_path"]
        ref = payload["ref"]
        eval_res = int(payload["eval_res"])
        ss = int(payload["supersample"])
        strokes = payload["strokes"]
        img_dict = {
            "rgb": img["rgb"], "rgba": img["rgba"], "alpha": img["alpha"],
            "width": img["width"], "height": img["height"],
            "long_side": img["long_side"], "path": inp,
        }
        doc = structdoc.build_doc_from_strokes(strokes, img_dict, inp, cfg, WarningLog(),
                                               method="structure_brush_training")
        rgb_u8 = render_mod.render(doc, resolution=eval_res, supersample=ss, mode="sweep",
                                   warn=WarningLog())
        a = rgb_u8.astype(np.float64) / 255.0
        p, s = _psnr_ssim(a, ref)
        io_format.write_fbs(doc, payload["fbs_path"])
        Image.fromarray(rgb_u8).save(payload["png_path"])
        return {"ok": True, "frac": payload["frac"], "psnr": p, "ssim": s,
                "fbs_path": payload["fbs_path"], "png_path": payload["png_path"],
                "strokes": len(strokes),
                "segments": int(payload["segments"])}
    except Exception:
        return {"ok": False, "frac": payload.get("frac"), "error": traceback.format_exc()}


def eval_variants_parallel(payloads: list[dict], workers: int | None = None) -> list[dict]:
    """并行执行多个评估 payload；返回结果列表（顺序与输入一致，失败的带 ok=False）。"""
    if len(payloads) <= 1:
        return [_worker(p) for p in payloads]
    cpu = os.cpu_count()
    if cpu is None:
        cpu = 2
    n = int(workers if workers is not None else min(len(payloads), max(1, cpu - 1)))
    n = max(1, min(n, len(payloads)))
    ctx = mp.get_context("spawn")
    try:
        with ctx.Pool(processes=n) as pool:
            return pool.map(_worker, payloads)
    except Exception:
        # 并行失败则退化为顺序（训练不中断）
        out = []
        for p in payloads:
            out.append(_worker(p))
        return out
