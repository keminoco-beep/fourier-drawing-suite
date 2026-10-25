"""Progressive drawing video for ``.fdraw``/``.fbs`` documents (WP-J).

The video is a *reveal* of the finished drawing: elements are painted in the
same order the renderers use (``layer`` → ``fill`` → document order, exactly
:func:`fourier_prompt.render_fast.truncate_doc`), one prefix per frame.  A frame
is only re-rendered when the number of visible elements changes, so a 480-frame
video of a 51-element drawing performs at most 51 renders.

Frame streaming
---------------
Frames are handed to ``imageio_ffmpeg``'s ``write_frames`` generator, i.e. a
``subprocess.Popen(ffmpeg, stdin=PIPE)`` that receives raw ``rgb24`` bytes.
Only the current frame is alive at any moment: no ``list`` of frames is built,
so peak memory is O(one frame) instead of O(frames × W × H × 3).  ``max_frames``
(600 hard ceiling, matching the batch constraint) caps the encode; when the
requested ``duration_s × fps`` exceeds it the timeline is shortened to
``max_frames / fps`` and a warning is recorded (never silently truncated).

Determinism
-----------
Rendering uses the deterministic fast backend (no randomness) and the reveal
planning is pure integer arithmetic.  The *evidence* is the per-frame SHA-256 of
the raw RGB payload, returned as ``frame_hashes`` / ``frame_hashes_sha256``:
two runs of the same input must produce the same sequence.  The mp4 container
bytes are hashed too (``sha256``) but are **not** the determinism criterion —
the container embeds encoder metadata (version string, muxing timestamps).

Failures
--------
Missing ffmpeg raises :class:`VideoError` with code ``VD_E_NO_FFMPEG`` (plus an
install hint) before any rendering starts; an encode that produces no bytes
raises ``VD_E_ENCODE``.  Nothing exits 0 silently.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from typing import Any, Iterable

import numpy as np

from .render_fast import render_auto, truncate_doc

try:  # pragma: no cover - environment dependent
    import imageio_ffmpeg  # type: ignore
except ImportError:  # pragma: no cover
    imageio_ffmpeg = None  # type: ignore

__all__ = [
    "VideoError",
    "HARD_MAX_FRAMES",
    "frame_indices",
    "sample_positions",
    "render_video",
    "render_video_from_fdraw",
    "render_frames",
    "probe_video",
    "resolve_ffmpeg",
]

#: absolute frame ceiling for one video (batch constraint "视频 ≤600 帧")
HARD_MAX_FRAMES = 600
#: constant encoder quality (imageio_ffmpeg 1..10 → libx264 crf 25..5)
_ENCODER_QUALITY = 7
#: default number of sampled frame hashes returned for cheap determinism checks
_SAMPLE_HASHES = 10

VALID_QUALITIES = ("fast", "exact")


class VideoError(Exception):
    """Video pipeline failure with a stable code (CLI maps it to an exit code)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        evidence: dict | None = None,
        hints: Iterable[str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence or {}
        self.hints = list(hints or [])


# ---------------------------------------------------------------------------
# ffmpeg discovery
# ---------------------------------------------------------------------------
_FFMPEG_HINT = "pip install imageio-ffmpeg（离线环境用离线 wheel；或设置 PATH 中的 ffmpeg）"


def resolve_ffmpeg() -> str:
    """Return the ffmpeg executable; raise :class:`VideoError` when absent."""
    if imageio_ffmpeg is None:
        raise VideoError(
            "VD_E_NO_FFMPEG",
            "缺少 imageio-ffmpeg，无法编码 mp4 视频",
            hints=[_FFMPEG_HINT],
        )
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - third-party backend
        raise VideoError(
            "VD_E_NO_FFMPEG",
            f"imageio-ffmpeg 找不到可用的 ffmpeg: {exc}",
            hints=[_FFMPEG_HINT],
        ) from exc
    if not exe or not os.path.isfile(exe):
        raise VideoError(
            "VD_E_NO_FFMPEG",
            f"imageio-ffmpeg 报告的 ffmpeg 路径不可用: {exe!r}",
            evidence={"ffmpeg": exe},
            hints=[_FFMPEG_HINT],
        )
    return str(exe)


# ---------------------------------------------------------------------------
# frame planning
# ---------------------------------------------------------------------------
def frame_indices(n_strokes: int, frames: int) -> list[int]:
    """Cumulative number of visible elements at each video frame.

    Returns a list of length ``frames``: entry ``i`` is the number of leading
    elements (in render order) painted in frame ``i``.  The first frame shows
    one element, the last frame shows all ``n_strokes``; the sequence is
    non-decreasing, so every frame adds zero or more new elements and nothing
    already drawn ever disappears.  With fewer frames than elements several
    elements are committed per frame; with more frames the tail holds.

    ``n_strokes == 0`` yields ``[0] * frames`` (a blank document still gets a
    valid, fully decodable video).  ``frames == 0`` yields ``[]``.
    """
    n = int(n_strokes)
    f = int(frames)
    if n < 0:
        raise ValueError(f"n_strokes 非法（>=0）: {n_strokes!r}")
    if f < 0:
        raise ValueError(f"frames 非法（>=0）: {frames!r}")
    if f == 0:
        return []
    if n == 0:
        return [0] * f
    if f == 1:
        return [n]
    span = n - 1
    return [1 + (i * span) // (f - 1) for i in range(f)]


def sample_positions(frames: int, count: int) -> list[int]:
    """Evenly spaced frame indices (inclusive of the first and last frame)."""
    f = int(frames)
    c = int(count)
    if f < 0:
        raise ValueError(f"frames 非法（>=0）: {frames!r}")
    if c < 0:
        raise ValueError(f"count 非法（>=0）: {count!r}")
    if f == 0:
        return []
    c = min(max(1, c), f)
    if c == 1:
        return [0]
    return sorted({int(round(i * (f - 1) / (c - 1))) for i in range(c)})


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------
def _int_arg(name: str, value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise VideoError("VD_E_ARG", f"{name} 必须是整数: {value!r}") from exc
    if isinstance(value, float) and not float(value).is_integer():
        raise VideoError("VD_E_ARG", f"{name} 必须是整数: {value!r}")
    if not (low <= number <= high):
        raise VideoError("VD_E_ARG", f"{name} 超出范围（{low}..{high}）: {value!r}")
    return number


def _check_quality(quality: Any) -> str:
    if not isinstance(quality, str) or quality not in VALID_QUALITIES:
        raise VideoError(
            "VD_E_ARG", f"quality 非法（fast|exact）: {quality!r}", evidence={"quality": quality}
        )
    return quality


def _check_codec(codec: Any) -> str:
    if not isinstance(codec, str) or not codec.strip():
        raise VideoError("VD_E_ARG", f"codec 非法: {codec!r}")
    return codec.strip()


def _check_background(background: Any) -> list[int] | None:
    if background is None:
        return None
    if isinstance(background, str) or not isinstance(background, (list, tuple)):
        raise VideoError("VD_E_ARG", f"background 需为 R,G,B 序列: {background!r}")
    values = list(background)
    if len(values) not in (3, 4):
        raise VideoError("VD_E_ARG", f"background 需 3 或 4 个分量: {background!r}")
    out: list[int] = []
    for value in values[:3]:
        try:
            channel = int(value)
        except (TypeError, ValueError) as exc:
            raise VideoError("VD_E_ARG", f"background 分量必须是整数: {background!r}") from exc
        if not (0 <= channel <= 255):
            raise VideoError("VD_E_ARG", f"background 分量超出 0..255: {background!r}")
        out.append(channel)
    return out


def _check_duration(duration_s: Any) -> float:
    try:
        value = float(duration_s)
    except (TypeError, ValueError) as exc:
        raise VideoError("VD_E_ARG", f"duration_s 必须是数字: {duration_s!r}") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise VideoError("VD_E_ARG", f"duration_s 必须 > 0: {duration_s!r}")
    if value > 3600.0:
        raise VideoError("VD_E_ARG", f"duration_s 过大（<=3600）: {duration_s!r}")
    return value


def _check_out(out: Any) -> str:
    if not isinstance(out, (str, os.PathLike)):
        raise VideoError("VD_E_ARG", f"out 必须是路径: {out!r}")
    path = os.fspath(out)
    if not path:
        raise VideoError("VD_E_ARG", "out 不能为空")
    if os.path.isdir(path):
        raise VideoError("VD_E_IO", f"输出路径是目录: {path}")
    parent = os.path.dirname(os.path.abspath(path))
    try:
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise VideoError("VD_E_IO", f"无法创建输出目录 {parent}: {exc}") from exc
    return path


# ---------------------------------------------------------------------------
# document loading
# ---------------------------------------------------------------------------
def _count_elements(doc: dict) -> int:
    return len([st for st in (doc.get("strokes") or []) if isinstance(st, dict)])


def _doc_from_fdraw(path: Any) -> dict:
    from .fdraw import FDrawError, unpack

    target = os.fspath(path)
    if not os.path.isfile(target):
        raise VideoError("VD_E_INPUT", f"文件不存在: {target}")
    try:
        unpacked = unpack(target)
    except FDrawError as exc:
        raise VideoError(
            exc.code, f"{target}: {exc.message}", evidence=getattr(exc, "evidence", None)
        ) from exc
    except OSError as exc:
        raise VideoError("VD_E_IO", f"无法读取 {target}: {exc}") from exc
    render = unpacked.get("render")
    if not isinstance(render, dict):
        raise VideoError("VD_E_RENDER", f"{target} 缺少 render.json 文档")
    return render


def _load_doc(doc_or_path: Any) -> dict:
    """Accept a render document dict or a ``.fdraw``/``.fbs``/``.json`` path."""
    if isinstance(doc_or_path, dict):
        return doc_or_path
    if isinstance(doc_or_path, (str, os.PathLike)):
        target = os.fspath(doc_or_path)
        if not os.path.isfile(target):
            raise VideoError("VD_E_INPUT", f"文件不存在: {target}")
        suffix = os.path.splitext(target)[1].lower()
        if suffix == ".fdraw":
            return _doc_from_fdraw(target)
        try:
            with open(target, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise VideoError("VD_E_JSON", f"JSON 解析失败: {target}: {exc}") from exc
        except OSError as exc:
            raise VideoError("VD_E_IO", f"无法读取 {target}: {exc}") from exc
        if not isinstance(data, dict):
            raise VideoError("VD_E_JSON", f"{target} 顶层不是 JSON 对象")
        return data
    raise VideoError("VD_E_ARG", f"doc_or_path 需为文档 dict 或路径: {type(doc_or_path).__name__}")


# ---------------------------------------------------------------------------
# memory / hashing helpers
# ---------------------------------------------------------------------------
def _process_memory_mb() -> dict:
    """Working-set / peak working-set of this process in MB (Windows; else empty)."""
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi = ctypes.WinDLL("psapi")
        probe = psapi.GetProcessMemoryInfo
        probe.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        probe.restype = wintypes.BOOL
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        if not probe(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return {}
        return {
            "rss_mb": round(counters.WorkingSetSize / (1024 * 1024), 2),
            "peak_rss_mb": round(counters.PeakWorkingSetSize / (1024 * 1024), 2),
        }
    except Exception:  # noqa: BLE001 - non Windows / restricted ctypes
        return {}


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_payload(img: np.ndarray) -> tuple[bytes, tuple[int, int]]:
    """Raw ``rgb24`` bytes for one frame, padding odd sides by edge replication.

    ``yuv420p`` (the only universally playable H.264 pixel format) requires even
    dimensions; the pad is one row/column at most and is deterministic.
    """
    arr = np.ascontiguousarray(img)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    height, width = int(arr.shape[0]), int(arr.shape[1])
    even_w = width + (width & 1)
    even_h = height + (height & 1)
    if (even_w, even_h) == (width, height):
        return arr.tobytes(), (width, height)
    padded = np.empty((even_h, even_w, 3), dtype=np.uint8)
    padded[:height, :width] = arr
    if even_w > width:
        padded[:height, width:] = arr[:, -1:]
    if even_h > height:
        padded[height:, :] = padded[height - 1 : height, :]
    return padded.tobytes(), (even_w, even_h)


# ---------------------------------------------------------------------------
# encoder
# ---------------------------------------------------------------------------
def _open_writer(path: str, size: tuple[int, int], fps: int, codec: str):
    """Start the ffmpeg subprocess and return its ``write_frames`` generator."""
    resolve_ffmpeg()  # fail fast with a stable code/message
    try:
        generator = imageio_ffmpeg.write_frames(
            path,
            tuple(int(v) for v in size),
            fps=float(fps),
            codec=codec,
            quality=_ENCODER_QUALITY,
            pix_fmt_in="rgb24",
            pix_fmt_out="yuv420p",
            macro_block_size=1,
            ffmpeg_log_level="warning",
        )
        next(generator)  # seed: spawns the subprocess
    except VideoError:
        raise
    except Exception as exc:  # noqa: BLE001 - Popen/OSError/RuntimeError
        raise VideoError(
            "VD_E_ENCODE",
            f"ffmpeg 写入器初始化失败: {exc}",
            evidence={"out": path, "size": list(size), "fps": int(fps), "codec": codec},
            hints=["检查 codec 名称（默认 libx264）与输出目录权限"],
        ) from exc
    return generator


def _probe_video(path: str) -> dict:
    """Decode-side metadata probe (size/fps/duration/frame estimate)."""
    reader = imageio_ffmpeg.read_frames(path)
    try:
        meta = dict(next(reader))
    finally:
        try:
            reader.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
    fps = float(meta.get("fps") or 0.0)
    duration = float(meta.get("duration") or 0.0)
    size = meta.get("size") or (0, 0)
    frames = int(round(duration * fps)) if (duration > 0.0 and fps > 0.0) else 0
    return {
        "size": [int(size[0]), int(size[1])],
        "fps": fps,
        "duration_s": duration,
        "frames": frames,
        "codec": meta.get("codec"),
        "pix_fmt": meta.get("pix_fmt"),
        "ffmpeg_version": meta.get("ffmpeg_version"),
    }


# ---------------------------------------------------------------------------
# planning shared by video + frames
# ---------------------------------------------------------------------------
def _plan_frames(duration_s: float, fps: int, max_frames: int) -> tuple[int, list[str]]:
    requested = max(1, int(round(duration_s * fps)))
    cap = int(max_frames)
    warnings: list[str] = []
    if cap > HARD_MAX_FRAMES:
        warnings.append(
            f"W_VIDEO_FRAME_CAP: max_frames={cap} 超过硬上限 {HARD_MAX_FRAMES}，按 {HARD_MAX_FRAMES} 帧执行"
        )
        cap = HARD_MAX_FRAMES
    frames = min(requested, cap)
    if frames < requested:
        warnings.append(
            f"W_VIDEO_FRAME_CAP: 请求 {requested} 帧（{duration_s:g}s@{fps}fps）> max_frames={cap}，"
            f"按时长上限编码 {frames} 帧（{frames / float(fps):.3f}s）"
        )
    return frames, warnings


def _absorb_render_info(info: dict, *, quality_used: set, fallbacks: list, approximations: set) -> None:
    used = info.get("quality")
    if used:
        quality_used.add(str(used))
    for reason in info.get("fallback_reason") or []:
        if reason not in fallbacks:
            fallbacks.append(str(reason))
    for shape in info.get("approximations") or []:
        approximations.add(str(shape))


# ---------------------------------------------------------------------------
# main entry points
# ---------------------------------------------------------------------------
def render_video(
    doc_or_path: Any,
    *,
    out: str,
    resolution: int = 512,
    duration_s: float = 20.0,
    fps: int = 24,
    quality: str = "fast",
    codec: str = "libx264",
    max_frames: int = 600,
    background=None,
) -> dict:
    """Render a progressive drawing video to ``out`` (mp4 by default).

    Elements are revealed in render order (layer → fill → document order); each
    frame carries the prefix of elements visible at that moment.  Frames are
    streamed straight into ffmpeg's stdin, so no frame list is retained.

    Returns ``{out, width, height, fps, frames, duration_s, bytes, sha256,
    quality, fallbacks, ...}``; see the module docstring for the determinism
    contract (``frame_hashes``).
    """
    started = time.perf_counter()
    doc = _load_doc(doc_or_path)
    out_path = _check_out(out)
    resolution_i = _int_arg("resolution", resolution, 8, 4096)
    fps_i = _int_arg("fps", fps, 1, 240)
    duration = _check_duration(duration_s)
    quality_s = _check_quality(quality)
    codec_s = _check_codec(codec)
    max_frames_i = _int_arg("max_frames", max_frames, 1, 1_000_000)
    background_rgb = _check_background(background)

    resolve_ffmpeg()  # before any rendering: no ffmpeg -> clear error, no half work
    elements = _count_elements(doc)
    frames, warnings = _plan_frames(duration, fps_i, max_frames_i)
    counts = frame_indices(elements, frames)

    quality_used: set[str] = set()
    fallbacks: list[str] = []
    approximations: set[str] = set()
    frame_hashes: list[str] = []
    unique_renders = 0
    render_s = 0.0
    encode_s = 0.0
    width = height = 0
    generator = None
    last_count: int | None = None
    payload: bytes | None = None
    failure: BaseException | None = None

    try:
        for count in counts:
            if count != last_count:
                t0 = time.perf_counter()
                # count == elements: the full document needs no copy
                subset = doc if count >= elements else truncate_doc(doc, at=count)
                img, info = render_auto(
                    subset,
                    resolution=resolution_i,
                    quality=quality_s,
                    return_info=True,
                    background=background_rgb,
                )
                render_s += time.perf_counter() - t0
                _absorb_render_info(
                    info,
                    quality_used=quality_used,
                    fallbacks=fallbacks,
                    approximations=approximations,
                )
                payload, (width, height) = _frame_payload(img)
                last_count = count
                unique_renders += 1
                if generator is None:
                    generator = _open_writer(out_path, (width, height), fps_i, codec_s)
            if generator is None:  # pragma: no cover - defensive (frames >= 1)
                raise VideoError("VD_E_ENCODE", "没有可编码的帧")
            if payload is None:  # pragma: no cover - defensive
                raise VideoError("VD_E_ENCODE", "帧数据为空")
            t0 = time.perf_counter()
            generator.send(payload)
            encode_s += time.perf_counter() - t0
            frame_hashes.append(hashlib.sha256(payload).hexdigest())
    except BaseException as exc:  # noqa: BLE001 - close ffmpeg, then re-raise
        failure = exc
    finally:
        if generator is not None:
            try:
                generator.close()
            except Exception as exc:  # noqa: BLE001 - bridge pipe / ffmpeg exit
                if failure is None:
                    failure = VideoError(
                        "VD_E_ENCODE",
                        f"ffmpeg 编码失败: {exc}",
                        evidence={"out": out_path, "codec": codec_s, "frames": frames},
                        hints=["检查 ffmpeg 可用性与 codec 名称"],
                    )
    if failure is not None:
        if isinstance(failure, VideoError):
            raise failure
        raise failure

    if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        raise VideoError(
            "VD_E_ENCODE",
            "ffmpeg 未生成有效视频（文件缺失或为空）",
            evidence={"out": out_path, "frames": frames, "codec": codec_s},
            hints=["检查 ffmpeg 版本是否支持该 codec", "检查磁盘剩余空间"],
        )

    video_bytes = os.path.getsize(out_path)
    try:
        probe = _probe_video(out_path)
    except Exception as exc:  # noqa: BLE001 - decoder refused the file
        raise VideoError(
            "VD_E_ENCODE",
            f"写入的视频无法解码: {exc}",
            evidence={"out": out_path, "bytes": video_bytes},
        ) from exc

    sampled = {
        str(index): frame_hashes[index]
        for index in sample_positions(frames, _SAMPLE_HASHES)
        if 0 <= index < len(frame_hashes)
    }
    elapsed = time.perf_counter() - started
    result: dict[str, Any] = {
        "out": os.path.abspath(out_path),
        "width": int(width),
        "height": int(height),
        "size": [int(width), int(height)],
        "fps": int(fps_i),
        "frames": int(frames),
        "duration_s": round(frames / float(fps_i), 6),
        "requested_frames": int(max(1, int(round(duration * fps_i)))),
        "requested_duration_s": round(duration, 6),
        "bytes": int(video_bytes),
        "sha256": _file_sha256(out_path),
        "quality": quality_s,
        "quality_used": sorted(quality_used),
        "fallbacks": list(fallbacks),
        "approximations": sorted(approximations),
        "codec": codec_s,
        "elements": int(elements),
        "unique_renders": int(unique_renders),
        "frame_hashes": frame_hashes,
        "frame_hashes_sha256": hashlib.sha256("".join(frame_hashes).encode("ascii")).hexdigest(),
        "frame_hashes_sampled": sampled,
        "warnings": warnings,
        "probe": probe,
        "elapsed_s": round(elapsed, 4),
        "render_s": round(render_s, 4),
        "encode_s": round(encode_s, 4),
        "ffmpeg": resolve_ffmpeg(),
        "imageio_ffmpeg": getattr(imageio_ffmpeg, "__version__", None),
    }
    result.update(_process_memory_mb())
    return result


def render_video_from_fdraw(path: Any, **kwargs: Any) -> dict:
    """``render_video`` for a ``.fdraw`` container (``render.json`` is authority)."""
    if not isinstance(path, (str, os.PathLike)):
        raise VideoError("VD_E_ARG", f"path 必须是 .fdraw 路径: {path!r}")
    doc = _doc_from_fdraw(path)
    result = render_video(doc, **kwargs)
    result["source"] = f"fdraw:{os.fspath(path)}"
    return result


def render_frames(
    doc_or_path: Any,
    *,
    out_dir: str,
    count: int = 10,
    resolution: int = 512,
    duration_s: float = 20.0,
    fps: int = 24,
    quality: str = "fast",
    max_frames: int = 600,
    background=None,
) -> dict:
    """Render ``count`` sampled reveal frames as PNGs into ``out_dir``.

    Uses the same planning as :func:`render_video`, so a PNG frame is exactly
    the RGB payload of that video frame (before even-size padding).
    """
    started = time.perf_counter()
    doc = _load_doc(doc_or_path)
    resolution_i = _int_arg("resolution", resolution, 8, 4096)
    fps_i = _int_arg("fps", fps, 1, 240)
    duration = _check_duration(duration_s)
    quality_s = _check_quality(quality)
    max_frames_i = _int_arg("max_frames", max_frames, 1, 1_000_000)
    count_i = _int_arg("count", count, 1, HARD_MAX_FRAMES)
    background_rgb = _check_background(background)
    if not isinstance(out_dir, (str, os.PathLike)) or not os.fspath(out_dir):
        raise VideoError("VD_E_ARG", f"out_dir 必须是目录路径: {out_dir!r}")
    directory = os.fspath(out_dir)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise VideoError("VD_E_IO", f"无法创建输出目录 {directory}: {exc}") from exc

    elements = _count_elements(doc)
    frames, warnings = _plan_frames(duration, fps_i, max_frames_i)
    counts = frame_indices(elements, frames)
    positions = sample_positions(frames, count_i)

    from PIL import Image

    quality_used: set[str] = set()
    fallbacks: list[str] = []
    approximations: set[str] = set()
    files: list[dict] = []
    cache: dict[int, tuple[np.ndarray, dict]] = {}
    for position in positions:
        count = counts[position]
        if count not in cache:
            subset = doc if count >= elements else truncate_doc(doc, at=count)
            img, info = render_auto(
                subset,
                resolution=resolution_i,
                quality=quality_s,
                return_info=True,
                background=background_rgb,
            )
            _absorb_render_info(
                info,
                quality_used=quality_used,
                fallbacks=fallbacks,
                approximations=approximations,
            )
            cache[count] = (img, info)
        img = cache[count][0]
        path = os.path.join(directory, f"frame_{position:04d}.png")
        try:
            Image.fromarray(img).save(path, format="PNG")
        except OSError as exc:
            raise VideoError("VD_E_IO", f"无法写入 {path}: {exc}") from exc
        files.append(
            {
                "frame": int(position),
                "elements": int(count),
                "path": path,
                "bytes": int(os.path.getsize(path)),
                "size": [int(img.shape[1]), int(img.shape[0])],
                "sha256": hashlib.sha256(np.ascontiguousarray(img).tobytes()).hexdigest(),
            }
        )
    return {
        "out_dir": os.path.abspath(directory),
        "resolution": int(resolution_i),
        "frames": int(frames),
        "requested_frames": int(max(1, int(round(duration * fps_i)))),
        "count": len(files),
        "elements": int(elements),
        "quality": quality_s,
        "quality_used": sorted(quality_used),
        "fallbacks": list(fallbacks),
        "approximations": sorted(approximations),
        "warnings": warnings,
        "files": files,
        "elapsed_s": round(time.perf_counter() - started, 4),
    }


def probe_video(path: str) -> dict:
    """Public wrapper around the decode-side probe (raises ``VD_E_INPUT``)."""
    if not isinstance(path, (str, os.PathLike)):
        raise VideoError("VD_E_ARG", f"path 必须是视频路径: {path!r}")
    target = os.fspath(path)
    if not os.path.isfile(target):
        raise VideoError("VD_E_INPUT", f"视频不存在: {target}")
    if os.path.getsize(target) <= 0:
        raise VideoError("VD_E_ENCODE", f"视频文件为空: {target}")
    resolve_ffmpeg()
    try:
        return _probe_video(target)
    except Exception as exc:  # noqa: BLE001 - decoder failure
        raise VideoError("VD_E_ENCODE", f"无法解码视频 {target}: {exc}") from exc
