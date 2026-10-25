# 发布包说明：fourier-drawing-suite v1.0.2（2026-10-25，GPU 渲染兼容性修复 P0）

## 包信息

| 项 | 值 |
|---|---|
| 文件 | `dist/fourier-drawing-suite-v1.0.2-release.zip` |
| SHA256 | `7C22D2EF9657B6EEE862D427D94FD7983BC1CA2B153AA86DC09A1D2D376C9A8F` |
| 大小 | **21,849,295 字节（约 20.8 MiB）**；解压后约 **28.8 MB**（≤120MB 上限 ✅） |
| 文件数 | **548**（MANIFEST 收录 546 个载荷 + MANIFEST/SHA256SUMS） |
| 顶层目录 | 解压后为 `fourier-drawing-suite-v1.0.2/`（单层） |
| 组件版本 | suite **v1.0.2**｜skill **v1.38.1-gpu-envfix**｜节点 **3.1.0**（不变）｜fbs 2.0.0｜fourier-prompt 0.1.0 |
| 上一版 | v1.0.1（`dist/fourier-drawing-suite-v1.0.1-release.zip`，SHA `5FBF77…`）与 v1.0.0 **保留不动** |

## 本版变更（P0：GPU 渲染被静默回退 CPU）

**根因**：ComfyUI 宿主环境里有其它节点设置 `PYOPENGL_PLATFORM=egl`；`RenderFdraw` 用
`sys.executable` 拉起的 `gpu_render.py` 子进程**继承**该变量 → PyOpenGL 在 Windows 上尝试 EGL 失败 →
`gpu_render.py` 按契约 **exit 3** → 节点**静默回退 CPU exact**（用户实测一张 100k 笔图 4096 =
**3159 s ≈ 52.6 min**）。

**修复（skill v1.38.1-gpu-envfix，只需替换 `workspace/.../scripts/gpu_render.py` + 测试）**：
1. Windows 下在 import OpenGL/glfw 前 `os.environ.pop("PYOPENGL_PLATFORM")`（改走桌面 WGL）；
2. GL 探测改为**防御重试**：首次失败且该 env 存在 → pop 后重试一次，仍失败才 exit 3（stderr 保留两次异常）；
3. **不改 `nodes.py`**——每次 Queue 都会拉起新的 `gpu_render.py` 子进程，**用户无需重启 ComfyUI**；
   Linux/macOS 不强制清除（防御重试兜底）。

**实测**：
- 同一 100k 笔 `.fdraw` 4096：**3159 s（回退 CPU）→ 56.4 s wall / 53.8 s GPU 渲染**（≈56×）；
  timing：load 11.0 / sample 27.8 / geometry 6.0 / gpu 1.7 / readback 4.6 s；
  输出 `fdraw_R4096_gpu.png` + `fdraw_R4096_gpu_timing.json`，**无 `_exact.png`**，tensor `(1,4096,2913,3)`。
- egl 专项：`PYOPENGL_PLATFORM=egl` 下 gpu_render 256 **rc0 1.13 s**，PNG SHA 与 clean env **完全一致**；
  真无 GL 环境仍 exit 3；节点级 egl 模拟：`fdraw_R512_gpu.png`+timing、无 `_exact.png`（5.42 s）；
  `FDP_GPU_RENDER=0` → `_exact.png`（11.85 s，符合契约）；发布包 `chibi.fdraw` @1024 + egl → GPU 7.87 s。
- 测试：全量 **146 tests** 三套件全绿（workbuddy 默认 146/16/0/104.3s；ComfyUI python 146/17/0/80.7s；
  `RUN_GPU=1` 子集 **23/1/0/29.5s**，新增 4 项 egl/env 回归）。发布包干净环境（不含 work/training
  演示图）**Ran 143 / skipped 18（workbuddy）/19（ComfyUI）** 全绿。

**注意（预期管理）**：GPU 只加速**最终渲染**；大图总耗时仍包含 CPU 的 `layered_base` 笔画生成
（约 2.5 min）+ `.fbs` 傅里叶编译（100k 笔 / 1536 约 11 min）+ `pack_fdraw` 等，不能只看渲染段。

**其它不变**：节点版本 3.1.0、workflow JSON、画法与质量结论（v1.36 质量 CONDITIONAL −0.486dB 等）
均与 v1.0.1 相同。文档例图、赞助邮箱、ENGINEERING §10 工业扩展路线沿用 v1.0.1。

## 自检结果（`python work/release_docs/verify_release.py`，全 OK）

| 检查 | 结果 |
|---|---|
| zip 大小 ≤120MB / testzip / 文件数 | ✅ 20.8MB / None / 548 |
| 解压后逐文件 sha256 == 源候选目录 | ✅ 0 mismatch |
| 绝对路径（含日志/JSON 转义变体） | ✅ 0 |
| 失效相对链接 / MD UTF-8 | ✅ 0 / 全部可解码 |
| 禁用目录（`__pycache__`/`work`/`training`/`dist`/`.dsh`） | ✅ 0 |
| 包内所有 .json 可 `json.load` | ✅ |
| MANIFEST / SHA256SUMS 一致性 | ✅ 546 条全匹配 |
| 节点 3.1.0 / 5 workflows / 6 文档 / examples+artifacts | ✅ |

## 配套 skill 组件包

- `dist/fourier-multistroke-training-skill-v1.38.1-gpu-envfix.zip`
  - **SHA256 `A591E0DC28C6D05E90F363D0204A416A6EAD9DF2D714EC75ED5C9B1FADD6DC38`**
  - 8,002,325 B / 54 文件；与 v1.38 包结构相同，仅 4 个文件变化：
    `SKILL.md`、`scripts/gpu_render.py`、`tests/README.md`、`tests/test_gpu_render.py`。
  - 自检：testzip OK、逐文件 sha 与源树一致、包内文档绝对路径/死链 0（`work/docs7/` 脚本）。

## 使用

与 v1.0.1 相同；**GPU 用户请更新到本版**：解压覆盖原发布包 → 重跑
`release\install.py --comfyui "<ComfyUI>"` → 重启 ComfyUI（节点更新需重启；GPU 脚本修复本身无需重启，
下次 Queue 生效）。验证：节点日志出现 `[FDP-T2I] GPU render ok ...`，输出目录有 `*_gpu.png` +
`*_gpu_timing.json` 且无 `*_exact.png`。

## 已知限制

- 画质：v1.36 人类画法相对 v1.35 冻结基线 content **−0.486dB**（CONDITIONAL）；剪影羽化 −0.46dB。
- GPU 非 bit-exact（PSNR 40.2/41.96/44.1dB、SSIM≥0.9957）；依赖 PyOpenGL+glfw+桌面 GL 4.x；
  headless 自动回退 CPU；alpha<1/region_fill 文档回退 CPU（exit 5/4）。
- **`PYOPENGL_PLATFORM` 仅 Windows 强制清除**；Linux/macOS 仅防御重试；其它 GPU 回退根因需看日志。
- 大图总耗时以 CPU 阶段（layered_base + fbs 编译 + pack）为主；GPU 只加速最终渲染。
- 训练/评估渲染与视频渲染未 GPU 化；仅 Windows+ComfyUI 实测。
- 工业设备扩展（ENGINEERING §10）为路线图，当前未实现导出器。

## 复现/再打包

```powershell
$env:FDP_RELEASE_DIR="fourier-drawing-suite-v1.0.2"; $env:FDP_RELEASE_VERSION="1.0.2"
python work/release_docs/build_manifest.py
python work/release_docs/build_zip.py --force
python work/release_docs/verify_release.py
# 组件包：python work/docs7/build_package.py && python work/docs7/verify_pkg.py
```
