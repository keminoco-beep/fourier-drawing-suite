"""WP-B 构建黄金基线：冻结 / 校验 `build_from_image` 的可观测输出。

为什么需要
----------
KICKOFF v1 §2.2 DoD-1/2：`document.py` 的 additive 重构（`assemble_document` /
`stroke_record` / `StyleTable`）必须证明 **行为不变**。工作区没有 git，也没有
可比的 `poc/out` 构建产物，所以基线文件本身就是版本锚点。

黄金口径（唯一非确定性字段只有 `created`；`source.path` 是绝对路径）
---------------------------------------------------------------
每个 case 记录四项**必须逐项相等**的指纹：
1. `doc_sha256`         —— 归一化 doc（去掉 `created` / `source.path`）的 JSON sha256；
2. `png_sha256`         —— 渲染像素（RGB uint8 原始字节）sha256 + 形状；
3. `fbs_bytes`          —— `io_format.write_fbs()` 报告的字节数；
4. `fbs_content_sha256` —— 量化后 doc（同样归一化）的 JSON sha256（比字节数更严）。

另记录 stroke/style/fft_passes/fill_soft/method/warnings 供报告解释。
`write_fbs` 文件自身 sha256 因 `created` 每次都变，**不作为**判据。

用法
----
    python tests/_baseline/freeze_baseline.py                    # 冻结（默认写 baseline.json）
    python tests/_baseline/freeze_baseline.py --mode check       # 与基线逐项比对（不等退出 1）
    python tests/_baseline/freeze_baseline.py --mode check --out other.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # fourier-brushstroke/
SCRIPTS = os.path.join(ROOT, "scripts")
TESTCASES = os.path.join(ROOT, "assets", "testcases")
DEFAULT_OUT = os.path.join(HERE, "baseline.json")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from fbs_core import document, io_format, render  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import WarningLog, sha256_json  # noqa: E402

SCHEMA = "fbs-build-baseline/1"
#: 7 个官方 build case（KICKOFF §2.2 DoD-1）：T1–T4 + 3 个合成线稿（T5/T12/T13）。
CASES: list[dict] = [
    {"name": "T1_signature@R256", "image": "T1_signature.png", "resolution": 256},
    {"name": "T2_icon@R256", "image": "T2_icon.png", "resolution": 256},
    {"name": "T3_flat_illustration@R256", "image": "T3_flat_illustration.png", "resolution": 256},
    {"name": "T4_taper@R256", "image": "T4_taper_thick_to_thin.png", "resolution": 256},
    {"name": "T5_sharp_corners@R256", "image": "T5_sharp_corners.png", "resolution": 256},
    {"name": "T12_color_gradient@R256", "image": "T12_color_gradient.png", "resolution": 256},
    {"name": "T13_step_intervals@R256", "image": "T13_step_intervals.png", "resolution": 256},
]
#: 额外分支覆盖（不进 7-case 判据，但同样记录/比对）：分辨率 + mesh/painterly method 分支，
#: 专门盯 `fill_soft_px` auto / mesh 硬边 / painterly·mesh method 三条覆盖逻辑。
EXTRAS: list[dict] = [
    {"name": "T1_signature@R512", "image": "T1_signature.png", "resolution": 512},
    {"name": "T2_icon@R256+mesh", "image": "T2_icon.png", "resolution": 256, "cfg": {"mesh": True}},
    {"name": "T1_signature@R256+painterly", "image": "T1_signature.png", "resolution": 256,
     "cfg": {"painterly": True, "brush_strokes": 300}},
]
GATE_FIELDS = ("doc_sha256", "png_sha256", "fbs_bytes", "fbs_content_sha256")


# ------------------------------------------------------------------ 工具
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_doc(doc: dict) -> dict:
    """黄金口径：深拷贝后剔除唯一非确定性字段 `created` 与绝对路径 `source.path`。"""
    out = json.loads(json.dumps(doc, ensure_ascii=False, default=str))
    out.pop("created", None)
    src = out.get("source")
    if isinstance(src, dict):
        src.pop("path", None)
    return out


def _env() -> dict:
    return {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "platform": platform.platform(),
        "skill_root": ROOT,
        "testcases": TESTCASES,
    }


def run_case(case: dict, outdir: str) -> dict:
    """构建 → 归一化 doc 指纹 → 渲染像素指纹 → 写盘字节 → 复跑一次验确定性。"""
    image = os.path.join(TESTCASES, case["image"])
    cfg_kwargs = dict(case.get("cfg") or {})
    cfg_kwargs["target_resolution"] = int(case["resolution"])
    cfg = FBSConfig(**cfg_kwargs)

    def build() -> tuple[dict, float, list[str]]:
        warn = WarningLog()
        t0 = time.perf_counter()
        doc, _info = document.build_from_image(image, cfg, warn)
        elapsed = time.perf_counter() - t0
        codes = [str(w.get("code")) for w in doc.get("warnings", [])]
        return doc, elapsed, codes

    doc, build_s, codes = build()
    norm = normalize_doc(doc)
    doc_sha = sha256_json(norm)

    png = render.render(doc, resolution=cfg.target_resolution, supersample=cfg.supersample,
                        mode=cfg.render_mode)
    png_sha = _sha256_bytes(np.ascontiguousarray(png).tobytes())

    fbs_path = os.path.join(outdir, case["name"].replace("@", "_").replace("+", "_") + ".fbs.json")
    written = io_format.write_fbs(doc, fbs_path)
    quant_sha = sha256_json(normalize_doc(io_format.quantize_doc(doc)))

    # 同输入复跑：只允许 `created` 不同（已被 normalize 剔除）
    doc2, _build2_s, _codes2 = build()
    doc_repro = sha256_json(normalize_doc(doc2)) == doc_sha
    png2 = render.render(doc2, resolution=cfg.target_resolution, supersample=cfg.supersample,
                         mode=cfg.render_mode)
    png_repro = np.array_equal(np.asarray(png), np.asarray(png2))

    rec = {
        "name": case["name"],
        "image": case["image"],
        "resolution": int(case["resolution"]),
        "cfg": dict(case.get("cfg") or {}),
        "doc_sha256": doc_sha,
        "png_sha256": png_sha,
        "png_shape": [int(v) for v in png.shape],
        "fbs_bytes": int(written["bytes"]),
        "fbs_file_bytes": int(os.path.getsize(fbs_path)),
        "fbs_content_sha256": quant_sha,
        "stroke_count": int(doc["stats"]["stroke_count"]),
        "style_count": len(doc["brush_styles"]),
        "fft_passes": int(doc["stats"]["fft_passes"]),
        "fill_soft_px": float(doc["decomposition"]["params"].get("fill_soft_px", -1.0)),
        "method": str(doc["decomposition"]["method"]),
        "image_class": str(doc["image_class"].get("klass")),
        "warning_codes": codes,
        "build_seconds": round(float(build_s), 3),
        "repro": {"doc": bool(doc_repro), "png": bool(png_repro)},
    }
    return rec


def run_all(records: list[dict]) -> dict:
    out_cases = []
    with tempfile.TemporaryDirectory(prefix="fbs_baseline_") as outdir:
        for case in records:
            rec = run_case(case, outdir)
            out_cases.append(rec)
            print(f"[baseline] {rec['name']:<32} doc={rec['doc_sha256'][:12]} "
                  f"png={rec['png_sha256'][:12]} bytes={rec['fbs_bytes']} "
                  f"repro={rec['repro']} {rec['build_seconds']:.2f}s")
    return out_cases


def freeze(out_path: str) -> int:
    started = time.perf_counter()
    groups = {"cases": run_all(CASES), "extras": run_all(EXTRAS)}
    all_recs = groups["cases"] + groups["extras"]
    bad = [r["name"] for r in all_recs if not (r["repro"]["doc"] and r["repro"]["png"])]
    payload = {
        "schema": SCHEMA,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": "python tests/_baseline/freeze_baseline.py",
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "env": _env(),
        "cases": groups["cases"],
        "extras": groups["extras"],
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"[baseline] 冻结 {len(groups['cases'])} cases + {len(groups['extras'])} extras "
          f"→ {out_path}（{payload['elapsed_seconds']}s）")
    if bad:
        print(f"[baseline] FAIL: 同输入复跑不一致: {', '.join(bad)}")
        return 1
    return 0


def check(base_path: str) -> int:
    with open(base_path, "r", encoding="utf-8") as fh:
        base = json.load(fh)
    if base.get("schema") != SCHEMA:
        print(f"[baseline] 基线 schema 不匹配: {base.get('schema')!r} != {SCHEMA}")
        return 1
    failures: list[str] = []
    rows: list[tuple[str, str, str]] = []
    with tempfile.TemporaryDirectory(prefix="fbs_check_") as outdir:
        for group in ("cases", "extras"):
            old = {r["name"]: r for r in base.get(group, [])}
            for case in CASES if group == "cases" else EXTRAS:
                name = case["name"]
                if name not in old:
                    failures.append(f"{name}: 基线缺少该 case")
                    continue
                rec = run_case(case, outdir)
                diffs = [f for f in GATE_FIELDS if rec[f] != old[name][f]]
                repro_ok = rec["repro"]["doc"] and rec["repro"]["png"]
                status = "OK" if not diffs and repro_ok else "DIFF"
                if diffs:
                    failures.append(f"{name}: " + ", ".join(diffs))
                if not repro_ok:
                    failures.append(f"{name}: 复跑不确定 {rec['repro']}")
                rows.append((name, status, ",".join(diffs) if diffs else "-"))
                print(f"[check] {name:<32} {status:<4} {','.join(diffs) if diffs else 'identical'}")
    print("\n| case | 结果 | 差异字段 |")
    print("|---|---|---|")
    for name, status, diff in rows:
        print(f"| {name} | {status} | {diff} |")
    if failures:
        print(f"\n[check] FAIL（{len(failures)} 项）")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\n[check] PASS：{len(rows)} 项 doc/PNG/字节数/量化内容 全部与基线相等，且复跑确定")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="冻结/校验 fourier-brushstroke 构建黄金基线")
    ap.add_argument("--mode", default=None, choices=("freeze", "check"), help="默认 freeze")
    ap.add_argument("--out", default=None, help=f"基线路径（默认 {DEFAULT_OUT}）")
    args = ap.parse_args(argv)
    mode = args.mode or "freeze"
    out_path = args.out or DEFAULT_OUT
    return freeze(out_path) if mode == "freeze" else check(out_path)


if __name__ == "__main__":
    raise SystemExit(main())
