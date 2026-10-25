# 测试基线结果（v1.38 GPU 渲染）—— fourier-multistroke-training

> 定稿时间：2026-10-25 10:0x（Asia/Shanghai）。
> 被验收代码：`scripts/gpu_render.py` SHA256
> `B55A1B790C864B268D514E7B9430983E5C56BAE82DBAA251ED4FBB9B5D0C5F6E`
> （fixer-core task-33 冻结版）；`scripts/layered_base.py` 与 v1.37 相同（v1.38 未改）。
> ComfyUI 节点包 `ComfyUI-FDP-T2I` PACKAGE_VERSION **3.0.1**（repo 副本与部署副本一致，
> fixer 复核 hash `8FE41719564CDAA5…`）。
> 写域：`skills/fourier-multistroke-training/tests/` + `work/tests6/`。
> 参考：`work/gpu_render/FIX_REPORT.md`、`work/gpu_render_eval/ANALYSIS.md`。

## 结论（先看这里）

1. **默认套件（workbuddy python，无 PyOpenGL）全绿**：
   **142 tests / 0 failed / 13 skipped（3 慢 + 10 GPU）/ 80.0 s**；
   `RUN_SLOW=1` **3/3 OK / 9.9 s**。GPU 用例默认 **skip**，未破坏原 121 用例。
2. **GPU 子集（ComfyUI python, `RUN_GPU=1`）全绿**：`test_gpu_render.py`
   **19 tests / 0 failed / 1 skipped（无 GL 用例在 GL 环境跳过）/ 19.7 s**。
3. **GPU vs CPU 等价（本仓库独立复算）**：合成 strokes 文档（5 笔：flat/gradient/
   细笔/round cap，alpha=1）**PSNR 62.29 dB / SSIM 1.0000**（门槛 32/0.98）；
   tiny `.fdraw` 256 → **35.395 / 0.9990**、512 → **35.085 / 0.9990**；白底保持、
   尺寸一致。fixer/analyst 在 chibi 大文档上：1024 **40.219/0.99566**、
   4096 **44.109/0.99724**（差图仅边缘 AA）。
4. **真实加速（独立复测）**：chibi `.fdraw` GPU 1024 **total_s=14.114**（fixer 14.2 /
   analyst 14.5，一致）；medium `.fdraw`（28,408 笔）512：GPU **9.05 s** vs
   CPU exact **91.72 s**（10.1×）；1024：GPU **9.24 s**。fixer/Lead 大基准：
   1024 14.2 s / 2048 15.1 s / 4096 23.6 s vs CPU 219 / 581.6 / ≈1260 s
   → **15.4× / 38.5× / 53×**。
5. **降级路径按契约**：无 PyOpenGL（workbuddy python）→ **exit 3**（0.28 s，stderr 清晰，
   无 PNG）；`alpha<1` 文档 GPU → **exit 5** + `alpha<1 not supported by GPU renderer`，
   CPU `--device cpu` 正常渲染；节点把 3/4/5/超时当 GPU 不可用 → fdrender exact
   （`FDP_GPU_RENDER=0` 实测 tiny 走 CPU 成功，tensor `(1,256,256,3)`）。
6. **节点契约**：`PACKAGE_VERSION == "3.0.1"`；`FDPT2IRenderFdraw` primitive widget
   仅 `resolution`（**=1**）；shipped UI workflow `widgets_values` 长度仍为 1
   （**workflow 未改**）；GPU 路径 log `[FDP-T2I] GPU render ok`。
7. **legacy 逐字节不变**：v1.33 canonical PNG `30CDCE74…` ✅（81,815 笔 / 23.1883 /
   0.61788）；v1.34 `69820EEF…` ✅（99,929 笔 / 24.3628 / 0.62545）。
8. **已知限制（如实记录，未放宽断言）**：D-1 GPU 仅 alpha=1（<1 → exit 5→CPU，按裁决）；
   D-2 极小文档 GPU 因启动开销略慢于 CPU（速度随分辨率/笔数放大）；D-3 非 round 笔形按
   round 近似（report `non_round_shapes`）；D-4 region_fill 文档 exit 4→CPU；
   D-5 `--device cpu` 对 strokes 用 `hatch_gen`、对 fdraw/fbs 用 `fbs_core`（两种 CPU
   参考实现，测试只在同一输入类型内对比）；D-6 `fbs.py render --device` 由 fixer 验证，
   非本套件覆盖范围。

## 命令与耗时（16 核 Windows，实测）

```powershell
Set-Location "skills/fourier-multistroke-training"
python -m unittest discover -s tests -v      # 142 tests, OK (skipped=13), 80.0 s
$env:RUN_SLOW="1"; python -m unittest tests.test_slow_real_image -v
                                             # 3 tests, OK, 9.9 s
# GPU 子集（ComfyUI python：PyOpenGL+glfw+桌面 GL）
$env:RUN_GPU="1"; "<ComfyUI-python.exe>" -m unittest tests.test_gpu_render -v
                                             # 19 tests, OK (skipped=1), 19.7 s
```

| 模块 | 用例 | 默认耗时 | 说明 |
|---|---|---|---|
| `test_layered_base.py` | 18 | ~6 s | 不变 |
| `test_contracts.py` | 26 | ~27 s | +v1.38 FROZEN/choices/文档门禁（`gpu_render.py --help` 在无 GL 下仍 0，32 脚本契约自动覆盖） |
| `test_gradient_safety.py` | 7 | ~9 s | 不变 |
| `test_texture_flat.py` | 17 | ~8 s | 不变 |
| `test_base_coat.py` | 19 | ~13 s | 不变 |
| `test_human_paint.py` | 18 | ~18 s | 不变 |
| `test_silhouette.py` | 15 | ~20 s | 不变 |
| **`test_gpu_render.py`** | **19** | **~2 s（10 skip）** | **新增 v1.38**；RUN_GPU 子集 ~20 s |
| `test_slow_real_image.py` | 3 | 0 s（skip） | RUN_SLOW |
| **合计** | **142** | **~80–100 s** | 13 skip（3 慢 + 10 GPU） |

## 一、新增用例（tests/test_gpu_render.py，19）

默认（workbuddy python，无 GL）：**9 run / 10 skip**；ComfyUI python `RUN_GPU=1`：
**18 run / 1 skip**（无-GL 用例在 GL 环境跳过）。

| 类 | 用例 | 断言 |
|---|---|---|
| `TestGPUInterface`（4） | help / frozen defaults / 输入互斥 / 无 GL | `--help` 列出 `--fdraw/--fbs/--render-json/--strokes/--out/--resolution/--supersample/--device/--background/--width-scale/--timing-json`；冻结默认 `resolution=1024`、`supersample=2`、`device=gl`、`background=None`、`width_scale=None`，choices 对齐；两个输入 → **exit 2**；无 GL → 渲染 **exit 3** + stderr 含 OpenGL/GL 关键字 + 不产 PNG，`--help` 仍 **0** |
| `TestCPUDevice`（2） | CPU 渲染 / 计时 | `--strokes + --device cpu` 成功、尺寸正确；计时 JSON 含 `load_s/render_s/total_s`、`device="cpu"` |
| `TestGPURenderQuality`（4） | GPU vs CPU / 计时字段 / SS+分辨率 / strokes 路径 | GPU 与 CPU 同文档：形状一致、**PSNR≥32 / SSIM≥0.98**（实测 62.29/1.0000）、白底角保持；计时 JSON 含 `load/sample/geometry/gl_init/gpu/readback/total`（`*_s` 实际名 + `segments/supersample/device`）；`--supersample {1,2,3}` + `--resolution {192,320,256}` 出图长边正确；`--strokes` 输入路径可用 |
| `TestFdrawInputPath`（1） | `.fdraw` 输入 | 3.4KB tiny fdraw GPU 渲染成功、尺寸正确 |
| `TestGPURefusesTranslucentDocs`（2） | alpha<1 → exit 5 / CPU 仍可 | GPU `--strokes` 含 alpha=0.55 笔 → **exit 5** + stderr 含 alpha/回退说明、无 PNG；`--device cpu` 同一文档渲染成功 |
| `TestNodePackageContract`（3） | 版本 / widget / workflow | `PACKAGE_VERSION == "3.0.1"`；`FDPT2IRenderFdraw.INPUT_TYPES()` primitive widget 仅 `["resolution"]`；shipped UI workflow 中 RenderFdraw `widgets_values` 长度 == 1（≥1 个 workflow 被检查） |
| `TestNodeGpuFallback`（3） | GPU 路径 / 强制 CPU / alpha fdraw fallback | tiny fdraw：正常 `_run` 返回 `(1,256,256,3)` tensor 且 stdout 含 GPU 日志；`FDP_GPU_RENDER=0` 仍成功且不用 GPU；alpha<1 fdraw（build_strokes_fbs+pack 生成）→ GPU exit 5 → 节点自动回退 CPU exact 成功且不打印 GPU ok |

契约更新：`test_contracts.py` 新增 `FROZEN_GPU_RENDER_DEFAULTS/CHOICES`、
`test_gpu_render_interface_frozen`、`test_gpu_render_options_documented`（SKILL.md v1.38
章节已存在 → 不 skip）；32 脚本 `--help`/`py_compile` 契约自动包含 `gpu_render.py`。

## 二、GPU vs CPU 质量/时间（本仓库独立复算）

证据：`work/tests6/gpu_evidence.json`、`work/tests6/logs_gpu_evidence.txt`。

| 文档 | 分辨率 | GPU 时间 | CPU 时间 | PSNR | SSIM | max_abs |
|---|---|---|---|---|---|---|
| 合成 strokes（5 笔，gradient+圆头，alpha=1） | 256 | total_s 0.652 | 0.352 s | **62.285** | **0.99999** | 0.078 |
| tiny `.fdraw`（30 笔） | 256 | 0.873 s | 2.852 s | **35.395** | **0.99900** | 0.082 |
| tiny `.fdraw` | 512 | 0.962 s | 7.258 s | **35.085** | **0.99901** | 0.090 |
| medium `.fdraw`（28,408 笔） | 512×432 | **9.048 s** | **91.719 s** | — | — | — |
| medium `.fdraw` | 1024×864 | **9.242 s** | —（>5 min） | — | — | — |
| chibi `.fdraw`（38,737 笔，ComfyUI run） | 1024×1002 | **14.114 s** | 219 s（Lead） | 40.219（fixer，1024） | 0.99566 | — |

计时拆解（chibi 1024，`*_s`）：`load 3.504 / sample 8.970 / geometry 0.519 /
gl_init 0.274 / gpu 0.107 / readback 0.385 / total 14.114` —— 采样+load 占约 88%，
与 fixer 分析一致（后续可缓存采样优化）。medium 512 CPU/GPU = **10.1×**；
极小文档 GPU 反而慢（GL 初始化 ~0.25 s 固定开销，D-2）。

fixer/analyst 大基准（引用）：1024 **14.2 s**、2048 **15.1 s**、4096 **23.6 s**；
CPU 219 s / 581.6 s / ≈1260 s → 15.4× / 38.5× / 53×；质量 1024 40.219/0.99566、
2048 代理 40.542/0.99458、4096 44.109/0.99724；4× 视觉对照
`work/gpu_render/gpu_vs_cpu_4x_crops.png` 无新锯齿/色带/漏笔。

## 三、降级 / fallback 证据

| 场景 | 结果 |
|---|---|
| workbuddy python（无 PyOpenGL）GL 渲染 | **exit 3**（0.28 s），stderr `GPU 渲染不可用（PyOpenGL/glfw 缺失…）；请用 --device cpu 或回退 CPU`，无 PNG |
| workbuddy python `--help` | **exit 0**（契约测试/文档可用） |
| alpha<1 文档 GPU | **exit 5**，stderr `alpha<1 not supported by GPU renderer; use --device cpu`，无 PNG |
| alpha<1 文档 CPU | 成功（render_s 0.352，256px） |
| 节点正常（ComfyUI python，tiny fdraw） | tensor `(1,256,256,3)`，log `[FDP-T2I] GPU render ok res=256 1.2s`（wall 4.01 s 含启动） |
| 节点 `FDP_GPU_RENDER=0` | tensor 成功，log `GPU render 未启用/脚本缺失，使用 CPU exact`（wall 3.47 s） |
| 节点 alpha<1 fdraw | GPU attempt exit 5 → 自动 CPU exact 成功（`test_translucent_fdraw_falls_back_to_cpu`） |
| fixer 大文档对照 | node GPU 1024 = 17.1 s（含解释器启动）；`FDP_GPU_RENDER=0` chibi 256 exact = 99.8 s（同旧逻辑） |

## 四、legacy 逐字节回归

| 场景 | 笔数 | PSNR / SSIM | PNG SHA256 | 期望 | 一致 |
|---|---|---|---|---|---|
| v1.33 canonical（gate off, quantized） | 81,815 | 23.1883 / 0.61788 | `30CDCE74F11A8E2C0CFBA7A0D760E49AC773DECB902180E2584EC17D6DEB9A01` | 同 | ✅ |
| v1.34 推荐（source + texture 37,000） | 99,929 | 24.3628 / 0.62545 | `69820EEF69386B8DC0561AF70DCA1363991D1A81D9C8CA8F52984E1CC4C265C3` | 同 | ✅ |

v1.38 未改 `layered_base.py`（GPU 渲染是独立脚本 + 节点集成），本节由
`work/tests4/_collect_evidence_v136.py` 复跑（87.5 s / 105.7 s）；证据
`work/tests4/regression_evidence_v136.json`、`work/tests6/logs_legacy_v138.txt`。

## 五、冻结接口同步位置

- `tests/test_gpu_render.py`：`FROZEN_GPU_DEFAULTS`（resolution 1024 / supersample 2 /
  device gl / background None / width-scale None）、`FROZEN_GPU_CHOICES`、
  `GPU_MIN_PSNR=32.0`、`GPU_MIN_SSIM=0.98`、`GPU_NO_ALPHA_EXIT=5`、
  计时阶段 `load/sample/geometry/gpu/readback/total`（`*_s` 前缀匹配）。
- `tests/test_contracts.py`：`FROZEN_GPU_RENDER_DEFAULTS/CHOICES` +
  `test_gpu_render_interface_frozen` + v1.38 文档门禁。
- `tests/README.md`：预算表（142/~80–100 s，13 skip）、覆盖矩阵 v1.38 行、
  RUN_GPU 命令、v1.38 契约表（CLI/退出码/计时/节点/实测/限制）。
- 证据：`work/tests6/{gpu_evidence.json, logs_full_final.txt, logs_slow_final.txt,
  logs_gpu_subset_v138.txt, logs_gpu_evidence.txt, evidence/}`；
  fixer：`work/gpu_render/FIX_REPORT.md`，analyst：`work/gpu_render_eval/ANALYSIS.md`。
