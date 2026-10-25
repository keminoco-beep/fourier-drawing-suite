# task-40 准备：发布包跨环境兼容性测试矩阵 + Runner 设计

状态：**准备阶段**（不 claim）。task-39（fixer 可移植改造）进行中；
`release/install.py`、`release/check_env.py`、env `FDP_WORKSPACE`/`FDP_PYTHON`、
`fdp_config.json`、节点 3.1.0 尚未落地。等 Lead 通知 task-39 complete → claim →
用最终候选/脚本冻结 runner → 跑矩阵（预计 30–40 min）→ `work/release_test/REPORT.md` +
`summary.json` + logs + PNG。

## 一、被测对象 / 环境

| 角色 | 路径 |
|---|---|
| 发布候选（只读） | 优先 `release/fourier-drawing-suite-v1.0.0/`（task-41 布局）；否则 `release/` 根（task-39 布局，workspace/comfyui/release）；否则 env `FDP_CANDIDATE`；再否则按 task-39 约定自建 `work/release_test/candidate/` |
| “别人的 ComfyUI python” | `<comfyui>\ComfyUI-aki-v1.7\ComfyUI-aki-v1.7\python\python.exe`（3.11.9，numpy/scipy/PIL/torch/glfw/PyOpenGL，GL 3.3 NVIDIA） |
| 无 GL python | workbuddy python 3.13.14（有 numpy/scipy/PIL，无 PyOpenGL → GPU 不可用） |
| 缺依赖 python | `work/release_test/tmp/empty_venv`（`python -m venv` 不装包 → numpy 缺失，测 check_env 报错） |
| 输入夹具 | `work/release_test/fixtures/input_shapes_320x256.png`（白底圆/三角/细条，触发 silhouette） |

所有复制/临时配置写在 `work/release_test/tmp/`；**不得修改被测候选目录**（只读）。

## 二、测试矩阵

| ID | 环境/位置 | 场景 | 判定 |
|---|---|---|---|
| A1 | 短英文路径（`<path>` 或 `tmp/short`） | `check_env.py` → PASS；小链路（layered_base 192 → build_strokes_fbs → render CPU 256） | 全 rc=0，≤5min |
| A2 | 空格+中文（`<path> 发布 测试\有空格 中文路径\workspace`） | 同上 | 全 rc=0 |
| A3 | 长路径 ≥120 字符（多层英文嵌套） | 同上 | 全 rc=0；记录总路径长度 |
| B1 | 仅 env：`FDP_WORKSPACE` + `FDP_PYTHON` 指向候选 | `check_env` + node `_workspace_paths`/小链路 | 解析成功 |
| B2 | 仅 config：`fdp_config.json`（installer 写） | 清空 env，`check_env --comfyui` + node 调用 | 解析成功 |
| B3 | 都没有（env/config 均空，候选不在 ComfyUI 目录下） | `check_env` / node `_workspace_paths` | **失败但可操作**（含 FDP_WORKSPACE/installer 提示，不崩溃） |
| C1 | fake ComfyUI（`tmp/fake_comfyui/custom_nodes/`） | `install.py --comfyui ... --workspace <候选> [--dry-run]` | 节点包复制、fdp_config.json 写入（workspace/python）、dry-run 不改盘 |
| C2 | C1 后 | `check_env.py --comfyui <fake>` | PASS |
| C3 | C1 后（ComfyUI python） | node `FDPT2IBuildFromImageV136._run`(256) → `FDPT2IPackFdraw._run` → `FDPT2IRenderFdraw._run`（GPU） | 返回 tensor/文件；GPU 日志 |
| C4 | C3 同参数 | `FDP_GPU_RENDER=0` RenderFdraw | CPU exact 成功（记录耗时） |
| D1 | 候选（env 指向） | `layered_base.py` 输入夹具 `--work-res 256 --silhouette auto`（或默认） | rc=0、report/PNG、≤60s |
| D2 | D1 输出 | `build_strokes_fbs.py --strokes ... --input ... --resolution 512` | rc=0、fbs JSON |
| D3 | D2 输出 | `pack_fdraw.py` → `fdrender` CPU exact 512 | rc=0、PNG 512 |
| D4 | D2/D3 输出 | `gpu_render.py --fbs/--fdraw 512 --device gl`（ComfyUI python）与 `--device cpu` | rc=0、尺寸对、计时 JSON |
| D5 | D3 fdraw | node `FDPT2IFourierVideo._run`(2s/512)（若视频脚本/依赖缺失 → SKIP 说明） | 成功或记录 SKIP 原因 |
| D6 | 候选 `comfyui/workflows/*.json` | 3 个 `FDP_v136_*` + 旧工作流结构校验（节点类型/链接/widget 数，`validate_workflows.py`） | 全部 PASS；RenderFdraw widget=1 |
| E1 | empty venv | `check_env.py`（FDP_PYTHON ↔ venv） | FAIL 且给出 `pip install -r requirements-core.txt` 类建议；不崩溃 |
| E2 | workbuddy python | `gpu_render.py --device gl` | **exit 3** + stderr 清晰 |
| E3 | ComfyUI python | `gpu_render.py --strokes alpha_doc.json`（alpha=0.55） | **exit 5** + CPU `--device cpu` 成功 |
| E4 | ComfyUI python | node RenderFdraw `FDP_GPU_RENDER=0` | CPU fallback 成功（另见 C4） |
| F1 | 候选 skill tests，ComfyUI python | `python -m unittest discover -s tests` | 142 tests / 0 fail / 13 skip |
| F2 | 候选 skill tests，workbuddy python | 同上 | 全绿（跨 3.11/3.13）；GPU 组 skip |
| F3 | 候选，ComfyUI python | `RUN_GPU=1 python -m unittest tests.test_gpu_render` | 19 tests / 0 fail / 1 skip |
| F4 | 原工作区（候选不含 training） | legacy v1.33 `30CDCE74…` / v1.34 `69820EEF…` canonical 复跑 | SHA 一致 |

## 三、Runner 设计（`work/release_test/run_release_tests.py`）

- 通用 `Step` 结果：`{id, group, env, scenario, status(PASS/FAIL/SKIP), seconds,
  detail, artifacts[], command}` → `summary.json` + `REPORT.md` 表格。
- 工具函数：
  - `copy_candidate(dst)`：robocopy/shutil 复制候选（排除 `__pycache__`、`out/`、
    `work/`、`dist/`、`*.mp4`、`.dsh`），保留结构；记录文件数/大小/耗时；
  - `run(args, env=..., cwd=..., timeout=...)`：统一子进程（capture、耗时、rc）；
  - `python(name)`：`comfy` / `wb` / `empty_venv`；
  - `check_json` / `assert_file` / `psnr_ssim` / `workflow_validate`；
  - `with_env(**kv)` 上下文，避免污染 runner 自身环境（FDP_* 变量）。
- 分组顺序：F4 legacy（后台）/ A / B / C / D / E / F1-F3（重）；
  每个 step 独立超时（默认 120s，重步骤 600s），失败不中断矩阵（记录 FAIL 继续）。
- 结果产出：`work/release_test/{REPORT.md, summary.json, logs/<step-id>.log,
  artifacts/<step-id>/*}`（关键 PNG：D1 render、D3 fdrender、D4 gpu、C3 node render）。
- 只读候选：所有临时 config/副本放 `tmp/`；install 只写 fake ComfyUI。

## 四、待冻结（task-39/41 落地后确认）

1. `release/install.py` / `check_env.py` 的精确 CLI（`--comfyui/--workspace/--python/--dry-run`）与退出码；
2. `fdp_config.json` 字段名/位置（节点目录；`{"workspace","python"}`）；
3. 节点 3.1.0 的 `_workspace_paths()` 解析顺序与环境变量名；
4. 候选实际布局（`release/fourier-drawing-suite-v1.0.0/` vs `release/`）及
   `comfyui/workflows/` 文件名（3 个 FDP_v136_* + 旧版）；
5. skill tests 在候选布局中的运行方式（cwd=候选 `skills/fourier-multistroke-training`）；
6. 视频节点依赖（imageio-ffmpeg 在 ComfyUI python 是否可用；视频脚本是否随包）。

## 五、风险

- 候选体积大 → 3 次重定位复制耗时；用 robocopy /MT + exclude 控制，单次目标 ≤60s。
- ComfyUI python 跑候选测试时 `sys.executable`=ComfyUI python（有 GL）→ 默认套件 GPU
  组仍 skip（RUN_GPU 未设），符合预期。
- fake ComfyUI 无 ComfyUI 运行时（folder_paths 缺失）→ 节点 import 已 try/except；
  `_output_base()` 回退 cwd/output，测试用显式 run_dir 避免。
- 缺依赖模拟用空 venv（无 numpy）→ check_env 应 FAIL 且给 pip 建议；若 check_env 本身
  需要 numpy 会直接崩 → 记录为问题。
