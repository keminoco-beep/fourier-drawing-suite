#!/usr/bin/env python
"""One-command local pipeline: analysis -> train -> detail/soft/dabs -> clean
-> colour calibration -> 4GiB compile -> pack -> verify -> local summary.

This keeps the per-stage logic in the existing scripts; the agent only calls this
driver once, so token/round-trip cost stays low.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_ws():
    env = os.environ.get("FOURIER_WS")
    if env:
        return os.path.abspath(env)
    cur = HERE
    for _ in range(8):
        if os.path.exists(os.path.join(cur, "fourier-brushstroke", "scripts", "fbs.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.path.join(HERE, "..", ".."))


WS = _find_ws()
PY = sys.executable


def run(cmd, log_path):
    t0 = time.time()
    with open(log_path, "a", encoding="utf-8") as log:
        log.write("\n$ " + " ".join(cmd) + "\n")
        log.flush()
        p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=WS)
    if p.returncode != 0:
        raise SystemExit(f"stage failed ({p.returncode}): {' '.join(cmd)} (log: {log_path})")
    return time.time() - t0


def script(name):
    return os.path.join(HERE, name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=0, help="0 = auto from analysis")
    ap.add_argument("--rounds", type=int, default=0, help="0 = auto")
    ap.add_argument("--preset", default="auto", choices=("auto", "default", "soft", "impasto"))
    ap.add_argument("--force-train", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-detail", action="store_true")
    ap.add_argument("--skip-color", action="store_true")
    ap.add_argument("--skip-pack", action="store_true")
    args = ap.parse_args()
    out = os.path.abspath(args.outdir)
    os.makedirs(out, exist_ok=True)
    log_path = os.path.join(out, "pipeline.log")
    t_all = time.time()

    from analyze_input import analyze
    rec = analyze(args.input)
    preset = rec["recommend"]["preset"] if args.preset == "auto" else args.preset
    work_res = args.work_res or int(rec["recommend"]["work_res"])
    rounds = args.rounds or int(rec["recommend"]["rounds"])
    detail_scale = float(rec["recommend"]["detail_scale"])
    sat_mul = float(rec["recommend"]["sat_mul"])
    run_dir = os.path.join(out, "run")
    state = {"analysis": rec, "preset": preset, "work_res": work_res,
             "rounds": rounds, "stages": {}, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # 1) train
    if not args.skip_train and (args.force_train or not os.path.exists(os.path.join(run_dir, "best_strokes.json"))):
        state["stages"]["train"] = run([
            PY, script("run_train.py"), "--input", args.input, "--outdir", run_dir,
            "--work-res", str(work_res), "--max-rounds", str(rounds),
            "--fix-passes", "2", "--fresh-every", "4",
            "--stroke-weight", "0.02", "--ssim-weight", "1.0",
            "--preset", preset,
        ], log_path)
    strokes = os.path.join(run_dir, "best_strokes.json")
    if not os.path.exists(strokes):
        raise SystemExit(f"missing {strokes}; run without --skip-train first")

    # 2) soft atmosphere (only for soft preset)
    if preset == "soft":
        soft_dir = os.path.join(out, "soft")
        state["stages"]["soft"] = run([
            PY, script("soft_atmosphere.py"), "--strokes", strokes, "--input", args.input,
            "--outdir", soft_dir, "--work-res", str(work_res),
        ], log_path)
        strokes = os.path.join(soft_dir, "soft_strokes.json")

    # 3) detail
    detail_dir = os.path.join(out, "detail")
    if not args.skip_detail:
        counts = [int(round(x * detail_scale)) for x in (5200, 4600, 6000, 420, 4200, 9000)]
        state["stages"]["detail"] = run([
            PY, script("refine_detail.py"), "--strokes", strokes, "--input", args.input,
            "--outdir", detail_dir, "--work-res", str(work_res),
            "--streamlines", str(counts[0]), "--edges", str(counts[1]),
            "--hatch", str(counts[2]), "--halos", str(counts[3]),
            "--dark", str(counts[4]), "--texture", str(counts[5] if preset != "soft" else counts[5] // 4),
            "--max-total", "90000",
        ], log_path)
        strokes = os.path.join(detail_dir, "detail_strokes.json")

    # 4) clean jitter
    clean_dir = os.path.join(out, "clean")
    state["stages"]["clean"] = run([
        PY, script("clean_strokes.py"), "--strokes", strokes, "--input", args.input,
        "--outdir", clean_dir, "--work-res", str(work_res),
    ], log_path)
    strokes = os.path.join(clean_dir, "cleaned_strokes.json")

    # 4.5) soft glaze under detail: unifies haze without erasing boats/sun
    if preset == "soft":
        glaze_dir = os.path.join(out, "glaze")
        state["stages"]["glaze"] = run([
            PY, script("soft_glaze.py"), "--strokes", strokes, "--input", args.input,
            "--outdir", glaze_dir, "--work-res", str(work_res), "--layer", "3",
            "--alpha", "0.35", "--width-min", "20", "--width-max", "45",
        ], log_path)
        strokes = os.path.join(glaze_dir, "glaze_strokes.json")

    # 5) thick dabs (impasto or soft image with lots of visible directional dabs)
    if preset == "impasto":
        dabs_dir = os.path.join(out, "dabs")
        state["stages"]["dabs"] = run([
            PY, script("thick_dabs.py"), "--strokes", strokes, "--input", args.input,
            "--outdir", dabs_dir, "--work-res", str(work_res), "--count", "16000",
        ], log_path)
        strokes = os.path.join(dabs_dir, "dabs_strokes.json")

    # 6) colour calibration
    if not args.skip_color:
        calib_dir = os.path.join(out, "calib")
        state["stages"]["color"] = run([
            PY, script("calibrate_color.py"), "--strokes", strokes, "--input", args.input,
            "--outdir", calib_dir, "--work-res", str(work_res),
            "--iters", "1", "--sat-mul", f"{sat_mul:.3f}", "--contrast-mul", "1.0",
        ], log_path)
        strokes = os.path.join(calib_dir, "calibrated_strokes.json")

    # 7) 4GiB compile
    final_dir = os.path.join(out, "final")
    state["stages"]["compile"] = run([
        PY, script("compile_best.py"), "--run", run_dir, "--strokes", strokes,
        "--input", args.input, "--outdir", final_dir,
        "--resolution", "1536", "--render", "2048",
        "--nmax", "16384", "--epsilon", "0.08", "--max-curves", "100000",
    ], log_path)
    fbs = sorted(glob.glob(os.path.join(final_dir, "*.fbs.json")),
                 key=os.path.getmtime)[-1]

    # 8) pack + verify
    fdraw = os.path.splitext(fbs)[0].replace(".fbs", "") + ".fdraw"
    if not args.skip_pack:
        state["stages"]["pack"] = run([
            PY, script("pack_fdraw.py"), "--fbs", fbs, "--out", fdraw,
        ], log_path)
    state["stages"]["verify"] = run([
        PY, script("verify_delivery.py"), "--dir", final_dir, "--input", args.input,
    ], log_path)
    state["fbs"] = fbs
    state["fdraw"] = fdraw
    state["verification"] = os.path.join(final_dir, "verification.json")
    state["seconds"] = round(time.time() - t_all, 1)
    with open(os.path.join(out, "pipeline_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"outdir": out, "preset": preset, "work_res": work_res,
                      "rounds": rounds, "fbs": fbs, "fdraw": fdraw,
                      "summary": os.path.join(out, "pipeline_summary.json"),
                      "seconds": state["seconds"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
