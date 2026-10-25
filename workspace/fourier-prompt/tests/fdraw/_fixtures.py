"""Shared fixtures for the ``.fdraw`` container tests (WP-E)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]
for candidate in (str(HERE), str(PKG_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fourier_prompt import fdraw  # noqa: E402

WORKSPACE = PKG_ROOT.parent
SKILL_DIR = WORKSPACE / "fourier-brushstroke"
ASSETS = SKILL_DIR / "assets" / "out"

SAMPLES = {
    "signature": ASSETS / "smoke" / "T1_signature.fbs.json",
    "icon": ASSETS / "acceptance" / "cases" / "T2_icon_R512" / "T2_icon.fbs.json",
    "solid": ASSETS / "acceptance" / "cases" / "T8a_solid_black_R512" / "T8a_solid_black.fbs.json",
}

ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def load_sample(name: str) -> dict:
    with open(SAMPLES[name], "r", encoding="utf-8") as fh:
        return json.load(fh)


def canonical(value) -> bytes:
    return fdraw.canonical_json_bytes(value)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def role_for(name: str) -> str:
    if name == "render.json":
        return "render"
    if name == "source.fdsl.json":
        return "source"
    if name.startswith("fonts/"):
        return "font"
    return "unknown"


def meta_for(
    files: dict,
    *,
    root: str | None = None,
    required: list[str] | None = None,
    schema: str = fdraw.FDRAW_VERSION,
    extra: dict | None = None,
) -> dict:
    """Build a minimally valid ``meta.json`` object for handcrafted ZIPs."""
    entries = []
    for name, data in files.items():
        if name == "meta.json":
            continue
        entries.append(
            {
                "path": name,
                "role": role_for(name),
                "bytes": len(data),
                "sha256": sha256(data),
            }
        )
    meta = {
        "schema": schema,
        "spec_version": fdraw.FDRAW_SPEC_VERSION,
        "capabilities": {
            "required": list(required or ["fourier/stroke"]),
            "optional": [],
        },
        "root_sha256": root or ("0" * 64),
        "entries": entries,
    }
    if extra:
        meta.update(extra)
    return meta


def make_zip(
    entries: list[tuple[str, bytes]],
    *,
    symlinks: set[str] | None = None,
    encrypted: set[str] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """Build a ZIP with fixed metadata (or deliberate symlink/encryption flags)."""
    symlinks = symlinks or set()
    encrypted = encrypted or set()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = compression
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            if name in symlinks:
                info.external_attr = 0o120777 << 16
            if name in encrypted:
                info.flag_bits |= 0x1
            zf.writestr(info, data)
    return buf.getvalue()


def container_bytes(
    files: dict[str, bytes],
    *,
    meta: dict | None = None,
    meta_bytes: bytes | None = None,
) -> bytes:
    """Assemble a container whose meta.json is derived from ``files``."""
    payload = dict(files)
    if meta_bytes is None:
        meta_bytes = canonical(meta if meta is not None else meta_for(payload))
    payload["meta.json"] = meta_bytes
    ordered = [("meta.json", meta_bytes)]
    ordered.extend((name, data) for name, data in payload.items() if name != "meta.json")
    return make_zip(ordered)


def rewrite_zip(blob: bytes, changes: dict[str, bytes]) -> bytes:
    """Replace selected entry payloads, preserving entry names/order."""
    with zipfile.ZipFile(io.BytesIO(blob), "r") as zf:
        entries = [(info.filename, changes.get(info.filename, zf.read(info))) for info in zf.infolist()]
    return make_zip(entries)


def zip_infos(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob), "r") as zf:
        return [(info, zf.read(info)) for info in zf.infolist()]


@contextlib.contextmanager
def patched_limits(**values):
    original = dict(fdraw.LIMITS)
    fdraw.LIMITS.update(values)
    try:
        yield
    finally:
        fdraw.LIMITS.clear()
        fdraw.LIMITS.update(original)


def error_code(exc: Exception) -> str:
    return getattr(exc, "code", "")
