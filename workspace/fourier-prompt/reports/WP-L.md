# WP-L 报告 — ComfyUI 节点包骨架（ComfyUI-FDP）

- 日期：2026-10-20 ｜ 状态：**完成（mock + 静态契约验收；真实 ComfyUI 未验）**
- 任务：KICKOFF v5 §3（WP-L，共享 task-22）
- 新增：`fourier-prompt/comfyui/ComfyUI-FDP/`（`__init__.py`、`nodes.py`、`README.md`、
  `requirements.txt`、`workflows/*.json` ×4）、`fourier-prompt/tests/comfyui/`（3 个测试
  文件 + `_fixtures.py` mock 夹具）、本报告
- 未改动：skill、`fourier_prompt/` 核心（只读调用）、`cli.py`、`fdrender_cli.py`、
  `fdvideo.py`、`docs/`、`analysis/`

## 1. 交付物

| 文件 | 字节 | 内容 |
|---|---:|---|
| `comfyui/ComfyUI-FDP/__init__.py` | 3,289 | 8 节点导出、`input/output/fdraw` 注册、core 路径引导（`FDP_CORE_PATH` / 仓库相对路径）、`WEB_DIRECTORY=None` |
| `comfyui/ComfyUI-FDP/nodes.py` | 41,250 | 8 节点 + `FDrawValue` 数据类 + mock 友好的 host 适配 |
| `comfyui/ComfyUI-FDP/README.md` | 10,104 | 安装、类型、节点表、工作流、缓存、错误码、限制、真机冒烟 |
| `comfyui/ComfyUI-FDP/requirements.txt` | 328 | 仅注释：不新增 pip 依赖（防止 Manager 升级 numpy） |
| `comfyui/ComfyUI-FDP/workflows/fdsl_to_fdraw.json` | 3,834 | FDSL → FDRAW → IMAGE / 保存 |
| `comfyui/ComfyUI-FDP/workflows/fdraw_to_image.json` | 2,883 | FDRAW → IMAGE（Preview + SaveImage） |
| `comfyui/ComfyUI-FDP/workflows/fdraw_to_video.json` | 3,194 | FDRAW → 静态帧 + mp4 |
| `comfyui/ComfyUI-FDP/workflows/batch_fdraw.json` | 3,054 | 批量 FDRAW（list 输出）→ 渲染/保存 |
| `tests/comfyui/_fixtures.py` | 6,179 | 伪造 `folder_paths`/`comfy`、临时 `.fdraw` 夹具、包导入 |
| `tests/comfyui/test_package.py` | 7,489 | 11 例：映射/类型/V1 字段/async/指纹/目录注册/导入白名单 |
| `tests/comfyui/test_nodes.py` | 11,367 | 19 例：Load/Save/Compile/Render/Preview/Inspect/Batch/Video/错误/中断 |
| `tests/comfyui/test_workflows.py` | 7,717 | 6 例：4 工作流解析、节点/端口/link/widget 自洽 |

## 2. 实现要点

- **V1 API only**：`INPUT_TYPES` / `RETURN_TYPES` / `RETURN_NAMES` / `FUNCTION` /
  `CATEGORY` / `OUTPUT_NODE` / `OUTPUT_IS_LIST`；不使用 `comfy_api.latest`。
- **8 节点**：`FDPLoadFdraw`、`FDPSaveFdraw`、`FDPCompileFDSL`、`FDPRenderFdraw`、
  `FDPPreviewFdraw`、`FDPVideoFdraw`、`FDPInspectFdraw`、`FDPBatchFdraw`；
  `NODE_CLASS_MAPPINGS` 与 `NODE_DISPLAY_NAME_MAPPINGS` 一一对应。
- **类型**：`FDRAW = "FDRAW"`（内存值 `FDrawValue`：`render` + `source_fdsl` + meta +
  root hash + inspect 快照 + fonts bytes）、`FDSL = "FDSL"`（Compile 的可选连线输入；
  工作流用多行 JSON 文本作为默认来源）。
- **只调用公共 API**：`fdraw.unpack/inspect/pack/canonical_json_bytes/KNOWN_CAPABILITIES`、
  `compile_plan`、`repair_plan`、`render_fast.render_auto/truncate_doc`、
  `fdvideo.render_video`。没有复制编译器/容器/渲染逻辑，也没有 `vendor/` 快照。
- **执行模型（analysis/11 §4.6）**：8 个 `FUNCTION` 全部 `async def`，重活
  `await asyncio.to_thread(...)`；调用前检查 `comfy.model_management.processing_interrupted()`，
  命中抛 `InterruptedError("FD_E_INTERRUPTED: ...")`；`to_thread` 内不碰 torch/ComfyUI 全局状态，
  IMAGE 张量转换只发生在协程内。
- **IMAGE 适配**：真实 ComfyUI 用 `torch.from_numpy(arr).float().div_(255.0).unsqueeze(0)`；
  无 torch 的 mock/headless 环境回退为 `(1,H,W,3) float32` numpy（KICKOFF §3.2 允许）。
- **缓存**：`IS_CHANGED` 与 `fingerprint_inputs` 双钩子；Load 用 `(size, mtime_ns)`，
  其余用 `FDRAW.cache_key`（= `root_sha256`）＋参数内容 hash；FDRAW 不进入 `IS_CHANGED`
  的取值依赖（按 analysis 的反向陷阱，上游把 cache_key 放在值里）。
- **错误纪律**：`RuntimeError("FD_E_*: 人话原因｜修复建议")`；核心 `FP_E_*` / `FD_E_*`
  前缀原样保留；不返回空图、不静默降级（fast→exact 由核心记录 fallback）。
- **输入不可变**：渲染/预览均不改 `FDRAW.render`；测试用 JSON 快照断言。

## 3. 节点表

| 类名（显示名） | 输入 | 输出 | 实现 / 缓存 |
|---|---|---|---|
| `FDPLoadFdraw`（Load FDraw） | `fdraw` 下拉、`strict`、`path_override` | `FDRAW`, `summary` | `fdraw.unpack` 全验 + `inspect`；strict 拒绝未知必需能力；指纹 `(path,size,mtime_ns,strict)` |
| `FDPSaveFdraw`（Save FDraw） | `FDRAW`, `filename_prefix` | `path`（相对 `output/`） | 写盘前重算 root hash；`_00001.fdraw` 递增；返回 `{"ui":{"fdp_fdraw":[...]},"result":(...)}` |
| `FDPCompileFDSL`（Compile FDSL） | `fdsl_json`、`resolution`、`repair`、`strict`、可选 `FDSL` | `FDRAW`, `report` | `repair_plan`→`compile_plan`→`fdraw.pack`→`inspect`；strict 把 warning 变 `FD_E_SCHEMA` |
| `FDPRenderFdraw`（Render FDraw） | `FDRAW`、`resolution`(0=契约)、`quality`、`background`、`supersample`、`width_scale` | `IMAGE` | async + to_thread；`render_fast.render_auto`；`auto/#rrggbb/white/black/paper/grey` |
| `FDPPreviewFdraw`（Preview FDraw） | `FDRAW`、`fraction`、`resolution`、`quality`、`background`、`supersample` | `IMAGE` | `truncate_doc(fraction)` 前缀预览；fraction ∈ (0,1] |
| `FDPVideoFdraw`（FDraw → Video） | `FDRAW`、`duration_s`、`fps`、`resolution`、`quality`、`max_frames`、`background`、`filename_prefix` | `mp4_path`, `info` | async + to_thread；`fdvideo.render_video`；**只回路径不回流张量** |
| `FDPInspectFdraw`（Inspect FDraw） | `FDRAW` | `info_json` | 尺寸/层/元素/字体/root hash/体积/能力/完整性 + 当前 cache_key |
| `FDPBatchFdraw`（Batch FDraw） | `paths`（每行一个）、`inspect`、`skip_missing` | `FDRAW` 列表, `summaries` 列表 | `OUTPUT_IS_LIST=(True,True)`；逐项 Load + inspect；两列表等长 |

## 4. mock 测试结果

环境：Windows / Python **3.13.14**（WorkBuddy）/ numpy **2.5.4** / Pillow **12.3.0** /
imageio-ffmpeg **0.6.0** / **无 torch**（用 fake torch 覆盖张量路径）。真实 ComfyUI 未安装。

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/comfyui -t tests/comfyui -v
```

**结果：36 tests OK（0.63 s），exit 0**；分布 `test_package` 11 / `test_nodes` 19 /
`test_workflows` 6。

**无 mock 冒烟**（同一进程、不伪造 `folder_paths`/`comfy`，直接 import 包）：
`out/verify/01.fdraw`（51 elements / 6 layers）→ Load（`path_override`）→ Render R64
`(1,64,64,3)` → Inspect（`root_sha256_ok=true`）均成功，说明包对核心的真实调用链
不依赖 mock。ComfyUI 主机 API（节点注册/前端/张量）仍未验。

覆盖：

1. 伪造 `folder_paths`/`comfy` 后 import 包；`NODE_CLASS_MAPPINGS` **8 项**、显示名齐全、
   类名一致；`FDRAW`/`FDSL` 类型字面量；每节点 `INPUT_TYPES.required` 非空、
   `RETURN_TYPES` 为字符串 tuple、`FUNCTION` 可达且为协程、`CATEGORY` 前缀 `FDP`。
2. Load：临时 `.fdraw` → `FDrawValue` + summary（4 elements / 2 layers / root hash）；
   坏 ZIP `FD_E_ZIP`；缺失 `FD_E_FILE_NOT_FOUND`；`path_override` 绝对路径。
3. Save：`output/fdraw/metrics_00001.fdraw` 可回读；**与源 `.fdraw` 逐字节一致**
   （同 SHA-256，验证 pack 确定性 + root hash 复核）；UI payload 结构。
4. Compile：JSON 文本 / `FDSL` 对象两种入口；report JSON（resolution/elements/root/cache_key）；
   坏 JSON `FD_E_FDSL_JSON`；坏 schema `E_*`；strict 把 `FP_W_LAYER_VISIBLE_UNSUPPORTED`
   转 `FD_E_SCHEMA`；空输入 `FD_E_FDSL_EMPTY`。
5. Render：numpy 回退 `(1,64,64,3) float32` 0..1；`resolution=0` 跟随文件契约（64）；
   fake torch 路径得到 `(1,32,32,3)`；非法 background/quality 明确报错；
   `comfy.model_management.processing_interrupted()` → `InterruptedError`；
   渲染前后 `FDRAW.render` JSON 快照相等（不改输入）。
6. Preview：50% 前缀图与全图不同；fraction=0 报 `FD_E_PREVIEW_FRACTION`。
7. Inspect：`root_sha256_ok=true`、elements=4、layers=2、font_count=0、has_source=true。
8. Batch：2 项 → 两个等长 list；缺文件整体失败 / `skip_missing` 跳过；空输入
   `FD_E_BATCH_EMPTY`。
9. Video：1 s@4 fps R48 → `frames=4`、`48x48`、mp4 文件存在且非空（2,274 B，0.17 s）。
10. 静态：导入白名单 AST 扫描（仅 stdlib + numpy + `fourier_prompt` + 延迟 `torch`/`comfy`/
    `folder_paths`），`WEB_DIRECTORY is None`，`input/fdraw`/`output/fdraw` 注册成功。
11. Workflows：4 个 JSON 可解析；节点 id 唯一、类型合法（本包 8 类 + 核心 `PreviewImage`/
    `SaveImage`）；**输入/输出端口与 `INPUT_TYPES`/`RETURN_TYPES` 完全一致**；widget 数量
    匹配；link 六元组完整、无悬空、双向引用一致；`last_node_id`/`last_link_id` 一致。

实测（mini 夹具：4 元素 / 2 层 / 7,676 B `.fdraw`，纯 CPU）：

| 操作 | 参数 | 结果 | 耗时 |
|---|---|---|---|
| Load | `mini.fdraw` | 4 elements / 2 layers / root `5929ffb6…` | 0.016 s |
| Render | R64 / fast / ss1 | `(1,64,64,3)` float32 | 0.034 s |
| Preview | 50% / R64 | `(1,64,64,3)`，与全图不同 | 0.012 s |
| Save | `fdraw/metrics` | 7,676 B，与源文件逐字节相等 | 0.006 s |
| Inspect | — | root_ok=true，elements=4，layers=2 | — |
| Batch | 2 文件 | 2 FDRAW + 2 JSON | — |
| Video | 1 s@4 fps / R48 | 4 帧，2,274 B mp4 | 0.168 s |

## 5. 工作流

| 文件 | 图 | 静态校验 |
|---|---|---|
| `fdsl_to_fdraw.json` | CompileFDSL → Render → PreviewImage；CompileFDSL → SaveFdraw（4 节点 / 3 link） | 通过；内嵌 128×128 示例 plan（2 元素） |
| `fdraw_to_image.json` | LoadFdraw → Render → PreviewImage + SaveImage（4 / 3） | 通过；2 条 IMAGE link |
| `fdraw_to_video.json` | LoadFdraw → Render → PreviewImage；LoadFdraw → VideoFdraw（4 / 3） | 通过；Video 输出 `mp4_path`/`info` |
| `batch_fdraw.json` | BatchFdraw → Render → PreviewImage；BatchFdraw → SaveFdraw（4 / 3） | 通过；Batch 两条 FDRAW link，路径 3 行 |

工作流为 ComfyUI 前端格式（`nodes`/`links`/`groups`/`config`/`version`），只用本包节点 +
核心 `PreviewImage`/`SaveImage`，**不依赖 VHS 等第三方节点**。模板文件名为占位
（`01_rose_medallion.fdraw` 等），用户从下拉框改成本机文件即可。

## 6. 限制 / 未验（重要）

1. **真实 ComfyUI 未验**：本机未找到 ComfyUI；analysis/11 §4.1 的 0.38 / Python 3.11 /
   torch 2.9 结论来自既有实测记录，本批未复测。节点注册、torch IMAGE 传递、
   `folder_paths` 自定义类型下拉、前端加载 4 个工作流均需真机冒烟。
2. **进度/取消粒度**：`render_fast.render_auto` 与 `fdvideo.render_video` 没有进度或中断
   回调；节点只能在线程启动前检查取消标志。单次 R1024 渲染（数秒）或整段视频编码
   期间点取消不会立刻停。细粒度需要核心暴露 callback（后续批次）。
3. **视频只回路径**：`FDPVideoFdraw` 输出 `mp4_path` + `info`，不返回帧张量；`max_frames`
   硬上限 600，避免 analysis 指出的 5.6–16.8 GB 帧内存。工作流中视频节点是终端节点。
4. **性能口径未测**：本批 R32–R64；R1024 的 fast/exact 耗时/RSS 见 `WP-F`/`WP-J`，
   节点内（线程 + 张量转换）开销未在真机测。
5. **版本矩阵**：开发机 numpy 2.5/Pillow 12，ComfyUI 0.38 是 numpy 1.26/Pillow 11；
   本包未用 numpy 2 专有 API，但"逐像素一致"口径仍是"同 engine 版本内"。
6. **FDSL 无生产节点**：8 节点里没有 Planner/Edit；`FDSL` 类型只作为 Compile 的可选
   连线入口（外部/未来节点可接），默认走 `fdsl_json` 文本。
7. **安装前置**：包不复制核心，单独复制到 `custom_nodes` 后必须
   `pip install -e fourier-prompt` 或设 `FDP_CORE_PATH`，否则 import 报
   `ImportError`（`__init__.py` 已给人话提示）。
8. **保存命名**：`Save` 总是追加 `_00001` 序号（类似核心 `SaveImage`），不覆盖已有文件；
   `Save` 只回传相对路径字符串，不做 PNG 缩略图。

## 7. 真机安装 / 验收步骤（交给 Lead）

```powershell
# 1) 让 ComfyUI 的 Python 能导入核心（二选一）
& "<ComfyUI>\python\python.exe" -m pip install -e "<workspace>\fourier-prompt"
$env:FDP_CORE_PATH = "<workspace>\fourier-prompt"   # 或用这个

# 2) 复制节点包
Copy-Item -Recurse "<workspace>\fourier-prompt\comfyui\ComfyUI-FDP" `
  "<ComfyUI>\custom_nodes\ComfyUI-FDP"

# 3) 重启 ComfyUI，确认 Console 无 ImportError，节点菜单出现 FDP 分类
# 4) 把一个 .fdraw 放进 <ComfyUI>\input\fdraw\，打开 workflows\fdraw_to_image.json
#    依次验证：Load 下拉列文件 -> Render 出图 -> PreviewImage 正常
# 5) 打开 fdsl_to_fdraw.json，Queue 应直接出 128×128 示例图
# 6) 打开 fdraw_to_video.json，确认 output\fdp\video_00001.mp4 可播放
# 7) 记录：ComfyUI 版本 / Python / torch / numpy / Pillow；节点内 R256 渲染耗时与 RSS
```

真机若失败，优先检查：`fourier_prompt` 是否可导入、`folder_paths.folder_names_and_paths`
是否有 `fdraw`、torch 是否可用、`imageio-ffmpeg` 是否存在；错误信息均带 `FD_E_*` 码。

## 8. 边界合规

- 写盘仅：`fourier-prompt/comfyui/`、`fourier-prompt/tests/comfyui/`、
  `fourier-prompt/reports/WP-L.md`（外加生成后删除的临时 workflow 生成脚本）。
- 未修改 skill、`fourier_prompt/` 核心、`cli.py`、`fdrender_cli.py`、`fdvideo.py`、
  `docs/`、`analysis/`；核心文件只读导入。
- 未联网、未安装任何包、未使用 GPU；测试全部纯 CPU，图像 R≤64、视频 4 帧。
- 未复制第三方节点代码；未引入任何第三方自定义节点依赖；`requirements.txt` 仅注释。
