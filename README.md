# Fourier Drawing Suite v1.0.0

把**图片或文字**变成「**纯笔画绘制的傅里叶画**」，并输出 PNG / 作画视频的完整套件：
一个 ComfyUI 自定义节点包 + 三个可独立使用的傅里叶绘制工作区 + 安装/自检脚本。

> 版本：**suite v1.0.0**（2026-10-25）｜skill `v1.38-gpu-render`｜ComfyUI 节点 **v3.1.0**（可移植安装）
> 支持平台：**Windows + ComfyUI**（Python 3.10–3.13；以 ComfyUI 自带 python 为准）
> 授权：**Proprietary / All Rights Reserved**（见 `NOTICE.md`；第三方库/字体/笔刷资源各自授权）

---

## 它是什么（30 秒）

| 你有的 | 套件给你 |
|---|---|
| 一张图（插画/线稿/照片风格图） | 用「多笔画傅里叶 + 笔刷属性」复刻成**纯笔画画**（无区域填色，`fills=0`） |
| 一句提示词 | 走**本地 Krea2**（无需 API Key）或**API**（Krea2 API）生成一张图，再复刻成笔画画 |
| 一份笔画画（`.fdraw`） | 渲染任意分辨率 PNG（**GPU 加速**，1024/2048/4096）或**傅里叶作画过程视频** |

**它不做什么**（重要）：不做照片级保真复刻；不直接用文字生图（必须经 Krea2/API 出图再画）；
不是手机 App；不保证在**没有 OpenGL 的服务器**上用 GPU（会自动回退 CPU，慢但可用）。

---

## 5 分钟快速开始

> 完整步骤（含截图级说明与排错）见 [`USER_GUIDE.md`](USER_GUIDE.md)；安装细节见 [`INSTALL.md`](INSTALL.md)。

1. **解压**整个发布包到任意目录（可以含空格/中文），例如 `<path>`。
2. 确认 **ComfyUI 已安装**并可正常打开（Windows 版即可）。
3. 打开 PowerShell，在**发布包根目录**运行安装器（把 ComfyUI 路径换成你的）：
   ```powershell
   "<ComfyUI 自带的 python>" release\install.py --comfyui "<path>"
   # 例： --comfyui "<path>"
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
   "<ComfyUI 自带的 python>" release\check_env.py --comfyui "<path>"
   ```

**速度参考**（RTX 4070 Ti SUPER，chibi 38,737 笔）：GPU 渲染 **1024 约 14–17 s、2048 约 15–19 s、
4096 约 22–28 s**；CPU exact 参考 **1024 = 219 s、4096 ≈ 21 min**。没有 GPU/OpenGL 时自动用 CPU。
桌面环境变量 `FDP_GPU_RENDER=0` 可强制 CPU。

---

## 目录地图

```
fourier-drawing-suite-v1.0.0/
├── README.md               本文件（总览 + 快速开始 + 组件版本）
├── INSTALL.md              安装/依赖/卸载/离线/常见失败
├── USER_GUIDE.md           普通人完整使用说明（三种工作流、参数、输出、FAQ）
├── ENGINEERING.md          工程构建思路 + 算法 + 数据格式 + 给 AI 的接手指南
├── CHANGELOG.md            v1.33→v1.38 变更摘要与 v1.0.0 发布说明
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
├── test_artifacts/                            完整测试产物（单元测试基线/各版本 VERDICT/指标/对比图/
│                                              发布兼容测试报告/复现命令）
│                                              （证据日志中的本机路径已脱敏为 <workspace>/<python> 等占位符）
├── MANIFEST.json                              全文件 SHA256 + 大小 + 组件版本
└── SHA256SUMS.txt                             校验和清单（sha256sum -c 可用）
```

## 组件版本

| 组件 | 版本 | 说明 |
|---|---|---|
| **suite** | **v1.0.0** | 本次整体发布（文档+组装） |
| skill `fourier-multistroke-training` | **v1.38-gpu-render** | 图片→多笔画傅里叶画；GPU 渲染；剪影感知；人类画师式画法 |
| ComfyUI 节点 `ComfyUI-FDP-T2I` | **3.1.0** | 可移植：`FDP_WORKSPACE`/`FDP_PYTHON`/`fdp_config.json` 解析，无本机绝对路径 |
| `fourier-brushstroke` | fbs 格式 **2.0.0** | 位图→中轴笔画→`.fbs`/`.fdraw`→静态图/一笔画/视频 |
| `fourier-prompt` | **0.1.0** | FDSL v2.1 规划/编译/`.fdraw` 工具链（stdlib-only core） |
| 工作流 | v1.36 系列 + 旧版 | 3 个 v1.36 主工作流 + 旧 fdraw + 视频工作流（workflow JSON 未改） |
| Python | 3.10–3.13 | 以 ComfyUI 自带 python 运行节点与 skill 脚本 |
| GPU（可选） | OpenGL 4.x / PyOpenGL 3.1+ / glfw 2.6+ | 无 GL 自动回退 CPU exact |

## 关键实测（均为本机真实测量，详见 `ENGINEERING.md` / `test_artifacts/`）

| 项 | 数值 |
|---|---|
| GPU 渲染（chibi `.fdraw` 38,737 笔） | **1024 14.2–16.8 s / 2048 15.1–19.4 s / 4096 21.9–27.6 s**（不同次测量区间） |
| CPU exact 对照 | 1024 **219 s**、2048 **581.6 s**、4096 **≈1260 s（21 min）** → GPU **15.4× / 38.5× / 53×** |
| GPU vs CPU 质量 | PSNR **40.219 / 41.9643 / 44.109 dB**（1024/2048 原生/4096）、SSIM ≥0.9957 |
| 剪影修复（白底插画） | 非底色笔越界 >0.5px **5,636→0**、>2px **3,182→0**、max **10.0→0.25 px** |
| 单元/契约测试 | **142 tests**（完整仓库默认 13 skip = 3 慢 + 10 GPU；**发布包干净环境为 139 ran/16 skip**——不含 `work/`、`training/` 演示图，两个真实图测试类会 skip；`RUN_GPU=1` 子集 19/0/1） |
| legacy 逐字节兼容 | v1.33 `30CDCE74…`、v1.34 `69820EEF…` 不变 |

## 进一步阅读

- 第一次用 → [`USER_GUIDE.md`](USER_GUIDE.md)
- 装不上/缺依赖/GL 失败 → [`INSTALL.md`](INSTALL.md) + `USER_GUIDE.md` 的 10 条 FAQ
- 想改代码/接手的 AI → [`ENGINEERING.md`](ENGINEERING.md)（架构图、公式、数据格式、模块地图、不变量、测试方法）
- 版本历史与已知限制 → [`CHANGELOG.md`](CHANGELOG.md)
- 授权 → [`NOTICE.md`](NOTICE.md)

> 本发布包不承诺“任何环境开箱即用/画质无损”：GPU 需要桌面 OpenGL；CPU exact 仍是
> legacy/参考口径；质量与速度数字均为特定机器上的实测（区间已如实标注）。
