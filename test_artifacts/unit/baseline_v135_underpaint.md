# 测试基线结果（v1.35 人类画师式底色）—— fourier-multistroke-training

> 定稿时间：2026-10-24 15:2x（Asia/Shanghai）。
> 被验收代码：`scripts/layered_base.py` SHA256
> `BFFE28D77F09083C64C58584B0D375FBAAE20EFC9340C1DF4862A599481411BA`
> （fixer-core v1.35 冻结版；task-17 已 complete）。
> 写域：`skills/fourier-multistroke-training/tests/` + `work/tests3/`。

## 结论（先看这里）

1. 新增 `tests/test_base_coat.py` **19 用例（~11 s）**；全量 **84 tests / 0 failed /
   3 skipped（仅 RUN_SLOW 慢测试）/ 49–58 s**；`RUN_SLOW=1` **3/3 OK / 9.6–11.3 s**。
2. **默认行为变更已按契约固化**：不传 `--base-color-mode` → `toned`；base 笔
   kind=`base_toned`/layer=0/alpha=1、颜色唯一（std=0）、无 gradient、无颜色抖动；
   单次薄涂栅格实测（12 个稠密合成配置，res 96–256 + 384×288，angle 0/90）：
   **coverage 全部 = 1.0000、overlap_fg 3.19–4.02%（≤5%）、count>2 = 0、max_count = 2**；
   与 report 的 `base_coverage/base_overlap_frac` 一致（`work/tests3/base_coat_compliance.json`）。
3. **none 模式**：pass1 零笔、`base_strokes=0`、`base_color/base_coverage/
   base_overlap_frac/base_overlap_canvas_frac=None`、`base_color_std=0.0`；
   线稿 128px 实测 `none` 正常出正式层（PSNR 有限）。
4. **legacy 逐字节回归（显式传参）**：v1.33 canonical（gate off + quantized）
   **81,815 笔 / 23.1883 / 0.61788**，PNG SHA `30CDCE74…` 与文档基线一致；
   v1.34 推荐命令（source + texture-flat 37000）**99,929 笔 / 24.3628 / 0.62545**，
   PNG SHA `69820EEF…` 与 `work/underpaint/legacy/legacy_compat.json` 完全一致。
5. **100k 硬上限**：纯默认 1536 → **37,843 笔、capped_100k=False**；
   `--pass4` 时 pre **109,548 → 100,000**、`capped_100k=True`、
   `cap_adjustments={"base_pass4":{"removed":9548,"frac":0.1332}}`（与 Lead 数据一致）；
   1536 base 64 笔纯色 [0.81176,0.76863,0.26275]、coverage 1.0、overlap 4.10%、
   canvas overlap 3.19%、std 0.0。小预算 `--max-curves 50` 单测覆盖抽稀+base 不丢。
6. 质量取舍（如实记录，数据来自 fixer `work/underpaint/FIX_REPORT.md`）：
   Sunflowers toned 1536 全幅 PSNR **21.7959**（v1.33 23.0791 / v1.34 24.3322）→
   底色不再携带图像信息后全幅 PSNR 有明显代价；内容区 24.5205（v1.33 24.4302 /
   v1.34 24.8173）、A 区 24.309（v1.33 24.0588 / v1.34 24.7211）。线稿 `none` 明显更好：
   IoU 0.8492 / recall 0.9755 / precision 0.8677 / SSIM 0.776（旧 v1.34 source 底
   0.7369 / 0.9645 / 0.7574 / 0.6378），白区灰草图比例 15.6%→7.2%。

## 命令与耗时

```powershell
Set-Location "skills/fourier-multistroke-training"
python -m unittest discover -s tests -v            # 84 tests, OK (skipped=3), 49–58 s
$env:RUN_SLOW = "1"; python -m unittest tests.test_slow_real_image -v; Remove-Item Env:RUN_SLOW
                                                    # 3 tests, OK, 9.6–11.3 s
```

## 一、新增用例（tests/test_base_coat.py，19）

| 类 | 用例 | 断言 |
|---|---|---|
| `TestBaseCoatInterface`（4） | help/frozen/err-pct/choices | `--base-color-mode` 默认 toned、choices `toned/none/single/quantized/source`；`--base-color auto`、coat angle 0/spacing 0/width-factor 1.04/jitter 0.5；err-pct 60/45/45；`--max-curves 100000` |
| `TestTonedDefault`（6） | 默认=toned、report 字段、coat 几何、纯色无 gradient、栅格覆盖/重叠、base-only 平坦、默认 recipe 仍出画、100k 默认上限 | 见结论 2；`base_only` 内部色 std≤1e-4；默认 `capped_100k=False`、`pre_cap_strokes==strokes_out` |
| `TestBaseColorParsing`（2） | `#3366CC` / `"10,200,30"(0–255)` | report.base_color 与所有 base 笔颜色一致（1e-5） |
| `TestBaseCoatAngle`（1） | `--base-coat-angle 90` | 笔方向垂直度≥0.9 且 coverage≥0.99 |
| `TestNoBase`（1） | `none` | base_strokes=0、无 base 笔、report 相关字段 None、正式层仍有输出 |
| `TestLegacyModes`（2） | quantized / source | legacy 底仍多色（unique>1、color_std>1e-4）、base_color/coverage 等为 None、不崩溃 |
| `TestHardCap`（1） | `--max-curves 50` | 总笔数≤50、`capped_100k=True`、`cap_adjustments` 为 `{layer:{removed,frac}}`、stdout WARNING、base 笔永不丢 |
| `TestLineArtSmoke`（2） | 真实线稿 128px（存在才跑） | `none`：零 base 笔；toned auto 色 ≈ 白纸（每通道≥0.85）且 std≈0 |

> 线稿稀疏场景的 toned 只断言纯色/无 gradient/auto≈白，**不**硬断言 fg overlap
> （fixer 实测稀疏墨线下 overlap fg 口径 8–12%，数学上不可避免；官方推荐 `none`）。

## 二、全量套件 before / after

| 项 | v1.34 基线（`work/tests2/`） | v1.35（本次） |
|---|---|---|
| 全量快测试 | 63 tests / OK (skipped=4) / ~39.6 s | **84 tests / OK (skipped=3) / 49–58 s** |
| `test_contracts.py` | 18 | **20**（+v1.35 底色接口/文档门禁） |
| `test_base_coat.py` | — | **19**（新） |
| 慢测试 `RUN_SLOW=1` | 3/3 OK / 9.1 s | **3/3 OK / 9.6–11.3 s** |
| v1.33/v1.34 旧语义断言 | 全绿 | 仍全绿（仅 FROZEN 默认值与 `base_toned` kind 映射按新契约更新，语义断言未放宽） |

v1.35 默认改动连带更新的既有测试（详见 `work/tests3/PATCHES_existing_tests.md`）：
`test_contracts.py`（默认 toned/err-pct/max-curves/新增 FROZEN_BASE_COAT_DEFAULTS/
choices）、`test_texture_flat.py`（choices + 几何舍入容差 1e-6→1e-3）、
`test_layered_base.py`（默认 CLI smoke 增加 `base_toned` kind/layer 与 v1.35 report 断言）、
`test_slow_real_image.py`（几何容差）、`tests/README.md`（预算/矩阵/v1.35 契约表）。

## 三、底座合规证据（稠密画面，真实栅格）

`work/tests3/base_coat_compliance.json`（12 个配置：scene 192×144 res 96/128/160/192/256 ×
angle 0/90 + scene 384×288 res 128/192）：

| 指标 | 实测范围 |
|---|---|
| fg coverage | **1.0000（全部 12 配置）** |
| overlap fg | 0.0319–0.0402（max 4.02% ≤5%） |
| count>2 像素 | 0（全部） |
| max count | 2 |
| report overlap_canvas | ≤0.0348 |
| base 颜色 std | 0.0；unique colors = 1；gradient strokes = 0 |

1536 Sunflowers 默认 toned：base **64 笔**、颜色 [0.81176, 0.76863, 0.26275]、
coverage **1.0**、overlap_fg **0.04102**、overlap_canvas 0.031937、std **0.0**
（`work/tests3/default_1536/`）。

## 四、legacy 逐字节回归（显式旧参数）

| 场景 | 笔数 | PSNR / SSIM | PNG SHA256 | 期望（legacy_compat.json） | 一致 |
|---|---|---|---|---|---|
| v1.33 canonical（gate off, quantized） | 81,815 | 23.1883 / 0.61788 | `30CDCE74F11A8E2C0CFBA7A0D760E49AC773DECB902180E2584EC17D6DEB9A01` | 同 | ✅ |
| v1.34 推荐（source + texture-flat 37,000） | 99,929（texture 37,000） | 24.3628 / 0.62545 | `69820EEF69386B8DC0561AF70DCA1363991D1A81D9C8CA8F52984E1CC4C265C3` | 同 | ✅ |

`strokes JSON` SHA：v1.33 = `110c9635…`，v1.34 = `3e7cb032…`。
证据：`work/tests3/regression_evidence_v135.json`、`work/tests3/legacy_v133/`、`legacy_v134/`。
说明：v1.33/v1.34 命令均**显式**传 `--base-color-mode quantized/source` 与旧 err-pct，
不受 v1.35 默认值变化影响。

## 五、100k 硬上限（`--max-curves`）

| 场景（Sunflowers_square, 1536） | pre_cap_strokes | strokes_out | capped_100k | cap_adjustments |
|---|---|---|---|---|
| 纯默认（input/outdir/work-res only） | 37,843 | **37,843** | False | None |
| 默认 + `--pass4` | **109,548** | **100,000** | True | `{"base_pass4":{"removed":9548,"frac":0.1332}}`（pass2/pass3/base/detail 全保留） |
| 合成小图 + `--max-curves 50`（单测） | >50 | ≤50 | True | 非空 `{layer:{removed,frac}}`，base 笔保留 |

report 新字段：`max_curves / capped_100k / cap_adjustments / pre_cap_strokes`
（`test_base_coat` 默认与 cap 两个路径均断言）。
证据：`work/tests3/cap_default_pass4/`、`work/tests3/default_1536/`、
`test_base_coat.py::TestHardCap`。

## 六、接口冻结同步位置

- `tests/_support.py`：`TEXTURE_FLAT_KIND`、`MAX_TOTAL_STROKES`、
  `stroke_footprints()/coverage_metrics()/render_strokes()/foreground_color_std()/
  stroke_color_stats()/parse_base_color()/lineart_image()`（v1.35 新增）。
- `tests/test_contracts.py`：`FROZEN_CORE_DEFAULTS`（v1.35 默认 toned/err-pct 60/45/45/
  max-curves 100000）、`FROZEN_BASE_COAT_DEFAULTS`、`FROZEN_BASE_MODE_CHOICES`、
  `test_base_coat_interface_frozen`、`test_base_coat_options_documented`。
- `tests/test_base_coat.py`：同类 `FROZEN_BASE_DEFAULTS / FROZEN_ERR_PCT_DEFAULTS` 自检。
- `tests/README.md`：预算表、覆盖矩阵、v1.35 契约表。

## 七、已知限制

1. **线稿/稀疏主体 toned 的 fg overlap**：8–12%（分母是少量墨线像素）——按 Lead 决定
   不做硬门禁，官方推荐 `--base-color-mode none`；测试只在线稿上断言纯色/auto≈白/none 零底。
2. **质量代价**：toned 不携带图像信息，Sunflowers 全幅 PSNR 21.7959 vs v1.33 23.0791 /
   v1.34 24.3322；默认 recipe（无 pass4/texture、无 detail）1536 PSNR 20.6959。
   属设计取舍（人类画法），需在文档中如实写明，不能宣称“质量无损”。
3. 本报告未独立复跑线稿 1200/2400 大图与 Sunflowers toned 推荐 recipe 的完整视觉流程
   （由 fixer/visual-analyst 产出，见 `work/underpaint/FIX_REPORT.md`）；测试侧覆盖了
   默认/legacy/硬上限/底座栅格等可自动化门禁。
