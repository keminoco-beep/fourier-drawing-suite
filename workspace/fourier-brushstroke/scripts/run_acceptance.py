"""验收台 :: fourier-brushstroke v2 的自动验收（A/B/R/T 四组共 40 项）。

定位（references/engineering_guardrails.md §8）
------------------------------------------------
这是**唯一**的验收口径：任何"A 比 B 好"的结论都必须来自这里与
``fbs_core.metrics.compare()``，禁止在别处临时造指标。

覆盖：
- ``A1–A15``（设计文档 v2.1 §7.1/7.2）：定量指标，MUST / SHOULD / MAY 分级；
- ``B1–B3``（§7.3）：可测代理（B4/B5 属文档定性项，不在本套件里冒充已验证）；
- ``R2–R8``（交接说明 §2）：需求专项（单次 FFT / 视频 / 笔刷属性 / sweep 线 / 连续渐变 /
  分层顺序 / 高谐波精度）；
- ``T1–T13``（§7.4）：合成用例逐例处理结果（成功建图 / 明确拒绝 / 降级告警）。

纪律：
1. **禁止静默跳过**：跑不了就 BLOCKED（缺模块/接口），按设计不跑就 SKIP（如 ``--quick``
   下的视频项），两者都写进报告与 stdout，绝不当作通过。
2. **失败打印第一处差异**：实测值 vs 阈值 + 可复现命令，进程退出码非 0。
3. 同分辨率 / 同契约：判断一律走 metrics.compare()，参考图缺省按 LANCZOS 重采样到
   渲染分辨率（只重采样参考侧、两侧同分辨率），原生像素口径由 metrics 的同分辨率闸门保证。
4. ``--quick`` 用小分辨率代理并在 note 里明确标注"代理"，不冒充全量结论。

用法：
    python scripts/run_acceptance.py --quick
    python scripts/run_acceptance.py --items A1,A7,R2
    python scripts/run_acceptance.py            # 全量（含真实图与 60s 视频）
    python scripts/fbs.py acceptance --quick
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

from fbs_core import attribute, config, io_format, metrics  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import FBSError, WarningLog, sha256_file  # noqa: E402

FIXTURES = os.path.join(ROOT, "assets", "testcases")
DEFAULT_REPORT_DIR = os.path.join(ROOT, "assets", "out", "acceptance")
# 真实图片样例改为环境变量提供（发布包不含本机路径；未设置时跳过对应验收项）：
#   FDP_ACC_HORN=<png 路径>   FDP_ACC_CHIBI=<png 路径>
_REAL_IMAGES = tuple(
    (name, os.environ[env])
    for name, env in (("horn", "FDP_ACC_HORN"), ("chibi", "FDP_ACC_CHIBI"))
    if os.environ.get(env)
)
MAX_HARMONICS = 65536
_TZ = timezone(timedelta(hours=8))

PASS, FAIL, SKIP, BLOCKED = "PASS", "FAIL", "SKIP", "BLOCKED"
#: BROKEN = 判据修正记录（旧判据已证伪/废止）；NOT_IMPLEMENTED = 设计文档要求但 v2 明确不实现的功能。
#: 两者都不算通过、不算失败，必须在报告单列。
BROKEN = "BROKEN"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
STATUS_MARK = {PASS: "[PASS]", FAIL: "[FAIL]", SKIP: "[SKIP]", BLOCKED: "[BLOCK]",
               BROKEN: "[BROKEN]", NOT_IMPLEMENTED: "[N/I]"}

# 模块就绪探测表：缺模块/接口的验收项一律 BLOCKED，而不是 FAIL 或静默跳过。
MODULE_NEEDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "document": ("fbs_core.document", ("build_from_image", "attach_quality", "write_report")),
    "render": ("fbs_core.render", ("render", "export_svg")),
    "onestroke": ("fbs_core.onestroke", ("build_one_stroke", "build_path")),
    "video": ("fbs_core.video", ("make_video", "probe_video")),
    "fourier": ("fbs_core.fourier", ("eval_coef", "eval_curve")),
}


# ===================================================================== 工具
class Blocked(RuntimeError):
    """依赖未就绪（模块缺失/接口未实现）：记 BLOCKED，不当通过也不当失败。"""


def _jsonable(obj):
    """递归转成 JSON 可序列化对象（numpy → 原生；NaN/inf → 字符串）。"""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        val = float(obj)
        return val if math.isfinite(val) else str(val)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    return obj


def _verdict(summary: dict) -> str:
    """报告结论：区分「项目级通过（MUST 全过）」与「有 MUST 失败」。"""
    if summary.get("ok"):
        return "✅ 通过（全部 PASS）"
    if summary.get("blocked"):
        return "⏸ 不完整（存在 BLOCKED，验收没跑完）"
    if summary.get("must_failed") == 0:
        return (f"✅ 项目级通过（MUST {summary.get('must_passed')}/{summary.get('must_total')} 全过；"
                f"非 MUST 失败 {summary.get('failed')} 项，见下方失败项）")
    return f"❌ 失败（MUST 失败 {summary.get('must_failed')} 项）"


def _short(value, limit: int = 160) -> str:
    """实测值转短字符串（报告表格用）。"""
    if value is None:
        return "—"
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(_jsonable(value), ensure_ascii=False, default=str)
        except TypeError:
            text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _py() -> str:
    return sys.executable


def _resize_ref(ref01: np.ndarray, width: int, height: int) -> np.ndarray:
    """把参考图重采样到渲染分辨率（只重采样参考侧；两侧最终同分辨率）。"""
    from PIL import Image

    if ref01.shape[1] == width and ref01.shape[0] == height:
        return ref01.astype(np.float64)
    u8 = np.clip(ref01 * 255.0 + 0.5, 0, 255).astype(np.uint8)
    im = Image.fromarray(u8).resize((int(width), int(height)), Image.LANCZOS)
    return np.asarray(im, dtype=np.float64) / 255.0


def _save_png(rgb: np.ndarray, path: str) -> None:
    from PIL import Image

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    Image.fromarray(np.ascontiguousarray(np.clip(rgb, 0, 255).astype(np.uint8))).save(path)


def _run_subprocess(argv: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess:
    """统一子进程调用：强制 UTF-8 管道编码（Windows 控制台默认 GBK 会解码失败）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd, env=env)


def _reset_world() -> None:
    """让 build_from_image 的确定性不受调用方 seed 影响（模块内部应自己固定）。"""
    np.random.seed(0)


# ===================================================================== 上下文
class Built:
    """一次 build 的完整缓存：文档 + 渲染 + 同分辨率参考 + 四维度量。"""

    __slots__ = ("key", "source_path", "doc", "info", "render", "reference", "bg",
                 "metrics", "fbs_path", "png_path", "report_path", "elapsed", "warnings")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def resolution(self) -> int:
        return int(max(self.render.shape[:2]))

    @property
    def long_side(self) -> int:
        return int(max(self.info["reference_rgb"].shape[:2]))

    def warning_codes(self) -> list[str]:
        return [str(w.get("code")) for w in (self.doc.get("warnings") or [])]


class Ctx:
    """一次验收运行的上下文：夹具、缓存、报告目录、耗时。"""

    def __init__(self, *, quick: bool, report_dir: str, skip_video: bool, verbose: bool = True):
        self.quick = bool(quick)
        self.report_dir = os.path.abspath(report_dir)
        self.cases_dir = os.path.join(self.report_dir, "cases")
        self.real_dir = os.path.join(self.report_dir, "real")
        self.skip_video = bool(skip_video)
        self.verbose = bool(verbose)
        # 最终验收口径（Lead 2026-10-18）：合成用例统一 R=512（快且足够），
        # A1/A2/A9 单独以 R=1024 正式跑（阈值本身按 @R1024 定义）。
        self.R = 512
        self.report_R = self.R
        self.builds: dict[tuple[str, int], Built] = {}
        self.one_docs: dict[tuple[str, int], dict] = {}
        self.errors: dict[tuple[str, int], Exception] = {}
        self.fixtures_manifest: dict = {}
        self.fixtures_note = ""
        self.preflight: dict[str, str] = {}
        self.timings: dict[str, float] = {}
        self.real: dict[str, dict] = {}
        self.notes: list[str] = []
        self.extra: dict = {}  # 跨项复用的中间结果（如 A6/A8 共用的 stamp/line 渲染对）
        os.makedirs(self.cases_dir, exist_ok=True)
        os.makedirs(self.real_dir, exist_ok=True)

    # ---------------------------------------------------------------- 环境
    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def probe(self) -> dict[str, str]:
        """探测各模块是否就绪；返回 {name: "ok"|"缺失原因"}。"""
        state: dict[str, str] = {}
        for name, (modname, attrs) in MODULE_NEEDS.items():
            try:
                mod = importlib.import_module(modname)
            except Exception as exc:  # noqa: BLE001 - 依赖探测：任何导入失败都记为未就绪
                state[name] = f"import {modname} 失败: {type(exc).__name__}: {exc}"
                continue
            missing = [a for a in attrs if not hasattr(mod, a)]
            state[name] = "ok" if not missing else f"{modname} 缺少接口: {', '.join(missing)}"
        cli = os.path.join(HERE, "fbs.py")
        state["cli"] = "ok" if os.path.exists(cli) else f"缺少 CLI: {cli}"
        self.preflight = state
        return state

    def require(self, *names: str) -> None:
        missing = [f"{n}({self.preflight.get(n, '未探测')})" for n in names
                   if self.preflight.get(n) != "ok"]
        if missing:
            raise Blocked("模块未就绪: " + "; ".join(missing))

    # ---------------------------------------------------------------- 夹具
    def ensure_fixtures(self) -> None:
        """夹具缺失/陈旧时自动重建（确定性生成脚本），记录用了哪个 manifest。"""
        gen = os.path.join(ROOT, "tests", "gen_fixtures.py")
        manifest = os.path.join(FIXTURES, "manifest.json")
        need = True
        if os.path.exists(manifest):
            proc = _run_subprocess([_py(), gen, "--check"])
            need = proc.returncode != 0
            if need:
                self.notes.append(f"夹具与 manifest 不一致，已重建（{proc.stdout.strip()[:200]}）")
        if need:
            if not os.path.exists(gen):
                raise Blocked(f"缺少夹具生成器: {gen}")
            proc = _run_subprocess([_py(), gen])
            if proc.returncode != 0:
                raise FBSError("E_FIXTURES", "合成夹具生成失败",
                               evidence={"stderr": (proc.stderr or "")[-800:]})
        with open(manifest, "r", encoding="utf-8") as fh:
            self.fixtures_manifest = json.load(fh)
        self.fixtures_note = (f"manifest seed={self.fixtures_manifest.get('seed')} "
                              f"count={self.fixtures_manifest.get('count')} "
                              f"schema={self.fixtures_manifest.get('schema')}")

    def fixture_file(self, case: str) -> str:
        """按 case 名（如 T4_taper_thick_to_thin）或文件名解析夹具路径。"""
        if os.path.isabs(case):
            return case
        for entry in self.fixtures_manifest.get("cases", []):
            if case in (entry["case"], entry["file"], os.path.splitext(entry["file"])[0]):
                return os.path.join(FIXTURES, entry["file"])
        path = os.path.join(FIXTURES, case)
        if os.path.exists(path):
            return path
        raise Blocked(f"夹具不存在: {case}（先跑 tests/gen_fixtures.py）")

    def fixture_spec(self, case: str) -> dict:
        for entry in self.fixtures_manifest.get("cases", []):
            if case in (entry["case"], entry["file"], os.path.splitext(entry["file"])[0]):
                with open(os.path.join(FIXTURES, entry["spec"]), "r", encoding="utf-8") as fh:
                    return json.load(fh)
        raise Blocked(f"夹具 spec 不存在: {case}")

    # ---------------------------------------------------------------- build
    def build(self, case: str, resolution: int | None = None) -> Built:
        """跑 Stage0–6：build_from_image → render → 同分辨率 metrics；带缓存。"""
        self.require("document", "render")
        path = self.fixture_file(case)
        R = int(resolution or self.R)
        key = (os.path.basename(path), R)
        if key in self.builds:
            return self.builds[key]
        if key in self.errors:
            raise self.errors[key]
        try:
            built = self._build_path(path, R, stem=os.path.splitext(os.path.basename(path))[0])
        except Exception as exc:
            self.errors[key] = exc
            raise
        self.builds[key] = built
        return built

    def _build_path(self, path: str, R: int, *, stem: str) -> Built:
        _reset_world()
        from fbs_core import document
        from fbs_core import render as render_mod

        outdir = os.path.join(self.cases_dir, f"{stem}_R{R}")
        os.makedirs(outdir, exist_ok=True)
        cfg = FBSConfig(target_resolution=int(R))
        warn = WarningLog()
        t0 = time.perf_counter()
        doc, info = document.build_from_image(path, cfg, warn)
        t_build = time.perf_counter() - t0
        t0 = time.perf_counter()
        render_rgb = render_mod.render(doc, resolution=int(R), supersample=cfg.supersample,
                                       mode=cfg.render_mode, warn=warn)
        t_render = time.perf_counter() - t0
        reference = _resize_ref(info["reference_rgb"], render_rgb.shape[1], render_rgb.shape[0])
        bg = np.asarray(doc.get("base_color", [255, 255, 255])[:3], dtype=np.float64) / 255.0
        t0 = time.perf_counter()
        m = metrics.compare(render_rgb, reference, doc=doc, bg=bg)
        t_metric = time.perf_counter() - t0
        document.attach_quality(doc, m)
        # 合并警告而不是覆盖：document.build_from_image 已把 W_SOLID_COLOR 等写进 doc["warnings"]，
        # 这里若直接覆盖会丢掉它们，导致纯色图在验收台内写盘必现 E_FBS_INVALID（生产 CLI 正常）。
        _merged: dict[tuple, dict] = {}
        for _w in (doc.get("warnings") or []) + warn.to_list():
            if isinstance(_w, dict) and _w.get("code"):
                _merged[(_w.get("code"), _w.get("message"))] = _w
        doc["warnings"] = list(_merged.values())
        fbs_path = os.path.join(outdir, f"{stem}.fbs.json")
        png_path = os.path.join(outdir, f"{stem}_R{R}.png")
        report_path = os.path.join(outdir, f"{stem}.report.json")
        try:
            io_format.write_fbs(doc, fbs_path)
            _save_png(render_rgb, png_path)
            document.write_report(report_path, doc=doc, info=info, metrics=m,
                                  files={"fbs": fbs_path, "png": png_path},
                                  timings={"stage_build": t_build, "render": t_render,
                                           "metrics": t_metric})
        except FBSError as exc:
            self.notes.append(f"{stem} R={R} 写盘失败: {exc.code}: {exc.message}")
            raise
        elapsed = t_build + t_render + t_metric
        built = Built(key=(stem, R), source_path=path, doc=doc, info=info,
                      render=render_rgb, reference=reference, bg=bg, metrics=m,
                      fbs_path=fbs_path, png_path=png_path, report_path=report_path,
                      elapsed=elapsed, warnings=warn.to_list())
        self.timings[f"build:{stem}@R{R}"] = round(t_build + t_render + t_metric, 3)
        self.log(f"    [build] {stem} @R{R}: strokes={doc['stats']['stroke_count']} "
                 f"psnr={m['psnr']:.2f} ssim={m['ssim']:.4f} "
                 f"({t_build:.1f}s+{t_render:.1f}s+{t_metric:.1f}s)")
        return built

    def build_external(self, name: str, path: str, resolution: int) -> Built:
        """真实图 build（不走夹具索引）。"""
        self.require("document", "render")
        key = (f"real:{name}", int(resolution))
        if key in self.builds:
            return self.builds[key]
        if key in self.errors:
            raise self.errors[key]
        stem = f"real_{name}"
        try:
            built = self._build_path(path, int(resolution), stem=stem)
        except Exception as exc:
            self.errors[key] = exc
            raise
        self.builds[key] = built
        return built


# ===================================================================== 通用断言/评估辅助
def _truth_width_px(spec: dict, ts: np.ndarray) -> np.ndarray | None:
    """按 spec 的 width_px 真值求宽度（px）。支持 linear_keys / step / constant。"""
    stroke = spec.get("stroke") or {}
    w = stroke.get("width_px") or {}
    mode = w.get("mode")
    if mode == "linear_keys":
        keys = np.asarray(w["keys"], dtype=np.float64)
        return np.interp(ts, keys[:, 0], keys[:, 1])
    if mode == "step":
        keys = np.asarray(w["keys"], dtype=np.float64)
        idx = np.clip(np.searchsorted(keys[:, 0], ts, side="right") - 1, 0, len(keys) - 1)
        return keys[:, 1][idx]
    if mode == "constant":
        return np.full_like(ts, float(w.get("value", 0.0)))
    return None


def _longest_medial(doc: dict) -> dict | None:
    cands = [s for s in doc.get("strokes", []) if s.get("fill_role") == "medial_branch"]
    if not cands:
        return None
    return max(cands, key=lambda s: float(s.get("arc_length", 0.0) or 0.0))


def _eval_width_px(doc: dict, stroke: dict, long_side: float, ts: np.ndarray) -> np.ndarray:
    curve = (stroke.get("brush") or {}).get("width")
    world = attribute.eval_numeric(curve, ts)
    return np.asarray(world, dtype=np.float64) * float(long_side)


def _rel_errors(got: np.ndarray, truth: np.ndarray) -> np.ndarray:
    return np.abs(got - truth) / np.maximum(np.abs(truth), 1e-9)


def _best_direction(got: np.ndarray, truth: np.ndarray) -> tuple[np.ndarray, str]:
    """t 方向可能被反演器反向（弧长参数方向不唯一）；取更优方向并记录。"""
    ef = _rel_errors(got, truth)
    er = _rel_errors(got[::-1], truth)
    if float(np.max(er)) < float(np.max(ef)):
        return er, "reversed"
    return ef, "forward"


def _width_inversion(ctx: Ctx, case: str, *, n_points: int = 32,
                     t_lo: float = 0.03, t_hi: float = 0.97) -> dict:
    """宽度反演评估：把 doc 的中轴笔画按空间投影回**真值中心线**，再逐点比宽度。

    为什么不是"取最长笔画、t 直接对 t"：分解可能把一条锥形笔画切成多段，每段的
    局部 t ∈[0,1] 与真值弧长 t 不是同一参数化（旧版/新管线都可能发生）。直接对齐
    t 会把"碎片化"误报成"宽度反演错误"。这里用笔画中心线点到真值中心线的投影
    距离得到每个采样点的真值 t（投影距离过大的样本剔除并记录），从而只考核宽度
    属性本身。spec 没有 centerline_px 时回退到"最长笔画 + t 对齐"并注明口径。
    """
    ctx.require("fourier")
    built = ctx.build(case)
    spec = ctx.fixture_spec(case)
    truth_keys = _truth_width_px(spec, np.array([0.0]))  # 仅用于检查真值是否存在
    if truth_keys is None:
        raise Blocked(f"{case} spec 未提供 width_px 真值")
    stroke_spec = (spec.get("stroke") or {})
    centerline = stroke_spec.get("centerline_px")
    medial = [s for s in built.doc.get("strokes", []) if s.get("fill_role") == "medial_branch"]
    if not medial:
        raise FBErrorAdapt("文档中没有 medial_branch 笔画，无法做宽度反演")
    if centerline:
        cl = np.asarray(centerline, dtype=np.float64).reshape(-1, 2)
        seg = np.linalg.norm(np.diff(cl, axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = float(cum[-1])
        us: list[np.ndarray] = []
        gots: list[np.ndarray] = []
        dists: list[np.ndarray] = []
        per_stroke: list[dict] = []
        n_raw = 0
        for st in medial:
            local_ts = np.linspace(t_lo, t_hi, int(n_points))
            pts_px = _curve_points_px(built.doc, st, built.long_side, local_ts)
            widths = _eval_width_px(built.doc, st, built.long_side, local_ts)
            a, b = cl[:-1], cl[1:]
            ab = b - a
            seg2 = np.maximum((ab * ab).sum(axis=1), 1e-12)
            u = np.zeros(len(pts_px))
            dist = np.zeros(len(pts_px))
            for i, p in enumerate(pts_px):
                tt = np.clip(((p - a) * ab).sum(axis=1) / seg2, 0.0, 1.0)
                proj = a + tt[:, None] * ab
                d = np.linalg.norm(proj - p[None, :], axis=1)
                j = int(np.argmin(d))
                u[i] = (cum[j] + tt[j] * seg[j]) / max(total, 1e-9)
                dist[i] = float(d[j])
            # 允许的投影偏差由局部宽度导出（亚像素中心线抽取误差 + 少量抖动）
            allowed = 2.0 + 0.25 * widths
            keep = (u >= t_lo) & (u <= t_hi) & (dist <= allowed)
            n_raw += len(pts_px)
            per_stroke.append({"stroke_id": st.get("id"), "n_samples": int(len(pts_px)),
                               "kept": int(keep.sum()), "max_proj_dist_px": round(float(dist.max()), 3)})
            us.append(u[keep])
            gots.append(widths[keep])
            dists.append(dist[keep])
        u_all = np.concatenate(us) if us else np.zeros(0)
        got_all = np.concatenate(gots) if gots else np.zeros(0)
        if u_all.size < 8:
            raise FBErrorAdapt(f"{case} 投影后有效宽度样本不足（{u_all.size}<8），无法做反演评估")
        truth_all = _truth_width_px(spec, u_all)
        errs = _rel_errors(got_all, truth_all)
        bins = np.clip((u_all * 16).astype(int), 0, 15)
        # A13 设计口径是"沿 t 取 32 点逐一比对"：按真值弧长 32 等分点取最近投影样本
        t32 = np.linspace(t_lo, t_hi, 32)
        errs32: list[float] = []
        missing32 = 0
        for tv in t32:
            sel = np.abs(u_all - tv) <= 0.02
            if not sel.any():
                missing32 += 1
                continue
            truth_v = float(_truth_width_px(spec, np.array([tv]))[0])
            errs32.append(float(np.min(np.abs(got_all[sel] - truth_v) / max(abs(truth_v), 1e-9))))
        return {
            "case": case,
            "R": built.resolution,
            "method": "projected_centerline",
            "direction": "n/a（按空间投影对齐，无需方向假设）",
            "n_medial_strokes": len(medial),
            "n_samples_used": int(u_all.size),
            "n_samples_raw": int(n_raw),
            "n_samples_rejected": int(n_raw - u_all.size),
            "truth_coverage_bins": f"{len(set(bins.tolist()))}/16",
            "max_rel_err_pct": round(float(np.max(errs)) * 100.0, 3),
            "mean_rel_err_pct": round(float(np.mean(errs)) * 100.0, 3),
            "median_rel_err_pct": round(float(np.median(errs)) * 100.0, 3),
            "max_rel_err_pct_32pt": round(max(errs32) * 100.0, 3) if errs32 else None,
            "points_32pt": len(errs32),
            "missing_32pt": int(missing32),
            "max_abs_err_px": round(float(np.max(np.abs(got_all - truth_all))), 3),
            "mean_proj_dist_px": round(float(np.mean(np.concatenate(dists))), 3) if dists else None,
            "per_stroke": per_stroke,
            "note": "把每个分支的傅里叶中心线采样点投影到真值中心线得到真值弧长 t；"
                    "投影距离 > max(2px, 2+0.25·width) 的样本剔除（记录 rejected 数）；"
                    "max_rel_err_pct_32pt = 设计口径的 32 个真值 t 等分点（±0.02 窗口取最近样本）",
        }
    # ---- 回退：无真值中心线 → 最长笔画 + t 对齐（旧口径） -----------------
    stroke = _longest_medial(built.doc)
    ts = np.linspace(t_lo, t_hi, int(n_points))
    truth = _truth_width_px(spec, ts)
    got = _eval_width_px(built.doc, stroke, built.long_side, ts)
    errs, direction = _best_direction(got, truth)
    return {
        "case": case,
        "R": built.resolution,
        "method": "longest_stroke_t_align（spec 缺 centerline_px，旧口径）",
        "stroke_id": stroke.get("id"),
        "direction": direction,
        "n_points": int(n_points),
        "t_range": [t_lo, t_hi],
        "max_rel_err_pct": round(float(np.max(errs)) * 100.0, 3),
        "mean_rel_err_pct": round(float(np.mean(errs)) * 100.0, 3),
        "max_abs_err_px": round(float(np.max(np.abs(got - truth))), 3)
        if direction == "forward" else round(float(np.max(np.abs(got[::-1] - truth))), 3),
        "truth_px_at_t": [round(float(v), 2) for v in truth],
        "got_px_at_t": [round(float(v), 2) for v in (got if direction == "forward" else got[::-1])],
        "spec_keys": stroke_spec.get("width_px"),
    }


def _curve_points_px(doc: dict, stroke: dict, long_side: float, ts: np.ndarray) -> np.ndarray:
    """把一个笔画的傅里叶中心线在 drawable t 上求值，转成源图像素坐标 (M,2)。"""
    from fbs_core import fourier

    coef = np.asarray(stroke["coef"], dtype=np.float64)
    closure = stroke.get("closure") or {"kind": "none"}
    world = np.asarray(fourier.eval_curve(coef, closure, ts), dtype=np.float64)
    return world * float(long_side)


class FBErrorAdapt(Exception):
    """验收语义错误（数据不满足前提），包装成 FAIL 而不是 BLOCKED。"""


def _modes_case(ctx: Ctx) -> tuple[Built, str, str]:
    """A6/A8 的载体选择：优先 T5b（**带尖角折返的 medial 笔画真值图**），再 T5，最后 T4。

    为什么优先 T5b：region_fill 的渲染与 mode 无关（显式多边形填充），只有 medial_branch
    才能测出 stamp / sweep / line 三种渲染基元的差异。T5b 是专门为此生成的"尖角折返
    变宽笔画"真值图（spec 里有 centerline 与 width 真值）。
    """
    for case, fallback_note in (
        ("T5b_sharp_fold_stroke.png", ""),
        ("T5_sharp_corners.png", "T5b 不可用，回退 T5"),
        ("T4_taper_thick_to_thin.png", "T5b/T5 均无可用 medial_branch，回退 T4 变宽笔画"),
    ):
        try:
            built = ctx.build(case, resolution=min(ctx.R, 512))
        except FBSError:
            continue
        medial = [s for s in built.doc.get("strokes", []) if s.get("fill_role") == "medial_branch"]
        if medial:
            return built, case, fallback_note
    raise Blocked("T5b/T5/T4 文档都没有 medial_branch 笔画，stamp/line 渲染模式无差异可测")


def _render_pair(ctx: Ctx) -> tuple[Built, str, dict]:
    """同一 doc 用 stamp / line 两条渲染路径各渲一次（A6/A8 共用，结果缓存在 ctx.extra）。"""
    cached = ctx.extra.get("render_pair") if hasattr(ctx, "extra") else None
    if cached is not None:
        return cached
    ctx.require("render")
    from fbs_core import render as render_mod

    built, case, fallback = _modes_case(ctx)
    R = built.resolution
    try:
        img_stamp = render_mod.render(built.doc, resolution=R, supersample=1, mode="stamp")
        img_line = render_mod.render(built.doc, resolution=R, supersample=1, mode="line")
    except FBSError as exc:
        raise Blocked(f"render 缺少 stamp/line 模式: {exc.code}: {exc.message}") from exc
    except TypeError as exc:
        raise Blocked(f"render 接口不支持 mode=stamp/line: {exc}") from exc
    pair = (built, case, {"stamp": img_stamp, "line": img_line, "fallback": fallback, "R": R})
    if hasattr(ctx, "extra"):
        ctx.extra["render_pair"] = pair
    return pair


def _boundary_p99(mask_a: np.ndarray, mask_b: np.ndarray) -> float:
    """两个墨迹掩码边界互差（像素，99 分位；无差异返回 0）。"""
    if np.array_equal(mask_a, mask_b):
        return 0.0
    d_ab = metrics.distance_transform(~mask_b)[mask_a & ~mask_b]
    d_ba = metrics.distance_transform(~mask_a)[mask_b & ~mask_a]
    vals = np.concatenate([d_ab.ravel(), d_ba.ravel()])
    return float(np.percentile(vals, 99)) if vals.size else 0.0


def _mode_pair_stats(built: Built) -> dict:
    """给定 built，渲染 stamp/line 两模式，返回与参考/互差的统计（A8 诊断载体用）。"""
    from fbs_core import render as render_mod

    R = built.resolution
    imgs = {mode: render_mod.render(built.doc, resolution=R, supersample=1, mode=mode)
            for mode in ("stamp", "line")}
    bg = built.bg
    ref_mask = metrics.ink_mask(built.reference, metrics.DEFAULT_INK_TOL, bg)
    masks = {mode: metrics.ink_mask(imgs[mode], metrics.DEFAULT_INK_TOL, bg) for mode in imgs}

    def _iou(m: np.ndarray) -> float:
        inter = int((m & ref_mask).sum())
        union = int((m | ref_mask).sum())
        return inter / union if union else 1.0

    err_stamp = 1.0 - _iou(masks["stamp"])
    err_line = 1.0 - _iou(masks["line"])
    p99_stamp_ref = _boundary_p99(masks["stamp"], ref_mask)
    p99_line_ref = _boundary_p99(masks["line"], ref_mask)
    return {
        "R": R,
        "shape_err_stamp": round(err_stamp, 5), "shape_err_line": round(err_line, 5),
        "ratio_stamp_over_line": round(err_stamp / err_line, 4) if err_line > 0 else None,
        "p99_stamp_to_ref_px": round(p99_stamp_ref, 3),
        "p99_line_to_ref_px": round(p99_line_ref, 3),
        "p99_stamp_vs_line_px": round(_boundary_p99(masks["stamp"], masks["line"]), 3),
        "p99_max_to_ref_px": max(p99_stamp_ref, p99_line_ref),
    }


def _render_modes_for_shape_error(ctx: Ctx) -> dict:
    """A6/A8 共用：stamp / line 渲染 vs 参考墨迹的形状误差 + 绝对边界偏差。"""
    built, case, pair = _render_pair(ctx)
    bg = built.bg
    ref_mask = metrics.ink_mask(built.reference, metrics.DEFAULT_INK_TOL, bg)
    masks = {name: metrics.ink_mask(pair[name], metrics.DEFAULT_INK_TOL, bg) for name in ("stamp", "line")}

    def shape_err(m: np.ndarray) -> tuple[float, float]:
        inter = int((m & ref_mask).sum())
        union = int((m | ref_mask).sum())
        iou = inter / union if union else 1.0
        return 1.0 - iou, iou

    err_stamp, iou_stamp = shape_err(masks["stamp"])
    err_line, iou_line = shape_err(masks["line"])
    med = [s for s in built.doc.get("strokes", []) if s.get("fill_role") == "medial_branch"]
    return {
        "case": case, "R": pair["R"], "fallback_note": pair["fallback"],
        "medial_strokes": len(med),
        "shape_err_stamp": round(err_stamp, 5), "iou_stamp": round(iou_stamp, 5),
        "shape_err_line": round(err_line, 5), "iou_line": round(iou_line, 5),
        "ratio_stamp_over_line": round(err_stamp / err_line, 4) if err_line > 0 else None,
        "p99_stamp_to_ref_px": round(_boundary_p99(masks["stamp"], ref_mask), 3),
        "p99_line_to_ref_px": round(_boundary_p99(masks["line"], ref_mask), 3),
        "p99_stamp_vs_line_px": round(_boundary_p99(masks["stamp"], masks["line"]), 3),
    }


def _continuity_check(ctx: Ctx) -> dict:
    """R6：宽度（T4）与颜色（T12）沿 t 的连续性；step 曲线（T13）不在本项考核范围。"""
    b4 = ctx.build("T4_taper_thick_to_thin")
    st4 = _longest_medial(b4.doc)
    if st4 is None:
        raise FBErrorAdapt("T4 没有 medial_branch 笔画")
    ts = np.linspace(0.0, 1.0, 65)
    w = _eval_width_px(b4.doc, st4, b4.long_side, ts)
    jumps = np.abs(np.diff(w)) / max(float(np.mean(w)), 1e-9)
    max_w_jump = float(np.max(jumps)) if jumps.size else 0.0

    b12 = ctx.build("T12_color_gradient")
    st12 = _longest_medial(b12.doc)
    if st12 is None:
        raise FBErrorAdapt("T12 没有 medial_branch 笔画")
    style_idx = int((st12.get("brush") or {}).get("style", 0))
    style = b12.doc.get("brush_styles", [])[style_idx]
    rgb, _alpha = attribute.eval_color(style.get("color"), ts)  # rgb 0..1
    de = np.sqrt(((metrics.srgb_to_lab(rgb[:-1]) - metrics.srgb_to_lab(rgb[1:])) ** 2).sum(axis=1))
    max_color_jump = float(np.max(de)) if de.size else 0.0

    return {
        "width_curve_interp": (st4["brush"]["width"] or {}).get("interp"),
        "width_max_jump_ratio": round(max_w_jump, 5),
        "width_median_px": round(float(np.median(w)), 3),
        "color_curve_interp": (style.get("color") or {}).get("interp"),
        "color_max_delta_e_step": round(max_color_jump, 3),
        "note": "65 点采样；Δwidth/mean(width) 与相邻 ΔE；T13 step 区间不在此项考核（A15 负责）",
    }


def _step_semantics(ctx: Ctx) -> dict:
    """A15/T13：T13 恢复的宽度曲线必须保留 step 区间语义（**判据已修正**）。

    前提纠正（Lead 2026-10-18，verifier 独立复现）：中轴分解的分支 t 方向任意，
    几何 coef 与 width 曲线**同向反转**时渲染自洽正确；"t 方向与原始笔画方向一致"
    是未证明的假前提。因此：
    - 同时计算 `err_fwd = |w(t) − truth(t)|` 与 `err_rev = |w(t) − truth(1−t)|`，
      按**全曲线相对误差**取更优方向并记录 `orientation`；
    - 边界 t 检查在同一 orientation 下做；
    - ``interp="step"`` 仅作"更优则记"的偏好项（自动反演用 linear 保持保真度是允许的），
      不判 FAIL；step 语义的硬约束由 A15 属性层单测保证。
    """
    built = ctx.build("T13_step_intervals")
    spec = ctx.fixture_spec("T13_step_intervals")
    truth = spec.get("step_truth") or {}
    stroke = _longest_medial(built.doc)
    if stroke is None:
        raise FBErrorAdapt("T13 没有 medial_branch 笔画")
    curve = (stroke.get("brush") or {}).get("width") or {}
    ts = np.linspace(0.0, 1.0, 101)
    w = _eval_width_px(built.doc, stroke, built.long_side, ts)
    before_truth = float(truth["before_width_px"])
    after_truth = float(truth["after_width_px"])
    mid = 0.5 * (before_truth + after_truth)

    def _rel_err(arr: np.ndarray, truth_fn) -> float:
        ref = np.asarray(truth_fn(ts), dtype=np.float64)
        return float(np.mean(np.abs(arr - ref) / np.maximum(np.abs(ref), 1e-9)))

    # 两个方向各自的真值剖面（step 真值只有两个值，反向后等价于对调）
    err_fwd = _rel_err(w, lambda t: _truth_width_px(spec, t))
    err_rev = _rel_err(w, lambda t: _truth_width_px(spec, 1.0 - t))
    if err_rev < err_fwd:
        orientation = "reversed"
        w_used = w[::-1]
        err_used = err_rev
    else:
        orientation = "forward"
        w_used = w
        err_used = err_fwd

    before = float(np.interp(truth["before_t"], ts, w_used))
    after = float(np.interp(truth["after_t"], ts, w_used))
    below = np.nonzero(w_used < mid)[0]
    boundary = float(ts[below[0]]) if below.size else None
    interp = str(curve.get("interp") or "")
    return {
        "case": "T13",
        "orientation": orientation,
        "oriented_err_fwd_pct": round(err_fwd * 100.0, 3),
        "oriented_err_rev_pct": round(err_rev * 100.0, 3),
        "oriented_mean_rel_err_pct": round(err_used * 100.0, 3),
        "width_interp": interp,
        "interp_step_preferred": bool(interp == "step"),
        "w_t0.25_px": round(before, 3), "truth_t0.25_px": before_truth,
        "w_t0.75_px": round(after, 3), "truth_t0.75_px": after_truth,
        "before_rel_err_pct": round(abs(before - before_truth) / max(before_truth, 1e-9) * 100, 2),
        "after_rel_err_pct": round(abs(after - after_truth) / max(after_truth, 1e-9) * 100, 2),
        "boundary_t": None if boundary is None else round(boundary, 3),
        "boundary_expected": float(truth.get("boundary_t", 0.5)),
        "note": "方向任意性已按 Lead 裁定修正：两种方向取优并记录 orientation；"
                "interp=step 仅偏好记录，不判 FAIL（attribute 层已硬验 step 语义）",
    }


def _a15_attribute_checks() -> dict:
    """A15 属性层：6 种 interp + 颜色渐变 + step 区间语义（不依赖流水线）。"""
    t = np.linspace(0.0, 1.0, 201)
    checks: dict[str, bool] = {}
    checks["constant"] = bool(np.allclose(
        attribute.eval_numeric({"interp": "constant", "keys": [[0.0, 0.3]]}, t), 0.3))
    checks["linear"] = bool(abs(float(attribute.eval_numeric(
        {"interp": "linear", "keys": [[0.0, 0.0], [1.0, 1.0]]}, [0.5])[0]) - 0.5) < 1e-9)
    checks["spline"] = bool(abs(float(attribute.eval_numeric(
        {"interp": "spline", "keys": [[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]]}, [0.5])[0]) - 1.0) < 1e-6)
    stp = attribute.eval_numeric({"interp": "step", "keys": [[0.0, 1.0], [0.5, 0.2]]},
                                 [0.1, 0.49, 0.51, 0.99])
    checks["step"] = bool(np.allclose(stp, [1.0, 1.0, 0.2, 0.2]))
    checks["fourier"] = bool(abs(float(attribute.eval_numeric(
        {"interp": "fourier", "coef": [[0, 0.5, 0.0], [1, 0.25, 0.0]]}, [0.0])[0]) - 0.75) < 1e-9)
    pv = attribute.eval_numeric(
        {"interp": "preset", "preset": "taper-out", "scale": 1.0, "strength": 0.8}, [0.0, 1.0])
    checks["preset"] = bool(abs(float(pv[0]) - 1.0) < 1e-9 and abs(float(pv[1]) - 0.2) < 1e-9)
    rgb, al = attribute.eval_color(
        {"interp": "linear",
         "stops": [{"t": 0.0, "rgb": [255, 0, 0], "a": 1.0},
                   {"t": 1.0, "rgb": [0, 0, 255], "a": 0.5}]}, [0.5])
    checks["color_gradient"] = bool(abs(float(rgb[0, 0]) - 0.5) < 0.01
                                    and abs(float(rgb[0, 2]) - 0.5) < 0.01
                                    and abs(float(al[0]) - 0.75) < 1e-6)
    sh = attribute.eval_shape({"interp": "step", "keys": [[0.0, "round"], [0.7, "marker"]]},
                              [0.1, 0.9])
    checks["shape_curve"] = sh == ["round", "marker"]
    return checks


# ===================================================================== 验收项注册
ITEMS: dict[str, dict] = {}


def item(iid: str, level: str, desc: str, *, needs: tuple[str, ...] = (), cmd: str = ""):
    def deco(fn):
        ITEMS[iid] = {"id": iid, "level": level, "desc": desc, "needs": needs,
                      "fn": fn, "cmd": cmd}
        return fn
    return deco


def _build_cmd(case: str, out: str, R: int | None = None) -> str:
    case_path = os.path.join("assets", "testcases", case)
    R = int(R if R is not None else 1024)
    return (f'"{_py()}" scripts/fbs.py build "{case_path}" '
            f'-o assets/out/acceptance/cases/{out} --resolution {R} --no-svg')


# ------------------------------------------------------------------ A 组
@item("A1", "MUST", "线稿类 PSNR @R=1024 ≥ 30dB（T1）",
      needs=("document", "render"), cmd=_build_cmd("T1_signature.png", "A1_T1", 1024))
def a1(ctx: Ctx) -> dict:
    R = 1024 if not ctx.quick else ctx.R
    b = ctx.build("T1_signature.png", resolution=R)
    value = {"psnr_db": b.metrics["psnr"], "R": b.resolution,
             "klass": b.doc.get("image_class", {}).get("klass")}
    threshold = "≥ 30 dB @R=1024"
    ok = float(b.metrics["psnr"]) >= 30.0
    note = "全量口径 @R=1024" if b.resolution == 1024 else f"quick 代理 R={b.resolution}（非 1024 全量口径）"
    return {"status": PASS if ok else FAIL, "value": value, "threshold": threshold, "note": note}


@item("A2", "MUST", "线稿类 SSIM @R=1024 ≥ 0.90（T1）",
      needs=("document", "render"), cmd=_build_cmd("T1_signature.png", "A2_T1", 1024))
def a2(ctx: Ctx) -> dict:
    R = 1024 if not ctx.quick else ctx.R
    b = ctx.build("T1_signature.png", resolution=R)
    ok = float(b.metrics["ssim"]) >= 0.90
    note = "全量口径 @R=1024" if b.resolution == 1024 else f"quick 代理 R={b.resolution}"
    return {"status": PASS if ok else FAIL,
            "value": {"ssim": b.metrics["ssim"], "R": b.resolution},
            "threshold": "≥ 0.90 @R=1024", "note": note}


@item("A3", "MUST", "N–R 契约：max_err·R≤ε 且 ≥0.5ε，N 随 R 单调增（T4）",
      needs=("document", "render"),
      cmd="python scripts/run_acceptance.py --items A3")
def a3(ctx: Ctx) -> dict:
    Rs = (256, 512) if ctx.quick else (256, 512, 1024)
    stats: dict[int, dict] = {}
    for R in Rs:
        b = ctx.build("T4_taper_thick_to_thin.png", resolution=R)
        ns = [int(s.get("N", 0)) for s in b.doc.get("strokes", []) if s.get("N")]
        errs = [float(s.get("max_err", 0.0)) for s in b.doc.get("strokes", []) if "max_err" in s]
        eps = float(b.doc["render_contract"]["epsilon_px"])
        stats[R] = {
            "N_median": int(np.median(ns)) if ns else 0,
            "err_max_x_R": round(max(errs) * R, 4) if errs else None,
            "eps": eps,
        }
    ok_contract = all(stats[R]["err_max_x_R"] is not None and stats[R]["err_max_x_R"] <= stats[R]["eps"]
                      for R in Rs)
    ok_tight = all(stats[R]["err_max_x_R"] is not None and stats[R]["err_max_x_R"] >= 0.5 * stats[R]["eps"]
                   for R in Rs)
    ns_sorted = [stats[R]["N_median"] for R in Rs]
    ok_mono = all(ns_sorted[i] <= ns_sorted[i + 1] for i in range(len(ns_sorted) - 1)) \
        and ns_sorted[0] < ns_sorted[-1]
    diagnostic = (ns_sorted[-1] / max(ns_sorted[0], 1), Rs[0], Rs[-1])
    return {
        "status": PASS if (ok_contract and ok_tight and ok_mono) else FAIL,
        "value": {"stats": stats, "contract": ok_contract, "tightness": ok_tight,
                  "monotonic": ok_mono,
                  "diagnostic_N_ratio": round(diagnostic[0], 3),
                  "ratio_R_range": [diagnostic[1], diagnostic[2]]},
        "threshold": "max_err·R ≤ ε；max_err·R ≥ 0.5ε；N 中位随 R 单调增（严格）",
        "note": "判据按工程护栏 §3.2 修正：旧版「每档必 2×」依赖 d(N)≈C/N 单幂律前提，实测为假；"
                "现直接验契约+紧致+单调。诊断倍率仅供记录。",
        "cmd": _build_cmd("T4_taper_thick_to_thin.png", f"A3_R{Rs[-1]}", Rs[-1]),
    }


@item("A4", "MUST", "全流程零外部网络依赖（静态 AST 扫描）",
      cmd="python scripts/run_acceptance.py --items A4")
def a4(ctx: Ctx) -> dict:
    forbidden = {"requests", "urllib", "urllib3", "httpx", "aiohttp", "websockets",
                 "socket", "http", "ftplib", "poplib", "imaplib", "smtplib",
                 "telnetlib", "xmlrpc", "webbrowser"}
    hits: list[str] = []
    scan_dirs = [HERE, os.path.join(HERE, "fbs_core")]
    self_names = {"run_acceptance.py", "check_hygiene.py"}
    import ast

    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn in self_names:
                continue
            path = os.path.join(d, fn)
            try:
                tree = ast.parse(open(path, "r", encoding="utf-8").read(), filename=path)
            except SyntaxError as exc:
                hits.append(f"{fn}: 语法错误 {exc}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in forbidden:
                            hits.append(f"{fn}:{node.lineno} import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    if root in forbidden:
                        hits.append(f"{fn}:{node.lineno} from {node.module}")
    return {
        "status": PASS if not hits else FAIL,
        "value": {"hits": hits[:20], "scanned_dirs": [os.path.relpath(d, ROOT) for d in scan_dirs]},
        "threshold": "无任何网络库 import / 网络调用",
        "note": "AST 静态扫描（不实际断网）；import 网络库即视为违规",
    }


@item("A5", "MUST", "异常用例零崩溃且原因明确（照片/透明/损坏/过小/格式/纯色）",
      needs=("document",),
      cmd="python scripts/run_acceptance.py --items A5")
def a5(ctx: Ctx) -> dict:
    from fbs_core import document

    cases = [
        ("T7_photo_like.png", {"E_PHOTO_DEFERRED"}),
        ("T8c_fully_transparent.png", {"E_INPUT_TRANSPARENT"}),
        ("T10b_损坏文件.png", {"E_INPUT_CORRUPT", "E_INPUT_FORMAT"}),
        ("T10c_太小_8x8.png", {"E_INPUT_TOO_SMALL"}),
        ("T10d_不是图片.txt", {"E_INPUT_FORMAT", "E_INPUT_MODE"}),
        ("不存在的文件_xyz.png", {"E_INPUT_MISSING"}),
    ]
    details: list[dict] = []
    ok_all = True
    for fname, expect in cases:
        path = os.path.join(FIXTURES, fname) if not fname.startswith("不存在") else os.path.join(FIXTURES, fname)
        try:
            document.build_from_image(path, FBSConfig(target_resolution=128), WarningLog())
            details.append({"case": fname, "result": "未按预期拒绝", "expected": sorted(expect)})
            ok_all = False
        except FBSError as exc:
            good = exc.code in expect
            details.append({"case": fname, "result": exc.code, "expected": sorted(expect),
                            "ok": good, "message": exc.message[:120]})
            ok_all &= good
        except Exception as exc:  # noqa: BLE001 - 未捕获异常本身就是验收失败
            details.append({"case": fname, "result": f"未捕获异常 {type(exc).__name__}: {exc}",
                            "expected": sorted(expect)})
            ok_all = False
    # 纯色图：允许「特殊路径+告警」或「明确拒绝」，但不得未捕获异常
    for fname in ("T8a_solid_black.png", "T8b_solid_white.png"):
        path = os.path.join(FIXTURES, fname)
        try:
            doc, _info = document.build_from_image(path, FBSConfig(target_resolution=128), WarningLog())
            codes = [str(w.get("code")) for w in doc.get("warnings", [])]
            good = "W_SOLID_COLOR" in codes
            details.append({"case": fname, "result": f"built warnings={codes}", "expected": ["W_SOLID_COLOR (或明确拒绝)"],
                            "ok": good})
            ok_all &= good
        except FBSError as exc:
            details.append({"case": fname, "result": f"明确拒绝 {exc.code}", "expected": ["W_SOLID_COLOR (或明确拒绝)"],
                            "ok": True, "message": exc.message[:120]})
        except Exception as exc:  # noqa: BLE001
            details.append({"case": fname, "result": f"未捕获异常 {type(exc).__name__}: {exc}"})
            ok_all = False
    return {
        "status": PASS if ok_all else FAIL,
        "value": {"cases": details},
        "threshold": "全部异常用例以 FBSError(code) 明确拒绝；纯色图走特殊路径+W_SOLID_COLOR，0 崩溃",
        "note": "A5 = 设计文档 §6 异常矩阵的零崩溃代理；错误信息可读性见 T7 项（证据/建议非空）",
    }


@item("A6", "MUST", "双渲染后端一致性（Python vs HTML Canvas）—— v2 明确不实现第二后端",
      needs=(), cmd="python scripts/run_acceptance.py --items A6")
def a6(ctx: Ctx) -> dict:
    """设计文档 A6 要求两套渲染后端（Python/Pillow 与 HTML Canvas）最大偏差 <1px。

    v2 是干净重建：**只保留 Python 渲染器**，不实现 HTML Canvas 第二后端
    （SKILL.md / requirements_traceability 已声明不承诺）。因此本项不是"失败"，
    而是 NOT_IMPLEMENTED（不计失败，报告单列）。渲染一致性 sanity 由回归项 R5b
    （生产基元 sweep vs 语义参考 stamp）承担。
    """
    return {
        "status": NOT_IMPLEMENTED,
        "value": {"design_item": "双渲染后端一致性（Python vs HTML Canvas，最大偏差 <1px）",
                  "implemented_backends": ["python_numpy_pillow"],
                  "planned_backends": ["html_canvas"],
                  "v2_scope": "干净重建只保留 Python 后端；不承诺 HTML Canvas 后端",
                  "related_regression_item": "R5b（stamp vs sweep 生产基元一致性）"},
        "threshold": "设计 A6：两后端最大偏差 <1px（v2 无第二后端，无法也不承诺验证）",
        "note": "NOT_IMPLEMENTED：v2 明确不实现 HTML Canvas 第二后端（见 SKILL.md / "
                "requirements_traceability.md 的范围声明）。本项不计入失败；"
                "渲染几何一致性由 R5b 回归项覆盖",
    }


def _width_item(iid: str, level: str, mode: str, threshold: str, desc: str) -> None:
    @item(iid, level, desc, needs=("document", "render", "fourier"),
          cmd=_build_cmd("T4_taper_thick_to_thin.png", f"{iid}_T4", 512))
    def _fn(ctx: Ctx) -> dict:
        info = _width_inversion(ctx, "T4_taper_thick_to_thin.png", n_points=32)
        if mode == "mean":
            ok = info["mean_rel_err_pct"] < 5.0
        elif info.get("max_rel_err_pct_32pt") is not None:
            # 设计口径："沿 t 取 32 点逐一比对"（max 用 32 等分点，避免碎片化过采样放大极值）。
            # 允许 ≤2 个真值 t 点没有对应抽样（分解碎片间的窄缺口），但必须记录 missing。
            ok = info["max_rel_err_pct_32pt"] < 5.0 and info.get("missing_32pt", 0) <= 2
        else:
            ok = info["max_rel_err_pct"] < 5.0
        n_used = info.get("n_samples_used", info.get("n_points"))
        note = (f"方法={info['method']}；样本 {n_used}；真值覆盖 {info.get('truth_coverage_bins', 'n/a')}")
        if "max_rel_err_pct_32pt" in info:
            note += (f"；32 点口径 max={info['max_rel_err_pct_32pt']}%（missing "
                     f"{info.get('missing_32pt')}），全样本 max={info['max_rel_err_pct']}%"
                     f"（后者含碎片端点，仅记录）")
        if "n_medial_strokes" in info and info["n_medial_strokes"] > 2:
            note += (f"；注意：T4 被分解成 {info['n_medial_strokes']} 段中轴（A10 单独判定），"
                     "本项按空间投影对齐真值后只考核宽度反演本身")
        return {"status": PASS if ok else FAIL, "value": info,
                "threshold": threshold, "note": note}
    return _fn


_width_item("A7", "MUST", "mean",
            "宽度反演平均相对误差 < 5%（T4 锥形笔画 22→2.5px）",
            "宽度反演精度（DT 直取，D4 核心优势）：T4 沿 t 32 点平均相对误差")
_width_item("A13", "MUST", "max",
            "沿 t 取 32 个真值等分点逐一比对，最大宽度相对误差 < 5%（T4；最多允许 2 点无对应抽样并记录）",
            "锥形笔画宽度曲线反演（A13）：T4 沿 t 32 点最大相对误差")


def _build_clean_spec_doc(ctx: Ctx, case: str, resolution: int) -> tuple[dict, np.ndarray]:
    """直接从 fixture spec（已知中心线/宽度/颜色）构造 v2 doc。

    目的：A8 要考核的是**渲染基元**，不该被上游分解碎片化污染。这条路径完全绕过
    preprocess/decompose，只把 spec 真值几何交给 fourier 拟合 + 渲染器，因此
    "abs 边界偏差 ≤1px" 才能公平地量出来。
    """
    ctx.require("fourier", "render")
    from fbs_core import fourier

    spec = ctx.fixture_spec(case)
    stroke_spec = spec.get("stroke") or {}
    if not stroke_spec.get("centerline_px"):
        raise Blocked(f"{case} spec 缺少 centerline_px，无法构造干净 doc")
    size = spec.get("size") or [384, 384]
    long_side = float(max(size))
    cl_px = np.asarray(stroke_spec["centerline_px"], dtype=np.float64)
    # fixture 坐标 = 像素中心；CONTRACT：像素 i 中心 world=(i+0.5)/long_side
    world = (cl_px + 0.5) / long_side
    cfg = FBSConfig(target_resolution=int(resolution))
    fit = fourier.fit_curve(world, False, cfg.contract(), cfg, WarningLog(), name=case)
    width_spec = stroke_spec.get("width_px") or {}
    wmode = width_spec.get("mode")
    if wmode == "linear_keys":
        width_curve = {"interp": "linear",
                       "keys": [[float(t), float(v) / long_side] for t, v in width_spec["keys"]]}
    elif wmode == "step":
        width_curve = {"interp": "step",
                       "keys": [[float(t), float(v) / long_side] for t, v in width_spec["keys"]]}
    elif wmode == "constant":
        width_curve = {"interp": "constant",
                       "keys": [[0.0, float(width_spec.get("value", 0.0)) / long_side]]}
    else:
        raise Blocked(f"{case} spec 的 width_px.mode 不支持: {wmode!r}")
    color = [int(v) for v in (stroke_spec.get("color_rgb") or [0, 0, 0])]
    style = {
        "color": {"interp": "constant", "stops": [{"t": 0.0, "rgb": color, "a": 1.0}]},
        "alpha": {"interp": "constant", "keys": [[0.0, 1.0]]},
        "shape": {"interp": "step", "keys": [[0.0, "round"]]},
        "jitter": 0.0,
    }
    canvas_h = float(size[1]) / long_side
    doc = io_format.new_document(
        mode=io_format.MODE_PER_STROKE,
        source={"path": case, "sha256": "", "width": int(size[0]), "height": int(size[1]),
                "mode": "RGB", "scale_applied": 1.0},
        image_class={"klass": str(spec.get("class_expected") or "lineart"), "evidence": {}},
        canvas={"width": 1.0, "height": canvas_h, "long_side_units": 1.0,
                "origin": "top-left", "y_axis": "down"},
        render_contract=cfg.contract(),
        decomposition={"method": "acceptance_spec_doc", "params": cfg.to_dict(), "region_count": 0},
        base_color=[255, 255, 255, 255],
        warnings=[],
        accelerators={},
    )
    doc["brush_styles"] = [style]
    doc["strokes"] = [{
        "id": 0, "closed": False, "fill_role": "medial_branch", "repr": "fourier",
        "N": int(fit["N"]), "arc_length": float(fit["arc_length"]),
        "max_err": float(fit["max_err"]), "layer": 0, "region_id": -1,
        "bbox": [float(world[:, 0].min()), float(world[:, 1].min()),
                 float(world[:, 0].max()), float(world[:, 1].max())],
        "coef": [[int(k), float(re), float(im)] for k, re, im in np.asarray(fit["coef"], dtype=np.float64)],
        "closure": dict(fit["closure"]),
        "brush": {"style": 0, "width": width_curve},
    }]
    io_format.finalize_document(doc)
    errs = io_format.validate(doc)
    if errs:
        raise FBErrorAdapt(f"干净 spec doc 不合法: {errs[:3]}")
    ref = metrics.load_rgb(ctx.fixture_file(case)).astype(np.float64) / 255.0
    return doc, ref


def _analytic_truth_mask(ctx: Ctx, case: str, resolution: int) -> np.ndarray:
    """解析圆盘并集真值掩码（A8 用）。

    用 `tests/gen_fixtures.Canvas` 的**独立解析实现**（逐段精确带覆盖
    ``min(0.5,d+r) − max(−0.5,d−r)`` = 路径⊕圆盘），直接按 spec 真值中心线 + 宽度曲线
    在渲染分辨率上重画，得到与渲染器实现无关的参考真值（不是拿另一种渲染模式的输出当真值）。
    """
    tests_dir = os.path.join(ROOT, "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import gen_fixtures as gf

    spec = ctx.fixture_spec(case)
    stroke_spec = spec.get("stroke") or {}
    if not stroke_spec.get("centerline_px"):
        raise Blocked(f"{case} spec 缺少 centerline_px")
    size = spec.get("size") or [384, 384]
    long_side = float(max(size))
    scale = float(resolution) / long_side
    centerline = np.asarray(stroke_spec["centerline_px"], dtype=np.float64)
    pts = (centerline + 0.5) * scale - 0.5
    width_spec = stroke_spec.get("width_px") or {}
    wmode = width_spec.get("mode")
    if wmode == "constant":
        wv = float(width_spec.get("value", 0.0)) * scale
        width_fn = lambda t: np.full(len(np.atleast_1d(t)), wv)  # noqa: E731 - 简单闭包
    elif wmode in ("linear_keys", "step"):
        keys = np.asarray(width_spec["keys"], dtype=np.float64)
        if wmode == "linear_keys":
            width_fn = lambda t: np.interp(np.atleast_1d(np.asarray(t, dtype=np.float64)),  # noqa: E731
                                           keys[:, 0], keys[:, 1]) * scale
        else:
            def width_fn(t: np.ndarray) -> np.ndarray:
                tt = np.atleast_1d(np.asarray(t, dtype=np.float64))
                idx = np.clip(np.searchsorted(keys[:, 0], tt, side="right") - 1, 0, len(keys) - 1)
                return keys[:, 1][idx] * scale
    else:
        raise Blocked(f"{case} spec 的 width_px.mode 不支持: {wmode!r}")
    canvas = gf.Canvas(int(resolution), int(resolution))
    canvas.add_stroke(pts, width_fn, gf.const_color((0, 0, 0)))
    img = np.asarray(canvas.to_array(), dtype=np.float64) / 255.0
    return metrics.ink_mask(img, metrics.DEFAULT_INK_TOL, np.array([1.0, 1.0, 1.0]))


def _clean_doc_mode_stats(ctx: Ctx, case: str) -> dict:
    """在"spec 直接构造的干净 doc"上比较 stamp / line 两基元相对**解析真值**的误差。

    误差口径（两个都报）：
    - ``iou_err = 1 − IoU(掩码, 解析真值)``：面积型形状误差；
    - ``band_px = 对称差像素数 / 真值边界像素数``：误差区域的**等效带宽（px）**，
      即"平均有百分之几像素宽的边界画偏了"，可跨载体比较。
    A8 判据用 band_px（`stamp_band ≤ 0.5 × line_band` 且 `stamp_band ≤ 1.0px`），
    「≤1px」即"等效误差带宽不超过 1 个像素"。
    """
    from fbs_core import render as render_mod
    from fbs_core.util import erode

    R = min(ctx.R, 512)
    doc, _ref = _build_clean_spec_doc(ctx, case, R)
    truth_mask = _analytic_truth_mask(ctx, case, R)
    boundary_px = int((truth_mask & ~erode(truth_mask, 1)).sum())
    bg = np.asarray(doc.get("base_color", [255, 255, 255])[:3], dtype=np.float64) / 255.0
    out: dict = {
        "case": case, "R": R,
        "doc": "spec_clean（绕过 preprocess/decompose，几何/宽度来自 *_spec.json 真值）",
        "truth": "analytic_disc_union（gen_fixtures.Canvas 精确带覆盖，独立实现）",
        "truth_ink_px": int(truth_mask.sum()), "truth_boundary_px": boundary_px,
        "fit_N": int(doc["strokes"][0]["N"]),
        "fit_max_err_x_R": round(float(doc["strokes"][0]["max_err"]) * R, 4),
    }
    for mode in ("stamp", "line"):
        img = render_mod.render(doc, resolution=R, supersample=2, mode=mode)
        m = metrics.ink_mask(img, metrics.DEFAULT_INK_TOL, bg)
        diff = int((m ^ truth_mask).sum())
        inter = int((m & truth_mask).sum())
        union = int((m | truth_mask).sum())
        out[f"{mode}"] = {
            "ink_px": int(m.sum()),
            "sym_diff_px": diff,
            "band_px": round(diff / max(boundary_px, 1), 4),
            "iou_err": round(1.0 - (inter / union if union else 1.0), 5),
        }
    out["ratio_band"] = (round(out["stamp"]["band_px"] / out["line"]["band_px"], 4)
                         if out["line"]["band_px"] > 0 else None)
    out["ratio_iou"] = (round(out["stamp"]["iou_err"] / out["line"]["iou_err"], 4)
                        if out["line"]["iou_err"] > 0 else None)
    return out


@item("A8", "MUST", "stamp（MAT 逆运算）相对解析真值的绝对形状误差（T5b 尖角折返，判据已修正）",
      needs=("document", "render", "fourier"), cmd=_build_cmd("T5b_sharp_fold_stroke.png", "A8_T5b", 512))
def a8(ctx: Ctx) -> dict:
    """A8（2026-10-18 最终判据修正，Lead 裁定）：干净 spec doc + 解析真值。

    旧判据「stamp ≤ 0.5 × line」的前提是"变宽线段渲染是产品级替代路径且明显更差"——
    旧版 SKILL 已明确记录该假设被实测推翻（0.824），v2 的 `line` 更是**故意粗糙的 A/B
    调试基元**（PIL 整宽线段+平均色）；两个基元都在逼近同一集合，2× 差距不可能出现。
    因此最终判据改为：
      ① ``stamp_band ≤ 0.5 × epsilon_px``（绝对精度：契约半像素预算；R512 下 = 0.25px）；
      ② ``stamp_band ≤ line_band``（不劣于调试对照基元）；
      ``ratio = stamp_band / line_band`` 作为记录值，不再作为硬阈值。
    """
    info = _clean_doc_mode_stats(ctx, "T5b_sharp_fold_stroke.png")
    stamp_band = info["stamp"]["band_px"]
    line_band = info["line"]["band_px"]
    # epsilon 从干净 doc 的 render_contract 取（不硬编码像素阈值）
    clean_doc, _ref = _build_clean_spec_doc(ctx, "T5b_sharp_fold_stroke.png", info["R"])
    eps = float((clean_doc.get("render_contract") or {}).get("epsilon_px", 0.5))
    half_eps = 0.5 * eps
    crit_abs = stamp_band <= half_eps
    crit_not_worse = stamp_band <= line_band
    ok = bool(crit_abs and crit_not_worse)
    # 诊断：经 pipeline 分解后的同一载体（碎片化 + 上游误差）仅供归因
    pipeline_diag: dict = {}
    try:
        pipeline_diag = _render_modes_for_shape_error(ctx)
    except (Blocked, FBErrorAdapt) as exc:
        pipeline_diag = {"note": f"pipeline 载体诊断不可用: {exc}"}
    return {
        "status": PASS if ok else FAIL,
        "value": {
            "case": info["case"], "R": info["R"], "doc": info["doc"], "truth": info["truth"],
            "fit_N": info["fit_N"], "fit_max_err_x_R": info["fit_max_err_x_R"],
            "stamp": info["stamp"], "line": info["line"],
            "ratio_band_recorded": info["ratio_band"], "ratio_iou_recorded": info["ratio_iou"],
            "epsilon_px": eps, "half_epsilon_px": round(half_eps, 4),
            "criteria": {"stamp_band_le_half_epsilon": crit_abs,
                         "stamp_band_le_line_band": crit_not_worse},
            "pipeline_carrier_diagnostic": pipeline_diag,
        },
        "threshold": f"解析真值下：stamp 等效误差带宽 ≤ 0.5×epsilon_px（={half_eps:g}px @R512 契约）"
                     f" 且 stamp_band ≤ line_band（ratio 仅记录）",
        "note": "判据修正（Lead 2026-10-18 最终版）：旧『stamp ≤0.5×line』依赖『线段渲染是产品级"
                "替代路径且明显更差』的前提，旧版 SKILL 已记录实测证伪（0.824）；v2 的 line 是故意"
                "粗糙的 A/B 调试基元。改为绝对精度（≤0.5ε）+ 不劣于对照基元。"
                "旧判据实测见 A8-legacy（BROKEN）",
    }


@item("A8-legacy", "MUST", "A8 旧判据（stamp ≤ 50% × line）—— 判据修正记录，不计入通过/失败",
      needs=("document", "render"), cmd=_build_cmd("T5b_sharp_fold_stroke.png", "A8_T5b", 512))
def a8_legacy(ctx: Ctx) -> dict:
    """保留旧判据的实测值，状态 BROKEN（已废止），不参与 ok/failed 统计。"""
    info = _render_modes_for_shape_error(ctx)
    ratio = info["ratio_stamp_over_line"]
    legacy_pass = ratio is not None and ratio <= 0.5
    return {
        "status": BROKEN,
        "value": {"legacy_criterion": "shape_err(stamp) ≤ 0.5 × shape_err(line)",
                  "measured_ratio": ratio, "legacy_criterion_pass": legacy_pass, **info},
        "threshold": "旧判据：shape_err(stamp) ≤ 0.5 × shape_err(line)",
        "note": "旧判据把 line 当『产品级替代渲染路径』的对照；v2 中 line 是**刻意粗糙的 A/B 调试基元**"
                "（PIL 整宽线段+平均色，见 render._paint_line），不再是产品渲染路径，旧判据在新架构下"
                "不可比。旧 SKILL 明确记录该假设被实测推翻（0.824，远达不到 0.5），此处保留实测值仅作判据修正记录，不算通过也不算失败。A8 现行判据见 A8。",
    }


@item("A9", "SHOULD", "扁平插画 PSNR @R=1024 ≥ 26dB（T3）",
      needs=("document", "render"), cmd=_build_cmd("T3_flat_illustration.png", "A9_T3", 1024))
def a9(ctx: Ctx) -> dict:
    R = 1024 if not ctx.quick else ctx.R
    b = ctx.build("T3_flat_illustration.png", resolution=R)
    ok = float(b.metrics["psnr"]) >= 26.0
    note = "全量口径 @R=1024" if b.resolution == 1024 else f"quick 代理 R={b.resolution}"
    return {"status": PASS if ok else FAIL,
            "value": {"psnr_db": b.metrics["psnr"], "R": b.resolution},
            "threshold": "≥ 26 dB @R=1024", "note": note}


@item("A10", "SHOULD", "中轴分支数 / 预期笔画数 ≤ 1.5×（T4 单笔画真值）",
      needs=("document", "render"), cmd=_build_cmd("T4_taper_thick_to_thin.png", "A10_T4", 512))
def a10(ctx: Ctx) -> dict:
    b = ctx.build("T4_taper_thick_to_thin.png")
    medial = [s for s in b.doc.get("strokes", []) if s.get("fill_role") == "medial_branch"]
    expected = 1
    ratio = len(medial) / expected
    ok = ratio <= 1.5
    return {
        "status": PASS if ok else FAIL,
        "value": {"medial_branches": len(medial), "expected": expected, "ratio": round(ratio, 3),
                  "stroke_count": b.doc["stats"]["stroke_count"],
                  "fill_count": b.doc["stats"].get("fill_count")},
        "threshold": "medial_branch 数 / 预期(1) ≤ 1.5×",
        "note": "T4 是单笔画合成真值；过度切分会在这里暴露",
    }


@item("A11", "MAY", ".fbs / PNG 体积比 ≤ 3×（T3，必须同时给 R 与绝对值）",
      needs=("document", "render"), cmd=_build_cmd("T3_flat_illustration.png", "A11_T3", 512))
def a11(ctx: Ctx) -> dict:
    b = ctx.build("T3_flat_illustration.png")
    fbs_bytes = os.path.getsize(b.fbs_path)
    png_bytes = os.path.getsize(b.png_path)
    rep = metrics.volume_report(b.doc, fbs_bytes, png_bytes, resolution=b.resolution)
    ok = rep["ratio_fbs_over_png"] is not None and rep["ratio_fbs_over_png"] <= 3.0
    return {
        "status": PASS if ok else FAIL,
        "value": rep,
        "threshold": "≤ 3×（MAY；设计文档已明确刻意宽松、放弃体积目标）",
        "note": "比值随 R 漂移：结论必须同时看 resolution_R 与分子分母绝对值",
    }


@item("A12", "MAY", "SVG 导出体积 vs .fbs（记录即可，不设阈值）",
      needs=("document", "render"), cmd=_build_cmd("T3_flat_illustration.png", "A12_T3", 512))
def a12(ctx: Ctx) -> dict:
    from fbs_core import render as render_mod

    b = ctx.build("T5_sharp_corners.png")
    svg_path = os.path.join(os.path.dirname(b.fbs_path), "export.svg")
    try:
        info = render_mod.export_svg(b.doc, svg_path, resolution=min(ctx.R, 512))
    except FBSError as exc:
        return {"status": FAIL, "value": {"error": exc.code, "message": exc.message},
                "threshold": "SVG 导出成功（体积仅记录）",
                "note": "导出失败属诚实缺陷（A12/B5 互通能力未达成）"}
    svg_bytes = os.path.getsize(svg_path)
    fbs_bytes = os.path.getsize(b.fbs_path)
    return {
        "status": PASS,
        "value": {"svg_bytes": svg_bytes, "fbs_bytes": fbs_bytes,
                  "ratio_svg_over_fbs": round(svg_bytes / fbs_bytes, 3) if fbs_bytes else None,
                  "export_info": info},
        "threshold": "无阈值（MAY：记录 SVG 体积与 fbs 比值）",
        "note": "SVG 只能离散变宽笔画/stamp 图案填充，体积大是格式限制而非 bug（格式规范 §5.2）",
    }


@item("A14", "MUST", "linear/spline/preset 三种 taper 表达渲染一致性 < 1%（T11）",
      needs=("document", "render"), cmd=_build_cmd("T11_taper_three_expressions.png", "A14_T11", 512))
def a14(ctx: Ctx) -> dict:
    import copy

    from fbs_core import render as render_mod

    b = ctx.build("T11_taper_three_expressions.png", resolution=min(ctx.R, 512))
    R = min(ctx.R, 512)
    medial = [s for s in b.doc.get("strokes", []) if s.get("fill_role") == "medial_branch"]
    if not medial:
        raise FBErrorAdapt("T11 文档没有 medial_branch 笔画，无法做 taper 表达一致性")


    def variants_for(stroke: dict) -> dict[str, dict]:
        w0 = float(attribute.eval_numeric((stroke.get("brush") or {}).get("width"), [0.0])[0])
        return {
            "linear": {"interp": "linear", "keys": [[0.0, w0], [1.0, 0.15 * w0]]},
            "spline": {"interp": "spline",
                       "keys": [[0.0, w0], [0.5, 0.575 * w0], [1.0, 0.15 * w0]]},
            "preset": {"interp": "preset", "preset": "taper-out",
                       "scale": w0, "strength": 0.85},
        }

    imgs: dict[str, np.ndarray] = {}
    for name in ("linear", "spline", "preset"):
        d = copy.deepcopy(b.doc)
        touched = 0
        for st in d.get("strokes", []):
            if st.get("fill_role") != "medial_branch":
                continue
            st["brush"]["width"] = copy.deepcopy(variants_for(st)[name])
            touched += 1
        imgs[name] = render_mod.render(d, resolution=R, supersample=1, mode="stamp")
    keys = list(imgs)
    diffs: dict[str, float] = {}
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, c = imgs[keys[i]].astype(np.float64), imgs[keys[j]].astype(np.float64)
            diffs[f"{keys[i]} vs {keys[j]}"] = round(float(np.mean(np.abs(a - c))) / 255.0, 5)
    worst = max(diffs.values()) if diffs else 1.0
    return {
        "status": PASS if worst < 0.01 else FAIL,
        "value": {"pairwise_mean_abs_diff": diffs, "worst": worst,
                  "R": R, "touched_strokes": len(medial)},
        "threshold": "两两平均绝对差异 < 1%（相对 0..1 色域）",
        "note": "同一 taper 的三种表达（linear / spline 共线三点 / preset taper-out strength=0.85）"
                "在渲染上应等价；按每个笔画的原 w(0) 归一化",
    }


@item("A15", "MUST", "6 种 interp 全部可用 + step 区间语义正确（含 T13 端到端）",
      needs=("document", "render"), cmd=_build_cmd("T13_step_intervals.png", "A15_T13", 512))
def a15(ctx: Ctx) -> dict:
    """A15：属性层硬验 6 种 interp + step 语义；端到端用 T13 **方向无关**判据（Lead 裁定）。

    方向约定被明确为"不预设"：分支 t 方向任意、几何与 width 同向反转是合法的；
    两种 orientation 取优后检查前后段宽度与边界 t。`interp="step"` 仅作偏好记录，
    不判 FAIL —— step 语义的硬约束由属性层（eval_numeric step 左右开区间）保证。
    """
    checks = _a15_attribute_checks()
    attr_ok = all(checks.values())
    note = "属性层 6+2 项检查全部通过" if attr_ok else f"属性层未通过: {[k for k, v in checks.items() if not v]}"
    try:
        step = _step_semantics(ctx)
        step_ok = (step["before_rel_err_pct"] <= 20.0 and step["after_rel_err_pct"] <= 25.0) \
            and (step["boundary_t"] is None or 0.40 <= step["boundary_t"] <= 0.60)
        if not step["interp_step_preferred"]:
            note += "；T13 反演曲线用 interp=linear（允许：只记偏好，不判 FAIL）"
    except (Blocked, FBErrorAdapt) as exc:
        return {"status": BLOCKED, "value": {"attribute_checks": checks},
                "threshold": "6 种 interp 可用 + step 区间语义端到端正确",
                "note": f"属性层通过={attr_ok}；T13 端到端无法评估: {exc}"}
    return {
        "status": PASS if (attr_ok and step_ok) else FAIL,
        "value": {"attribute_checks": checks, "t13_step": step,
                  "criteria": {"attribute_layer_all": attr_ok, "t13_end_to_end": step_ok}},
        "threshold": "6 种 interp（constant/linear/spline/step/fourier/preset）属性层全部可用；"
                     "T13 端到端（两种方向取优）：前段 20px±20%、后段 6px±25%、边界 t∈[0.4,0.6]",
        "note": note,
    }


# ------------------------------------------------------------------ B 组
@item("B1", "MUST", "一条命令完成线稿 → .fbs + PNG + report.json（无需手工调参）",
      needs=("document", "render"), cmd='python scripts/fbs.py build assets/testcases/T1_signature.png -o assets/out/acceptance/cases/B1 --resolution 256 --no-svg --quiet')
def b1(ctx: Ctx) -> dict:
    ctx.require("cli", "document", "render")
    outdir = os.path.join(ctx.report_dir, "cli", "B1")
    os.makedirs(outdir, exist_ok=True)
    cmd = [_py(), os.path.join(HERE, "fbs.py"), "build",
           os.path.join(FIXTURES, "T1_signature.png"), "-o", outdir,
           "--resolution", "256", "--no-svg", "--quiet"]
    t0 = time.perf_counter()
    proc = _run_subprocess(cmd, cwd=ROOT)
    elapsed = time.perf_counter() - t0
    fbs = os.path.join(outdir, "T1_signature.fbs.json")
    png = os.path.join(outdir, "T1_signature_R256.png")
    report = os.path.join(outdir, "T1_signature.report.json")
    files_ok = all(os.path.exists(p) for p in (fbs, png, report))
    report_ok = False
    if os.path.exists(report):
        with open(report, "r", encoding="utf-8") as fh:
            rep = json.load(fh)
        report_ok = bool(rep.get("schema") and (rep.get("quality") or {}).get("psnr") is not None)
    ok = proc.returncode == 0 and files_ok and report_ok
    return {
        "status": PASS if ok else FAIL,
        "value": {"exit_code": proc.returncode, "files_exist": files_ok,
                  "report_valid": report_ok, "elapsed_s": round(elapsed, 2),
                  "stdout_tail": (proc.stdout or "").strip().splitlines()[-3:]},
        "threshold": "CLI 退出码 0 且 .fbs/PNG/report.json 齐备、report 含 quality.psnr",
        "note": "subprocess 真跑 CLI（不是 import 调用），同时覆盖 CLI 参数默认值纪律",
    }


@item("B2", "MUST", "报告能区分「忠实复刻」与「降级复刻」",
      needs=("document", "render"), cmd='python scripts/fbs.py build assets/testcases/T6_gradient.png -o assets/out/acceptance/cases/B2 --resolution 256 --no-svg')
def b2(ctx: Ctx) -> dict:
    # 忠实样例：T1（report 四维齐备）；降级样例：优先 T8a/T6/T8b 中第一个能建出带 warning 的
    faithful = ctx.build("T1_signature.png")
    with open(faithful.report_path, "r", encoding="utf-8") as fh:
        f_rep = json.load(fh)
    f_quality = f_rep.get("quality") or {}
    four_dims = all(k in f_quality for k in ("structure", "thickness", "tone", "linewidth"))
    faithful_ok = bool(f_rep.get("schema")) and four_dims

    degraded_name = None
    degraded_codes: list[str] = []
    degraded_ok = False
    for case in ("T8a_solid_black.png", "T6_gradient.png", "T8b_solid_white.png"):
        try:
            b = ctx.build(case)
        except FBSError as exc:
            degraded_name = f"{case} → 明确拒绝 {exc.code}"
            degraded_codes = [exc.code]
            degraded_ok = bool(exc.message)
            break
        except Exception:  # noqa: BLE001 - 继续尝试下一个降级样例
            continue
        degraded_codes = b.warning_codes()
        if degraded_codes:
            with open(b.report_path, "r", encoding="utf-8") as fh:
                d_rep = json.load(fh)
            degraded_ok = bool(d_rep.get("warnings")) and "warnings" in d_rep
            degraded_name = case
            break
    return {
        "status": PASS if (faithful_ok and degraded_ok) else FAIL,
        "value": {"faithful": {"report": os.path.relpath(faithful.report_path, ROOT),
                               "four_dimensions": four_dims,
                               "warnings": len(f_rep.get("warnings") or [])},
                  "degraded": {"sample": degraded_name, "codes": degraded_codes,
                               "distinguishable": degraded_ok}},
        "threshold": "忠实报告含 schema + 四维 quality；降级报告含 warnings（或明确拒绝码）",
        "note": "B4/B5（JSON 可读性 / Illustrator 打开）属文档定性项，本套件不冒充已验证",
    }


@item("B3", "MUST", "笔刷属性在渲染中肉眼可辨（变宽 / 变色 / 变透明，消融实验）",
      needs=("document", "render"), cmd=_build_cmd("T4_taper_thick_to_thin.png", "B3_T4", 512))
def b3(ctx: Ctx) -> dict:
    """消融实验：把某一条属性曲线替换成常量后重渲，看画面是否**局部可见**地变化。

    这比"比较两幅图的整图平均差"可靠：笔画只占画面几个百分点，平均差会被背景稀释到
    看似"没变化"。判据：有变化像素占比 ≥0.1% 且变化像素内部 P99 差 ≥8 灰度级。
    """
    import copy

    from fbs_core import render as render_mod

    R = min(ctx.R, 512)

    def diff(normal: np.ndarray, ablated: np.ndarray, applicable: int) -> dict:
        stats = _render_diff_stats(normal, ablated)
        stats["applicable_items"] = int(applicable)
        stats["visible"] = bool(applicable > 0 and stats["changed_frac"] >= 0.001
                                and stats["p99_diff_on_changed"] >= 8.0)
        return stats

    # ---- 变宽：把宽度曲线压平到各自中位宽度 ------------------------------
    b4 = ctx.build("T4_taper_thick_to_thin.png")

    def flatten_widths(doc: dict) -> tuple[dict, int]:
        d = copy.deepcopy(doc)
        changed = 0
        for st in d.get("strokes", []):
            if st.get("fill_role") != "medial_branch":
                continue
            curve = (st.get("brush") or {}).get("width")
            s = np.linspace(0.0, 1.0, 33)
            vals = attribute.eval_numeric(curve, s)
            if float(np.ptp(vals)) <= 0.02 * max(float(np.max(np.abs(vals))), 1e-9):
                continue
            const = float(np.median(vals))
            st["brush"]["width"] = {"interp": "constant", "keys": [[0.0, const]]}
            changed += 1
        return d, changed

    norm = render_mod.render(b4.doc, resolution=R, supersample=1, mode="stamp")
    d_flat, n_width = flatten_widths(b4.doc)
    w_stats = diff(norm, render_mod.render(d_flat, resolution=R, supersample=1, mode="stamp"), n_width)

    # ---- 变色：把所有 style 的颜色曲线压成同一个平均色 --------------------
    b12 = ctx.build("T12_color_gradient.png")

    def constant_colors(doc: dict) -> tuple[dict, int]:
        d = copy.deepcopy(doc)
        rgbs: list[np.ndarray] = []
        for style in d.get("brush_styles", []):
            for stop in (style.get("color") or {}).get("stops") or []:
                rgbs.append(np.asarray(stop.get("rgb", [0, 0, 0]), dtype=np.float64))
        if not rgbs:
            return d, 0
        mean_rgb = np.mean(np.stack(rgbs, axis=0), axis=0)
        changed = 0
        for style in d.get("brush_styles", []):
            stops = (style.get("color") or {}).get("stops") or []
            if not stops:
                continue
            alpha0 = float(stops[0].get("a", 1.0))
            style["color"] = {"interp": "constant",
                              "stops": [{"t": 0.0,
                                         "rgb": [int(round(v)) for v in mean_rgb],
                                         "a": alpha0}]}
            changed += 1
        return d, changed

    norm_c = render_mod.render(b12.doc, resolution=R, supersample=1, mode="stamp")
    d_const, n_color = constant_colors(b12.doc)
    c_stats = diff(norm_c, render_mod.render(d_const, resolution=R, supersample=1, mode="stamp"), n_color)

    # ---- 变透明：alpha 曲线消融（若文档本身有非恒定 alpha 才适用） --------
    def flatten_alpha(doc: dict) -> tuple[dict, int]:
        d = copy.deepcopy(doc)
        changed = 0
        for style in d.get("brush_styles", []):
            curve = style.get("alpha")
            if not isinstance(curve, dict):
                continue
            s = np.linspace(0.0, 1.0, 33)
            try:
                vals = attribute.eval_numeric(curve, s, default=1.0)
            except FBSError:
                continue
            if float(np.ptp(vals)) <= 0.02:
                continue
            style["alpha"] = {"interp": "constant", "keys": [[0.0, float(np.median(vals))]]}
            changed += 1
        return d, changed

    norm_a = render_mod.render(b12.doc, resolution=R, supersample=1, mode="stamp")
    d_alpha, n_alpha = flatten_alpha(b12.doc)
    a_stats = diff(norm_a, render_mod.render(d_alpha, resolution=R, supersample=1, mode="stamp"), n_alpha)
    alpha_applicable = n_alpha > 0
    alpha_ok = a_stats["visible"] if alpha_applicable else True

    ok = w_stats["visible"] and c_stats["visible"] and alpha_ok
    return {
        "status": PASS if ok else FAIL,
        "value": {"width": w_stats, "color": c_stats, "alpha": a_stats,
                  "alpha_applicable": alpha_applicable, "R": R},
        "threshold": "宽度/颜色/（若存在 alpha 变化）alpha 曲线压成常量后：有变化像素占比 ≥0.1% "
                     "且变化像素 P99 差 ≥8 灰度级（0..255）",
        "note": "消融实验：T4 压平宽度、T12 把所有 style 颜色压成同一平均色（含色相/明度差）、"
                "alpha 曲线压成中位值；文档无 alpha 变化时 alpha 维度记 n/a（不判失败）。"
                "透明度的格式层语义由 A15/T13 的独立曲线覆盖",
    }


def _render_diff_stats(a: np.ndarray, b: np.ndarray) -> dict:
    """两张渲染图的局部差异统计（用于 B3「肉眼可辨」判定）。

    整图平均差会被大面积背景稀释（T4 的笔画只占几个百分点），所以判据看**局部**：
    有变化像素的占比 + 变化像素内部的 P99 差异。
    """
    da = np.abs(a.astype(np.float64) - b.astype(np.float64)).max(axis=2)
    changed = da > 8.0
    n_changed = int(changed.sum())
    return {
        "mean_abs_diff_0_255": round(float(da.mean()), 4),
        "changed_frac": round(float(changed.mean()), 5),
        "changed_px": n_changed,
        "p99_diff_on_changed": round(float(np.percentile(da[changed], 99)), 3) if n_changed else 0.0,
        "max_diff": round(float(da.max()), 3),
    }


# ------------------------------------------------------------------ R 组
@item("R2", "MUST", "一笔画通道：仅 1 条 stroke + 单次 FFT + 路径偏差 ≤0.05px",
      needs=("document", "render", "onestroke"),
      cmd=_build_cmd("T1_signature.png", "R2_T1", 512)
      + f' && "{_py()}" scripts/fbs.py onestroke --fbs assets/out/acceptance/cases/R2_T1/T1_signature.fbs.json')
def r2(ctx: Ctx) -> dict:
    built = ctx.build("T1_signature.png")
    one = _one_stroke(ctx, built)
    strokes = one.get("strokes") or []
    st = strokes[0] if strokes else {}
    stats = one.get("stats") or {}
    _dev = st.get("path_deviation_px")
    deviation = float(_dev) if _dev is not None else 9.9
    _dev_max = stats.get("path_deviation_max_px", st.get("path_deviation_max_px"))
    deviation_max = float(_dev_max) if _dev_max is not None else 9.9
    ok = (one.get("mode") == "one_stroke"
          and len(strokes) == 1
          and int(stats.get("fft_passes", -1)) == 1
          and deviation <= 0.05
          and deviation_max <= 0.5)
    out = os.path.join(ctx.cases_dir, "onestroke")
    os.makedirs(out, exist_ok=True)
    one_path = os.path.join(out, "R2_T1.onestroke.fbs.json")
    io_format.write_fbs(one, one_path)
    return {
        "status": PASS if ok else FAIL,
        "value": {"mode": one.get("mode"), "stroke_count": len(strokes),
                  "fft_passes": stats.get("fft_passes"),
                  "path_deviation_px": deviation,
                  "path_deviation_max_px": deviation_max,
                  "path_samples": stats.get("path_samples"),
                  "N": st.get("N"), "travel_length": st.get("travel_length"),
                  "files": os.path.relpath(one_path, ROOT)},
        "threshold": "mode=one_stroke；strokes 恰好 1 条；stats.fft_passes==1；"
                     "path_deviation_px ≤ 0.05 且 path_deviation_max_px ≤ 0.5（@契约 R）",
        "note": f"本项在 R={built.resolution} 下评估；两项偏差分别取 stroke.path_deviation_px"
                f"（RMS/代表值）与 stats.path_deviation_max_px（最大值）"
                f"（{'quick 代理' if built.resolution != 1024 else '全量口径 R=1024'}）",
    }


def _one_stroke(ctx: Ctx, built: Built) -> dict:
    """构建（并缓存）一笔画文档；R2/R3/R4/R8 共用。"""
    ctx.require("onestroke")
    from fbs_core import onestroke as onestroke_mod

    key = (built.key[0], built.resolution)
    if key in ctx.one_docs:
        return ctx.one_docs[key]
    cfg = FBSConfig.from_dict((built.doc.get("decomposition") or {}).get("params") or {})
    cfg.target_resolution = int(built.resolution)
    warn = WarningLog()
    t0 = time.perf_counter()
    one = onestroke_mod.build_one_stroke(built.doc, cfg, warn,
                                         harmonics=int(cfg.harmonics), max_fft=int(cfg.max_fft))
    ctx.timings[f"onestroke:{built.key[0]}@R{built.resolution}"] = round(time.perf_counter() - t0, 3)
    # 注意：build_one_stroke 内部已 finalize（并回填 path_deviation 等诊断字段），
    # 这里**不能**再调 finalize_document —— 它会重算 stats、把 path_deviation_max_px 等冲掉。
    ctx.one_docs[key] = one
    return one


@item("R3", "MUST", "60s 作画视频：时长 60±5%、帧数≥步骤数、时间轴等比、不省略步骤",
      needs=("document", "render", "onestroke", "video"),
      cmd="python scripts/fbs.py acceptance --items R3")
def r3(ctx: Ctx) -> dict:
    """R3：优先用 Lead 正式演示产物的真实 60s 视频（冻结后生成），否则退化到自产短视频。

    判定（可测代理，不把推测写成结论）：
    - probe：duration ∈[57,63]s、size>0、fps 合理；
    - 不省略步骤：frames ≥ len(step_spans) 且 step_spans 连续覆盖 [0,1]；
    - 时间轴等比：用 onestroke.build_path 的 t 与 arc_cum 验证线性（timeline=arc），
      逐帧画面对齐未解码验证（需要时另跑人工抽帧）。
    """
    if ctx.quick or ctx.skip_video:
        return {"status": SKIP, "value": "—",
                "threshold": "duration∈[57,63]s；frames≥steps；timeline 等比；不省略步骤",
                "note": "quick/--skip-video 模式不跑视频（SKIP，非通过）"}
    # ---- 1) 优先：Lead 正式演示产物里的真实 60s 视频 ------------------------
    # 冻结后 mp4 仍在后台渲染：最多轮询等待 180s（Lead 指定），超时走回退并写 note。
    waited = _wait_demo_videos(180.0)
    for name, orig_path in _REAL_IMAGES:
        if not os.path.exists(orig_path):
            continue
        demo = _demo_artifacts(name)
        if not demo.get("video"):
            continue
        one = None
        if demo.get("onestroke"):
            try:
                one = io_format.read_fbs(demo["onestroke"])
                ctx.one_docs[(f"demo:{name}", 1024)] = one
            except FBSError:
                one = None
        res = _video_checks(demo["video"], one)
        return {
            "status": res["status"],
            "value": {k: v for k, v in res.items() if k != "status"},
            "threshold": "duration∈[57,63]s；frames≥步骤数；size>0；"
                         "timeline=arc 等比（build_path 的 t/arc_cum 线性）；step_spans 连续覆盖 [0,1]",
            "note": f"真实 60s 演示视频（{name}，Lead 正式演示产物）；"
                    "时间轴等比用 build_path 的 t/arc_cum 线性验证；逐帧画面对齐未解码验证",
        }
    # ---- 2) 回退：自产一条 60s 视频（demo 尚未生成时） ----------------------
    built = ctx.build("T1_signature.png")
    one = _one_stroke(ctx, built)
    from fbs_core import video as video_mod

    outdir = os.path.join(ctx.report_dir, "video")
    os.makedirs(outdir, exist_ok=True)
    video_path = os.path.join(outdir, "R3_T1_1min.mp4")
    warn = WarningLog()
    t0 = time.perf_counter()
    info = video_mod.make_video(one, video_path, duration=60.0, fps=30, size=None,
                                circles=600, mode="sweep", warn=warn)
    elapsed = time.perf_counter() - t0
    res = _video_checks(video_path, one)
    return {
        "status": res["status"],
        "value": {**{k: v for k, v in res.items() if k != "status"},
                  "case": "synthetic:T1（回退自产视频）",
                  "value_source": "self_generated_fallback",
                  "elapsed_s": round(elapsed, 2)},
        "threshold": "duration∈[57,63]s；frames≥步骤数；size>0；"
                     "timeline=arc 等比（build_path 的 t/arc_cum 线性）；step_spans 连续覆盖 [0,1]",
        "note": f"未找到 demo 真实 60s 视频，回退自产（make_video {elapsed:.1f}s）；"
                "时间轴等比用 build_path 的 t/arc_cum 线性验证",
    }


@item("R4", "MUST", "每条笔画 brush.width/color/shape 齐全，有填充图 fill_count>0",
      needs=("document", "render"), cmd=_build_cmd("T3_flat_illustration.png", "R4_T3", 512))
def r4(ctx: Ctx) -> dict:
    b3 = ctx.build("T3_flat_illustration.png")
    b1 = ctx.build("T1_signature.png")
    problems: list[str] = []
    checked = 0
    for built in (b3, b1):
        styles = built.doc.get("brush_styles") or []
        for st in built.doc.get("strokes", []):
            checked += 1
            sid = st.get("id")
            brush = st.get("brush") or {}
            if not isinstance(brush.get("width"), dict):
                problems.append(f"{built.key[0]} stroke {sid}: 缺 brush.width")
            si = brush.get("style")
            if not isinstance(si, int) or not (0 <= si < len(styles)):
                problems.append(f"{built.key[0]} stroke {sid}: brush.style 非法/越界 {si}")
                continue
            style = styles[si]
            for key in ("color", "alpha", "shape"):
                if not isinstance(style.get(key), dict):
                    problems.append(f"{built.key[0]} stroke {sid}: style 缺 {key}")
            if st.get("fill_role") == "region_fill" and not st.get("poly"):
                problems.append(f"{built.key[0]} stroke {sid}: region_fill 缺显式 poly")
    fill_count = int(b3.doc.get("stats", {}).get("fill_count", 0))
    ok = not problems and fill_count > 0
    return {
        "status": PASS if ok else FAIL,
        "value": {"checked_strokes": checked, "problems": problems[:12],
                  "fill_count_T3": fill_count, "region_count_T3": b3.doc["stats"].get("region_count"),
                  "style_count_T3": len(b3.doc.get("brush_styles") or [])},
        "threshold": "所有 stroke: width 曲线 + style 索引合法 + style 含 color/alpha/shape；"
                     "T3（有填充图）fill_count>0；region_fill 必须有 poly",
        "note": "R4 检查「笔触属性完整」的线格式层；视觉可辨性由 B3 负责",
    }


@item("R5", "MUST", "sweep 画出的必须是连续的线，不是一串点（T1 单笔画）",
      needs=("document", "render", "fourier"),
      cmd=_build_cmd("T1_signature.png", "R5_T1", 512))
def r5(ctx: Ctx) -> dict:
    import copy

    from fbs_core import fourier
    from fbs_core import render as render_mod

    built = ctx.build("T1_signature.png")
    stroke = _longest_medial(built.doc)
    if stroke is None:
        raise FBErrorAdapt("T1 文档没有 medial_branch 笔画")
    R = min(ctx.R, 512)
    d = copy.deepcopy(built.doc)
    d["strokes"] = [copy.deepcopy(stroke)]
    img = render_mod.render(d, resolution=R, supersample=1, mode="sweep")
    mask = metrics.ink_mask(img, metrics.DEFAULT_INK_TOL, built.bg)
    from fbs_core.util import dilate, label_components

    if not mask.any():
        return {"status": FAIL, "value": {"ink_px": 0},
                "threshold": "sweep 渲染有墨，且墨迹连通、中心线采样覆盖率≥0.98",
                "note": "渲染为空：sweep 路径可能未执行"}
    lab, n = label_components(mask)
    areas = np.bincount(lab.reshape(-1))[1:]
    big = int((areas >= 10).sum())
    ts = np.linspace(0.0, 1.0, 200)
    coef = np.asarray(stroke["coef"], dtype=np.float64)
    closure = stroke.get("closure") or {"kind": "none"}
    try:
        pts = np.asarray(fourier.eval_curve(coef, closure, ts), dtype=np.float64)
    except TypeError as exc:
        raise Blocked(f"fourier.eval_curve 签名与契约不符: {exc}") from exc
    # world → 渲染像素索引：i = world * R - 0.5（像素 i 中心 world=(i+0.5)/R）
    px = pts * float(R) - 0.5
    H, W = mask.shape
    xi = np.clip(np.round(px[:, 0]).astype(int), 0, W - 1)
    yi = np.clip(np.round(px[:, 1]).astype(int), 0, H - 1)
    dil = dilate(mask, 1)
    coverage = float(dil[yi, xi].mean())
    ok = coverage >= 0.98 and big == 1
    return {
        "status": PASS if ok else FAIL,
        "value": {"sample_coverage": round(coverage, 5), "components": big,
                  "ink_px": int(mask.sum()), "sample_points": int(len(ts)), "R": R,
                  "skeleton_samples": "沿傅里叶中心线 200 点，±1px 邻域内是否有墨"},
        "threshold": "中心线采样覆盖率 ≥ 0.98 且墨迹主要连通域数 == 1（点串会有大量缺口/碎片）",
        "note": "墨迹阈值 0.5（低于 0.5 会把极淡的点串也算作线）",
    }


@item("R5b", "SHOULD", "（非设计文档项）生产基元一致性回归：stamp vs sweep 边界 p99≤1.5px、PSNR 差<0.5dB",
      needs=("document", "render"), cmd=_build_cmd("T4_taper_thick_to_thin.png", "R5b_T4", 512))
def r5b(ctx: Ctx) -> dict:
    """A6 的替代 sanity（Lead 2026-10-18 指定）：不冒充设计文档 A6。

    `sweep` 是生产渲染基元（路径⊕圆盘连续扫掠），`stamp` 是 MAT 逆运算的语义参考实现
    （圆盘并集）。两者对同一 doc 的几何应一致；若出现大偏差说明至少一个基元的实现
    与契约语义漂移。`line` 是刻意粗糙的 A/B 对照基元，不参与本项。
    """
    from fbs_core import render as render_mod

    built = ctx.build("T4_taper_thick_to_thin.png")
    R = min(ctx.R, 512)
    imgs = {mode: render_mod.render(built.doc, resolution=R, supersample=2, mode=mode)
            for mode in ("stamp", "sweep")}
    masks = {mode: metrics.ink_mask(img, metrics.DEFAULT_INK_TOL, built.bg) for mode, img in imgs.items()}
    p99 = _boundary_p99(masks["stamp"], masks["sweep"])
    ref = _resize_ref(built.reference, imgs["stamp"].shape[1], imgs["stamp"].shape[0])
    psnr_stamp = metrics.psnr(imgs["stamp"].astype(np.float64) / 255.0, ref)
    psnr_sweep = metrics.psnr(imgs["sweep"].astype(np.float64) / 255.0, ref)
    d_psnr = abs(psnr_stamp - psnr_sweep)
    ok = p99 <= 1.5 and d_psnr < 0.5
    return {
        "status": PASS if ok else FAIL,
        "value": {"case": "T4_taper_thick_to_thin.png", "R": R,
                  "boundary_p99_stamp_vs_sweep_px": round(p99, 3),
                  "psnr_stamp_db": round(psnr_stamp, 3), "psnr_sweep_db": round(psnr_sweep, 3),
                  "psnr_diff_db": round(d_psnr, 4),
                  "note": "stamp 圆盘并集语义 vs sweep 生产扫掠基元；line 调试基元不参与"},
        "threshold": "stamp/sweep 墨迹边界 p99 ≤1.5px 且两者对参考图 PSNR 差 <0.5dB",
        "note": "非设计文档项（A6 是 Python vs HTML Canvas 双后端，v2 不实现；A6=NOT_IMPLEMENTED）。"
                "本项是渲染基元一致性的回归 sanity，不冒充 A6",
    }


@item("R6", "SHOULD", "宽度/颜色沿 t 连续变化（T4 宽度、T12 颜色）",
      needs=("document", "render"), cmd=_build_cmd("T12_color_gradient.png", "R6_T12", 512))
def r6(ctx: Ctx) -> dict:
    info = _continuity_check(ctx)
    ok = (info["width_max_jump_ratio"] <= 0.10 and info["color_max_delta_e_step"] <= 12.0)
    return {
        "status": PASS if ok else FAIL,
        "value": info,
        "threshold": "相邻采样 |Δwidth|/mean(width) ≤ 10%；相邻采样 ΔE76 ≤ 12",
        "note": "R6 要求 sweep 中宽度/颜色可平滑渐变；step 曲线（T13）的区间跳变由 A15 单独考核，不在此项",
    }


@item("R7", "MUST", "分层顺序：fill 层在分支层之下；fill 面积降序（同层 footprint 降序为诊断项）",
      needs=("document", "render"), cmd=_build_cmd("T2_icon.png", "R7_T2", 512))
def r7(ctx: Ctx) -> dict:
    def footprint(stroke: dict, built: Built) -> float:
        if stroke.get("fill_role") == "region_fill":
            poly = np.asarray(stroke.get("poly") or [], dtype=np.float64).reshape(-1, 2)
            if len(poly) < 3:
                return 0.0
            x, y = poly[:, 0], poly[:, 1]
            return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)
        arc = float(stroke.get("arc_length", 0.0) or 0.0)
        try:
            w = _eval_width_px(built.doc, stroke, built.long_side, np.linspace(0.0, 1.0, 9))
        except Exception:  # noqa: BLE001 - 曲线求值失败时退回 bbox 面积代理并记录
            w = np.zeros(1)
        return arc * float(np.mean(w)) if w.size else arc

    results: dict[str, dict] = {}
    ok_all = True
    for case in ("T2_icon.png", "T3_flat_illustration.png", "T4_taper_thick_to_thin.png"):
        b = ctx.build(case)
        strokes = b.doc.get("strokes", [])
        fills = [s for s in strokes if s.get("fill_role") == "region_fill"]
        medial = [s for s in strokes if s.get("fill_role") == "medial_branch"]
        detail: dict = {"fills": len(fills), "medial": len(medial)}
        if fills and medial:
            max_fill_layer = max(int(s.get("layer", 0)) for s in fills)
            min_med_layer = min(int(s.get("layer", 0)) for s in medial)
            layer_ok = max_fill_layer < min_med_layer
            detail.update({"max_fill_layer": max_fill_layer,
                           "min_medial_layer": min_med_layer, "fill_below_branch": layer_ok})
            ok_all &= layer_ok
        # R7 必需项：fill 面积在 fill 层内非增序（先铺大块，再压小块）
        layers: dict[int, list[dict]] = {}
        for st in strokes:
            layers.setdefault(int(st.get("layer", 0)), []).append(st)
        fill_area_violations: list[str] = []
        branch_order_violations: list[str] = []
        for layer, group in layers.items():
            group_fills = [st for st in group if st.get("fill_role") == "region_fill"]
            group_medial = [st for st in group if st.get("fill_role") == "medial_branch"]
            fps = [footprint(st, b) for st in group_fills]
            for i in range(len(fps) - 1):
                if fps[i] + 1e-9 < fps[i + 1] * 0.995:
                    fill_area_violations.append(
                        f"layer{layer} fill[{i}] area {fps[i]:.4g} < next {fps[i + 1]:.4g}")
            mfps = [footprint(st, b) for st in group_medial]
            for i in range(len(mfps) - 1):
                if mfps[i] + 1e-9 < mfps[i + 1] * 0.995:
                    branch_order_violations.append(
                        f"layer{layer} branch[{i}] footprint {mfps[i]:.4g} < next {mfps[i + 1]:.4g}")
        detail["fill_area_violations"] = fill_area_violations[:6]
        detail["fill_area_desc"] = not fill_area_violations
        detail["branch_footprint_violations_diagnostic"] = branch_order_violations[:6]
        ok_all &= not fill_area_violations
        results[case] = detail
    return {
        "status": PASS if ok_all else FAIL,
        "value": results,
        "threshold": "region_fill 的 layer < medial_branch 的 layer；同一 fill 层内面积非增序（大块先铺）",
        "note": "必需项按交接说明 R7「先铺底层、再压上层」+「按 footprint 面积从大到小」；"
                "分支同层 footprint 降序单列为诊断项（dcmp 侧排序策略，不影响 R7 判定但记入报告）",
    }


@item("R8", "MUST", "一笔画高谐波精度：N≈65536（Nyquist 取整容忍）+ 路径偏差 ≤0.05px（真实图）",
      needs=("document", "render", "onestroke"),
      cmd="python scripts/fbs.py acceptance --items R8")
def r8(ctx: Ctx) -> dict:
    """R8：真实图上一笔画 N 达标（请求 65536，Nyquist 上界允许 ±2 取整）+ 路径偏差。

    口径（Lead 2026-10-18 裁决）：
    - ``stroke.path_deviation_px`` 是逐点最近距离 **RMS**，阈值 ≤0.05px；
    - ``path_deviation_max_px`` 阈值 ≤0.5px（尖角重采样切角主导）；
    - ``path_deviation_p995_px`` 只记录、不设阈值（不得用它判 FAIL）；
    - N 由 ``N = min(harmonics, M//2-2)`` 决定，因此允许 N ≥ harmonics−2（Nyquist 取整），
      更低的 N 只有带 ``W_N_CLIPPED`` 明确告警才可接受。
    """
    if ctx.quick:
        return {"status": SKIP, "value": "—",
                "threshold": f"N≈{MAX_HARMONICS}（允许 Nyquist ±2）且 path_deviation_px≤0.05",
                "note": "quick 模式跳过真实图一笔画（SKIP，非通过）"}
    names = [(name, path) for name, path in _REAL_IMAGES if os.path.exists(path)]
    if not names:
        return {"status": SKIP, "value": "—", "threshold": "同上",
                "note": "真实测试图不存在，无法验证高谐波精度（SKIP，非通过）"}
    results: dict[str, dict] = {}
    ok_all = True
    # ---- 1) 优先读 Lead 正式演示产物里的 *.onestroke.fbs.json ----------------
    for name, _orig in names:
        demo = _demo_artifacts(name)
        if not demo.get("onestroke"):
            continue
        try:
            one = io_format.read_fbs(demo["onestroke"])
        except FBSError as exc:
            results[name] = {"ok": False, "error": exc.code, "message": exc.message[:160]}
            ok_all = False
            continue
        rec = _onestroke_summary(one)
        rec["source"] = "demo_artifacts"
        rec["file"] = os.path.relpath(demo["onestroke"], ROOT)
        ok_all &= rec["ok"]
        results[name] = rec
        ctx.one_docs[(f"demo:{name}", 1024)] = one
    if results:
        return {
            "status": PASS if ok_all else FAIL,
            "value": results,
            "threshold": f"N ≥ {MAX_HARMONICS}−2（Nyquist 取整）或 W_N_CLIPPED 告警；"
                         f"path_deviation_px(RMS) ≤0.05 且 path_deviation_max_px ≤0.5 @R=1024",
            "note": "R8 口径（Lead 裁决）：RMS≤0.05、max≤0.5、p99.5 只记录不判 FAIL；"
                    "数据读自 Lead 正式演示产物 *.onestroke.fbs.json（不重复构建）",
        }
    # ---- 2) 回退：现场 build + onestroke ------------------------------------
    from fbs_core import onestroke as onestroke_mod

    for name, real_path in names:
        built = ctx.build_external(name, real_path, resolution=1024)
        cfg = FBSConfig.from_dict((built.doc.get("decomposition") or {}).get("params") or {})
        cfg.target_resolution = 1024
        warn = WarningLog()
        try:
            one = onestroke_mod.build_one_stroke(built.doc, cfg, warn,
                                                 harmonics=MAX_HARMONICS, max_fft=int(cfg.max_fft))
        except FBSError as exc:
            results[name] = {"ok": False, "error": exc.code, "message": exc.message[:160]}
            ok_all = False
            continue
        one.setdefault("warnings", [])
        for rec_w in warn.to_list():
            one["warnings"].append(rec_w)
        rec = _onestroke_summary(one)
        rec["source"] = "live_build（未找到 demo 产物）"
        ok_all &= rec["ok"]
        results[name] = rec
        ctx.one_docs[(f"real:{name}", 1024)] = one
    return {
        "status": PASS if ok_all else FAIL,
        "value": results,
        "threshold": f"N ≥ {MAX_HARMONICS}−2（Nyquist 取整）或 W_N_CLIPPED 告警；"
                     f"path_deviation_px(RMS) ≤0.05 且 path_deviation_max_px ≤0.5 @R=1024",
        "note": "R8 口径（Lead 裁决）：RMS≤0.05、max≤0.5、p99.5 只记录不判 FAIL；"
                "未找到 demo 产物，回退现场构建",
    }


# ------------------------------------------------------------------ T 组（合成用例）
def _accept_expected_error(ctx: Ctx, case: str, expect: set[str]) -> dict:
    from fbs_core import document

    path = ctx.fixture_file(case)
    try:
        document.build_from_image(path, FBSConfig(target_resolution=128), WarningLog())
        return {"ok": False, "observed": "未按预期拒绝"}
    except FBSError as exc:
        return {"ok": exc.code in expect, "observed": exc.code,
                "message": exc.message[:160], "evidence": _short(exc.evidence, 200),
                "suggestions": [str(s)[:80] for s in exc.suggestions[:3]]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "observed": f"未捕获异常 {type(exc).__name__}: {exc}"}


def _t_case_build(ctx: Ctx, case: str, extra_note: str = "") -> dict:
    """通用 T 项：成功 build 即 PASS，并记录分类/笔画/质量/警告。"""
    try:
        b = ctx.build(case)
    except FBSError as exc:
        return {"status": FAIL, "value": {"error": exc.code, "message": exc.message[:160]},
                "threshold": "build 成功", "note": extra_note}
    return {
        "status": PASS,
        "value": {"klass": b.doc.get("image_class", {}).get("klass"),
                  "stroke_count": b.doc["stats"]["stroke_count"],
                  "fill_count": b.doc["stats"].get("fill_count"),
                  "region_count": b.doc["stats"].get("region_count"),
                  "psnr_db": b.metrics["psnr"], "ssim": b.metrics["ssim"],
                  "warnings": b.warning_codes()[:8], "R": b.resolution},
        "threshold": "build 成功（该用例不设质量阈值，质量由对应 A 项考核）",
        "note": extra_note,
    }


@item("T1", "MUST", "T1 签名线稿：最佳场景，build 成功且分类=lineart",
      needs=("document", "render"), cmd=_build_cmd("T1_signature.png", "T1", 512))
def t1(ctx: Ctx) -> dict:
    r = _t_case_build(ctx, "T1_signature.png", "A1/A2 的载体")
    if r["status"] == PASS:
        klass = r["value"]["klass"]
        if klass != "lineart":
            r["status"] = FAIL
            r["note"] = f"分类={klass}，期望 lineart（T1 是三色以内的线稿）"
    return r


@item("T2", "SHOULD", "T2 图标（直角+曲线）：build 成功、fill_count>0",
      needs=("document", "render"), cmd=_build_cmd("T2_icon.png", "T2", 512))
def t2(ctx: Ctx) -> dict:
    r = _t_case_build(ctx, "T2_icon.png", "尖角分支保留由 A8 的 T5 场景考核；T2 验区域填充")
    if r["status"] == PASS and int(r["value"].get("fill_count") or 0) <= 0:
        r["status"] = FAIL
        r["note"] = "图标是实心色块，fill_count 应 >0（区域填充缺失）"
    return r


@item("T3", "SHOULD", "T3 扁平插画（8 色）：build 成功、多区域 + 填充",
      needs=("document", "render"), cmd=_build_cmd("T3_flat_illustration.png", "T3", 512))
def t3(ctx: Ctx) -> dict:
    r = _t_case_build(ctx, "T3_flat_illustration.png", "A9/A11/R7 的载体")
    if r["status"] == PASS:
        if int(r["value"].get("region_count") or 0) <= 0 or int(r["value"].get("fill_count") or 0) <= 0:
            r["status"] = FAIL
            r["note"] = "8 色平涂应产生 regions>0 且 fill_count>0"
    return r


@item("T4", "MUST", "T4 锥形笔画（已知 w(t)）：build 成功且有 medial_branch",
      needs=("document", "render"), cmd=_build_cmd("T4_taper_thick_to_thin.png", "T4", 512))
def t4(ctx: Ctx) -> dict:
    r = _t_case_build(ctx, "T4_taper_thick_to_thin.png", "A3/A7/A10/A13 的载体")
    if r["status"] == PASS:
        b = ctx.build("T4_taper_thick_to_thin.png")
        if _longest_medial(b.doc) is None:
            r["status"] = FAIL
            r["note"] = "锥形笔画没有被表示为 medial_branch（宽度反演将不可用）"
    return r


@item("T5", "MUST", "T5 尖角形状：build 成功、fill_count>0",
      needs=("document", "render"), cmd=_build_cmd("T5_sharp_corners.png", "T5", 512))
def t5(ctx: Ctx) -> dict:
    r = _t_case_build(ctx, "T5_sharp_corners.png", "A6/A8 的载体")
    if r["status"] == PASS and int(r["value"].get("fill_count") or 0) <= 0:
        r["status"] = FAIL
        r["note"] = "实心星形应产生 region_fill（fill_count>0）"
    return r


@item("T6", "SHOULD", "T6 渐变图：无硬边 → 必须明确降级/告警，不崩溃",
      needs=("document",), cmd=_build_cmd("T6_gradient.png", "T6", 256))
def t6(ctx: Ctx) -> dict:
    from fbs_core import document

    path = ctx.fixture_file("T6_gradient.png")
    try:
        doc, _info = document.build_from_image(path, FBSConfig(target_resolution=256), WarningLog())
    except FBSError as exc:
        return {"status": FAIL,
                "value": {"error": exc.code, "message": exc.message[:160]},
                "threshold": "渐变图应「特殊路径 + 告警」，崩溃或静默都不合格",
                "note": "设计文档 §6.2：渐变属内容异常 → 降级 + warning，不是拒绝"}
    except Exception as exc:  # noqa: BLE001
        return {"status": FAIL, "value": {"uncaught": f"{type(exc).__name__}: {exc}"},
                "threshold": "零崩溃", "note": "未捕获异常"}
    codes = [str(w.get("code")) for w in doc.get("warnings", [])]
    degrade = [c for c in codes if c.startswith("W_")]
    return {
        "status": PASS if degrade else FAIL,
        "value": {"klass": doc.get("image_class", {}).get("klass"), "warnings": codes,
                  "stroke_count": doc["stats"]["stroke_count"]},
        "threshold": "build 成功且 warnings 非空（明确降级），无未捕获异常",
        "note": "渐变无硬边 → 中轴退化；必须让调用方从 warnings 看出「这是降级复刻」",
    }


@item("T7", "MUST", "T7 伪照片：明确拒绝 E_PHOTO_DEFERRED，错误信息含证据与建议",
      needs=("document",), cmd='python scripts/fbs.py build assets/testcases/T7_photo_like.png -o assets/out/acceptance/cases/T7 --resolution 256 --no-svg')
def t7(ctx: Ctx) -> dict:
    from fbs_core import document

    path = ctx.fixture_file("T7_photo_like.png")
    try:
        document.build_from_image(path, FBSConfig(target_resolution=256), WarningLog())
    except FBSError as exc:
        has_evidence = bool(exc.evidence)
        has_suggest = bool(exc.suggestions)
        ok = exc.code == "E_PHOTO_DEFERRED" and len(exc.message) >= 10 and has_evidence and has_suggest
        return {
            "status": PASS if ok else FAIL,
            "value": {"code": exc.code, "message": exc.message[:200],
                      "has_evidence": has_evidence, "has_suggestions": has_suggest,
                      "suggestion_count": len(exc.suggestions)},
            "threshold": "E_PHOTO_DEFERRED + 非空 evidence + 非空 suggestions（错误信息可读且可行动）",
            "note": "D5：照片暂缓支持，必须明确拒绝（证据来自实测分类特征）",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": FAIL, "value": {"uncaught": f"{type(exc).__name__}: {exc}"},
                "threshold": "E_PHOTO_DEFERRED", "note": "未捕获异常"}
    return {"status": FAIL, "value": {"observed": "未拒绝（被当成可画图）"},
            "threshold": "E_PHOTO_DEFERRED", "note": "照片类必须拒绝；被静默接受会产生无意义结果"}


@item("T8", "MUST", "T8 纯色/纯白/全透明：纯色告警、全透明拒绝",
      needs=("document",), cmd='python scripts/fbs.py acceptance --items T8')
def t8(ctx: Ctx) -> dict:
    from fbs_core import document

    results: dict[str, dict] = {}
    ok_all = True
    for case, expect in (("T8a_solid_black.png", "warn"),
                         ("T8b_solid_white.png", "warn")):
        path = ctx.fixture_file(case)
        try:
            doc, _info = document.build_from_image(path, FBSConfig(target_resolution=128), WarningLog())
            codes = [str(w.get("code")) for w in doc.get("warnings", [])]
            ok = "W_SOLID_COLOR" in codes
            results[case] = {"result": "built", "warnings": codes, "ok": ok,
                             "strokes": doc["stats"]["stroke_count"]}
            ok_all &= ok
        except FBSError as exc:
            results[case] = {"result": f"rejected {exc.code}", "ok": True,
                             "message": exc.message[:120]}
        except Exception as exc:  # noqa: BLE001
            results[case] = {"result": f"uncaught {type(exc).__name__}: {exc}", "ok": False}
            ok_all = False
    tr = _accept_expected_error(ctx, "T8c_fully_transparent.png", {"E_INPUT_TRANSPARENT"})
    results["T8c_fully_transparent.png"] = tr
    ok_all &= bool(tr.get("ok"))
    return {
        "status": PASS if ok_all else FAIL,
        "value": results,
        "threshold": "纯色：W_SOLID_COLOR 告警（或明确拒绝）；全透明：E_INPUT_TRANSPARENT",
        "note": "纯色图允许「特殊路径+告警」或「明确拒绝」；全透明必须拒绝（无可见内容）",
    }


@item("T9", "SHOULD", "T9 模式转换：16bit / CMYK / alpha / 调色板均可自动转换",
      needs=("document",), cmd='python scripts/fbs.py acceptance --items T9')
def t9(ctx: Ctx) -> dict:
    results: dict[str, dict] = {}
    ok_all = True
    for case in ("T9a_16bit.png", "T9b_cmyk.jpg", "T9c_alpha.png", "T9d_palette.png"):
        try:
            b = ctx.build(case)
            mode = str((b.doc.get("source") or {}).get("mode") or "")
            results[case] = {"ok": bool(mode), "source_mode": mode,
                             "stroke_count": b.doc["stats"]["stroke_count"],
                             "klass": b.doc.get("image_class", {}).get("klass")}
            ok_all &= bool(mode)
        except FBSError as exc:
            results[case] = {"ok": False, "error": exc.code, "message": exc.message[:140]}
            ok_all = False
        except Exception as exc:  # noqa: BLE001
            results[case] = {"ok": False, "uncaught": f"{type(exc).__name__}: {exc}"}
            ok_all = False
    return {
        "status": PASS if ok_all else FAIL,
        "value": results,
        "threshold": "4 种特殊模式全部 build 成功，且 doc.source.mode 记录了原始模式",
        "note": "设计文档 §6.1：16bit/CMYK/调色板 → 自动转换并记入 source_meta.mode",
    }


@item("T10", "MUST", "T10 中文名大图成功 + 损坏文件明确拒绝",
      needs=("document",), cmd='python scripts/fbs.py acceptance --items T10')
def t10(ctx: Ctx) -> dict:
    ok_cn = True
    value: dict = {}
    try:
        b = ctx.build("T10a_中文名_大图.png")
        value["chinese_name"] = {"ok": True,
                                 "source_path_kept": b.doc.get("source", {}).get("path", "").endswith(".png"),
                                 "stroke_count": b.doc["stats"]["stroke_count"],
                                 "psnr_db": b.metrics["psnr"]}
        ok_cn = value["chinese_name"]["source_path_kept"]
    except Exception as exc:  # noqa: BLE001 - 中文路径失败是 T10 的核心缺陷
        value["chinese_name"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok_cn = False
    corrupt = _accept_expected_error(ctx, "T10b_损坏文件.png", {"E_INPUT_CORRUPT", "E_INPUT_FORMAT"})
    value["corrupt"] = corrupt
    ok = ok_cn and bool(corrupt.get("ok"))
    return {
        "status": PASS if ok else FAIL,
        "value": value,
        "threshold": "中文名 512 图 build 成功；损坏 PNG 拒绝码 ∈ {E_INPUT_CORRUPT,E_INPUT_FORMAT}",
        "note": "非 ASCII 路径与损坏文件是 §6.1 的两条硬要求",
    }


@item("T11", "MUST", "T11 taper 三表达载体：build 成功且有 medial_branch",
      needs=("document", "render"), cmd=_build_cmd("T11_taper_three_expressions.png", "T11", 512))
def t11(ctx: Ctx) -> dict:
    r = _t_case_build(ctx, "T11_taper_three_expressions.png", "A14 的载体")
    if r["status"] == PASS:
        b = ctx.build("T11_taper_three_expressions.png")
        if _longest_medial(b.doc) is None:
            r["status"] = FAIL
            r["note"] = "taper 线条未被表示为 medial_branch，A14 无法评估"
    return r


@item("T12", "SHOULD", "T12 颜色渐变反演：恢复的 color 曲线与真值 stops 平均 ΔE ≤ 25",
      needs=("document", "render"), cmd=_build_cmd("T12_color_gradient.png", "T12", 512))
def t12(ctx: Ctx) -> dict:
    b = ctx.build("T12_color_gradient.png")
    spec = ctx.fixture_spec("T12_color_gradient.png")
    stops = (spec.get("stroke") or {}).get("color_stops") or []
    stroke = _longest_medial(b.doc)
    if stroke is None:
        raise FBErrorAdapt("T12 没有 medial_branch 笔画")
    style_idx = int((stroke.get("brush") or {}).get("style", 0))
    style = b.doc.get("brush_styles", [])[style_idx]
    ts = np.linspace(0.02, 0.98, 9)
    rgb, _alpha = attribute.eval_color(style.get("color"), ts)  # 0..1
    st = np.asarray([s["t"] for s in stops], dtype=np.float64)
    truth = np.stack([np.interp(ts, st, [s["rgb"][c] for s in stops]) for c in range(3)], axis=1) / 255.0
    de_f = metrics.delta_e76(rgb, truth)
    de_r = metrics.delta_e76(rgb[::-1], truth)
    mean_de = float(np.mean(de_f)) if float(np.mean(de_f)) <= float(np.mean(de_r)) else float(np.mean(de_r))
    direction = "forward" if float(np.mean(de_f)) <= float(np.mean(de_r)) else "reversed"
    ok = mean_de <= 25.0
    return {
        "status": PASS if ok else FAIL,
        "value": {"mean_delta_e": round(mean_de, 3),
                  "max_delta_e": round(float(np.max(de_f if direction == "forward" else de_r)), 3),
                  "direction": direction, "color_interp": (style.get("color") or {}).get("interp"),
                  "samples": int(len(ts)), "R": b.resolution},
        "threshold": "恢复颜色曲线 vs 真值 stops 的平均 ΔE76 ≤ 25（SHOULD；颜色场近似允许偏差）",
        "note": "颜色反演是「色调维度」的端到端载体；方向不唯一时取更优方向并记录",
    }


@item("T13", "MUST", "T13 step 区间：恢复宽度曲线保留 t=0.5 处 20→6px 区间语义（方向无关判据）",
      needs=("document", "render"), cmd=_build_cmd("T13_step_intervals.png", "T13", 512))
def t13(ctx: Ctx) -> dict:
    """T13：方向任意性已按 Lead 裁定修正（两种 orientation 取优后判前后段宽度/边界）。"""
    info = _step_semantics(ctx)
    ok = (info["before_rel_err_pct"] <= 20.0 and info["after_rel_err_pct"] <= 25.0) \
        and (info["boundary_t"] is None or 0.40 <= info["boundary_t"] <= 0.60)
    note = ("区间语义必须用 step 显式表达（格式规范 §4 硬性约束 2）；"
            "自动反演曲线用 linear 但数值区间正确时允许（interp=step 仅偏好记录）")
    if not info.get("interp_step_preferred"):
        note += "；本次反演 interp 非 step（已记录，不判 FAIL）"
    return {
        "status": PASS if ok else FAIL,
        "value": info,
        "threshold": "方向取优后：w(0.25)≈20px（±20%）、w(0.75)≈6px（±25%）、边界 t∈[0.4,0.6]；"
                     "interp=step 仅偏好记录，不判 FAIL",
        "note": note,
    }


# 报告顺序（显式定义，不依赖装饰器执行顺序）
ITEM_ORDER = (
    [f"A{i}" for i in range(1, 16)] + ["A8-legacy"]
    + [f"B{i}" for i in range(1, 4)]
    + ["R2", "R3", "R4", "R5", "R5b", "R6", "R7", "R8"]
    + [f"T{i}" for i in range(1, 14)]
)


# ===================================================================== 真实图
def _first_existing_real() -> tuple[str, str] | None:
    for name, path in _REAL_IMAGES:
        if os.path.exists(path):
            return name, path
    return None


def _probe_existing_video(name: str) -> dict | None:
    """若 assets/out 下已有**合格的 60s 演示视频**，直接 probe（避免验收重复编码）。

    只接受 duration ∈ [57,63]s 且 fps ∈ [1,120] 的视频；smoke/短测试片（如 5s/207fps）
    会被忽略，否则会把 smoke 片当成 R3 合格证据（假通过）。
    """
    from fbs_core import video as video_mod

    out_root = os.path.join(ROOT, "assets", "out")
    if not os.path.isdir(out_root):
        return None
    for root, _dirs, files in os.walk(out_root):
        for fn in files:
            if not fn.lower().endswith(".mp4"):
                continue
            if name.lower() not in (root + "/" + fn).lower():
                continue
            path = os.path.join(root, fn)
            try:
                probe = video_mod.probe_video(path)
            except Exception:  # noqa: BLE001 - 损坏的演示视频跳过
                continue
            dur = float(probe.get("duration_s") or 0.0)
            fps = float(probe.get("fps") or 0.0)
            if probe.get("frames") and 57.0 <= dur <= 63.0 and 1.0 <= fps <= 120.0:
                return {"path": os.path.relpath(path, ROOT), **probe}
    return None


DEMO_ROOT = os.path.join(ROOT, "assets", "out", "demo")


def _demo_artifacts(name: str) -> dict:
    """读取 Lead 生成的正式演示产物（冻结后生成，验收直接读，不重复构建）。

    期望目录：``assets/out/demo/<name>/``，含 R=1024 `.fbs.json`、`_R1024.png`、
    `*.onestroke.fbs.json`、`*_1min.mp4`（以及 demo report）。
    """
    d = os.path.join(DEMO_ROOT, name)
    out: dict = {"dir": d, "exists": os.path.isdir(d)}
    if not out["exists"]:
        return out
    files: list[str] = []
    for root, _dirs, fs in os.walk(d):
        for fn in fs:
            files.append(os.path.join(root, fn))
    files.sort()

    def pick(pred) -> str | None:
        for p in files:
            if pred(os.path.basename(p).lower()):
                return p
        return None

    out["fbs"] = pick(lambda f: f.endswith(".fbs.json") and ".onestroke." not in f)
    out["onestroke"] = pick(lambda f: f.endswith(".onestroke.fbs.json"))
    out["png"] = pick(lambda f: f.endswith(".png") and "_r1024" in f and "onestroke" not in f) \
        or pick(lambda f: f.endswith(".png") and "onestroke" not in f)
    out["report"] = pick(lambda f: f.endswith(".report.json"))
    out["video"] = pick(lambda f: f.endswith(".mp4") and "1min" in f) \
        or pick(lambda f: f.endswith(".mp4"))
    return out


def _wait_demo_videos(timeout_s: float = 180.0, poll_s: float = 5.0) -> dict:
    """等待 Lead 的 60s 演示视频落盘（冻结后仍在后台渲染）：最多轮询 timeout_s 秒。

    返回 {name: video_path|None}；超时也返回当前状态（由 R3 按回退逻辑处理并写 note）。
    """
    deadline = time.time() + float(timeout_s)
    got: dict = {}
    while True:
        got = {name: _demo_artifacts(name).get("video") for name, _p in _REAL_IMAGES}
        if any(got.values()) or time.time() >= deadline:
            return got
        time.sleep(max(1.0, float(poll_s)))


def _onestroke_summary(one: dict) -> dict:
    """从一笔画文档提取 R2/R8 口径的判定字段（RMS / max / p99.5 / N / fft_passes）。"""
    st = (one.get("strokes") or [{}])[0]
    stats = one.get("stats") or {}
    dev = st.get("path_deviation_px")
    dev = float(dev) if dev is not None else 9.9
    dev_max = stats.get("path_deviation_max_px", st.get("path_deviation_max_px"))
    dev_max = float(dev_max) if dev_max is not None else 9.9
    p995 = st.get("path_deviation_p995_px", stats.get("path_deviation_p995_px"))
    n_val = int(st.get("N", 0))
    codes = [str(w.get("code")) for w in (one.get("warnings") or [])]
    clipped = "W_N_CLIPPED" in codes
    ok = (n_val >= MAX_HARMONICS - 2 or clipped) and dev <= 0.05 and dev_max <= 0.5
    return {
        "ok": bool(ok), "N": n_val, "requested_harmonics": MAX_HARMONICS,
        "nyquist_clipped": clipped, "warnings": codes,
        "path_deviation_px": dev, "path_deviation_max_px": dev_max,
        "path_deviation_p995_px": p995,
        "path_samples": stats.get("path_samples"),
        "fft_passes": stats.get("fft_passes"),
        "travel_length": st.get("travel_length"),
        "threshold": "path_deviation_px(RMS)≤0.05 且 path_deviation_max_px≤0.5；"
                     "N≈65536（Nyquist ±2 或 W_N_CLIPPED）",
    }


def _arc_timeline_deviation(one_doc: dict, resolution: int = 1024) -> dict:
    """时间轴等比性的可测代理：build_path 的 t 与弧长累计必须线性（timeline=arc）。"""
    from fbs_core import onestroke as onestroke_mod

    try:
        path = onestroke_mod.build_path(one_doc, int(resolution), max_vertices=None)
    except Exception as exc:  # noqa: BLE001 - 纯记录，不因此改判
        return {"available": False, "note": f"build_path 失败: {type(exc).__name__}: {exc}"}
    t = np.asarray(path.get("t"), dtype=np.float64).reshape(-1)
    arc = np.asarray(path.get("arc_cum"), dtype=np.float64).reshape(-1)
    if t.size < 2 or arc.size != t.size:
        return {"available": False, "note": "build_path 缺 t/arc_cum"}
    total = float(arc[-1] - arc[0])
    if abs(total) <= 1e-12:
        return {"available": False, "note": "弧长退化"}
    rel = (arc - arc[0]) / total
    dev = float(np.max(np.abs(rel - t)))
    return {"available": True, "max_abs_dev": round(dev, 5),
            "samples": int(t.size), "note": "|arc_cum/total − t| 最大值（timeline=arc 等高比）"}


def _video_checks(video_path: str, one_doc: dict | None, *, steps: int | None = None) -> dict:
    """探测视频并做 R3 判定（时长±5%、帧数≥步骤、尺寸、step_spans 连续、arc 等比）。"""
    from fbs_core import video as video_mod

    try:
        probe = video_mod.probe_video(video_path)
    except Exception as exc:  # noqa: BLE001 - 视频不可读要记录为失败
        return {"status": FAIL, "error": f"{type(exc).__name__}: {exc}",
                "path": os.path.relpath(video_path, ROOT)}
    frames = int(probe.get("frames") or 0)
    duration = float(probe.get("duration_s") or 0.0)
    fps = float(probe.get("fps") or 0.0)
    span_ok = True
    span_gaps: list[float] = []
    timeline_dev: dict = {"available": False, "note": "未提供一笔画文档"}
    if one_doc:
        st = (one_doc.get("strokes") or [{}])[0]
        spans = st.get("step_spans") or []
        if spans:
            prev = 0.0
            for sp in spans:
                t0s, t1s = float(sp[0]), float(sp[1])
                span_gaps.append(round(t0s - prev, 5))
                prev = t1s
            span_ok = abs(span_gaps[0]) < 1e-3 and all(abs(g) < 1e-3 for g in span_gaps[1:]) \
                and abs(prev - 1.0) < 1e-3
            steps = len(spans)
        timeline_dev = _arc_timeline_deviation(one_doc, 1024)
    steps = int(steps or 0)
    checks = {
        "duration_ok": 57.0 <= duration <= 63.0,
        "frames_ge_steps": frames >= steps and frames > 0,
        "fps_plausible": 1.0 <= fps <= 120.0,
        "size_ok": bool(probe.get("size")) and min(probe.get("size") or [0]) > 0,
        "steps_covered": span_ok,
        "timeline_arc_proportional": bool(timeline_dev.get("available")
                                        and timeline_dev.get("max_abs_dev", 9) <= 0.02),
        "file_ok": os.path.exists(video_path) and os.path.getsize(video_path) > 10_000,
    }
    return {
        "status": PASS if all(checks.values()) else FAIL,
        "path": os.path.relpath(video_path, ROOT),
        "probe": probe, "steps": steps, "checks": checks,
        "step_spans_contiguous": span_ok, "gaps": span_gaps[:6],
        "timeline": "arc" if timeline_dev.get("available") else "unknown",
        "timeline_arc": timeline_dev,
        "bytes": os.path.getsize(video_path) if os.path.exists(video_path) else 0,
    }


def _real_entry_from_demo(ctx: Ctx, name: str, orig_path: str, demo: dict) -> dict:
    """用正式演示产物计算真实图四维指标（不重复 build；同分辨率比较）。"""
    doc = io_format.read_fbs(demo["fbs"])
    png = metrics.load_rgb(demo["png"])
    ref = metrics.load_rgb(orig_path).astype(np.float64) / 255.0
    ref_r = _resize_ref(ref, png.shape[1], png.shape[0])
    bg = np.asarray(doc.get("base_color", [255, 255, 255])[:3], dtype=np.float64) / 255.0
    m = metrics.compare(png, ref_r, doc=doc, bg=bg)
    entry: dict = {
        "exists": True, "source": "demo_artifacts",
        "path": orig_path, "R": int(max(png.shape[:2])),
        "status": {"build": "PASS（读取 Lead 正式演示产物，未重复构建）"},
        "klass": doc.get("image_class", {}).get("klass"),
        "stroke_count": doc["stats"]["stroke_count"],
        "fill_count": doc["stats"].get("fill_count"),
        "psnr_db": m["psnr"], "ssim": m["ssim"], "rmse": m["rmse"], "mae": m["mae"],
        "structure": {k: m["structure"][k] for k in
                      ("recall", "precision", "iou", "missing_pct", "extra_pct")},
        "tone": {"mean_delta_e": m["tone"]["mean_delta_e"],
                 "wrong_ink_pct": m["tone"]["wrong_ink_pct"]},
        "linewidth": {"ref_median_px": m["linewidth"]["ref_median_px"],
                      "rec_median_px": m["linewidth"]["rec_median_px"],
                      "ratio": m["linewidth"]["ratio"]},
        "warnings": [str(w.get("code")) for w in doc.get("warnings") or []][:10],
        "files": {"fbs": os.path.relpath(demo["fbs"], ROOT),
                  "png": os.path.relpath(demo["png"], ROOT)},
    }
    if demo.get("onestroke"):
        one = io_format.read_fbs(demo["onestroke"])
        rec = _onestroke_summary(one)
        rec["status"] = "PASS" if rec["ok"] else "FAIL"
        rec["file"] = os.path.relpath(demo["onestroke"], ROOT)
        entry["onestroke"] = rec
        ctx.one_docs[(f"demo:{name}", entry["R"])] = one
    else:
        entry["onestroke"] = {"status": SKIP, "note": "演示目录缺 *.onestroke.fbs.json"}
    if demo.get("video"):
        one = ctx.one_docs.get((f"demo:{name}", entry["R"]))
        entry["video"] = _video_checks(demo["video"], one)
    else:
        entry["video"] = {"status": SKIP, "note": "演示目录缺 *_1min.mp4"}
    return entry


def _run_real_images(ctx: Ctx) -> None:
    """真实图：优先读 Lead 正式演示产物（demo/），缺失才回退现场 build；每图给四维+一笔画+视频。"""
    ctx.require("document", "render")
    R = 512 if ctx.quick else 1024
    if not ctx.quick and not ctx.skip_video:
        # 演示视频可能仍在后台渲染：最多再等 60s（R3 已等过 180s，这里兜底）
        _wait_demo_videos(60.0)
    for name, path in _REAL_IMAGES:
        if not os.path.exists(path):
            ctx.real[name] = {"exists": False, "path": path, "status": SKIP,
                              "note": "真实测试图不存在（SKIP，不静默当通过）"}
            continue
        demo = _demo_artifacts(name)
        if demo.get("fbs") and demo.get("png"):
            try:
                entry = _real_entry_from_demo(ctx, name, path, demo)
            except (FBSError, Blocked) as exc:
                entry = {"exists": True, "source": "demo_artifacts",
                         "status": {"build": f"FAIL demo 产物不可用: {exc}"}}
            ctx.real[name] = entry
            if entry.get("psnr_db") is not None:
                ctx.log(f"    [real/demo] {name}: psnr={entry.get('psnr_db')} "
                        f"ssim={entry.get('ssim')} strokes={entry.get('stroke_count')} "
                        f"R={entry.get('R')}")
            continue
        # ---- 回退：现场 build（demo 产物尚未生成时） ------------------------
        ctx.notes.append(f"真实图 {name} 未找到 demo 产物，回退现场 build（R={R}）")
        entry = {"exists": True, "path": path, "R": R, "source": "live_build", "status": {}}
        try:
            built = ctx.build_external(name, path, R)
        except FBSError as exc:
            entry["status"]["build"] = f"FAIL {exc.code}: {exc.message[:200]}"
            ctx.real[name] = entry
            continue
        except Exception as exc:  # noqa: BLE001
            entry["status"]["build"] = f"FAIL 未捕获异常 {type(exc).__name__}: {exc}"
            ctx.real[name] = entry
            continue
        m = built.metrics
        entry.update({
            "status": {"build": "PASS"},
            "klass": built.doc.get("image_class", {}).get("klass"),
            "stroke_count": built.doc["stats"]["stroke_count"],
            "fill_count": built.doc["stats"].get("fill_count"),
            "psnr_db": m["psnr"], "ssim": m["ssim"], "rmse": m["rmse"], "mae": m["mae"],
            "structure": {k: m["structure"][k] for k in
                          ("recall", "precision", "iou", "missing_pct", "extra_pct")},
            "tone": {"mean_delta_e": m["tone"]["mean_delta_e"],
                     "wrong_ink_pct": m["tone"]["wrong_ink_pct"]},
            "linewidth": {"ref_median_px": m["linewidth"]["ref_median_px"],
                          "rec_median_px": m["linewidth"]["rec_median_px"],
                          "ratio": m["linewidth"]["ratio"]},
            "files": {"fbs": os.path.relpath(built.fbs_path, ROOT),
                      "png": os.path.relpath(built.png_path, ROOT)},
            "warnings": built.warning_codes()[:10],
        })
        if not ctx.quick:
            try:
                one = _one_stroke(ctx, built)
                rec = _onestroke_summary(one)
                rec["status"] = "PASS" if rec["ok"] else "FAIL"
                entry["onestroke"] = rec
            except Exception as exc:  # noqa: BLE001 - 真实图一笔画失败要记录，不静默
                entry["onestroke"] = {"status": f"FAIL {type(exc).__name__}: {exc}"}
            entry["video"] = {"status": SKIP, "note": "回退现场 build 不生成视频（等待 demo 产物）"}
        else:
            entry["onestroke"] = {"status": SKIP, "note": "quick 模式跳过真实图一笔画"}
            entry["video"] = {"status": SKIP, "note": "quick 模式跳过视频"}
        ctx.real[name] = entry
        ctx.log(f"    [real/live] {name}: psnr={entry.get('psnr_db')} "
                f"ssim={entry.get('ssim')} strokes={entry.get('stroke_count')} R={R}")


# ===================================================================== 执行与报告
def _verify_items_selection(items: list[str] | None) -> list[str]:
    order = list(ITEM_ORDER)
    if not items:
        return order
    known = set(order)
    upper_to_id = {k.upper(): k for k in order}
    seen: list[str] = []
    unknown: list[str] = []
    for raw in items:
        token = str(raw).strip().upper()
        if not token:
            continue
        if token not in upper_to_id:
            unknown.append(token)
            continue
        iid = upper_to_id[token]
        if iid not in seen:
            seen.append(iid)
    if unknown:
        raise FBSError("E_ACCEPTANCE_ITEMS",
                       f"未知验收项: {', '.join(unknown)}",
                       evidence={"known": order},
                       suggestions=[f"可用: {','.join(order)}"])
    if not seen:
        raise FBSError("E_ACCEPTANCE_ITEMS", "验收项过滤后为空")
    return [i for i in order if i in seen]


def _execute_item(ctx: Ctx, meta: dict) -> dict:
    t0 = time.perf_counter()
    rec = {"id": meta["id"], "level": meta["level"], "desc": meta["desc"],
           "status": BLOCKED, "value": None, "threshold": "", "note": "", "cmd": meta.get("cmd", "")}
    missing = [f"{n}({ctx.preflight.get(n, '未探测')})" for n in meta["needs"]
               if ctx.preflight.get(n) != "ok"]
    if missing:
        rec["status"] = BLOCKED
        rec["note"] = "依赖模块未就绪: " + "; ".join(missing)
        rec["threshold"] = rec["desc"]
        rec["elapsed_s"] = round(time.perf_counter() - t0, 3)
        return rec
    try:
        out = meta["fn"](ctx) or {}
        rec["status"] = out.get("status", PASS)
        rec["value"] = _jsonable(out.get("value"))
        rec["threshold"] = str(out.get("threshold", ""))
        rec["note"] = str(out.get("note", ""))
        if out.get("cmd"):
            rec["cmd"] = str(out["cmd"])
    except Blocked as exc:
        rec["status"] = BLOCKED
        rec["note"] = str(exc)
        rec["threshold"] = rec["desc"]
    except FBErrorAdapt as exc:
        rec["status"] = FAIL
        rec["note"] = f"验收前提不满足: {exc}"
        rec["threshold"] = rec["desc"]
    except FBSError as exc:
        rec["status"] = FAIL
        rec["note"] = f"{exc.code}: {exc.message}" + (
            f" | evidence={_short(exc.evidence)}" if exc.evidence else "")
        rec["threshold"] = rec["desc"]
    except Exception as exc:  # noqa: BLE001 - 未捕获异常必须暴露为 FAIL（禁止静默）
        tb = traceback.format_exc().strip().splitlines()
        rec["status"] = FAIL
        rec["note"] = f"未捕获异常 {type(exc).__name__}: {exc} | {tb[-2].strip() if len(tb) > 1 else ''}"
        rec["threshold"] = rec["desc"]
    rec["elapsed_s"] = round(time.perf_counter() - t0, 3)
    ctx.timings[f"item:{meta['id']}"] = rec["elapsed_s"]
    return rec


def _summary(items: list[dict]) -> dict:
    counts = {PASS: 0, FAIL: 0, SKIP: 0, BLOCKED: 0, BROKEN: 0, NOT_IMPLEMENTED: 0}
    for rec in items:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    must = [r for r in items
            if r["level"] == "MUST" and r["status"] not in (BROKEN, NOT_IMPLEMENTED)]
    # 退出语义：PASS / FAIL / BLOCKED / SKIP / BROKEN / NOT_IMPLEMENTED 严格区分。
    # BLOCKED = 依赖未就绪、验收没跑完 → 不能算成功；SKIP = 设计上不跑；
    # BROKEN = 判据修正记录；NOT_IMPLEMENTED = v2 明确不实现的功能。
    # 只有 FAIL / BLOCKED 影响 ok。
    ok = counts[FAIL] == 0 and counts[BLOCKED] == 0
    # 分组统计（Lead 要求：A1–A15 / B1–B3 / R2–R8 / T1–T13 各自的 PASS/FAIL/NI/SKIP/BLOCKED）
    by_group: dict[str, dict] = {}
    for rec in items:
        g = rec["id"][:1] if rec["id"][0] in "ABRT" else "other"
        slot = by_group.setdefault(g, {PASS: 0, FAIL: 0, BLOCKED: 0, SKIP: 0, BROKEN: 0,
                                       NOT_IMPLEMENTED: 0, "total": 0})
        slot[rec["status"]] = slot.get(rec["status"], 0) + 1
        slot["total"] += 1
    return {
        "passed": counts[PASS], "failed": counts[FAIL],
        "blocked": counts[BLOCKED], "skipped": counts[SKIP],
        "broken": counts[BROKEN], "not_implemented": counts[NOT_IMPLEMENTED],
        "total": len(items),
        "must_total": len(must),
        "must_passed": sum(1 for r in must if r["status"] == PASS),
        "must_failed": sum(1 for r in must if r["status"] == FAIL),
        "must_blocked": sum(1 for r in must if r["status"] == BLOCKED),
        "complete": counts[BLOCKED] == 0,
        "ok": ok,
        "by_group": by_group,
        "note": "ok = failed==0 且 blocked==0；SKIP/BROKEN/NOT_IMPLEMENTED 不算通过，也不让验收假失败",
    }


def _env_info() -> dict:
    import PIL

    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "pillow": getattr(PIL, "__version__", "?"),
        "metrics_version": metrics.METRICS_VERSION,
        "skill_root": ROOT,
        "cwd": os.getcwd(),
    }


def _code_snapshot() -> dict:
    """被测代码快照（sha256 前 16 位 + mtime）：并行开发期报告必须注明"以该快照为准"。"""
    files: list[str] = ["scripts/fbs.py", "scripts/run_acceptance.py", "tests/gen_fixtures.py"]
    core_dir = os.path.join(HERE, "fbs_core")
    if os.path.isdir(core_dir):
        for fn in sorted(os.listdir(core_dir)):
            if fn.endswith(".py"):
                files.append(f"scripts/fbs_core/{fn}")
    entries: dict[str, dict] = {}
    digest_parts: list[str] = []
    for rel in files:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(path):
            entries[rel] = {"exists": False}
            continue
        st = os.stat(path)
        sha = sha256_file(path)[:16]
        entries[rel] = {
            "sha256_16": sha,
            "mtime": datetime.fromtimestamp(st.st_mtime, _TZ).isoformat(timespec="seconds"),
            "bytes": int(st.st_size),
        }
        digest_parts.append(f"{rel}:{sha}")
    digest = hashlib.sha256("\n".join(sorted(digest_parts)).encode("utf-8")).hexdigest()[:16]
    return {
        "digest": digest,
        "files": entries,
        "note": "并行开发期代码可能变动；本报告结论以该 sha256 快照为准，冻结后需复跑",
    }


def _write_reports(ctx: Ctx, items: list[dict], *, started: float) -> tuple[str, str]:
    summary = _summary(items)
    failures = [r for r in items if r["status"] == FAIL]
    first_failure = failures[0] if failures else None
    report = {
        "schema": "fbs-acceptance/2",
        "generated": datetime.now(_TZ).isoformat(timespec="seconds"),
        "quick": ctx.quick,
        "skip_video": ctx.skip_video,
        "env": _env_info(),
        "code_snapshot": _code_snapshot(),
        "fixtures": {
            "dir": os.path.relpath(FIXTURES, ROOT),
            "note": ctx.fixtures_note,
            "seed": ctx.fixtures_manifest.get("seed"),
            "count": ctx.fixtures_manifest.get("count"),
        },
        "preflight": ctx.preflight,
        "summary": summary,
        "first_failure": first_failure,
        "items": items,
        "real_images": ctx.real,
        "notes": ctx.notes,
        "timings_s": {k: round(float(v), 3) for k, v in ctx.timings.items()},
        "total_elapsed_s": round(time.perf_counter() - started, 2),
        "repro": {
            "quick": f'"{_py()}" scripts/run_acceptance.py --quick',
            "full": f'"{_py()}" scripts/run_acceptance.py',
            "items": f'"{_py()}" scripts/run_acceptance.py --items A1,A7,R2',
            "cli": f'"{_py()}" scripts/fbs.py acceptance --quick',
        },
    }
    report_json = os.path.join(ctx.report_dir, "report.json")
    with open(report_json, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(_jsonable(report), fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    # ---------------------------------------------------------------- markdown
    lines = [
        "# fourier-brushstroke v2 验收报告",
        "",
        f"- 生成时间：{report['generated']}　模式：{'quick（小分辨率代理）' if ctx.quick else '全量'}"
        f"　skip_video={ctx.skip_video}",
        f"- 环境：Python {report['env']['python']} / numpy {report['env']['numpy']}"
        f" / Pillow {report['env']['pillow']} / metrics {report['env']['metrics_version']}",
        f"- 夹具：{ctx.fixtures_note}",
        f"- 被测代码快照（并行开发期以此为准）：digest=`{report['code_snapshot']['digest']}`"
        f"（run_acceptance/metrics/document/render/onestroke/fourier/brush/decompose... 的 sha256 前 16 位）",
        f"- 预检：{_short(ctx.preflight, 400)}",
        "",
        f"## 汇总：通过 {summary['passed']} / 失败 {summary['failed']}"
        f" / 阻塞 {summary['blocked']} / 跳过 {summary['skipped']}"
        f" / 判据修正记录 {summary.get('broken', 0)}"
        f" / 未实现 {summary.get('not_implemented', 0)}（共 {summary['total']}）",
        f"　MUST 通过 {summary['must_passed']}/{summary['must_total']}"
        f"（失败 {summary['must_failed']}，阻塞 {summary['must_blocked']}）",
        f"　分组：" + "　".join(
            f"{g}组 {v.get(PASS, 0)}P/{v.get(FAIL, 0)}F/{v.get(NOT_IMPLEMENTED, 0)}NI"
            f"/{v.get(SKIP, 0)}S/{v.get(BLOCKED, 0)}B（共 {v.get('total', 0)}）"
            for g, v in sorted((summary.get("by_group") or {}).items())
        ),
        f"- **结论：{_verdict(summary)}**"
        f"　（ok = failed==0 且 blocked==0；SKIP/BROKEN/NOT_IMPLEMENTED 不算通过，但也不让验收假失败）",
        "",
    ]
    if first_failure:
        lines += [
            "## 第一处差异",
            "",
            f"- **{first_failure['id']}**（{first_failure['level']}）{first_failure['desc']}",
            f"- 实测：`{_short(first_failure['value'], 400)}`",
            f"- 阈值：{first_failure['threshold']}",
            f"- 备注：{first_failure['note']}",
            f"- 复现：`{first_failure['cmd']}`",
            "",
        ]
    blocked = [r for r in items if r["status"] == BLOCKED]
    skipped = [r for r in items if r["status"] == SKIP]
    broken = [r for r in items if r["status"] == BROKEN]
    not_impl = [r for r in items if r["status"] == NOT_IMPLEMENTED]
    if blocked:
        lines += ["## 阻塞项（BLOCKED，不计为通过）", ""]
        for r in blocked:
            lines.append(f"- {r['id']}（{r['level']}）：{r['note']}")
        lines.append("")
    if skipped:
        lines += ["## 跳过项（SKIP，不计为通过）", ""]
        for r in skipped:
            lines.append(f"- {r['id']}（{r['level']}）：{r['note']}")
        lines.append("")
    if not_impl:
        lines += ["## 未实现项（NOT_IMPLEMENTED，设计文档要求但 v2 明确不实现，不计入失败）", ""]
        for r in not_impl:
            lines.append(f"- {r['id']}（{r['level']}）：{r['note']}")
            lines.append(f"  - 设计口径：{r['threshold']}")
        lines.append("")
    if broken:
        lines += ["## 判据修正记录（BROKEN，旧判据已证伪/废止，不计入通过/失败）", ""]
        for r in broken:
            lines.append(f"- {r['id']}（{r['level']}）：{r['note']}")
            lines.append(f"  - 旧判据实测：`{_short(r['value'], 300)}`")
        lines.append("")
    lines += ["## 逐项结果", "",
              "| 项 | 级别 | 状态 | 实测 | 阈值 | 备注 |",
              "|---|---|---|---|---|---|"]
    for r in items:
        lines.append(
            f"| {r['id']} | {r['level']} | {r['status']} | `{_short(r['value'], 220)}` "
            f"| {_short(r['threshold'], 120)} | {_short(r['note'], 160)} |")
    lines.append("")
    if ctx.real:
        lines += ["## 真实图", ""]
        for name, entry in ctx.real.items():
            if not entry.get("exists"):
                lines.append(f"- {name}: SKIP（{entry.get('note')}）")
                continue
            lines.append(
                f"- **{name}**（R={entry.get('R')}，{entry.get('klass')}，"
                f"strokes={entry.get('stroke_count')}）：PSNR {entry.get('psnr_db')} dB，"
                f"SSIM {entry.get('ssim')}，结构 IoU {entry.get('structure', {}).get('iou')}，"
                f"色调 ΔE {entry.get('tone', {}).get('mean_delta_e')}，"
                f"线宽比 {entry.get('linewidth', {}).get('ratio')}")
            one = entry.get("onestroke") or {}
            lines.append(f"  - 一笔画：{_short({k: one.get(k) for k in ('status','N','path_deviation_px')}, 160)}")
            vid = entry.get("video") or {}
            lines.append(f"  - 视频 probe：{_short({k: vid.get(k) for k in ('status','frames','duration_s','fps','size')}, 160)}")
        lines.append("")
    lines += ["## 复现命令", "", "```bash"]
    for key, cmd in report["repro"].items():
        lines.append(f"# {key}")
        lines.append(cmd)
    lines += ["```", "", f"总耗时 {report['total_elapsed_s']}s"]
    report_md = os.path.join(ctx.report_dir, "report.md")
    with open(report_md, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return report_json, report_md


def run(*, quick: bool = False, items: list[str] | None = None,
        report_dir: str | None = None, skip_video: bool = False,
        verbose: bool = True) -> dict:
    """跑验收套件，返回 {summary, report_json, report_md, items}（Lead 的 CLI 依赖此签名）。"""
    t_start = time.perf_counter()
    selected = _verify_items_selection(items)
    ctx = Ctx(quick=quick, report_dir=report_dir or DEFAULT_REPORT_DIR,
              skip_video=skip_video, verbose=verbose)
    ctx.probe()
    ctx.log(f"[acceptance] {'quick' if quick else 'full'} 模式，项目 {len(selected)} 项，"
            f"报告目录 {ctx.report_dir}")
    if verbose:
        for name, state in ctx.preflight.items():
            ctx.log(f"  预检 {name}: {state}")
    # 夹具（缺则自动生成；生成器缺失 = 阻塞，而不是静默跳过）
    try:
        ctx.ensure_fixtures()
    except Blocked as exc:
        ctx.notes.append(f"夹具不可用: {exc}")
        ctx.fixtures_note = f"BLOCKED: {exc}"
    except FBSError as exc:
        ctx.notes.append(f"夹具生成失败: {exc.code}: {exc.message}")
        ctx.fixtures_note = f"BLOCKED: {exc.code}"
    ctx.log(f"[acceptance] 夹具: {ctx.fixtures_note}")

    results: list[dict] = []
    for iid in selected:
        meta = ITEMS[iid]
        ctx.log(f"  → {iid}（{meta['level']}）{meta['desc']}")
        rec = _execute_item(ctx, meta)
        results.append(rec)
        ctx.log(f"    {STATUS_MARK.get(rec['status'], rec['status'])} {iid}: "
                f"{_short(rec['value'], 140)}"
                + (f"  | {rec['note']}" if rec["status"] in (FAIL, BLOCKED, SKIP, BROKEN) else ""))
    # 真实图（全量模式；或用户显式要求 R3/R8 时）
    want_real = (not quick) and (items is None or any(i in selected for i in ("R3", "R8")))
    if want_real and ctx.preflight.get("document") == "ok":
        ctx.log("[acceptance] 真实图验收 ...")
        try:
            _run_real_images(ctx)
        except Blocked as exc:
            ctx.notes.append(f"真实图跳过: {exc}")
        except Exception as exc:  # noqa: BLE001 - 真实图环节失败要记录
            ctx.notes.append(f"真实图环节异常 {type(exc).__name__}: {exc}")
    elif not quick and items is not None:
        ctx.notes.append("过滤模式下未选中 R3/R8，真实图验收未运行（如需请加 R3 或 R8）")

    # 环境未就绪时补一条显式说明（README/报告都会看到）
    if all(v != "ok" for k, v in ctx.preflight.items()):
        ctx.notes.append("所有流水线模块均未就绪：本报告只包含静态/属性层项，其余为 BLOCKED")
    report_json, report_md = _write_reports(ctx, results, started=t_start)
    summary = _summary(results)
    # 失败第一处差异打印（stdout；验收纪律要求）
    fails = [r for r in results if r["status"] == FAIL]
    if fails:
        r = fails[0]
        print("\n[第一处差异] " + "=" * 60)
        print(f"  {r['id']}（{r['level']}）{r['desc']}")
        print(f"  实测: {_short(r['value'], 500)}")
        print(f"  阈值: {r['threshold']}")
        print(f"  备注: {r['note']}")
        print(f"  复现: {r['cmd']}")
        print("=" * 78)
    verdict = "OK" if summary.get("ok") else ("FAIL" if summary.get("failed") else "INCOMPLETE(有 BLOCKED)")
    print(f"\n[acceptance] {verdict} :: 通过 {summary['passed']} / 失败 {summary['failed']}"
          f" / 阻塞 {summary['blocked']} / 跳过 {summary['skipped']}"
          f" / 判据修正 {summary.get('broken', 0)} / 未实现 {summary.get('not_implemented', 0)}"
          f"（共 {summary['total']}，耗时 {time.perf_counter() - t_start:.1f}s）")
    print(f"[acceptance] 报告: {os.path.relpath(report_json, ROOT)}  /  "
          f"{os.path.relpath(report_md, ROOT)}")
    return {
        "summary": summary,
        "ok": bool(summary["ok"]),
        "report_json": report_json,
        "report_md": report_md,
        "items": results,
        "real_images": ctx.real,
        "preflight": ctx.preflight,
    }


# ===================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="fourier-brushstroke v2 验收套件（A/B/R/T）")
    ap.add_argument("--quick", action="store_true", help="快速模式：R=512、跳过视频/真实图一笔画")
    ap.add_argument("--items", default=None, help="只跑指定项，逗号分隔，如 A1,R2,T4")
    ap.add_argument("--report-dir", dest="report_dir", default=None)
    ap.add_argument("--skip-video", dest="skip_video", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    items = [s.strip() for s in args.items.split(",")] if args.items else None
    try:
        report = run(quick=bool(args.quick), items=items,
                     report_dir=args.report_dir, skip_video=bool(args.skip_video),
                     verbose=not args.quiet)
    except FBSError as exc:
        print(f"[错误] {exc.code}: {exc.message}", file=sys.stderr)
        for sug in exc.suggestions:
            print(f"  建议: {sug}", file=sys.stderr)
        return 2
    # 退出码语义：只有 failed==0 且 blocked==0 才算成功（BLOCKED/SKIP 不冒充 PASS）。
    return 0 if report["summary"].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())









