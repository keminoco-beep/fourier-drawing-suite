# ComfyUI-FDP-T2I（发布包 v3.1.0，可移植安装）

ComfyUI 自定义节点包：**Krea2/任意位图 → 傅里叶笔触 `.fbs` → `.fdraw` → GPU/CPU 渲染 / 作画视频**。
节点本身不新增 ComfyUI 的 pip 依赖（只用 ComfyUI 自带 torch/numpy/Pillow），
skill 脚本（`workspace/skills`、`workspace/fourier-brushstroke`、`workspace/fourier-prompt`）
通过外部 Python 子进程调用。

## 便携安装（推荐）

发布包布局：

```
<发布包根目录>/
  workspace/{skills, fourier-brushstroke, fourier-prompt}
  comfyui/{custom_nodes, workflows}
  release/{install.py, check_env.py, uninstall.py, requirements-*.txt}
```

1. 安装节点包（把 `comfyui/custom_nodes/ComfyUI-FDP-T2I` 复制到你的 ComfyUI，并写配置）：

   ```bash
   python release/install.py --comfyui "<path>"
   # 可选：--workspace <发布包根目录> --python <ComfyUI自带python> --dry-run
   ```

2. 安装核心依赖（ComfyUI 自带 python 通常已有；缺了 install.py 会给出 pip 命令）：

   ```bash
   "<ComfyUI python>" -m pip install -r release/requirements-core.txt
   # 可选 GPU 渲染依赖：-r release/requirements-gpu.txt
   ```

3. 重启 ComfyUI，导入 `comfyui/workflows/` 下的 workflow JSON（或在发布包中直接打开）。
4. 自检：

   ```bash
   python release/check_env.py --comfyui "<path>"
   ```

卸载：`python release/uninstall.py --comfyui "<path>" --yes`

## 路径解析顺序（不依赖本机绝对路径）

节点执行 skill 脚本时按以下顺序解析：

| 项目 | 顺序 |
|---|---|
| workspace | ① `FDP_WORKSPACE` 环境变量 → ② 节点目录 `fdp_config.json` 的 `workspace` → ③ 发布包布局自动探测（节点在 `<root>/comfyui/custom_nodes/…` → `<root>/workspace` 或 `<root>`）→ ④ 可操作报错（提示运行 install.py 或设置环境变量） |
| python | ① `FDP_PYTHON` → ② `fdp_config.json` 的 `python` → ③ ComfyUI 自带 python（`sys.executable`）→ ④ 依赖缺失时报错并给出 `pip install -r requirements-core.txt` 命令 |

其它环境变量：`FDP_CONFIG`（自定义配置文件路径）、`FDP_GPU_RENDER=0`（强制 CPU 渲染）、
`FOURIER_WS`（skill 脚本级覆盖，节点会自动注入）。

`fdp_config.json` 示例（install.py 会自动生成；模板见 `release/config.example.json`）：

```json
{ "workspace": "<发布包根目录>/workspace", "python": "<ComfyUI python 路径>" }
```

## 节点（7 个）

| 节点 | 输入 → 输出 | 说明 |
|---|---|---|
| `FDPT2IWorkflowStart` | 模式 + `krea2_image`/`manual_image`（均 lazy）→ `source_mode`、`run_dir`、`image` | 工作流入口：选文生图/图生图并创建任务目录 |
| `FDPT2IBuildFromImage` | IMAGE + RUN_DIR + `resolution`/`quality` → `.fbs` | 位图 → 傅里叶 `.fbs`（旧引擎） |
| `FDPT2IBuildFromImageV136` | IMAGE + RUN_DIR + `work_res`/`resolution`/`quality`/`texture_flat`/`thick_dabs`/`clip_to_fg`/`paint_order` → `.fbs` | v1.36 人类画师式纯笔画流水线（wash/flow/region，全本地无 AI） |
| `FDPT2IPackFdraw` | `.fbs` + `source_mode` + RUN_DIR → `.fdraw` | 打包容器（图生图不写自定义 provenance meta） |
| `FDPT2IRenderFdraw` | `.fdraw` + RUN_DIR + `resolution` → IMAGE | **GPU 渲染优先（OpenGL），失败/无 GL 自动回退 CPU exact**；`FDP_GPU_RENDER=0` 强制 CPU |
| `FDPT2IFourierVideo` | `.fdraw` + RUN_DIR + `size`/`circles`/`duration_s` → mp4 + poster | 默认 2160² / 16384 圆 / 30s |
| `FDPT2IFdrawPath` / `FDPT2IRunDir` | 独立 `.fdraw`/任务目录工作流用 | — |

所有产物落在同一任务目录 `ComfyUI/output/fdp_t2i_runs/<时间戳>_<t2i|i2i|video>_<id>/`。

## GPU 渲染（v1.38，可选）

- 需要 PyOpenGL + glfw + 桌面 OpenGL 上下文（ComfyUI python 环境通常已具备）；缺失时自动回退 CPU。
- 实测 chibi 38,737 笔：1024px ≈14–17s、2048px ≈15–18s、4096px ≈24–28s（CPU exact 分别为 219s / ≈582s / ≈21min）。
- GPU 与 CPU exact 同分辨率 PSNR 40.2dB@1024、44.1dB@4096（SSIM 0.995+）。
- 限制：**alpha<1 的文档 GPU 不支持**（退出码 5，节点自动回退 CPU exact；当前 skill 流水线输出 alpha=1）；
  含 `region_fill` 填充的文档走 CPU（退出码 4）；非 round 笔形按 round 近似。
- `gpu_render.py` 也可独立使用：
  `python workspace/skills/fourier-multistroke-training/scripts/gpu_render.py --fdraw X.fdraw --out X.png --resolution 1024`

## 输出 / 诊断

- 每个任务目录含 `source.png`、`build/…fbs.json`、`fdraw.fdraw`、`fdraw_R<r>_gpu.png`（或 `_exact.png`）、
  `fdraw_R<r>_gpu_timing.json`、`run.json`。
- 节点日志（ComfyUI 控制台）会打印 GPU/CPU 路径与耗时。
- 排障：`python release/check_env.py --comfyui <ComfyUI目录>` 会检查 workspace/python 依赖/GL/节点/配置/workflow。
