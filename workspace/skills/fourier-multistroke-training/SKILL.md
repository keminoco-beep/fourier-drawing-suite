---
name: fourier-multistroke-training
description: 把任意参考位图训练成「多笔画/逐笔画傅里叶 + 笔刷属性」的纯笔画复刻：无区域填色、每轮保存例图与指标、支持断点续训、全局细节强化、4GiB 高精度编译与容器校验。适用于彩插/线稿/油画/图标的手绘笔触复刻、训练循环、参数上限（R≤1536、ε=0.08、N_max≤32768、max_curves≤100000、.fbs/.fdraw ≤4GiB）内的精度优化。触发词：多笔画训练、逐笔画傅里叶、纯笔画复刻、笔触训练、图生图笔触、高精度笔触、fbs 训练、fdraw 训练、fourier multistroke。不要用于：单条傅里叶曲线（会因抬笔/折返变成糊块，已实测否决）、照片级复刻、文字生图。
agent_created: true
---

# fourier-multistroke-training

把一张位图训练成 **per-stroke（多笔画）傅里叶表示**：每条笔画 = 一条 Fourier 路径 +
逐点 width / RGB / alpha / shape；**没有区域填充元素**（`fill_count = 0`）。
训练循环按人类绘画逻辑分层（铺色排线 → 纹理 → 勾线 → 细节 → 补洞），
每轮生成、渲染、评分、保存例图与指标，支持断点续训；训练后跑一轮**与内容无关的全局细节强化**
（顺流细线 / 低对比边缘 / 小色区细排线 / 亮斑同心弧 / 暗部细笔，自动按像素数缩放），
最后用 4GiB 高精度模式编译 `.fbs`、渲染高分辨率 PNG、打包 `.fdraw` 并自动校验。

> **4GiB 高精度模式（2026-10 起）**：`.fbs`、`render.json`、`.fdraw` 解压总大小均放宽到 4GiB；
> `max_curves` 放宽到 100000；`N_max` 可到 32768（一般 16384 已够）。
> 训练时用 `epsilon_px=0.08`、`N_max=16384`；细节强化后再编译，精度不降级。

> **重要边界**：本 skill 明确是**多曲线**（多条 stroke）版本。
> 曾尝试 strict single-curve（一条路径/一次 FFT）在同一张 17k 笔彩插上训练，
> 结果只能是线稿或糊块；单曲线 + 0 填色 + 高密度彩铅笔画在当前 N 上限下不可兼得。
> 需要高保真/低性能开销时用本 skill，不要重新引入 single-curve。

## 何时使用 / 不要用

| 场景 | 用不用 | 说明 |
|---|---|---|
| 参考位图 → 纯笔画复刻（彩插/线稿/图标） | ✅ | 本 skill 主路径 |
| 想复刻“人类分层用笔”：排线铺色、勾线、细节 | ✅ | 层 0/2/3/5/6/7 显式分层 |
| 要每轮训练图 + 指标 + 断点续训 | ✅ | `run_train.py` |
| 要把训练结果交付成 `.fbs` / `.fdraw` / PNG | ✅ | `compile_best.py` + `pack_fdraw.py` |
| 要 1 条傅里叶曲线画完整图 | ❌ | 已实测失败；改用多笔画 |
| 照片/实拍写实复刻 | ❌ | 用 `fourier-brushstroke` 的照片实验通道，不承诺保真 |
| 文字 prompt 直接生图 | ❌ | 用 `fourier-prompt` |
| 需要 SVG 保真/无限缩放 | ❌ | 本 skill 目标是像素复刻，不是 SVG |

## 目录与依赖

```
fourier-multistroke-training/
├── SKILL.md
├── scripts/
│   ├── analyze_input.py        本地图像分析/预设推荐（不需要 LLM）
│   ├── pipeline.py             **一键本地流水线**（分析→训练→细节/柔化→校色→编译→校验）
│   ├── soft_atmosphere.py      通用大气/雾感底层（宽而薄的色带）
│   ├── soft_glaze.py           通用顶层柔光罩（压掉硬排线，保细节）
│   ├── sparse_precision.py     笔画预算/平坦区清理/高梯度修补（少笔版）
│   ├── gradient_swath.py       区域主方向渐变宽笔（替代大面积排线）
│   ├── human_workflow.py       人类画家流程：ground/分层/线稿置顶
│   ├── coarse_to_fine.py       粗底细顶：底层宽笔随意涂抹，线稿层精确
│   ├── opaque_base.py          不透明底色：先涂匀再覆盖，去掉半透明涂抹
│   ├── base_orientation_fix.py 修正 base 直线纹理与局部流向不一致的斜线伪影
│   ├── flow_tile_base.py       全图局部流向 + 重叠网格底：覆盖保证、无扫描线
│   ├── detail_aware_flow.py    细节尺度自适应 + 暗线恢复（小结构/村庄/屋顶）
│   ├── layered_base.py         分层底色 + 局部多级色板（64/32/16px）
│   ├── fdraw_to_video.py       .fdraw 一键转绘制过程 MP4（无 AI）
│   ├── hatch_gen.py            纯笔触光栅/渲染核心（供其它脚本 import）
│   ├── hybrid_gen.py           训练用生成器（base/tex/detail/hairflow）
│   ├── run_train.py            多轮训练循环（断点续训/每轮存图）
│   ├── refine_best.py          脸部/墨线精修
│   ├── refine_visual.py        线条锥度 + 顺流发丝精修
│   ├── refine_detail.py        **通用全局细节强化**（不针对具体图像）
│   ├── calibrate_color.py      **通用色彩校准**（渲染后色彩反馈，提升还原）
│   ├── clean_strokes.py        **通用笔触清理**（重采样/平滑/去卷曲抖动）
│   ├── thick_dabs.py           **通用粗短笔画层**（3–9px 短笔、宽度 3–8px）
│   ├── prune_strokes.py        笔画剪枝（Pareto 省笔）
│   ├── compile_best.py         strokes → .fbs → 高分辨率 PNG
│   ├── pack_fdraw.py           .fbs → .fdraw + validate
│   ├── verify_delivery.py      容器/精度/填充数自动校验
│   ├── audit_rounds.py         每轮产物审计
│   ├── make_progress.py        轮次总表 + GIF
│   ├── make_video_circles.py   傅里叶圆过程视频（多笔画逐笔）
│   ├── build_strokes_fbs.py    手写 strokes → .fbs（可选）
│   └── gpu_render.py           **GPU 渲染（v1.38）**：fdraw/fbs/strokes → PNG（OpenGL/GLSL）
├── references/
│   ├── PIPELINE.md             完整工作流/指标/参数（本 skill 详细版）
│   ├── ALGORITHM.md            算法分层/评分/坐标/上限
│   ├── METRICS.md              官方四维度量读数
│   ├── RESULTS.md              历史训练/交付档位/性能数据
│   ├── SINGLE_CURVE_TRIAL.md   已否决单曲线尝试与根因
│   ├── VERIFICATION.md         交付合规校验
│   └── ROUNDS_AUDIT.md         每轮产物审计样例
├── tests/
│   # unittest 自动化测试（v1.38 起；142 用例，运行方式见「测试」一节）
│   ├── README.md               测试说明/覆盖矩阵/慢测试开关
│   ├── __init__.py             测试包标记
│   ├── _support.py             公共工具（路径/CLI/合成场景/渐变 Δ 重建）
│   ├── test_base_coat.py       v1.35 底色：纯色/单次薄涂/无底色（19 用例）
│   ├── test_contracts.py       CLI/文档/编码/接口契约（26 用例）
│   ├── test_gpu_render.py      **v1.38 GPU 渲染**（19 用例，10 条 GPU 用例默认 skip / RUN_GPU=1）
│   ├── test_gradient_safety.py 渐变安全门缺陷回归（7 用例）
│   ├── test_human_paint.py     v1.36 人类画法：wash/flow/区域顺序（18 用例）
│   ├── test_layered_base.py    单元不变量 + 渐变门单元（18 用例）
│   ├── test_silhouette.py      v1.37 剪影感知笔触生成（15 用例）
│   ├── test_slow_real_image.py 真实图慢测试（RUN_SLOW=1，3 用例）
│   └── test_texture_flat.py    texture-flat 细纹理层（17 用例，v1.34）
└── assets/
    ├── preview_lean_fix_R2048.png
    └── compare_sheet.png
```

**依赖**：Python ≥3.10、`numpy`、`Pillow`、`scipy`、`imageio`+`imageio-ffmpeg`（视频）。
必须有一个可 import 的 `fourier-brushstroke/scripts/fbs_core`；本 skill 的脚本会从自身向上查找
`fourier-brushstroke/scripts/fbs.py`。若 base skill 不在同一 workspace 祖先链中，设置：
`$env:FOURIER_WS = "<包含 fourier-brushstroke/ 与 fourier-prompt/ 的目录>"`。
`.fdraw` 打包需要 `fourier-prompt/fourier_prompt`（同一 workspace）。

## 一键本地流水线（推荐）

先把图像分析、训练、细节/柔化、校色、4GiB 编译、打包、校验全部放到本地脚本，
agent 只需调用一次，避免多轮 tool 往返和 token 消耗：

```powershell
$PY = "<python.exe>"    # 换成你的 Python 解释器路径（已装 numpy/Pillow/scipy）
$SK = "<workspace>/skills/fourier-multistroke-training"
& $PY "$SK/scripts/pipeline.py" --input "<input>" --outdir "<output>" --preset auto
# 输出 outdir/pipeline_summary.json，内含 analysis/preset/fbs/fdraw/verification
```

> 尖括号占位符（`<workspace>`、`<python.exe>`、`<input>` 等）请替换成本机实际路径（全文同）。

`pipeline.py` 会：
1. `analyze_input.py` 本地算对比度/纹理/饱和/画幅，自动选 `default|soft|impasto`、工作分辨率、轮数；
2. 调 `run_train.py`（带对应 presets）；
3. 柔化图自动走 `soft_atmosphere → 轻细节 → clean → soft_glaze`；
4. 厚涂图自动走 `thick_dabs`；
5. `calibrate_color → compile 4GiB → pack → verify`，最后写 `pipeline_summary.json`。

下面是分阶段手动命令（调试/精细控制用）。

## 快速开始

```powershell
$PY = "<python.exe>"    # 换成你的 Python 解释器路径（已装 numpy/Pillow/scipy）
$SK = "<workspace>/skills/fourier-multistroke-training"
$WS = "<workspace>"
$IN = "$WS/training/input/Lvpi5.jpg"

# 1) 多轮训练（可断点续训；每轮写 rounds/round_XXXX_R1408.png + report）
& $PY "$SK/scripts/run_train.py" --input $IN --outdir "$WS/training/run_1408_q" `
  --work-res 1408 --max-rounds 100000 --minutes 0 `
  --fix-passes 2 --fresh-every 12 --stroke-weight 0.02 --ssim-weight 1.0

# 停止：在 run 目录创建 STOP，下一轮边界退出
New-Item -ItemType File "$WS/training/run_1408_q/STOP" -Force

# 2) 取 best 做精修（脸部墨线 → 顺流发丝/锥度）
& $PY "$SK/scripts/refine_best.py" --run "$WS/training/run_1408_q" --input $IN `
  --outdir "$WS/training/refine_1408_r21" --count 300 --work-res 1408
& $PY "$SK/scripts/refine_visual.py" `
  --strokes "$WS/training/refine_1408_r21/refined_strokes.json" `
  --input $IN --outdir "$WS/training/refine_visual_r21" --work-res 1408 --flow-count 550

# 2.5) 通用全局细节强化（自动按像素数缩放；与图像内容无关）
& $PY "$SK/scripts/refine_detail.py" `
  --strokes "$WS/training/refine_visual_r21/visual_strokes.json" `
  --input $IN --outdir "$WS/training/detail_r21" --work-res 1408

# 2.55) 通用笔触清理：重采样 + 平滑，去掉叶尖/花瓣尖的卷曲抖动
& $PY "$SK/scripts/clean_strokes.py" `
  --strokes "$WS/training/detail_r21/detail_strokes.json" --input $IN `
  --outdir "$WS/training/clean_r21" --work-res 1408 `
  --max-pts 16 --smooth-iters 3 --max-turn 2.2 --max-step-turn 0.75

# 2.58) 通用粗短笔画层（油画厚涂/麦田类画面；增量，不删旧笔）
& $PY "$SK/scripts/thick_dabs.py" `
  --strokes "$WS/training/clean_r21/cleaned_strokes.json" --input $IN `
  --outdir "$WS/training/dabs_r21" --work-res 1408 --count 18000 `
  --len-min 3.5 --len-max 9 --width-min 3.5 --width-max 8

# 2.6) 通用色彩校准（全局 + 局部残差；可选，但建议）
& $PY "$SK/scripts/calibrate_color.py" `
  --strokes "$WS/training/dabs_r21/dabs_strokes.json" --input $IN `
  --outdir "$WS/training/calib_r21" --work-res 1408 --iters 1 `
  --sat-mul 1.05 --contrast-mul 1.0

# 3) 4GiB 高精度编译 .fbs + 渲染
& $PY "$SK/scripts/compile_best.py" --run "$WS/training/run_1408_q" `
  --strokes "$WS/training/calib_r21/calibrated_strokes.json" --input $IN `
  --outdir "$WS/training/final_4g" --resolution 1536 --render 2048,4096 `
  --nmax 16384 --epsilon 0.08 --max-curves 100000

# 4) 打包 .fdraw + validate
& $PY "$SK/scripts/pack_fdraw.py" `
  --fbs "$WS/training/final_4g/Lvpi5_purestroke_R1536.fbs.json" `
  --out "$WS/training/final_4g/Lvpi5_purestroke.fdraw"

# 5) 自动校验（fills=0 / 容器上限 / 渲染复算 / SHA256）
& $PY "$SK/scripts/verify_delivery.py" --dir "$WS/training/final_4g" --input $IN

# 6) 训练过程总表 + GIF
& $PY "$SK/scripts/make_progress.py" --run "$WS/training/run_1408_q" `
  --outdir "$WS/training/progress_1408" --cell 340 --cols 4
```

## 算法要点（完整见 `references/ALGORITHM.md`）

1. **纯笔触表示**：元素只有 `stroke`；每条含 Fourier 路径 + width/color/alpha/shape 曲线；
   `fill_count` 必须为 0。铺色也必须是宽笔扫掠/密集排线，不能用 region fill。
2. **人类分层**：
   - 层 0：粗分割色区的扫描线/宽笔铺色；
   - 层 2：细分割区的纹理排线；
   - 层 3：高光/白回收；
   - 层 5：图像梯度/暗墨骨架的细节线；
   - 层 6：误差补洞；
   - 层 7/8：脸部墨线、顺流发丝。
3. **训练循环**：每轮对上一 best 参数做变异（分区/模糊/笔宽/间距/edge 阈值/颜色抖动），
   生成 → 渲染 → 补洞 → 评分 → 保存 → 只保留 J 更高的 best。
   `J = PSNR + ssim_weight*SSIM − stroke_weight*(笔数/1000)`；
   质量优先用 `--stroke-weight 0.02 --ssim-weight 1.0`，
   省笔优先用 `--stroke-weight 0.10`。
4. **分辨率策略**：先在 1024 快速迭代，再用 1408 热启动微调；最终 `.fbs` 契约 R=1536，
   渲染 2048（或 4096 展示）。
5. **精修**：`refine_best` 加脸部墨线；`refine_visual` 给线条加锥度并加顺流发丝，
   打散量化色块；再用 `prune_strokes` 剪枝得到省笔变体。
6. **通用细节强化**：`refine_detail.py` 对任意图跑同一套全局细笔（不裁局部、不按图像定制）：
   结构张量顺流曲线、低对比彩色边缘、小色区细排线、亮斑同心弧、暗部细笔；
   数量按 `(H*W)/(1280*1280)` 自动缩放。4GiB 模式下总笔数可到 100000，细节层通常 20k–60k 笔。
7. **通用色彩校准**：`calibrate_color.py` 渲染当前笔画 → 对比源图全局均值/方差/饱和度 →
   修正每条笔画颜色（可加局部质心残差）；再渲染评估。默认 `--sat-mul 1.05` 轻微提饱和，
   避免过饱和；`--sat-mul 1.15` 更适合颜色偏灰的图。该 pass 与图像内容无关，任意图可用。
8. **低色差纹理 + 去抖动**：
   - `refine_detail.py` 新增 `oil_texture` pass：用高通局部能量（不是颜色差）找种子头纤维、
     厚涂肌理等低色差纹理，沿高频结构张量追 10–30px 短曲线；默认 `9000×megapixels` 笔。
   - `clean_strokes.py`：所有笔画按弧长重采样到 ≤16 点、平滑 3 次；按总转角/单步转角
     丢弃卷曲抖动（默认 `max-turn=2.2 rad`、`max-step=0.75 rad`），同时保留 base/tex/fix。
     这一步解决叶尖、花瓣尖的“毛刺/打结”，不改变大色块。
9. **粗短笔画层（thick_dabs.py）**：油画厚涂/麦田/天空需要大量短而粗的方向笔。
   按 4px 抖动网格 + 局部结构张量方向，生成 3–9px 长、3–8px 宽的三点微弯短笔，
   颜色取源图中位数；叠在基础层之上、细节层之下。增量添加，不删除旧笔。
10. **任意画幅**：`hatch_gen._render_strokes` 已改为按长边 `L=max(H,W)` 统一缩放 x/y
    （旧版按 W/H 分轴只对方形正确）。宽幅/竖幅输入可直接训练，不再需要补白。
11. **柔化/雾感模式**：
    - `run_train.py --preset soft`：大宽笔、低纹理、高 edge 阈值，适合莫奈/印象派低对比图；
    - `soft_atmosphere.py`：多尺度模糊 + 宽半透明色带做底层雾感；
    - `soft_glaze.py`：顶层/细节下宽 20–45px、alpha 0.35 的柔光罩，压掉硬分割排线但保留船/太阳；
    - `analyze_input.py` 会根据 contrast/high-frequency ratio 自动判定；
      白底占比 >22% 且低饱和时判为 line-art/chibi，走 `default`（避免粗短笔/厚涂误判）。
12. **本地优先**：图像分析、预设选择、参数推荐、轮次、编译、校验、对照图均在本地脚本完成；
    agent 不需要逐轮读图/调参，显著减少 token 与往返次数。
13. **少笔 / 去纹路（实验性）**：
    - `sparse_precision.py --target-keep 0.75`：保护线条笔，删弱 base/tex，平坦区去 fix/detail，
      只在高梯度残差补笔；实测可减 21% 笔画，代价约 -0.5dB；
    - `soft_glaze.py` 宽 15–30px/alpha 0.25 可压大面积色块纹路，PSNR/SSIM 反而略升，但笔数增加；
    - 两者目标冲突，默认交付仍走完整 clean；见 `training/SPARSE_OPTIMIZATION.md`。
14. **单线端点渐变笔（endpoint gradient）**：stroke 增加
    `"gradient":{"start":[r,g,b],"end":[r,g,b]}`，颜色按弧长从起点平滑过渡到终点；
    `hatch_gen` 训练渲染、`compile_best`/`structdoc`/`brush` 的 `.fbs` 编译均已支持，
    常数色路径不变。实测合成双色渐变 30 条宽笔：单色 14.54dB → 渐变 **23.76dB**；
    `.fbs` 渲染 23.83dB，0 填色，2-stop color curve。不能把已有短排线事后全改渐变
    （端点落在白底/边界会掉到 9–18dB）；正确用法是沿区域主方向生成少量长宽笔。
    详见 `training/GRADIENT_STROKES.md`。
15.5 **混合笔触（`compile_best.py --mixed-brushes`）**：按 stroke kind 选笔刷家族：
    线/轮廓/边缘 → `ink_gpen`(ink)，发丝/流线 → `ink_fineliner`(round)，
    base/tex → `basic_round_hard`，柔化/渐变 → `basic_round_soft`。
    白发少女示例：15,303 笔，2048 PSNR **21.824** / SSIM **0.8861**，
    fbs 105.4MB，`.fdraw` 12.2MB，all_ok=true。适合线稿/淡彩，不适合照片厚涂。
16.5 **人类画家流程（`human_workflow.py`）**：参考 Krita 线稿分层、CSP 肤色
    BASE→SHADOW→BLUSH→HIGHLIGHT→线稿层序、古典油画 toned ground→block-in→glaze；
    新增 `ground` 底层（-20），线稿/轮廓置 **20 层最上**，微调 fix 压到线稿下方；
    `compile_best --mixed-brushes` 按线稿/发丝/底色/柔化分配 ink/round/hard/soft。
    白发少女：15,603 笔，2048 PSNR 21.262 / SSIM 0.8741。详见 `training/HUMAN_WORKFLOW.md`。
16.8 **粗底细顶（`coarse_to_fine.py`）**：按层控制精度——底层宽笔（≥8px）随机偏移/加宽、
允许涂出界；中层轻微抖动；线稿/细节层完全不动；可选顶层精确轮廓压边。
油画示例：15,412 笔，2048 PSNR 27.421 / SSIM 0.640，fbs 479.7MB，`.fdraw` 52.7MB，all_ok；
细线稿需调 `--min-coarse-px 12 --base-jitter-px 2` 或关闭。详见 `training/COARSE_TO_FINE.md`。
16.9 **不透明底色（`opaque_base.py`）**：删除 ground/soft_atmosphere/soft_glaze 等
    半透明底层，按量化区域生成 layer=-5、alpha=1 的 opaque_base 平铺层，base 也强制 α=1；
    上层再覆盖阴影/纹理/线稿。全幅画布会先加 48 条不透明 toned ground 防白洞。
    莫奈示例：14,622 笔，2048 PSNR **27.231** / SSIM **0.6356**，fbs 446.3MB，
    `.fdraw` 48.0MB，all_ok=true；视频前段是整幅均匀底色，不再有半透明涂抹。
    详见 `training/OPAQUE_BASE.md`。
16.95 **base 斜线伪影修复（`base_orientation_fix.py`）**：base 层按“每个色区一个
    region_orientation”做扫描线，遇到漩涡/高曲率区域会产生穿过流线的长直斜笔。
    逐条比较 base 方向与局部 structure-tensor 切向，偏差超阈值就替换为若干沿局部切向的
    短笔；detail/线稿不动。星月夜漩涡：替换 863 条，2048 PSNR 18.597 / SSIM 0.3949，
    斜线基本消失；不针对具体图像。详见 `training/starry_night/BASE_DIAGONAL_ROOTCAUSE.md`。
16.97 **全图流向 + 覆盖保证（`flow_tile_base.py`）**：用三套错位网格（16/12/10px）
    按局部 structure-tensor 切向生成重叠 base 宽笔，替代“每色区一个方向”的扫描线；
    笔长≈1.9×间距、笔宽≈1.35×间距，因此天然覆盖。星月夜重画：27,080 base + 18,969 detail，
    前景白像素仅 **2**，PSNR 17.526 / SSIM 0.3526；月亮/山脊不再有错误斜线。
    详见 `training/starry_night/FLOW_TILE_BASE.md`。
16.98 **细节尺度自适应（`detail_aware_flow.py`）**：flow tile 用统一宽笔会把
    1–3px 的村庄线/屋顶抹平；高梯度区改用 6/5px 细网格，另加 12k 条暗线恢复层。
    星月夜村庄：62,526 笔，白像素 1，PSNR 17.986 / SSIM 0.4078；能复原多层
    屋顶/窗光细笔，但仍受 detail 层分辨率限制。详见 `training/starry_night/DETAIL_LOSS_FIX.md`。
16.989 **`.fdraw` 一键出视频（`fdraw_to_video.py`）**：本地读取/验证 `.fdraw`，
    解出 render JSON 后调用现有过程渲染器，输出 MP4；无需 AI、无需手工编译 `.fbs`。
    `--duration/--fps/--size/--art/--circles` 全参数化，结束打印 JSON 摘要。
    30 笔样张 5s/512 用时 10.4s；大文档可先 5s/512 预跑。详见 `training/FDRAW_VIDEO.md`。
16.992 **上层渐变补色（`layered_base.py --upper-gradient`）**：底色 palette 收敛后，
    Pass2/3/4 用局部源色 + 两端渐变（抖动 0.006–0.008），可补回 palette 平色造成的
    色差。v1.32 旧行为（`--gradient-gate off`）Sunflowers 内容区：palette 24.253/0.530 →
    +渐变 **24.5058/0.53887**（81,815 笔，doc 内容区口径），高于旧逐笔源色底 24.178/0.520；
    严格单色底仅 23.443。v1.33 起默认开启**渐变安全门**（`--gradient-gate on`）：
    canonical 1536 输出 81,010 笔、内容区 **24.4302/0.53653**（−0.076 dB / −0.0023），
    输出渐变笔端点 Δ>0.12 由 5,329 条降为 **0**。参数/语义/A-B/限制见
    「上层渐变安全门（v1.33）」一节，训练记录见 `training/sunflowers/GRADIENT_SAFETY_FIX.md`。
    注意：文档旧“建议命令”缺 `--coarse 20 --mid 9 --fine 4.5 --mid-err-pct 85 --fine-err-pct 78`
    等参数，跑不出上表数字（QA-02），可复现命令以上一节为准。
16.991 **局部多级色板（`layered_base.py`）**：底色收敛后，Pass2/3/4 用 64/32/16px
    局部 tile 各自 K=6/8/8 色 palette，每笔仍只取一个平色、抖动 ≤0.015。
    Sunflowers：K=8 底 23.04/0.601 → 加局部色层 **23.52/0.611**（73,722 笔）；
    严格单色底即使 4 级色层也只有 20.99/0.584，不划算。
    详见 `training/sunflowers/COLOR_LAYER_GRANULARITY.md`。
16.990 **底色颜色约束（`layered_base.py`）**：v1.35 起默认 `--base-color-mode toned`（单一纯色 +
单次薄涂，见「人类画师式底色（v1.35）」）；`single|quantized|source` 保留为 legacy（仅用于复现
v1.33/v1.34 历史产物）+ `--base-k/--mid-k/--fine-k` + `--base-jitter/--mid-jitter/--fine-jitter`；
    避免逐笔采原色导致“多色草稿底”。Sunflowers：旧逐笔源色底 std 0.293/range 1.00；
    K=8 受控色板 48,584 笔，PSNR 23.04 / SSIM 0.601；严格单色底 std 0.006/range 0.045，
    但 PSNR 20.66。详见 `training/sunflowers/LAYERED_COLOR.md`。
16.988 **分层底色（`layered_base.py`）**：第一层粗网格全幅覆盖（唯一一次），
    之后按渲染误差只补局部（85/78 分位），细笔只进高梯度区；全幅覆盖笔数
    66,570→10,546（-84%），总笔数 75,302→62,700（-16.5%），PSNR 18.65 / SSIM 0.593。
    详见 `training/action_layered/LAYERED_UNDERPAINTING.md`。
16.987 **禁止后期 fix 覆盖纹理（`detail_aware_flow.py`）**：合并 detail 时默认移除
    `fix/soft_atmosphere/soft_glaze`；fix 若保留强制 layer=1。否则旧 detail 的 644 条
    fix（2px、alpha=1、layer6）会在约 37,895 笔处把小船纹理整片涂掉。
    修复后 43,842 笔，2048 PSNR 28.588 / SSIM 0.6374，纹理不再被覆盖。
    详见 `training/monet/NO_FIX_DETAIL.md`。
16.985 **线层防黑网（`detail_aware_flow.py`）**：dark/edge 线层必须看“局部暗差 ≥4.5%”
    才画、颜色取 40 分位、alpha ≤0.6、数量 2.5k/2k；否则会把颜料纹理骨架化成
    12k+ 条近黑线铺满整幅。星月夜修复版 2048：55,421 笔，PSNR 18.06，黑线明显减少。
    详见 `training/starry_night/BLACK_LINES_FIX.md`。
16.99 **Detail 层分辨率自适应（`refine_detail.py`）**：`rs=max(H,W)/1280`，
    所有 line/edge/texture 的像素宽度、step、长度、种子间距、hatch spacing 都乘 rs；
    数量仍按面积放大，受 `--max-total`/100k 上限约束；新增 `--res-scale` 和
    `--skip-streamlines/edges/hatch/halos/dark/texture`。星月夜 2048 (rs=1.6)：
    50,952 笔，SSIM 0.448（1280 为 0.408）；5000 源图若与用户图对齐则自动 rs=3.9。
    详见 `training/starry_night/DETAIL_DYNAMIC.md`。
17. **区域渐变宽笔生成器 `gradient_swath.py`**：量化+区域分析找大块平滑色区，
    沿区域主方向生成少量宽笔，端点色取模糊源图（避免白底/边界），保留原有线/细节笔，
    删除区域内 base/tex/hatch。支持 `--target-count` 预算 + `--repair` 残差短笔。
    chibi 示例：30,249 → **20,000 笔（-33.9%）**，677 条渐变宽笔 + 高梯度修补，
    2048 PSNR **25.35** / SSIM **0.9042**，fbs 211.6MB，`.fdraw` 25.0MB，all_ok=true。
    `--additive` 不删原笔、只叠加宽笔：莫奈 13,986 → **18,737 笔**，
    2048 PSNR **28.38** / SSIM **0.6489**（旧版 26.29 / 0.5915），fbs 320MB，`.fdraw` 35.7MB。
    **结构保护**：`--protect-edges --protect-px 15 --max-line-frac 0.0001 --skip-structured`
    防止宽笔盖掉背景建筑/桅杆；修正版 15,012 笔 / 26.46 / 0.5952，背景 HF 能量 0.0074→0.0110。

## 上层渐变安全门（v1.33）

`layered_base.py --upper-gradient` 让 Pass2/3/4 的笔携带两端颜色渐变（start/end 按弧长插值）。
v1.32 及以前，端点色直接取局部模糊源色，跨色边界的宽笔会把“边界两侧颜色”沿笔长插值成
彩色楔形/糊斑。v1.33 在 `tile_pass()` 内加入**默认开启**的渐变安全门：不合格的渐变笔退化为
“有效采样均值单色笔”，不改变笔几何，也不引入额外渲染/滤波。

| 参数 | 默认 | 含义 |
|---|---|---|
| `--gradient-gate {on,off}` | **on** | `off` = 完全跳过安全门，逐字节复现 v1.32 旧行为（A/B 与回滚用） |
| `--gradient-max-delta FLOAT` | **0.12** | 端点色差 `Δ = max_c abs(src(start)_c − src(end)_c)` 上限，超阈值→退单色；`<=0` 关闭该子门 |
| `--gradient-edge-delta FLOAT` | **0.10** | 沿笔 9 点路径采样中“相邻采样色跳”上限（各通道最大绝对差），超阈值→退单色；`<=0` 关闭 |
| `--gradient-edge-pct FLOAT` | **100.0（=禁用 edge-map 子门）** | 仅当 `0<pct<100` 时启用：路径采样点落在高梯度像素（`max_filter(grad,5) > P_pct(grad[fg])`）→退单色；`100` 或 `<=0` 关闭 |

**严格语义（Lead 冻结，2026-10-24）**——接受一条渐变笔需同时满足：

1. 9 点路径采样全部落在前景（fg）内；
2. 端点 `Δ <= --gradient-max-delta`，**无平滑 ramp 例外**（落盘 jitter 后的存储端点同样受限）；
3. 相邻采样色跳 `<= --gradient-edge-delta`；
4. 若 `--gradient-edge-pct` < 100，路径采样点不得命中高梯度 edge-map。

`--gradient-gate off` = 旧行为；`--gradient-edge-pct 90/95` 是比默认更保守的可选档。

**canonical 复现命令（v1.33 基线；visual-analyst 用 PNG SHA256 验证 = 用户所见“K8+upper gradients”版）**：

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input training/input/Sunflowers_square.png ^
  --detail training/sunflowers/clean_r1/cleaned_strokes.json ^
  --outdir <OUT> --work-res 1536 --coarse 20 --mid 9 --fine 4.5 ^
  --mid-err-pct 85 --fine-err-pct 78 --fine-grad-pct 55 ^
  --base-color-mode quantized --base-k 8 --base-jitter 0.006 ^
  --mid-jitter 0.006 --fine-jitter 0.008 ^
  --upper-color-mode source --upper-blur 2.5 --upper-gradient ^
  --pass4 --pass4-err-pct 75 --pass4-spacing 5
# A/B 修复前：同命令追加  --gradient-gate off
#   → 81,815 笔 / 全幅 23.1883 / 0.61788（float 渲染口径）
#   → PNG SHA256 30CDCE74F11A8E2C0CFBA7A0D760E49AC773DECB902180E2584EC17D6DEB9A01
#     与 training/sunflowers/layered_quant_grad/layered_base_R1536.png 逐字节一致
```

注意：这是**历史 canonical 实验参数**，不是脚本默认值；旧文档“建议命令”漏了
`--coarse 20 --mid 9 --fine 4.5 --mid-err-pct 85 --fine-err-pct 78 --fine-grad-pct 55`
`--mid-jitter 0.006 --fine-jitter 0.008`（QA-02），照抄跑不出 81,815 笔。

**前后实测（同参数、同 detail，只有门控开关不同）**：

| 指标 | before（`--gradient-gate off`） | after（默认安全门） | Δ |
|---|---|---|---|
| 总笔数 | 81,815 | 81,010 | −805 |
| 全幅 PSNR / SSIM（float 渲染） | 23.1883 / 0.61788 | **23.1059 / 0.61583** | −0.082 dB / −0.0021 |
| 内容区 PSNR / SSIM（doc 协议 ¹） | 24.5058 / 0.53887 | **24.4302 / 0.53653** | −0.076 dB / −0.0023 |
| 输出渐变笔端点 Δ>0.12 / >0.15 / >0.2 | 5,329 / 3,160 / 1,417 | **0 / 0 / 0** | 清零 |
| 输出渐变笔端点 Δ 最大 | 0.6875 | **0.1200** | −0.5675 |
| 渐变接受率（接受/候选） | 100% | **89.1%**（50,950 / 57,171） | −10.9% |
| 耗时（同机同会话） | 99.2 s | 97.1 s | 无退化 |

¹ doc 内容区协议：参考 = `training/input/8iNfq.png` LANCZOS→1165×1536，渲染裁 x∈[185,1350)；
`work/fix/metrics_1536_final.json` 是另一套裁剪/SSIM 实现口径，数值不同，不可与本表混用。

对照：`--gradient-edge-pct 90` 把渐变接受率压到 **15.7%**，全幅 PSNR 反而降到 **23.0642**
→ 默认 100（关闭 edge-map 子门）；90/95 只在需要更保守时手动开。

**低分辨率提示**：低分辨率（如 `--work-res` ≤384 的快速预览）下笔相对画布更大，
门控的 PSNR 代价会放大（独立验收实测 ChibiAction 384 下 **−0.478 dB**）。
正式交付建议用 canonical 1536；只在低分辨率做预览时可加 `--gradient-gate off` 关掉门控。

**report JSON 新增冻结字段**（测试/verifier 断言用，名字不可变）：
`gradient_strokes`（实际输出的渐变笔数）、`gradient_rejected_delta`、`gradient_rejected_edge`、
`gradient_mean_delta`（候选笔 Δ 均值）、`gradient_max_delta`、`stray_color_px`（前景内终图与参考
`max_c abs(ren−rgb) > 0.25` 的像素数）；另有 `gradient_candidates`、`gradient_rejected_oob`、
`gradient_accepted_mean_delta`、`gradient_by_pass`、`stray_color_frac`、`gradient_gate_params`
等诊断字段。旧 report 字段全部保留。

**A/B 与回滚**：同一条命令加/不加 `--gradient-gate off` 即公平 A/B（门控不改变 RNG 消耗，
gate on/off 的笔几何一致，被拒笔只改颜色写法）；行为回滚不换文件，加 `--gradient-gate off` 即可，
整文件回滚见 `work/fix/FIX_REPORT.md` §10。

**红圈缺陷是否解决（如实记录）**：

- **红圈 B（花瓶右缘/地面前景的跨边界深色楔形）局部减轻**：楔形区域平均 |Δ| 0.1768→0.1602；
  三联对比见 `work/fix/crops_B_triple.png`、`work/fix/zoom_B_wedge_triple.png`。
- **红圈 A（花头糊斑）基本无改善**：A 区 mae 0.04256→0.04294；见 `work/fix/crops_A_triple.png`。
- **根因**：红圈视觉主体来自 **K8 底色宽笔 + 调色板区域边界 + pass2/3/4 高误差百分位只局部覆盖**，
  不是渐变笔本身——把 doc 原图全部 57,976 条渐变笔压平后，A/B ROI mean|Δ| 仅 0.0052 / 0.0031。
  本版消掉的是“跨色边界的彩色渐变”这一类缺陷，**不承诺解决红圈全部观感问题**。
  后续建议（未实现、未验证）：结构区减小 pass2 笔宽、提高 pass3/4 局部覆盖、在色边界处把
  跨边界笔拆成两段单色落笔。

## 细纹理层（texture-flat，v1.34）

**要解决的问题：Sunflowers 中间红圈 A 的“颜色分层”**（render 坐标 `(735,692)-(1077,968)`，
花头/花瓣区）。pass1 底色每笔是一整块平面色（`palette_map(K=8)` / 单色，宽 1.5×coarse、
长 2×coarse），Pass2/3/4 只补高误差百分位 → A 区大面积停在底色平色块上。v1.33 after 量化：
3×3 luma std<0.01 的平色块占比 **49.1%**（reference 3.8%）、≥50px 平色块 **103** 个（ref 2）、
16 级亮度色带长度 run_y **5.99**（ref 2.43）、grad_x 0.0156（ref 0.0338）、uniq16 183（ref 407）。
只把底色换成更平滑的 source 色能提 PSNR、但 flat_frac 反而升高，**必须给平涂区补高频颜色变化**。

**v1.34 = 底色软化（可选）+ texture-flat 细纹理 pass（默认 off）**：

- `--base-color-mode source` + `--base-blur`：pass1 笔色取 `gaussian_blur(src, σ)` 的三点中位色
  （替换 K8 调色板平色）；pass1 还可 `--base-gradient`、`--base-width-factor/--base-length-factor`
  调几何。source 底色单独只到“用平滑换 PSNR”（A 24.06→24.54、blocks 103→53、flat_frac 升高）。
- `--texture-flat`：在**当前渲染**的 3×3 luma std < `--texture-flat-lstd` 的局部平坦区，
  按参考图局部纹理强度（`--texture-flat-ref-std` 过滤 + 降序优先分配）撒细短笔：
  长/宽按 1280 基准像素缩放（默认 5–9px × 2.5–3.5px），颜色 = 参考图局部均值 + 抖动，
  `layer=19`（detail 之上、最顶层 20 之下），kind=`oil_flat_texture`。它把“平色块/色带”
  打散成有方向的细纹理，是 **opt-in 推荐 recipe，不是默认行为改变**。

| 参数 | 默认 | 含义 |
|---|---|---|
| `--base-color-mode {single,quantized,source}` | **single**（不变） | source = pass1 取 `gaussian_blur(src, --base-blur)` 三点中位色（底色软化） |
| `--base-blur FLOAT` | **4.0** | source 模式的高斯 σ（@work_res 像素） |
| `--base-gradient` | off | pass1 笔端点渐变（走 v1.33 同一渐变安全门） |
| `--base-width-factor` / `--base-length-factor` | **1.5 / 2.0**（= 旧硬编码） | pass1 笔宽/长 = 系数 × `--coarse` |
| `--texture-flat` | **off** | 细纹理层开关；不传 = 与 v1.33 逐字节一致 |
| `--texture-flat-count` | **8000** | 纹理笔上限；0 = 按画布面积 auto（~6000@1536）；总笔数硬上限 100000 |
| `--texture-flat-spacing` | **2.5** | 候选网格 px@1280，按 rs=max(H,W)/1280 缩放 |
| `--texture-flat-len-min` / `--texture-flat-len-max` | **5.0 / 9.0** | 笔长 px@1280（按 rs 缩放） |
| `--texture-flat-width-min` / `--texture-flat-width-max` | **2.5 / 3.5** | 笔宽 px@1280（按 rs 缩放） |
| `--texture-flat-lstd` | **0.01** | 渲染 3×3 luma std 平坦阈值（低于即“平”候选） |
| `--texture-flat-ref-std` | **0.0** | 参考图 3×3 luma std 下限（0=off；推荐 0.015–0.02），并作候选降序优先级 |
| `--texture-flat-layer` | **19** | 纹理笔层号（detail 之上、不压最顶层 20） |
| `--texture-flat-jitter` | **0.01** | 每笔颜色抖动 |

**推荐 canonical v1.34（Sunflowers 1536；实测 99,929 笔 / 105.6 s）**：

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input training/input/Sunflowers_square.png ^
  --detail training/sunflowers/clean_r1/cleaned_strokes.json ^
  --outdir <OUT> --work-res 1536 ^
  --coarse 12 --mid 9 --fine 4.5 ^
  --mid-err-pct 85 --fine-err-pct 78 --fine-grad-pct 55 ^
  --base-color-mode source --base-blur 4 --base-k 8 --base-jitter 0.006 ^
  --mid-jitter 0.006 --fine-jitter 0.008 ^
  --upper-color-mode source --upper-blur 2.5 --upper-gradient ^
  --texture-flat --texture-flat-count 37000 --texture-flat-ref-std 0.015 ^
  --texture-flat-layer 19
# 不传 --pass4（腾 ~24k 笔预算给纹理层）；gate 默认 0.12/0.10/100
# 期望：strokes_out=99,929 / texture_flat_strokes=37,000 / ~105.6 s
# before（v1.33 after）= 上一节 v1.33 canonical：81,010 笔 / 全幅 23.0791 / 内容 24.4302
```

预算分配：v1.33 canonical 81,010 笔中 pass4 约 24,979–25,294 笔；去掉 pass4 后 base=62,929 笔，
100k 上限只剩 37,071 笔 → texture count=37,000。**“去 pass4”是这张图的预算取舍，不是默认行为变化**；
需要 pass4 细色层的图可保留 pass4 并降低 texture count（对照 A PSNR 24.16–24.21、blocks 101–112，
改善幅度小很多）。

**前后实测（analyst banding 口径：ROI A=(735,692)-(1077,968)，参考 `8iNfq.png`→1165×1536）**：

| 指标 | v1.33 after（before） | final v1.34 | Δ |
|---|---|---|---|
| A 区 PSNR / MAE | 24.0588 / 0.04343 | **24.7211 / 0.03965** | +0.662 dB |
| A 区 flat_frac（3×3 std<0.01） | 0.491 | **0.437** | −0.054 |
| A 区平色块数（≥50px） | 103 | **74** | **−29** |
| A 区 run_len_y / bstep_p95 | 5.99 / 0.141 | **5.45 / 0.133** | −0.54 / −0.008 |
| A 区 uniq16 / entropy16 | 183 / 4.97 | **195 / 5.32** | +12 / +0.35 |
| A 纹理笔数（`oil_flat_texture`，中心在 ROI） | 0 | **2,320 / 37,000**（足迹覆盖 74.9%，含重叠） | — |
| 全幅 PSNR / SSIM | 23.0791 / 0.61572 | **24.3322 / 0.62533** | **+1.253** / +0.0096 |
| 内容区 PSNR / SSIM（doc 协议） | 24.4302 / 0.53653 | **24.8173 / 0.54451** | +0.387 / +0.0080 |
| B 区 PSNR | 26.9247 | **28.0935** | **+1.169** |
| 总笔数 / 耗时 | 81,010 / 97 s | **99,929 / 105.6 s** | +18,919 / +8.6 s（含额外渲染） |
| `stray_color_px` | 23,423 | 22,093 | −1,330 |

视觉终判：**A 区“颜色分层”视觉上明显减轻**（平坦色带被打散成有方向细纹，seedhead 从平滑泥块
变回细纹）；B 区旧深色楔形/脏块消失、白花瓶边缘干净；无白点/彩色噪点；去 pass4 在本图无副作用
（分区 PSNR：背景 +0.22、地面 +2.93、花瓶 +0.38 dB；分区 flat_frac 复算：bg 0.420→0.374 下降、
ground 0.838→0.845 与 vase 0.510→0.525 略升——flat_frac 子项不改变“PSNR 无副作用”的主结论）。
图：`work/fix2/crops_A_final.png`（4×）、
`work/fix2/crops_B_final.png`（2×）、`work/fix2/final_v134/layered_base_R1536.png`。

**report 新增字段**：`texture_flat_strokes`（实际输出纹理笔数）、`texture_flat_candidates`、
`texture_flat_params`（requested/emitted/candidates/budget/ref_std_thr/spacing_px/flat_frac_fg/
capped_100k 等）；pass1 新字段 `base_color_mode/base_blur/base_gradient/base_width_factor/
base_length_factor`。旧字段全部保留；v1.33 渐变门字段不变。

**关闭 / 回滚**：不传 `--texture-flat` 即无纹理层（默认行为与 v1.33 逐字节一致，256 渲染
md5 `5CEDAAC084511CA7` 证据见 `work/fix2/FIX_REPORT.md` §8）；回到 v1.33 canonical 再加
`--coarse 20 --base-color-mode quantized --pass4 --pass4-err-pct 75 --pass4-spacing 5`。
**注意**：v1.33 canonical（含 pass4/quantized/coarse 20）仍可逐字节复现历史产物，v1.34 只是
**新增推荐 recipe**，旧命令行为不变。

**已知限制（如实）**：blocks 74 仍高于 analyst MUST 65（差 9）；`largest_flat_fraction`
0.064→0.112（唯一反向指标）：残余最大连续平色块位于画面中部（render bbox (735,735)-(881,967)），
参考图该处有中等局部纹理（ref 3×3 lstd p50≈0.027），预算限制下该区域纹理笔不足，
这是 largest_flat_fraction 反向上升的原因；
grad_x 0.0156 / uniq16 195 / flat_frac 0.437 三项 SHOULD 未达；纹理判词“偏糊”
（texture_ratio 0.615，距可接受线 0.62 差 0.005），4× 可见规则“梳状”细纹（画风取舍）。
根本瓶颈是 100k 笔上限下 A 区只能拿 2,320 条纹理笔；改进方向（未实现、未验证）：
提高 100k 曲线上限、区域自适应密度（高纹理平坦区局部加密）、用更细 detail 笔替换部分 pass3。
通用性：本 recipe 只在 Sunflowers 1536 实测；纹理笔颜色来自参考图，风格化/非写实输入应关闭；
去 pass4 对依赖 pass4 补平色阶的图（大面积平滑渐变/图形化输入）可能掉色准，需按 flat 面积
占比/误差决策。

## 人类画师式底色（v1.35）

**用户原则（本版设计约束）**：底色**只允许纯色**；**一个区域只涂一次**；底色**不得呈现“原图草图/
多色草稿”**；作画重心在**正式绘制层**；一切以“人类画师能做到且会去做”为准。
v1.32–v1.34 的 `quantized`（K8 局部取色多色平铺）和 `source`（逐笔取局部源色 + 渐变）会让底色
本身就是原图的模糊色块/色带，像“多色草图底”——v1.35 改变默认。

**新默认 `--base-color-mode toned`**：一个纯色 + 一次薄涂（平行长笔行扫，覆盖到 fg 边界，
每区域只涂一次），kind=`base_toned`、`layer=0`；**无 gradient、无逐笔颜色抖动、无局部取色**，
alpha=1.0、所有底笔同色。`--base-color-mode none` = 完全不涂底色（线稿/白纸最“人类”）。
legacy `single|quantized|source` 保留，但标注 **legacy，不推荐**——仅用于逐字节复现 v1.33/v1.34
历史产物（见各版 canonical 命令）。

| 参数 | 默认 | 含义 |
|---|---|---|
| `--base-color-mode {toned,none,single,quantized,source}` | **toned** | toned=纯色单次薄涂（新默认）；none=无底色；其余为 legacy |
| `--base-color` | **auto** | auto=整幅图像逐通道中位色（线稿≈白纸色）；也支持 `#RRGGBB` 或 `"r,g,b"`（0–1 或 0–255） |
| `--base-coat-angle` | **0.0** | 行扫角度（0=水平，90=垂直） |
| `--base-coat-spacing` | **0.0** | 行距 px；0=取 `--coarse`（report 写解析后实际值 `base_coat_spacing`） |
| `--base-coat-width-factor` | **1.04** | 笔宽 = pitch × factor；稠密画面 overlap 3.2–4.0%，稀疏 fg 自动收窄（report `base_coat_width_factor` 为实际值、`_requested` 为 CLI 值） |
| `--base-coat-jitter` | **0.5** | 中线沿笔方向抖动 px（不开缝） |

**正式绘制重心（新默认）**：`--mid-err-pct / --fine-err-pct / --pass4-err-pct` 默认从 82/72/60
下调为 **60/45/45**，Pass2/3/4 笔数占 97–99%，画面由正式层承担；不显式传旧 err-pct 时命令
行为随版本变化（显式传参的旧命令不受影响）。

**100k 硬上限（4GiB 交付契约）**：`--max-curves` 默认 **100000**。所有层与 detail 合并后若超限，
按 **texture-flat → pass4 → pass3 → pass2** 顺序做确定性均匀抽稀（base/detail 最后兜底），
抽稀后重渲染并记录 `capped_100k / cap_adjustments / pre_cap_strokes`。
实测：纯默认调用 **37,843 笔 ≤100k（capped_100k=false）**；默认 + `--pass4` **109,548 → 100,000**
（`cap_adjustments={"base_pass4":{"removed":9548,"frac":0.1332}}`，pass2 6,116 / pass3 31,663 全保留）。

**底色合规性实测（可量化）**：

- Sunflowers 1536 的 toned 底座 = **77 条长笔、单一纯色 `[0.812,0.769,0.263]`、颜色 std=0.0、
  无 gradient、栅格覆盖 100%、行带重叠 0%、>2 层 0%**（`work/underpaint/check_base.py` PASS）；
  只渲底色的前景颜色 std 0.0103（只剩抗锯齿边），边缘能量仅源图 1.7%。
- 合成实心画布 96–256px × angle 0/90 与 384×288：覆盖率 **1.0000**、overlap **3.19–4.00% ≤5%**、
  maxcnt=2、uncovered 行 0；report 字段与探针复算一致。
- `none`：`base_strokes=0`、`base_coverage/base_overlap_frac/base_overlap_canvas_frac=None`、
  `base_color_std=0.0`（不写 1.0）。
- 旧底色全部不合格（analyst 独立口径）：v1.34 source 底 12,544 色、重叠>1 99.9%、base-only
  颜色 std 0.154；v1.33 quantized 4,543 色、重叠 99.9%、std 0.140；线稿旧 source 底 3,987 色、
  std 0.297、重叠 93.7%——正是用户说的“多色草图底”。

**线稿演示（用户当前场景，`none`）**：输入 `work/input2/img_20261016.png`，work-res 1200 →
归一化笔触渲染 2400 高，**96,636 笔（0 底笔）**：**IoU 0.8492 / recall 0.9755 / precision 0.8677 /
d1 precision 0.9089 / PSNR 18.276 / SSIM 0.776**；旧 v1.34 source 底同图 **IoU 0.7369 /
precision 0.7574 / PSNR 14.437 / SSIM 0.6378**，且旧底 4,115 笔、颜色 std 0.2977（灰草图底）。
→ 白底不再有灰色草图，线条精度大幅提高。图：`work/underpaint/lineart_triple.png`、
`work/input2/lineart_v135_compare.png`、`work/underpaint_eval/fig_lineart_4panel.png`。

**推荐命令 A：线稿 / 白纸（base none + 正式层）**

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input work/input2/img_20261016.png --outdir <OUT> ^
  --work-res 1200 --base-color-mode none ^
  --coarse 12 --mid 6 --fine 3 --mid-err-pct 55 --fine-err-pct 40 ^
  --pass4 --pass4-err-pct 40 --pass4-spacing 3 ^
  --mid-jitter 0.004 --fine-jitter 0.006 ^
  --upper-color-mode source --upper-blur 0.8 --upper-gradient
# 96,636 笔（≤100k，余 3.4%）；IoU 0.8492 / PSNR 18.276 / SSIM 0.776
```

**Sunflowers toned 演示（1536，89,205 笔；质量代价如实）**：

| 指标 | v1.33 after | v1.34 final | v1.35 toned |
|---|---|---|---|
| 笔数（≤100k） | 81,010 | 99,929 | **89,205** |
| 底色笔 / 纯色 std / 覆盖 / 重叠 | 4,543 多色（std .17） | 62,929 源色 | **77 / 0.0 / 1.0 / 0.0** |
| 全幅 PSNR / SSIM | 23.0791 / 0.61572 | 24.3322 / 0.62533 | **21.7959 / 0.60797** |
| 内容区 PSNR / SSIM | 24.4302 / 0.53653 | 24.8173 / 0.54451 | **24.5205 / 0.51072** |
| A 区 PSNR | 24.0588 | 24.7211 | **24.3090** |
| A 区 flat_frac / blocks / run_y | .491 / 103 / 5.99 | .437 / 74 / 5.45 | **.578 / 51 / 6.73** |
| A 区 grad_x / uniq16 | .0156 / 183 | .0156 / 195 | **.0149 / 178** |

**全幅 PSNR 21.7959 不能与 v1.33 的 23.0791 直接横比**：偏低主因是正式层笔触越界污染了左右白边
（白边 mean 0.9777→0.9701/0.9637；左/右白边 PSNR 19.7→18.6、21.6→17.3），不是底色本身。
按 doc 内容区口径 Lead 复算 content **24.511 / 0.5351**（v1.33 24.430/0.5365、v1.34 24.817/0.5445）、
A 区 **24.309**（v1.33 24.0588）→ **画面主体质量没有下降、内容区反而略升**。白边过笔是 formal 笔
越界（后续可加 fg 裁剪），不作为本门槛；追求最大保真（含白边干净）请用 legacy 配方。

**推荐命令 B：油画 / 彩插（toned + 正式层 + 可选 texture-flat，用满 100k）**

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input training/input/Sunflowers_square.png ^
  --detail training/sunflowers/clean_r1/cleaned_strokes.json --work-res 1536 ^
  --coarse 20 --mid 10 --fine 5.5 --mid-err-pct 60 --fine-err-pct 45 ^
  --pass4 --pass4-err-pct 45 --pass4-spacing 7 ^
  --base-color-mode toned --base-color auto ^
  --mid-jitter 0.006 --fine-jitter 0.008 ^
  --upper-color-mode source --upper-blur 2.5 --upper-gradient ^
  --texture-flat --texture-flat-count 10700 --texture-flat-ref-std 0.015 ^
  --texture-flat-layer 19
# 99,905 笔（texture 10,700）；纯色底座 77 笔/覆盖 1.0/重叠 0
# content 24.5669/0.51407、A PSNR 24.321；A blocks 103→44、largest 0.447→0.276、
# flat .578→.509、uniq16 178→192（toned 单独时 A 区 banding 会回退，用 texture-flat 补）
# 画布色可显式指定：--base-color #C9A24B（不传则 auto=整幅中位色）
```

**legacy 复现**：v1.33 canonical（`--base-color-mode quantized --coarse 20 ... --gradient-gate off`）
与 v1.34 推荐命令（`--base-color-mode source --base-blur 4 --coarse 12 ... --texture-flat 37000`）
仍可**逐字节复现**历史产物（v1.33 PNG SHA `30CDCE74…EB9A01`、v1.34 SHA `69820EEF…`，
`work/underpaint/legacy/legacy_compat.json`）；只是不再是不传参数时的默认。默认行为变化仅两处：
不显式传 `--base-color-mode` → toned；不显式传 err-pct → 60/45/45。

**report 新增字段**：`base_color_mode / base_color / base_coat_angle / base_coat_spacing /
base_coat_width / base_coat_width_factor / base_coat_width_factor_requested / base_coat_jitter /
base_strokes / base_coverage / base_overlap_frac / base_overlap_canvas_frac / base_color_std /
base_gradient_strokes`；硬上限：`max_curves / capped_100k / cap_adjustments / pre_cap_strokes`。
旧字段全部保留。

**已知限制（如实）**：

- toned 配方全幅 PSNR 低于 legacy（21.80 vs 23.08）是设计原则的直接后果（底色不带图像信息），
  非 bug；按内容区口径不降、A 区略升。两者兼得需给正式层更多预算（>100k 曲线）或专门正式层细节 pass。
- **A 区 banding 回退**：toned 单独时最大连续平色块 6.4%→44.7%、flat_frac .491→.578
  （单色底 + 默认 err-pct 让 A 区停在平色块）→ 油画场景请叠加 `--texture-flat`（推荐命令 B：
  blocks 103→44、largest 0.447→0.276、flat .578→.509）。
- 稀疏主体（线稿）toned 的 fg 口径 overlap 偏高（res192 4.35%、res128 8.0%，行带重叠落在少量墨线上）：
  **线稿/稀疏主体推荐 `none`**；toned 在稀疏 fg 上“合法但不推荐”，硬门禁以稠密画面 + canvas 口径为准。
- `--base-color auto` 取整幅图像中位色：白留边多的图会偏亮，可用 `--base-color #RRGGBB` 指定画师想要的画布色。
- 行带是直线平行笔，非凸/多孔主体带内 fg 之间的空白也会被涂（toned 只保证覆盖主体，不精确沿轮廓裁剪）；
  线稿/镂空场景请用 `none`。
- 硬上限抽稀是均匀删减（不重新采样），被裁层会轻微稀疏、指标可能小幅下降；`gradient_by_pass`
  统计仍按生成期计数（被裁笔的梯度统计未回算），仅诊断用途。
- 白边过笔导致全幅 PSNR 不可横比（见上）；可考虑笔端点裁剪到 fg 或按内容区裁剪交付。

## 人类画师式画法（v1.36）

**用户批评**：“现在的画法还是从上到下扫描式地画了好几轮，这是人类画师会做的事情吗？
想想真人画师是怎么画画的。” v1.35 已把底色改成纯色单次薄涂，但**画法**仍是机器式：
底色是水平行扫长笔、正式层按轴对齐 tile 网格成行生成、笔序列按 y 排序 → 视频里呈“逐行扫描、
多轮重复铺”的打印机感。

**v1.35 扫描性基线（改前，analyst/test-harness 口径，`work/tests4/baseline_v135_order_metrics.json`、
`work/humanorder_eval/order_v135.json`）**：从头重画的《向日葵》99,683 笔中 **full_canvas_rounds=5**
（6 个 layer 中 5 个覆盖整幅 y/x）、**layer1/2/3 的 y-Spearman = +1.000（纯自上而下整轮扫描）**、
底色 77 条长笔全部水平（axis_frac=1.000、方向熵 0）、K=12 空间簇 `cluster_compactness_mean=0.085`
（同一区域被拆成 5 层反复画）、`region_run_frac=0.661`。→ 用户不满的核心是**铺底几何 + 绘制顺序**，
不是笔触方向（v1.35 方向已由结构张量保证 flow_align 0.839）。进度帧证据：
`work/humanorder_eval/progress_contact_sheet.png`（3% 仅顶部横带、7% 上半、12% 大半、20% 几乎全图）。

**人类画师式画法（v1.36 三件事）**：

1. **wash 铺底（`--base-coat-pattern wash`，新默认）**：方向多变宽笔薄涂（斜向/弧线/交叉，
   非水平行扫）；仍是单一纯色、一次薄涂、无 gradient/颜色抖动/局部取色（v1.35 底色原则不变）。
   `rows`=v1.35 旧行扫（legacy 兼容）。
2. **正式层笔触顺形体（`--flow-strokes on`，新默认）**：笔触方向取源图结构张量切向
   （低相干时取区域主方向/±抖动），不再按屏幕轴成行生成；report 新增
   `flow_orientation_frac`（笔方向与局部切向夹角 ≤20° 的比例）与 `axis_aligned_frac`
   （≤10° 于水平/垂直的比例）。
3. **按区域成组绘制（`--paint-order region`，新默认）**：按空间区域（K=8–16）分组，
   组内 block-in→fix→detail→texture 的层序，画完一块再下一块；不再“一层扫完全图再来一层”。
   `scan`=按 y 排序（旧式）；`legacy`=完全旧行为。report 新增
   `paint_order / paint_order_effective / order_y_spearman / order_adjacent_median_px /
   order_region_run_frac / region_count_used`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-coat-pattern {wash,rows}` | **wash** | 方向多变宽笔薄涂（非行扫）；`rows`=v1.35 旧行扫（legacy） |
| `--wash-angle FLOAT` | **-22.0** | wash 行带主方向（度）；带内方向随带变化，非屏幕水平 |
| `--flow-strokes {on,off}` | **on** | 正式层笔触顺源图结构张量切向 |
| `--paint-order {region,scan,legacy}` | **region** | 区域成组顺序；scan=按 y 排序；legacy=旧行为 |
| `--region-count INT` | **12** | 区域成组数量（空间 k-means 簇数，8–16 可用） |
| `--thick-dabs INT` | **0** | 厚涂短笔数量（0=关；油画 demo 用 14000） |
| `--clip-to-fg {auto,on,off}` | **auto** | 笔端点裁剪到前景：auto=v1.36 模式自动裁白纸（legacy 模式不裁，保证逐字节复现）；on/off 强制 |

**legacy 自动回退**：显式传 legacy 底色模式（`--base-color-mode single|quantized|source`）且
未显式传 flow/paint-order 时，自动 `flow=off` + `paint_order=legacy` + `pattern=rows`
→ v1.33/v1.34 旧命令逐字节复现；不显式传新参数时默认 wash/on/region。

**wash 铺底实测（真实栅格）**：合成 canvas 192×144（res 96–256）→ coverage **0.9926–0.9969**、
overlap_fg **3.25–4.80%**、max_count=2、单色 std=0；384×288 → coverage 0.9942–0.9972 /
overlap 3.42–4.14%；稀疏线稿 toned → 0.9919–0.9942 / 5.15–6.08%；Sunflowers 1536 底座
**71 笔**、coverage 1.0 / overlap **0.0713**（>768px analytic 口径，report `base_metrics_mode`）。

**与 v1.35 的关系**：v1.35 改“涂什么底”（纯色/单次薄涂/正式层承担），v1.36 改“怎么画/什么顺序”；
默认叠加后为 toned/wash 底 + 正式层 flow 顺形体 + region 顺序。100k cap、v1.33 渐变门、
v1.34 texture-flat、v1.35 hard-cap 字段与原则不变。

**report 新增字段**：`base_coat_pattern`、`wash_angle`、`base_metrics_mode`、`base_metrics_scale`、
`base_coverage`、`base_overlap_frac`、`base_strokes`（+ v1.35 的 base_only 系列保留）、
`flow_strokes`、`flow_orientation_frac`、`axis_aligned_frac`、`direction_strokes`、`paint_order`、
`paint_order_effective`、`order_y_spearman`、`order_adjacent_median_px`、`order_region_run_frac`、
`region_count_used`、`clip_to_fg`、`thick_dabs`、`thick_dab_strokes`、`texture_flat_strokes`；
旧 `gradient_strokes` / `capped_100k` / `cap_adjustments` 全保留。

**推荐命令（Sunflowers 从头重画，v1.36 默认 recipe）**：

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input training/input/Sunflowers_square.png ^
  --outdir <OUT> --work-res 1536 ^
  --coarse 20 --mid 11 --fine 5.5 --mid-err-pct 60 --fine-err-pct 45 ^
  --pass4 --pass4-err-pct 45 --pass4-spacing 7 ^
  --upper-color-mode source --upper-blur 2.5 --upper-gradient ^
  --texture-flat --texture-flat-count 20000 --texture-flat-ref-std 0.015 ^
  --thick-dabs 14000
# wash 默认、flow 默认 on、paint-order 默认 region、clip-to-fg 默认 auto
# 期望：100,000 笔 / content 24.1320（doc 协议，冻结基线 runE_dabs）/ full 25.2338 / 178.6 s
# 旧 v1.35 配方（行扫 + 旧顺序）：--base-coat-pattern rows --flow-strokes off --paint-order legacy
```

**legacy 复现**：显式 legacy 模式（`--base-color-mode quantized|source ...`）且不传
flow/paint-order 时自动回退 rows/off/legacy，v1.33 产物 PNG SHA `30CDCE74…EB9A01`、
v1.34 推荐 `69820EEF…` 逐字节不变；不显式传新参数时默认 wash/on/region。

**已知限制（如实）**：

- **质量 CONDITIONAL（口径修正）**：内容区 PSNR（doc 协议，冻结基线 **runE_dabs**）
  24.6184→**24.1320（−0.486 dB）**，**超 ≤0.4 允许线 0.086 dB**；square 口径 24.6875→24.1985
  （≈−0.49）。A 区 PSNR 24.625→**24.115**、A largest_flat **0.222→0.416**（回退）、A blocks
  64→65；B 28.149→27.4957（−0.65）。**注**：runE（无 thick_dabs）的 square 24.5367 / doc
  24.4675 是**错口径基线，勿再引用**。
- **簇内连续度 compactness 0.085→0.201** 仍低于目标 0.25：区域推进仍有“前沿”（多区域并行度
  未完全达到理想的人类“一块画完再下一块”），残余。
- `flow_align` 口径差异：fixer report `flow_orientation_frac=0.9086`，analyst 独立口径
  0.819（角度/切向定义差异）——两条都如实列出，不取单一口径宣称达标。
- **运行时间 178.6 s**（v1.35 runE_dabs 148.0 s = 107.9 + 40.1 含 dabs），未达 ≤120 s 理想线（5 次全场渲染为主）。
- `>768px` 的 base coverage/overlap 为 **analytic 几何口径**（report `base_metrics_mode=analytic`）；
  真实渲染器栅格断言只在 ≤384px。Sunflowers 1536 的 overlap 0.0713 是 analytic 值。
- wash 是对角主扫 + 带内弓形，不是任意交叉笔；带间/笔间自然重叠 3–7%，非 0。
- 白边处理：v1.36 默认 `--clip-to-fg auto` 裁掉越界白纸（全幅 PSNR 因此不可与 v1.35 直接横比）；
  legacy 模式不裁以保证逐字节。
- 质量/顺序评估基于 Sunflowers 单图 1536 实测；其它图/风格未广泛验证。

**实测（Sunflowers 从头重画，1536 原生，100,000 笔，178.6 s）**：

| 指标 | v1.35 fresh **runE_dabs**（冻结基线） | **v1.36 final** | 说明 |
|---|---|---|---|
| 笔数 / cap | 99,683 | **100,000**（≤100k，未触 cap，fill=0） | — |
| 底色笔 / pattern | 77 / rows | **71 / wash** | 非行扫 |
| 底色 coverage / overlap | 1.0 / 0.0 | **1.0 / 0.0713**（analytic；小图真实栅格 3.2–6.1%） | ≤15% |
| 全幅 PSNR（clip 后不可横比） | 21.845 | **25.2338** | clip-to-fg auto 修白边 |
| 内容区 PSNR（doc 协议，冻结基线） | **24.6184** | **24.1320** | **−0.486 dB：超 ≤0.4 线 0.086 → 质量 CONDITIONAL** |
| 内容区 PSNR（square 口径，参考） | 24.6875 | 24.1985 | ≈−0.49（仅供对照） |
| A 区 PSNR / blocks / largest_flat | 24.625 / 64 / 0.222 | **24.115 / 65 / 0.416** | A PSNR −0.51、largest_flat 回退 |
| B 区 PSNR | 28.149 | **27.4957** | B −0.65 |
| 白边 mean / white_frac | —（口径不同） | **0.9984 / 0.9946** | ≥0.998 ✅ |
| flow_orientation_frac（≤20°） | — | **0.9086** | 顺形体 |
| axis_aligned_frac（≤10°） | 底色旧 1.000 / 正式层旧 0.787 | **0.2663**（wash 底 0.183） | 明显下降 |
| order_y_spearman | 0.130（分层后 +1.0） | **0.197** | 无整轮 y 扫描 |
| order_region_run_frac / adjacent | — / 9.0px | **0.9993 / 190.65px** | 区域成组 |
| gradient / texture / dabs | — | **53,709 / 17,464 / 14,000** | — |
| 运行时间 | 148.0 s（107.9+40.1，含 dabs） | **178.6 s** | 未达 ≤120 s 理想线 |

**扫描性/顺序独立评估（analyst `work/humanorder_eval/`）**：洗刷底色 axis≤10° 由旧 **1.000 → 0.183**、
扫描层数（share≥2% 且 |y_spearman|≥0.8）由 **3 → 0**、layer 块连续性 **1.000 → 0.121**、
簇 span **0.974 → 0.548**、formal y-Spearman 0.201 → 判定 **顺序/扫描性 PASS**；
但簇内连续度 compactness **0.085→0.201** 仍低于目标 0.25（区域推进仍有前沿，残余，见上“已知限制”）。

## 剪影感知笔触生成（v1.37）

**用户反馈**：ComfyUI 新节点生成的 chibi 图（白底 421×412，work_res 1280）**轮廓边缘大量溢出笔触/毛刺**。
要求**用绘制算法修复，禁止后期修图**（不能“生成后把画布裁掉/描边/模糊”冒充修复）。

**根因（Lead 实测，chibi 1280×1253 / 38,737 笔）**：
1. 源图是 AI 插画，白底但有**柔和光晕/抗锯齿边**；`layered_base` 的 fg=`~all(rgb>0.945)` 把光晕也算
   前景（fg 43.6%，收紧后 40.4%），笔触在光晕区取样/绘制；
2. **笔宽越界**：**5,677 条**笔足迹越界 >0.5px、**3,223 条** >2px、**max 16.97px**（中位笔宽 7.0px、
   p90 8.7px）；
3. 既有 `clip_to_fg` 只在最终合成时把 fg 外像素置白（硬切），**光晕/fg 内的越界笔仍在**——视觉就是
   边缘毛刺/溢出；`clip_to_fg` 不是算法修复。

**方案（生成期几何裁剪，非后期）**：
1. **收紧剪影**：`sil = distance_transform_edt(fg) > shrink_px`（默认 1.0px，去掉光晕/飞边）；
   小图/薄特征保护：若 sil 面积 < 0.2×fg 则 fallback 到 fg 并 warn；
2. **生成期笔触几何裁剪**（在所有 pass/detail/dabs/texture 生成后、hard-cap 与 paint_order 之前，
   对每条非 `base_toned` 笔）：
   a. **端点回缩**：从两端循环去掉 `d_i < max(0.5, width_i/2 - 0.5)` 的端点（round cap 不出界），
      直到稳定；点数 <2 → 丢笔；
   b. **逐点笔宽钳制**：`width_i = min(width_i, 2·max(d_i - margin_px, 0))`；
   c. **过细笔丢弃**：钳制后 `max(width_i) < min_width_px` → 丢笔；
3. **渲染时剪影羽化合成**（属渲染合成，不是 later post-process）：`alpha = clip(1 - m/feather, 0, 1)`
   （`m=distance_transform_edt(~sil)`），`ren = ren·alpha + 1·(1-alpha)`，替代原来的硬切 fg。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--silhouette {auto,on,off}` | **auto** | 剪影感知开关；auto 仅 v1.36 风格启用，legacy 自动 off |
| `--silhouette-shrink-px FLOAT` | **1.0** | 从 fg 收缩得到绘制剪影（去光晕/飞边），px@work_res |
| `--silhouette-margin-px FLOAT` | **0.25** | 笔宽保持 `2·(dist - margin)` 内 |
| `--silhouette-min-width-px FLOAT` | **0.6** | 钳制后最大宽度低于此 → 丢笔 |
| `--silhouette-feather-px FLOAT` | **0.8** | 最终合成 alpha 羽化（渲染合成的一部分） |

**auto 语义（保 legacy 逐字节）**：仅当 `base_coat_pattern=wash` 且 `paint_order≠legacy` 且
`clip_to_fg≠off` 且白底占比 >2% 时启用；legacy 模式（`single/quantized/source` + `paint_order legacy`）
自动 off → v1.33/v1.34 旧命令逐字节不变。白底插画（如 chibi）即使不传参数也会自动触发；
全幅照片/背景不可分的图 auto 不触发（不硬套剪影）。

**report 新增字段**：`silhouette_mode`、`silhouette_on`、`silhouette_area_frac`、
`silhouette_fallback`、`silhouette_shrink_px`、`silhouette_margin_px`、
`silhouette_min_width_px`、`silhouette_feather_px`、`silhouette_clipped_strokes`、
`silhouette_points_trimmed`、`silhouette_dropped_strokes`、`silhouette_overshoot_before_max_px`、
`silhouette_overshoot_after_max_px`、`white_background_frac`、`fill_count`（旧字段全保留）。

**实测 before / after（chibi 1280×1253；同参考掩码，`work/silhouette/FIX_REPORT.md` §4）**：

| 指标 | before（v1.36 旧行为，`--silhouette off`） | after（v1.37 auto→on） | 说明 |
|---|---|---|---|
| 笔数 | 38,737 | **34,107** | clipped 3,893 / trimmed 2,013 点 / dropped 4,630 |
| 越界笔（**非底色**，n=38,683→34,053，vs fg） >0.5px | **5,636** | **0** | ✅ 生成期几何裁剪 |
| 越界笔（非底色） >2px | **3,182** | **0** | ✅ |
| 非底色 max overshoot | **10.00 px** | **0.25 px**（= margin 界限） | ✅ |
| 越界笔（全部笔） >0.5 / >2px / max | 5,677 / 3,223 / 16.97px | 41 / 41 / 16.97px | 41 条**全为 `base_toned`**（wash 54 笔设计上跳过裁剪，由剪影合成遮住） |
| 污染：painted outside **sil** | 13,325 px | **0 px** | ✅ 合成羽化后 sil 外无着色 |
| 污染：outside **char（max(rgb)≤0.88+opening）** | 56,821 px / **3.54%** | 40,976 px / **2.55%** | 残余为角色半透明抗锯齿边（char 38.49% < sil 42.63%，合理边缘） |
| PSNR（同参考 resize 口径） | 25.8273 | 25.3655（−0.46 dB） | 羽化让 1px 边缘变柔；report 内部口径 25.0158→**25.3636（+0.35dB）** |
| SSIM | 0.88795 | 0.88562（≈不变） | — |
| silhouette 指标 | — | on、area_frac **0.9768**、fallback false、report overshoot **9.999→0.0px** | auto 触发（白底 56.36%） |
| 耗时 | 49.5 s | **63.4 s**（wall 68.8s） | 裁剪+羽化 +14s |
| fill / 100k / capped | 0 / 未触 / false | **0 / 34,107 / false** | — |

**说明**：`base_toned` 的 54 条 wash 底色笔按规格**跳过几何裁剪**（最终剪影合成会遮住；report 的
`overshoot_after_max` 只统计被裁剪笔，故为 0.0）。确切数字与独立口径见
`work/silhouette/FIX_REPORT.md`、`work/silhouette_eval/ANALYSIS.md`；图：
`work/silhouette/edge_before_after_4x.png`、`chibi_fix_R2048.png`、`contact_sheet_fix.png`。

**节点/独立验证（补充）**：ComfyUI 节点重建 `.fbs` **34,107 笔 / 151.3MB**、2048 渲染无溢出
（Lead 验证）；analyst 独立口径（chibi 640）off >0.5px/>2px **4,816/2,018/max 9.996px** →
on **0/0/0.25px**（232px AA 残差）、PSNR 640 on **+0.84dB**；合成图 off 36 条 >2px → on 0；
`--silhouette off` 重建的 render PNG SHA 与旧版**完全相同**（无旁路改动）；legacy v1.33
`30CDCE74…` / v1.34 `69820EEF…` 逐字节一致。

**推荐命令（白底插画 / chibi）**：

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input work/chibi_clip/source.png --outdir <OUT> ^
  --work-res 1280 --coarse 20 --mid 12 --fine 6 ^
  --mid-err-pct 60 --fine-err-pct 45 ^
  --pass4 --pass4-err-pct 45 --pass4-spacing 6 ^
  --base-color-mode toned --base-color auto ^
  --upper-color-mode source --upper-blur 2.0 --upper-gradient ^
  --texture-flat --texture-flat-count 12000 --texture-flat-ref-std 0.015 ^
  --clip-to-fg auto --silhouette on
# 白底插画可显式 --silhouette on；保持默认 auto 时满足条件也会自动启用
# 期望：34,107 笔 / 非 base 笔越界 0/0（max 0.25px）/ PSNR 25.3655 / 63.4 s
# 人像照片/全幅背景：无需剪影（auto 不触发），不要强行 on
```

**注意事项**：人像照片/全幅背景无需剪影（auto 不触发，或显式 `--silhouette off`）；
chibi/白底插画可显式 `--silhouette on`；`clip_to_fg` 仍保留用于白边处理，但 v1.37 起
边缘溢出的修复来自上面的生成期几何裁剪，而非硬切。

**已知限制**：

- **依赖背景可分**：剪影来自 fg 收缩，白底/干净背景效果好；全幅照片/复杂背景 auto 不触发
  （白底 >2% 条件不满足），如强行 `--silhouette on` 可能把主体边界裁错。
- **`base_toned` 底色笔不裁剪**（按规格跳过；54 条 wash 笔自身仍可能越界，但最终剪影合成会遮住）。
  若未来需要底色也裁剪，可去掉 skip 并同样钳制（成本 +54 笔）。
- **细特征可能变细/被削**：shrink=1.0 为本图实测（鲸尾/鳍完整未丢特征）；若用户觉得细鳍/鲸尾
  被削薄，可 `--silhouette-shrink-px 0.5`；越大越紧。
- **羽化让边缘变柔**：feather=0.8px 是视觉毛刺消失的原因之一，同参考口径 PSNR −0.46dB 属预期；
  需更锐可 `--silhouette-feather-px 0.4`（会略增边缘碎屑）。
- **确定角色 mask 外仍有 2.55% 被涂**（before 3.54%）：残余是角色本身的半透明抗锯齿边
  （char 38.49% < sil 42.63%，属合理边缘），不是越界笔——非底笔越界已为 0。
- **耗时 +14s**（49.5→63.4s）：34k 笔 Python 级几何裁剪 + EDT，直接开销约 2–3s，余为同机波动。
- 剪影几何裁剪不影响纯笔画风格/`fill_count=0`；legacy 模式自动 off，v1.33/v1.34 逐字节不变。

## GPU 渲染（v1.38）

**用户痛点**：“渲染/输出太慢”。CPU `fdrender --quality exact` 实测：chibi `.fdraw`（38,737 笔）
**1024 = 219 s**、**4096 ≈ 21 min**；视频/高分辨率更慢，训练/评估的渲染也占大头。要求 **GPU 加速**。

**方案（OpenGL 离屏 GLSL 胶囊光栅化）**：`scripts/gpu_render.py`（在 **ComfyUI python** 下运行，
该解释器自带 PyOpenGL 3.1.10 + glfw 2.10.0、桌面 GL 4.6 可用）：

1. 读入 `.fdraw`（render.json）/`.fbs`/layered_base `strokes` JSON；
2. 采样**复用** `fbs_core.render.sample_stroke`（不重写采样；JSON 加载/采样仍是 CPU）；
3. 每段构造 capsule 几何（2 三角形覆盖 bbox；gradient 用逐段两端色近似），VBO 上传；
4. GLSL 330 片元着色器：`cov = clamp(r + 0.5 - dist, 0, 1) * alpha`，按绘制顺序混合到
   supersample 倍 FBO；
5. `glReadPixels` → 翻转/SS 下采样 → PNG；输出计时 JSON
   （`load / sample / geometry / gpu / readback / total`）。
   支持 alpha<1 与 gradient（逐段近似）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--fdraw` / `--render-json` / `--fbs` / `--strokes` | — | 输入四选一（`.fdraw` 优先） |
| `--out` | — | 输出 PNG 路径 |
| `--resolution INT` | — | 长边分辨率（1024/2048/4096） |
| `--supersample {1,2,3}` | **2** | SSAA 倍数（光栅化前） |
| `--background` | 白 | 背景色 |
| `--device {gl,cpu}` | **gl** | `cpu` = CPU 参考路径；无 GL/PyOpenGL 时 `gl` 退出码 **3**（供 fallback） |
| `--width-scale FLOAT` | 1.0 | 笔宽缩放 |

**ComfyUI 节点（RenderFdraw）**：默认**自动 GPU**（用节点进程的 `sys.executable` 调 `gpu_render.py`），
失败/无 GL/异常 → **自动回退 CPU fdrender exact**；`FDP_GPU_RENDER=0` 强制 CPU；
**workflow JSON / 节点 widget 数量不变**；节点包 `PACKAGE_VERSION` → **3.0.1**。

**实测（chibi `.fdraw`；ComfyUI python + RTX 4070 Ti SUPER）**：

| 分辨率 | **GPU 总耗时** | GPU 阶段拆分（load / sample / geometry / gpu / readback） | CPU `fdrender exact` | 加速比 |
|---|---|---|---|---|
| 1024 | **14.2 s**（内部 13.49） | 3.64 / 8.27 / 0.50 / 0.11 / 0.34 | **219 s** | **≈15.4×** ✅ |
| 2048 | **15.1 s**（14.36） | 3.54 / 7.88 / 0.90 / 0.32 / 1.15 | **581.6 s** | **≈38.5×** ✅ |
| 4096 | **23.6 s**（22.70） | 3.78 / 10.13 / 1.81 / 1.20 / 5.14 | ≈1260 s（~21 min） | **≈53×** |
| 512（冒烟） | 15.4 s | 4.32 / 9.99 / 0.43 / 0.06 / 0.14 | — | — |

（不同次测量/并发负载，参考区间：**1024 14.2–16.8 s / 2048 15.1–19.4 s / 4096 21.9–27.6 s**；
analyst 独立复跑 14.5 / 16.5 / 23.3 s。）

- 分辨率 = `[长边, 另一维]`（chibi 为 1024×1002 / 2048×2004 / 4096×4008；`--supersample 2`）。
- **GPU 光栅化本身只要 0.1–1.4 s**；剩余耗时主要是 **CPU load+sample 约 12 s**
  （JSON 加载 + 复用 `fbs_core.sample_stroke` 的 CPU 采样，约占总时间 80%）——后续可缓存采样几何/
  并行化进一步提速。
- 4096 显存：16F FBO 8192×8016×RGBA16F ≈ **525 MB** + 实例缓冲 7.89M×44B ≈ **347 MB** + 读回
  float ≈1 GB 暂态，16 GB 显存无压力。

**该 GPU 路径的输入/输出**：`.fdraw`（render.json，zip 直接读）/`.fbs`/`render.json`/原始
`--strokes`（含 gradient/alpha）；`--device cpu` 时 `.fdraw/.fbs` 走 `fbs_core.render`、
`--strokes` 走 `hatch_gen._render_strokes`。输出 PNG 与计时 JSON（`*_s` 字段：
`load_s/sample_s/geometry_s/gl_init_s/gpu_s/readback_s/total_s`）。`fourier-brushstroke` 的
`fbs.py render --device {auto,gl,cpu}` 可复用（**默认 cpu**，原 render 输出字节不变）。

**推荐命令（高分辨率出图，GPU）**：

```powershell
# 在 ComfyUI python 下运行（该解释器自带 PyOpenGL 3.1.10 + glfw 2.10.0 / 桌面 GL 4.6）
python skills/fourier-multistroke-training/scripts/gpu_render.py ^
  --fdraw <x.fdraw> --out <out_4096.png> --resolution 4096 ^
  --supersample 2 --device gl --timing-json <timing.json>
# 期望：4096 ≈23.6 s、2048 ≈15.1 s、1024 ≈14.2 s（不同次测量波动见上表区间）
# 无 PyOpenGL/桌面 GL 时 --device gl 会 exit code 3（供节点自动回退），不会崩
# CPU exact 参考仍可用：fdrender --quality exact（慢，legacy/canonical 参考路径）
# ComfyUI：无需改 workflow；RenderFdraw 自动 GPU，FDP_GPU_RENDER=0 强制 CPU
```

**质量与回退**：

- **GPU vs CPU exact（同分辨率）**：1024 **PSNR 40.219 / SSIM 0.99566**；
  2048 **41.9643 / 0.99623（verifier 原生 CPU exact 实测）**（fixer 早期代理口径 40.542/0.99458）；
  4096 **44.109 / 0.99724**（门槛 >32dB，远高于）。
- 与源图相比差异 **≤0.11 dB / ≤0.004**；差图差异仅在**笔触边缘 AA/覆盖**（1024 边缘环带 PSNR
  31.76dB、单通道 max 差 0.30，属覆盖率核差异），平坦区差 ≈0（实测均值差 0.15/255）；两版 vs 源图 PSNR ≈21.13/21.24
  （1024）、20.85/20.88（4096），对“像源图”的程度无实质差异。
- 6 处 4× 视觉对照（发丝 / 鲸尾棘 / 左鳍 / 脸颊渐变 / 头顶蕾丝 / 蝴蝶结）无新锯齿、无色带、无漏笔。
- gradient 为**逐段颜色近似**（非逐点颜色曲线），上述质量指标已覆盖该近似。
- **确定性**：独立复跑 GPU PNG 与 fixer 产物像素 0 差异（SHA 相同）；三份 CPU exact PNG 像素 0 差异。
- **节点回退**：`RenderFdraw` GPU 成功（1024 端到端 17.1 s，含解释器启动；gpu_render 内部 16.2 s）；
  `FDP_GPU_RENDER=0` → CPU exact 回退成功（256px 99.8s，与旧逻辑一致）；模拟无 GL →
  gpu_render exit 3 → 自动回退；**workflow JSON / widget 数不变**；`PACKAGE_VERSION=3.0.1`；
  repo 副本与部署副本 `nodes.py` hash 完全一致（`8FE41719564CDAA5…`）。
- **GPU 退出码语义**（节点据此自动回退 CPU）：**3** = 无 PyOpenGL/桌面 GL；
  **5** = 文档含 alpha<0.999 笔（GPU 逐段合成对 alpha<1 不精确，CPU 整笔 band 才正确）；
  **4** = 含 `region_fill` 填充。当前 skill 全部流水线输出 **alpha=1**，chibi/正常出图不受影响；
  alpha<1 或 region_fill 的文档走 CPU exact 即可。

**已知限制**：

- **需要 PyOpenGL + glfw + 桌面 GL 上下文**（ComfyUI python 自带；workbuddy python 无 PyOpenGL）。
  headless/服务器/无 GL 环境：`gpu_render.py` **exit code 3** 且 stderr 明确 → 节点自动回退
  CPU exact；不会崩溃。
- **4096 显存/纹理**：16GB 显存实测通过；更大分辨率未验证（受 GL 最大纹理/显存约束）。
- **gradient 逐段近似**：与 CPU exact 的逐点颜色相比有可见但微小的边缘差异（质量指标如上）。
- **2048 原生 CPU exact 参考已补测**（verifier）：PSNR **41.9643 / SSIM 0.99623**；fixer 早期
  代理口径 40.542/0.99458（4096 exact 下采样）保留说明，两者均已写入文档。
- **仅保证 alpha≈1 文档**：alpha<0.999 → **exit 5** + 节点回退 CPU（CPU 整笔 band 只合成一次，
  alpha<1 时才正确）；`region_fill` 文档 → exit 4 回退。当前流水线 alpha=1，不受影响。
- **非 round 笔形按 round 近似**（soft/marker/texture 核）；report `non_round_shapes` 会列出
  （chibi 全 round）。
- **CPU exact 仍是 legacy/canonical 参考**：GPU 是快速近似路径；逐字节 legacy 产物仍由 CPU 路径产生
  （GPU 输出不是 bit-exact，alpha=1 时边缘差 ≤0.42/255）。
- 训练/评估的渲染（`hgr._render_strokes`）与**视频渲染未改**（视频未来可缓存采样，本次未做）；
  本次只加 GPU 出图路径。
- 无新增第三方依赖（只用已有 PyOpenGL/glfw/torch/numpy/Pillow）。
- 当前瓶颈已转移：CPU load+sample 12.7–14 s 占大头，GPU 光栅化 0.1–1.4 s 已不是瓶颈。

## 产物

```
run_dir/
├── rounds/round_XXXX_R1408.png      每轮成果图
├── rounds/round_XXXX_report.json    每轮参数/指标/耗时
├── training_log.jsonl
├── state.json                        断点续训状态
├── best_R1408.png / best_strokes.json
└── summary.md
final_dir/
├── *_R1536.fbs.json                  最终傅里叶表示（可复渲染）
├── *_R1536.png / _R2048.png / _R4096.png
├── *.fdraw                           便携容器（validate 通过）
├── compile_report.json / final_report.json / verification.json
└── final_compare_sheet.png
```

## 参数上限与实测（交付口径）

| 项 | 4GiB 高精度上限 | 旧 2GiB 档（历史） |
|---|---|---|
| Build R | ≤1536（可 2048 实验） | 1536 |
| ε | 0.08 | 0.08 |
| N_max | ≤32768（默认 16384） | 16384 |
| curve_tol | 0.002 | 0.002 |
| max_curves | ≤100000 | 40000 |
| 渲染 | ≤4096 | 2048/4096 |
| `.fbs` 体积 | **≤4GiB** | <1GiB |
| `.fdraw` render.json | **≤4GiB** | ≤1GiB |
| `.fdraw` 解压总大小 | **≤4GiB** | ≤2GiB |
| 填色元素 | 0 | 0 |

性能参考（16 核 CPU、单进程）：1024 训练 ~50s/轮；1408 训练 ~95–110s/轮；
30k 笔 4GiB 编译 ~6–10min；2048 fast 渲染 ~2–3min。细节层越多，编译线性变慢。
历史 2GiB 档 `final_lean_fix_r21`：2048 PSNR 18.277 / SSIM 0.5336；
官方 1536 四维：IoU 0.8416、召回 0.9272、精度 0.9011、`wrong_pct=0`。
Star Night 4GiB 细节档：30,273 笔，2048 PSNR 19.28 / SSIM 0.398，`.fbs` ~1.2GB。

## 测试（`tests/`，v1.33 起）

`skills/fourier-multistroke-training/tests/` 是纯标准库 `unittest` 套件（**不依赖 pytest**），
覆盖 data 结构/算法不变量、32 个脚本的 CLI 与文档契约、渐变安全门 / texture-flat / v1.35 底色 /
v1.36 画法 / v1.37 剪影感知 / v1.38 GPU 渲染 缺陷回归、真实图慢测试。

```powershell
Set-Location skills/fourier-multistroke-training
python -m unittest discover -s tests -v     # 142 用例，约 80–100 s；慢测试/GPU 用例默认 skip
# 连慢测试一起跑（真实图 256px，3 个用例）：
$env:RUN_SLOW = "1"; python -m unittest discover -s tests -v; Remove-Item Env:RUN_SLOW
# GPU 子集（需 ComfyUI python，10 条 GPU 用例；workbuddy python 无 PyOpenGL 会 skip）：
$env:RUN_GPU = "1"; & "<ComfyUI-python>" -m unittest tests.test_gpu_render -v; Remove-Item Env:RUN_GPU
```

- 模块与用例数：`test_layered_base.py` 18 / `test_contracts.py` 26 /
  `test_gradient_safety.py` 7 / `test_texture_flat.py` 17（v1.34 新增）/
  `test_base_coat.py` 19（v1.35 新增）/ `test_human_paint.py` 18（v1.36 新增）/
  `test_silhouette.py` 15（v1.37 新增）/ `test_gpu_render.py` 19（v1.38 新增；10 条 GPU 用例
  默认 skip）/ `test_slow_real_image.py` 3（仅 `RUN_SLOW=1`）；详见 `tests/README.md`。
- 覆盖：`save_strokes`/`load_strokes` 字段与精度、palette/tile 不变量、渐变门严格契约
  （含平滑 ramp 不允许绕过）、texture-flat 平坦区判定/ref-std 门控/预算截断/确定性、
  **v1.35 底色**（toned 纯色单次薄涂栅格门禁、none、颜色解析、angle、legacy、100k 硬上限）、
  **v1.36 人类画法**（wash 非行扫/覆盖/重叠、flow on/off 方向性、region vs scan 顺序指标、
  legacy 回退）、**v1.37 剪影**（on 时非底笔足迹不出 sil、off 时确实越界、auto 语义、
  legacy 自动 off、report 字段）、**v1.38 GPU**（合成 strokes GPU vs CPU 等价 PSNR/SSIM≥32/0.98、
  尺寸/白底一致、无 PyOpenGL exit 3、alpha<1 exit 5、`--device cpu` 等价、计时 `*_s` 字段）、
  report 缺陷字段齐全且自洽、32 脚本 `--help` 与 `py_compile`、
  SKILL.md 目录树声明与磁盘双向一致、严格 UTF-8、脚本无写死绝对路径、
  `--detail` 丢弃 `base_pass*/fix_*/soft_*`。
- 缺陷捕获证据：修复前旧快照（`work/backup/skill_before_v1.33`）跑同一套件为 **4 failed**
  —— 2 个渐变门缺陷（68 条 Δ>0.12、report 缺 6 个字段）+ 2 个 `--detail` 过滤缺陷，
  见 `work/tests/baseline_result.md`；修复后全绿。v1.34 texture-flat / v1.35 底色 /
  v1.36 人类画法 / v1.37 剪影 / v1.38 GPU 用例基线分别见 `work/tests2/`、`work/tests3/`、
  `work/tests4/`、`work/tests5/`、`work/tests6/`。
- 当前实现：**142 tests OK（skipped=13 = 3 个 `RUN_SLOW` 慢测试 + 10 条 GPU 用例）** / 80.0 s；
  ComfyUI python（有 PyOpenGL/GL）下 `RUN_GPU=1` 的 GPU 子集 **19 tests / 0 failed / 1 skipped /
  19.7 s**（合成 strokes GPU vs CPU：**PSNR 62.29 / SSIM 1.0000**）。
- 测试在 skill 根目录运行；路径全部由 `__file__` 推导，不写死机器绝对路径。

## 已知问题与后续（v1.38）

**v1.38 GPU 渲染的残余（如实）**：

- **需要 PyOpenGL + glfw + 桌面 GL 上下文**（ComfyUI python 自带 PyOpenGL 3.1.10/glfw 2.10.0、
  桌面 GL 4.6；workbuddy python 无 PyOpenGL）。headless/服务器/无 GL 环境：`gpu_render.py
  --device gl` **exit code 3** + 明确 stderr → 节点自动回退 CPU exact，不崩溃。
  **不能宣称“任何环境可用”**。
- **4096 显存/纹理**：16GB 显存实测通过；更大分辨率未验证（受 GL 最大纹理/显存约束）。
- **gradient 逐段近似**：与 CPU exact 的逐点颜色曲线相比有边缘微差（质量指标 40dB 级已覆盖）。
- **CPU exact 仍是 legacy/canonical 参考**：GPU 是快速近似路径；逐字节 legacy 产物仍由 CPU 产生。
- **训练/评估渲染与视频渲染未改**：本次只加 GPU 出图路径；视频未来可缓存采样（未做）。
- 当前瓶颈：CPU load+sample **12.7–14 s**（JSON 加载 + `fbs_core.sample_stroke` CPU 采样）
  占大头，GPU 光栅化 0.1–1.4 s 已不是瓶颈（后续可 GPU 采样/异步回读）。
- 无新增第三方依赖（只用已有 PyOpenGL/glfw/torch/numpy/Pillow）。

**v1.37 剪影感知的残余（历史记录，如实）**：

- **依赖背景可分**：剪影来自 fg 收缩，白底/干净背景效果好；全幅照片/复杂背景 auto 不触发
  （白底 >2% 条件不满足），强行 `--silhouette on` 有裁错主体边界风险。
- **`base_toned` 54 条 wash 底色笔不参与几何裁剪**（按规格跳过；由最终剪影合成遮住）。
  全部笔口径的越界笔 41 条、max 16.97px 全部来自它们；非底色笔越界已为 **0/0（max 0.25px）**。
- **细特征可能变细/被削**：shrink=1.0 为本图实测（鲸尾/鳍完整）；觉得削薄可
  `--silhouette-shrink-px 0.5`，越大越紧。
- **羽化让边缘变柔**：feather=0.8px（毛刺消失原因之一），同参考口径 PSNR **−0.46dB** 属预期；
  更锐可 `--silhouette-feather-px 0.4`（略增边缘碎屑）。report 内部口径 PSNR 反而 +0.35dB。
- **确定角色 mask 外残余 2.55%**（before 3.54%）为角色半透明抗锯齿边（char 38.49% < sil 42.63%），
  不是越界笔。
- **耗时 +14s**（49.5→63.4s）：34k 笔几何裁剪 + EDT 额外开销。
- 剪影不改变纯笔画风格/`fill_count=0`；legacy 自动 off，v1.33/v1.34 逐字节不变。
- 后续方向（未实现、未验证）：对 `base_toned` 也做几何裁剪（+54 笔成本）、按锐度需求自适应
  feather、非白底场景的剪影估计。

**v1.36 人类画师式画法的残余（历史记录，如实）**：

- **顺序/扫描性 PASS、但未完全“人类化”**：wash 底色 axis≤10° 1.000→**0.183**、扫描层数 **3→0**、
  layer 块连续性 1.000→**0.121**、簇 span 0.974→**0.548**、formal y-Spearman **0.201**；
  但簇内连续度 compactness 0.085→**0.201** 仍低于目标 0.25——区域推进仍有“前沿”，
  多区域并行度未完全达到理想的一块画完再下一块。
- **质量 CONDITIONAL（口径修正）**：冻结基线 **runE_dabs** 的 content（doc 协议）24.6184→
  **24.1320（−0.486 dB）**，**超 ≤0.4 允许线 0.086 dB**；square 口径 24.6875→24.1985。
  A 区 PSNR 24.625→**24.115**（−0.51）、A largest_flat **0.222→0.416**（回退）、A blocks
  64→**65**；B 28.149→27.4957（−0.65）。注：runE（无 dabs）square 24.5367 / doc 24.4675
  为**错口径基线，勿再引用**。
- **运行时间 178.6 s**（v1.35 runE_dabs 148.0 s = 107.9 + 40.1 含 dabs）未达 ≤120 s 理想线。
- **口径限制**：>768px 的 base coverage/overlap 为 **analytic 几何口径**（report
  `base_metrics_mode`），真实栅格断言只在 ≤384px；`flow_align` 两口径（report
  `flow_orientation_frac=0.9086` vs analyst 独立口径 0.819，角度/切向定义差异）。
- **wash 几何**：对角主扫（`--wash-angle -22`）+ 带内弓形，不是任意交叉笔；带间自然重叠 3–7%。
- **白边**：v1.36 默认 `--clip-to-fg auto` 裁掉越界白纸 → 全幅 PSNR（25.2338）不可与 v1.35
  直接横比；legacy 模式不裁以保证逐字节。
- 质量/顺序评估基于 Sunflowers 单图 1536 实测；其它图/风格未广泛验证。
- 后续方向（未实现、未验证）：提高区域并行/完成度（compactness）、减少全场渲染次数
  （局部缓存）把耗时压到 ≤120 s、把 base 栅格统计扩展到 >768px 的真实渲染。

**v1.35 人类画师式底色（保留，原则不变）**：底色纯色/单区域单次薄涂/正式层承担画面的原则不变；
v1.36 只改“怎么画/什么顺序”。v1.35 周期的代价与残余（历史记录，如实）：

- **Sunflowers 全幅 PSNR 低于 legacy**（toned 21.7959 vs v1.33 23.0791）：设计原则的直接后果
  （底色不带图像信息），非 bug；**全幅不可与 v1.33 横比**——偏低主因是正式层笔越界污染左右白边
  （白边 mean 0.9777→0.9701/0.9637；左/右白边 PSNR 19.7→18.6、21.6→17.3）。按 doc 内容区口径
  Lead 复算 24.511/0.5351（v1.33 24.430/0.5365）→ 主体不降、内容区略升。
- **A 区 banding 回退**：toned 单独时最大连续平色块 6.4%→44.7%、flat_frac .491→.578（单色底 +
  默认 err-pct 让 A 区停在平色块）→ 油画场景叠加 `--texture-flat`（推荐命令 B：blocks 103→44、
  largest .447→.276、flat .578→.509）。
- **白边过笔**：正式层笔端点越界到白边（后续可裁剪到 fg 或按内容区交付），本版不作为门槛。
- **稀疏主体 toned 的 fg overlap 偏高**（线稿 res192 4.35%、res128 8.0%）→ 线稿/稀疏主体推荐
  `--base-color-mode none`；toned 在稀疏 fg 上“合法但不推荐”。
- `--base-color auto` 取整幅中位色，白留边多的图会偏亮 → 可显式 `--base-color #RRGGBB`。
- 行带几何对非凸/多孔主体会涂带内空白（只保证覆盖主体，不精确沿轮廓裁剪）→ 线稿/镂空用 `none`。
- 100k 硬上限抽稀为均匀删减（不重新采样），被裁层轻微稀疏、`gradient_by_pass` 未回算（仅诊断）；
  线稿 none 96,636 笔余量仅 3.4%。

**红圈 A“颜色分层”（v1.34 记录，历史）**：texture-flat recipe 后**视觉上明显减轻**
（平色带被打散成有方向细纹、seedhead 从平滑泥块变回细纹；A PSNR +0.662、blocks −29、
run_y −0.54），但仍有残余：blocks 74 > MUST 65、`largest_flat_fraction` 0.064→0.112（唯一反向
指标：残余最大连续平色块在画面中部 render bbox (735,735)-(881,967)，参考图该处有中等局部纹理
ref 3×3 lstd p50≈0.027，预算限制下纹理笔不足所致）、grad_x / uniq16 / flat_frac 三项
SHOULD 未达、纹理判词“偏糊”（texture_ratio 0.615 vs 可接受线 0.62）、4× 可见规则“梳状”细纹。
根本瓶颈：100k 笔上限下 A 区只能拿 2,320 条纹理笔。改进方向（未实现、未验证）：提高 100k
曲线/合同上限、区域自适应密度、用更细 detail 笔替换部分 pass3。详见「细纹理层（v1.34）」。

其余为 v1.33 周期遗留、本轮未修复（`work/qa/ISSUES.md` 有复现记录，不要当作已解决）：

- **P2** QA-05/06：RGBA 输入 alpha 被丢弃；竖幅 `--work-res` 仍按宽度 resize；
- **P2** QA-08：`layered_base.py` 数值参数无校验（非法 work-res / k / tile / spacing 崩溃或静默退化）；
- **P2** QA-09：R / max_curves / 体积上限在 SKILL.md、PIPELINE.md、ALGORITHM.md 与
  `verify_delivery.py` 之间口径不一致；本 SKILL 以 **4GiB 档**为准，其余待统一；
- **P3** QA-12/13/14/15/17/18/19：16.9x 编号乱序、轮次数字过期、错误信息不友好、
  `fill_count` 分支覆盖不全、`run_train` 边界参数静默、`compile_best` docstring 过期、
  `pipeline.py` 相对路径受 cwd 影响（QA-19 为读码推断，未实测）；
- v1.34 的候选/通用性风险：去 pass4 对依赖 pass4 补平色阶的图可能掉色准；纹理笔依赖参考图
  纹理、风格化输入应关闭；这些是 recipe 的使用条件，不是缺陷。

## 检查表

- [ ] `fill_count == 0`（`verify_delivery.py` 会判）
- [ ] 训练每轮都有 PNG + report（`audit_rounds.py`）
- [ ] `.fdraw` `validate` 通过，render.json / 解压总大小 <4GiB
- [ ] 最终 PNG 与源图同分辨率比较；PSNR/SSIM 不跨分辨率横比
- [ ] 精修后若改变了笔数，重新 compile + pack + verify
- [ ] 不要把 single-curve 的线稿结果当成本 skill 的交付

## 参考

- `references/PIPELINE.md` — 本工作区完整训练/精修/编译说明与历史结果
- `references/ALGORITHM.md` — 分层、评分、坐标、容器字段、参数边界
- `references/METRICS.md` — `fbs.py eval` 官方四维度量
- `references/RESULTS.md` — 历史训练/交付档位/性能数据
- `references/SINGLE_CURVE_TRIAL.md` — **已否决**的单曲线尝试与根因，避免重复踩坑
- `references/VERIFICATION.md` / `ROUNDS_AUDIT.md` — 自动校验与每轮审计样例
- 底层格式/渲染：`fourier-brushstroke` skill；`.fdraw` 打包：`fourier-prompt` skill

## 版本记录

- **v1.38-gpu-render（2026-10-25）**：**GPU 渲染**——新增 `scripts/gpu_render.py`
  （PyOpenGL+glfw 离屏 FBO + GLSL 胶囊距离场光栅化；采样复用 `fbs_core.render.sample_stroke`；
  `--device gl|cpu`（无 GL 时 gl 退出码 **3**）、`--supersample`（默认 2）、`--timing-json`；
  输入 `.fdraw/.fbs/strokes JSON`；gradient 逐段近似）。ComfyUI `RenderFdraw` 默认自动 GPU，
  失败/无 GL/超时自动回退 CPU fdrender exact（`FDP_GPU_RENDER=0` 强制 CPU）；workflow JSON/
  widget 数不变，`PACKAGE_VERSION` → **3.0.1**。实测（chibi `.fdraw` 38,737 笔）：GPU **1024
  14.2s / 2048 15.1s / 4096 23.6s** vs CPU exact **219s / 581.6s / ≈1260s（21min）** →
  **≈15.4× / ≈38.5× / ≈53×**（GPU 光栅化本身仅 0.1–1.2s，瓶颈转移为 CPU load+sample ~80%）；
  时间参考区间 14.2–16.8 / 15.1–19.4 / 21.9–27.6s（不同次测量/并发）；质量 GPU vs CPU exact
  **PSNR 40.219/SSIM 0.99566（1024）、41.9643/0.99623（2048 原生；代理 40.542/0.99458）、
  44.109/0.99724（4096）**（门槛 >32dB），与源图差异 ≤0.11dB/0.004，差图仅笔触边缘 AA、
  平坦区差 ≈0（均值差 0.15/255）；6 处 4× 视觉无新锯齿/漏笔。节点 GPU 1024 端到端 17.1s（内部 16.2s）；无 GL exit 3 自动回退。
  限制（GL 依赖、4096 显存、gradient 逐段近似、CPU exact 仍是 legacy 参考、训练/评估/视频未改）
  见「已知问题与后续（v1.38）」。新增 `tests/test_gpu_render.py`（GPU 默认 skip，`RUN_GPU=1`）。
  交付包：`dist/fourier-multistroke-training-skill-v1.38-gpu-render.zip`。
- **v1.37-silhouette-aware（2026-10-25）**：**剪影感知笔触生成（生成期几何裁剪，非后期修图）**——
  新增 `--silhouette {auto,on,off}`（默认 auto）+ `--silhouette-shrink-px 1.0` /
  `--silhouette-margin-px 0.25` / `--silhouette-min-width-px 0.6` / `--silhouette-feather-px 0.8`；
  所有 pass/detail/dabs/texture 生成后、hard-cap/paint_order 前对非 `base_toned` 笔做
  端点回缩 + 逐点笔宽钳制 + 过细丢笔，最终合成用剪影羽化（替代硬切 fg）。auto 仅 v1.36 风格
  （wash + 非 legacy order + clip≠off + 白底>2%）启用；legacy 自动 off，v1.33/v1.34 逐字节不变。
  chibi 1280 实测：非底笔越界 >0.5px **5,636→0**、>2px **3,182→0**、max **10.0→0.25px**、
  painted outside sil **13,325→0px**、确定角色外 **3.54%→2.55%**（残余 AA 边）；PSNR 同参考
  25.8273→25.3655（−0.46dB 羽化）、report 内部口径 +0.35dB；笔数 38,737→34,107（clipped 3,893 /
  trimmed 2,013 / dropped 4,630）；耗时 49.5→63.4s；fill=0、≤100k。新增 `tests/test_silhouette.py`
  15 用例（总 121）。残余（base_toned 不裁剪、背景可分依赖、细特征可调、+14s）见
  「已知问题与后续（v1.37）」。交付包：
  `dist/fourier-multistroke-training-skill-v1.37-silhouette-aware.zip`。
- **v1.36-human-paint-order（2026-10-24）**：**人类画师式画法（非扫描）**——新增默认
  `--base-coat-pattern wash`（方向多变宽笔薄涂，`--wash-angle -22`；`rows`=v1.35 行扫 legacy）、
  `--flow-strokes on`（正式层笔触顺源图结构张量切向；report `flow_orientation_frac`/
  `axis_aligned_frac`/`direction_strokes`）、`--paint-order region`（区域 k-means 成组绘制，
  区内 block-in→detail→texture；`scan`/`legacy` 可选；`--region-count 12`）、`--thick-dabs`
  （默认 0）、`--clip-to-fg {auto,on,off}`（默认 auto：v1.36 模式裁白纸，legacy 不裁）。
  legacy 自动回退：显式 legacy 底色模式且未传 flow/paint-order → rows/off/legacy，v1.33/v1.34
  旧命令逐字节复现。Sunflowers 1536 原生实测（100,000 笔 / 178.6 s）：wash 71 笔纯色
  coverage 1.0/overlap 0.0713（analytic）、content（doc 协议）24.6184→**24.1320**
  （**−0.486dB**，超 ≤0.4 线 0.086 → 质量 **CONDITIONAL**）、square 24.1985、
  full 21.845→**25.2338**（白边修复）、A 24.115/blocks 65/largest_flat 0.416、B 27.4957；
  flow_orientation 0.9086、
  axis 0.2663、order_y_spearman 0.197、region_run 0.9993；analyst 独立验收：wash 底 axis
  1.000→0.183、扫描层数 3→0、层块连续性 1.000→0.121、簇 span 0.974→0.548（顺序/扫描性 PASS；
  compactness 0.201 仍低于 0.25）。残余（质量 −0.486dB 超线 0.086 → CONDITIONAL、
  A largest_flat 0.222→0.416、A blocks 64→65、178.6s、analytic 口径、
  flow_align 两口径）已如实写入「已知问题与后续（v1.36）」。新增 `tests/test_human_paint.py`。
  交付包：`dist/fourier-multistroke-training-skill-v1.36-human-paint-order.zip`。
- **v1.35-human-underpainting（2026-10-24）**：**人类画师式底色**——pass1 默认改为
  `--base-color-mode toned`（单一纯色 + 单次薄涂，kind=`base_toned`/layer=0，无 gradient/
  颜色抖动/局部取色），新增 `--base-color auto|#RRGGBB|"r,g,b"`、`--base-coat-angle 0.0` /
  `--base-coat-spacing 0.0(→coarse)` / `--base-coat-width-factor 1.04` / `--base-coat-jitter 0.5`；
  `--base-color-mode none` = 不涂底色（线稿/白纸推荐）；legacy `single|quantized|source` 保留但
  仅用于逐字节复现 v1.33/v1.34 历史产物。正式绘制默认 `--mid/fine/pass4-err-pct` **60/45/45**
  （原 82/72/60）；新增 `--max-curves 100000` 硬上限（超限按 texture→pass4→pass3→pass2 均匀抽稀，
  report `capped_100k/cap_adjustments/pre_cap_strokes`）。实测：toned 底座 77 笔 1 色 std=0/
  覆盖 100%/重叠 0；线稿 `none` 96,636 笔 IoU 0.8492/precision 0.8677/PSNR 18.276/SSIM 0.776
  （旧 source 底 IoU 0.7369/precision 0.7574/PSNR 14.437）；Sunflowers toned 89,205 笔
  内容区 24.5205/0.51072（v1.33 24.4302/0.53653；**全幅 21.7959 不可横比**，白边过笔所致）、
  toned+texture 99,905 笔 content 24.5669/0.51407、A blocks 103→44。默认行为变化仅两处：
  底色默认 toned、err-pct 默认 60/45/45；旧命令显式传参时逐字节不变。新增
  `tests/test_base_coat.py`；残余代价（全幅 PSNR/白边过笔/A 区 banding 回退等）见
  「已知问题与后续（v1.35）」。交付包：
  `dist/fourier-multistroke-training-skill-v1.35-human-underpainting.zip`。
- **v1.34-color-banding（2026-10-24）**：`layered_base.py` 新增 **opt-in 细纹理层**
  `--texture-flat*`（默认关）+ pass1 底色软化参数（`--base-color-mode source` /
  `--base-blur 4.0` / `--base-gradient` / `--base-width-factor 1.5` /
  `--base-length-factor 2.0`）；新增推荐 canonical recipe（source/blur4/coarse12、去掉 pass4
  腾 ~24k 预算、texture count 37000、ref-std 0.015、layer 19）。Sunflowers 1536 实测：
  A 区 PSNR 24.0588→24.7211、平色块 103→74、run_y 5.99→5.45；全幅 23.0791→24.3322、
  内容 24.4302→24.8173、B 26.9247→28.0935；99,929 笔 / 105.6 s。
  不传 `--texture-flat` 时与 v1.33 逐字节一致（md5 `5CEDAAC084511CA7`）；旧 v1.33 canonical
  仍可复现历史产物。新增 `tests/test_texture_flat.py` 17 用例（总 63 用例）。
  残余（blocks/largest_flat_frac/grad_x/uniq16/flat_frac 未全达标、
  纹理偏糊/梳状细纹）见「已知问题与后续（v1.34）」。
  交付包：`dist/fourier-multistroke-training-skill-v1.34-color-banding.zip`。
- **v1.33-gradient-safe（2026-10-24）**：`layered_base.py` 新增默认开启的**上层渐变安全门**
  （`--gradient-gate on` / `--gradient-max-delta 0.12` / `--gradient-edge-delta 0.10` /
  `--gradient-edge-pct 100.0`）+ report 6 个缺陷指标；`--detail` 改为前缀过滤
  （丢弃 `base_pass*/fix_*/soft_*`）；`verify_delivery.py` 缺 `.fdraw` 不再假阳性、
  `.dsh/skills` junction 下交付脚本可 import；新增 `tests/`（43 用例）与 canonical 1536 复现命令。
  canonical 1536 前后：81,815 → 81,010 笔，全幅 PSNR 23.1883 → 23.1059（−0.082 dB），
  输出渐变笔端点 Δ>0.12 由 5,329 → **0**；红圈 B 局部减轻、红圈 A 未根治（已知限制，见上节）。
  交付包：`dist/fourier-multistroke-training-skill-v1.33-gradient-safe.zip`。

