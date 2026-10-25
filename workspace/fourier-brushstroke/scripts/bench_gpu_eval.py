"""GPU 评估渲染基准：速度 + 与 CPU renderer 的 PSNR 差异。

用法：python scripts/bench_gpu_eval.py --fbs <path> [--resolution 768] [--ref <image>]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fbs_core import gpueval, io_format, metrics  # noqa: E402
from fbs_core import render as render_mod  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GPU 评估渲染基准")
    ap.add_argument("--fbs", required=True)
    ap.add_argument("--ref", default=None)
    ap.add_argument("--resolution", type=int, default=768)
    args = ap.parse_args(argv)
    doc = io_format.read_fbs(args.fbs)
    if not gpueval.gpu_available():
        print("[bench] GPU 不可用（torch/cuda 缺失）")
        return 2
    import torch

    print(f"[bench] torch {torch.__version__}, cuda {torch.version.cuda}, "
          f"device {torch.cuda.get_device_name(0)}")
    # CPU 基准（SS=1，与训练评估口径一致）
    t = time.time()
    rgb_cpu = render_mod.render(doc, resolution=args.resolution, supersample=1, mode="sweep",
                                warn=WarningLog())
    t_cpu = time.time() - t
    ref = None
    if args.ref:
        ref = np.asarray(Image.open(args.ref).convert("RGB").resize(
            (rgb_cpu.shape[1], rgb_cpu.shape[0]), Image.LANCZOS), dtype=np.float64) / 255.0
    torch.cuda.synchronize()
    t = time.time()
    rgb_gpu = gpueval.render_gpu(doc, resolution=args.resolution, supersample=1)
    torch.cuda.synchronize()
    t_gpu = time.time() - t
    torch.cuda.synchronize()
    t = time.time()
    rgb_gpu2 = gpueval.render_gpu(doc, resolution=args.resolution, supersample=2)
    torch.cuda.synchronize()
    t_gpu2 = time.time() - t
    res = {"cpu_s": round(t_cpu, 2), "gpu_s": round(t_gpu, 2), "gpu_ss2_s": round(t_gpu2, 2)}
    if ref is not None:
        mc = metrics.compare(rgb_cpu.astype(np.float64) / 255.0, ref)
        mg = metrics.compare(rgb_gpu.astype(np.float64) / 255.0, ref)
        mg2 = metrics.compare(rgb_gpu2.astype(np.float64) / 255.0, ref)
        res.update({"cpu_psnr": round(mc["psnr"], 3), "cpu_ssim": round(mc["ssim"], 4),
                    "gpu_psnr": round(mg["psnr"], 3), "gpu_ssim": round(mg["ssim"], 4),
                    "gpu_ss2_psnr": round(mg2["psnr"], 3), "gpu_ss2_ssim": round(mg2["ssim"], 4),
                    "gpu_minus_cpu_psnr": round(mg["psnr"] - mc["psnr"], 3),
                    "gpu_ss2_minus_cpu_psnr": round(mg2["psnr"] - mc["psnr"], 3)})
    diff = np.abs(rgb_gpu.astype(np.int16) - rgb_cpu.astype(np.int16))
    res["pixel_mae"] = round(float(diff.mean()), 3)
    res["pixel_p95"] = int(np.percentile(diff, 95))
    print("[bench] " + str(res))
    outdir = os.path.dirname(os.path.abspath(args.fbs))
    Image.fromarray(rgb_gpu).save(os.path.join(outdir, f"bench_gpu_R{args.resolution}.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
