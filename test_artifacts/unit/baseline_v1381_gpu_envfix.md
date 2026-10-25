# v1.38.1-gpu-envfix 回归基线（2026-10-25）

> 任务：P0 修复 ComfyUI 宿主 `PYOPENGL_PLATFORM=egl` 导致 GPU 渲染静默回退 CPU。
> 证据（源工程，路径已脱敏）：`work/gpu_env_fix/`、`work/tests7/`。修复文件：
> `workspace/skills/fourier-multistroke-training/scripts/gpu_render.py` + `tests/test_gpu_render.py`。

## 根因与修复

- 宿主 env `PYOPENGL_PLATFORM=egl` 被子进程继承 → Windows 无 EGL → `from OpenGL import GL` 失败 →
  `gpu_render.py` exit 3 → 节点自动回退 CPU exact（100k 笔 4096 实测 **3159 s ≈ 52.6 min**）。
- 修复：Windows 下 import OpenGL/glfw 前清除该变量；GL 探测防御重试一次；**不改 nodes.py**，
  **无需重启 ComfyUI**（每次 Queue 用新子进程）。

## 验证结果

| 项 | 结果 |
|---|---|
| egl env 下 gpu_render（小样本） | rc0（修复前 exit 3）；输出与 clean env **像素完全一致**（maxdiff 0.0） |
| 真实部署节点 4096（100k 笔） | **56.4 s wall / 50.2 s 渲染**；生成 `*_gpu.png` + `*_gpu_timing.json`；**无 `*_exact.png`**；tensor (1,4096,2913,3) finite |
| 节点日志 | `[FDP-T2I] GPU render ok res=4096` |
| 全量测试（workbuddy python） | **Ran 146 tests, OK (skipped=16)** / ~103 s |
| 全量测试（ComfyUI python） | **Ran 146 tests, OK (skipped=17)** / ~81 s |
| GPU 子集（ComfyUI python，`RUN_GPU=1`） | **23 tests / 0 failed / 1 skipped** / ~31 s（含新增 4 个 egl 回归用例） |
| 无 GL 环境 | 仍按契约 exit 3（行为不变） |

新增回归用例（`tests/test_gpu_render.py`）：
1. `test_source_clears_hostile_pyopengl_platform`（默认套件也运行）：脚本在任何 OpenGL/glfw import 前清除 env；
2. `test_egl_env_does_not_break_gpu`：子进程 egl env → rc0、非 exit 3、PNG 非空白；
3. `test_egl_env_output_matches_clean_env`：egl 与 clean 两次渲染像素完全一致；
4. `test_hostile_pyopengl_platform_uses_gpu`：进程内设置 egl 后调用节点 `_run` → 走 GPU、无 `_exact.png` 回退。
