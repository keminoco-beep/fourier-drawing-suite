# ComfyUI-FDP — Fourier Drawing Program nodes for ComfyUI

ComfyUI 自定义节点包，把 `fourier-prompt` 的 FDSL 编译、`.fdraw` 容器、快速渲染与
渐进式视频能力接进 ComfyUI。**只用 ComfyUI V1 节点 API**（`INPUT_TYPES` /
`RETURN_TYPES` / `FUNCTION` / `CATEGORY`），不依赖任何第三方自定义节点，也不复制
编译器 / 容器 / 渲染器代码——所有工作都调用 `fourier_prompt` 公共 API。

> **验证状态（诚实声明）**：本机未找到可用的 ComfyUI 安装，本批验收为
> **mock `folder_paths`/`comfy`/`torch` + 静态契约**（36 例测试全绿，见
> `fourier-prompt/tests/comfyui/`）。**未在真实 ComfyUI 进程中加载过**；
> 真机安装后请先跑一个 4 节点最小工作流（Load → Render → PreviewImage）。

## 1. 目录

```
comfyui/ComfyUI-FDP/
├── __init__.py        # 注册 8 节点 + input/output/fdraw 目录 + core 路径引导
├── nodes.py           # 8 节点（async + asyncio.to_thread）
├── README.md
├── requirements.txt   # 仅注释：不新增 pip 依赖
└── workflows/
    ├── fdsl_to_fdraw.json
    ├── fdraw_to_image.json
    ├── fdraw_to_video.json
    └── batch_fdraw.json
```

## 2. 安装

前提：ComfyUI 的 Python 环境里已有 `numpy` / `Pillow` / `torch`（官方包自带）。
本包**没有**任何第三方 pip 依赖；视频编码用 ComfyUI 环境通常自带的
`imageio-ffmpeg`（缺失时 Video 节点报 `VD_E_NO_FFMPEG`，静态渲染不受影响）。

1. 把 `ComfyUI-FDP/` 整个目录复制（或 junction/symlink）到
   `ComfyUI/custom_nodes/ComfyUI-FDP/`。
2. 让 `fourier_prompt` 可被 ComfyUI 的 Python 导入，二选一：
   - 在仓库里执行 `pip install -e fourier-prompt`（装进 ComfyUI 的环境）；或
   - 设置环境变量 `FDP_CORE_PATH=<path> skill\fourier-prompt`
     （指向**包含** `fourier_prompt/` 的目录）。
   仓库布局下（`fourier-prompt/comfyui/ComfyUI-FDP`）包会自动向上找到核心，
   但把目录单独复制进 `custom_nodes` 后必须用上面两种方式之一。
3. 重启 ComfyUI。启动日志里出现 `FDP` 分类即注册成功；`input/fdraw`、
   `output/fdraw` 会被自动创建并注册为 `fdraw` 文件类型。

`WEB_DIRECTORY = None`：本包不带前端 JS 扩展，避免 ComfyUI 前端 API 漂移。

## 3. 类型

| 类型 | 含义 | 持久化 |
|---|---|---|
| `FDRAW` | 已校验的 `.fdraw` 内存值（`render.json` + 可编辑 `source.fdsl.json` + root hash） | 否，只在同一次执行图内传递 |
| `FDSL` | FDSL v2.1 plan 对象（保留给上游生成器/外部节点；本包 Compile 也接受 JSON 文本） | 否 |

ComfyUI 只用类型字符串做连线合法性检查，不会序列化 `FDRAW`；工作流保存的是
"如何生成 FDRAW"，不是图数据本身。

## 4. 节点（8 个）

| # | 类名（显示名） | 输入 → 输出 | 说明 |
|---|---|---|---|
| 1 | `FDPLoadFdraw`（Load FDraw） | `fdraw`(下拉) / `strict` / `path_override` → `FDRAW`, `summary` | `fdraw.unpack` 全量校验 + `inspect`；严格模式拒绝未知必需能力；`IS_CHANGED` 用 `(size, mtime_ns)` |
| 2 | `FDPSaveFdraw`（Save FDraw） | `FDRAW` + `filename_prefix` → `path`(相对路径) | 写 `output/<prefix>_00001.fdraw`；写盘前复核 root hash；输出节点返回 `{"ui","result"}` |
| 3 | `FDPCompileFDSL`（Compile FDSL） | `fdsl_json`(多行) / `resolution` / `repair` / `strict`（可选 `FDSL` 连线）→ `FDRAW`, `report` | `repair_plan` + `compile_plan` + `fdraw.pack`；`strict` 把 warning 变错误 |
| 4 | `FDPRenderFdraw`（Render FDraw） | `FDRAW` / `resolution`(0=文件契约) / `quality` / `background` / `supersample` / `width_scale` → `IMAGE` | `async` + `to_thread`；`fast` 不支持时由核心显式回退 `exact` |
| 5 | `FDPPreviewFdraw`（Preview FDraw） | `FDRAW` / `fraction` / `resolution` / `quality` / `background` / `supersample` → `IMAGE` | 用 `render_fast.truncate_doc` 只画前 N% 元素，适合先看构图 |
| 6 | `FDPVideoFdraw`（FDraw → Video） | `FDRAW` / `duration_s` / `fps` / `resolution` / `quality` / `max_frames` / `background` / `filename_prefix` → `mp4_path`, `info` | `fdvideo.render_video` 帧流式编码；**只回 mp4 路径**，不回帧张量（避免 OOM） |
| 7 | `FDPInspectFdraw`（Inspect FDraw） | `FDRAW` → `info_json` | 尺寸/层/元素/字体/root hash/体积/能力/完整性，JSON 文本 |
| 8 | `FDPBatchFdraw`（Batch FDraw） | `paths`(每行一个) / `inspect` / `skip_missing` → `FDRAW` 列表, `summaries` 列表 | `OUTPUT_IS_LIST`；下游非 list 节点会自动逐项执行 |

共同约定：

- **重节点全部 `async def` + `await asyncio.to_thread(...)`**：ComfyUI 0.38 的 V1
  同步函数在事件循环内直接执行，同步写会让 UI/取消/队列全部卡住。
- 启动前检查 `comfy.model_management.processing_interrupted()`，命中抛
  `InterruptedError("FD_E_INTERRUPTED: ...")`；执行中无法细粒度取消的说明见 §8。
- 错误一律 `RuntimeError("FD_E_*: 人话原因｜修复建议")`，不吞异常、不返回空图。
- `IS_CHANGED` 与 `fingerprint_inputs` 同时提供；指纹是内容 hash，不是时间戳。
- 不修改输入 `FDRAW`；渲染/预览使用核心的 copy/truncate 语义。

## 5. 工作流（4 个）

| 文件 | 图 | 用法 |
|---|---|---|
| `fdsl_to_fdraw.json` | CompileFDSL → Render → PreviewImage；CompileFDSL → SaveFdraw | 在 Compile 节点粘贴/编辑 FDSL JSON，Queue 出图；改 Save 前缀可落 `.fdraw` |
| `fdraw_to_image.json` | LoadFdraw → Render → PreviewImage / SaveImage | 打开已有 `.fdraw` 出 PNG；Render 的 `resolution=0` 跟随文件契约 |
| `fdraw_to_video.json` | LoadFdraw → Render → PreviewImage；LoadFdraw → VideoFdraw | 先看静态帧，再生成 mp4（默认 20 s@24fps、R256、≤600 帧） |
| `batch_fdraw.json` | BatchFdraw → Render → PreviewImage；BatchFdraw → SaveFdraw | 每行一个文件，批量渲染/另存 |

工作流为 **ComfyUI 前端格式**（`nodes` / `links` / `groups` / `config`），只使用本包
节点与核心 `PreviewImage` / `SaveImage`，不依赖 VHS 等第三方节点。模板里的
`01_rose_medallion.fdraw` 等文件名是占位；打开后从下拉框改成本机 `input/fdraw`
里的文件即可。`fdsl_to_fdraw.json` 内嵌了一个可直接运行的 128×128 示例 plan。

## 6. 缓存与重跑

| 节点 | `IS_CHANGED` 依据 |
|---|---|
| Load | 绝对路径 + `(size, mtime_ns)` + strict |
| Save | `FDRAW.cache_key` + 前缀 |
| Compile | `fdsl_json` + resolution + repair + strict |
| Render | `FDRAW.cache_key` + resolution/quality/background/supersample/width_scale |
| Preview | 同上 + fraction |
| Video | 同上 + duration/fps/resolution/max_frames/prefix |
| Inspect / Batch | `FDRAW.cache_key` / 路径文本 + 选项 |

`FDRAW.cache_key` 就是容器 `root_sha256`（render.json 的 SHA-256），因此同一文件
在不同工作流里命中同一指纹；上游内容一变，下游全部失效重跑。

## 7. 错误码（节选）

`FD_E_FILE_NOT_FOUND`、`FD_E_ZIP`、`FD_E_INTEGRITY`、`FD_E_CAPABILITY`、
`FD_E_TYPE`、`FD_E_FDSL_EMPTY`、`FD_E_FDSL_JSON`、`FD_E_SCHEMA`（strict
warning）、`FD_E_BACKGROUND`、`FD_E_QUALITY`、`FD_E_RESOLUTION`、
`FD_E_PREVIEW_FRACTION`、`FD_E_RENDER`、`FD_E_VIDEO` / `VD_E_NO_FFMPEG`、
`FD_E_BATCH_EMPTY`、`FD_E_INTERRUPTED`。
核心自己的 `FP_E_*` / `FD_E_*` 代码会原样保留在消息前缀里。

## 8. 限制与已知风险（真机验收前必读）

1. **未在真实 ComfyUI 加载**：mock 覆盖了 8 节点映射、Load/Render/Preview/
   Inspect/Batch/Video 执行、异步、错误路径与 4 个工作流静态结构；真机的节点
   注册、`folder_paths` 注册、torch IMAGE 传递仍需一次冒烟（建议 0.38 + Py3.11）。
2. **取消粒度**：`render_fast.render_auto` 与 `fdvideo.render_video` 没有进度/中断
   回调，节点只能在线程启动前检查取消标志；单次 R1024 渲染（数秒）或整段视频
   编码期间点取消不会立刻生效。细粒度进度需要核心暴露 callback（后续批次）。
3. **视频内存**：本节点只返回路径+文本，不返回帧张量；`max_frames≤600` 是硬上限。
   20 s@24 fps R720×1080 的完整帧集合约 5.6 GB float32，绝不要接 `SaveImage`。
4. **R≤256 之外的性能未验**：本批测试用 R32–R64；R1024 的 fast/exact 耗时与
   RSS 见 `reports/WP-F`、`reports/WP-J`，真机节点内耗时未测。
5. **numpy/Pillow 版本**：ComfyUI 0.38 环境是 numpy 1.26 / Pillow 11，开发机是
   numpy 2.5 / Pillow 12；本包不使用 numpy 2 专有 API，但逐像素一致性口径是
   "同一 engine 版本内一致"。
6. **无 `vendor/` 快照**：本包按约束只调用公共 API，不复制核心；因此必须
   `pip install -e fourier-prompt` 或设 `FDP_CORE_PATH`（§2）。
7. **`requirements.txt` 故意只有注释**：防止 ComfyUI-Manager 自动升级 numpy
   把 aki 环境搞坏。

## 9. 测试（mock，无需 ComfyUI）

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/comfyui -t tests/comfyui -v
```

测试会伪造 `folder_paths`/`comfy`（以及一个用例里的 `torch`），在临时目录编译
一个小 FDSL → pack 成 `.fdraw` → 走 Load/Render/Preview/Inspect/Batch/Video，
并静态校验 4 个工作流的节点/连线自洽。

## 10. 真机安装后的最小冒烟

1. 把任意 `.fdraw` 放进 `ComfyUI/input/fdraw/`（或渲染后用 Save 节点产出）。
2. 加载 `workflows/fdraw_to_image.json`，确认 Load 下拉框能列到该文件。
3. Queue：PreviewImage 应出图且无红色节点；Console 无 `FD_E_*`。
4. 再跑 `fdsl_to_fdraw.json`（内嵌示例 plan）验证编译链路。
5. 最后跑 `fdraw_to_video.json`，确认 `output/fdp/video_00001.mp4` 可播放。
