# Fourier Drawing Suite v1.0.2

把**图片或文字**变成「**纯笔画绘制的傅里叶画**」，并输出 PNG / 作画视频的完整套件：
一个 ComfyUI 自定义节点包 + 三个可独立使用的傅里叶绘制工作区 + 安装/自检脚本。

> 版本：**suite v1.0.2**（2026-10-25，GPU 渲染兼容性修复 P0）｜skill `v1.38.1-gpu-envfix`｜ComfyUI 节点 **v3.1.0**（可移植安装）
> 支持平台：**Windows + ComfyUI**（Python 3.10–3.13；以 ComfyUI 自带 python 为准）
> 授权：**Proprietary / All Rights Reserved**（见 `NOTICE.md`；第三方库/字体/笔刷资源各自授权）

> ❤️ **赞助与支持 / Sponsor**
>
> 本项目由个人独立开发与维护。如果它对你的创作或工作有帮助，欢迎赞助支持
> （用于 GPU 渲染、工业设备后处理、更多画法研究等后续开发）。
> **赞助、合作或定制开发请联系：keminoco@outlook.com**

---

## 它是什么（30 秒）

| 你有的 | 套件给你 |
|---|---|
| 一张图（插画/线稿/照片风格图） | 用「多笔画傅里叶 + 笔刷属性」复刻成**纯笔画画**（无区域填色，`fills=0`） |
| 一句提示词 | 走**本地 Krea2**（无需 API Key）或**API**（Krea2 API）生成一张图，再复刻成笔画画 |
| 一份笔画画（`.fdraw`） | 渲染任意分辨率 PNG（**GPU 加速**，1024/2048/4096）或**傅里叶作画过程视频** |

**它暂时做不到的事**（重要）：做不到照片级保真复刻；暂时也无法直接文字生图（需先经 Krea2 或其他扩散式绘图模型出图，再进行笔画后加工）；不保证在**没有 OpenGL 的服务器**上使用 GPU（会自动回退 CPU，慢但可用）。

### 作品示例

![Sunflowers v1.36 人类画师式纯笔画](docs/images/sunflowers_v136_content_R2048.png)

*《向日葵》纯笔画复刻（v1.36 人类画师式笔画，约 10 万笔，2048 像素长边；无区域填色，每一笔都是真实曲线）。*

---

## 从屏幕到工业设备：可扩展方向

本项目的核心资产不是“一张 PNG”，而是**有序的笔画数据**：每一笔 = 路径点序列 + 宽度（覆盖/压强）+ 颜色/材料 + 时序。这套表示与具体执行器无关——今天它驱动屏幕上的像素笔刷，明天就可以驱动：

- **数控铣床 / CNC**：笔画宽度 → 刀具半径与 Z 轴深度，路径 → G-code；先在木/亚克力上做 2.5D 浮雕刻画；
- **刺绣机**：路径 → 平针/缎面/填针线迹，宽度 → 线迹密度，颜色 → 换色顺序，导出 DST/EXP/JEF；
- **激光雕刻 / 绘图仪**：笔画宽度与速度 → 激光功率/进给，或笔压与落笔顺序；
- **喷墨 / UV 打印**：笔画顺序 → 喷头 pass 规划，宽度 → 覆盖率；
- **机械臂 / 协作机器人**：路径 + 方向 → TCP 轨迹与姿态。

> 详细的设备映射、工具路径管线（展平→刀具半径补偿→速度规划→设备指令→数字孪生仿真）与分阶段路线图见 [`ENGINEERING.md` §10 工业设备扩展路线](ENGINEERING.md#10-工业设备扩展路线cnc--刺绣--激光--机器人)。

---

## 5 分钟快速开始

> 完整步骤（含截图级说明与排错）见 [`USER_GUIDE.md`](USER_GUIDE.md)；安装细节见 [`INSTALL.md`](INSTALL.md)。

1. **解压**整个发布包到任意目录（可以含空格/中文），例如 `D:\FDP\fourier-drawing-suite-v1.0.2`。
2. 确认 **ComfyUI 已安装**并可正常打开（Windows 版即可）。
3. 打开 PowerShell，在**发布包根目录**运行安装器（把 ComfyUI 路径换成你的）：
   ```powershell
   "<ComfyUI 自带的 python>" release\install.py --comfyui "D:\ComfyUI"
   # 例： --comfyui "D:\ComfyUI_windows_portable\ComfyUI"
   ```
   安装器会把节点包复制到 `ComfyUI\custom_nodes\ComfyUI-FDP-T2I` 并写入 `fdp_config.json`
   （记录发布包位置与 python 路径，之后不需要改任何代码）。
4. 若 ComfyUI python 缺依赖（脚本会提示），在发布包根目录运行：
   ```powershell
   "<ComfyUI 自带的 python>" -m pip install -r release\requirements-core.txt
   # 想要 GPU 渲染（可选）：
   "<ComfyUI 自带的 python>" -m pip install -r release\requirements-gpu.txt
   ```
5. **重启 ComfyUI**，打开 `comfyui\workflows\` 里的任一工作流 JSON（或拖入 ComfyUI 窗口）：
   - 不上 AI：`FDP_v136_Local_NoAI_UI.json`（图片 → 笔画画）
   - 本地 Krea2（无需 API Key）：`FDP_v136_T2I_Krea2_Local_UI.json`
   - Krea2 API（需 Key）：`FDP_v136_T2I_Krea2_API_UI.json`
   - 旧版 fdraw 工作流 / 视频：`FDP_T2I_Krea2_fdraw_UI.json`、`FDP_fdraw_to_fourier_video_UI.json`
6. 点 **Queue** 运行；结果在 `ComfyUI\output\fdp_t2i_runs\<时间戳>_<模式>\`（PNG/`.fdraw`/`strokes.json`/报告）。
7. 自检（可选，不联网）：
   ```powershell
   "<ComfyUI 自带的 python>" release\check_env.py --comfyui "D:\ComfyUI"
   ```

**速度参考**（RTX 4070 Ti SUPER，chibi 38,737 笔）：GPU 渲染 **1024 约 14–17 s、2048 约 15–19 s、
4096 约 22–28 s**；CPU exact 参考 **1024 = 219 s、4096 ≈ 21 min**。没有 GPU/OpenGL 时自动用 CPU。
桌面环境变量 `FDP_GPU_RENDER=0` 可强制 CPU。

---

## 目录地图

```
fourier-drawing-suite-v1.0.2/
├── README.md               本文件（总览 + 快速开始 + 组件版本）
├── INSTALL.md              安装/依赖/卸载/离线/常见失败
├── USER_GUIDE.md           普通人完整使用说明（三种工作流、参数、输出、FAQ）
├── ENGINEERING.md          工程构建思路 + 算法 + 数据格式 + 给 AI 的接手指南
├── CHANGELOG.md            v1.33→v1.38 变更摘要与 v1.0.0/v1.0.1/v1.0.2 发布说明
├── NOTICE.md               授权与第三方许可证汇总
├── release/                安装/自检/卸载脚本 + requirements + config 模板
├── workspace/
│   ├── skills/fourier-multistroke-training/   核心：图片→多笔画傅里叶画（skill v1.38）
│   ├── fourier-brushstroke/                   位图→中轴笔画/Fourier（.fbs/.fdraw 与渲染器）
│   ├── fourier-prompt/                        FDSL 提示词规划与 .fdraw 工具链
│   └── poc/t2i/fdraw_fourier_video.py         作画视频脚本（`FDPT2IFourierVideo` 节点调用）
├── comfyui/
│   ├── custom_nodes/ComfyUI-FDP-T2I/          ComfyUI 节点包 v3.1.0（可移植）
│   └── workflows/                             5 个工作流 JSON（3 个 v1.36 + 旧 fdraw + 视频）
├── examples/                                  chibi 示例（源图/strokes/fdraw/渲染 PNG/命令）
├── docs/images/                                文档示例图（《向日葵》纯笔画渲染）
├── test_artifacts/                            完整测试产物（单元测试基线/各版本 VERDICT/指标/对比图/
│                                              发布兼容测试报告/复现命令）
│                                              （证据日志中的本机路径已脱敏为 <workspace>/<python> 等占位符）
├── MANIFEST.json                              全文件 SHA256 + 大小 + 组件版本
└── SHA256SUMS.txt                             校验和清单（sha256sum -c 可用）
```

## 组件版本

| 组件 | 版本 | 说明 |
|---|---|---|
| **suite** | **v1.0.2** | GPU 渲染兼容性修复（宿主 `PYOPENGL_PLATFORM=egl` 导致静默回退 CPU）；文档例图/赞助/工业扩展沿用 v1.0.1 |
| skill `fourier-multistroke-training` | **v1.38.1-gpu-envfix** | 图片→多笔画傅里叶画；GPU 渲染；剪影感知；人类画师式画法；**修复 EGL env 导致的 GPU 静默回退** |
| ComfyUI 节点 `ComfyUI-FDP-T2I` | **3.1.0** | 可移植：`FDP_WORKSPACE`/`FDP_PYTHON`/`fdp_config.json` 解析，无本机绝对路径 |
| `fourier-brushstroke` | fbs 格式 **2.0.0** | 位图→中轴笔画→`.fbs`/`.fdraw`→静态图/一笔画/视频 |
| `fourier-prompt` | **0.1.0** | FDSL v2.1 规划/编译/`.fdraw` 工具链（stdlib-only core） |
| 工作流 | v1.36 系列 + 旧版 | 3 个 v1.36 主工作流 + 旧 fdraw + 视频工作流（workflow JSON 未改） |
| Python | 3.10–3.13 | 以 ComfyUI 自带 python 运行节点与 skill 脚本 |
| GPU（可选） | OpenGL 4.x / PyOpenGL 3.1+ / glfw 2.6+ | 无 GL 自动回退 CPU exact |

## GPU 被静默回退 CPU？先看这里（v1.0.2 已修复）

**现象**：节点没报错，但一张图渲染了很久（例如 100k 笔 4096 用了 **3159 秒 ≈ 52.6 分钟**），
输出目录里只有 `*_exact.png`（CPU exact）而没有 `*_gpu.png` / `*_gpu_timing.json`。

**根因（v1.38.1 修复）**：ComfyUI 宿主环境里有其它节点设置的 `PYOPENGL_PLATFORM=egl`，
被节点拉起的 `gpu_render.py` 子进程**继承**；Windows 没有 EGL 运行时 → PyOpenGL 加载失败 →
`gpu_render.py` 按契约 exit 3 → 节点**自动回退 CPU exact**（这是“静默”的来源）。

**修复内容（v1.0.2 / skill v1.38.1-gpu-envfix）**：Windows 下 `gpu_render.py` 在加载 OpenGL/glfw 前
清除 `PYOPENGL_PLATFORM`（改走桌面 WGL），并加防御重试（首次失败且存在该变量 → 清掉后重试一次，
仍失败才 exit 3 并保留完整错误）。**不改节点代码、无需重启 ComfyUI**——每次 Queue 都会用新的
`gpu_render.py` 子进程。

**修复后实测**：真实节点 4096（100,000 笔）**56.4 s wall / 50.2 s 渲染**（约 56× 恢复），
输出 `fdraw_R4096_gpu.png` + `fdraw_R4096_gpu_timing.json`，**无 `_exact.png`**，日志
`[FDP-T2I] GPU render ok res=4096`。回归：全量 **146 tests**、GPU 子集 **23 tests / 0 failed / 1 skip**。

**如果升级后仍回退**：看节点日志的 `[FDP-T2I]` 行与 `*_timing.json`；确认 `FDP_GPU_RENDER` 不为 0、
已装 `release\requirements-gpu.txt`（PyOpenGL/glfw）、有桌面 OpenGL；alpha<1 文档（exit 5）与
`region_fill` 文档（exit 4）按设计走 CPU exact。

**日志判定**：出现 `[FDP-T2I] GPU render ok ...` = 成功；出现 `GPU render 失败…回退 CPU exact` 时，
请把 `comfyui.log` 里该行（含 exit 码与 stderr 摘要）发我们，便于定位。

> 注意：GPU 只加速**最终渲染**。大图（如 100k 笔）的其它阶段仍在 CPU：`layered_base` 笔画生成
> 约 2.5 分钟、`.fbs` 傅里叶编译（100k 笔 / 1536）约 11 分钟、`pack_fdraw` 打包等；总耗时不能只看渲染段。

## 关键实测（均为本机真实测量，详见 `ENGINEERING.md` / `test_artifacts/`）

| 项 | 数值 |
|---|---|
| GPU 渲染（chibi `.fdraw` 38,737 笔） | **1024 14.2–16.8 s / 2048 15.1–19.4 s / 4096 21.9–27.6 s**（不同次测量区间）；v1.38.1 修复 EGL env 回退后，真实节点 100k 笔 4096 = **56.4 s**（故障时 **3159 s**） |
| CPU exact 对照 | 1024 **219 s**、2048 **581.6 s**、4096 **≈1260 s（21 min）** → GPU **15.4× / 38.5× / 53×** |
| GPU vs CPU 质量 | PSNR **40.219 / 41.9643 / 44.109 dB**（1024/2048 原生/4096）、SSIM ≥0.9957 |
| 剪影修复（白底插画） | 非底色笔越界 >0.5px **5,636→0**、>2px **3,182→0**、max **10.0→0.25 px** |
| 单元/契约测试 | **146 tests**（完整仓库默认 skip 16 = 3 慢 + 13 GPU；ComfyUI python 17；**发布包干净环境 Ran 143 / skipped 18**——不含 `work/`、`training/` 演示图；`RUN_GPU=1` 子集 **23 ran/0 failed/1 skip**；含 `PYOPENGL_PLATFORM=egl` 回归） |
| legacy 逐字节兼容 | v1.33 `30CDCE74…`、v1.34 `69820EEF…` 不变 |

## 进一步阅读

- 第一次用 → [`USER_GUIDE.md`](USER_GUIDE.md)
- 装不上/缺依赖/GL 失败/GPU 回退 → [`INSTALL.md`](INSTALL.md) + `USER_GUIDE.md` 的 FAQ（含 GPU 未生效排查）
- 想改代码/接手的 AI → [`ENGINEERING.md`](ENGINEERING.md)（架构图、公式、数据格式、模块地图、不变量、测试方法）
- 版本历史与已知限制 → [`CHANGELOG.md`](CHANGELOG.md)
- 授权 → [`NOTICE.md`](NOTICE.md)

> 本发布包不承诺“任何环境开箱即用/画质无损”：GPU 需要桌面 OpenGL；CPU exact 仍是
> legacy/参考口径；质量与速度数字均为特定机器上的实测（区间已如实标注）。
> v1.0.2 修复了宿主 `PYOPENGL_PLATFORM=egl` 导致的 GPU 静默回退（详见下方 FAQ；无需重启 ComfyUI）。
