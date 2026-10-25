"""Standalone progressive-video CLI (WP-J).

    python -m fourier_prompt.fdvideo_cli video  --fdraw X.fdraw --out X.mp4
        [--resolution 512] [--duration 20] [--fps 24]
        [--quality fast|exact] [--codec libx264] [--max-frames 600]
        [--background R,G,B] [--json]

    python -m fourier_prompt.fdvideo_cli frames --fdraw X.fdraw --out-dir D
        [--count 10] [--resolution 256] [--duration 20] [--fps 24]
        [--quality fast|exact] [--max-frames 600] [--json]

``--fdraw`` goes through :func:`fourier_prompt.fdraw.unpack` (``render.json`` is
the rendering authority); ``--fbs`` accepts a bare ``.fbs``/``.json`` document.

Exit codes: 0 ok, 1 input/render/encode failure, 2 usage (argparse or bad
argument values), 3 IO/internal.
"""

from __future__ import annotations

import argparse
import json
import sys

from .fdvideo import (
    HARD_MAX_FRAMES,
    VideoError,
    render_frames,
    render_video,
    render_video_from_fdraw,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3

_USAGE_CODES = frozenset({"VD_E_ARG", "VD_E_USAGE"})
_INTERNAL_CODES = frozenset({"VD_E_IO", "VD_E_INTERNAL"})


def _parse_background(text: str | None) -> list[int] | None:
    if not text:
        return None
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if len(parts) not in (3, 4):
        raise VideoError("VD_E_ARG", f"--background 需要 R,G,B，收到 {text!r}")
    try:
        values = [int(p) for p in parts[:3]]
    except ValueError as exc:
        raise VideoError("VD_E_ARG", f"--background 必须是整数: {text!r}") from exc
    if any(not (0 <= value <= 255) for value in values):
        raise VideoError("VD_E_ARG", f"--background 分量超出 0..255: {text!r}")
    return values


def _exit_code(exc: VideoError) -> int:
    if exc.code in _USAGE_CODES:
        return EXIT_USAGE
    if exc.code in _INTERNAL_CODES:
        return EXIT_INTERNAL
    return EXIT_FAIL


def _render_target(args: argparse.Namespace) -> dict:
    """Call the library with the parsed arguments (loader chosen by flag)."""
    common = dict(
        out=args.out,
        resolution=args.resolution,
        duration_s=args.duration,
        fps=args.fps,
        quality=args.quality,
        codec=args.codec,
        max_frames=args.max_frames,
        background=_parse_background(args.background),
    )
    if args.fdraw:
        return render_video_from_fdraw(args.fdraw, **common)
    result = render_video(args.fbs, **common)  # _load_doc: .fbs/.json
    result["source"] = f"fbs:{args.fbs}"
    return result


def _cmd_video(args: argparse.Namespace) -> int:
    result = _render_target(args)
    payload = {
        "command": "video",
        "out": result["out"],
        "source": result.get("source"),
        "width": result["width"],
        "height": result["height"],
        "fps": result["fps"],
        "frames": result["frames"],
        "duration_s": result["duration_s"],
        "bytes": result["bytes"],
        "sha256": result["sha256"],
        "quality": result["quality"],
        "quality_used": result["quality_used"],
        "fallbacks": result["fallbacks"],
        "codec": result["codec"],
        "elements": result["elements"],
        "unique_renders": result["unique_renders"],
        "frame_hashes_sha256": result["frame_hashes_sha256"],
        "frame_hashes_sampled": result["frame_hashes_sampled"],
        "warnings": result["warnings"],
        "probe": result["probe"],
        "elapsed_s": result["elapsed_s"],
        "render_s": result["render_s"],
        "encode_s": result["encode_s"],
        "rss_mb": result.get("rss_mb"),
        "peak_rss_mb": result.get("peak_rss_mb"),
        "ffmpeg": result["ffmpeg"],
        "imageio_ffmpeg": result["imageio_ffmpeg"],
    }
    if args.hashes:
        payload["frame_hashes"] = result["frame_hashes"]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return EXIT_OK
    print(
        f"video {payload['out']} {payload['width']}x{payload['height']} "
        f"{payload['frames']} frames @{payload['fps']}fps "
        f"({payload['duration_s']:.3f}s) quality={payload['quality']} "
        f"{payload['bytes']} bytes {payload['elapsed_s']:.2f}s"
    )
    print(
        f"  elements={payload['elements']} unique_renders={payload['unique_renders']} "
        f"sha256={payload['sha256'][:16]}… frame_hashes={payload['frame_hashes_sha256'][:16]}…"
    )
    print(f"  probe: {payload['probe']['size']} @{payload['probe']['fps']}fps "
          f"~{payload['probe']['frames']} frames, codec={payload['probe']['codec']}")
    for warning in payload["warnings"]:
        print(f"  warning: {warning}")
    for reason in payload["fallbacks"]:
        print(f"  fallback: {reason}")
    return EXIT_OK


def _cmd_frames(args: argparse.Namespace) -> int:
    common = dict(
        out_dir=args.out_dir,
        count=args.count,
        resolution=args.resolution,
        duration_s=args.duration,
        fps=args.fps,
        quality=args.quality,
        max_frames=args.max_frames,
        background=_parse_background(args.background),
    )
    target = args.fdraw or args.fbs
    result = render_frames(target, **common)
    result["command"] = "frames"
    result["source"] = f"fdraw:{args.fdraw}" if args.fdraw else f"fbs:{args.fbs}"
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return EXIT_OK
    print(
        f"frames {result['out_dir']} {result['count']} PNG "
        f"({result['resolution']}px, {result['frames']} planned frames, "
        f"{result['elements']} elements) {result['elapsed_s']:.2f}s"
    )
    for entry in result["files"]:
        print(f"  frame {entry['frame']:4d} elements={entry['elements']:3d} "
              f"{entry['size'][0]}x{entry['size'][1]} {entry['bytes']} bytes")
    for warning in result["warnings"]:
        print(f"  warning: {warning}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def _add_source(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fdraw", help="input .fdraw container (render.json via unpack)")
    group.add_argument("--fbs", help="input .fbs/.json document")


def _add_plan_opts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--resolution", type=int, default=512, help="long-side pixel size")
    parser.add_argument("--duration", type=float, default=20.0, help="target duration in seconds")
    parser.add_argument("--fps", type=int, default=24, help="frames per second")
    parser.add_argument("--quality", choices=("fast", "exact"), default="fast")
    parser.add_argument("--max-frames", type=int, default=600,
                        help=f"frame ceiling (hard ceiling {HARD_MAX_FRAMES})")
    parser.add_argument("--background", default=None, help="override background R,G,B")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.fdvideo_cli",
        description="fd-video: progressive drawing video (streamed, deterministic frames).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    video = sub.add_parser("video", help="encode a progressive reveal mp4")
    _add_source(video)
    _add_plan_opts(video)
    video.add_argument("--codec", default="libx264")
    video.add_argument("--out", required=True)
    video.add_argument("--hashes", action="store_true", help="include every frame hash in --json")
    video.add_argument("--json", action="store_true")
    video.set_defaults(func=_cmd_video)

    frames = sub.add_parser("frames", help="dump sampled reveal frames as PNG")
    _add_source(frames)
    _add_plan_opts(frames)
    frames.add_argument("--out-dir", required=True)
    frames.add_argument("--count", type=int, default=10)
    frames.add_argument("--json", action="store_true")
    frames.set_defaults(func=_cmd_frames)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except VideoError as exc:
        print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
        for hint in exc.hints:
            print(f"  hint: {hint}", file=sys.stderr)
        return _exit_code(exc)
    except ValueError as exc:
        print(f"error[VD_E_ARG]: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as exc:
        print(f"error[VD_E_INPUT]: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as exc:
        print(f"error[VD_E_IO]: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error[VD_E_INTERNAL]: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
