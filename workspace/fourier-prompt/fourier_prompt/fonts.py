"""Font loading, validation, metadata and build-time subsetting (WP-K).

FontTools is a **build-time / compile-time** dependency only: ``text.py`` bakes
glyph outlines into plain fill polygons, so the skill renderer and a shipped
``.fbs`` never need a font parser.  Import ``fontTools`` lazily (inside the
functions) so importing this module costs nothing.

Asset policy (KICKOFF v5 §2.1 / analysis/10 §2.6):

* default font: ``assets/fonts/NotoSansSC-VF.ttf`` -- a static, renamed subset
  of Noto Sans SC (OFL-1.1) built with ``fontTools`` by :func:`subset_font`;
* the subset is renamed ``FdrawSubset NotoSansSC <hash8>`` (OFL RFN safety);
* ``assets/fonts/OFL.txt`` + ``assets/fonts/LICENSES.md`` ship next to it.

Failure is always structured: :class:`FontError` carries an ``FP_E_*`` code.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

__all__ = [
    "ASSETS_DIR",
    "DEFAULT_FONT_PATH",
    "DEFAULT_FONT_NAME",
    "FLATTEN_PX_DEFAULT",
    "FontError",
    "FontHandle",
    "common_charset",
    "default_font_path",
    "font_info",
    "load_font",
    "require_glyph",
    "subset_font",
]

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
DEFAULT_FONT_NAME = "NotoSansSC-VF.ttf"
DEFAULT_FONT_PATH = ASSETS_DIR / DEFAULT_FONT_NAME
FLATTEN_PX_DEFAULT = 0.25

#: Tables a TrueType/OpenType font needs before we trust it for outlines.
_REQUIRED_TABLES = ("head", "hhea", "hmtx", "cmap", "maxp", "name", "OS/2")
_OUTLINE_TABLES = ("glyf", "loca", "CFF ", "CFF2")

#: Test / sample characters that must survive subsetting (DoD WP-K).
SAMPLE_TEXT = "你好，Fourier! 2026"

#: GB2312 level-1 hanzi (3755 chars) is a reasonable "common Chinese" set.
_GB2312_LEVEL1_HI = range(0xB0, 0xD8)

_COMMON_PUNCT = (
    "，。！？：；、…—·“”‘’（）《》【】〈〉「」『』～￥·"
    "！＂＃＄％＆＇＊＋，－．／０１２３４５６７８９：；＜＝＞？＠"
)


class FontError(RuntimeError):
    """Structured font failure (``FP_E_TEXT_FONT`` / ``FP_E_FONT_SUBSET``)."""

    def __init__(self, code: str, message: str, path: str | os.PathLike | None = None):
        self.code = str(code)
        self.message = str(message)
        self.path = str(path) if path is not None else ""
        super().__init__(f"{self.code}: {self.message}" + (f" [{self.path}]" if self.path else ""))

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


class FontHandle:
    """An open, validated font plus the metrics/outline caches the compiler needs."""

    __slots__ = (
        "path",
        "data_sha256",
        "size",
        "ttfont",
        "upem",
        "cmap",
        "glyph_order",
        "glyph_set",
        "ascent",
        "descent",
        "line_gap",
        "family",
        "subfamily",
        "postscript_name",
        "license_name",
        "license_url",
        "tables",
        "_advance",
        "_contours",
        "_names",
        "_gid_by_char",
    )

    def __init__(self, path: str, data: bytes, ttfont):
        self.path = str(path)
        self.data_sha256 = hashlib.sha256(data).hexdigest()
        self.size = len(data)
        self.ttfont = ttfont
        self.tables = tuple(sorted(ttfont.keys()))
        self.upem = int(ttfont["head"].unitsPerEm)
        self.cmap = ttfont.getBestCmap() or {}
        self.glyph_order = ttfont.getGlyphOrder()
        self.glyph_set = ttfont.getGlyphSet()
        self._advance: dict[int, int] = {}
        self._contours: dict[tuple[int, float], list] = {}
        self._names: dict[int, str] = {}
        self._gid_by_char: dict[str, int] = {}
        self.ascent, self.descent, self.line_gap = _metrics(ttfont, self.upem)
        self.family, self.subfamily, self.postscript_name, self.license_name, self.license_url = _name_info(ttfont)

    # -- metrics ---------------------------------------------------------
    @property
    def ascent_ratio(self) -> float:
        return self.ascent / float(self.upem)

    @property
    def descent_ratio(self) -> float:
        return self.descent / float(self.upem)

    @property
    def line_gap_ratio(self) -> float:
        return self.line_gap / float(self.upem)

    # -- glyph lookup ----------------------------------------------------
    def glyph_id(self, char: str) -> int | None:
        if not isinstance(char, str) or len(char) != 1:
            return None
        cached = self._gid_by_char.get(char)
        if cached is not None:
            return cached if cached >= 0 else None
        gid = self.cmap.get(ord(char))
        if gid is not None:
            # fontTools cmaps map codepoint -> glyph name, not glyph id.
            gid = int(self.ttfont.getGlyphID(gid))
        self._gid_by_char[char] = -1 if gid is None else int(gid)
        return None if gid is None else int(gid)

    def has_glyph(self, char: str) -> bool:
        return self.glyph_id(char) is not None

    def glyph_name(self, gid: int) -> str:
        name = self._names.get(gid)
        if name is None:
            name = self.glyph_order[gid]
            self._names[gid] = name
        return name

    def advance_units(self, gid: int) -> int:
        cached = self._advance.get(gid)
        if cached is None:
            cached = int(self.ttfont["hmtx"].metrics[self.glyph_name(gid)][0])
            self._advance[gid] = cached
        return cached

    def info(self) -> dict:
        return {
            "path": self.path,
            "file": os.path.basename(self.path),
            "size_bytes": self.size,
            "sha256": self.data_sha256,
            "family": self.family,
            "subfamily": self.subfamily,
            "postscript_name": self.postscript_name,
            "license": self.license_name or "",
            "license_url": self.license_url or "",
            "upem": self.upem,
            "ascender_units": self.ascent,
            "descender_units": self.descent,
            "line_gap_units": self.line_gap,
            "ascent_ratio": round(self.ascent_ratio, 6),
            "descent_ratio": round(self.descent_ratio, 6),
            "line_gap_ratio": round(self.line_gap_ratio, 6),
            "cmap_entries": len(self.cmap),
            "glyphs": len(self.glyph_order),
            "tables": list(self.tables),
            "fonttools": _fonttools_version(),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[int, int, FontHandle]] = {}


def default_font_path() -> str:
    """Absolute path of the packaged default font (may not exist in a source tree)."""
    return str(DEFAULT_FONT_PATH)


def _fonttools_version() -> str:
    try:
        import fontTools  # noqa: PLC0415 - optional build-time dependency

        return str(getattr(fontTools, "version", "unknown"))
    except Exception:  # pragma: no cover - only when fontTools is absent
        return ""


def _metrics(ttfont, upem: int) -> tuple[int, int, int]:
    os2 = ttfont["OS/2"] if "OS/2" in ttfont else None
    ascent = descent = line_gap = None
    if os2 is not None:
        try:
            ascent = int(os2.sTypoAscender)
            descent = int(os2.sTypoDescender)
            line_gap = int(getattr(os2, "sTypoLineGap", 0) or 0)
        except Exception:
            ascent = descent = line_gap = None
    if not ascent or not descent:
        ascent = int(ttfont["hhea"].ascent)
        descent = int(ttfont["hhea"].descent)
        line_gap = int(getattr(ttfont["hhea"], "lineGap", 0) or 0)
    if ascent <= 0 or descent >= 0 or upem <= 0:
        raise FontError(
            "FP_E_TEXT_FONT",
            f"font has invalid vertical metrics (upem={upem}, ascent={ascent}, descent={descent})",
        )
    return ascent, descent, max(0, int(line_gap or 0))


def _name_info(ttfont) -> tuple[str, str, str, str, str]:
    family = subfamily = postscript = license_name = license_url = ""
    try:
        names = ttfont["name"].names
    except Exception:
        return "", "", "", "", ""
    for record in names:
        try:
            text = record.toUnicode()
        except Exception:
            continue
        if record.nameID == 1 and not family:
            family = text
        elif record.nameID == 2 and not subfamily:
            subfamily = text
        elif record.nameID == 6 and not postscript:
            postscript = text
        elif record.nameID == 13 and not license_name:
            license_name = text
        elif record.nameID == 14 and not license_url:
            license_url = text
    return family, subfamily, postscript, license_name, license_url


def load_font(path: str | os.PathLike | FontHandle | None = None) -> FontHandle:
    """Load and validate a font for outline extraction.

    ``path=None`` means the packaged default asset.  Missing files, unreadable
    fonts, missing required tables or a broken ``upem`` raise
    :class:`FontError` with code ``FP_E_TEXT_FONT`` -- never a silent fallback
    to a system font (that would break cross-machine determinism).
    """
    if isinstance(path, FontHandle):
        return path
    target = Path(default_font_path() if path is None else os.fspath(path)).expanduser()
    location = str(target.resolve()) if target.exists() else str(target)
    try:
        stat = target.stat()
    except OSError as exc:
        raise FontError(
            "FP_E_TEXT_FONT",
            f"font file not found or unreadable ({exc.__class__.__name__}: {exc}); "
            f"no system-font fallback is allowed",
            location,
        ) from exc
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(location)
    if cached is not None and cached[0] == key[0] and cached[1] == key[1]:
        return cached[2]
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise FontError("FP_E_TEXT_FONT", f"cannot read font: {exc}", location) from exc
    if not data:
        raise FontError("FP_E_TEXT_FONT", "font file is empty", location)
    try:
        from fontTools.ttLib import TTFont  # noqa: PLC0415 - build-time dependency
    except Exception as exc:  # pragma: no cover - environment guard
        raise FontError(
            "FP_E_TEXT_FONT",
            f"fontTools is required to compile text (build/compile time only): {exc}",
            location,
        ) from exc
    try:
        ttfont = TTFont(io.BytesIO(data), lazy=True)
    except Exception as exc:
        raise FontError("FP_E_TEXT_FONT", f"not a readable TTF/OTF font: {exc}", location) from exc

    missing = [table for table in _REQUIRED_TABLES if table not in ttfont]
    if missing:
        raise FontError(
            "FP_E_TEXT_FONT", f"font is missing required tables: {missing}", location
        )
    if not any(table in ttfont for table in _OUTLINE_TABLES):
        raise FontError(
            "FP_E_TEXT_FONT",
            "font has no glyf/CFF outline table (bitmap-only fonts are unsupported)",
            location,
        )
    try:
        upem = int(ttfont["head"].unitsPerEm)
    except Exception as exc:
        raise FontError("FP_E_TEXT_FONT", f"font head.unitsPerEm is unreadable: {exc}", location) from exc
    if not 16 <= upem <= 16384:
        raise FontError("FP_E_TEXT_FONT", f"font unitsPerEm {upem} outside [16, 16384]", location)
    try:
        cmap = ttfont.getBestCmap() or {}
    except Exception as exc:
        raise FontError("FP_E_TEXT_FONT", f"font cmap is unreadable: {exc}", location) from exc
    if not cmap:
        raise FontError("FP_E_TEXT_FONT", "font has no usable Unicode cmap", location)
    try:
        handle = FontHandle(location, data, ttfont)
    except FontError:
        raise
    except Exception as exc:
        raise FontError("FP_E_TEXT_FONT", f"font metadata is invalid: {exc}", location) from exc
    _CACHE[location] = (key[0], key[1], handle)
    return handle


def font_info(path: str | os.PathLike | FontHandle | None = None) -> dict:
    """Return metadata for ``path`` (default asset when ``None``); see :class:`FontHandle`."""
    return load_font(path).info()


def require_glyph(font: FontHandle, char: str, *, where: str = "") -> int:
    """Return the glyph id for ``char`` or raise ``FP_E_TEXT_GLYPH`` (never skip)."""
    gid = font.glyph_id(char)
    if gid is None:
        codepoint = ord(char) if isinstance(char, str) and len(char) == 1 else -1
        raise FontError(
            "FP_E_TEXT_GLYPH",
            f"font {font.family or os.path.basename(font.path)!r} does not cover "
            f"U+{codepoint:04X} {char!r}; no system-font fallback is allowed"
            + (f" (at {where})" if where else ""),
            font.path,
        )
    return gid


# ---------------------------------------------------------------------------
# Build-time subsetting
# ---------------------------------------------------------------------------


def common_charset(text: str = SAMPLE_TEXT) -> str:
    """ASCII + GB2312 level-1 hanzi + common punctuation + ``text`` (deterministic)."""
    chars = set()
    chars.update(chr(code) for code in range(0x20, 0x7F))
    for lead in _GB2312_LEVEL1_HI:
        for trail in range(0xA1, 0xFF):
            try:
                chars.add(bytes((lead, trail)).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    chars.update(_COMMON_PUNCT)
    chars.update(str(text))
    chars.discard("\n")
    chars.discard("\r")
    chars.discard("\t")
    return "".join(sorted(chars))


def _sha256(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rename_subset(ttfont, family: str, subfamily: str, postscript: str) -> None:
    name_table = ttfont["name"]
    for record in name_table.names:
        try:
            if record.nameID == 1:
                record.string = family
            elif record.nameID == 2:
                record.string = subfamily
            elif record.nameID == 4:
                record.string = family
            elif record.nameID == 6:
                record.string = postscript
            elif record.nameID == 16:
                record.string = family
            elif record.nameID == 17:
                record.string = subfamily
        except Exception:
            continue


def subset_font(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    text: str,
    flavor: str | None = None,
    weight: float | None = 400,
    rename: bool = True,
) -> dict:
    """Subset (and optionally pin the variable-font weight of) ``src`` into ``dst``.

    Build-time only.  Returns a metadata dict (``source_sha256``, ``charset``,
    ``glyphs``, ``bytes``, ``sha256``, ``renamed``, ``command``) that
    ``LICENSES.md`` mirrors.  Deterministic: the timestamp is not recalculated
    and the output name is derived from the source/charset hashes.
    """
    source = Path(os.fspath(src)).expanduser()
    out = Path(os.fspath(dst)).expanduser()
    if not isinstance(text, str) or not text:
        raise FontError("FP_E_FONT_SUBSET", "subset text/charset is empty", str(source))
    if not source.is_file():
        raise FontError("FP_E_FONT_SUBSET", "source font not found", str(source))
    try:
        from fontTools.subset import Options, Subsetter  # noqa: PLC0415
        from fontTools.ttLib import TTFont  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - environment guard
        raise FontError("FP_E_FONT_SUBSET", f"fontTools is not available: {exc}", str(source)) from exc

    source_sha = _sha256(source)
    charset = "".join(sorted({ch for ch in text if ch not in "\n\r\t"}))
    charset_sha = hashlib.sha256(charset.encode("utf-8")).hexdigest()
    tag = hashlib.sha256((source_sha + charset_sha).encode("ascii")).hexdigest()[:8]
    family = f"FdrawSubset NotoSansSC {tag}"
    postscript = f"FdrawSubsetNotoSansSC-{tag}"

    font = TTFont(str(source), lazy=False, recalcTimestamp=False)
    try:
        pinned = None
        if weight is not None and "fvar" in font:
            from fontTools.varLib.instancer import instantiateVariableFont  # noqa: PLC0415

            axes = {axis.axisTag for axis in font["fvar"].axes}
            location = {"wght": float(weight)} if "wght" in axes else {}
            if location:
                instantiateVariableFont(font, location, inplace=True, updateFontNames=False)
                pinned = float(weight)

        options = Options()
        options.layout_features = []
        options.hinting = False
        options.desubroutinize = True
        options.notdef_outline = True
        options.recommended_glyphs = True
        options.name_IDs = ["*"]
        options.name_legacy = True
        options.name_languages = ["*"]
        options.recalc_timestamp = False
        if "DSIG" not in options.drop_tables:
            options.drop_tables.append("DSIG")
        subsetter = Subsetter(options=options)
        subsetter.populate(text=charset)
        subsetter.subset(font)

        if rename:
            _rename_subset(font, family, "Regular", postscript)
        if "OS/2" in font:
            font["OS/2"].usWeightClass = 400 if pinned is not None else int(
                getattr(font["OS/2"], "usWeightClass", 400) or 400
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        font.save(str(out), reorderTables=True)
    except FontError:
        raise
    except Exception as exc:
        raise FontError("FP_E_FONT_SUBSET", f"subsetting failed: {exc}", str(out)) from exc
    finally:
        font.close()

    # Hard gate: reload and prove the cmap covers every requested character.
    check = TTFont(str(out), lazy=True)
    try:
        cmap = check.getBestCmap() or {}
        missing = sorted({ch for ch in charset if ord(ch) not in cmap}, key=ord)
        if missing:
            raise FontError(
                "FP_E_FONT_SUBSET",
                "subset is missing characters: " + ", ".join(f"U+{ord(ch):04X} {ch!r}" for ch in missing),
                str(out),
            )
        glyphs = len(check.getGlyphOrder())
        if "fvar" in check:
            # A pinned static asset is what outline extraction documents; a
            # leftover VF would make outlines depend on the default instance.
            if pinned is not None:
                raise FontError("FP_E_FONT_SUBSET", "variable font was not instanced", str(out))
    finally:
        check.close()

    result = {
        "path": str(out),
        "source": str(source),
        "source_sha256": source_sha,
        "charset_chars": len(charset),
        "charset_sha256": charset_sha,
        "family": family if rename else "",
        "postscript_name": postscript if rename else "",
        "renamed": bool(rename),
        "weight": pinned,
        "glyphs": glyphs,
        "bytes": out.stat().st_size,
        "sha256": _sha256(out),
        "fonttools": _fonttools_version(),
        "command": (
            f"pyftsubset <source> --text-file=charset.txt --output-file={out} "
            "--layout-features='' --no-hinting --desubroutinize --notdef-outline "
            "--recommended-glyphs --name-IDs='*' --name-legacy --name-languages='*' "
            "--drop-tables+=DSIG --no-recalc-timestamp"
        ),
    }
    return result


# ---------------------------------------------------------------------------
# CLI: build the packaged subset asset (run from ``fourier-prompt/``)
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m fourier_prompt.fonts")
    sub = parser.add_subparsers(dest="command")
    build = sub.add_parser("build", help="build the packaged OFL subset asset")
    build.add_argument("--source", default=r"C:\Windows\Fonts\NotoSansSC-VF.ttf")
    build.add_argument("--out", default=str(DEFAULT_FONT_PATH))
    build.add_argument("--text", default=SAMPLE_TEXT)
    build.add_argument("--weight", type=float, default=400.0)
    build.add_argument("--no-rename", action="store_true")
    build.add_argument("--json", action="store_true")
    info = sub.add_parser("info", help="print font metadata as JSON")
    info.add_argument("--font", default=None)
    args = parser.parse_args(argv)

    if args.command == "build":
        charset = common_charset(args.text)
        meta = subset_font(
            args.source,
            args.out,
            text=charset,
            weight=args.weight,
            rename=not args.no_rename,
        )
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0
    if args.command == "info":
        print(json.dumps(font_info(args.font), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
