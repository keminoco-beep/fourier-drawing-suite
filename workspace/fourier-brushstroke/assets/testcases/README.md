# 合成测试用例（T1–T13）

> 由 `tests/gen_fixtures.py` 确定性生成（seed=20261018），不要手改图片。
> 每张图附 `<case>_spec.json` 真值（已知宽度曲线 / 颜色 stops / 预期错误码），
> 供 `scripts/run_acceptance.py` 做反演精度验收。`manifest.json` 记录 sha256。

| 用例 | 文件 | 尺寸 | 预期类别 | 用途 |
|---|---|---|---|---|
| T10a_中文名_大图 | T10a_中文名_大图.png | 512×512 | lineart | T10/A5 |
| T10b_损坏文件 | T10b_损坏文件.png | — | corrupt | T10/A5 |
| T10c_太小_8x8 | T10c_太小_8x8.png | 8×8 | too_small | A5 |
| T10d_不是图片 | T10d_不是图片.txt | — | invalid_format | A5 |
| T11_taper_three_expressions | T11_taper_three_expressions.png | 384×384 | lineart | A14 |
| T12_color_gradient | T12_color_gradient.png | 384×384 | lineart | T12 |
| T13_step_intervals | T13_step_intervals.png | 384×384 | lineart | A15 |
| T1_signature | T1_signature.png | 384×384 | lineart | A1/A2 |
| T2_icon | T2_icon.png | 384×384 | flat_art | T2 |
| T3_flat_illustration | T3_flat_illustration.png | 384×384 | illustration | A9/A11/A10 |
| T4_taper_thick_to_thin | T4_taper_thick_to_thin.png | 384×384 | lineart | A7/A13（宽度反演精度）、A3（N–R 契约）、A10（分支数） |
| T5_sharp_corners | T5_sharp_corners.png | 384×384 | flat_art | A8 |
| T5b_sharp_fold_stroke | T5b_sharp_fold_stroke.png | 384×384 | lineart | A8（修正判据）载体 |
| T6_gradient | T6_gradient.png | 256×256 | gradient | T6 |
| T7_photo_like | T7_photo_like.png | 384×384 | photo | A5/D5 |
| T8a_solid_black | T8a_solid_black.png | 256×256 | solid | T8/A5 |
| T8b_solid_white | T8b_solid_white.png | 256×256 | solid | T8/A5 |
| T8c_fully_transparent | T8c_fully_transparent.png | 256×256 | empty | T8/A5 |
| T9a_16bit | T9a_16bit.png | 256×256 | any | T9/A5 |
| T9b_cmyk | T9b_cmyk.jpg | 256×256 | any | T9/A5 |
| T9c_alpha | T9c_alpha.png | 256×256 | any | T9/A5 |
| T9d_palette | T9d_palette.png | 256×256 | any | T9/A5 |
