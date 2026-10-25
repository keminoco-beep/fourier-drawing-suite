# ENGINEERING — 工程构建思路 + 给接手的 AI 的完整说明（suite v1.0.2）

> 读者：想改代码/继续开发的工程师，或**接手的 AI agent**。
> 目标：读完这一份就能定位入口、理解不变量、跑通测试、安全地加功能。
> 更细的 skill 文档在 `workspace/skills/fourier-multistroke-training/SKILL.md` 与
> `references/PIPELINE.md` / `REFERENCES/ALGORITHM.md`（本文件是跨组件的总览）。

---

## 1. 目标与设计原则（先懂这些，再改代码）

1. **纯笔画（fills=0）**：画面完全由有宽度的笔画构成，`fill_count` 必须为 0；有填色即视为违约。
2. **人类画师式画法**（v1.36 起）：不是“从上到下扫描式多轮铺”，而是
   **wash 铺底 → 大笔顺形体（flow）→ 按区域成组完成（region）**；顺序指标是硬验收项。
3. **生成期修复，禁后期修图**（v1.37 起）：轮廓溢出必须靠**笔触几何裁剪**（生成阶段）+
   渲染时剪影合成解决，**禁止**“渲染后裁画布/描边/模糊/涂白”冒充修复。
4. **本地优先、可移植**（v1.0.0）：节点不硬编码本机绝对路径；workspace/python 靠 env/config/布局探测；
   默认用 ComfyUI 自带 python；无网络也能装（pip wheel 自备）。
5. **契约与上限**：R≤1536（Build）、ε=0.08、N_max≤32768、max_curves≤100000、
   `.fbs`/`.fdraw` 各 ≤4GiB；超限走硬上限抽稀（`capped_100k`/`cap_adjustments` 必须如实记录）。
6. **兼容优先**：CLI 参数只增不减；legacy 命令逐字节复现（v1.33 `30CDCE74…`、v1.34 `69820EEF…`）；
   workflow JSON 与节点 widget 数不变。
7. **如实度量**：文档/报告中的数字必须来自实测（含区间与口径），不得夸大“无损/任何环境可用”。

---

## 2. 总架构与数据流

> 作品示例（《向日葵》纯笔画复刻，v1.36 人类画师式笔画、约 10 万笔、2048 长边）：
> [查看示例图](docs/images/sunflowers_v136_content_R2048.png)（也见 `README.md`「作品示例」）。

```
┌────────────────────────────── ComfyUI（宿主，用户已装） ──────────────────────────────┐
│  workflows/*.json   ──>  custom_nodes/ComfyUI-FDP-T2I（节点包 v3.1.0，portable config）  │
│    WorkflowStart ──select──> BuildFromImageV136 ──> PackFdraw ──> RenderFdraw ──> Preview │
│        │(lazy Krea2)                 │                    │              │               │
│        └─ Krea2 local / API ─┐       │                    │              │               │
└──────────────────────────────┼───────┼────────────────────┼──────────────┼───────────────┘
                               │       │ 子进程（ComfyUI python）          │
                               ▼       ▼                    ▼              ▼
                     IMAGE ──> layered_base.py ──> strokes.json ──> build_strokes_fbs.py
                                  (wash/flow/region/          │            │
                                   silhouette/texture/dabs)   │            ▼
                                                              │      .fbs.json（傅里叶表示）
                                                              │            │ pack_fdraw.py
                                                              │            ▼
                                                              │        .fdraw（便携容器）
                                                              │            │
                                                              │        fdrender（CPU exact） / gpu_render（OpenGL）
                                                              │            ▼
                                                              └────>  PNG（任意分辨率）
                                                                           │ fdraw_to_video.py
                                                                           ▼
                                                                      MP4（傅里叶作画视频）
```

**流水线阶段（v1.36+ 默认）**：

1. **背景/剪影**：`fg=~all(rgb>0.945)`（v1.37 剪影再收紧 `sil=edt(fg)>shrink`）；
2. **底色 wash**：方向多变宽笔薄涂（`--base-coat-pattern wash`，默认 `--wash-angle -22`），
   `base_toned`/layer0、纯色、目标 coverage≈1、overlap 5–15%；
3. **正式层**：pass2/pass3/pass4（误差百分位分块）+ texture-flat（平坦区细笔）+ thick-dabs（可选厚涂）；
   笔触方向由源图**结构张量切向**决定（`--flow-strokes on`）；
4. **区域顺序**：k-means（K=12）区域 → 区域内 block-in→细节→texture → 区域间 Hilbert/最近邻推进
   （`--paint-order region`）；report 记录 `order_y_spearman/region_run/adjacent`；
5. **剪影几何裁剪**（v1.37，hard-cap 与 paint_order **之前**）：端点回缩→逐点宽钳制→过细丢笔；
6. **hard-cap**：`--max-curves 100000` 超限按 texture→pass4→pass3→pass2→base 抽稀；
7. **编译**：`build_strokes_fbs.py` 把每条笔触拟合为傅里叶系数（`.fbs`）；
8. **打包**：`pack_fdraw.py` 生成 `.fdraw`（zip：render.json + meta），供渲染/视频复用；
9. **渲染**：CPU `fdrender --quality exact`（legacy/canonical 参考）或 GPU `gpu_render.py`（OpenGL
   GLSL，快 15–53×，非 bit-exact）；最终合成可选剪影羽化（v1.37）；
10. **视频**：`fdraw_to_video.py`（傅里叶圆逐圈作画，CPU，慢）。

---

## 3. 关键算法与公式

### 3.1 逐笔傅里叶表示（`.fbs`）

每条笔触是一条（或多条）闭合曲线，用复傅里叶级数表示：

```
p(t) = Σ_{k=-N..N} c_k · e^{i·k·ω·t},   t ∈ [0,1),  ω = 2π
c_k  = 采样点离散 FFT 后取前 N 项（N = N_max 或按 ε 自适应）
```

- 渲染即 `p(t)` 采样 + 圆形笔刷沿折线扫掠（capsule）。
- 笔宽/颜色/alpha 是沿 t 的逐点曲线（`widths[]`、`colors[]`，或整笔常数）。
- 坐标：归一化到 `[0,1]`（长边=1），渲染时乘目标分辨率；`.fbs` 的 `canvas` 记录长边。
- 编译参数：`--epsilon 0.08`（拟合容差）、`--nmax 32768`、`--max-curves 100000`。

### 3.2 Capsule 覆盖率（渲染核心）

对每段笔触（A→B，半径 r），像素到线段距离 `d`：

```
coverage = clamp(min(0.5, d + r) − max(−0.5, d − r), 0, 1)     # 1px 抗锯齿
color    = mix(c0, c1, t_proj) ; alpha = mix(a0, a1, t_proj)
out      = color·alpha·coverage + out·(1 − alpha·coverage)      # 按绘制顺序 alpha blend
```

- CPU：`hatch_gen._render_strokes`（整笔 band 并集只合成一次）+ `fdrender --quality exact`；
- GPU：`gpu_render.py` 每段 1 个 instanced quad，片段着色器算上述覆盖率，RGBA16F FBO（SSAA=2）
  → `glReadPixels` → 翻转/下采样 → PNG；计时字段 `load_s/sample_s/geometry_s/gl_init_s/gpu_s/readback_s/total_s`。

### 3.3 人类画师式画法（v1.36）

- **wash 铺底**：沿 `--wash-angle` 的行带内做弓形宽笔，带内方向随位置变化；非水平行扫
  （顺序指标 wash 底色 axis≤10° 从 1.000 → 0.183）。
- **flow 顺形体**：结构张量 `J = Σ w·∇I·∇Iᵀ` 的切向 = 主特征向量的垂直方向；
  正式层笔触方向取 `flow_orientation_frac`（≤20° 内占比）。
- **region 顺序**：像素特征（位置+颜色）k-means → 区域簇；区域间按 Hilbert/最近邻推进，
  每个区域内按 block-in（大笔）→ detail（细笔）→ texture 完成；指标
  `order_y_spearman`（应≈0）、`order_region_run_frac`（应≈1）、`order_adjacent_median_px`（区域跳转）。
- 质量对照（冻结基线 runE_dabs）：content doc 24.6184 → 24.1320（**−0.486 dB**，CONDITIONAL）。

### 3.4 剪影感知（v1.37）

- 剪影：`sil = distance_transform_edt(fg) > shrink_px`（默认 1.0；`sil.mean() < 0.2·fg.mean()` 时
  fallback 到 fg 并 warn）；
- 生成期裁剪（对非 `base_toned` 笔）：
  1. **端点回缩**：从两端循环去掉 `d_i < max(0.5, width_i/2 − 0.5)` 的端点（round cap 不出界），
     `<2` 点丢笔；
  2. **逐点宽钳制**：`width_i ← min(width_i, 2·max(d_i − margin_px, 0))`；
  3. **过细丢笔**：`max(width_i) < min_width_px` → 丢；
- 渲染合成：`m = edt(~sil)`，`alpha = clip(1 − m/feather_px, 0, 1)`，
  `ren = ren·alpha + 1·(1−alpha)`（渲染合成的一部分，不是后期修图）；
- 指标：`overshoot = width/2 − d`，report `overshoot_before/after_max_px`、`silhouette_clipped_strokes` 等。
- 实测（chibi 1280）：非底色笔越界 >0.5px **5,636→0**、>2px **3,182→0**、max **10.0→0.25 px**。

### 3.5 渐变安全门（v1.33，保留）

渐变笔端点色差 `Δ = max|color_end − color_start|`；`Δ>0.12` 或跨边 ≥0.1 的候选被拒
（`gradient_rejected_delta/edge/oob`），避免跨色边界糊斑。

### 3.6 关键指标定义（文档/测试共用口径）

| 指标 | 定义 |
|---|---|
| PSNR / SSIM | 与参考图（源图/CPU exact）逐像素；`content` 口径=裁掉白边后的内容区 |
| `fill_count` | 渲染中填充多边形计数，必须为 **0** |
| `flow_orientation_frac` | 笔触方向与结构张量切向夹角 ≤20° 的笔占比 |
| `axis_aligned_frac` | 与 0°/90° 夹角 ≤10° 的笔占比（越低越“人画”） |
| `order_y_spearman` | 笔序列 y 坐标的 Spearman 相关（≈0 才好；扫描式接近 +1） |
| `order_region_run_frac` | 相邻笔属于同一区域的连续比例（越高越成组） |
| overshoot | `width/2 − dist_sil`，>0.5px / >2px 计越界笔 |
| GPU 质量 | GPU vs CPU exact 同分辨率 PSNR/SSIM；差异应在笔触边缘 AA |

---

## 4. 数据格式

### 4.1 `strokes.json`（layered_base 输出，人可读）

```json
[{"points": [[x,y],...], "widths": [w,...], "color": [r,g,b], "alpha": 1.0,
  "kind": "base_toned|oil|texture|dab|...", "layer": 0, "gradient": [[c0],[c1]]?}]
```
- `points` 归一化 [0,1]（乘长边）；`widths` 归一化；`color` [0,1]；`gradient` 可选两端色。
- 坐标/单位/精度要点：`x,y∈[0,1]`；`widths` 相对长边；保留 6 位小数；UTF-8。

### 4.2 `.fbs.json`（傅里叶表示，v2.0）

关键字段：`version`、`mode`（`per_stroke`/`one_stroke`）、`canvas`（长边像素）、`strokes[]`，
每笔含 `fourier`（系数 c_k、N、ε）、`width_curve`、`color`、`layer`、`stats`。
- 上限：`N_max≤32768`、`max_curves≤100000`、文件 ≤4GiB。
- 校验：`verify_delivery.py` / `fbs.py validate`。

### 4.3 `.fdraw`（便携容器）

zip 包内主要 `render.json`（+ 可选 `meta.json`）；`render.json` 是归一化笔画 + 渲染参数（含画法/剪影/report
所需字段）。`fdraw_cli validate` 校验；`fdrender` 渲染；`gpu_render.py` 可直接读。
- 上限：render.json ≤4GiB；解压总大小 ≤4GiB；alpha 约定：当前流水线输出 alpha=1。

### 4.4 report JSON（每次运行）

`layered_base_report.json` / node `*_report.json` / `*_timing.json`：参数、笔数、耗时、质量、
剪影/画法/report 字段（缺字段会被测试契约判失败）。

---

## 5. 模块地图

### 5.1 skill `skills/fourier-multistroke-training`（32 scripts，核心）

| 脚本 | 职责 |
|---|---|
| `analyze_input.py` | 输入分析（尺寸/色彩/前景占比/风格） |
| `layered_base.py` | **主入口**：底色/正式层/texture/dabs/silhouette/paint_order/report（v1.33–1.38 全部画法） |
| `hatch_gen.py` | 纯笔触渲染器（`_render_strokes`，CPU 逐笔 capsule） |
| `hybrid_gen.py` / `hybrid_gen2.py` / `opaque_base.py` / `flow_tile_base.py` | 历史/对照生成器 |
| `base_orientation_fix.py` / `detail_aware_flow.py` / `human_workflow.py` / `coarse_to_fine.py` | 画法实验/修复辅助 |
| `soft_glaze.py` / `soft_atmosphere.py` / `gradient_swath.py` | 柔光/渐变层实验 |
| `thick_dabs.py` / `texture_flat`（在 layered_base 内） | 厚涂短笔 / 细纹理层 |
| `clean_strokes.py` / `prune_strokes.py` / `sparse_precision.py` | 笔画清理/剪枝/少笔版 |
| `calibrate_color.py` | 颜色校准 |
| `build_strokes_fbs.py` | strokes → `.fbs`（傅里叶编译） |
| `pack_fdraw.py` | `.fbs` → `.fdraw` + validate |
| `compile_best.py` | `.fbs` → PNG（旧路径） |
| `gpu_render.py` | **GPU 渲染**（fdraw/fbs/strokes → PNG，OpenGL） |
| `fdraw_to_video.py` | `.fdraw` → 作画视频（CPU） |
| `verify_delivery.py` | 容器/精度/fill 数自动校验 + SHA |
| `pipeline.py` / `run_train.py` / `refine_*.py` / `make_progress.py` / `make_video_circles.py` / `audit_rounds.py` / `fdraw_to_video.py` | 训练循环/精修/进度/视频/审计 |
| `fbs.py`（在 fourier-brushstroke） | `.fbs` CLI（build/render/validate，`render --device auto|gl|cpu`） |

### 5.2 fourier-brushstroke（位图→中轴笔画/Fourier）

- `scripts/fbs_core/`：`.fbs` 读写、`render.sample_stroke`（**GPU/CPU 共用采样器**）、validate；
- `scripts/fbs.py`：CLI（build/render/validate/onestroke/video）；
- `scripts/make_onestroke_process_video.py`：一笔画视频；`run_acceptance.py`：验收；
- assets/testcases 为小样例（`assets/out` 是本机演示输出，**发布包已剔除**）。

### 5.3 fourier-prompt（FDSL 规划/编译/`.fdraw`）

- `fourier_prompt/`：FDSL v2.1 schema/编译/修复/brush/llm tools/compat；
- CLI：`fourier_prompt.cli`、`fdraw_cli`、`fdrender_cli`；
- `comfyui/ComfyUI-FDP-T2I`：节点的 repo 源副本（与部署副本内容一致）；
- `assets/fonts`（NotoSansSC 子集 + OFL）、`assets/brush`（自测笔刷数据）。

### 5.4 ComfyUI 节点包（8 个节点）

`FDPT2IWorkflowStart`（模式/lazy 输入）、`FDPT2IBuildFromImage`、`FDPT2IBuildFromImageV136`（v1.36 画法）、
`FDPT2IPackFdraw`、`FDPT2IRenderFdraw`（GPU→CPU fallback）、`FDPT2IFdrawPath`、`FDPT2IRunDir`、
`FDPT2IFourierVideo`（视频 → 调 `<workspace>/poc/t2i/fdraw_fourier_video.py`；该脚本用
`__file__` 向上 2 级定位 workspace，自行把 `workspace/fourier-prompt` 与
`workspace/fourier-brushstroke/scripts` 加入 `sys.path`；节点 `_env()` 同时注入
`FOURIER_WS`/`PYTHONPATH`，任意 cwd 可用）。

### 5.5 工作流（5 个）

- `FDP_v136_Local_NoAI_UI.json`：图生图（无 AI）；
- `FDP_v136_T2I_Krea2_Local_UI.json`：本地 Krea2（UNET/CLIP/VAE/LoRA/KSampler）；
- `FDP_v136_T2I_Krea2_API_UI.json`：Krea2 API；
- `FDP_T2I_Krea2_fdraw_UI.json`（旧版保留）；`FDP_fdraw_to_fourier_video_UI.json`（视频）。

---

## 6. ComfyUI 节点数据流 / 懒加载 / 降级语义

```
WorkflowStart(source_mode, krea2_image←KSampler/VAEDecode lazy, manual_image←LoadImage lazy)
   └─(按 source_mode 只取一个输入)─> BuildFromImageV136(image, run_dir, work_res, resolution,
                                        quality, texture_flat, thick_dabs, clip_to_fg, paint_order)
   └─> PackFdraw(fbs_path, run_dir) ─> RenderFdraw(fdraw_path, resolution)
   └─> FourierVideo(fdraw_path, size, circles, duration_s)
```

- **lazy 输入**：`FDPT2IWorkflowStart` 的 `krea2_image`/`manual_image` 是 `lazy=True`；
  选“图生图”时 Krea2 分支不会被求值（**分支上不要挂输出节点**，否则 ComfyUI 仍会执行）。
- **GPU→CPU fallback**：`RenderFdraw` 先以 `sys.executable` 子进程调 `gpu_render.py`；
  成功（rc=0 且 PNG 存在）→ 读图；否则（异常/超时/**rc=3 无 GL**/**rc=5 alpha<1**/**rc=4 region_fill**）
  → 走原 `fdrender_cli --quality exact`。`FDP_GPU_RENDER=0` 强制 CPU。
- **产物目录**：`<ComfyUI>/output/fdp_t2i_runs/<时间戳>_<mode>/`，字段见 USER_GUIDE §7。
- **workflow/widget 不变**：RenderFdraw 只有 `fdraw_path/run_dir/resolution` 三个 widget。

---

## 7. 配置与环境

| 项 | 解析顺序 |
|---|---|
| workspace | `FDP_WORKSPACE` env → 节点目录 `fdp_config.json` 的 `workspace` → 发布包布局自动探测 → 可操作报错 |
| python | `FDP_PYTHON` env → `fdp_config.json` 的 `python` → `sys.executable`（ComfyUI 运行时） |
| GPU | 默认自动；`FDP_GPU_RENDER=0` 强制 CPU |
| 依赖 | `release/requirements-{core,gpu,comfyui}.txt`；`release/check_env.py` 自检（不联网） |
| 安装/卸载 | `release/install.py --comfyui <dir> [--workspace ...] [--python ...] [--dry-run]`；`uninstall.py` |
| GPU 兼容（v1.38.1） | Windows 下子进程自动清除 `PYOPENGL_PLATFORM`；`FDP_GPU_RENDER=0` 强制 CPU；
  输出 `*_gpu.png`/`*_gpu_timing.json` 表示 GPU 成功，`*_exact.png` 表示回退 |

**退出码约定（gpu_render.py，节点据此 fallback）**：`0` 成功；`2` 输入错误；`3` 无 PyOpenGL/GL；
`4` 不支持的文档（region_fill 填充）；`5` alpha<0.999 文档。

---

## 8. 测试与验证

### 8.1 单元/契约测试（skill 根目录）

```powershell
cd workspace\skills\fourier-multistroke-training
python -m unittest discover -s tests
# 完整仓库：Ran 146 tests, OK（workbuddy skip=16 = 3 RUN_SLOW + 13 GPU；ComfyUI python skip=17）
# 发布包干净环境：Ran 143 / skipped 18（workbuddy）/ 19（ComfyUI）——真实图测试类依赖 work/、training/（包内不含）
$env:RUN_SLOW="1"; python -m unittest discover -s tests; Remove-Item Env:RUN_SLOW   # 慢测试 3/3
# GPU 子集（ComfyUI python，有 PyOpenGL/GL）：23 tests / 0 failed / 1 skipped（含 egl env 回归）
$env:RUN_GPU="1"; & "<ComfyUI python>" -m unittest tests.test_gpu_render -v; Remove-Item Env:RUN_GPU
```

模块：`test_layered_base` 18 / `test_contracts` 26 / `test_gradient_safety` 7 / `test_texture_flat` 17 /
`test_base_coat` 19 / `test_human_paint` 18 / `test_silhouette` 15 / `test_gpu_render` 23 /
`test_slow_real_image` 3（仅 `RUN_SLOW`）。

### 8.2 legacy 逐字节回归

- v1.33 canonical（渐变门 off）1536 PNG SHA **`30CDCE74…EB9A01`**；
- v1.34 推荐 PNG **`69820EEF…`**；
- 由 `test_gradient_safety.py` / `test_gpu_render.py` 断言（自动回退 legacy 参数）。

### 8.3 测试产物（本包 `test_artifacts/`）

| 目录 | 内容 |
|---|---|
| `unit/` | v1.33–v1.38 各版本单元测试基线（`baseline_result_*.md` 等）+ 运行命令/结果 |
| `verdicts/` | v1.35–v1.38 独立验收 VERDICT（含 PASS/CONDITIONAL 与逐项证据） |
| `metrics/` | `sunflowers_metrics.json`、`silhouette_metrics.json`、`gpu_quality.json`、`gpu_*_timing.json` |
| `images/` | 精选对比图（剪影边缘 4×、chibi 前后、GPU vs CPU 4×、lineart、Sunflowers） |
| `release_test/` | 跨环境发布兼容测试报告（task-40：重定位/空格中文路径/安装模拟/全链路/降级） |
| `reproduce.md` | 每个产物的复现命令 |

验收口径：GPU vs CPU 质量 PSNR≥32/SSIM≥0.98（实测 40.2/42.0/44.1 dB）；剪影越界笔≈0；
146 tests 全绿（发布包干净环境 Ran 143 / skipped 18（workbuddy）/19（ComfyUI），属预期）；legacy SHA 不变；发布包 zip ≤120MB 且无绝对路径。

---

## 9. 给接手的 AI：清单

### 9.1 入口文件（按你想做的事）

| 想改 | 入口 |
|---|---|
| 画法/底色/flow/区域顺序/剪影 | `workspace/skills/fourier-multistroke-training/scripts/layered_base.py`（单文件 ~2000+ 行；看 `main` 里参数解析顺序与各 pass 函数） |
| GPU 渲染 | `scripts/gpu_render.py`（`GLRenderer`/shader/readback）；CPU 采样在 `fbs_core.render.sample_stroke` |
| 渲染质量/合成 | `hatch_gen._render_strokes`（CPU）、`gpu_render`（GPU）、`fdrender_cli`（exact） |
| 傅里叶编译/容器 | `build_strokes_fbs.py`、`pack_fdraw.py`、`fbs_core`、`fourier_prompt.fdraw_cli` |
| 节点行为/fallback/config | `fourier-prompt/comfyui/ComfyUI-FDP-T2I/nodes.py`（`_workspace_paths`/`workspace_python()`/`_run`） |
| 工作流 | `fourier-prompt/comfyui/ComfyUI-FDP-T2I/workflows/*.json`（发布包 `comfyui/workflows/`） |
| 安装/自检 | `release/install.py`、`release/check_env.py`、`release/uninstall.py` |
| 文档 | skill `SKILL.md`/`references/`；本包 `USER_GUIDE.md`/`ENGINEERING.md` |

### 9.2 不可破坏的不变量（改代码前必读）

1. `fill_count == 0`；无填色/无后期修图（裁剪必须在笔触生成阶段）。
2. legacy 逐字节：v1.33 `30CDCE74…` / v1.34 `69820EEF…`（默认 legacy 参数时）。
3. workflow JSON 不变、节点 widget 数不变；`PACKAGE_VERSION` 3.x（≥3.0.1）。
4. report 字段名/口径不能改名（测试契约会失败）；新增字段只能追加。
5. CLI 参数只增不减；默认值变更必须同步：`tests/*` 的 FROZEN 表、`SKILL.md`、`references/`、本文件。
6. `gpu_render.py` 退出码 3/4/5 语义（节点 fallback 依赖）；无 GL 必须优雅退出，不崩。
7. 文档数字必须与实测一致（区间/口径如实标注），不得写“无损/任何环境可用”。
8. 不改 `tests/` 断言来“通过”；测试是交付契约。

### 9.3 跑测试 / 小链路

```powershell
# 1) 单元（最快，~80–95s）
cd workspace\skills\fourier-multistroke-training && python -m unittest discover -s tests
# 2) 小链路（不碰 AI，<3min；ComfyUI python 或 workbuddy python 均可，需 numpy/scipy/PIL）
python workspace\skills\fourier-multistroke-training\scripts\layered_base.py `
  --input examples\chibi\source.png --outdir %TEMP%\fdp_smoke --work-res 256 `
  --base-color-mode toned --clip-to-fg auto --silhouette auto
python workspace\skills\fourier-multistroke-training\scripts\gpu_render.py `
  --strokes %TEMP%\fdp_smoke\layered_base_strokes.json --out %TEMP%\fdp_smoke\gpu.png --resolution 256
# 3) 发布自检
python release\check_env.py --comfyui <ComfyUI 目录>
```

### 9.4 如何加功能（安全流程）

1. 先读 `references/PIPELINE.md` 对应章节 + `layered_base.py` 的 `main()` 参数解析与 pass 顺序；
2. 加 CLI 参数（默认值不改变旧行为）→ 加 report 字段（追加）→ 写测试（`tests/`，FROZEN 表同步）；
3. 跑全量 + `RUN_SLOW`（涉及 GPU 再跑 `RUN_GPU`）+ legacy SHA 断言；
4. 同步文档：`SKILL.md` 新节 + `references/PIPELINE`/`ALGORITHM` + `training/` 实测笔记 + 版本记录；
5. 重打 skill zip（`work/docs6` 模式：build_package.py + check_docs + verify_pkg），版本记录写清兼容性；
6. 发布级改动再更新 `release/` 文档与 `MANIFEST/SHA256SUMS`。

### 9.5 已知限制 / 待办（截至 v1.0.2）

- **画质**：v1.36 人类画法 content −0.486 dB（CONDITIONAL）；剪影羽化 −0.46 dB 同参考口径；
  A 区 largest_flat 0.222→0.416；compactness 0.201<0.25（区域推进仍有前沿）。
- **GPU**：非 bit-exact；依赖 PyOpenGL/glfw/桌面 GL；4096 显存 FBO≈525MB+实例≈347MB；
  alpha<1→exit 5、region_fill→exit 4 回退 CPU；2048 原生参考已补测（41.9643/0.99623）。
  **v1.38.1**：Windows 下自动清除宿主 `PYOPENGL_PLATFORM=egl`（防静默回退 CPU）+ 防御重试；
  Linux/macOS 保留该变量（重试兜底），其它回退根因仍需看日志。
- **性能**：GPU 端到端瓶颈已转移到 CPU load+sample（~80%）；视频/训练渲染未 GPU 化。
- **平台**：仅 Windows+ComfyUI 实测；mac/Linux 未验证；未做 CI。
- **历史 QA 未修**：`work/qa/ISSUES.md` 的 QA-05/06/08/09/12–15/17–19（RGBA/竖幅/参数校验/
  口径不一致/错误信息等）；发布包不含 `work/`，接手时需向原维护者索取。
- **待办（可做）**：GPU 采样/异步回读提速；training/评估渲染 GPU 化（渲染结果与画法解耦）；
  视频采样缓存；跨平台验证；compactness 优化（区域并行收敛）。

### 9.6 常见坑

- 用**错误的 python**（系统 python）跑 skill → 缺 numpy/scipy/PIL；必须用 ComfyUI python 或
  `FDP_PYTHON` 指向的解释器（workbuddy python 无 PyOpenGL，GPU 用例会 skip/exit 3）。
- 改了 report 字段名没同步 `SKILL.md`/测试 FROZEN → 文档门禁失败。
- 在 `layered_base` 里把裁剪/羽化放到渲染之后再处理 → 违反“生成期修复”原则（会被验收打回）。
- 节点 README/工作流与 repo 副本不一致 → 发布包与部署不一致；改节点后两处副本都要同步。
- 文档写本机盘符绝对路径（如 Windows 用户目录/工作区路径）→ 打包自检失败（check_docs 会扫）。
- 改了 GPU 渲染/spawn 环境逻辑后，记得测 `PYOPENGL_PLATFORM=egl` 环境（宿主 env 污染是 v1.38.1 的 P0 根因）。
- 把 `work/`、`dist/` 旧包、`training/`、LoRA 大文件打进发布包 → 超 120MB 上限。

---

## 10. 工业设备扩展路线（CNC / 刺绣 / 激光 / 机器人）

> 本项目的核心是**有序笔画数据**（`points` / `width` / `color` / `alpha` / `kind` / `layer` / 绘制顺序），
> 与“在屏幕上画像素”这一执行器解耦。下面的路线把同一份数据映射到物理设备；**当前发布版尚未实现
> 这些导出器**，本节是架构与工程路线，供合作开发与接手者参考。

### 10.1 为什么可扩展：笔画数据模型与执行器无关

`.fdraw`（`render.json` 为权威）中的每条笔触 = 归一化路径点序列 + 逐点宽度（覆盖/压强代理）+
颜色/alpha + 笔触类型/层 + 有序时序。渲染器（CPU exact / GPU GL）只是“解释器”之一；
换成刀具路径解释器、绣花线迹解释器或激光功率解释器，数据本身不需要重画。关键不变量：
顺序（先画后画 → 覆盖关系）、宽度（几何覆盖）、颜色（材料/线色）、时序（作画/加工顺序）。

### 10.2 设备映射表

| 设备 | 路径 → | 宽度 → | 颜色 → | 顺序 → | 输出格式 | 主要难点 |
|---|---|---|---|---|---|---|
| 数控铣床（三轴 2.5D） | 折线/G-code 走刀 | 刀具半径 + Z 轴深度（分层） | 材料/换刀（可选） | 加工顺序（减少空程） | G-code（M3/M5、F、S、G0/G1） | 刀具半径导致细特征丢失、尖角过切、夹持与回零 |
| 刺绣机 | 平针/缎面/填针线迹 | 线迹密度/针距 | 换色顺序 | 换色与跳针顺序 | DST / EXP / JEF | 张力、跳针、最小线迹长度、换色次数 |
| 激光雕刻 / 绘图仪 | 路径 + 功率/进给 | 功率/速度或笔压 | 材料/笔色 | 先内后外减少熏黑 | G-code / HPGL / SVG | 烧焦/熔化、烟尘、焦点与功率标定 |
| 喷墨 / UV 打印 | 喷头 pass 路径 | 覆盖率/墨量 | CMYK/专色 | 分层 pass 顺序 | 厂商 RIP 格式 / 位图分色 | 喷头对齐、UV 固化、覆盖率限制 |
| 机械臂 / 协作机器人 | TCP 轨迹 | 工具半径/压力 | 工具/材料 | 姿态与可达性 | 关节/笛卡尔轨迹（ROS/URScript 等） | 奇异点、碰撞、姿态连续性 |

### 10.3 工具路径管线（拟新增 `toolpath.py`）

1. **读入** `.fdraw`（`render.json` 权威；或 `strokes.json`），取有序笔画与宽度/颜色；
2. **自适应展平**：按弦高容差把贝塞尔/采样折线离散为加工点（容差按设备精度设定）；
3. **补偿**：刀具半径补偿（CNC）、针距/线迹密度（刺绣）、光斑半径（激光）；
4. **重采样与规划**：按设备加速度/速度限制重采样；规划进给/功率/针距；
5. **指令生成**：G-code（`G0/G1`、`F`、`S`、`M3/M5`）或绣花格式（DST/EXP/JEF，需要针迹引擎）；
6. **数字孪生仿真**：复用 `gpu_render.py` / CPU renderer 把工具路径“画”出来做预演对比
   （可加 `--simulate` 输出对比图，不改现有渲染/流水线）；
7. **校验**：设备限位/软限位、最小特征尺寸、空程与安全高度、夹持区域碰撞检查。

### 10.4 物理量映射契约（草案）

| 笔画属性 | CNC | 刺绣 | 激光/绘图仪 | 喷墨/UV | 机械臂 |
|---|---|---|---|---|---|
| `width` | 刀具直径 + Z 深度 | 线迹密度/针距 | 功率/进给 或 笔宽 | 覆盖率/墨量 | 工具半径/压力 |
| `color` | 材料/换刀编号 | 线色/换色索引 | 材料/功率档 | CMYK/专色分色 | 工具编号 |
| 区域/层（region） | 夹持分区/工序 | 换色顺序/绣框分区 | 先内后外 | pass 分组 | 可达工作区 |
| 时序（order） | 加工顺序/空程优化 | 跳针顺序 | 扫描顺序 | 喷头 pass | 轨迹顺序 |
| 逐点宽度 | 变半径雕刻 | 变针距缎面 | 变功率 | 覆盖率调制 | 变压力/速度 |

### 10.5 各设备关键工程问题

- **CNC**：刀具半径会使细特征消失、尖角过切；需半径补偿与最小特征校验；木/亚克力回弹、熔化
  （转速/进给/冷却）；夹持重复定位与粉尘。
- **刺绣**：绣线张力与跳针、最小线迹长度、换色次数与绣框移动；需要打版/试绣标定。
- **激光/绘图仪**：功率-速度标定、焦点、烟尘与烧焦、材料差异；先小样矩阵标定。
- **喷墨/UV**：喷头对齐、覆盖率上限、UV 固化与材料附着。
- **机械臂**：奇异点/碰撞/姿态连续；需要安全围栏与急停。
- **通用**：安全（急停/限位/防护）、粉尘/烟尘/激光防护、合规与责任、先低成本设备验证。

### 10.6 分阶段路线图

- **Phase 0（低成本验证）**：SVG/HPGL/G-code 导出 + 数字孪生仿真对比；不接物理设备。
- **Phase 1**：三轴 CNC 2.5D（木/亚克力）浮雕/刻画；刀具半径补偿与最小特征校验。
- **Phase 2**：刺绣机 DST 生成与试绣（针迹引擎、换色、张力标定）。
- **Phase 3**：激光/绘图仪/多轴机械臂；速度/功率/姿态规划。
- **Phase 4**：闭环视觉校准——用本套渲染器作为“预期结果”，相机采集实物偏差后回写补偿参数。

### 10.7 代码落点（拟）

- 新增 `workspace/skills/fourier-multistroke-training/scripts/toolpath.py`（或
  `workspace/fourier-brushstroke/scripts/` 内）：在 `fbs_core.render.sample_stroke` 的采样几何之上
  做展平/补偿/规划/导出；CLI `toolpath.py --fdraw X.fdraw --device cnc|embroidery|laser|plotter ...`。
- `gpu_render.py` 作为**数字孪生仿真器**：渲染工具路径预览图；测试可加 `--simulate` 输出对比图。
- 不改动现有渲染/流水线与节点；新功能以可选项加入（不影响 v1.0.x 兼容）。

### 10.8 风险与边界（如实）

- **不是 CAM 替代品**：没有材料库/刀具库/机床后置处理，需要专业 CAM 与实测标定配合。
- 需要材料/刀具/张力等**实测标定**；工业精度需要**闭环校准**（开环只能做艺术性/低精度加工）。
- **安全与合规责任**：设备操作、粉尘/烟尘/激光防护、急停与资质由使用者/集成方承担。
- 建议先用低成本设备（绘图仪、小型 CNC/绣花机）验证，再进入生产环境。

### 10.9 合作方式

工业设备扩展（CNC / 刺绣 / 激光 / 机器人）的联合开发、定制与咨询：
**keminoco@outlook.com**。

## 11. 变更历史指针

| 版本 | 主题 | 详见 |
|---|---|---|
| v1.33 | 渐变安全门 / 测试套件落地 | skill `references/` + `test_artifacts/unit/` |
| v1.34 | texture-flat 细纹理层 | `training/sunflowers/COLOR_BANDING_FIX.md`（skill 内） |
| v1.35 | 人类画师式底色（runE_dabs 冻结基线） | `training/sunflowers/UNDERPAINT_PRINCIPLE.md` |
| v1.36 | 人类画师式画法（wash/flow/region） | `training/sunflowers/HUMAN_PAINT_ORDER.md` |
| v1.37 | 剪影感知笔触生成 | `training/silhouette/SILHOUETTE_AWARE.md` |
| v1.38 | GPU 渲染（OpenGL） | `training/gpu_render/GPU_RENDER.md` |
| v1.0.0 | 可移植组装 + v1.0.0 发布包 | `CHANGELOG.md` + `test_artifacts/release_test/` |
| v1.0.1 | 文档与展示更新（例图/赞助/工业扩展 §10） | `CHANGELOG.md` + `README.md` 作品示例 |
| v1.0.2 | GPU 渲染兼容性修复（`PYOPENGL_PLATFORM=egl` 静默回退 CPU） | `CHANGELOG.md` + `tests/test_gpu_render.py` |

> 本文件与 skill 内文档冲突时：**画法/算法细节以 skill `references/` 为准**，跨组件/发布信息以
> 本文件与 `INSTALL.md`/`USER_GUIDE.md` 为准；数字以 `test_artifacts/` 的实测记录为准。
