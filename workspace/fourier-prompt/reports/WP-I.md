# WP-I 报告 — 分层语义 + PSD/KRA 导出

- 日期：2026-10-20 ｜ 状态：**完成（v1，真实 PS/Krita 未验证，见 §9）**
- 契约：KICKOFF_v4 §2 ｜ 参考：analysis/10、analysis/12 §I1（只借鉴格式规范，未复制 Daub 代码/资产）
- 写盘范围：`fourier_prompt/{layers.py,psd.py,kra.py,layer_cli.py}`、`compile.py`（仅 additive 层元数据）、`tests/layers/`、本报告。**未改** skill、`cli.py`、`render_fast.py`、`tools/`、`fdvideo*`、docs/、analysis/。

## 0. 结论

| 项 | 结果 |
|---|---|
| 01_rose（6 层 / 51 元素）R256 | PSD + KRA + 6 张逐层 PNG 全部成功；PSD 2,032,334 B，KRA 199,347 B，PNG 76,841 B；导出 4.6 s（含逐层渲染） |
| 06_cat_tophat（含 pencil）R128 | 导出成功；pencil 所在层 L5 自动回退 exact，层像素与 exact 分层渲染合成差 **max=1 / mean=0.0018**；PSD 705,512 B，KRA 55,752 B |
| 确定性 | 01 同参数（R256）两次导出：PSD、KRA、6 张 PNG **SHA256 全部相同**；R64 重复同样一致 |
| 测试 | `tests/layers/` **38 例全部通过（6.7 s）**；全仓 7 个测试目录 **375 例全部通过**（`compile.py` additive 改动未破坏既有 67 例 integration） |
| 未验证 | `psd-tools` 未安装（离线）；真实 Photoshop / Krita 未打开抽检；见 §9 |

## 1. 交付文件

```text
fourier-prompt/fourier_prompt/
├── layers.py      # doc_layers/filter_doc/render_layers/render_layer_pngs/add_layer_metadata
├── psd.py         # 手写 8BPS v1：bottom-first / straight alpha / paper / luni
├── kra.py         # 最小 KRA：ZIP/mimetype/maindoc/tile/merged/preview
└── layer_cli.py   # export / inspect-layers
fourier-prompt/tests/layers/   # 38 例（_fixtures + test_layers/test_psd/test_kra/test_cli）
fourier-prompt/reports/WP-I.md # 本文件
```

`compile.py` 的唯一改动：装配完成、`width_scale` 钉死后调用 `add_layer_metadata(doc, plan21)`，只写入 `doc["layers"]`；`.fbs` 写盘（`io_format.write_fbs`）验证会原样保留该顶层字段（已实测），所有渲染字段不变。

## 2. 层语义（`layers.py`）

### 2.1 接口与决定

| 接口 | 行为 |
|---|---|
| `doc_layers(doc)` | 优先读 `doc["layers"]`（`index/id/name/visible/opacity/order`，校验类型/范围/重复/上限 32/名称 ≤64）；缺失时按 `stroke["layer"]`/`text["layer"]` 去重合成 `L0..Ln`。返回追加 `stroke_count/fill_count/text_count/bbox`，按 `(order,index)` **bottom-first** 排序，底层 index 0。 |
| `add_layer_metadata(doc, source_plan)` | 原地 additive：plan `layers[]` 按列表序映射为编译器的整数层 index（与 `compile._layer_index` 一致），写 `index/id/name/visible/opacity/order`；无 plan 层时合成 `L0..Ln`。只碰 `layers` 键。 |
| `filter_doc(doc, i, include_above=False)` | 返回副本：`include_above=False` 只留 `layer==i`；`True` 留 `layer<=i`（该层 + **以下**各层，即画到该层为止的累积栈，与 docstring 的“该层（或以下各层）”一致）。不改输入（stroke dict 只读共享）。未知层 → `LayerError(FP_E_LAYER_INDEX)`。 |
| `canvas_size(doc, R)` | 与 skill `render._output_size` 对齐（长边=R，非方形时取整），单测逐分辨率核对。 |

`01_rose` 的 `source.fdsl.json` 没有 `layers`，因此表为合成名；层数/笔画数与 `render.json` 完全一致：

| index | name | visible | opacity | order | strokes | fills | bbox（世界坐标） |
|---:|---|---|---:|---:|---:|---:|---|
| 0 | L0 | true | 1.0 | 0 | 0 | 1 | 0.0332, 0.0332, 0.9668, 0.9668 |
| 1 | L1 | true | 1.0 | 1 | 26 | 0 | 0.0332, 0.0332, 0.9668, 0.9668 |
| 2 | L2 | true | 1.0 | 2 | 2 | 0 | 0.1289, 0.1289, 0.8711, 0.8711 |
| 3 | L3 | true | 1.0 | 3 | 0 | 2 | 0.3340, 0.3340, 0.6660, 0.6660 |
| 4 | L4 | true | 1.0 | 4 | 1 | 0 | 0.3535, 0.3535, 0.6465, 0.6465 |
| 5 | L5 | true | 1.0 | 5 | 18 | 1 | 0.2432, 0.2432, 0.7568, 0.7568 |

合计 51 元素，与 `render.json` 的 `len(strokes)==51` 一致。`06_cat_tophat` 的层 index 为 `0..7,9`（**没有 8**）：不重编号，PSD/KRA 记录 9 个文档层 + paper = 10。

### 2.2 逐层 straight alpha

skill 渲染器只输出 RGB（背景不透明），故每层渲染**两次**（黑/白底）反解 straight alpha：

```text
C_black = C·a
C_white = C·a + (1−a)·255
a = 1 − (C_white − C_black)/255 ; C = C_black / a
```

- 每层用 `render_fast.render_auto(..., quality=...)`，不支持 shape 自动回退 exact 并把 `fallback_reason` 逐层记录；
- `opacity != 1`：反解后 alpha 乘 opacity（烘焙），写盘层 opacity 固定 255，避免读取端二次应用；
- `visible=false`：仍渲染像素并写隐藏标记（PSD flags bit1=2 / KRA `visible="0"`），附 warning `W_LAYER_HIDDEN`；
- 空层写全透明像素 + `W_LAYER_EMPTY`；`W_LAYER_OPACITY_BAKED` 明示 opacity 已烘焙。
- 量化误差：单层“反解→合成回底色”对 direct layer render 实测 **max ≤2**（06 pencil L5 @R128 max=1, mean=0.0018；合成栈的累计量化在 01 @R64 为 mean=0.33 / max=15，因此 PSD/KRA 的 **merged/composite 一律直接用整图渲染**，不靠逐层重合成）。

## 3. PSD（`psd.py`）

### 3.1 结构证据（01 R256，自写解析器 + Pillow）

| 检查 | 实测 |
|---|---|
| 签名/版本 | `8BPS`, version 1 |
| 画布/深度/色彩 | 256×256，depth 8，color mode 3（RGB），merged channels 3 |
| 层记录 | **7 条，bottom-first：paper, L0, L1, L2, L3, L4, L5** |
| 层名 | 每条同时写 Pascal ASCII（4 字节对齐）与 `luni`（UTF-16BE）；01 全 ASCII，`luni_name` 解析回同名；Pillow 读出 `['paper','L0'...'L5']` |
| 通道 | 每层 4 通道 `R,G,B,-1`；channel length=**65538**=`2+256²`（含 2 字节 compression=0 标记，逐通道记录） |
| 混合/opacity/flags | `8BIM`+`norm`，opacity=255，visible 层 flags=0、hidden 层 flags=2（bit1=hidden 的真实读取语义） |
| layer-info 长度 | 偶数填充；外层 section 长度含内层长度字段 + global mask 长度（0） |
| 像素 | 自写解析器按 channel offset 取 plane：R/G/B/A 与 `render_layers()` 的 RGBA **逐字节相等**（01 全部 6 层）；paper plane = 底色 `[246,239,224]`、alpha=255 |
| merged composite | 与 `render_auto(R256, background=底色)` **逐字节相等**（Pillow 打开为 256×256 RGB） |
| Pillow | `Image.open` 成功，`format=PSD`，`size=(256,256)`，`mode=RGB`，`n_frames=7`，`layers` 名称顺序正确 |

PSD 采用**未压缩**通道（compression=0），因此体积可预测：`(层数+1) × 4 × (2 + W·H) + 3·W·H + 头部`。01 R256 为 2.03 MB（7×4×65,538=1.835 MB + merged 0.197 MB + 结构）。

### 3.2 层名与失败路径

- 非 ASCII（测试用“底色/墨线”）→ Pascal 写 ASCII 回退（`?` 替换），`luni` 写完整 UTF-16BE；warning `W_PSD_NAME_ASCII`；unicode 解析器读回原文。`luni` 的 4 字节长度按 **UTF-16 code unit** 计（psd-tools 的读取语义），payload 为 UTF-16BE。
- 构造 lone surrogate 名称使 UTF-16BE 编码失败 → warning `W_PSD_LUNI_FALLBACK`，仍写 ASCII Pascal，不丢层、不崩溃。
- fail-loud：非法尺寸/层数 → `FP_E_PSD_LIMIT`，层像素尺寸不符 → `FP_E_PSD_LAYER`，写盘失败 → `FP_E_PSD_IO`。

## 4. KRA（`kra.py`）

### 4.1 结构证据（01 R256）

| 检查 | 实测 |
|---|---|
| ZIP | 12 条；**第 1 条 `mimetype`，STORED**，内容 `application/x-krita` |
| 条目 | `maindoc.xml`、`documentinfo.xml`（固定日期，规避 Krita 空日期解析）、`preview.png`、`mergedimage.png`、`fourier-prompt/layers/layer0..layer6` |
| maindoc 层序 | **TOP-first**：`L5,L4,L3,L2,L1,L0,paper`；`filename` 与流一一对应，顶层 `selected="true"` |
| 层名 | UTF-8 XML 属性（“底色/墨线”测试读回原文）；hidden → `visible="0"`；opacity 烘焙，XML 固定 255 |
| tile 流 | 头 `VERSION 2 / TILEWIDTH 64 / TILEHEIGHT 64 / PIXELSIZE 4 / DATA n`；每 tile `x,y,LZF,16385` + **flag 0（未压缩）** + BGRA **channel-planar** 64×64×4 |
| 像素回读 | 自写解码器重建每层 BGRA，与 `render_layers()` RGBA **逐字节相等**（01 六层；48×32、192×128 多 tile/边缘填零均验证） |
| merged / preview | 256×256 RGB；merged 与整图渲染逐字节相等；preview 长边 ≤256 |
| 体积 | R256 原始 tile 流 7×262,481 B ≈ 1.8 MB，ZIP(deflate) 后 **199,347 B** |

### 4.2 简化与边界

- tile 未压缩（flag=0），但仍写 `LZF` codec 标记与 `csize=16385`；**未实现 LZF 压缩**（契约明确允许的简化）；
- 未写 ICC（避免引入第三方 ICC 资产；maindoc 不含 `profile`）；未写 `.defaultpixel`/`.icc` 旁文件、选区/滤镜层；
- 未写混合模式（仅 `compositeop="normal"`）、组、动画帧。

## 5. 06 pencil 回退 exact 证据

`06_cat_tophat.fbs`（51 元素）先 `fdraw_cli pack` 成 `out/layers/06.fdraw`（19,479 B）再走 CLI：

| 层 | 元素 | quality | fallback |
|---:|---:|---|---|
| 5 | 7 | **exact** | 7 条 `shape=pencil（需 exact）` |
| 其余 8 层 | 44 | fast | 无 |

- L5 单独 `exact` 渲染 vs “反解 RGBA 合成回底色”：R128 **max=1，mean=0.0018**；R32 单测门禁 ≤2。
- PSD 10 层（paper + 9 文档层），KRA 10 条 tile 流，合并图 128×128；导出耗时 5.7 s（含逐层渲染）。

## 6. CLI（`layer_cli.py`）

```powershell
python -m fourier_prompt.layer_cli export --fdraw X.fdraw --out-dir D [--psd] [--kra] [--layers-png] [--resolution 256] [--quality fast] [--json]
python -m fourier_prompt.layer_cli inspect-layers --fdraw X.fdraw [--json]
```

- 三者都不指定时默认全导出；输出 `<stem>.psd`、`<stem>.kra`、`<stem>_layers/layer_XX_<name>.png` + `layers.json`；`--name` 可改前缀；
- 输入 `.fdraw` 只读：`fdraw.unpack()` 验证 root/entries 后，只在内存副本上补层元数据；
- 退出码：0 成功；1 `LayerError`/`FDrawError`（坏容器、分辨率越界、导出失败）；2 argparse 用法错误（如 `--quality ultra`）；3 I/O/内部异常；
- `inspect-layers` 打印层表（idx/order/name/visible/opacity/strokes/fills/bbox）或 JSON。

## 7. 测试（`tests/layers/`，38 例）

| 文件 | 覆盖 | 例数 |
|---|---|---:|
| `test_layers.py` | 合成/元数据层表、additive、过滤（单层/累积/未知层）、PNG、opacity 烘焙、分辨率/quality 失败、canvas 尺寸、composite 重建、06 回退 exact | 12 |
| `test_psd.py` | Pillow 打开、头/层记录/通道长度、hidden flag、luni、ASCII 回退、逐层像素、merged 相等、确定性、01 层一致 | 11 |
| `test_kra.py` | ZIP/mimetype 首条 STORED、maindoc TOP-first/UTF-8/隐藏、tile 重建、多 tile 边缘、merged、确定性、01 层数、06 回退 | 10 |
| `test_cli.py` | 全量导出、默认全导出、inspect JSON/文本、退出码 1/2（坏分辨率、缺文件、坏 quality） | 5 |

命令：

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/layers -v     # Ran 38 tests ... OK (6.7 s)
```

回归：`tests/{core,integration,render,fdraw,tools,brush}` 共 337 例全部通过（integration 67 例含 `compile.py` 改动后复验）。

## 8. 体积 / 性能

| 产物 | R | 体积 | 备注 |
|---|---:|---:|---|
| 01 PSD | 256 | 2,032,334 B | 未压缩通道；7 层；2.03 MB |
| 01 KRA | 256 | 199,347 B | 7 层未压缩 tile，ZIP deflate |
| 01 逐层 PNG（6 张） | 256 | 76,841 B | RGBA straight alpha |
| 01 PSD | 64 | 127,694 B | 确定性复跑同字节 |
| 06 PSD / KRA | 128 | 705,512 / 55,752 B | 10 层，1 层 exact |
| 导出耗时 | 256 | 4.6 s | 6 层 ×2 次渲染 + 2 次整图渲染 + PNG |
| 导出耗时 | 128 | 5.7 s | 06：9 层 ×2（其中 1 层 exact）+ 2 次整图 |

## 9. 限制与未验证项（诚实清单）

1. **psd-tools 未安装（离线）**：PSD 证据来自 (a) Pillow 12.3.0 打开/取 composite/帧数/层名，(b) 本包自写解析器读签名/尺寸/深度/层记录/通道长度/plane 偏移。未做 psd-tools 的 8 项独立证据；真实 Photoshop / Clip Studio / Affinity 未打开。
2. **真实 Krita 未验证**：KRA 仅做 ZIP/XML/自写 tile 解码验证；未在 Krita 5.x 打开。已按公开格式写 `documentinfo.xml`、TOP-first、BGRA planar、raw flag=0；ICC 缺失可能导致 Krita 用默认 sRGB（预期行为）。
3. **PSD v1 范围**：不写混合模式/图层组/调整层/蒙版；所有层 `norm`；opacity 烘焙进 alpha（PSD opacity 字节=255，读端不会二次应用）；隐藏层仍写完整像素 + hidden flag；不写 ICC/DPI resource；通道未压缩（体积偏大）。
4. **KRA v1 范围**：未压缩 tile；无 LZF；无 ICC/旁文件/defaultpixel；无组/滤镜/选区；opacity 同样烘焙。
5. **alpha 量化**：反解 alpha 在 8-bit 下 ±1；单层合成回底色 max≤2、06 L5 max=1。**跨层独立重合成**会累计量化（01 R64 mean 0.33 / max 15），故 merged/composite 一律用整图渲染（逐字节相等），逐层重合成只用于诊断/测试。
6. **层名**：PSD 的 Unicode 依赖 `luni`；Pillow 只读 Pascal ASCII（显示 `?` 是 Pillow 行为，非文件错误）。KRA UTF-8 正常。
7. **层索引不重编号**：06 的 0..7,9 保留原 index（保证 `stroke["layer"]` 可追溯）；导出层数按实际条目计。
8. **不承诺回导**：PSD/KRA 是派生只读视图；未实现“改 PSD/KRA 后回写 `.fdraw`”。
9. R≤512、层数 ≤32（schema 上限）为导出硬限制；超出 fail-loud（`FP_E_LAYER_RESOLUTION/FP_E_LAYER_LIMIT`）。

## 10. 复现命令

```powershell
$PY = "<python>"
$FP = "<workspace>\fourier-prompt"; cd $FP

# 01：PSD + KRA + 逐层 PNG（DoD）
& $PY -m fourier_prompt.layer_cli export --fdraw out\verify\01.fdraw --out-dir out\layers\demo --psd --kra --layers-png --resolution 256 --quality fast --json

# 06：先打包为 .fdraw，再导出（pencil 自动回退 exact）
& $PY -m fourier_prompt.fdraw_cli pack --fbs out\wp-d\06_cat_tophat.fbs --out out\layers\06.fdraw
& $PY -m fourier_prompt.layer_cli export --fdraw out\layers\06.fdraw --out-dir out\layers\06_demo --psd --kra --layers-png --resolution 128 --quality fast --json

# 层表
& $PY -m fourier_prompt.layer_cli inspect-layers --fdraw out\verify\01.fdraw --json

# 测试
& $PY -m unittest discover -s tests/layers -v
```

## 11. 后续（非本批）

- 有环境时用 `psd-tools` + 真实 Photoshop / Krita 5.x 抽检（层可见性/opacity/Unicode 名/merged），并把结果补进本报告；
- PSD 可加 RLE 压缩与 ResolutionInfo/ICC（自生成 sRGB）以减体积；KRA 可补 LZF、ICC、defaultpixel；
- 若产品需要“PSD/KRA 回导”，需另立包定义映射与冲突语义（当前明确不支持）。
