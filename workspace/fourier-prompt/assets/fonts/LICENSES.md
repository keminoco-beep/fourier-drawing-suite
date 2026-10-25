# Font assets — licensing and build record (WP-K)

This directory ships the outline-mode text font and its license. The renderer
never loads these files: `compile_plan` bakes glyph outlines into plain fill
polygons, so a `.fbs` produced from text has **no font dependency**.

## Files

| File | Bytes | SHA-256 |
|---|---|---|
| `NotoSansSC-VF.ttf` (packaged subset) | 1,120,384 | `ce2c3985a41fa7164cca59d8578208140767bfa8788766af6d90b43da6a9a268` |
| `OFL.txt` (SIL OFL 1.1 full text) | 4,463 | `fcac737e761ec63dbfbdce11030a1780161920d80315edba9c8beff1c2bac5a2` |

## Upstream font and license

- **Family**: Noto Sans SC / Source Han Sans SC (same OFL-1.1 design; downstream
  Noto build).
- **Installed source used for the subset**: `<path>`
  (17,773,244 bytes, SHA-256
  `763146584cf0710223441356b4395e279021b0806c196614377a7a0174ae074a`,
  version `2.04;241114210130;non-release`, variable axis `wght` 100–900,
  `unitsPerEm` 1000).
- **License**: SIL Open Font License, Version 1.1 (OFL-1.1), SPDX `OFL-1.1`.
  The font's name table states: *"This Font Software is licensed under the SIL
  Open Font License, Version 1.1 …"* (name ID 13) and
  `http://scripts.sil.org/OFL` (name ID 14). The full text is in `OFL.txt`.
- **Upstream**: <https://github.com/notofonts/noto-cjk>,
  <https://github.com/adobe-fonts/source-han-sans>.
- **License text source**: one-time download (2026-10-20) of
  <https://raw.githubusercontent.com/adobe-fonts/source-han-sans/release/LICENSE.txt>
  (the Source Han Sans upstream license file; `OFL.txt` is the same text with
  line endings normalized to LF and the BOM removed, 4,463 bytes). This was the
  only network access used to build this asset.

## Subset / modification record

The packaged file is a **modified** (subset + static-instance + renamed) copy:

- Charset: 3,903 characters = printable ASCII (`U+0020..U+007E`) + GB2312
  level-1 hanzi (3,755 common simplified characters) + common CJK punctuation
  + the test/sample string `你好，Fourier! 2026` + `…`. Charset SHA-256:
  `0e5e129979a8d488b3d9c8e7d729bbe08f0a863ccadb55e5a2c4c5ffcb6b791c`.
- Variable font pinned to a static `wght=400` instance so outline extraction
  cannot depend on the default (Thin) instance; `gvar`/`HVAR`/`fvar`/`avar`
  removed, hinting dropped, layout features removed (no kerning).
- Glyphs: 3,904. Result: 1,120,384 bytes.
- **Renamed for OFL RFN compliance** (the upstream declares Reserved Font Name
  `'Source'`): family `FdrawSubset NotoSansSC 3acf1b15`, PostScript name
  `FdrawSubsetNotoSansSC-3acf1b15` (name IDs 1/4/6/16/17). This rename is
  mandatory for any modified copy; do not restore the upstream name.
- No glyph outlines were altered beyond instancing/subsetting; the font is not
  sold separately.

### Reproduce

From `fourier-prompt/` with `fontTools==4.67.0` (build/compile time only):

```powershell
$PY = "<python>"
& $PY -m fourier_prompt.fonts build `
    --source "<path>" `
    --out "assets\fonts\NotoSansSC-VF.ttf" `
    --weight 400
```

`fonts.subset_font` performs, in order: `fontTools.varLib.instancer` pin
`wght=400` → `fontTools.subset.Subsetter` (`layout-features=''`,
`--no-hinting`, `--desubroutinize`, `--notdef-outline`,
`--recommended-glyphs`, `--name-IDs='*'`, `--name-legacy`,
`--name-languages='*'`, drop `DSIG`, **no timestamp recalculation**) →
RFN rename → cmap coverage hard gate. Equivalent `pyftsubset` command:

```text
pyftsubset <static-instance.ttf> --text-file=charset.txt `
  --output-file=NotoSansSC-VF.ttf --layout-features='' --no-hinting `
  --desubroutinize --notdef-outline --recommended-glyphs `
  --name-IDs='*' --name-legacy --name-languages='*' `
  --drop-tables+=DSIG --no-recalc-timestamp
```

Determinism: the output name/tag is derived from
`sha256(source) + sha256(charset)`, and the head timestamp is preserved, so two
runs on the same fontTools version produce byte-identical assets
(`tests/text/test_fonts.py::SubsetReproducibilityCase`).

## Obligations checklist (OFL-1.1)

- [x] Full license text shipped next to the font (`OFL.txt`).
- [x] Modified copy renamed (RFN `'Source'`).
- [x] Copyright notice preserved in the font name table and `OFL.txt`.
- [x] Font not sold separately (bundled with the software/document only).
- [x] No system-font fallback: missing glyphs/fonts fail loud
      (`FP_E_TEXT_GLYPH` / `FP_E_TEXT_FONT`).
