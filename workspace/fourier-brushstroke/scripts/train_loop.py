#!/usr/bin/env python
"""训练循环 CLI（可断点续训，跑满指定时长）。"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fbs_core import trainloop  # noqa: E402


def _parse_policy_overrides(items: list[str] | None) -> dict | None:
    if not items:
        return None
    out: dict[str, dict] = {}
    for it in items:
        try:
            klass, rest = it.split("=", 1)
            preset, w, l = rest.split("|")
            out.setdefault(klass.strip(), {}).update(
                {"preset": preset.strip(), "width_scale": float(w), "length_scale": float(l)}
            )
        except Exception:
            raise SystemExit("坏掉的 --policy-override：%r（格式 class=preset|w|l）" % (it,))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="结构笔触训练循环（RL + 剪枝 + checkpoint）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--hours", type=float, default=None, help="训练时长（默认 9 小时；断点续训沿用原 deadline）")
    ap.add_argument("--eval-res", type=int, default=None,
                    help="每轮评估渲染分辨率（默认 1024，与交付口径一致；最终图按 final-res 全质量渲染）")
    ap.add_argument("--final-res", type=int, default=None, help="best 展示渲染分辨率（默认 2048）")
    ap.add_argument("--max-rounds", type=int, default=None, help="最多轮数（dry-run 用）")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--n-candidates", type=int, default=None, help="每笔连续动作候选数（默认 2）")
    ap.add_argument("--w-strokes", type=float, default=None)
    ap.add_argument("--w-segments", type=float, default=None)
    ap.add_argument("--eval-ss", dest="eval_ss", type=int, default=None,
                    help="每轮评估渲染超采样（默认 1，速度优先；质量口径与 2 不同）")
    ap.add_argument("--workers", type=int, default=None, help="并行评估进程数（默认 min(3, CPU-1)）")
    ap.add_argument("--policy-override", action="append", default=None,
                    help="策略覆盖：class=preset|width_scale|length_scale（可重复，如 shade=basic_round_hard|1.0|1.0）")
    ap.add_argument("--generator", choices=["structpaint", "finelines"], default=None,
                    help="笔触生成器：structpaint=宽笔结构笔触（默认）| finelines=精细元素纯细线")
    ap.add_argument("--std-threshold", dest="std_threshold", type=float, default=None,
                    help="B：区域颜色标准差阈值（越大越倾向判 flat；默认 contour=0.06）")
    ap.add_argument("--detail-area-frac", dest="detail_area_frac", type=float, default=None,
                    help="B：detail 区域面积占比阈值（默认 0.0012）")
    ap.add_argument("--edge-strokes", dest="edge_strokes", action="store_true", default=None,
                    help="C：沿区域边界+图像梯度生成窄沿边笔")
    ap.add_argument("--width-cap-px", dest="width_cap_px", type=float, default=None,
                    help="笔宽上限（源分辨率像素；如 12 表示 @421 空间最大 12px）")
    args = ap.parse_args(argv)
    state = trainloop.run_training_loop(
        args.input,
        args.outdir,
        hours=float(args.hours if args.hours is not None else 9.0),
        eval_res=int(args.eval_res if args.eval_res is not None else 1024),
        final_res=int(args.final_res if args.final_res is not None else 2048),
        max_rounds=args.max_rounds,
        seed0=int(args.seed if args.seed is not None else 20261018),
        n_candidates=int(args.n_candidates if args.n_candidates is not None else 2),
        w_strokes=float(args.w_strokes if args.w_strokes is not None else 0.02),
        w_segments=float(args.w_segments if args.w_segments is not None else 0.01),
        eval_ss=int(args.eval_ss if args.eval_ss is not None else 1),
        workers=args.workers,
        policy_overrides=_parse_policy_overrides(args.policy_override),
        width_cap_px=args.width_cap_px,
        edge_strokes=bool(args.edge_strokes),
        std_threshold=args.std_threshold,
        generator=str(args.generator if args.generator is not None else "structpaint"),
        detail_area_frac=args.detail_area_frac,
    )
    print(f"[train_loop] done: round={state['round']} best J={state['best_J']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
