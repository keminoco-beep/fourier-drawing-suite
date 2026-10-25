"""Reproduce the WP-F parity/bench numbers in ``reports/WP-F.md``.

Usage (from ``fourier-prompt/``)::

    python -B tests/render/parity_bench.py > parity.json

The script only reads the six WP-D samples and the two hand-written FDSL plans
in ``tests/render/fixtures``; nothing is written to disk.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from fourier_prompt import render_fast as rf  # noqa: E402
from fourier_prompt.compile import compile_plan  # noqa: E402

SAMPLE_DIR = PKG_ROOT / "out" / "wp-d"
SAMPLES = [
    ("01_rose_medallion", SAMPLE_DIR / "01_rose_medallion.fbs"),
    ("02_star_badge", SAMPLE_DIR / "02_star_badge.fbs"),
    ("03_golden_spiral", SAMPLE_DIR / "03_golden_spiral.fbs"),
    ("04_ocean_waves", SAMPLE_DIR / "04_ocean_waves.fbs"),
    ("05_aurora", SAMPLE_DIR / "05_aurora.fbs"),
    ("06_cat_tophat", SAMPLE_DIR / "06_cat_tophat.fbs"),
]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compiled_plans() -> list[tuple[str, dict]]:
    out = []
    for path in sorted((HERE / "fixtures").glob("*.plan.json")):
        result = compile_plan(load_json(path), resolution=256)
        out.append((path.stem.replace(".plan", ""), result["fbs"]))
    return out


def parity_row(name: str, doc: dict, resolution: int = 256) -> dict:
    ok, reasons = rf.fast_supported(doc)
    t0 = time.perf_counter()
    fast, info = rf.render_with_info(doc, resolution=resolution)
    fast_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    exact = rf.render_auto(doc, resolution=resolution, quality="exact")
    exact_s = time.perf_counter() - t0
    metrics = rf.compare_images(exact, fast)
    return {
        "name": name,
        "supported": ok,
        "reasons": reasons,
        "quality": info["quality"],
        "fallback_reason": info.get("fallback_reason") or [],
        "fast_s": round(fast_s, 3),
        "exact_s": round(exact_s, 3),
        "speedup": round(exact_s / fast_s, 2) if fast_s else None,
        **metrics,
    }


def main() -> int:
    docs = [(name, load_json(path)) for name, path in SAMPLES]
    docs += compiled_plans()
    report = {"parity_r256": [parity_row(name, doc) for name, doc in docs]}

    # R512 timing + parity for two typical samples
    r512 = []
    for name in ("01_rose_medallion", "03_golden_spiral"):
        doc = dict(docs)[name]
        row = parity_row(name, doc, resolution=512)
        bench = rf.bench(doc, resolution=512, repeats=2, include_exact=False)
        row["bench_p50_s"] = bench["fast_p50_s"]
        row["peak_mem_mb"] = bench["peak_mem_mb"]
        r512.append(row)
    report["r512"] = r512

    # R1024 smoke (fast only)
    doc = dict(docs)["03_golden_spiral"]
    t0 = time.perf_counter()
    img = rf.render_auto(doc, resolution=1024, quality="fast")
    report["r1024_smoke"] = {
        "name": "03_golden_spiral",
        "fast_s": round(time.perf_counter() - t0, 2),
        "size": [int(img.shape[1]), int(img.shape[0])],
        "quality": rf.LAST_INFO.get("quality"),
        "ss": rf.LAST_INFO.get("supersample"),
    }

    # determinism: same input twice -> identical pixels
    det = []
    for name, doc in docs:
        a = rf.render_auto(doc, resolution=256, quality="fast")
        b = rf.render_auto(doc, resolution=256, quality="fast")
        det.append({"name": name, "identical": bool((a == b).all())})
    report["determinism"] = det
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
