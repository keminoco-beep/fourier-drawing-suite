# WP-S 报告 — SVG → FDSL 导入器

- 日期：2026-10-21 ｜ 状态：**已交付并实测**（`tests/svg` 74 tests OK；8.3 MB / 4.9 万路径真实 SVG → FDSL → compile → PNG 成功）
- 共享任务：task-29 ｜ 上游：WP-R（OmniSVG，写作时尚未产出 SVG）｜ 路线依据：`analysis/15_复杂图像生成路线.md` §2.2
- 写盘范围：`fourier_prompt/svg_import.py`、`tests/svg/`、本报告。**未改** `schema.py` / `compile.py` / `generators.py` / `planner.py` / `skill/` / `docs/` / `analysis/` / `refs/`（只读 import / 读取样例）。

## 1. 流程与接口

```
SVG (text / path / bytes)
  └─ xml.etree 解析 → 去除命名空间、CSS、defs（渐变/pattern 取平均色）
      └─ 递归遍历 g/transform/style，展平 path/curve/arc 到画布坐标
          ├─ 闭合填充环 → 环嵌套分析（evenodd / nonzero）→ fill(poly + holes)
          └─ 开放/描边中心线 → stroke(points, brush preset ← stroke-width)
              └─ 上限 / 溢出策略 → palette / layers / elements
                  └─ FDSL v2.1 plan（末尾用 schema.validate_plan_detailed 自检）
```

```python
from fourier_prompt.svg_import import svg_to_fdsl, brush_preset_for_width
plan, warnings, stats = svg_to_fdsl(
    "in.svg",                 # 或 SVG 文本 / bytes
    canvas=(384, 384),        # None: 由 viewBox 推导（长边 = target_size=384）
    max_paths=600, max_total_points=200_000, max_colors=32,
    max_points_per_path=4096, on_overflow="trim",   # 或 "error"
    flatten_tolerance=0.35,   # 画布像素
    include_fill=True, include_stroke=True,
    brush_preset=None, background="#ffffff",
)
# plan: 已通过 fdsl/2.1 校验，可直接 compile_plan(plan) + render
# warnings: [{"severity","code","path","message"}, ...]
# stats: source sha256/viewBox、elements/fills/strokes/points/colors、dropped、seconds
```

CLI（`--out` 省略时打印 plan；`--png`/`--fdraw` 触发 compile+render/pack）：

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m fourier_prompt.svg_import --svg ..\poc\complex_eval\out_ref0\ref0.svg `
    --out out\svg\ref0.plan.json --png out\svg\ref0.png --fdraw out\svg\ref0.fdraw `
    --resolution 192 --max-paths 600 --json
```

退出码：`0` 成功、`1` 导入失败（坏 XML/路径/超限/空图）、`2` 用法、`3` compile/render/IO 失败。
`plan["meta"]["source"]` 记录 SVG 文件名、SHA-256、字节数、viewBox；`plan["meta"]["svg_import"]` 记录容差/上限/策略。plan 内无时间戳，同输入 → **byte-identical**。

## 2. 支持矩阵

| 类别 | 已支持 | 说明 |
|---|---|---|
| 路径命令 | `M/L/H/V/C/S/Q/T/A/Z`，绝对+相对，隐式重复（M 后坐标=L），多子路径 | 贝塞尔自适应展平（容差 0.35 px）；arc 按 SVG 1.1 F.6.5 端点→圆心参数化采样；退化 arc→直线 |
| 图元 | `path`、`polygon`、`polyline`、`rect`、`circle`、`ellipse`、`line` | 圆/椭圆按弦高容差采样（12…2048 点）；`rx/ry` 圆角矩形按直角近似并 warning |
| 结构 | `g`、`a`、`switch`、嵌套 `g`、`defs` 跳过 | 顶层 `g` → FDSL `layers`（上限 32，超出合并并 warning）；嵌套组继承图层 |
| 变换 | `matrix/translate/scale/rotate(a[,cx,cy])/skewX/skewY`，可组合 | 全部烘焙进输出坐标；stroke-width 按 `sqrt(|det M|)` 缩放（非均匀变换为近似） |
| 样式来源 | presentation attributes、`<style>` 最小 CSS（`tag`/`.class`/`#id`/`tag.class`/`*`）、`style=""`、`currentColor` | CSS 优先级按 (id, class, tag) + 源顺序；inline style 最高；不支持后代/伪类选择器（忽略） |
| 绘制属性 | `fill`、`fill-opacity`、`fill-rule`、`stroke`、`stroke-width`、`stroke-opacity`、`opacity`、`display`、`visibility` | 组 opacity 逐层相乘后写入元素 `opacity`；`display:none` 剪枝；`visibility:hidden` 跳过元素 |
| 颜色 | `#rgb`、`#rgba`、`#rrggbb`、`#rrggbbaa`、148 CSS named、`rgb()`/`rgba()`（逗号/空格/百分比） | 颜色去重进 palette（`c0…cN`）；alpha 保留在元素 opacity（24 位 palette 值） |
| 视口 | `viewBox`、`width/height`（px/pt/pc/mm/cm/in）、`preserveAspectRatio`（meet/slice/none + xMin/xMid/xMax/yMin/yMid/yMax） | 无 viewBox 时用 width/height，仍缺则 384×384 + warning |
| 填充规则 | `evenodd` 按嵌套奇偶；`nonzero` 按相邻环绕向 | 同向嵌套环在 nonzero 下是“岛”，输出为同色独立 fill 区域（视觉等价） |
| 描边 → 笔刷 | `stroke-width` → catalog：≤0.9 `ink_fineliner`；≤2.2 `ink_gpen`；≤5.5 `ink_brush_rough`；>5.5 `basic_round_hard` | `brush_preset=` 可强制覆盖；宽度夹到 [0.1, 256] 并 warning |

## 3. 降级策略（v1：明确 warning，不静默）

| Code | 输入 | 处理 |
|---|---|---|
| `SI_W_GRADIENT_APPROX` | `linearGradient`/`radialGradient`（含 `href` 继承 stop） | 取 stop 加权平均色；stats 计数 |
| `SI_W_PATTERN_APPROX` | `pattern` | 取 pattern 内容平均 fill/stroke 色；空则 `#808080` |
| `SI_W_PAINT_FALLBACK` | `url(#missing)` | 黑色 + warning |
| `SI_W_FILTER_IGNORED` / `SI_W_CLIP_IGNORED` / `SI_W_MASK_IGNORED` / `SI_W_MARKER_IGNORED` | filter/clip-path/mask/marker-* | 忽略效果，保留几何与颜色 |
| `SI_W_DASH_IGNORED` | `stroke-dasharray` | 实线描边 |
| `SI_W_TEXT_SKIPPED` / `SI_W_IMAGE_SKIPPED` / `SI_W_USE_SKIPPED` / `SI_W_FOREIGNOBJECT_SKIPPED` / `SI_W_UNSUPPORTED_ELEMENT` | text/image/use/foreignObject/未知元素 | 跳过该元素（子节点不展开）；`<use>` 不解析引用 |
| `SI_W_ROUNDED_RECT_APPROX` | `rect rx/ry` | 直角矩形 |
| `SI_W_UNSUPPORTED_PAINT` | `hsl()`、非法颜色 | 黑色 + warning |
| `SI_W_POINTS_DECIMATED` / `SI_W_PATH_LIMIT` / `SI_W_TOTAL_POINTS_LIMIT` / `SI_W_COLOR_QUANTIZED` / `SI_W_HOLE_LIMIT` | 超限 | 降采样 / 丢弃 / 最近色量化 / 丢最小 hole，均 warning |
| `SI_W_DEGENERATE_SKIPPED` | 空/零面积几何 | 跳过 |
| `SI_W_WARNINGS_SUPPRESSED` | 单一 code 超过 8 条 | 汇总一条，避免警告爆炸 |

## 4. 上限与溢出策略

| 配置 | 默认 | 硬上限 | 溢出行为 |
|---|---|---|---|
| `max_paths`（生成的 fill+stroke 元素数） | 600 | 2000（schema `MAX_ELEMENTS_HARD`） | `on_overflow="trim"`：按“墨量”（fill 面积 / stroke 长度×宽度）保留最大者，**保持文档顺序**；`"error"`：`SI_E_PATH_LIMIT` |
| `max_total_points` | 200 000 | —（≥16） | trim：按文档顺序累加，超预算停止并 warning；error：`SI_E_POINT_LIMIT` |
| `max_colors` | 32 | 64（schema `MAX_PALETTE`） | 超出后最近 RGB 量化 + `SI_W_COLOR_QUANTIZED` |
| `max_points_per_path` | 4096 | 20000（schema `MAX_STROKE_POINTS`） | 均匀抽稀 + warning；fill poly 仍 ≤8192 |
| `max_warnings_per_code` | 8 | — | 超出汇总 |
| 源文件大小 | — | 32 MB | `SI_E_TOO_LARGE` |

单元素点数满足 schema：fill poly ≤ 4096（上限 8192）、holes ≤ 64、stroke ≤ 4096（上限 20000）。生成后调用 `validate_plan_detailed`，失败则 `SI_E_PLAN`（fail-loud）。

## 5. 测试

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/svg -v          # 74 tests OK (1 skipped: opt-in real case)
$env:FDSL_SVG_REAL="1"
& $PY -m unittest discover -s tests/svg -p "test_real.py" -v   # 真实 8.3MB SVG
```

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_paths.py` | 7 | 全命令、相对/隐式、多子路径、arc 采样/退化、坏数据 fail-loud、空路径 |
| `test_shapes.py` | 7 | rect/circle/ellipse/polygon/polyline/line、圆角矩形、零尺寸、组/图层 |
| `test_transforms.py` | 7 | translate/scale/rotate/matrix/skew、嵌套组合、viewBox 偏移、letterbox、坏 transform |
| `test_fill_holes.py` | 7 | evenodd donut、nonzero 同向/反向、多 hole、岛中岛、hole 上限、微小环 |
| `test_stroke.py` | 7 | 宽度→preset 阈值、变换缩放、开闭标志、fill/stroke opacity、顺序、强制 preset、细线夹取 |
| `test_colors.py` | 6 | 短/长/8 位 hex、named/rgb/rgba/currentColor、palette 去重、量化、非法 paint |
| `test_degrade.py` | 16 | 渐变/渐变 href/pattern/缺失引用、filter/clip/mask/dash、text/image/use、display/visibility、CSS 优先级、空/坏/非 SVG |
| `test_limits.py` | 8 | max_paths error/trim、总点数 error/trim、单路径抽稀、非法配置、plan schema、fill/stroke 开关 |
| `test_determinism.py` | 3 | 两次转换 byte-identical、basename+SHA 元数据、palette 顺序 |
| `test_render.py` | 5 | compile+render（含 hole 像素断言）、stroke-only、CLI plan+PNG+fdraw、错误退出码 |
| `test_real.py` | 1（可选） | 真实 SVG：转换 + compile + render 非空白 |

不依赖 pytest / cairosvg；除渲染用到的 numpy/Pillow（现有依赖）外，导入器本身仅标准库。整套快速用例约 **1.1 s**。

## 6. 真实 SVG 实测

样例：`poc/complex_eval/out_ref0/ref0.svg`（8.26 MB，49,129 个节点、49,110 条可绘制路径；由本地 brushstroke 导出器产生的复杂 SVG，**不是 OmniSVG 输出**）。WP-R 写作时尚未产出：`poc/omnisvg/` 与 `reports/WP-R.md` 均不存在；`test_real.py` 会在 `poc/omnisvg/**/*.svg` 出现后自动优先使用它。

| 配置 | 导入耗时 | 元素 | 点数 | 颜色 | compile | render | 非白像素 | 唯一色 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 默认（max_paths=600, trim） | 5.5 s | 600 fill | 49,568 | 32（量化） | 3.3 s @R192 | 0.32 s | 55.0% | 1,325 |
| `max_paths=64` | 2.9 s | 64 fill | 29,798 | 31 | 1.4 s @R192 | 0.10 s | 49.7% | 872 |
| 测试用 `max_paths=32` | 2.8 s | 32 fill | 23,281 | 13 | ok @R96 | ok | > 5% | — |

- 49,110 条路径 → 默认保留 600（丢弃 48,510，`SI_W_PATH_LIMIT`），另有 8 条零面积路径 `SI_W_DEGENERATE_SKIPPED`、颜色量化与警告汇总；**没有任何静默降级**。
- 渲染非空白、结构可辨；说明 8 MB / 4.9 万路径量级不会让导入器或 compile 崩溃，但默认 600 元素已是 CPU 端可接受的上限（R192 下 compile+render 约 3.6 s，含导入约 9.1 s）。
- CLI 小样例（240×160 四元素）：canvas 384×256、4 元素 / 86 点 / 4 色、PNG 7.3 KB、`.fdraw` 13.8 KB，视觉检查通过（fill/描边/背景均正确）。

## 7. 限制与后续

1. **渐变/pattern/filter 仅近似**：渐变取平均色，会丢失方向/多层光效；复杂插画的渐变阴影在 FDSL 中不可见。若 OmniSVG 输出以纯色 fill 为主（预期），影响小。
2. **text/image/use 跳过**：SVG 文字不转轮廓（FDSL text 需要字体/box 语义，交给后续 WP）；`<use>` 不解析。
3. **描边中心线**：stroke 被视为中心线+brush 宽度，无法还原 calligraphic 轮廓（外轮廓描边路径会被当作填充，通常仍正确）；非均匀变换下宽度用几何平均近似。
4. **nonzero 同向嵌套环**：输出为同色独立 fill（视觉等价，不是单一 poly+holes）。
5. **CSS 子集**：不支持后代/子代选择器、伪类、`@media` 内的规则（忽略而非报错）。
6. **性能**：全量展平后按墨量裁剪；4.9 万路径默认配置 5.5 s。更大文档建议先 `max_paths` 限制或预处理；渲染端可后续做区域分块。
7. **未做**：SVGZ 解压、外部实体、字体轮廓、渐变网格、pattern 平铺、clip/mask 几何求交。

建议后续：WP-R 产出 SVG 后直接跑 `test_real.py`（自动优先 OmniSVG 输出）；若 OmniSVG 路径数 >600，用 `--max-paths` 或按 `--on-overflow error` 先筛查复杂度，再决定是否提高上限。
