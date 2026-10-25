# 发布包说明：fourier-drawing-suite v1.0.0（2026-10-25）

## 包信息

| 项 | 值 |
|---|---|
| 文件 | `dist/fourier-drawing-suite-v1.0.0-release.zip` |
| SHA256 | `EB91A818BBC84C6CF9E9AFAEC2660794243C0A62BA8735E6793AD40F356789A8` |
| 大小 | **18,597,759 字节（约 17.7 MiB）**；解压后约 **25.6 MB**（≤120MB 上限 ✅） |
| 文件数 | **546**（含 MANIFEST.json + SHA256SUMS.txt；MANIFEST 收录 544 个载荷文件） |
| 顶层目录 | 解压后为 `fourier-drawing-suite-v1.0.0/`（单层，无嵌套包） |
| 打包脚本 | `work/release_docs/assemble.py` + `build_manifest.py` + `build_zip.py` |
| 自检脚本 | `work/release_docs/verify_release.py` |

> **task-43 重打说明（verifier CONDITIONAL PASS 修复后版本）**：上一版
> `4ACABA69…35ECB1`（19,643,755 B）已作废。本版：①JSON-aware sanitizer——包内
> 所有 `.json` 先 `json.load` 再替换字符串路径，修后全部可 `json.load`（含
> `test_artifacts/release_test/summary.json`、`fourier-brushstroke/.../acceptance/report.json`）；
> ②证据日志/JSON 中本机路径全部脱敏为 `<workspace>`/`<comfyui>`/`<python>`/`<path>`
> （`README`/`reproduce.md` 已注明）；③examples `render_R2048.png` 改为由包内
> `chibi.fdraw` 重新渲染（GPU 5.1s）并与 `commands.md` 自洽；④P3 文档小修
> （卸载 `--yes`、139/16 vs 142/13 说明、`workspace_python()`、示例相对路径、legacy
> canonical python 口径）。

## 包内容清单

```
fourier-drawing-suite-v1.0.0/
├── README.md / INSTALL.md / USER_GUIDE.md / ENGINEERING.md / CHANGELOG.md / NOTICE.md
├── release/             install.py、check_env.py、uninstall.py、requirements-core/gpu/comfyui.txt、config.example.json
├── workspace/
│   ├── skills/fourier-multistroke-training/   skill v1.38-gpu-render（SKILL/references/assets/tests 54 文件）
│   ├── fourier-brushstroke/                   scripts/SKILL/references/tests/LICENSE（排除 assets/out）
│   ├── fourier-prompt/                        包/pyproject/LICENSE/assets(fonts+brush)/comfyui 副本（排除 out/、LLM LoRA）
│   └── poc/t2i/fdraw_fourier_video.py          作画视频脚本（FDPT2IFourierVideo 节点调用）
├── comfyui/
│   ├── custom_nodes/ComfyUI-FDP-T2I/         可移植节点包 **3.1.0**（FDP_WORKSPACE/FDP_PYTHON/config 解析）
│   └── workflows/                             5 个：FDP_v136_Local_NoAI_UI、FDP_v136_T2I_Krea2_Local_UI、
│                                              FDP_v136_T2I_Krea2_API_UI、FDP_T2I_Krea2_fdraw_UI（旧版保留）、
│                                              FDP_fdraw_to_fourier_video_UI
├── examples/chibi/      source.png / strokes.json(1.42MB) / chibi.fdraw(3.73MB) / layered_base_R512.png /
│                        render_R2048.png / report.json / commands.md
├── test_artifacts/
│   ├── unit/            v1.33–v1.38 各版本测试基线 + TEST_COMMANDS.md
│   ├── verdicts/        v1.35–v1.38 独立验收 VERDICT
│   ├── metrics/         sunflowers/silhouette/humanpaint/gpu 质量与计时 JSON
│   ├── images/          chibi/剪影/GPU 对比/lineart/Sunflowers 精选图（jpg）
│   ├── release_test/    task-40 跨环境矩阵：REPORT.md、summary.json、PLAN.md、logs/(35)、artifacts/(13)
│   └── reproduce.md     所有产物的复现命令
├── MANIFEST.json        544 个载荷文件的 path/size/sha256 + 组件版本
└── SHA256SUMS.txt       sha256sum -c 兼容
```

## 自检结果（`python work/release_docs/verify_release.py`，全 OK）

| 检查 | 结果 |
|---|---|
| zip 大小 ≤120MB / testzip / 文件数 | ✅ 17.7MB / None / 546 |
| 解压后逐文件 sha256 == 源候选目录 | ✅ 0 mismatch |
| 绝对路径（`C:\Users` / `G:\AI` / 正斜杠与 JSON 转义变体、含日志） | ✅ 0 命中 |
| 失效相对链接（所有 .md） | ✅ 0 |
| 禁用目录（`__pycache__`/`work`/`training`/`dist`/`.dsh`） | ✅ 0 |
| 文档 UTF-8 | ✅ 全部可解码 |
| **包内所有 .json 可 `json.load`** | ✅ 0 无效（含 summary.json / report.json） |
| MANIFEST / SHA256SUMS 一致性 | ✅ 544 条全匹配 |
| 节点版本 3.1.0 / 5 workflows / 文档齐全 / examples + artifacts | ✅ |

> 证据日志/JSON 中的本机路径已脱敏（`<workspace>`/`<comfyui>`/`<python>`/`<path>`），
> 不影响可读性与复现；`examples/chibi/render_R2048.png` 由包内 `chibi.fdraw` 重新渲染。

**跨环境矩阵（task-40，`test_artifacts/release_test/REPORT.md`）**：**24 PASS / 0 FAIL / 0 SKIP / 592.5s**。
覆盖：3 路径（纯英文/空格+中文/长路径 156 字符）、配置解析（env/config/可操作报错）、安装模拟
（install.py→假 ComfyUI→节点 `_run` GPU 28.7s / CPU fallback 44.7s）、全链路（layered_base→fbs→
fdraw→CPU/GPU 渲染）、节点视频 2s/512（63.3s，mp4 258KB + poster）、5 workflow 结构校验、
依赖降级（无 GL exit 3 / alpha<1 exit 5）、回归（候选默认套件 F0 139/16 因发布包按规则不含
work/training 演示图；F1 ComfyUI python 142/14；F2 workbuddy 142/13；F3 GPU 子集 19/1；
legacy v1.33/v1.34 逐字节一致）。

## 安装/使用

见包内 `README.md`（5 分钟快速开始）、`INSTALL.md`（安装/依赖/卸载/离线/常见失败）、
`USER_GUIDE.md`（零基础完整功能说明 + 参数 + 10 条 FAQ）。最短路径：

```powershell
cd fourier-drawing-suite-v1.0.0
"<ComfyUI python>" release\install.py --comfyui "<ComfyUI 目录>"
"<ComfyUI python>" release\check_env.py --comfyui "<ComfyUI 目录>"     # PASS/WARN/FAIL，不联网
# 重启 ComfyUI → 打开 comfyui\workflows\FDP_v136_Local_NoAI_UI.json
```

## 已知限制（与包内 CHANGELOG 一致，不夸大）

- 画质：v1.36 人类画法相对 v1.35 冻结基线 content **−0.486dB**（质量子项 CONDITIONAL）；
  剪影羽化另一 **−0.46dB** 同参考口径代价；A 区 largest_flat 0.222→0.416。
- GPU 渲染非 bit-exact（PSNR 40.2/41.96/44.1dB、SSIM≥0.9957），依赖 PyOpenGL+glfw+桌面 GL 4.x；
  headless 自动回退 CPU；alpha<1/region_fill 文档回退 CPU（exit 5/4）。
- 训练/评估渲染与视频渲染未 GPU 化；仅 Windows+ComfyUI 实测；无 mac/Linux 验证。
- 发布包按规则剔除 `work/`、`training/`、`dist/` 旧包、`__pycache__`、LLM LoRA、demo 视频；
  因此测试产物中 F0 场景为 139 ran/16 skip（原因已在 release_test REPORT 中说明）。

## 复现/再打包

```powershell
python work/release_docs/assemble.py --force      # 从源工程重建 release/fourier-drawing-suite-v1.0.0
python work/release_docs/build_manifest.py        # MANIFEST.json + SHA256SUMS.txt
python work/release_docs/build_zip.py --force     # dist/fourier-drawing-suite-v1.0.0-release.zip
python work/release_docs/verify_release.py        # 全项自检
```
