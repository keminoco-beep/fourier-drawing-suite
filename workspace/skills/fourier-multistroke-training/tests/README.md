# tests/ — fourier-multistroke-training 自动化测试

纯 Python 标准库 `unittest`（**不依赖 pytest**）。所有依赖 numpy / Pillow / scipy 都是本
skill 的运行依赖；子进程一律用 `sys.executable`，路径全部从 `__file__` 推导，不写死机器
绝对路径，合成图用固定 seed，因此结果确定、可在任意机器复跑。

## 怎么跑

从 **skill 根目录**运行（Windows PowerShell 示例；`$PY` 换成任意已装依赖的 Python ≥3.10）：

```powershell
$PY = "<python.exe>"    # 换成你的 Python 解释器路径（需已装 numpy/Pillow/scipy ≥3.10）
Set-Location "<workspace>/skills/fourier-multistroke-training"
& $PY -m unittest discover -s tests -v
```

常用变体：

```powershell
# 只跑某一个模块
& $PY -m unittest tests.test_gradient_safety -v

# 只跑渐变安全门（约 5 s，最重要的回归门）
& $PY -m unittest tests.test_gradient_safety -v

# 连慢测试一起跑（真实图 256px，两个用例，约 30–90 s）
$env:RUN_SLOW = "1"; & $PY -m unittest discover -s tests -v; Remove-Item Env:RUN_SLOW
```

```powershell
# GPU 子集（需要 ComfyUI python：PyOpenGL + glfw + 桌面 GL；默认套件里 GPU 用例 skip）
$env:RUN_GPU = "1"; & "<ComfyUI-python.exe>" -m unittest tests.test_gpu_render -v; Remove-Item Env:RUN_GPU
# v1.38.1：23 tests / 0 failed / 1 skipped（含 PYOPENGL_PLATFORM=egl 回归；默认 146 tests / 16 skip）
# 无 PyOpenGL 的 python 跑整套：GPU 用例自动 skip，其余（含 --device cpu）照常
```

当前预算（16 核 Windows / 本仓库实测）：

| 模块 | 用例数 | 耗时 | 运行内容 |
|---|---|---|---|
| `test_layered_base.py` | 18 | ~6 s | 单元不变量 + 渐变门单元（含平滑 ramp 严格契约）+ 默认参数 CLI 冒烟（128px 合成图，v1.36 默认 wash 底/flow/region） |
| `test_contracts.py` | 22 | ~30 s | 31 脚本 `--help`/编译/文档/编码/接口契约（v1.33 渐变门 / v1.34 texture-flat / v1.35 底色 / v1.36 人类画法）`--detail` 契约 |
| `test_gradient_safety.py` | 7 | ~9 s | 渐变安全门缺陷复现与 A/B（128px 高对比边界合成图；v1.33 非回归断言在 legacy 几何下测，v1.36 默认几何数字另打印） |
| `test_texture_flat.py` | 17 | ~8 s | v1.34 texture-flat 细纹理层：默认零笔/传参/report/几何/平坦区判定/100k 上限/推荐 recipe smoke |
| `test_base_coat.py` | 19 | ~13 s | v1.35/v1.36 底色：wash 默认栅格门禁（覆盖≥99%/overlap≤15%）/ rows 旧行为≤5% / none / 颜色解析 / angle / legacy / 100k 硬上限 / 线稿 smoke |
| `test_human_paint.py` | 18 | ~18 s | **v1.36 人类画法**：wash 非行扫 + 单色无 gradient + 覆盖/重叠；flow on/off 轴对齐对比 + 真实图 axis<0.5；region vs scan 顺序指标（y-Spearman/相邻距离/region run）；legacy 自动回退；默认预算 + 100k 硬上限 |
| `test_silhouette.py` | 15 | ~20 s | **v1.37 剪影感知笔触**：on 时逐笔栅格足迹不出 sil（越界像素≈0、overshoot_after≤1px）；off 时 >2px 越界计数>0（证明有区分力）；auto 语义（wash+region→on、quantized+legacy→off、非白底→off）；sil 面积 fallback；细长条不崩；336 质量/预算；report 字段齐全 | v1.37 轮廓溢出回归、生成期剪影契约 |
| `test_gpu_render.py` | 23 | ~3 s（默认；13 条 GPU 用例 skip）/ RUN_GPU 子集 ~30 s | **v1.38/v1.38.1 GPU 渲染（含 PYOPENGL_PLATFORM=egl 回归）**：合成 strokes GPU vs CPU 等价（PSNR/SSIM ≥32/0.98；实测 62.3/1.000）；`--device cpu`、`--supersample 1/2/3`、`--resolution`、`--strokes` 输入；无 GL **exit 3**；`alpha<1` 文档 **exit 5**（CPU 可渲染）；计时 JSON 字段；节点 PACKAGE_VERSION 3.0.1 / RenderFdraw widget=1 / workflow 不变；GPU 环境 node GPU→CPU fallback；验收门：GPU/CPU 等价、GL 降级与节点契约回归 |
| `test_slow_real_image.py` | 3 | 0 s（默认 skip） | `RUN_SLOW=1` 才跑：真实图 256px 渐变门两方向 + texture-flat |
| **合计** | **146** | **~83–103 s** | 默认 16 skip（3 慢 + 13 GPU；ComfyUI python 17）；GPU 环境 `RUN_GPU=1` 子集另 ~30 s（23 tests / 1 skip） |

## 测试文件与覆盖矩阵

| 文件 | 覆盖点 | 对应质量门 |
|---|---|---|
| `test_layered_base.py` | `save_strokes`/`load_strokes` 字段与精度（points 6 位 / widths 7 位 / color 5 位）、roundtrip、默认加载丢弃 `base_pass*`/`fix_*`/`soft_*`；`palette_map`、`local_palette_map` 形状/范围；`tile_pass` 笔数 ≤ 网格数、points∈[0,1]、widths>0、color∈[0,1]、alpha=1、layer/kind、mask/fg 外不生成；渐变门单元（gate on 只接受安全 Δ、gate off 恢复旧行为、edge_map/oob 拒绝、gate on/off 前两 pass 几何完全一致）；`_stray_color_metrics` 计数 | 数据结构契约、算法不变量 |
| `test_contracts.py` | 31 脚本 `--help` 退出码 0（timeout 60s）、全部 `py_compile`；SKILL.md 关键小节、目录树声明的 scripts/references/assets 与实际文件双向一致；严格 UTF-8；脚本内无盘符绝对路径（`<drive>:/...` 形式，由正则检出）；layered_base 冻结默认值与文档数字（64/32/16px、K=6/8/8）一致性；v1.33 渐变门接口（参数名/默认值/choices）；`--detail` 正确丢弃 `base_pass*`/`fix_*`/`soft_*` | CLI 契约、文档漂移、可移植性、缺陷回归 |
| `test_gradient_safety.py` | 合成 192×144 红蓝硬边界 + 黄/黑/绿色块，`--upper-gradient --upper-color-mode source --pass4`：**断言 A** gate on 时不存在 `Δ = max_c|src(start)-src(end)| > --gradient-max-delta + 1e-4` 的渐变笔（**严格契约，无平滑 ramp 例外**）；**断言 C** report 6 个缺陷指标齐全且自洽（candidates=accepted+rejected_*、by_pass 求和、`gradient_max_delta`/`gradient_accepted_mean_delta` 与实际笔一致、stray 与 PNG 重算一致）；**断言 B** gate off/on 的渲染级虚假色差 A/B 数字打印 + 非回归；gate off 时存在 Δ>阈值 的笔（证明测试有区分力）；自定义 `--gradient-max-delta`；重复运行逐字节一致（确定性）；v1.37 裁剪产生的 2 点渐变笔以存盘 gradient 字段为准 | 本轮核心缺陷（跨边界渐变糊斑）回归 |
| `test_slow_real_image.py` | `RUN_SLOW=1`：真实图（`training/input/Lvpi5.jpg` 等）256px 跑 gate on（无 unsafe 笔 + report 指标）与 gate off（存在 unsafe 笔），以及 v1.34 texture-flat 推荐 recipe 小跑（`--base-color-mode source --base-blur 4 --coarse 12 --texture-flat --texture-flat-ref-std 0.015`）；找不到真实图则 skip | 真实内容集成 |
| `test_texture_flat.py` | **默认 off**：`texture_flat_strokes==0`、`texture_flat_params/candidates is None`、strokes JSON 无 `oil_flat_texture` 笔、v1.33 pass 计数不变；**传参 on**：report 字段/参数回显（count/spacing/lstd/ref_std/len_px/width_px/layer 19/jitter）、kind=`oil_flat_texture`、layer 19、alpha=1、points∈[0,1]、按 rs=max(H,W)/1280 缩放的 len/width 范围、`budget=100000-base 笔数`、`texture_flat_strokes≤count`、总笔数 ≤100000；**单元**（直调 `texture_flat_pass`）：controlled render「左平坦/右噪」→ ≥70% 落平坦侧、`--texture-flat-ref-std` 门 → ≥70% 落参考纹理侧、hard_cap=37/0 截断、count=0 自动量=6000·面积/1536²、同 seed 确定性；**CLI**：推荐 recipe（source/blur 4/coarse 12/无 pass4/ref-std 0.015）smoke、超大 count=200000 触发 100k 截断 warning 且总笔数 ≤100000 | v1.34 颜色分层修复回归、opt-in 默认不变、100k 预算门 |
| `test_base_coat.py` | **默认=toned + `--base-coat-pattern wash`**：report `base_color_mode/base_color/base_coat_*/base_strokes/base_coverage/base_overlap_frac/base_color_std/base_coat_pattern` 齐全、颜色唯一 std≤1e-6、无 gradient、kind=`base_toned`/layer0/alpha1；**wash 栅格门禁**（自算逐笔足迹）：覆盖 ≥0.99、overlap_fg ≤0.15、max_count≤3；显式 `--base-coat-pattern rows` 子断言保留旧行扫契约（axis_aligned≥0.99、overlap≤0.05、max_count≤2）；base-only 8bit 单色渲染；`--base-color #RRGGBB` / `"r,g,b"`(0–255) 解析；`--base-coat-angle 90`（显式 rows）；`none` 零 base 笔且 report None；legacy quantized/source 多色底保持；**100k 硬上限**（`--max-curves 120` → 抽稀 + `capped_100k/cap_adjustments{layer:{removed,frac}}/pre_cap_strokes`、base 不被抽稀）；线稿小分辨率 `none` smoke + toned auto≈白 | v1.35/v1.36 底色原则回归、硬预算门；v1.37 起默认 silhouette auto：pass 计数 − dropped == strokes_out（测试已按公式断言） |
| `test_human_paint.py` | **新增 v1.36**：默认 `base_coat_pattern=="wash"`、`flow_strokes==True`、`paint_order=="region"`、`region_count_used==12`；wash 方向 axis_aligned<0.5 且方向熵>0（v1.35 行扫=1.0/0）；wash 单色/无 gradient/覆盖≥0.99/overlap≤0.15；`rows` 显式=旧行扫（axis≥0.99）；flow on vs off 在 split 合成图：on.axis_aligned ≤ off−0.05、`flow_orientation_frac>0.5`（真实 Sunflowers 小图 axis<0.5）；paint_order region vs scan：region y-Spearman<0.5 且 <scan−0.2、相邻笔距中位数更小、region_run 更高；legacy 顺序/自动回退（quantized 无新参数 → flow off/order legacy/pattern rows）；report 新字段齐全（含 `base_metrics_mode/base_metrics_scale/clip_to_fg/direction_strokes/thick_dab_strokes`）；默认预算 ≤100k/fill=0；100k cap 只抽正式层 | v1.36 人类画法（非扫描）回归 |
| `test_silhouette.py` | **新增 v1.37**：合成白底圆/三角/细长条（128–256px）；`--silhouette on`：自算逐笔 magic 足迹 vs `sil=dist_edt(fg)>shrink`，越界像素≈0（容差 1px）、overshoot_after≤1.0px、report `silhouette_clipped_strokes/points_trimmed/dropped`与 before/after stats；`--silhouette off`：>2px 越界笔数>0、栅格越界像素>100（证明修复有效）；auto：wash+region+clip 白底→on、quantized legacy→off、非白底→off，且 auto 关闭时与显式 off 渲染 PNG SHA 相同；sil 面积<0.2*fg → fallback 不崩、report `silhouette_fallback`；细长条不崩；256px 大主体质量不降（on PSNR ≥ off−0.5dB）、≤100k、fill=0 | | 轮廓溢出回归、生成期剪影契约 |
| `test_gpu_render.py` | **新增 v1.38**：合成 strokes 文档（5 笔：flat / gradient / 细笔 / round cap，alpha=1）GPU `--device gl` vs CPU `--device cpu`：尺寸/白底一致、自算 **PSNR≥32 / SSIM≥0.98**（实测 62.3/1.000）；`--supersample 1/2/3` 与 `--resolution` 出图尺寸正确；`--strokes` 输入路径；计时 JSON `load_s/sample_s/geometry_s/gl_init_s/gpu_s/readback_s/total_s` 齐全；无 GL（workbuddy python）→ **exit 3** + 清晰 stderr、`--help` 仍 0；`alpha<1` 文档 GPU → **exit 5** + stderr，CPU 仍可渲染；节点契约 **PACKAGE_VERSION 3.0.1**、RenderFdraw primitive widget 仅 `resolution`（=1）、shipped UI workflow `widgets_values` 仍为 1；GPU 环境 node `_run` 走 GPU 并返回 tensor，`FDP_GPU_RENDER=0` 强制 CPU fallback 成功（含 alpha<1 fdraw 自动 fallback） | v1.38 GPU 加速回归、无 GL/alpha 降级契约、节点不变量 |

### 断言 B 的已知限制（有实测依据）

任务书建议用「离边界 >3px 的像素渲染色相对源图色差 >0.25 的占比」来断言修复后显著下降。
在本合成场景上实测（128px；v1.33 期使用单色底，v1.35 起默认 toned，指标随底色变化）：

- v1.33 单色底：gate off `stray_color_px=3750`、far>3px 比例 `0.3470`；gate on `3687 / 0.3378`；
- v1.35 toned 底：gate off `371 / 0.0009`；gate on `432 / 0.0050`（仍满足“非回归”断言）；
- v1.36 默认（wash 底 + flow 顺形体 + region 顺序）：gate off `859 / 0.0699`；
  gate on `1431 / 0.1165` —— **默认几何下旧的 ≤1.10× 非回归契约不再成立**
  （顺形体长笔跨色界更多），记录为已知偏差 D-1；v1.33 契约改在
  `--flow-strokes off --paint-order legacy` 的 legacy 几何上断言（实测
  `317 / 0.0000` vs `377 / 0.0011`，满足 ≤1.10×）。默认几何只做宽松 sanity 上界
  （≤2×off+0.03），数字全部打印进日志供 verifier 引用。

该指标被底座/正式层几何与整体色差主导，gate 的收益只有约 1–2%，不足以支撑“显著低于”的
硬断言。因此测试**打印** before/after 两侧数字（verifier 可直接引用），A/B 断言只做
“gate on 不得显著变差”（legacy 几何 ≤1.10× + 0.02 下限）；真正的缺陷捕获是笔画级断言 A
（v1.33 修复前 68 条 unsafe、最大 Δ=0.4371；修复后 0 条）。

## 接口契约（Lead 同步，2026-10-24 v1.33 冻结）

下列数值同时被 `test_contracts.py` 中的冻结表固定，**改参数必须同步改测试和 SKILL.md**：

| CLI | 默认值 | 说明 |
|---|---|---|
| `--gradient-gate` | `on`（choices `on/off`） | `off` = 旧行为（公平 A/B） |
| `--gradient-max-delta` | `0.12` | 端点色差 `Δ=max_c|src(start)-src(end)|`（jitter 后的存储值也不得超）；`<=0` 关闭该子门 |
| `--gradient-edge-delta` | `0.10` | 相邻采样色跳 `max_k ∞norm(src_k-src_{k+1})`（9 点路径）；`<=0` 关闭 |
| `--gradient-edge-pct` | `100.0 = 禁用 edge-map 子门` | `<100` 时路径采样点命中 `fg & maximum_filter(grad,5)>pct` 即拒绝；`<=0` 或 `>=100` 关闭 |

> **默认值以测试常量 `_support.GATE_DEFAULT_*` 与 `test_contracts.FROZEN_GRADIENT_DEFAULTS`
> 为准（Lead 2026-10-24 冻结）**；改默认值必须同步这三处 + SKILL.md + 本表。

**接受条件（Lead 裁决的最终严格契约）**：① 所有路径采样点都在 fg 内；② 端点
`Δ = max_c|src(start)-src(end)| <= --gradient-max-delta`（**无例外**：平滑 ramp 不允许
绕过；jitter 后的存储端点同样受限）；③ 相邻采样色跳 `max_k ∞norm(src_k-src_{k+1})`
`<= --gradient-edge-delta`；④ 若 `--gradient-edge-pct` <100（默认 100 = 关闭该子门），
路径采样点不得落在高梯度 map。`--gradient-gate off` = 旧行为。
report 里的 `gradient_mean_step/gradient_max_step` 只作诊断，不改变接受逻辑；若 fixer 再
引入“平滑 ramp 例外”，`test_layered_base.py::TestGradientGateUnit::
test_smooth_ramp_with_large_delta_is_rejected` 会立刻失败（该用例用平滑红蓝 ramp 构造
Δ≈0.30 / 相邻步长≈0.03 的笔画，严格契约要求全部拒绝）。

report JSON 必有字段（`test_gradient_safety` 断言）：`gradient_strokes`、
`gradient_rejected_delta`、`gradient_rejected_edge`、`gradient_mean_delta`、
`gradient_max_delta`、`stray_color_px`；另有
`gradient_candidates/rejected_oob/rejected_edge_color/rejected_edge_grad/accepted_mean_delta/
rejected_*_frac/stray_color_frac/stray_color_thr/gradient_gate_params/gradient_by_pass/
gradient_edge_frac` 等辅助字段。

## 接口契约（Lead 同步，v1.34 texture-flat 冻结，2026-10-24）

下列数值同时被 `test_contracts.py::FROZEN_TEXTURE_FLAT_DEFAULTS` /
`FROZEN_CORE_DEFAULTS` 与 `test_texture_flat.py::FROZEN_*` 固定，**改参数必须同步改测试、
SKILL.md 与本表**：

| CLI | 默认值 | 说明 |
|---|---|---|
| `--texture-flat` | `False`（off） | v1.34 opt-in 细纹理层；默认关 = v1.33 旧行为 |
| `--texture-flat-count` | `8000` | 上限笔数；`0`=按面积自动（`round(6000·H·W/1536²)`）；超过 100k 剩余预算截断并告警 |
| `--texture-flat-spacing` | `2.5` | 候选网格间距（1280 长边基准，按 `rs=max(H,W)/1280` 缩放） |
| `--texture-flat-len-min` / `-len-max` | `5.0` / `9.0` | 笔长 px（1280 基准，按 rs 缩放） |
| `--texture-flat-width-min` / `-width-max` | `2.5` / `3.5` | 笔宽 px（1280 基准，按 rs 缩放） |
| `--texture-flat-lstd` | `0.01` | 当前渲染 3×3 局部 luma std 低于此值 → 平坦候选 |
| `--texture-flat-ref-std` | `0.0` | 额外要求参考图局部 std ≥ 此值；候选按参考纹理能量排序（推荐 0.015） |
| `--texture-flat-layer` | `19` | 输出笔 layer |
| `--texture-flat-jitter` | `0.01` | 笔色 jitter |

v1.34 pass1 同时新增（同样冻结）：`--base-color-mode` choices 增加 `source`
（默认仍 `single`）；`--base-blur 4.0`、`--base-gradient`（默认 off）、
`--base-width-factor 1.5`、`--base-length-factor 2.0`。

- 输出 strict 契约：kind=**`oil_flat_texture`**、`layer=19`、`alpha=1`、
  `points∈[0,1]`、`--texture-flat` 默认 off 时 **零** texture 笔且
  `texture_flat_params/texture_flat_candidates == None`。
- report 新字段：`texture_flat_strokes`、`texture_flat_candidates`、
  `texture_flat_params`（内含 requested/auto/planned/candidates/budget/emitted/
  capped_100k/spacing_px/ref_std_thr/flat_frac_fg 诊断键），以及
  `base_color_mode/base_blur/base_gradient/base_width_factor/base_length_factor`。
- 总笔数硬上限 `_MAX_STROKES = 100000`（`test_texture_flat` 断言）。
- 平坦区判定在单元级断言（controlled render：左平坦/右噪 → ≥70% 落平坦侧）；
  CLI 层在同样合成图上因渲染两侧都较平坦、候选按参考纹理能量排序，纹理笔会
  集中到参考纹理更强的一侧（这是 v1.34 的预期行为，见测试 docstring）。

## 接口契约（Lead 同步，v1.35 人类画师式底色冻结，2026-10-24）

下列数值同时被 `test_contracts.py::FROZEN_BASE_COAT_DEFAULTS` /
`FROZEN_CORE_DEFAULTS` 与 `test_base_coat.py::FROZEN_*` 固定，改参数必须同步
测试 + SKILL.md + 本表：

| CLI | 默认值 | 说明 |
|---|---|---|
| `--base-color-mode` | **`toned`**（choices `toned/none/single/quantized/source`） | 新默认=纯色单次薄涂；`none`=不涂底；`single/quantized/source`=legacy（仅复现旧产物） |
| `--base-color` | `auto` | `auto`=整幅图像所有像素逐通道 median（线稿≈白纸色）；也支持 `#RRGGBB` / `"r,g,b"`（0–1 或 0–255 自动判别） |
| `--base-coat-angle` | `0.0` | 行扫角度（度，0=水平） |
| `--base-coat-spacing` | `0.0`（哨兵） | 行距 px；0 = 取 `--coarse`（report 写解析后实际值） |
| `--base-coat-width-factor` | `1.04` | 笔宽 = pitch × factor；稠密画面栅格 overlap 3.2–4.0%；report 的 `base_coat_width_factor` 为自适应后的实际值，`_requested` 为 CLI 值 |
| `--base-coat-jitter` | `0.5` | 中线沿笔方向抖动 px（不会开缝） |
| `--mid-err-pct` / `--fine-err-pct` / `--pass4-err-pct` | **`60` / `45` / `45`** | v1.35 正式绘制默认下调（显式传参的旧命令不受影响） |
| `--max-curves` | `100000` | 4GiB 硬上限；超出时确定性均匀抽稀，优先级 **texture_flat → pass4 → pass3 → pass2**（base/detail 兜底），report 记 `capped_100k / cap_adjustments({layer:{removed,frac}}) / pre_cap_strokes` |

- 底色笔 strict 契约：kind=**`base_toned`**、layer=0、alpha=1、颜色唯一（std≈0）、
  **无 gradient / 无逐笔颜色抖动 / 无局部取色**；`none` 时 pass1 零笔且
  `base_color/base_coverage/base_overlap_frac/base_overlap_canvas_frac=None`、
  `base_color_std=0.0`、`base_strokes=0`。
- report 新字段：`base_color_mode/base_color/base_coat_angle/base_coat_spacing/
  base_coat_width/base_coat_width_factor(_requested)/base_coat_jitter/base_strokes/
  base_coverage/base_overlap_frac/base_overlap_canvas_frac/base_color_std/
  base_gradient_strokes/base_coverage_analytic/base_overlap_analytic/
  base_overlap_px_over2/base_max_count`。
- 栅格门禁（稠密画面）：fg 覆盖 ≥99%、`overlap_frac ≤0.05`、`count>2 == 0`、
  `max_count ≤2`（`test_base_coat.py` 用 `_support.stroke_footprints` 自算，
  与 report 的 `_base_raster_metrics` 同口径）。线稿稀疏场景推荐 `none`，
  toned 只断言纯色/无 gradient/canvas 口径，不硬断言 fg overlap。

> **v1.36 起默认底色是 `--base-coat-pattern wash`**（方向分散、overlap≤15%、
> 覆盖≥99%）；上表的行扫数值需显式 `--base-coat-pattern rows` 才能复现，
> `test_base_coat.py` 已按 wash / rows 两个分支分别断言。

## 接口契约（Lead 同步，v1.36 人类画法冻结，2026-10-24）

下列数值同时被 `test_contracts.py::FROZEN_HUMAN_PAINT_DEFAULTS/CHOICES` 与
`test_human_paint.py::FROZEN_*` 固定，改参数必须同步测试 + SKILL.md + 本表：

| CLI | 默认值 | 说明 |
|---|---|---|
| `--base-coat-pattern` | **`wash`**（choices `wash/rows`） | 宽笔斜向/弧线洗刷铺底（方向随带变化、相邻带轻叠、局部补缺），非水平行扫；`rows`=v1.35 行扫（legacy 复现） |
| `--wash-angle` | `-22.0` | wash 主方向（度）；report `wash_angle` |
| `--flow-strokes` | **`on`**（choices `on/off`） | 正式层抖动网格采样 + 结构张量切向 + 轻弧线，长=宽×1.5–3；低相干区用区域主方向（远离 0/90°）；off=旧 tile_pass |
| `--paint-order` | **`region`**（choices `region/scan/legacy`） | 区域质心最近邻路径→区域内部按 layer→同层大小降序；scan=旧 y 序；legacy=生成序 |
| `--region-count` | `12` | k-means 区域数（report `region_count_used`） |
| `--thick-dabs` | `0` | 可选厚涂笔数 int（demo 14000；report `thick_dab_strokes`） |
| `--clip-to-fg` | **`auto`**（choices `auto/on/off`） | v1.36 模式裁掉白纸（report `clip_to_fg`）；legacy 模式不裁 |
| `--max-curves` | `100000` | 不变；抽稀优先级/report 口径同 v1.35 |

- **legacy 自动回退**：显式 `--base-color-mode single/quantized/source` 且未传
  `--flow-strokes` / `--paint-order` 时 → flow=off、paint-order=legacy、pattern=rows、
  clip_to_fg=off，v1.33/v1.34 canonical 逐字节兼容
  （PNG SHA `30CDCE74…` / `69820EEF…`，`work/tests4/regression_evidence_v136.json`）。
- **wash 栅格契约**（≤768 真实栅格）：fg 覆盖 ≥0.99、`overlap_fg ≤0.15`、单色 std≈0、
  无 gradient/无逐笔颜色抖动；`--max-curves` 小预算只抽稀正式层，base 不被抽稀
  （`--max-curves 120` 单测）。
- **flow 指标口径**：`flow_orientation_frac` = 笔方向与局部结构张量切向夹角 ≤20° 的
  比例；`axis_aligned_frac` = 笔方向距水平/垂直 ≤10° 的比例；
  `direction_strokes` = 参与方向统计的正式笔数。
- **顺序指标口径**：`order_y_spearman` = 笔序列 vs 笔中心 y 的 Spearman
  （scan 应高、region 应低）；`order_adjacent_median_px` = 相邻笔中心距离中位数
  （区域成组应更小）；`order_region_run_frac` = 相邻笔落在同一 k-means 区域的比例。
- **底色统计口径**：`base_metrics_mode=raster`（work-res ≤768，真实逐笔栅格，与
  `_support.stroke_footprints` 同口径）+ `base_metrics_scale`（raster 时约 1.0）；`>768` 为
  `analytic`（report 标口径，不做 raster 硬断言——1536 Sunflowers 实测 analytic）。
- **report 新字段**：`base_coat_pattern/wash_angle/base_metrics_mode/base_metrics_scale/
  flow_strokes/flow_orientation_frac/axis_aligned_frac/direction_strokes/paint_order/
  paint_order_effective/order_y_spearman/order_adjacent_median_px/order_region_run_frac/
  region_count_used/clip_to_fg/thick_dabs/thick_dab_strokes/texture_flat_strokes`；
  v1.35 全部旧字段保留。
- v1.36 1536 实测（fixer 冻结版）：100k 笔、178.6 s、full PSNR 25.234 /
  content 24.1985、flow 0.9086 / axis 0.2663 / y-Spearman 0.197 / region_run 0.9993。

## 接口契约（Lead 同步，v1.37 剪影感知笔触生成冻结，2026-10-25）

下列数值被 `test_contracts.py::FROZEN_SILHOUETTE_DEFAULTS/CHOICES`、
`test_silhouette.py::FROZEN_SILHOUETTE_DEFAULTS` 固定，改参数必须同步测试 + SKILL.md + 本表：

| CLI | 默认值 | 说明 |
|---|---|---|
| `--silhouette` | **`auto`**（choices `auto/on/off`） | auto = `base_coat_pattern==wash` 且 `paint_order!=legacy` 且 `clip_to_fg!=off` 且白底占比>2% |
| `--silhouette-shrink-px` | `1.0` | `sil = distance_transform_edt(fg) > shrink`（去光晕）；sil 面积 <0.2*fg → fallback fg + warn（report `silhouette_fallback`） |
| `--silhouette-margin-px` | `0.25` | 逐点笔宽钳制 `width=min(width, 2*(dist_sil-margin))` 留边 |
| `--silhouette-min-width-px` | `0.6` | 钳制后 max 宽度低于此 → 丢笔 |
| `--silhouette-feather-px` | `0.8` | 最终合成 alpha 羽化宽度 |

- **生成期几何裁剪**（在 hard-cap / paint_order 之前，非后期修图）：
  非 `base_toned` 笔端点回缩（round cap 不出界）→ 逐点宽度钳制 →
  `max(width)<min-width` 丢笔；gradient 笔同样裁（不改色）；base_toned 跳过。
  最终合成用 sil 羽化 alpha（渲染合成的一部分，不是 post-process）。
- report：`silhouette_mode/silhouette_on/silhouette_area_frac/silhouette_fallback/
  silhouette_shrink_px/silhouette_margin_px/silhouette_min_width_px/
  silhouette_feather_px/silhouette_clipped_strokes/silhouette_points_trimmed/
  silhouette_dropped_strokes/silhouette_overshoot_before_max_px/
  silhouette_overshoot_after_max_px`；旧字段全保留。
- **账目口径**：裁剪在生成之后、cap 之前，因此
  `pass 计数(含 detail/texture/dabs) − silhouette_dropped_strokes == strokes_out`；
  `white_px_in_fg ≈ fg*(1−silhouette_area_frac)`（未涂的 halo 环）。
- `--silhouette off` == 旧渲染路径（无裁剪/羽化）；在 auto 不触发的非白底图上，
  `auto` 与显式 `off` 的渲染 PNG SHA 相同。
- legacy（single/quantized/source + paint_order legacy）自动 off →
  v1.33 `30CDCE74…` / v1.34 `69820EEF…` 逐字节不变。
- fixer 实测（chibi 1280，同口径）：非底笔 >0.5px 越界 **5,636→0**、
  >2px **3,182→0**、max **16.97→0.25px**、painted outside sil **13,325→0**、
  PSNR 25.827→25.366（−0.46dB）、笔数 38,737→34,107。

## 接口契约（Lead 同步，v1.38 GPU 渲染冻结，2026-10-25）

下列数值被 `test_contracts.py::FROZEN_GPU_RENDER_DEFAULTS/CHOICES` 与
`test_gpu_render.py` 固定，改参数必须同步测试 + SKILL.md + 本表：

| CLI / 项 | 默认值 | 说明 |
|---|---|---|
| 输入（互斥） | — | `--fdraw` / `--fbs` / `--render-json` / `--strokes`（layered_base 原始笔） |
| `--out` | 必填 | 输出 PNG |
| `--resolution` | `1024` | 长边像素；宽高比按文档画布 |
| `--supersample` | `2`（choices `1/2/3`） | 超采样倍率（SS×2） |
| `--device` | `gl`（choices `gl/cpu`） | `cpu` = 复用 `fbs_core.render`（fdraw/fbs）或 `hatch_gen._render_strokes`（strokes） |
| `--background` / `--width-scale` | `None`（白底 / 文档默认） | 可选覆盖 |
| `--timing-json` | `None` | 计时 JSON 输出路径 |

- **退出码**：`0` 成功；`2` 输入错误；**`3` 无 PyOpenGL/GL 上下文**（调用方回退
  CPU exact）；`4` 不支持的文档（如 region_fill）；**`5` 文档含 alpha<1 笔**
  （GPU 仅支持 alpha=1；节点把 3/4/5/超时都当 GPU 不可用 → fdrender exact）。
- **计时 JSON**：`load_s / sample_s / geometry_s / gl_init_s / gpu_s /
  readback_s / total_s`（+ `segments/strokes/resolution/out_size/supersample/
  device/float_fbo`）；CPU 设备路径记 `load_s/render_s/total_s + device="cpu"`。
- **节点**：`ComfyUI-FDP-T2I` v3.0.1；`FDPT2IRenderFdraw` 先 GPU（`sys.executable
  gpu_render.py`）→ 失败自动回退 `fdrender_cli render --quality exact`；env
  `FDP_GPU_RENDER=0` 强制 CPU；**workflow/widget 不变**（primitive widget 只有
  `resolution`，shipped UI workflow `widgets_values` 长度仍为 1）。
- **无新依赖**：仅用已有 PyOpenGL + glfw（ComfyUI python 自带）+ numpy + PIL；
  `fbs.py render --device {auto,gl,cpu}` 默认 `cpu`，旧 CPU 输出逐字节不变。
- **实测（fixer/analyst，chibi 38,737 笔，SS=2）**：GPU
  **1024=14.2 s / 2048=15.1 s / 4096=23.6 s** vs CPU exact 219 s / 581.6 s /
  ≈1260 s → **15.4× / 38.5× / 53×**；质量 GPU vs CPU exact
  **1024 40.219 dB/0.99566、4096 44.109 dB/0.99724**（差图仅边缘 AA）。
  本仓库独立复测（`work/tests6/gpu_evidence.json`）：chibi GPU 1024
  `total_s=14.114`；tiny fdraw 256/512 **PSNR 35.40/0.9990、35.09/0.9990**；
  合成 strokes **62.29/1.0000**；medium fdraw 512 GPU 9.0 s vs CPU exact 91.7 s；
  node GPU 路径（tiny）4.0 s vs `FDP_GPU_RENDER=0` CPU 3.5 s。
- **限制**：GPU 需桌面 OpenGL 上下文（headless/无 GL 自动 exit 3 → CPU）；仅
  alpha=1 文档（alpha<1 → exit 5 → CPU exact，当前 skill 流水线输出均为 alpha=1）；
  region_fill 文档 exit 4 → CPU；非 round 笔形按 round 近似（report
  `non_round_shapes`）；采样+load 占 GPU 总时间约 80%（后续可缓存/并行优化）。

## 如何加新测试

1. 公共工具都放在 `tests/_support.py`：路径常量（`SKILL_ROOT`/`SCRIPTS_DIR`）、
   `run_layered()`/`run_layered_ok()`、合成场景 `scene_canvas/scene_split/scene_checker/
   scene_flat_noise` + `flat_render()` + `write_scene()`、`argparse_defaults()`/
   `argparse_choices()`、`script_help()`/`has_cli_arg()`、`reconstruct_gradient_deltas()`、
   `stray_color_pixels()`、`load_rgb()`、`fg_mask()`、`texture_flat_strokes()`、
   `expected_texture_{width,length}_px()`、`sha256_file()`；
   **v1.36 画法指标**：`base_coat_strokes()`/`formal_strokes()`、`stroke_direction_angles()`、
   `axis_aligned_frac()`、`direction_entropy()`、`sequence_y_spearman()`、
   `adjacent_center_distance_median()`、`region_run_frac()`、`flow_orientation_frac()`
   （全部只读 strokes JSON + 源图，确定性）；新测试直接
   `import _support as S`（包方式运行时有 `from tests import _support as S` 兜底）。
2. 需要跑 CLI 的测试：用 `tempfile.TemporaryDirectory()` 建临时目录，输入图写临时目录，
   `S.run_layered_ok(self, [...], label="...")` 自动带 stdout/stderr 失败信息；**不要**读写
   `training/`、`work/` 或 skill 目录里的任何文件。
3. 断言合成图上的数值前先确认量级：小图（128–192px）单次 `layered_base` 约 1.5 s，够用；
   不要用 512+ 或真实大图（放 `test_slow_real_image.py` 并加 `RUN_SLOW=1` 门禁）。
4. 新 CLI 参数要同时更新：`_support.py` 常量（若冻结）→ `test_contracts.py`
   `FROZEN_*_DEFAULTS` → SKILL.md/README 契约表。
5. 保持确定性：固定 `--seed`、固定 numpy seed、不依赖随机临时路径内容；不要断言
   `seconds`、绝对路径、mtime。
6. 新的已知缺陷（修复前预期失败）写成普通失败并在 `work/tests/baseline_result.md` 说明；
   若整个套件必须保持绿，用 `@unittest.skipUnless(环境变量...)` 包起来并在 README 记录。

## 修复前/后基线复现（可选）

`_support.layered_script()` 支持环境变量 `FOURIER_LAYERED_BASE` 指向另一个
`layered_base.py`（默认不设置 = 本 skill 的 `scripts/layered_base.py`，verifier 无需设置）：

```powershell
$env:FOURIER_LAYERED_BASE = "<workspace>/work/backup/skill_before_v1.33/scripts/layered_base.py"
& $PY -m unittest discover -s tests -v          # 修复前快照：4 失败 = 2 渐变缺陷 + 2 --detail 过滤缺陷
Remove-Item Env:FOURIER_LAYERED_BASE
& $PY -m unittest discover -s tests -v          # 修复后：全绿
```

两次完整输出分别保存在 `work/tests/logs/before_full.txt` / `after_full.txt`，
摘要见 `work/tests/baseline_result.md`。
