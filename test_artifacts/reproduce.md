# test_artifacts 复现命令

> 说明：证据日志/JSON 中的本机路径已在打包时脱敏为 <workspace>/<comfyui>/<python> 等占位符（原值来自测试机）。

> 所有命令在**发布包根目录**执行（`workspace/skills/...` 相对路径）；python 用 ComfyUI 自带 python。

## 1. 单元/契约测试（142 tests）
```powershell
cd workspace\skills\fourier-multistroke-training
python -m unittest discover -s tests
# 期望 Ran 142 tests, OK (skipped=13)；RUN_SLOW=1 → 3/3；RUN_GPU=1 → tests.test_gpu_render 19/0/1
```

## 2. 各版本测试基线（unit/）
基线文件来自各版本 `work/tests*/baseline_result_*.md`（构建时归档）。复跑方法同上；
GPU 用例需 ComfyUI python（含 PyOpenGL/glfw），workbuddy 环境会自动 skip。

## 3. 独立验收 VERDICT（verdicts/）
来自 `work/verify4..7/VERDICT.md`（v1.35–v1.38）。**归档文件**：原验收脚本在 `work/`（发布包不含）；
复现验收需按 VERDICT 内引用的原始工作目录。核心断言（可手工验证）：
```powershell
# legacy 逐字节
python -m unittest tests.test_gradient_safety -v
# GPU vs CPU 质量（有 GL）
python workspace\skills\fourier-multistroke-training\scripts\gpu_render.py --fdraw examples\chibi\chibi.fdraw --out %TEMP%\gpu.png --resolution 1024
```

## 4. 指标 JSON（metrics/）
- `sunflowers_metrics_v135.json`：v1.35 冻结基线（runE_dabs）
- `humanpaint_v136_metrics.json`：v1.36 人类画法前后指标
- `silhouette_metrics_v137.json`：chibi 剪影越界/污染指标
- `gpu_quality_v138.json`：GPU vs CPU PSNR/SSIM + 计时
- `gpu_R*_timing.json`：GPU 计时拆分

复现：重跑对应版本命令（见 `workspace/skills/fourier-multistroke-training/SKILL.md` 对应章节），
numpy/PIL 计算指标。

## 5. 对比图（images/）
为控体积已转 JPG（长边 ≤1600、质量 82），原始 PNG 在源项目 `work/`（发布包不含）。
复现：按 `SKILL.md` 各版本「实测」章节的命令重跑并截图/对比。

## 6. 发布兼容测试（release_test/）
来自 `work/release_test/REPORT.md`（跨路径/空格中文/安装模拟/全链路/降级/回归）。
复现需完整发布包 + ComfyUI python；详见该 REPORT 的矩阵与命令列。
