"""在线笔触强化学习（stroke RL）：画一笔 → 看局部误差 → 接受/撤回，并学习每类结构的用笔力度。

与之前"离线 UCB 选笔刷"不同，这里是**在线闭环**：
1. 维护一张浮点画布（快速软边光栅化，不走完整渲染链，代价低）；
2. 按结构顺序逐笔落笔：先按该类的手臂策略（臂 = 同一笔刷下的"用笔力度"：
   保守/标准/大胆 = 宽度×alpha 组合）落笔；
3. 在笔触影响区域内计算误差变化 ΔE：ΔE<0 才保留（否则回退 = 撤回这一笔）；
4. 每类的臂用 UCB1 选择，reward = max(0, -ΔE)/scale（有效改进=正奖励），
   在线更新均值与计数 → 真的会"训练"出该类结构该用多重的笔；
5. 训练日志（每类/每臂/接受率/总误差曲线）落盘，可复现。

注：RL 阶段的光栅化是软边近似（圆盘笔触 + 宽度插值），用于快速的**决策**；
最终出图仍由标准渲染链（含 10 种笔形内核/纹理）渲染，故决策与实际渲染高度相关但不完全等价。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

import numpy as np

from .util import gaussian_blur

# 每个结构类的"用笔力度臂"：保守 / 标准 / 大胆（宽度×alpha 乘子）
ARMS: tuple[tuple[str, float, float], ...] = (
    ("保守", 0.85, 0.75),
    ("标准", 1.00, 1.00),
    ("大胆", 1.15, 1.00),
)
# 接受判据：误差改进超过该相对阈值才保留（避免噪声笔触）
_ACCEPT_EPS = 1e-4
# 训练日志里的统计精度（仅日志可读性，与 .fbs 渲染契约无关）
_LOG_DECIMALS = 6


def _r(x: float) -> float:
    """日志统计取整（避免浮点噪声；不影响画布/渲染精度）。"""
    return round(float(x), _LOG_DECIMALS)


def _stroke_mask_local(
    pts: np.ndarray,
    widths: np.ndarray,
    soft_px: float,
    bbox: tuple[int, int, int, int],
) -> np.ndarray:
    """在 bbox 内把折线变宽笔触光栅化成覆盖率 mask（软边，供 RL 决策用）。"""
    x0, y0, x1, y1 = bbox
    xs = np.arange(x0, x1, dtype=np.float64)[None, :] + 0.5
    ys = np.arange(y0, y1, dtype=np.float64)[:, None] + 0.5
    n_seg = len(pts) - 1
    best_d = np.full((y1 - y0, x1 - x0), 1e9, dtype=np.float64)
    best_t = np.zeros_like(best_d)
    best_i = np.zeros_like(best_d)
    for i in range(n_seg):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        vx, vy = bx - ax, by - ay
        L2 = vx * vx + vy * vy
        if L2 <= 1e-12:
            continue
        t = np.clip(((xs - ax) * vx + (ys - ay) * vy) / L2, 0.0, 1.0)
        d = np.hypot(xs - (ax + t * vx), ys - (ay + t * vy))
        upd = d < best_d
        best_d = np.where(upd, d, best_d)
        best_t = np.where(upd, t, best_t)
        best_i = np.where(upd, float(i), best_i)
    hw = np.zeros_like(best_d)
    for i in range(n_seg):
        m = best_i == i
        if not np.any(m):
            continue
        hw = np.where(m, 0.5 * (widths[i] * (1.0 - best_t) + widths[i + 1] * best_t), hw)
    soft = max(float(soft_px), 0.3)
    cov = np.clip((hw + soft - best_d) / (2.0 * soft), 0.0, 1.0)
    return cov


def _stroke_bbox(points: np.ndarray, widths: np.ndarray, H: int, W: int, margin: int = 3) -> tuple[int, int, int, int]:
    r = float(np.max(widths)) * 0.5 + margin
    x0 = int(max(0, np.floor(points[:, 0].min() - r)))
    x1 = int(min(W, np.ceil(points[:, 0].max() + r) + 1))
    y0 = int(max(0, np.floor(points[:, 1].min() - r)))
    y1 = int(min(H, np.ceil(points[:, 1].max() + r) + 1))
    return x0, y0, x1, y1


def _ucb_pick(stats: dict[str, dict[str, float]], total: int, c: float = 0.4) -> str:
    best_k, best_v = None, -1e18
    for k, st in stats.items():
        mean = st["reward_sum"] / max(st["n"], 1)
        explore = c * math.sqrt(math.log(max(total, 2)) / max(st["n"], 1))
        v = mean + explore
        if v > best_v:
            best_v, best_k = v, k
    assert best_k is not None
    return best_k


def run_stroke_rl(
    target_rgb: np.ndarray,
    strokes: list[dict],
    log_path: str,
    rng: np.random.Generator | None = None,
    max_strokes: int | None = None,
) -> dict:
    """在线笔触 RL：逐笔落笔/撤回 + 每类 UCB 力度学习；返回 {accepted, stats, log}。"""
    rng = rng or np.random.default_rng(20261018)
    target = np.asarray(target_rgb, dtype=np.float64)
    H, W = target.shape[:2]
    canvas = np.ones((H, W, 3), dtype=np.float64)
    err = np.abs(target - canvas).mean(axis=2)  # 逐像素误差（0..1）
    total_err0 = float(err.mean())
    stats: dict[str, dict[str, dict[str, float]]] = {}
    per_class: dict[str, dict[str, Any]] = {}
    curve: list[dict] = []
    accepted: list[dict] = []
    n = len(strokes) if max_strokes is None else min(len(strokes), int(max_strokes))
    for idx in range(n):
        st = strokes[idx]
        klass = str(st.get("class", "flat"))
        # 该类的手臂统计初始化（每类三个力度臂）
        if klass not in stats:
            stats[klass] = {name: {"reward_sum": 0.0, "n": 0, "acc": 0} for name, _w, _a in ARMS}
        arm_names = [a[0] for a in ARMS]
        arm = _ucb_pick(stats[klass], sum(int(v["n"]) for v in stats[klass].values()))
        wm, am = next((w, a) for name, w, a in ARMS if name == arm)
        pts = np.asarray(st["points_px"], dtype=np.float64)
        widths = np.asarray(st["widths_px"], dtype=np.float64).reshape(-1) * wm
        if len(widths) == 1:
            widths = np.full(len(pts), widths[0])
        alpha = float(np.clip(float(st["alpha"]) * am, 0.05, 1.0))
        color = np.asarray(st["color"], dtype=np.float64).reshape(3)
        bbox = _stroke_bbox(pts, widths, H, W)
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        x0, y0, x1, y1 = bbox
        cov = _stroke_mask_local(pts, widths, 1.2, bbox)
        if not bool(np.any(cov > 0.0)):
            continue
        eff = (cov * alpha)[..., None]
        before = canvas[y0:y1, x0:x1].copy()
        old_err = float(np.abs(target[y0:y1, x0:x1] - before).sum())
        new_px = before * (1.0 - eff) + color[None, None, :] * eff
        new_err = float(np.abs(target[y0:y1, x0:x1] - new_px).sum())
        delta = (new_err - old_err) / max(old_err, 1e-9)
        acc = delta < -_ACCEPT_EPS  # 误差确实下降才保留
        # 中性笔触按 35% 概率保留（覆盖类需求；不提升也不明显变坏）
        if not acc and abs(delta) <= _ACCEPT_EPS and rng.random() < 0.35:
            acc = True
        reward = max(0.0, -delta)
        arm_stats = stats[klass][arm]
        arm_stats["reward_sum"] += reward
        arm_stats["n"] = int(arm_stats["n"]) + 1
        if acc:
            arm_stats["acc"] = int(arm_stats["acc"]) + 1
        cls_stats = per_class.setdefault(klass, {"n": 0, "accepted": 0, "delta_sum": 0.0, "stroke_px": 0})
        cls_stats["n"] = int(cls_stats["n"]) + 1
        if acc:
            canvas[y0:y1, x0:x1] = new_px
            err[y0:y1, x0:x1] = np.abs(target[y0:y1, x0:x1] - new_px).mean(axis=2)
            cls_stats["accepted"] = int(cls_stats["accepted"]) + 1
            cls_stats["stroke_px"] = int(cls_stats["stroke_px"]) + int(np.sum(cov > 0.5))
            out = dict(st)
            out["widths_px"] = widths
            out["alpha"] = alpha
            out["arm"] = arm
            accepted.append(out)
        cls_stats["delta_sum"] = float(cls_stats["delta_sum"]) + delta
        if (idx + 1) % 200 == 0 or idx == n - 1:
            curve.append(
                {
                    "stroke_index": int(idx + 1),
                    "mean_abs_err": _r(np.abs(target - canvas).mean()),
                    "accepted_so_far": int(len(accepted)),
                }
            )
    err_end = float(np.abs(target - canvas).mean())
    psnr = 10.0 * math.log10(1.0 / max(float(np.mean((target - canvas) ** 2)), 1e-12))
    summary = {
        "strokes_total": int(n),
        "strokes_accepted": int(len(accepted)),
        "accept_rate": round(len(accepted) / max(n, 1), 4),
        "err_before": _r(total_err0),
        "err_after": _r(err_end),
        "canvas_psnr_db": round(float(psnr), 3),
        "classes": per_class,
        "arms": {
            klass: {arm: {"n": int(st["n"]), "acc": int(st["acc"]),
                          "mean_reward": _r(st["reward_sum"] / max(st["n"], 1))}
                     for arm, st in arms.items()}
            for klass, arms in stats.items()
        },
        "curve": curve,
    }
    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=1)
        md = os.path.splitext(log_path)[0] + ".md"
        lines = ["# 在线笔触强化学习日志（stroke RL）\n",
                 f"- 笔触数：{n}，接受 {len(accepted)}（接受率 {summary['accept_rate']*100:.1f}%）\n",
                 f"- 平均绝对误差：{total_err0:.5f} → {err_end:.5f}，训练画布 PSNR {psnr:.2f} dB\n",
                 "\n## 各类结构：接受率与学习到的力度臂\n",
                 "| 结构类 | 笔数 | 接受 | 平均ΔE | 各臂(n/接受/平均奖励) |\n|---|---|---|---|---|\n"]
        for klass, cs in per_class.items():
            arms_desc = "；".join(
                f"{arm}: n={v['n']} acc={v['acc']} R={v['mean_reward']:.4f}"
                for arm, v in summary["arms"].get(klass, {}).items()
            )
            lines.append(
                f"| {klass} | {cs['n']} | {cs['accepted']} | "
                f"{cs['delta_sum']/max(cs['n'],1):+.4f} | {arms_desc} |\n"
            )
        lines.append("\n## 误差曲线（每 200 笔）\n\n| 笔数 | 平均绝对误差 | 已接受 |\n|---|---|---|\n")
        for c in curve:
            lines.append(f"| {c['stroke_index']} | {c['mean_abs_err']} | {c['accepted_so_far']} |\n")
        with open(md, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(lines)
        summary["md"] = md
    return {"accepted": accepted, "stats": summary}
