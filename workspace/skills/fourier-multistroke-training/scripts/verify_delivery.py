#!/usr/bin/env python
"""Verify a delivery folder against the task constraints and container caps."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
# Resolve junctions/symlinks first: registered skills are reached through
# .dsh/skills/<skill> -> skills/<skill>, and walking up the junction path lands
# in .dsh/skills (which has no real fourier_prompt/ package) instead of the
# workspace root.  FOURIER_WS still overrides the auto-detection.
_REAL_HERE = os.path.dirname(os.path.realpath(__file__))


def _find_ws():
    env = os.environ.get("FOURIER_WS")
    if env:
        return os.path.abspath(env)
    cur = _REAL_HERE
    for _ in range(8):
        if os.path.exists(os.path.join(cur, "fourier-brushstroke", "scripts", "fbs.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.path.join(_REAL_HERE, "..", ".."))

WS = _find_ws()
for p in (HERE, os.path.join(WS, "fourier-brushstroke", "scripts"), os.path.join(WS, "fourier-prompt")):
    if p not in sys.path:
        sys.path.insert(0, p)

import hatch_gen as hgr  # noqa: E402
from fourier_prompt import fdraw  # noqa: E402


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--name", default=None)
    args = ap.parse_args()
    d = os.path.abspath(args.dir)
    src = Image.open(args.input).convert("RGB")
    import glob
    if args.name:
        fbs_path = os.path.join(d, f"{args.name}_R1536.fbs.json")
        fdraw_path = os.path.join(d, f"{args.name}.fdraw")
    else:
        fbs_candidates = glob.glob(os.path.join(d, "*.fbs.json"))
        fd_candidates = glob.glob(os.path.join(d, "*.fdraw"))
        if not fbs_candidates:
            raise SystemExit(f"no .fbs.json found under {d}")
        fbs_path = max(fbs_candidates, key=os.path.getmtime)
        fdraw_path = max(fd_candidates, key=os.path.getmtime) if fd_candidates else os.path.join(d, "missing.fdraw")
    with open(fbs_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    strokes = doc.get("strokes") or []
    contract = doc.get("render_contract") or {}
    fills = int(doc.get("stats", {}).get("fill_count", -1))
    checks = []

    def check(name: str, ok: bool, value):
        checks.append({"check": name, "ok": bool(ok), "value": value})
        return ok

    check("fbs_mode_per_stroke", doc.get("mode") == "per_stroke", doc.get("mode"))
    check("fbs_fill_count_zero", fills == 0, fills)
    check("stroke_count_le_max_curves", len(strokes) <= 100000, len(strokes))
    check("fbs_bytes_under_4GiB", os.path.getsize(fbs_path) < 4096 * 1024 ** 2, os.path.getsize(fbs_path))
    check("contract_target_resolution", int(contract.get("target_resolution", 0)) <= 8192,
          contract.get("target_resolution"))
    check("contract_epsilon", float(contract.get("epsilon_px", 0)) == 0.08, contract.get("epsilon_px"))
    renders = []
    fbs_stem = os.path.splitext(os.path.basename(fbs_path))[0]
    render_stem = fbs_stem.rsplit("_R", 1)[0]
    for R in (1536, 2048, 4096):
        preferred = os.path.join(d, f"{render_stem}_R{R}.png")
        matches = [preferred] if os.path.exists(preferred) else glob.glob(os.path.join(d, f"*_R{R}.png"))
        if not matches:
            continue
        p = matches[0]
        rec_img = Image.open(p).convert("RGB")
        ref = np.asarray(src.resize(rec_img.size, Image.LANCZOS), dtype=np.float64) / 255.0
        rec = np.asarray(rec_img, dtype=np.float64) / 255.0
        renders.append({"resolution": R, "psnr": round(hgr._psnr(ref, rec), 4),
                        "ssim": round(hgr._ssim(ref, rec), 5),
                        "bytes": os.path.getsize(p), "sha256": sha256_file(p)[:16]})
    check("renders_present", len(renders) >= 1, [r["resolution"] for r in renders])
    # The portable .fdraw container is part of the delivery contract.  A missing
    # container must fail verification instead of silently skipping the whole
    # fdraw block (pipeline --skip-pack used to report all_ok=true here).
    fdraw_present = os.path.exists(fdraw_path)
    fd = {"path": fdraw_path, "present": fdraw_present}
    check("fdraw_present", fdraw_present, fdraw_present)
    if fdraw_present:
        errors = fdraw.validate(fdraw_path)
        info = fdraw.inspect(fdraw_path)
        fd.update({"bytes": os.path.getsize(fdraw_path), "validate_errors": errors,
                   "zip_bytes": info.get("zip_bytes"),
                   "uncompressed_bytes": info.get("uncompressed_bytes"),
                   "under_4GiB": os.path.getsize(fdraw_path) < 4096 * 1024 ** 2,
                   "uncompressed_under_4GiB": int(info.get("uncompressed_bytes") or 0) < 4096 * 1024 ** 2})
        check("fdraw_valid", len(errors) == 0, errors)
        check("fdraw_container_under_4GiB", os.path.getsize(fdraw_path) < 4096 * 1024 ** 2,
              os.path.getsize(fdraw_path))
        check("fdraw_uncompressed_under_4GiB", int(info.get("uncompressed_bytes") or 0) < 4096 * 1024 ** 2,
              info.get("uncompressed_bytes"))
    else:
        # Only additive field: keeps old verification.json consumers working.
        fd["reason"] = f"missing .fdraw container: {fdraw_path}"
    report = {
        "name": args.name or os.path.basename(d),
        "dir": d,
        "input": os.path.abspath(args.input),
        "fbs": {"path": fbs_path, "bytes": os.path.getsize(fbs_path),
                "stroke_count": len(strokes), "fill_count": fills,
                "coef_count": int(doc.get("stats", {}).get("coef_count", 0))},
        "contract": contract,
        "fdraw": fd,
        "renders": renders,
        "checks": checks,
        "all_ok": all(c["ok"] for c in checks),
    }
    out = os.path.join(d, "verification.json")
    json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    summary = {"name": report["name"], "all_ok": report["all_ok"],
               "fails": [c for c in checks if not c["ok"]],
               "renders": renders, "fbs_bytes": report["fbs"]["bytes"],
               "fdraw_bytes": fd.get("bytes"), "fills": fills}
    if not fdraw_present:
        summary["fdraw_missing"] = fdraw_path
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if report["all_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
