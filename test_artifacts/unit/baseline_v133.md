# 测试基线结果（before / after）—— fourier-multistroke-training v1.33

> 定稿时间：2026-10-24 10:45（Asia/Shanghai）。
> 被验收代码：`scripts/layered_base.py` SHA256
> `4B356C8FCAFBE11C8C745E5EB11863B68DABD81CDF12813FEECFD1E8B887DB7D`（fixer-core 冻结版）。
> 接口默认值已由 Lead 冻结：
> `--gradient-gate on` / `--gradient-max-delta 0.12` / `--gradient-edge-delta 0.10` /
> `--gradient-edge-pct 100.0（=禁用 edge-map 子门）`；严格契约（无平滑 ramp 例外，
> jitter 后的存储端点 Δ 同样受限）。
> 测试侧常量以 `tests/_support.py::GATE_DEFAULT_*` + `tests/test_contracts.py::
> FROZEN_GRADIENT_DEFAULTS` 为准。

## 结论（先看这里）

1. **测试套件已建成**：`skills/fourier-multistroke-training/tests/` 共 **43 用例**
   （41 快测试 + 2 慢测试默认 skip 2），默认全量 **43 tests OK (skipped=4) / 31.0 s**；
   `RUN_SLOW=1` 时慢测试 **2/2 OK / 6.7 s**（另 2 个 skip 是 fixer-docs 未完成前的文档等待项）。
2. **缺陷已被测试捕获（修复前）**：用 `work/backup/skill_before_v1.33` 旧快照跑同一套件
   **43 tests → 4 failed / 15 skipped / 29.0 s**，其中 **2 个失败 = 渐变缺陷**
   （68 条渐变笔 Δ>0.12、最大 0.4371；report 缺 6 个缺陷指标字段），另 2 个失败 =
   Lead 确认的 `--detail` 过滤缺陷（`base_pass*/fix_*/soft_*` 未丢弃，13 条只留 9）。
3. **canonical 1536 端到端 A/B 已复跑**（Lead 给的 canonical 命令）：
   修复前（`--gradient-gate off`）**81,815 笔 / PSNR 23.1883 / SSIM 0.61788**
   —— 与文档基线**完全一致**；修复后（gate on）**81,010 笔 / 23.1059 / 0.61583**，
   渐变笔中端点 Δ>0.12 的 unsafe 笔 **5,329 → 0**（max Δ 0.6875 → 0.1200）。
4. 代价：PSNR −0.082 dB、SSIM −0.00205（0.35%/0.33%），页面级 `stray_color_px`
   23,055 → 23,423（+1.6%，该指标被整体底色误差主导，不灵敏，见 §三备注）；
   用户红圈区域的视觉改善以 `work/visual`/`work/fix` 的 ROI 图与指标为准。
5. 默认跑法的 4 个 skip：2 个慢测试（需 `RUN_SLOW=1`）+ 2 个文档等待项
   （SKILL.md 尚未写入 v1.33 新参数、目录树尚未加入 tests/，属 fixer-docs 交付项）；
   fixer-docs 完成后这两项会自动执行/转绿。

## 环境与命令

- Python：`<python>`
  （3.13 / numpy 2.5.4 / Pillow 12.3 / scipy 1.18.1），Windows，16 核。
- 测试 cwd：`skills/fourier-multistroke-training`；子进程解释器一律 `sys.executable`，
  路径全部由 `__file__` 推导，合成图固定 seed，结果确定。

```powershell
# 快测试（全量）
Set-Location "skills/fourier-multistroke-training"
python -m unittest discover -s tests -v

# 慢测试
$env:RUN_SLOW = "1"; python -m unittest tests.test_slow_real_image -v; Remove-Item Env:RUN_SLOW

# 一键复跑三类日志（UTF-8）：python work/tests/run_suite.py [after|before|slow|all]
```

## 一、测试套件基线：修复前 vs 修复后

| 项 | 修复前（旧快照 `A9554DD3…`） | 修复后（当前实现，冻结默认值） |
|---|---|---|
| 全量快测试 | 43 tests / **4 failed** / 15 skipped / 29.0 s | **43 tests / 0 failed / 4 skipped / 31.0 s** |
| `test_gradient_safety` | 2 failed（断言 A、C）+ 4 skip（旧代码无 gate API/参数） | 7/7 PASS（断言 A/C/B、gate off 区分力、自定义阈值、确定性） |
| `test_layered_base` | 2 failed（`--detail` 默认过滤）+ 7 skip（旧代码无 gate API/指标） | 18/18 PASS（含平滑 ramp 严格契约单测） |
| `test_contracts` | 1 failed（`--detail` CLI）+ 2 skip（文档等待项） | 16/16 PASS + 2 skip（文档等待项） |
| 慢测试 `RUN_SLOW=1` | 2/2 skip（旧代码无 gate API） | **2/2 PASS / 6.7 s** |
| 日志 | `work/tests/logs/before_full.txt` | `work/tests/logs/after_full.txt`、`slow_run.txt` |

修复前失败证据（原文摘要）：

```
FAIL test_no_unsafe_gradient_strokes: 68 gradient stroke(s) exceed --gradient-max-delta 0.12
     top: base_pass4 Δ=0.4371, base_pass2 Δ=0.4078, base_pass3 Δ=0.4035 ...
FAIL test_report_has_defect_metrics: report missing ['gradient_strokes',
     'gradient_rejected_delta','gradient_rejected_edge','gradient_mean_delta',
     'gradient_max_delta','stray_color_px']
FAIL test_detail_underpainting_strokes_are_dropped: --detail kept 9 of 13 strokes; expected 3
FAIL test_load_default_drops_underpainting_kinds: ['base','base_pass1','base_pass2'...] kept
```

## 二、canonical 1536 端到端 A/B（Lead 提供的 canonical 命令）

命令（before 额外加 `--gradient-gate off`；after 用默认 gate on）：

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py `
  --input training/input/Sunflowers_square.png `
  --detail training/sunflowers/clean_r1/cleaned_strokes.json `
  --work-res 1536 --coarse 20 --mid 9 --fine 4.5 `
  --mid-err-pct 85 --fine-err-pct 78 --fine-grad-pct 55 `
  --base-color-mode quantized --base-k 8 --base-jitter 0.006 `
  --mid-jitter 0.006 --fine-jitter 0.008 `
  --upper-color-mode source --upper-blur 2.5 --upper-gradient `
  --pass4 --pass4-err-pct 75 --pass4-spacing 5 `
  --outdir work/tests/canonical_after1536        # before: + --gradient-gate off
                                                 #         --outdir work/tests/canonical_before1536
```

| 指标（同参数同 detail，1536） | before：canonical + gate off（旧行为） | after：canonical + gate on（默认） |
|---|---|---|
| 总笔数 / detail 笔 | **81,815 / 19,296** | 81,010 / 19,296 |
| 全幅 PSNR / SSIM | **23.1883 / 0.61788**（= 文档基线，完全一致） | 23.1059 / 0.61583（−0.082 dB / −0.00205） |
| 渐变笔数 | 57,976（占 70.9%） | 50,950（占 62.9%） |
| 渐变笔端点 Δ（存储值 max-abs）max | **0.6875** | **0.1200**（= 阈值上限） |
| 其中 Δ>0.12 / >0.15 / >0.20 | **5,329 / 3,160 / 1,417** | **0 / 0 / 0** |
| report 拒绝计数（delta/edge/oob） | 0 / 0 / 0（旧代码无门控） | **5,494 / 725 / 2** |
| `stray_color_px`（全幅，thr 0.25） | 23,055（frac 0.012873） | 23,423（frac 0.013078，+1.6%） |
| 运行时间（本机，单进程，含 detail 读取） | 88.7 s | 73.8 s（无变慢） |
| strokes JSON SHA256（前 16） | `110c96355db184d3` | `7f98ecb413fc1977` |

结论：canonical + gate off **逐位复现文档基线**（81,815 笔 / 23.1883 / 0.61788），
证明 canonical 参数反解正确；gate on 后 unsafe 渐变笔 5,329 → **0**，代价 PSNR −0.082 dB。
原始数据：`work/tests/canonical_compare.json`；运行输出：
`work/tests/logs/canonical_before1536.out.txt`、`canonical_after1536.out.txt`。

> 备注：全幅 `stray_color_px`/stray_frac 对门控不敏感（合成图实测 3750→3671、
> canonical 23,055→23,423），因为它统计的是「渲染色与源图差 >0.25」的全图像素，
> 被底色层与细节层误差主导。因此测试里的断言 B 只做「非回归 + 打印数字」，
> 缺陷捕获以笔画级端点 Δ（断言 A）为准；红圈区域是否明显减轻由
> `work/visual`、`work/fix` 的 ROI 指标与三联图判定。

## 三、before/after 缺陷指标（合成图 + 真实图两层证据）

| 证据 | before | after |
|---|---|---|
| 合成 192×144 红蓝硬边界（128px），gate on 的 accepted 渐变笔 | 528 条全接受；68 条 Δ>0.12；max 0.4371 | 439 条；0 条 Δ>0.12；max 0.1045 |
| 合成图 A/B 渲染（同场景，gate off vs on） | stray_px 3750；far>3px 假色比 0.3470 | stray_px 3671；far>3px 假色比 0.3361 |
| 真实图 Lvpi5.jpg 256px（慢测试，gate on） | —（旧代码无门控） | 713 条渐变；max Δ=0.1199；0 unsafe |
| `--detail` 丢弃 `base_pass*/fix_*/soft_*` | 13 条留 9 条 | 13 条留 3 条 |

> dev 中间版本的插曲：fixer 曾在 hash `94A37849…` 短暂加入「平滑 ramp 例外」，
> 导致真实图 8/287 条 Δ>0.12 的平滑渐变被接受（max 0.1711）；新增单测
> `test_layered_base.py::TestGradientGateUnit::test_smooth_ramp_with_large_delta_is_rejected`
> 立即捕获（25 条 Δ 最高 0.2976 的平滑渐变被错误接受，`work/tests/logs/dev_smooth_ramp.txt`），
> Lead 裁决回退为严格契约。该用例保留为防回归。

## 四、证据与产物

| 文件 | 内容 |
|---|---|
| `skills/fourier-multistroke-training/tests/` | 测试套件（`test_layered_base.py` 18 / `test_contracts.py` 16 / `test_gradient_safety.py` 7 / `test_slow_real_image.py` 2 / `_support.py` / `README.md`） |
| `work/tests/logs/before_full.txt` | 修复前旧快照全量（43 tests，4 failed，29.0 s） |
| `work/tests/logs/after_full.txt` | 修复后全量（43 tests，OK，31.0 s） |
| `work/tests/logs/slow_run.txt` | `RUN_SLOW=1` 慢测试（2/2 OK，6.7 s） |
| `work/tests/logs/canonical_*.out.txt` | canonical 1536 A/B 运行输出 |
| `work/tests/canonical_compare.json` | canonical A/B 指标（笔数/PSNR/SSIM/渐变 Δ 分布/SHA） |
| `work/tests/calib_ab.json` | 旧快照 vs gate off 逐字节一致 + 三张合成图 A/B 数字 |
| `work/tests/run_suite.py` | 一键复跑 before/after/slow 日志（UTF-8） |

## 五、备注

- 任务书口径「2 个失败 = 已捕获的渐变缺陷」= 第一节的
  `test_no_unsafe_gradient_strokes` + `test_report_has_defect_metrics`；
  另有 2 个失败是 Lead 确认、本轮一并修复的 `--detail` 过滤缺陷（单元 + CLI 两层各抓一次）。
- 最终默认值改动的同步点（3 处 + 文档）：
  `tests/_support.py::GATE_DEFAULT_*`、`tests/test_contracts.py::FROZEN_GRADIENT_DEFAULTS`、
  `tests/README.md` 契约表、`SKILL.md`。
- canonical 命令若在后续周期改变，请同步本报告与 `work/tests/run_suite.py` 中的复跑说明。
