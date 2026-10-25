# WP-H — CLI/MCP 工具面（批次 3）

- 日期：2026-10-20 ｜ 状态：**完成**（8 工具 + stdlib MCP server + 33 测试：23 工具 + 10 MCP）
- 契约：KICKOFF_v3 §3 / §4 ｜ 参考：analysis/12 §I3（只借鉴设计，未复制代码）
- 依赖：只读 skill；组合 `schema` / `compile` / `fdraw` / `compat.skill_api` / `render_fast`（WP-F）

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `fourier_prompt/tools/__init__.py` | 注册表 `TOOLS`/`TOOL_NAMES`，`list_tools()`，`call_tool()`，inputSchema 参数校验，`fdraw_*` 别名 |
| `fourier_prompt/tools/common.py` | `ToolError(code/message/evidence)`、JSON 安全化、输入嗅探/加载、PNG 写出、≤768 base64、渲染分派、文档摘要、catalog 加载 |
| `fourier_prompt/tools/{status,list_brushes,inspect,render,preview,frame,compare,validate}.py` | 8 个工具（一个模块一个工具） |
| `fourier_prompt/mcp_server.py` | stdlib JSON-RPC 2.0 / stdio（逐行）+ MCP image content |
| `tests/tools/{_fixtures,test_tools,test_mcp}.py` | 33 例（23 工具 + 10 MCP 子进程） |
| `reports/WP-H.md` | 本报告 |

未改：skill、`cli.py`、`fdraw.py`、`render_fast.py`、`schema.py`、`compile.py`、`docs/`、`analysis/`、WP-F 文件。

## 2. 工具表（统一返回 JSON 可序列化 dict；失败 `raise ToolError`）

| 工具 | 签名（Python） | 行为 | 主要返回字段 |
|---|---|---|---|
| `status` | `status(*, selftest=True)` | skill 路径/版本、numpy/Pillow、字体无、ffmpeg 可用性、catalog 20/11、fdraw 版本、selftest 摘要、`render_fast` 可用性 | `skill{found,path,version,selftest}`、`dependencies`、`fonts`、`ffmpeg`、`brush_catalog`、`fdraw`、`render_backends` |
| `list_brushes` | `list_brushes(*, status="all", role=None, category=None)` | catalog active 20 + planned 11（`renderable=false`）；每项 id/name/category/roles/width_eff/shape | `counts`、`roles_available`、`categories_available`、`brushes[]` |
| `inspect` | `inspect(path)` | `.fdraw` / `.fbs` / FDSL plan 摘要：版本/画布/元素/层/文本/字体/root_sha256/体积 | `kind`、`document{counts,layers,text,...}`、`container{root_sha256_ok,...}` / `plan{valid,errors,...}` |
| `render` | `render(path, out, resolution=512, quality="fast", supersample=2, include_image=False)` | `.fdraw`/`.fbs`/plan → PNG；优先 `render_fast.render_auto`，不可用则 skill exact 并记 `fallback_reason` | `out,width,height,resolution,quality,backend,fallback,fallback_reason,fast_fallback_reason,fast_approximations,elapsed_ms,png_bytes,pixel_sha256` |
| `preview` | `preview(path, out, fraction=0.3, resolution=512, quality="fast", include_image=False)` | 前 `ceil(n·fraction)` 个元素 → PNG（内存截断副本，不改原文件；截断时丢弃尾部 texts） | `kept,total,percent,truncated,full_preview` + render 字段 |
| `frame` | `frame(path, out, at=20, resolution=512, quality="fast", include_image=False)` | 前 `at` 个元素 → PNG；`at>总数` 时截断全量并 `clamped=true` | `at,kept,total,truncated,clamped,note` + render 字段 |
| `compare` | `compare(a, b, region=None)` | mean/max diff、RMSE、PSNR、within1/2、`parity_pass`（max≤2 且 PSNR≥42 dB）；region 支持 `{x,y,width,height}` 或 `[x,y,w,h]` | `metrics{}`、`parity_pass`、`thresholds`、`region`、`backend` |
| `validate` | `validate(path, *, strict=False)` | 三类分派：FDSL plan（schema 报告）/ `.fbs`（skill `io_format.validate`）/ `.fdraw`（`fdraw.validate` + inspect 校验位） | `ok,kind,errors[],warnings[],checks{}` |

### 2.1 错误码（`ToolError.code`）

| code | 含义 |
|---|---|
| `TOOL_E_ARG` | 参数非法（类型/范围/enum/未知参数/缺少必填/out 覆盖输入） |
| `TOOL_E_NOT_FOUND` | 输入路径不存在 |
| `TOOL_E_INPUT` | 扩展名/内容不是受支持的三类，或 plan 编译失败 |
| `TOOL_E_LOAD` | 文件无法解析（坏 JSON / 坏 `.fdraw` / 坏 `.fbs`） |
| `TOOL_E_RENDER` | 渲染器返回不可用像素 / 两个后端都失败 |
| `TOOL_E_WRITE` | PNG 写出失败 |
| `TOOL_E_COMPARE` | 两图尺寸不一致，无法比较 |
| `TOOL_E_CATALOG` | brush catalog 缺失/损坏 |
| `TOOL_E_INTERNAL` | 未预期异常（被 `call_tool` 归一化） |

约定：`out` 必须是 `.png`、不得等于输入路径（工具绝不修改输入）；父目录自动创建。

## 3. MCP server（`python -m fourier_prompt.mcp_server`）

- stdlib only，JSON-RPC 2.0，**逐行**（含 batch 数组）；`stdout` 只有协议 JSON，日志全走 `stderr`（前缀 `[fourier-prompt-mcp]`）。
- 方法：`initialize`、`notifications/initialized`（无响应）、`ping`、`tools/list`、`tools/call`。
- 错误：`-32700` parse / `-32600` invalid request / `-32601` method not found / `-32602` 未知工具与参数错误 / `-32603` 工具执行失败；`data` 带 `tool_code`+`evidence`。
- 图片：`render`/`preview`/`frame` 自动 `include_image=true`，返回 `text` + `image`（base64 PNG，长边 ≤768；超长边 LANCZOS 缩放）。
- 通知（无 `id`）不产生响应；未知通知静默。

### 3.1 消息样例（真实输出，`...` 为裁剪标记）

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"1.0"}}}
```
```json
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "fourier-prompt", "title": "fourier-prompt tools", "version": "0.1.0+tools1.0"}, "instructions": "fourier-prompt agent tools: status, list_brushes, inspect, render, preview, frame, compare, validate. ..."}}
```
```json
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```
```json
{"jsonrpc": "2.0", "id": 2, "result": {"tools": [
  {"name": "status", "description": "Report tool/skill/dependency status: ...", "inputSchema": {"type": "object", "properties": {"selftest": {"type": "boolean", "default": true}}, "additionalProperties": false}},
  "... 7 more: list_brushes, inspect, render, preview, frame, compare, validate ...",
  {"name": "render", "description": "Render an .fdraw container, a .fbs document or an FDSL plan to a PNG. ...", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "resolution": {"type": "integer", "minimum": 16, "maximum": 1024, "default": 512}, "quality": {"type": "string", "enum": ["fast", "exact"], "default": "fast"}, "supersample": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2}, "include_image": {"type": "boolean", "default": false}}, "required": ["path", "out"], "additionalProperties": false}}
]}}
```
```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"render","arguments":{"path":"out/verify/01.fdraw","out":"<path>","resolution":64}}}
```
```json
{"jsonrpc": "2.0", "id": 3, "result": {"content": [
  {"type": "text", "text": "{\"ok\": true, \"tool\": \"render\", \"out\": \"<path>", \"width\": 64, \"height\": 64, \"quality\": \"fast\", \"backend\": \"render_fast\", \"fallback\": false, ...}"},
  {"type": "image", "data": "iVBORw0KGgoAAAANSUhEUg...(base64 truncated)", "mimeType": "image/png"}
], "isError": false}}
```
```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"nope","arguments":{}}}
```
```json
{"jsonrpc": "2.0", "id": 4, "error": {"code": -32602, "message": "TOOL_E_ARG: unknown tool 'nope'", "data": {"tool_code": "TOOL_E_ARG", "evidence": {"name": "nope", "known": ["status", "list_brushes", "inspect", "render", "preview", "frame", "compare", "validate"]}, "tool": "nope"}}}
```

## 4. WP-F 集成（render_fast）

- 集成前（`render_fast.py` 尚未落地）：`from ..render_fast import render_auto` → `ImportError`，工具自动回退 `compat.skill_api.render`，返回 `backend="skill_exact"`、`fallback=true`、`fallback_reason="render_fast unavailable (ImportError: ...)"`（测试与首次手测均验证）。
- 集成后（WP-F 落地，`render_fast.py` 29476 B）：`render_auto(..., return_info=True)` 成功，`backend="render_fast"`、`fallback=false`、`fast_fallback_reason=[]`（WP-F 内部 fallback 与 approximations 单独上报，不混同本工具的 backend 回退）。
- `compare` 在 WP-F 可用时用 `render_fast.compare_images`（`backend="render_fast"`），否则本地等价实现（`backend="local"`）；两者 `within1/2` 与 `parity_pass` 同规则（max≤2、PSNR≥42 dB）。注意：完全相同的图，WP-F 返回 `psnr_db=99.0` 哨兵，本地回退返回 `null`（∞ 不可 JSON 化），`parity_pass` 均为 true。
- 实测（本机 CPU，`out/verify/01.fdraw` 51 元素）：

| 操作 | 分辨率 | backend | 耗时 |
|---|---|---|---|
| `status`（含 selftest） | – | – | 0.77 s |
| `render` | R128 | render_fast | 0.39 s |
| `render` | R512 | render_fast | 1.60 s |
| `preview` 30% | R256 | render_fast | 0.29 s |
| `frame` 前 20 | R256 | render_fast | 0.34 s |
| `compare` | R128 同图 | render_fast | 0.01 s |

## 5. 测试

```powershell
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/tools -v
# Ran 33 tests in 5.6-6.3 s — OK（exit 0）
```

- `test_tools.py`（23 例）：status 正常/坏参数；list_brushes active/planned 标记/过滤/坏 status；inspect fdraw/fbs/plan/不存在/不支持类型；render fdraw、fbs+plan、坏参数（quality/resolution/supersample/覆盖输入/非 png）、确定性；preview 前缀/100% 全量/非法 fraction；frame 前缀/越界 clamp/坏 at；compare 同图/异图/region/尺寸不一致/坏 region；validate 三类/坏 plan/损坏容器/不支持类型；输入文件 sha256 前后不变；注册表+别名+JSON 安全。
- `test_mcp.py`（10 例）：子进程真实 stdio → `initialize` 握手、`tools/list`（8 工具 + inputSchema）、`tools/call status`、`render`（text+image，PNG magic，长边≤768）、`compare`（同/异）、未知工具 `-32602`、坏参数 4 组 `-32602`、不存在路径 `TOOL_E_NOT_FOUND`、`ping` 与未知方法 `-32601`、通知无响应、坏 JSON `-32700` + stdout 全部可 `json.loads` + stderr 有日志。
- 测试自建 fixture（temp 目录内编译 mini plan → `.fbs` → `.fdraw`），**不写仓库 `out/`**，不依赖他人产物。

## 6. 限制 / 风险

1. **字体/文本**：skill 渲染无文本路径（`render()` 不读 `texts`）。`inspect`/`validate` 会统计 texts 与 font 引用，`render` 不画文本且 `status.fonts.available=false`；批次 5 字体子系统落地后需在 `render` 返回中显式 `texts_rendered=false`（当前仅在 inspect 中标注）。
2. **preview/frame 只截断 `strokes`**：元素序即数组序；截断时丢弃尾部 `texts`，`regions` 保留（region_fill 需要）。不产出“截断 `.fdraw`”（Daub 式可继续编辑的冻结帧）——本批只交付 PNG，容器输出留待后续。
3. **resolution 上限 1024**（KICKOFF §5 约束：本批 R≤512 常用；测试用 R32–96）。`--fdraw` 的 `render_contract.target_resolution` 不强制等于本次渲染分辨率；plan 输入内部按 256 编译后以目标分辨率渲染。
4. **PSNR 哨兵差异**：同图时 `psnr_db` 可能为 `null`（本地，∞）或 `99.0`（WP-F），均 `parity_pass=true`；下游若做数值断言需容忍两者。
5. **MCP 形态**：同步单线程、单客户端、无流式进度/取消、无鉴权（stdio 信任边界）；batch 支持但未做并发。`tools/call` 的图片为内联 base64（≤768 长边），大图请读 `out` 文件。
6. **错误即 JSON-RPC error**：按 KICKOFF §3.2 实现；因此工具业务失败不会以 `isError=true` 的 CallToolResult 返回（MCP 规范两种都允许，接入方需按 JSON-RPC error 分支处理）。
7. **依赖竞态**：WP-F 文件在本次开发中落地；`render_document` 对 import 失败/`render_auto` 抛错均回退 skill exact 并记录原因，因此 WP-F 半成品不会打断 WP-H，但回退时耗时回到 exact（R512 ≈ 3.5 s）。
