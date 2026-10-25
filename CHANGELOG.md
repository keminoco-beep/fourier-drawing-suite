# CHANGELOG — Fourier Drawing Suite

> 本文件覆盖 **suite v1.0.0 / v1.0.1 / v1.0.2** 发布说明与组成组件的近版变更（v1.33 → v1.38）。
> 每个版本的完整测试基线见 `test_artifacts/unit/`；验收结论见 `test_artifacts/verdicts/`。

## v1.0.2 — GPU 渲染兼容性修复（2026-10-25，skill v1.38.1-gpu-envfix）

**P0 根因**：ComfyUI 宿主环境里有其它节点设置 `PYOPENGL_PLATFORM=egl`；`RenderFdraw` 用
`sys.executable` 拉起的 `gpu_render.py` 子进程**继承**该变量 → PyOpenGL 在 Windows 上尝试 EGL 失败 →
`gpu_render.py` 按契约 **exit 3** → 节点**静默回退 CPU exact**（用户实测一张 100k 笔图 4096 需
**3159 s ≈ 52.6 min**）。

**修复（skill v1.38.1-gpu-envfix，`scripts/gpu_render.py`）**：①Windows 下在 import OpenGL/glfw 前
`os.environ.pop("PYOPENGL_PLATFORM")`（改走桌面 WGL）；②GL 探测改为防御重试：首次失败且该 env 存在 →
pop 后重试一次，仍失败才 exit 3（stderr 保留两次异常）；③**不改 `nodes.py`**——每次 Queue 都会用新的
子进程，**用户无需重启 ComfyUI**；Linux/macOS 不强制清除（防御重试兜底）。

**实测（同一 100k 笔 `.fdraw`，4096）**：
- 修复前（egl env，回退 CPU exact）：**3159 s**
- 修复后（真实部署节点）：**56.4 s wall / 53.8 s GPU 渲染**（timing：load 11.0 / sample 27.8 /
  geometry 6.0 / gpu 1.7 / readback 4.6 s）；输出 `fdraw_R4096_gpu.png` + `fdraw_R4096_gpu_timing.json`，
  **无 `_exact.png`**，tensor `(1, 4096, 2913, 3)` finite；日志 `[FDP-T2I] GPU render ok res=4096`。
- egl 专项：`PYOPENGL_PLATFORM=egl` 下 gpu_render 256 **rc0 1.13 s**，PNG SHA 与 clean env **完全一致**
  （maxdiff 0.0）；真无 GL 环境仍 exit 3（行为不变）。
- 节点级复验：模拟 ComfyUI egl env → 只出 `fdraw_R512_gpu.png` + timing、无 `_exact.png`（5.42 s）；
  `FDP_GPU_RENDER=0` → `_exact.png`（11.85 s，符合契约）；发布包 `chibi.fdraw` @1024 + egl → GPU 7.87 s。

**测试**：三套件全绿——workbuddy 默认 **146 ran / 16 skip / 0 fail / 104.3 s**；ComfyUI python 默认
**146 / 17 / 0 / 80.7 s**；`RUN_GPU=1` 子集 **23 ran / 1 skip / 0 fail / 29.5 s**（新增 4 项 egl/env 回归）。
发布包干净环境（不含 work/training 演示图）：**Ran 143 / skipped 18（workbuddy）/ 19（ComfyUI）** 全绿。

**注意（预期管理）**：GPU 只加速**最终渲染**；大图总耗时仍包含 CPU 的 `layered_base` 笔画生成（约 2.5 min）
+ `.fbs` 傅里叶编译（100k 笔 / 1536 约 11 min）+ `pack_fdraw` 等，不能只看渲染段。

**其它不变**：节点版本 3.1.0、workflow JSON、画法与质量指标（负 −0.486dB CONDITIONAL 等结论）均不变。

## v1.0.1 — 文档与展示更新（2026-10-25）


- 文档新增《向日葵》纯笔画示例图 `docs/images/sunflowers_v136_content_R2048.png`（README / USER_GUIDE / ENGINEERING）。
- 新增赞助/合作联系方式 **keminoco@outlook.com**，置于 README/USER_GUIDE 醒目位置与 INSTALL/NOTICE。
- ENGINEERING 新增 §10「工业设备扩展路线（CNC / 刺绣 / 激光 / 机器人）」：设备映射、工具路径管线、物理量映射、路线图、风险。
- 功能代码、节点版本、测试与验证结论不变（沿用 v1.0.0）。

## v1.0.0 — 整体发布（2026-10-25）

**发布内容**：可移植 ComfyUI 节点包（3.1.0）+ 三个工作区（skill v1.38-gpu-render、
fourier-brushstroke、fourier-prompt）+ 安装/自检/卸载脚本 + 5 个工作流 + chibi 示例 +
完整测试产物 + 本套文档。

**本版重点**：

- **可移植安装**：节点不再硬编码本机路径；`release/install.py` 自动复制节点并写
  `fdp_config.json`；workspace/python 解析顺序 `FDP_WORKSPACE`/`FDP_PYTHON` env > config > 自动探测。
- **GPU 渲染**（v1.38）：`gpu_render.py`（OpenGL 离屏 GLSL 胶囊光栅化）接入 `RenderFdraw`，
  自动 GPU→CPU fallback；1024/2048/4096 提速 **15.4×/38.5×/53×**（实测区间见下）。
- **剪影感知**（v1.37）：白底插画轮廓溢出生成期修复（非后期修图）。
- **人类画师式画法**（v1.36）：wash 铺底 + 顺形体笔触 + 区域成组顺序，替换扫描式画法。
- **完整测试产物**：142 个单元/契约测试 + 各版本独立验收 VERDICT + 指标 JSON + 对比图 +
  跨环境发布兼容测试报告，均随包提供（`test_artifacts/`，复现命令见 `reproduce.md`）。

**已知限制（发布版如实声明，不夸大）**：

1. **画质代价**：v1.36 人类画法相对 v1.35 冻结基线（runE_dabs，doc 协议）content PSNR
   **24.6184→24.1320（−0.486 dB）**，超出 ≤0.4 dB 允许线 0.086 dB → 质量子项 **CONDITIONAL**；
   A 区 largest_flat 0.222→0.416 回退。剪影修复另带来同参考口径 **−0.46 dB**（羽化代价，属预期）。
2. **GPU 渲染不是 bit-exact**：与 CPU exact 相比 PSNR 40.2/41.96/44.1 dB、SSIM ≥0.9957，
   差异集中在笔触边缘 AA；需要逐字节等价时用 CPU exact（`FDP_GPU_RENDER=0`）。
3. **GPU 依赖**：PyOpenGL + glfw + 桌面 OpenGL 4.x；headless/远程/无 GL 环境自动回退 CPU（不崩）。
4. **alpha<1 / region_fill 文档**：GPU 不支持（exit 5/4），节点自动回退 CPU；当前流水线输出 alpha=1。
5. **训练/评估渲染与视频渲染未 GPU 化**；视频渲染仍是 CPU 路径（未来可缓存采样）。
6. **平台**：只在 Windows + ComfyUI 实测；无 mac/Linux 验证；照片级保真、直接文字生图、手机端**不支持**。

---

## v1.38-gpu-render（2026-10-25，skill）

- 新增 `scripts/gpu_render.py`：PyOpenGL+glfw 离屏 FBO + GLSL 330 胶囊距离场光栅化，
  采样复用 `fbs_core.render.sample_stroke`；`--device gl|cpu`、`--supersample`（默认 2）、
  `--timing-json`；输入 `.fdraw/.fbs/render.json/--strokes`。
- `RenderFdraw` 自动 GPU（节点进程 `sys.executable`）→ 失败/无 GL（exit 3）/alpha<1（exit 5）/
  region_fill（exit 4）/超时 → CPU `fdrender exact` fallback；`FDP_GPU_RENDER=0` 强制 CPU；
  workflow JSON/widget 数不变；`PACKAGE_VERSION` 3.0.1（v1.0.0 发布节点可移植版为 **3.1.0**）。
- 实测：GPU **1024 14.2–16.8 s / 2048 15.1–19.4 s / 4096 21.9–27.6 s** vs CPU exact
  **219 s / 581.6 s / ≈1260 s** → **15.4× / 38.5× / 53×**；质量 GPU vs CPU exact
  PSNR **40.219 / 41.9643（原生 2048）/ 44.109 dB**、SSIM **0.99566 / 0.99623 / 0.99724**。
- 新增 `tests/test_gpu_render.py`（19 用例，10 条默认 skip，`RUN_GPU=1` 执行）；
  套件总 **142 tests**（默认 13 skip = 3 慢 + 10 GPU）。

## v1.37-silhouette-aware（2026-10-25，skill）

- 新增 `--silhouette {auto,on,off}`（默认 auto）+ `--silhouette-shrink-px 1.0` / `-margin-px 0.25` /
  `-min-width-px 0.6` / `-feather-px 0.8`；生成期几何裁剪（端点回缩→逐点宽钳制→过细丢笔）+
  渲染剪影羽化合成；legacy 自动 off（v1.33/v1.34 逐字节不变）。
- 实测（chibi 1280）：非底色笔越界 >0.5px **5,636→0**、>2px **3,182→0**、max **10.0→0.25 px**；
  painted outside sil **13,325→0 px**；确定角色外 **3.54%→2.55%**（残余 AA 边）；
  笔数 38,737→34,107；PSNR 同参考 25.827→25.366（−0.46 dB 羽化；report 内部口径 +0.35 dB）；
  耗时 49.5→63.4 s。新增 `tests/test_silhouette.py`（15 用例），套件总 **121 tests**。

## v1.36-human-paint-order（2026-10-24，skill）

- 画法从“扫描式多轮”改为**人类画师式**：`--base-coat-pattern wash`（方向多变宽笔薄涂，
  `--wash-angle -22`）、`--flow-strokes on`（笔触顺结构张量切向）、`--paint-order region`
  （区域成组，区内 block-in→细节→texture）、`--region-count 12`、`--thick-dabs`、`--clip-to-fg`。
- 顺序/扫描性独立评估 **PASS**（wash 底 axis 1.000→0.183、扫描层数 3→0、层块连续性 1.000→0.121、
  簇 span 0.974→0.548）；质量 **CONDITIONAL**（−0.486 dB，见 v1.0.0 已知限制）；
  compactness 0.085→0.201 仍低于 0.25。新增 `tests/test_human_paint.py`（18），套件总 **104 tests**。

## v1.35-human-underpainting（2026-10-24，skill）

- 底色改为**人类画师式纯色单次薄涂**（`--base-color-mode toned`，kind=`base_toned`/layer0，
  无 gradient/抖动）；`none` 用于线稿；`--base-color`、`--base-coat-*` 参数；100k 硬上限。
- 该版重画（runE_dabs）成为后续质量冻结基线：doc 协议 content **24.6184**、square **24.6875**、
  A 24.625 / largest_flat 0.222 / blocks 64。新增 `tests/test_base_coat.py`（19），套件总 84。

## v1.34-color-banding（2026-10-24，skill）

- 新增 `--texture-flat` 细纹理层（平坦区细短笔），A 区“颜色分层”明显减轻
  （blocks −29、largest_flat 0.447→0.276）；`tests/test_texture_flat.py`（17）。

## v1.33-gradient-safe（2026-10-24，skill）

- 渐变安全门默认开启（端点 Δ>0.12 拒绝），跨色边界糊斑修复；`--detail` 字段过滤修复；
  初始 unittest 套件落地。legacy 参考 PNG SHA **`30CDCE74…EB9A01`**（v1.33 canonical；canonical python 口径——**ComfyUI python（3.11）下 v1.33 重建与旧产物 ≤1 LSB 差异**，逐字节断言以 workbuddy python 记录为准）。

---

## 兼容性说明

- **legacy 逐字节**：v1.33 `30CDCE74…` / v1.34 `69820EEF…` 在 v1.36/v1.37/v1.38 中均由
  `--flow-strokes off --paint-order legacy`（或旧底色模式自动回退）复现，测试持续断言。
- **workflow JSON**：v1.36 工作流在 v1.37/v1.38 均未改（GPU 渲染由节点内部自动选择）。
- **`.fbs` 格式**：fourier-brushstroke `fbs 2.0.0`；`.fdraw` 容器字段向后兼容。
- **公开接口**：skill 脚本 CLI 参数只增不减；节点 widget 数量不变；节点包版本 3.x（≥3.0.1）兼容。
