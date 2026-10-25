# WP-E 报告 — `.fdraw` 单文件容器（`fdraw/1`）

- 日期：2026-10-20 ｜ 包：**WP-E** ｜ 共享任务：task-16
- 状态：**完成**（容器 + CLI + 49 例测试；baseline 样例 35/35 通过）
- 契约：KICKOFF_v2 §3、analysis/10 §1、实现方案 v2 §4.1

## 1. 交付文件

| 文件 | 说明 |
|---|---|
| `fourier-prompt/fourier_prompt/fdraw.py` | 容器实现（纯 stdlib；约 1.3k 行）：`pack/unpack/inspect/validate`、`FDrawError`、常量、限值表 |
| `fourier-prompt/fourier_prompt/fdraw_cli.py` | 独立 CLI：`pack/unpack/inspect/validate`；**未改 `cli.py`** |
| `fourier-prompt/tests/fdraw/_fixtures.py` | 测试夹具：样例定位、手工 ZIP、meta 构造、限值 patch |
| `fourier-prompt/tests/fdraw/test_container.py` | 18 例：roundtrip/确定性/ZIP 布局/hash/inspect/source/fonts/CLI 前置 |
| `fourier-prompt/tests/fdraw/test_security.py` | 23 例：zip-slip/绝对路径/未知条目/符号链接/加密/限额/JSON/哈希/manifest |
| `fourier-prompt/tests/fdraw/test_cli.py` | 6 例：`python -m fourier_prompt.fdraw_cli` 子进程端到端 |
| `fourier-prompt/tests/fdraw/test_render.py` | 2 例：unpack 后用 skill `fbs_core.render` 出 PNG / 像素一致（R=128） |

未触碰 skill、`cli.py`、`compile.py`、`brush/`、WP-A/C 文件、docs/、analysis/。

## 2. 接口（与契约一致）

```python
FDRAW_VERSION = "fdraw/1"
class FDrawError(Exception):          # .code / .message / .evidence
pack(fbs, *, source_fdsl=None, meta=None, fonts=None, out=None) -> bytes
unpack(data_or_path, *, out_dir=None) -> dict
inspect(path_or_bytes) -> dict         # 不渲染
validate(data_or_path) -> list[str]    # [] = 合法
```

### 容器布局（v1 最小）

```text
X.fdraw = ZIP(deflate, 固定时间戳, 无加密)
├─ meta.json            # 清单权威；第一条；必需
├─ render.json          # .fbs v2 严格超集；渲染唯一权威；必需
├─ source.fdsl.json     # 可选；原始计划；缺失→capabilities.editing="restricted"
└─ fonts/<name>         # 可选；仅透传（TTF/OTF/WOFF/WOFF2/TTC/TXT）
```

`render.json` **原样保留**（只做规范化 JSON 序列化，不量化、不改字段、不加 `fdraw` 块），因此
`pack → unpack` 后与原始 `.fbs` 的规范化 JSON 严格相等；`.fbs` 的 v2 字段（`texts/order/fdraw/width_scale`）
作为 additive 超集被接受，不做语义改写。

`meta.json` 关键字段：

```jsonc
{
  "schema": "fdraw/1", "fdraw": "fdraw/1", "spec_version": "1.0",
  "created": "…",                          // 仅在调用方提供时写入（默认无，保证确定性）
  "capabilities": {
    "required": ["fourier/stroke", "fill/polygon"],   // 未知 required → FD_E_CAPABILITY
    "optional": ["source/fdsl-2", "fonts/passthrough", "render/width_scale-default"],
    "source": "fdsl/2.1", "editing": "full"            // 无 source 时 null/"restricted"
  },
  "profiles": ["core"],                    // 含 text/glyph 时追加 "text"
  "root_sha256": "…",                      // render.json 规范化字节目录哈希
  "sizes": {"render_bytes":…, "source_bytes":…, "fonts_bytes":…,
            "total_uncompressed":…, "entry_count":…},
  "counts": {"elements":…, "strokes":…, "fills":…, "texts":…, "fonts":…},
  "layers": [0,1,…], "canvas": {"width":…, "height":…},
  "render_contract": {…, "width_scale": null, "width_scale_pinned": false},
  "entries": [{"path","role","bytes","sha256","media","required"}…],
  "fonts": [{"path","bytes","sha256"}…],
  "producer": {"name":"fourier-prompt","version":"0.1.0","container":"fdraw/1","codegen":"fbs-v2"},
  "integrity": {"algorithm":"sha256","root_sha256":…,"root_scope":"render.json",
                "entries_root_sha256":…, "manifest_sha256":…}
}
```

## 3. Hash 与确定性

| 项 | 实现 |
|---|---|
| `root_sha256` | `sha256(canonical_json_bytes(render.json))`；规范 JSON = 键排序、无空白、UTF-8、禁 NaN/Inf |
| `integrity.entries_root_sha256` | 对非 meta 条目按 path 排序：`path\0sha256\0bytes\n` 的 sha256（与 ZIP 字节无关） |
| `integrity.manifest_sha256` | `meta.json` 去掉自身该字段后的规范化 JSON sha256 |
| `inspect` | 复算三项并返回 `*_ok`；`unpack/validate` 对 `root_sha256` 不一致抛 `FD_E_HASH` |
| ZIP 条目顺序 | `meta.json` → `render.json` → `source.fdsl.json` → `fonts/<name>`（字体按 path 排序） |
| ZIP 时间戳 | 默认 `1980-01-01 00:00:00`；`SOURCE_DATE_EPOCH` 可用时取 UTC（钳制 1980–2107） |
| 压缩 | 每条 `ZIP_DEFLATED`、`compresslevel=9`、`create_system=3`、`external_attr=0o100644<<16`、无加密 |
| JSON | 严格解析：`parse_constant` 拒绝 NaN/Infinity；嵌套 ≤32 层；重复键由 JSON 语义最后值（不接受非 JSON 类型） |
| 可复现 | 同输入两次 `pack` 字节相同；`unpack → 用 meta/render/source/fonts 重 pack` 字节相同（测试覆盖） |

> `created` 默认不写；若调用方传入则保留，并参与 `manifest_sha256`（仍可复现，因为输入相同）。
> `sizes.total_uncompressed` 不含 `meta.json` 自身（避免自引用）；`inspect.uncompressed_bytes` 是实际全量。

## 4. 安全限制（容器按不可信输入处理）

| 类别 | 限制/行为 | 错误码 |
|---|---|---|
| 路径 | 仅 `[A-Za-z0-9._/-]`；禁 `..`、绝对路径、盘符、反斜杠、NUL、尾 `/` | `FD_E_PATH` |
| 白名单 | 只允许 `meta.json` / `render.json` / `source.fdsl.json` / `fonts/<单段 name>.<允许扩展>` | `FD_E_UNKNOWN_ENTRY` |
| 条目类型 | 只接受普通文件；拒绝目录/符号链接/设备（`external_attr` mode 位） | `FD_E_ENTRY_TYPE` |
| ZIP 模式 | 拒绝加密位、非 STORED/DEFLATED、多卷；坏 ZIP | `FD_E_ZIP_MODE` / `FD_E_ZIP` |
| 条目数 | ≤32（含 meta） | `FD_E_TOO_MANY_ENTRIES` |
| 单条目 | meta ≤1 MiB；render ≤32 MiB；source ≤256 KiB；字体 ≤8 MiB/个、总量 ≤8 MiB；其余 ≤32 MiB | `FD_E_ENTRY_TOO_BIG` |
| 总量 | 解压总大小 ≤64 MiB | `FD_E_BOMB` |
| 压缩比 | ≥1 KiB 的条目压缩比 ≤200:1；实际解压按上限 +1 读取，防 zip bomb | `FD_E_BOMB` |
| 重复 | 条目名大小写不敏感去重 | `FD_E_DUPLICATE` |
| JSON | 坏 JSON / 非 UTF-8 / NaN/Inf / 深嵌套 | `FD_E_JSON` |
| 清单 | 缺 meta、meta 非对象、entries 缺失/不匹配/未登记/登记不存在 | `FD_E_MANIFEST` |
| 渲染权威 | 缺 render、render 非合法 `.fbs` v2 | `FD_E_RENDER` |
| 版本/能力 | 非 `fdraw/1.x`；未知 required capability | `FD_E_VERSION` / `FD_E_CAPABILITY` |
| 哈希 | root_sha256 不符 | `FD_E_HASH` |
| 行为 | 零网络、零子进程、零动态执行；字体只读字节，不做字体解析/安装 | — |

`unpack(out_dir=...)` 额外用 `realpath` 校验目标仍位于输出目录内，防止输出目录内已有符号链接造成逃逸。

## 5. 测试结果

```powershell
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/fdraw -v
# Ran 49 tests in 2.3s — OK
```

| 文件 | 例数 | 覆盖 |
|---|---:|---|
| `test_container.py` | 18 | 2 个 skill 样例 + WP-D 产物 roundtrip、两次 pack 字节相同、unpack→repack 字节相同、ZIP 顺序/时间戳/压缩、root/manifest/entries hash、inspect 字段、source/fonts/meta、out_dir、坏 render/坏字体名/未知 required、`validate` |
| `test_security.py` | 23 | zip-slip/绝对路径/`..`、未知条目、符号链接、大小写重复、缺 meta/render、坏 JSON/NaN、超大条目、条目数、压缩比、总量、篡改 root、未登记/登记缺失、加密/压缩方法守卫、pack 限额、bytes 上限 |
| `test_cli.py` | 6 | 子进程 `pack --source-plan` → inspect JSON → validate → unpack；坏包退出码 1；human inspect；坏 fbs；缺输入 |
| `test_render.py` | 2 | unpack 后 skill `io_format.validate` + `render` 与原 `.fbs` 像素一致（R=128）；输出 PNG 魔数校验 |

### DoD 对照

1. **2 个 `.fbs` roundtrip**：`assets/out/smoke/T1_signature.fbs.json`（1 笔）与 `T2_icon_R512`（7 笔，4 fill，2 层）→ `render.json` 规范化相等；另用 **WP-D 编译产物 `out/01_rose.fbs`**（51 笔、4 fill、6 层、13,565 B 容器）验证，条件存在时测试自动执行；35 个 per-stroke 样例全部 `pack→unpack` 后经 skill `io_format.validate` **35/35 == []**；unpack 后 `render` 出 PNG 成功。
2. **确定性**：同输入两次 `pack` **字节相同**；`unpack` 后用 `meta/render/source/fonts` 重 pack **字节相同**（含 `created` 保留场景）。
3. **安全**：zip-slip、超大条目、未知文件、缺 render、坏 JSON、缺 meta 均拒绝并带 `FD_E_*` code；另有符号链接/重复/压缩炸弹/总量/加密/篡改哈希/manifest 用例。
4. **inspect**：版本/spec、ZIP 体积/解压体积/条目数、canvas/R、元素/笔画/填充/文本、层列表、字体数、source schema、root hash、capabilities、profiles、counts、sizes；不渲染。
5. **测试/报告**：`tests/fdraw/` 共 49 例（≥15），本报告。

## 6. 体积

对 `fourier-brushstroke/assets` 下全部 **35 个 per-stroke `.fbs.json`** 做 pack（无 source/fonts）：

| 指标 | 值 |
|---|---:|
| 样例数 | 35 |
| `.fbs` 规范 JSON 合计 | 8,284,312 B |
| `.fdraw` 合计 | 2,015,564 B |
| 平均 `.fdraw` | 57,588 B |
| 中位 `.fdraw` | 16,011 B |
| 最大 `.fdraw` | 559,939 B（`QQ20261016-052636`，470 笔，R1024） |
| 压缩比（规范 JSON / `.fdraw`） | 2.28×–7.38×，平均 4.11× |

代表样例：

| 样例 | 笔画/填充 | 层 | R | `.fbs` 规范字节 | `.fdraw` 字节 | 压缩比 |
|---|---:|---:|---:|---:|---:|---:|
| `T8a_solid_black` | 0 / 0 | 0 | 512 | 7,038 | 3,093 | 2.28× |
| `T1_signature`（smoke） | 1 / 0 | 1 | 512 | 9,920 | 4,274 | 2.32× |
| `T2_icon` | 7（4 fill） | 2 | 512 | 97,116 | 16,011 | 6.07× |
| `T3_flat_illustration` | 14 | 2 | 512 | 138,561 | 18,764 | 7.38× |
| `real_chibi` | 516 | 2 | 1024 | 2,333,552 | 555,483 | 4.20× |
| `QQ20261016-052636` | 470 | 2 | 1024 | 2,339,374 | 559,939 | 4.18× |
| **WP-D `out/01_rose.fbs`** | **51（4 fill）** | **6** | 512 | **62,726** | **13,565**（带 source 14,242） | **4.62×** |

字体透传按真实字节计入；单字体 ≤8 MiB、总量 ≤8 MiB。未内嵌 preview/timeline/onestroke，符合 v1 容器最小范围与体积目标。

## 7. 限制 / 风险（需 Lead/WP-D 知悉）

1. **`width_scale` 未钉死**：现有 skill 样例与 WP-D `out/01_rose.fbs` 均没有 `render_contract.width_scale`。为满足“roundtrip 后 render.json 与原始 `.fbs` 相等”，`pack` **不静默改 render.json**，只在 meta 记 `width_scale: null`、`width_scale_pinned: false`，并在 capabilities.optional 标 `render/width_scale-default`。渲染端仍须显式传 `render(doc, width_scale=1.0)`（或由 WP-D 在上游钉死），否则 100+ 笔 lineart/flat_art 会被 skill 自动取 0.95，产生粗细漂移。
2. **字体仅透传**：本批不做字体 magic/解析/子集/许可校验，也不做 `texts[]` 的 glyph 光栅化；`texts` 只做结构校验（`as ∈ outline|glyph`），渲染仍由 `render.json` 权威。`outline` 文本若已被 WP-D 烘成 `region_fill` 则可正常渲染；`glyph` 需批次 3 的 text profile。
3. **容器条目白名单是 v1 最小集**：`timeline.json`、`texts.json`、`preview.png`、`derived/*`、`LICENSES/*` 目前会被 `FD_E_UNKNOWN_ENTRY` 拒绝。analysis/10 的完整布局留给后续批次；若本批需要 timeline/preview，需扩展白名单与 meta.entries。
4. **`root_sha256` 作用域**：按任务约定只覆盖 `render.json` 规范化字节；全条目审计用 `entries_root_sha256`，meta 自身用 `manifest_sha256`。三者 `inspect` 都会复算。
5. **meta `sizes` 不含 meta 自身**：避免自引用；`inspect.uncompressed_bytes` 是包含 meta 的实际总量。
6. **反斜杠条目**：Python `zipfile` 读写会把 `\` 规范为 `/`，因此“原始反斜杠条目”无法由 Python 生产；`_validate_rel_path` 仍显式拒绝 `\`，并有直接守卫测试，防非 Python 生产者/读取器。
7. **`fourier-prompt/tests/core` 当前 1 例失败**（与本包无关）：`test_reserved_command_fails_loudly` 期望 `plan2fbs` 返回退出码 3，但 WP-D 已把 `plan2fbs/draw` 加入 `cli.py`，实际返回 2。请 WP-D/Lead 更新该断言；WP-E 未改 `cli.py`。
8. 未做跨机器 ZIP 字节回归（依赖同 zlib 版本）；`root_sha256` 与内容哈希跨机器稳定，ZIP 容器字节在不同 zlib 版本下可能不同（契约只要求同输入同环境两次相同）。

## 8. 复现命令

```powershell
$PY = "<python>"
$FP = "<workspace>\fourier-prompt"
cd $FP

# 测试（49 例）
& $PY -m unittest discover -s tests/fdraw -v

# CLI：pack → inspect → validate → unpack（对 WP-D 产物）
& $PY -m fourier_prompt.fdraw_cli pack --fbs out\01_rose.fbs `
    --source-plan ..\poc\fdsl\examples\01_rose_medallion.json --out $env:TEMP\rose.fdraw
& $PY -m fourier_prompt.fdraw_cli inspect --fdraw $env:TEMP\rose.fdraw --json
& $PY -m fourier_prompt.fdraw_cli validate --fdraw $env:TEMP\rose.fdraw
& $PY -m fourier_prompt.fdraw_cli unpack --fdraw $env:TEMP\rose.fdraw --out-dir $env:TEMP\rose_unpacked
```
