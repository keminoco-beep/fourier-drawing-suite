# WP-K — 文本/字体子系统（outline 模式）交付报告

- 日期：2026-10-20 ｜ 任务：task-21 ｜ 状态：**完成（batch 5）**
- 范围：字体资产/许可、字形轮廓→填充多边形、排版、`compile.py` additive 接入、测试与样例
- 渲染端承诺：**outline 模式下 `.fbs` 只含 `region_fill`，渲染路径零字体、零 fontTools**

## 1. 交付文件

| 文件 | 说明 |
|---|---|
| `fourier_prompt/fonts.py` | `load_font` / `font_info` / `subset_font` / `require_glyph` / 默认资产路径 / 构建 CLI |
| `fourier_prompt/text.py` | `layout_text` / `glyph_contours` / `text_fills`（轮廓展平、外轮廓/洞分类、box/transform） |
| `fourier_prompt/compile.py`（additive） | `kind=="text"` 与 `plan.texts[]` → 既有 fill 路径；`font=`/`text_mode=`；新统计 `text` / `text_fills` |
| `assets/fonts/NotoSansSC-VF.ttf` | 打包子集（1,120,384 B；3,904 glyphs） |
| `assets/fonts/OFL.txt` | SIL OFL 1.1 全文（4,463 B） |
| `assets/fonts/LICENSES.md` | 来源/许可/子集命令/sha256/RFN 改名/复现 |
| `tests/text/`（6 个测试文件 + fixture） | **54 例**全 OK |
| `tests/text/fixtures/text_plan.json` | 手写两行中文+英文+数字 plan（DoD 样例） |
| `tests/text/fixtures/text_sample_{256,512}.png` | 样例渲染结果（可复现） |
| `reports/WP-K.md` | 本报告 |

未改 `cli.py`、skill、其他包、docs/、analysis/（写盘边界见 KICKOFF v5 §4）。

## 2. 字体与许可

- **字体**：Noto Sans SC（思源黑体同源，OFL-1.1）。源文件
  `<path>`：17,773,244 B，SHA-256
  `763146584cf0710223441356b4395e279021b0806c196614377a7a0174ae074a`，
  `unitsPerEm=1000`，可变轴 `wght` 100–900（默认 Thin）。
- **打包资产**：静态 pin `wght=400` → 子集（ASCII 0x20–0x7E + GB2312 一级
  汉字 3,755 + 常用中文标点 + 测试串 `你好，Fourier! 2026` + `…`，共 3,903
  字符）→ 去 hinting/layout features → OFL RFN 改名
  `FdrawSubset NotoSansSC 3acf1b15`（name IDs 1/4/6/16/17）。
  产物 SHA-256 `ce2c3985a41fa7164cca59d8578208140767bfa8788766af6d90b43da6a9a268`。
- **许可**：`OFL.txt` 为上游 Source Han Sans `LICENSE.txt` 全文（一次联网
  下载，2026-10-20；BOM/CRLF 归一化），`LICENSES.md` 记录 source sha256、
  子集字符集 sha256、`pyftsubset` 等价命令、fontTools 版本（4.67.0）、
  RFN 改名与义务清单。子集可复现（两次输出字节相同，测试覆盖）。
- **不回退系统字体**：`load_font` 缺文件/缺表/坏 upem → `FP_E_TEXT_FONT`；
  字符不在 cmap → `FP_E_TEXT_GLYPH`（附 U+XXXX），从不静默跳过。

构建/复现：

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m fourier_prompt.fonts build `
    --source "<path>" `
    --out "assets\fonts\NotoSansSC-VF.ttf" --weight 400
```

## 3. 编译接入（additive）

`compile_plan(..., font=None, text_mode="auto")`：

| text_mode | 行为 |
|---|---|
| `"auto"`（默认） | 编译：显式 `"as"` 的 text、字体 id 属于本批（`source_han_sans_sc`/`noto_sans_sc`/`noto_sans_cjk_sc`）的 text、以及**未写 font.id** 的 text；`plan.texts[]` 一律编译。仅**旧内置但本批未打包**的 font id（Inter/Caveat/霞鹜文楷等，如 batch-2 fixture）保持旧行为（`FP_W_TEXT_SKIPPED` + `text_skipped`），因此既有 67 例 integration 不变；未知名 id 会 fail-loud |
| `"outline"` | 所有 text 元素与 `texts[]` 都烘焙（配 `font=PATH` 可绕过未打包的 id）；无 `as` 也编译 |
| `"legacy"` | 全部 text 跳过（显式降级，仍警告） |

- `text` / `texts[]` 生成的 fill 直接走既有 `_fill_record`（`region_fill` +
  `fill_holes`），不新增渲染语义；`source.fdsl` 仍保留原文/字体引用（可编辑）。
- 字体解析：`font=PATH`（或 `FontHandle`）覆盖默认资产；进入编译后 `font.id` 仅接受
  `source_han_sans_sc` / `noto_sans_sc` / `noto_sans_cjk_sc`，其他 id →
  `FP_E_TEXT_FONT`（避免“写 Inter 却贴 Noto”的静默替换；auto 模式下旧内置 id 在
  进入编译前即按 §3 表格跳过并警告）。
- 错误码：缺字 `FP_E_TEXT_GLYPH`；缺字体/坏字体 `FP_E_TEXT_FONT`；
  `as="glyph"` `FP_E_TEXT_MODE`；排版参数 `FP_E_TEXT_PARAM`/`_ALIGN`/`_BOX`；
  box 越界 → `FP_W_TEXT_OVERFLOW`（warning，不静默裁剪）。
- 新增 stats/ledger 字段：`text`（烘焙文本组数）、`text_fills`（文本贡献的
  fill 记录数）；`fill` 统计全部 fill 记录（显式 fill + 文本烘焙），`records` 为
  最终 `.fbs` 笔画数（文本全部是 `region_fill`）。

## 4. 排版与轮廓

- **排版**：`\n` 显式换行；box 内按字符贪心折行（CJK 任意断，ASCII 空格优先）；
  `left/center/right` 按行宽计算；`line_gap` 作为行高倍数（默认 1.2）；
  字号=px；基线=`box.y + ascent_ratio*size + line*size*line_gap`。
  输入字符逐字查 cmap，缺字立即失败；控制字符/制表符 fail-loud。
- **轮廓**：`DecomposingRecordingPen`（复合字形已分解）→ 一次/二次贝塞尔展平，
  每段 `max(1, ceil(seg_len / (0.25px * upem / size)))` 采样，容差约 0.25 px；
  闭合环隐式闭合（首尾不重复点）；退化环丢弃。
- **外轮廓/洞**：按**绕向**（TrueType 外轮廓顺时针；最大面积环定符号）分类，
  而不是天真嵌套——打包 Noto 子集的 `合` 存在多个**嵌套但同向的外轮廓**
  （重叠笔画），按嵌套会错误挖洞；按绕向 + 最小包含环挂洞后：
  `o`=1 洞、`8`=2 洞、`合`=0 洞、`回/日`=1 洞，渲染目评正确（见样例）。
- **变换**：`transform.center/anchor/scale/rotate` 作用于整段文本（统一 pivot），
  `skew` 明确拒绝 `FP_E_TEXT_TRANSFORM`。

## 5. 样例（DoD §2.4）

手写 plan：`tests/text/fixtures/text_plan.json`，两行
`你好，Fourier! 2026` / `Outline 字体烘焙`，加一个 `texts[]` 条目；
canvas 384×256，字号 32/24/20，中英文+数字+标点。

- 编译：3 个文本组 → **106** 条 `region_fill`、**13** 个 `fill_holes`、
  11,076 个轮廓点；`compile_plan` 1.26 s；`.fbs` **534,288 B**（未压缩 JSON，
  含 106 条 fit 系数）。
- 渲染：R256 → 256×171×3，R512 → 512×341×3；R256 0.055 s / R512 0.064 s。
  R256 PNG 像素 SHA-256 `13247b78020f4bee88c84caecdd3c6dfd72d440a2542574912d6bf40442b841a`。
- 确定性：同输入两次 `normalized_fbs` 相同（normalized SHA-256
  `feef9b13d1209033852d1790a4f0807d236524c9a558d63cce0ead1458e76615`），
  两次渲染像素 hash 相同（测试断言）。
- 样例图：`tests/text/fixtures/text_sample_512.png`
  !text sample

CLI（无需字体参数，plan 显式 `as:"outline"`）：

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m fourier_prompt.cli plan2fbs --plan tests\text\fixtures\text_plan.json --out $env:TEMP\text.fbs --resolution 256
& $PY -m fourier_prompt.cli draw      --plan tests\text\fixtures\text_plan.json --out $env:TEMP\text.png --resolution 256
```

## 6. 测试

`& $PY -m unittest discover -s tests/text -t tests/text` → **54 tests OK**（约 8 s）。

| 文件 | 覆盖 |
|---|---|
| `test_fonts.py`（9） | 资产/upem/字体表/许可全文/LICENSES 记录/缺字体/缺字/字符集覆盖/子集可复现与改名/子集缺字 |
| `test_contours.py`（8） | 闭合性/有限性/展平密度、`o` 1 洞、`8` 2 洞、`合` 重叠外轮廓不误判、空格、非法 gid、点数上限 |
| `test_layout.py`（8） | 换行/行距、left-center-right、box 折行、缺字、tab/控制字符、字号/对齐校验、advance=hmtx |
| `test_bake.py`（8） | 洞、alpha×opacity、整段旋转/缩放、skew 拒绝、box 越界 warning、缺字、默认 box |
| `test_compile_text.py`（13） | auto 跳过旧字体 id / 默认字体无 as、显式 outline、`texts[]`、force outline + font 覆盖、legacy、缺字/缺字体/未发布 font id/glyph 模式/非法 mode、层与 alpha |
| `test_render_text.py`（8） | 可见像素、归一化 hash+像素 hash 确定性、`.fbs` 无 font 依赖、holes roundtrip、**子进程屏蔽 fontTools 仍可渲染**、编译不 import fontTools、CLI plan2fbs+draw |

全量回归（见 §8）：core/brush/integration/fdraw/render/tools/layers/video 保持既有结果，
**integration 67 例全 OK**。

## 7. 已知限制（明确未做）

1. **`glyph` 模式未做**：`as="glyph"` 明确 `FP_E_TEXT_MODE`；本批只有 outline。
2. **竖排、字距/字偶、连字、合字、标点挤压、避头尾、RTL/bidi 未做**；行内 shaping
   只按 hmtx 单字 advance，西文不做 kerning（子集已删 layout features）。
3. **CLI `--font PATH` 未加**：`cli.py` 属本包禁写边界；目前 CLI 用默认资产，
   自定义字体需 API `compile_plan(font=PATH)`。KICKOFF §2.3 的 CLI 覆盖项留待
   Lead/后续批次接入（只需把参数透传给 `compile_plan`）。
4. **洞的合成用画布底色**：skill 渲染器 `fill_holes` 以 `base_color` 挖洞（既有
   语义）。文字压在彩色图形上时，字怀（`o`/`回` 内洞）会露出画布底色而非下层
   图形；纯色背景/文字层场景正确。若要“文字压在任意图形上”，需要渲染端支持
   多边形 even-odd 填充（非本批范围）。
5. **字体只有 Noto Sans SC 一个**（OFL）；schema 其他内置 id（Inter/宋体/文楷等）
   未打包，使用会 `FP_E_TEXT_FONT`。默认 `font.id` 可省略。
6. **体积**：outline 文本按每字轮廓+系数存 JSON，样例 3 组 106 fills 为 534 KB；
   设计估算的 40 字 5–30 KiB 未达到（`.fbs` 还保存了 fill 的 `coef/s/colors`）。
   简化（RDP 0.15 px、按组共用样式/点压缩）留待后续；不阻塞正确性。
7. **`.fdraw` 字体子集/`texts[]` 语义层未接**（WP-E 容器 batch 4 无字体块）；
   本批只保证 `.fbs` 自包含渲染。
8. 轮廓精度门限：0.25 px 展平（KICKOFF）而非 analysis/10 的 0.15 px RDP；
   目评/像素 hash 稳定，未做与 FreeType 的像素级对照。

## 8. 回归结果

（运行于 2026-10-20，本机 CPU）

| 套件 | 结果 |
|---|---|
| core | 83 OK |
| brush | 69 OK |
| integration | 67 OK |
| fdraw | 49 OK（1 skip） |
| render | 36 OK |
| tools | 33 OK |
| layers | 38 OK |
| video | 26 OK |
| **text（新增）** | **54 OK** |

合计 455 tests（1 skip），与批次 4 的 401 例基线相比无回归；`tests/text`
单独运行约 8 s，全量约 72 s。

## 9. 风险 / 后续

- 字体许可已闭环（OFL 全文 + RFN 改名 + LICENSES 记录）；若换字体需重新走
  `subset_font` + license 清单，勿用系统专有字体。
- 文本压在图形上时的洞合成语义（限制 4）是唯一会影响观感的边界，建议 P2.5
  渲染端升级 even-odd 填充或在 outline 烘焙阶段做多边形布尔运算（去洞）。
- `text_mode="auto"` 对“旧内置但未打包字体 id”保留跳过警告是兼容性妥协：新 plan
  应显式写 `"as": "outline"` 并使用本批字体 id（或省略 font.id）；后续可把默认切到
  outline 并更新 batch-2 断言。
