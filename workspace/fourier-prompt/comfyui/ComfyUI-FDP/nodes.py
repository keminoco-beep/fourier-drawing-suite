"""ComfyUI-FDP — eight V1-API nodes for the Fourier Drawing Program (`.fdraw`).

Design rules (KICKOFF v5 §3, analysis/11 §4.6):

* only the public ``fourier_prompt`` API is used (``compile`` / ``fdraw`` /
  ``render_fast`` / ``fdvideo``); no compiler, container or renderer logic is
  copied into this package;
* no third-party custom-node dependency — stdlib + numpy + Pillow (via
  ``fourier_prompt``) and, in a real ComfyUI, the bundled torch for IMAGE;
* every heavy operation is an ``async def`` that delegates to
  ``asyncio.to_thread`` so the ComfyUI event loop keeps serving the UI;
* errors raise ``RuntimeError("FD_E_*: human readable reason")``; nothing is
  swallowed and no empty image is fabricated;
* ``FDRAW``/``FDSL`` are in-memory Python values (never workflow-persisted).

``IS_CHANGED`` / ``fingerprint_inputs`` return a content fingerprint so a
parameter change re-runs a node while an unchanged graph is served from cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from fourier_prompt import fdvideo
from fourier_prompt import fdraw as container
from fourier_prompt import render_fast
from fourier_prompt.compile import CompileError, compile_plan
from fourier_prompt.repair import repair_plan
from fourier_prompt.schema import FDSchemaError

LOG = logging.getLogger("fourier_prompt.comfyui")

#: custom ComfyUI types (strings only; used for link validation / UI colour)
FDRAW = "FDRAW"
FDSL = "FDSL"

#: user metadata keys preserved when Save re-packs an already-opened container
_USER_META = ("title", "id", "created", "description", "author", "tags")

_DEFAULT_FDSL = json.dumps(
    {
        "schema": "fdsl/2.1",
        "name": "comfy-demo",
        "canvas": {"width": 128, "height": 128, "background": [250, 248, 242]},
        "palette": {"ink": "#202630", "gold": "#d8a83e"},
        "layers": [{"id": 0, "name": "ink"}],
        "elements": [
            {
                "kind": "fourier_gen",
                "gen": "circle",
                "layer": 0,
                "id": "c1",
                "transform": {"center": [64, 64], "scale": 0.6},
                "params": {"r": 1.0},
                "brush": {"preset": "ink_gpen", "width": 2.0, "color": "ink"},
            }
        ],
    },
    ensure_ascii=False,
    indent=1,
)


# ---------------------------------------------------------------------------
# FDRAW value
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FDrawValue:
    """In-memory ``FDRAW`` value passed between nodes.

    It carries the already-validated ``render.json`` document, the editable
    FDSL source (when present) and a content fingerprint used by ``IS_CHANGED``.
    It is *not* serialised into workflow JSON; downstream nodes re-create it.
    """

    render: dict
    source_fdsl: dict | None = None
    meta: dict = field(default_factory=dict)
    path: str | None = None
    root_sha256: str = ""
    zip_bytes: int = 0
    inspection: dict = field(default_factory=dict)
    fonts: tuple[tuple[str, bytes], ...] = ()
    report: dict | None = None

    @property
    def cache_key(self) -> str:
        if self.root_sha256:
            return self.root_sha256
        return _hash_value({"render": self.render, "source": self.source_fdsl})


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _hash_value(value: Any) -> str:
    payload = json.dumps(
        _stable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stable(value: Any) -> Any:
    if isinstance(value, FDrawValue):
        return {"__fdraw__": value.cache_key}
    if isinstance(value, np.ndarray):
        data = np.ascontiguousarray(value)
        return {
            "__array__": list(data.shape),
            "sha256": hashlib.sha256(data.tobytes()).hexdigest(),
        }
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": hashlib.sha256(bytes(value)).hexdigest()}
    if isinstance(value, dict):
        return {
            str(key): _stable(val)
            for key, val in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _fingerprint(*parts: Any) -> str:
    return _hash_value(parts)


def _fail(code: str, message: str, hint: str | None = None) -> None:
    text = f"{code}: {message}"
    if hint:
        text += f"｜{hint}"
    raise RuntimeError(text)


def _check_interrupt() -> None:
    """Raise when ComfyUI's cancellation flag is set (no import requirement)."""
    try:
        from comfy import model_management  # type: ignore
    except Exception:
        return
    try:
        interrupted = bool(model_management.processing_interrupted())
    except Exception:
        return
    if interrupted:
        raise InterruptedError("FD_E_INTERRUPTED: 用户取消了执行｜点 Queue 重新运行即可")


def _folder_paths():
    try:
        import folder_paths  # type: ignore
    except Exception:
        return None
    return folder_paths


def _search_dirs() -> list[str]:
    dirs: list[str] = []
    fp = _folder_paths()
    if fp is not None:
        for getter in ("get_input_directory", "get_output_directory"):
            fn = getattr(fp, getter, None)
            if callable(fn):
                try:
                    base = os.fspath(fn())
                except Exception:
                    continue
                if base:
                    dirs.append(os.path.join(base, "fdraw"))
                    dirs.append(base)
        registry = getattr(fp, "folder_names_and_paths", None)
        if isinstance(registry, dict):
            entry = registry.get("fdraw")
            if isinstance(entry, (tuple, list)) and entry:
                for registered in entry[0] or []:
                    try:
                        dirs.append(os.fspath(registered))
                    except TypeError:
                        continue
    out: list[str] = []
    seen: set[str] = set()
    for directory in dirs:
        key = os.path.normcase(os.path.abspath(directory))
        if key not in seen:
            seen.add(key)
            out.append(directory)
    return out


def _list_fdraw_files() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for directory in _search_dirs():
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in entries:
            if not name.lower().endswith(".fdraw"):
                continue
            if not os.path.isfile(os.path.join(directory, name)):
                continue
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _lookup_path(name: str, path_override: str = "") -> str | None:
    override = (path_override or "").strip().strip('"')
    if override:
        candidate = os.path.abspath(os.path.expanduser(override))
        return candidate if os.path.isfile(candidate) else None
    raw = (name or "").strip().strip('"')
    if not raw or raw in ("<empty>", "None"):
        return None
    if os.path.isfile(raw):
        return os.path.abspath(raw)
    for directory in _search_dirs():
        candidate = os.path.abspath(os.path.join(directory, raw))
        if os.path.isfile(candidate):
            return candidate
    return None


def _require_path(name: str, path_override: str = "") -> str:
    path = _lookup_path(name, path_override)
    if path:
        return path
    searched = ", ".join(_search_dirs()) or "<no folder_paths>"
    _fail(
        "FD_E_FILE_NOT_FOUND",
        f"找不到 .fdraw 文件: {path_override or name!r}",
        f"可选目录: {searched}；也可把绝对路径填进 path_override",
    )
    return ""  # pragma: no cover - _fail raises


def _sanitize_prefix(prefix: str, default: str) -> str:
    text = (prefix or "").strip().replace("\\", "/")
    if not text:
        text = default
    if text.lower().endswith(".fdraw"):
        text = text[: -len(".fdraw")]
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        _fail("FD_E_PREFIX", f"filename_prefix 非法: {prefix!r}", "不要使用 .. 或绝对路径")
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", part) for part in parts]
    return "/".join(parts)


def _unique_output(directory: str, prefix: str, suffix: str) -> str:
    os.makedirs(directory, exist_ok=True)
    base = os.path.basename(prefix)
    for counter in range(1, 100000):
        candidate = os.path.join(directory, f"{base}_{counter:05d}{suffix}")
        if not os.path.exists(candidate):
            return candidate
    _fail("FD_E_IO", f"无法在 {directory} 找到可用的输出文件名")


def _parse_background(value: str) -> tuple[int, int, int] | None:
    text = (value or "auto").strip().lower()
    if text in ("", "auto", "none", "transparent"):
        return None
    named = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "paper": (250, 248, 242),
        "grey": (128, 128, 128),
        "gray": (128, 128, 128),
    }
    if text in named:
        return named[text]
    match = re.fullmatch(r"#?([0-9a-f]{6})", text)
    if match:
        raw = match.group(1)
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    _fail(
        "FD_E_BACKGROUND",
        f"background 非法: {value!r}",
        '用 "auto"、"#rrggbb" 或 white/black/paper/grey',
    )
    return None  # pragma: no cover


def _resolve_resolution(value: Any, doc: dict) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        _fail("FD_E_RESOLUTION", f"resolution 必须是整数: {value!r}")
    if requested > 0:
        if not 8 <= requested <= 4096:
            _fail("FD_E_RESOLUTION", f"resolution 必须在 8..4096: {requested}")
        return requested
    contract = doc.get("render_contract") or {}
    target = contract.get("target_resolution") if isinstance(contract, dict) else None
    try:
        target_i = int(target) if target is not None else 0
    except (TypeError, ValueError):
        target_i = 0
    if target_i > 0:
        return min(4096, max(8, target_i))
    return 512


def _image_tensor(array: np.ndarray):
    """``(H,W,3) uint8`` -> ComfyUI IMAGE.

    Real ComfyUI always has torch; the numpy fallback keeps the package usable
    in headless/mock environments (KICKOFF v5 §3.2).
    """
    data = np.ascontiguousarray(array, dtype=np.uint8)
    try:
        import torch  # type: ignore
    except ImportError:
        return (data.astype(np.float32) / 255.0)[None, ...]
    return torch.from_numpy(data).float().div_(255.0).unsqueeze(0)


def _user_meta(meta: dict) -> dict:
    if not isinstance(meta, dict):
        return {}
    return {key: meta[key] for key in _USER_META if key in meta and meta[key] is not None}


def _expect_fdraw(value: Any, node: str) -> FDrawValue:
    if not isinstance(value, FDrawValue):
        _fail(
            "FD_E_TYPE",
            f"{node} 需要 FDRAW 输入，收到 {type(value).__name__}",
            "把 Load FDraw 或 Compile FDSL 的输出连到这个输入",
        )
    return value


def _coerce_plan(fdsl_value: Any, fdsl_json: str) -> dict:
    if isinstance(fdsl_value, FDrawValue):  # defensive: never accept FDRAW as FDSL
        _fail("FD_E_TYPE", "FDSL 输入收到了 FDRAW", "连接 Compile 的 FDSL 输出或粘贴 JSON")
    if isinstance(fdsl_value, dict):
        return fdsl_value
    raw = fdsl_value if isinstance(fdsl_value, str) and fdsl_value.strip() else fdsl_json
    if not isinstance(raw, str) or not raw.strip():
        _fail("FD_E_FDSL_EMPTY", "FDSL JSON 为空", "在 fdsl_json 里粘贴一个 fdsl/2.x 对象")
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(
            "FD_E_FDSL_JSON",
            f"FDSL JSON 解析失败: {exc.msg} (line {exc.lineno} column {exc.colno})",
            "检查 JSON 引号/逗号；不要包含 markdown 代码围栏",
        )
    if not isinstance(plan, dict):
        _fail("FD_E_FDSL_JSON", f"FDSL 必须是 JSON 对象，收到 {type(plan).__name__}")
    return plan


# ---------------------------------------------------------------------------
# blocking implementations (run inside asyncio.to_thread)
# ---------------------------------------------------------------------------
def _load_blocking(path: str, strict: bool) -> tuple[FDrawValue, str]:
    started = time.perf_counter()
    try:
        unpacked = container.unpack(path)
    except container.FDrawError as exc:
        _fail(exc.code, exc.message, "用 fd-render info 检查文件是否损坏/版本过新")
    except OSError as exc:
        _fail("FD_E_IO", f"读取失败 {path}: {exc}")
    meta = unpacked["meta"]
    render = unpacked["render"]
    capabilities = meta.get("capabilities") or {}
    required = list(capabilities.get("required") or [])
    if strict:
        unknown = [cap for cap in required if cap not in container.KNOWN_CAPABILITIES]
        if unknown:
            _fail(
                "FD_E_CAPABILITY",
                f"文件要求本读取端不支持的能力: {unknown}",
                "取消 strict，或升级 fourier_prompt 后重试",
            )
        if unpacked["root_sha256"] != unpacked["root_sha256_recorded"]:
            _fail("FD_E_INTEGRITY", "root_sha256 与 manifest 不一致")
    try:
        inspection = container.inspect(path)
    except container.FDrawError as exc:  # pragma: no cover - unpack already stricter
        _fail(exc.code, f"inspect 失败: {exc.message}")
    fonts = tuple((name, raw) for name, raw in sorted(unpacked["fonts"].items()))
    value = FDrawValue(
        render=render,
        source_fdsl=unpacked["source_fdsl"],
        meta=meta,
        path=os.path.abspath(path),
        root_sha256=unpacked["root_sha256"],
        zip_bytes=int(unpacked["zip_bytes"]),
        inspection=inspection,
        fonts=fonts,
    )
    counts = meta.get("counts") or {}
    canvas = meta.get("canvas") or {}
    summary = (
        f"{os.path.basename(path)}: {canvas.get('width')}x{canvas.get('height')} units, "
        f"{counts.get('elements', len(render.get('strokes') or []))} elements "
        f"({counts.get('strokes')} strokes + {counts.get('fills')} fills), "
        f"{len(meta.get('layers') or [])} layers, {len(fonts)} fonts, "
        f"root={unpacked['root_sha256'][:12]}…, {value.zip_bytes} B, "
        f"{time.perf_counter() - started:.2f}s"
    )
    return value, summary


def _save_blocking(value: FDrawValue, filename_prefix: str) -> tuple[str, str]:
    fp = _folder_paths()
    if fp is None or not callable(getattr(fp, "get_output_directory", None)):
        output_dir = os.path.join(tempfile.gettempdir(), "comfyui-fdp", "output")
    else:
        try:
            output_dir = os.fspath(fp.get_output_directory())
        except Exception as exc:  # pragma: no cover - host integration
            _fail("FD_E_IO", f"无法获取 ComfyUI output 目录: {exc}")
    prefix = _sanitize_prefix(filename_prefix, "fdraw/art")
    subdir = os.path.dirname(prefix)
    target_dir = os.path.join(output_dir, subdir) if subdir else output_dir
    path = _unique_output(target_dir, prefix, ".fdraw")
    actual = hashlib.sha256(container.canonical_json_bytes(value.render)).hexdigest()
    if value.root_sha256 and actual != value.root_sha256:
        _fail(
            "FD_E_INTEGRITY",
            "FDRAW 的 render 内容与记录的 root_sha256 不一致，拒绝写盘",
            "重新 Load/Compile；不要手工修改 FDRAW 对象",
        )
    try:
        container.pack(
            value.render,
            source_fdsl=value.source_fdsl,
            meta=_user_meta(value.meta),
            fonts=list(value.fonts) or None,
            out=path,
        )
    except container.FDrawError as exc:
        _fail(exc.code, f"打包失败: {exc.message}")
    except OSError as exc:
        _fail("FD_E_IO", f"写入失败 {path}: {exc}")
    rel = os.path.relpath(path, output_dir).replace("\\", "/")
    return path, rel


def _compile_blocking(plan: dict, resolution: int, repair: bool, strict: bool):
    warnings: list[Any] = []
    prepared = plan
    if repair:
        try:
            prepared, repair_warnings = repair_plan(plan)
            warnings.extend(repair_warnings)
        except FDSchemaError as exc:
            _fail(exc.code, exc.message, "修正 FDSL 或关闭 repair 后重试")
    try:
        compiled = compile_plan(prepared, resolution=resolution)
    except CompileError as exc:
        _fail(exc.code, exc.message, "检查 canvas/elements/palette；参见 FDSL 规范")
    except FDSchemaError as exc:
        _fail(exc.code, exc.message)
    warnings.extend(compiled.get("warnings") or [])
    if strict and warnings:
        first = warnings[0] if isinstance(warnings[0], dict) else {"code": "FP_W_UNKNOWN"}
        _fail(
            "FD_E_SCHEMA",
            f"strict 模式下存在 {len(warnings)} 条 warning（首条 {first.get('code')}）",
            "修正 warning 或关闭 strict",
        )
    blob = container.pack(compiled["fbs"], source_fdsl=prepared)
    inspection = container.inspect(blob)
    stats = compiled.get("stats") or {}
    value = FDrawValue(
        render=compiled["fbs"],
        source_fdsl=prepared,
        meta={},
        path=None,
        root_sha256=inspection["root_sha256"],
        zip_bytes=len(blob),
        inspection=inspection,
        fonts=(),
        report={"stats": stats, "warnings": warnings},
    )
    report = {
        "resolution": resolution,
        "elements": len(compiled["fbs"].get("strokes") or []),
        "stats": stats,
        "warning_count": len(warnings),
        "warning_codes": sorted({str(w.get("code")) for w in warnings if isinstance(w, dict)}),
        "root_sha256": inspection["root_sha256"],
        "zip_bytes": len(blob),
        "repair": bool(repair),
        "strict": bool(strict),
        "cache_key": value.cache_key,
    }
    return value, json.dumps(report, ensure_ascii=False, indent=1, default=str)


def _render_blocking(
    value: FDrawValue,
    resolution: int,
    quality: str,
    background: str,
    supersample: int,
    width_scale: float,
) -> tuple[np.ndarray, dict]:
    doc = value.render
    res = _resolve_resolution(resolution, doc)
    quality_s = str(quality or "fast").lower()
    if quality_s not in ("fast", "exact"):
        _fail("FD_E_QUALITY", f"quality 非法: {quality!r}", "用 fast 或 exact")
    ss = int(supersample)
    if not 1 <= ss <= 4:
        _fail("FD_E_SUPERSAMPLE", f"supersample 必须在 1..4: {ss}")
    ws = float(width_scale)
    if ws < 0.0 or not np.isfinite(ws):
        _fail("FD_E_WIDTH_SCALE", f"width_scale 非法: {width_scale!r}")
    started = time.perf_counter()
    try:
        image, info = render_fast.render_auto(
            doc,
            resolution=res,
            quality=quality_s,
            return_info=True,
            supersample=ss,
            background=_parse_background(background),
            width_scale=ws if ws > 0.0 else None,
        )
    except (ValueError, RuntimeError) as exc:
        _fail("FD_E_RENDER", f"渲染失败: {exc}", "试 exact 质量或检查 .fdraw 是否完整")
    info = dict(info)
    info["elapsed_s"] = round(time.perf_counter() - started, 4)
    info["cache_key"] = value.cache_key
    if info.get("fallback_reason"):
        LOG.info("FDP render fell back to exact: %s", info["fallback_reason"])
    return image, info


def _preview_blocking(
    value: FDrawValue,
    fraction: float,
    resolution: int,
    quality: str,
    background: str,
    supersample: int,
) -> tuple[np.ndarray, dict]:
    frac = float(fraction)
    if not np.isfinite(frac) or not 0.0 < frac <= 1.0:
        _fail("FD_E_PREVIEW_FRACTION", f"fraction 必须在 (0,1]: {fraction!r}")
    strokes = [item for item in (value.render.get("strokes") or []) if isinstance(item, dict)]
    doc = value.render
    if strokes and frac < 1.0:
        doc = render_fast.truncate_doc(value.render, fraction=frac)
    return _render_blocking(
        FDrawValue(
            render=doc,
            source_fdsl=value.source_fdsl,
            meta=value.meta,
            path=value.path,
            root_sha256=value.root_sha256,
            zip_bytes=value.zip_bytes,
            inspection=value.inspection,
            fonts=value.fonts,
        ),
        resolution,
        quality,
        background,
        supersample,
        0.0,
    )


def _video_blocking(
    value: FDrawValue,
    duration_s: float,
    fps: int,
    resolution: int,
    quality: str,
    max_frames: int,
    background: str,
    filename_prefix: str,
) -> tuple[str, str]:
    fp = _folder_paths()
    if fp is None or not callable(getattr(fp, "get_output_directory", None)):
        output_dir = os.path.join(tempfile.gettempdir(), "comfyui-fdp", "output")
    else:
        try:
            output_dir = os.fspath(fp.get_output_directory())
        except Exception:  # pragma: no cover - host integration
            _fail("FD_E_IO", "无法获取 ComfyUI output 目录")
    prefix = _sanitize_prefix(filename_prefix, "fdp/video")
    subdir = os.path.dirname(prefix)
    target_dir = os.path.join(output_dir, subdir) if subdir else output_dir
    path = _unique_output(target_dir, prefix, ".mp4")
    started = time.perf_counter()
    try:
        result = fdvideo.render_video(
            value.render,
            out=path,
            resolution=_resolve_resolution(resolution, value.render),
            duration_s=float(duration_s),
            fps=int(fps),
            quality=str(quality or "fast").lower(),
            max_frames=int(max_frames),
            background=_parse_background(background),
        )
    except fdvideo.VideoError as exc:
        _fail(exc.code, exc.message, "；".join(exc.hints) or "检查 ffmpeg / 参数")
    except (ValueError, OSError) as exc:
        _fail("FD_E_VIDEO", f"视频渲染失败: {exc}")
    info = (
        f"frames={result['frames']} fps={result['fps']} "
        f"size={result['width']}x{result['height']} bytes={result['bytes']} "
        f"sha256={str(result['sha256'])[:12]}… elapsed={time.perf_counter() - started:.2f}s"
    )
    return os.path.abspath(result["out"]), info


def _inspect_blocking(value: FDrawValue) -> str:
    inspection = value.inspection or {}
    render_block = inspection.get("render") or {}
    counts = inspection.get("counts") or {}
    strokes = [item for item in (value.render.get("strokes") or []) if isinstance(item, dict)]
    fills = sum(1 for item in strokes if item.get("fill_role") == "region_fill")
    actual = hashlib.sha256(container.canonical_json_bytes(value.render)).hexdigest()
    payload = {
        "path": value.path or "<compiled>",
        "schema": inspection.get("schema"),
        "spec_version": inspection.get("spec_version"),
        "canvas": render_block.get("canvas")
        or {
            "width": (value.meta.get("canvas") or {}).get("width"),
            "height": (value.meta.get("canvas") or {}).get("height"),
        },
        "element_count": len(strokes),
        "stroke_count": len(strokes) - fills,
        "fill_count": fills,
        "text_count": render_block.get("text_count", counts.get("texts", 0)),
        "layer_count": len(render_block.get("layers") or value.meta.get("layers") or []),
        "layers": render_block.get("layers") or value.meta.get("layers") or [],
        "font_count": len(value.fonts),
        "fonts": [name for name, _raw in value.fonts],
        "root_sha256": value.root_sha256,
        "root_sha256_ok": (actual == value.root_sha256) if value.root_sha256 else False,
        "zip_bytes": value.zip_bytes,
        "uncompressed_bytes": inspection.get("uncompressed_bytes"),
        "capabilities": inspection.get("capabilities"),
        "profiles": inspection.get("profiles"),
        "has_source": bool(value.source_fdsl),
        "cache_key": value.cache_key,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------
class _FDPNode:
    """Shared cache-fingerprint helpers for all nodes."""

    CATEGORY = "FDP"

    @staticmethod
    def _fingerprint(*parts: Any) -> str:
        return _fingerprint(*parts)

    def IS_CHANGED(self, **kwargs: Any) -> str:
        return self._fingerprint(self.__class__.__name__, sorted(kwargs.items()))

    def fingerprint_inputs(self, *args: Any, **kwargs: Any) -> str:
        """Newer ComfyUI prefers this hook; dispatch to the subclass fingerprint."""
        return self.IS_CHANGED(*args, **kwargs)


class FDPLoadFdraw(_FDPNode):
    CATEGORY = "FDP/1 Input"
    RETURN_TYPES = (FDRAW, "STRING")
    RETURN_NAMES = ("fdraw", "summary")
    FUNCTION = "load"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fdraw": (
                    cls._list_fdraw(),
                    {"tooltip": "input/fdraw 或 output/fdraw 下的 .fdraw；也可用 path_override 填绝对路径"},
                ),
                "strict": ("BOOLEAN", {"default": False, "tooltip": "严格校验：未知必需能力直接失败"}),
            },
            "optional": {
                "path_override": (
                    "STRING",
                    {"default": "", "tooltip": "绝对路径/UNC；非空时优先于下拉框"},
                )
            },
        }

    @classmethod
    def _list_fdraw(cls) -> list[str]:
        found = _list_fdraw_files()
        return found or ["<empty>"]

    def IS_CHANGED(self, fdraw: Any = "", strict: bool = False, path_override: str = "", **kwargs: Any) -> str:
        path = _lookup_path(str(fdraw or ""), str(path_override or ""))
        if not path:
            return self._fingerprint("missing", str(fdraw), str(path_override), bool(strict))
        try:
            stat = os.stat(path)
        except OSError:
            return self._fingerprint("unreadable", path, bool(strict))
        return self._fingerprint("load", path, stat.st_size, stat.st_mtime_ns, bool(strict))

    async def load(self, fdraw: Any = "", strict: bool = False, path_override: str = "", **kwargs: Any):
        _check_interrupt()
        path = _require_path(str(fdraw or ""), str(path_override or ""))
        value, summary = await asyncio.to_thread(_load_blocking, path, bool(strict))
        LOG.info("FDP load %s", summary)
        return (value, summary)


class FDPSaveFdraw(_FDPNode):
    CATEGORY = "FDP/1 Input"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    FUNCTION = "save"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fdraw": (FDRAW,),
                "filename_prefix": (
                    "STRING",
                    {"default": "fdraw/art", "tooltip": "相对 output/ 的前缀；自动追加 _00001.fdraw"},
                ),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    def IS_CHANGED(self, fdraw: Any = None, filename_prefix: str = "", **kwargs: Any) -> str:
        return self._fingerprint("save", _stable(fdraw), str(filename_prefix))

    async def save(
        self,
        fdraw: Any,
        filename_prefix: str = "fdraw/art",
        prompt: Any = None,
        extra_pnginfo: Any = None,
        **kwargs: Any,
    ):
        _check_interrupt()
        value = _expect_fdraw(fdraw, "Save FDraw")
        path, rel = await asyncio.to_thread(_save_blocking, value, str(filename_prefix))
        LOG.info("FDP saved %s", path)
        ui = {
            "fdp_fdraw": [
                {
                    "filename": os.path.basename(path),
                    "subfolder": os.path.dirname(rel),
                    "type": "output",
                }
            ]
        }
        return {"ui": ui, "result": (rel,)}


class FDPCompileFDSL(_FDPNode):
    CATEGORY = "FDP/2 Compile"
    RETURN_TYPES = (FDRAW, "STRING")
    RETURN_NAMES = ("fdraw", "report")
    FUNCTION = "compile"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fdsl_json": (
                    "STRING",
                    {"multiline": True, "default": _DEFAULT_FDSL, "tooltip": "FDSL v2.1 JSON 文本（不要代码围栏）"},
                ),
                "resolution": ("INT", {"default": 256, "min": 16, "max": 4096, "step": 16}),
                "repair": ("BOOLEAN", {"default": True, "tooltip": "编译前做确定性 repair"}),
                "strict": ("BOOLEAN", {"default": False, "tooltip": "任何 warning 都转为错误"}),
            },
            "optional": {
                "fdsl": ("FDSL", {"forceInput": True, "tooltip": "连接 FDSL 输出时优先于 fdsl_json"})
            },
        }

    def IS_CHANGED(
        self,
        fdsl_json: str = "",
        resolution: int = 256,
        repair: bool = True,
        strict: bool = False,
        fdsl: Any = None,
        **kwargs: Any,
    ) -> str:
        return self._fingerprint(
            "compile", str(fdsl_json), int(resolution), bool(repair), bool(strict), _stable(fdsl)
        )

    async def compile(
        self,
        fdsl_json: str = "",
        resolution: int = 256,
        repair: bool = True,
        strict: bool = False,
        fdsl: Any = None,
        **kwargs: Any,
    ):
        _check_interrupt()
        plan = _coerce_plan(fdsl, str(fdsl_json))
        value, report = await asyncio.to_thread(
            _compile_blocking, plan, int(resolution), bool(repair), bool(strict)
        )
        LOG.info(
            "FDP compile %s elements root=%s",
            len(value.render.get("strokes") or []),
            value.root_sha256[:12],
        )
        return (value, report)


class FDPRenderFdraw(_FDPNode):
    CATEGORY = "FDP/3 Render"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "render"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fdraw": (FDRAW,),
                "resolution": (
                    "INT",
                    {"default": 0, "min": 0, "max": 4096, "step": 8, "tooltip": "0 = 用文件契约的 target_resolution"},
                ),
                "quality": (["fast", "exact"], {"default": "fast"}),
                "background": ("STRING", {"default": "auto", "tooltip": "auto | #rrggbb | white/black/paper"}),
            },
            "optional": {
                "supersample": ("INT", {"default": 2, "min": 1, "max": 4}),
                "width_scale": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 3.0, "step": 0.01}),
            },
        }

    def IS_CHANGED(
        self,
        fdraw: Any = None,
        resolution: int = 0,
        quality: str = "fast",
        background: str = "auto",
        supersample: int = 2,
        width_scale: float = 0.0,
        **kwargs: Any,
    ) -> str:
        return self._fingerprint(
            "render",
            _stable(fdraw),
            int(resolution),
            str(quality),
            str(background),
            int(supersample),
            float(width_scale),
        )

    async def render(
        self,
        fdraw: Any,
        resolution: int = 0,
        quality: str = "fast",
        background: str = "auto",
        supersample: int = 2,
        width_scale: float = 0.0,
        **kwargs: Any,
    ):
        _check_interrupt()
        value = _expect_fdraw(fdraw, "Render FDraw")
        image, info = await asyncio.to_thread(
            _render_blocking,
            value,
            int(resolution),
            str(quality),
            str(background),
            int(supersample),
            float(width_scale),
        )
        LOG.info(
            "FDP render %dx%d quality=%s elapsed=%.2fs",
            image.shape[1],
            image.shape[0],
            info.get("quality"),
            info.get("elapsed_s", 0.0),
        )
        return (_image_tensor(image),)


class FDPPreviewFdraw(_FDPNode):
    CATEGORY = "FDP/3 Render"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "preview"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fdraw": (FDRAW,),
                "fraction": (
                    "FLOAT",
                    {"default": 0.35, "min": 0.01, "max": 1.0, "step": 0.01, "tooltip": "只画前 N% 元素"},
                ),
                "resolution": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 8}),
                "quality": (["fast", "exact"], {"default": "fast"}),
                "background": ("STRING", {"default": "auto"}),
            },
            "optional": {"supersample": ("INT", {"default": 2, "min": 1, "max": 4})},
        }

    def IS_CHANGED(
        self,
        fdraw: Any = None,
        fraction: float = 0.35,
        resolution: int = 0,
        quality: str = "fast",
        background: str = "auto",
        supersample: int = 2,
        **kwargs: Any,
    ) -> str:
        return self._fingerprint(
            "preview",
            _stable(fdraw),
            float(fraction),
            int(resolution),
            str(quality),
            str(background),
            int(supersample),
        )

    async def preview(
        self,
        fdraw: Any,
        fraction: float = 0.35,
        resolution: int = 0,
        quality: str = "fast",
        background: str = "auto",
        supersample: int = 2,
        **kwargs: Any,
    ):
        _check_interrupt()
        value = _expect_fdraw(fdraw, "Preview FDraw")
        image, _info = await asyncio.to_thread(
            _preview_blocking,
            value,
            float(fraction),
            int(resolution),
            str(quality),
            str(background),
            int(supersample),
        )
        return (_image_tensor(image),)


class FDPVideoFdraw(_FDPNode):
    CATEGORY = "FDP/3 Render"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("mp4_path", "info")
    FUNCTION = "video"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fdraw": (FDRAW,),
                "duration_s": ("FLOAT", {"default": 20.0, "min": 0.5, "max": 120.0, "step": 0.5}),
                "fps": ("INT", {"default": 24, "min": 1, "max": 60}),
                "resolution": ("INT", {"default": 512, "min": 8, "max": 2048, "step": 8}),
                "quality": (["fast", "exact"], {"default": "fast"}),
            },
            "optional": {
                "max_frames": (
                    "INT",
                    {"default": 600, "min": 1, "max": 600, "tooltip": "帧预算硬上限；超过会均匀降帧"},
                ),
                "background": ("STRING", {"default": "auto"}),
                "filename_prefix": ("STRING", {"default": "fdp/video"}),
            },
        }

    def IS_CHANGED(
        self,
        fdraw: Any = None,
        duration_s: float = 20.0,
        fps: int = 24,
        resolution: int = 512,
        quality: str = "fast",
        max_frames: int = 600,
        background: str = "auto",
        filename_prefix: str = "fdp/video",
        **kwargs: Any,
    ) -> str:
        return self._fingerprint(
            "video",
            _stable(fdraw),
            float(duration_s),
            int(fps),
            int(resolution),
            str(quality),
            int(max_frames),
            str(background),
            str(filename_prefix),
        )

    async def video(
        self,
        fdraw: Any,
        duration_s: float = 20.0,
        fps: int = 24,
        resolution: int = 512,
        quality: str = "fast",
        max_frames: int = 600,
        background: str = "auto",
        filename_prefix: str = "fdp/video",
        **kwargs: Any,
    ):
        _check_interrupt()
        value = _expect_fdraw(fdraw, "Video FDraw")
        path, info = await asyncio.to_thread(
            _video_blocking,
            value,
            float(duration_s),
            int(fps),
            int(resolution),
            str(quality),
            int(max_frames),
            str(background),
            str(filename_prefix),
        )
        LOG.info("FDP video %s (%s)", path, info)
        return (path, info)


class FDPInspectFdraw(_FDPNode):
    CATEGORY = "FDP/4 Inspect"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("info_json",)
    FUNCTION = "inspect"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"fdraw": (FDRAW,)}}

    def IS_CHANGED(self, fdraw: Any = None, **kwargs: Any) -> str:
        return self._fingerprint("inspect", _stable(fdraw))

    async def inspect(self, fdraw: Any, **kwargs: Any):
        value = _expect_fdraw(fdraw, "Inspect FDraw")
        payload = await asyncio.to_thread(_inspect_blocking, value)
        return (payload,)


class FDPBatchFdraw(_FDPNode):
    CATEGORY = "FDP/5 Batch"
    RETURN_TYPES = (FDRAW, "STRING")
    RETURN_NAMES = ("fdraw_list", "summaries")
    OUTPUT_IS_LIST = (True, True)
    FUNCTION = "batch"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "paths": (
                    "STRING",
                    {"multiline": True, "default": "", "tooltip": "每行一个 .fdraw（相对名或绝对路径）"},
                ),
                "inspect": ("BOOLEAN", {"default": True, "tooltip": "为每项生成 inspect JSON 摘要"}),
            },
            "optional": {
                "skip_missing": ("BOOLEAN", {"default": False, "tooltip": "跳过找不到的行而不是整体失败"})
            },
        }

    def IS_CHANGED(self, paths: str = "", inspect: bool = True, skip_missing: bool = False, **kwargs: Any) -> str:
        return self._fingerprint("batch", str(paths), bool(inspect), bool(skip_missing))

    async def batch(self, paths: str = "", inspect: bool = True, skip_missing: bool = False, **kwargs: Any):
        _check_interrupt()
        raw_lines = [line.strip() for line in str(paths or "").splitlines()]
        wanted = [line for line in raw_lines if line and not line.startswith("#")]
        if not wanted:
            _fail("FD_E_BATCH_EMPTY", "批量输入为空", "每行填一个 .fdraw 文件名或绝对路径")
        values, reports = await asyncio.to_thread(
            _batch_blocking, wanted, bool(inspect), bool(skip_missing)
        )
        return (values, reports)


def _batch_blocking(wanted: list[str], with_inspect: bool, skip_missing: bool) -> tuple[list, list[str]]:
    values: list[FDrawValue] = []
    reports: list[str] = []
    for item in wanted:
        path = _lookup_path(item, "")
        if path is None:
            if skip_missing:
                continue
            _fail("FD_E_FILE_NOT_FOUND", f"批量项找不到: {item!r}", "检查路径或开启 skip_missing")
        try:
            value, _summary = _load_blocking(path, False)
        except RuntimeError:
            if skip_missing:
                continue
            raise
        values.append(value)
        report = {
            "source": item,
            "path": value.path,
            "root_sha256": value.root_sha256,
            "canvas": (value.meta.get("canvas") or {}),
            "elements": len(value.render.get("strokes") or []),
            "layers": len(value.meta.get("layers") or []),
        }
        if with_inspect:
            report["inspect"] = json.loads(_inspect_blocking(value))
        reports.append(json.dumps(report, ensure_ascii=False))
    if not values:
        _fail("FD_E_BATCH_EMPTY", "批量输入没有成功加载任何文件")
    return values, reports


NODE_CLASS_MAPPINGS = {
    "FDPLoadFdraw": FDPLoadFdraw,
    "FDPSaveFdraw": FDPSaveFdraw,
    "FDPCompileFDSL": FDPCompileFDSL,
    "FDPRenderFdraw": FDPRenderFdraw,
    "FDPPreviewFdraw": FDPPreviewFdraw,
    "FDPVideoFdraw": FDPVideoFdraw,
    "FDPInspectFdraw": FDPInspectFdraw,
    "FDPBatchFdraw": FDPBatchFdraw,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FDPLoadFdraw": "Load FDraw (.fdraw)",
    "FDPSaveFdraw": "Save FDraw (.fdraw)",
    "FDPCompileFDSL": "Compile FDSL → FDraw",
    "FDPRenderFdraw": "Render FDraw",
    "FDPPreviewFdraw": "Preview FDraw (prefix)",
    "FDPVideoFdraw": "FDraw → Video (mp4)",
    "FDPInspectFdraw": "Inspect FDraw",
    "FDPBatchFdraw": "Batch FDraw",
}
