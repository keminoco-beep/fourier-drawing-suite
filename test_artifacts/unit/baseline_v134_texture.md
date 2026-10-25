# 测试基线结果（v1.34 texture-flat）—— fourier-multistroke-training

> 定稿时间：2026-10-24 11:58（Asia/Shanghai）。
> 被验收代码：`scripts/layered_base.py` SHA256
> `00BC219F1E7C78EDFC350592211E6D9BCF566251FC86BC548B68E3C3034D45C8`（fixer-core v1.34 冻结版）。
> 写域：`skills/fourier-multistroke-training/tests/` + `work/tests2/`。

## 结论（先看这里）

1. 新增 `tests/test_texture_flat.py` **17 用例（~8 s）**；全量 **63 tests / 0 failed / 4 skipped / 39.6 s**
   （4 skip = 3 慢测试默认 skip + 1 个 v1.34 文档等待项，fixer-docs v1.34 完成后自动执行）。
   `RUN_SLOW=1`：**3/3 OK / 9.1 s**（v1.33 渐变门两方向 + v1.34 texture-flat 真实图）。
2. **默认行为逐字节不变**（v1.34 回归硬证据）：v1.34 + canonical 默认参数 与 v1.33 产物
   **strokes JSON / PNG SHA256 完全相同**：
   gate off `110c9635…` / `30cdce74…`（81,815 笔、23.1883、0.61788）；
   gate on `7f98ecb4…` / `62b3a488…`（81,010 笔、23.1059、0.61583）。
   证据：`canonical_regression_v134.json`。
3. **texture-flat opt-in 生效**（合成小图 A/B）：默认 off 230 笔/PSNR 19.8689/SSIM 0.53887 →
   `--texture-flat --texture-flat-count 300` 530 笔（+300 texture）/19.9843/0.55374；
   推荐 recipe 小跑 705 笔/400 texture、总笔数 ≤100k。
4. **100k 预算门**：count 超大（200000）时告警并截断，`texture_flat_strokes ≤ budget`、
   总笔数 ≤100,000 不崩溃（单元 hard_cap=37/0 亦覆盖）。
5. 1536 端到端指标以 fixer-core `work/fix2/FIX_REPORT.md` 为准（测试侧只复核默认不变 +
   opt-in 开关/字段/几何/预算）：A 区 PSNR 24.059→24.721、blocks 103→74、flat_frac
   0.491→0.437；全幅 23.079→24.332、内容 24.430→24.817、B 26.925→28.094；
   99,929 笔 ≤100k。残余差距（flat_frac 0.437 未到 ≤0.35 等）见该报告“剩余差距”。

## 新增用例（tests/test_texture_flat.py，17）

| 类 | 用例 | 断言 |
|---|---|---|
| `TestTextureFlatInterface`（3） | help/defaults/base | 11 个 `--texture-flat*` 均在 `--help`；默认值 frozen（count 8000 / spacing 2.5 / len 5-9 / width 2.5-3.5 / lstd 0.01 / ref-std 0.0 / layer 19 / jitter 0.01 / flag off）；v1.34 pass1 新增参数与 `--base-color-mode` choices（single/quantized/source） |
| `TestTextureFlatUnit`（9，直调 `texture_flat_pass`） | 输出笔数/info、笔字段、几何缩放、平坦区判定、ref-std 门、hard_cap、零预算、auto count、确定性 | kind=`oil_flat_texture`、layer=19、alpha=1、points∈[0,1]；按 `rs=max(H,W)/1280` 缩放的 len/width；**controlled render 左平坦/右噪 → 100% 落平坦侧（≥70%）**；ref-std 门 → 100% 落参考纹理侧；hard_cap=37 → 37 笔、cap=0 → 0 笔；count=0 → planned=round(6000·H·W/1536²)；同 seed 逐字段一致 |
| `TestTextureFlatCLIDefaultOff`（1） | 默认 off | `texture_flat_strokes==0`、`params/candidates is None`、strokes JSON 无 texture 笔、pass 计数守恒、PSNR 有限 |
| `TestTextureFlatCLIOn`（2） | 传参 on | report 字段/参数回显、`budget=100000−base笔数`、total≤100k、`texture_flat_strokes≤count`；全部 texture 笔 layer/alpha/points/len/width 合规 |
| `TestTextureFlatCLIRecipe`（1） | 推荐 recipe smoke | `--base-color-mode source --base-blur 4 --coarse 12 --texture-flat --texture-flat-count 400 --texture-flat-ref-std 0.015`、无 `--pass4`：base 字段回显、texture>0、pass4_local=0、total≤100k |
| `TestTextureFlatCLIBudget`（1） | 100k 上限 | `--texture-flat-count 200000` → 退出码 0、stdout 有 `WARNING texture-flat count`、`capped_100k=true`、`texture_flat_strokes≤budget`、total≤100,000 |

## 命令与耗时

```powershell
# 全量（快）
Set-Location "skills/fourier-multistroke-training"
python -m unittest discover -s tests -v
# → Ran 63 tests ... OK (skipped=4) in 39.6 s（本机 16 核）

# 慢测试（真实图 256px ×3）
$env:RUN_SLOW = "1"; python -m unittest tests.test_slow_real_image -v; Remove-Item Env:RUN_SLOW
# → Ran 3 tests ... OK in 9.1 s

# 新模块单跑
python -m unittest tests.test_texture_flat -v   # → Ran 17 tests ... OK in 8.1 s
```

## 一、测试套件 before / after

| 项 | v1.33 基线（`work/tests/logs/`） | v1.34（本次） |
|---|---|---|
| 旧快照（`skill_before_v1.33`）跑当时套件 | 43 tests / 4 failed（2 渐变缺陷 + 2 `--detail` 过滤）/ 29.0 s | —— |
| v1.33 修复后套件 | 43 tests / OK (skipped=4) / 31.0 s | —— |
| v1.34 全量（含新 17 用例） | —— | **63 tests / OK (skipped=4) / 39.6 s** |
| v1.34 慢测试 `RUN_SLOW=1` | v1.33：2/2 OK / 6.7 s | **3/3 OK / 9.1 s**（+texture-flat 真实图） |

> v1.33 的 43 个用例在 v1.34 代码上全部保持通过（同一份全量日志），未被新参数改变。
> 4 个 skip：`test_gate_off_real_image_has_unsafe_gradients` /
> `test_gate_on_real_image` / `test_texture_flat_real_image`（需 `RUN_SLOW=1`）+
> `test_texture_flat_options_documented`（fixer-docs v1.34 未完成）。

## 二、默认行为逐字节回归（canonical 1536，v1.34 vs v1.33）

同命令（Lead/v1.33 canonical，含 detail `training/sunflowers/clean_r1/cleaned_strokes.json`）：
v1.34 代码默认参数（不传任何 texture/base 新参数）重跑 → 与 v1.33 产物比较：

| canonical 场景 | v1.33 产物 | v1.34 默认 | strokes SHA 一致 | PNG SHA 一致 |
|---|---|---|---|---|
| gate off（旧行为） | 81,815 笔 / 23.1883 / 0.61788；strokes `110c9635…`；png `30cdce74…` | 相同 | ✅ | ✅ |
| gate on（v1.33 默认） | 81,010 笔 / 23.1059 / 0.61583；strokes `7f98ecb4…`；png `62b3a488…` | 相同 | ✅ | ✅ |

`texture_flat_strokes=0`、`base_color_mode=quantized`（默认不变）、`base_blur=4.0`（报告字段新增但
默认路径不参与 pass1 颜色）。证据：`work/tests2/canonical_regression_v134.json`，
原始输出 `work/tests2/canonical_v134_gateoff/`、`canonical_v134_default/`。

## 三、texture-flat opt-in A/B（合成 192×144 左平坦/右噪，work-res 128）

证据：`work/tests2/texture_ab_small.json`

| 配置 | 总笔数 | texture 笔 | PSNR | SSIM | 备注 |
|---|---|---|---|---|---|
| v1.34 默认 off | 230 | 0（params=null） | 19.8689 | 0.53887 | 旧行为 |
| `--texture-flat --texture-flat-count 300` | 530 | 300（candidates 10,625） | **19.9843** | **0.55374** | opt-in 生效 |
| 推荐 recipe 小跑（source/blur4/coarse12/ref-std 0.015，count 400，无 pass4） | 705 | 400（candidates 4,902） | 19.6283 | 0.54476 | 与上一行不同 base，仅 smoke |

- 默认 off 的 strokes JSON **无** `oil_flat_texture` 笔，report `texture_flat_params/candidates=null`。
- 100k 上限：`--texture-flat-count 200000` → 输出 10,855 笔（base+texture），stdout 告警
  `WARNING texture-flat count ...`，`capped_100k=true`、`texture_flat_strokes(10,625) ≤ budget(99,770)`。

### 平坦区判定口径说明

单元级用 controlled render（左半恒定色=平坦、右半噪声=不平坦）断言 **100%（≥70%）** texture 笔落在
平坦侧；另用 ref-std 门断言 100% 落在“参考图有纹理”的一侧。CLI 合成图上渲染两侧都较平坦
（`flat_frac_fg≈0.9`），候选按参考纹理能量排序会把预算送到参考纹理更强的一侧——这是 v1.34 的
预期行为（A 区分层处正是“渲染平但参考有纹理”），故 CLI 层不断言“左侧≥70%”，只断言
report/几何/预算；该口径已写入 `tests/test_texture_flat.py` docstring 与 `tests/README.md`。

## 四、接口冻结同步位置

- `tests/_support.py`：`TEXTURE_FLAT_KIND="oil_flat_texture"`、`MAX_TOTAL_STROKES=100000`、
  `TEXTURE_FLAT_REF_RES=1280`、`scene_flat_noise()/flat_render()`、
  `expected_texture_{width,length}_px()`。
- `tests/test_contracts.py`：`FROZEN_TEXTURE_FLAT_DEFAULTS`（11 项）+ v1.34 pass1 默认值
  （`--base-blur 4.0`、`--base-gradient off`、`--base-width-factor 1.5`、
  `--base-length-factor 2.0`）+ `--base-color-mode` choices 增加 `source` +
  `test_texture_flat_interface_frozen` / `test_texture_flat_options_documented`（文档待 fixer-docs v1.34）。
- `tests/test_texture_flat.py`：同类 `FROZEN_TEXTURE_DEFAULTS` / `FROZEN_BASE_V134_DEFAULTS` 自检。
- `tests/README.md`：预算表、覆盖矩阵、v1.34 契约表（kind/layer/默认值/report 字段/100k 上限）。

## 五、证据文件

| 文件 | 内容 |
|---|---|
| `work/tests2/logs/after_full_v134.txt` | 全量 63 tests OK，39.6 s |
| `work/tests2/logs/slow_run_v134.txt` | `RUN_SLOW=1` 3/3 OK，9.1 s |
| `work/tests2/canonical_regression_v134.json` | v1.34 默认 vs v1.33 逐字节（SHA）回归 |
| `work/tests2/texture_ab_small.json` | 合成图 default-off / texture-on / recipe 小跑 A/B |
| `work/tests2/canonical_v134_gateoff/`、`canonical_v134_default/` | v1.34 默认 canonical 1536 产物 |
| `work/tests2/_*.py` | 标定/复跑脚本（probe/AB/canonical regression） |
| `work/fix2/FIX_REPORT.md` | 1536 端到端指标（A 区 banding/PSNR、99,929 笔 ≤100k） |

## 六、已知限制 / 遗留

1. fixer-docs v1.34 未完成：`test_texture_flat_options_documented` 目前 skip；文档更新后自动执行，
   若文档把 kind 写成 `texture_flat`（应为 `oil_flat_texture`）或默认值不一致，该门禁会红。
2. 平坦区“≥70% 落平坦侧”在 CLI 合成图上不可直接断言（渲染两侧都平 + 参考能量排序），
   已在单元级用 controlled render 覆盖；真实 A 区效果以 `work/fix2/FIX_REPORT.md` 的
   banding 指标与 `crops_A_final.png` 视觉验收为准。
3. 残余 banding 差距（flat_frac 0.437 vs 目标 ≤0.35、grad_x/uniq16/run_y 未完全达标）属
   fixer 已记录的预算限制（100k 下 A 区仅 ~2.3k 纹理笔），不是测试回归。
