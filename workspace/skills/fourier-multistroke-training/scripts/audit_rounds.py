#!/usr/bin/env python
"""Audit per-round training artifacts and metrics for every run directory."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


def read_log(path: str):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def audit_run(run_dir: str, pattern: str = "round_*_R*.png"):
    stem = os.path.basename(run_dir.rstrip("\\/"))
    rounds_dir = os.path.join(run_dir, "rounds")
    pngs = glob.glob(os.path.join(rounds_dir, pattern))
    by_round = {}
    for p in pngs:
        m = re.search(r"round_(\d+)_R(\d+)\.png$", p)
        if m:
            by_round.setdefault(int(m.group(1)), []).append((int(m.group(2)), p))
    log = read_log(os.path.join(run_dir, "training_log.jsonl"))
    log_by_round = {int(r.get("round", -1)): r for r in log if "round" in r}
    state = {}
    sp = os.path.join(run_dir, "state.json")
    if os.path.exists(sp):
        try:
            state = json.load(open(sp, encoding="utf-8"))
        except Exception:
            pass
    rs = sorted(set(by_round) | set(log_by_round))
    missing_png = [r for r in rs if r not in by_round]
    missing_report = [r for r in rs if not os.path.exists(os.path.join(rounds_dir, f"round_{r:04d}_report.json"))]
    best = max(log, key=lambda r: float(r.get("psnr", -1e9))) if log else None
    last = log[-1] if log else None
    return {
        "run": stem,
        "dir": os.path.abspath(run_dir),
        "rounds_in_log": len(log),
        "rounds_with_png": len(by_round),
        "range": [rs[0], rs[-1]] if rs else None,
        "missing_png": missing_png,
        "missing_report": missing_report,
        "first": {"round": log[0].get("round"), "psnr": log[0].get("psnr"),
                  "ssim": log[0].get("ssim"), "strokes": log[0].get("strokes")} if log else None,
        "last": {"round": last.get("round"), "psnr": last.get("psnr"),
                 "ssim": last.get("ssim"), "strokes": last.get("strokes")} if last else None,
        "best_psnr": {"round": best.get("round"), "psnr": best.get("psnr"),
                      "ssim": best.get("ssim"), "strokes": best.get("strokes")} if best else None,
        "state": {"round": state.get("round"), "best_round": state.get("best_round"),
                  "best_psnr": state.get("best_psnr"), "best_ssim": state.get("best_ssim"),
                  "best_strokes": state.get("best_strokes")},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    runs = []
    for d in sorted(glob.glob(os.path.join(a.root, "run_*"))):
        rd = os.path.join(d, "rounds")
        if not os.path.isdir(rd):
            continue
        runs.append(audit_run(d, "round_*_R*.png"))
    report = {"runs": runs, "total_rounds_logged": sum(r["rounds_in_log"] for r in runs),
              "all_rounds_have_png": all(not r["missing_png"] for r in runs)}
    json.dump(report, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    md = [f"# 每轮训练产物审计\n", f"- 运行目录：{len(runs)} 个",
          f"- 总记录轮数：{report['total_rounds_logged']}",
          f"- 每轮都有 PNG：{'是' if report['all_rounds_have_png'] else '否'}\n",
          "| run | 轮数 | 范围 | PNG | 缺 PNG | 缺 report | best PSNR | best 轮 | last PSNR | last 笔数 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        bp = r["best_psnr"] or {}
        last = r["last"] or {}
        md.append(f"| {r['run']} | {r['rounds_in_log']} | {r['range']} | {r['rounds_with_png']} | "
                  f"{len(r['missing_png'])} | {len(r['missing_report'])} | {bp.get('psnr')} | "
                  f"{bp.get('round')} | {last.get('psnr')} | {last.get('strokes')} |")
    md.append("\n缺失明细：")
    for r in runs:
        if r["missing_png"] or r["missing_report"]:
            md.append(f"- {r['run']}: missing_png={r['missing_png']} missing_report={r['missing_report']}")
    md_path = os.path.splitext(a.out)[0] + ".md"
    open(md_path, "w", encoding="utf-8", newline="\n").write("\n".join(md) + "\n")
    print(json.dumps({"runs": [(r["run"], r["rounds_in_log"], r["all_ok"] if "all_ok" in r else
                                (not r["missing_png"] and not r["missing_report"])) for r in runs],
                      "total_rounds_logged": report["total_rounds_logged"],
                      "all_rounds_have_png": report["all_rounds_have_png"]}, ensure_ascii=False, indent=1))
    print(md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
