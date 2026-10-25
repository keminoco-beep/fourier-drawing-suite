# 模板：自然语言 prompt → FDSL plan

> 用途：agent 收到“画一个 …”类请求时，**先填这份模板写出 FDSL v2.1 plan**，
> 再交给 `fourier-prompt` 校验/编译。本包没有本地 LLM，**不会**自动完成这一步。

---

## 0. 先判断

| 检查 | 不满足时 |
|---|---|
| 请求是“画一张静态图 / 海报 / 字标 / 几何插画” | 若是“位图转笔触”，改用 `fourier-brushstroke` |
| 画面能拆成 3–30 个形状/文本/笔触 | 过度复杂时应**降级为示意构图**，先说明简化策略 |
| 不需要像素级复刻参考图 | 需要复刻时说明做不到，改用位图→笔触流程 |
| 文本字符在打包 Noto Sans SC 子集内 | 缺字会 `FP_E_TEXT_GLYPH`；换字/换英文或直说限制 |

---

## 1. 填 plan（复制后改）

```json
{
  "schema": "fdsl/2.1",
  "name": "<短名，英文/下划线>",
  "description": "<一句话>",
  "canvas": {"width": 512, "height": 512, "background": [250, 248, 242]},
  "palette": {
    "ink": "#1e2430",
    "accent": "#c04030",
    "gold": "#e8c37a",
    "paper": "#faf8f2"
  },
  "layers": [
    {"id": 0, "name": "paper", "order": 0},
    {"id": 1, "name": "shapes", "order": 1},
    {"id": 2, "name": "type", "order": 2},
    {"id": 3, "name": "ink", "order": 3}
  ],
  "elements": [
    {"kind": "fill", "id": "e1", "layer": 0, "color": "paper",
     "poly": [[0, 0], [512, 0], [512, 512], [0, 512]]},

    {"kind": "fourier_gen", "id": "e2", "gen": "circle", "layer": 1,
     "params": {"at": [180, 220], "r": 120},
     "brush": {"preset": "basic_round_hard", "color": "gold", "alpha": 1.0}},

    {"kind": "fill", "id": "e3", "layer": 1, "color": "accent",
     "poly": [[300, 180], [470, 260], [330, 390]],
     "holes": [[[350, 250], [420, 270], [370, 330]]]},

    {"kind": "stroke", "id": "e4", "layer": 3,
     "points": [[80, 430], [170, 400], [280, 440], [430, 405]],
     "brush": {"preset": "ink_brush_rough",
               "width": [[0, 1.5], [0.5, 7], [1, 2]],
               "color": "ink", "alpha": 0.9}},

    {"kind": "text", "id": "e5", "layer": 2, "as": "outline",
     "text": "标题 / Title",
     "font": {"id": "source_han_sans_sc", "size_px": 56,
              "color": "ink", "align": "center", "line_gap": 1.15},
     "box": [40, 40, 432, 100]}
  ],
  "constraints": {"max_elements": 400}
}
```

---

## 2. 字段速查

| 字段 | 规则 |
|---|---|
| `schema` | 固定 `"fdsl/2.1"`（兼容读 `fdsl/2`） |
| `canvas.width/height` | 整数 16–8192；`background` 用 `[r,g,b]` 或调色板名/hex |
| `palette` | 可选；名称 → `#rrggbb`，元素颜色可直接写 hex |
| `layers[]` | `{id:int, name:str, order?, visible?, opacity?}`；element 的 `layer` 必须存在 |
| `elements[].kind` | `fourier_gen` / `stroke` / `fill` / `text` |
| `fourier_gen` | `gen` + `params`；闭合生成元默认填充，`closed:false` 可转笔画 |
| `stroke` | `points` ≥2；`brush.width` 标量或 `[[t,w],…]`（t 非降） |
| `fill` | `poly` ≥3 点；`holes` 可选（每个洞 ≥3 点） |
| `text` | `text` + `as:"outline"` + `font{id,size_px,color,align}` + `box[x,y,w,h]` |
| `brush` | `brush.preset` 必须内置；`width`、`color`、`alpha`、`shape` 可选 |
| `constraints.max_elements` | 默认 400，硬上限 2000 |

**颜色**：`#rrggbb` / `#rrggbbaa` / 调色板名。`#abc`、裸 hex 会被 repair 展开，但
**严格校验前建议写规范格式**。

**文本字体**：编译器只接受 `noto_sans_sc` / `noto_sans_cjk_sc` / `source_han_sans_sc`
（三者都映射到打包的 Noto Sans SC 子集）。schema 里其它内置 id 会 warning，
编译时会 `FP_E_TEXT_FONT`。**没有系统字体回退**。

**生成元参数**（常用）：

| gen | params |
|---|---|
| `circle` | `at, r, phase` |
| `ellipse` | `at, rx, ry, rot` |
| `regular_polygon` | `at, r, n, rot` |
| `star` | `at, r, ri, n, rot` |
| `rose` | `at, R/r, k, phi, rot` |
| `wave` | `at, w, amp, cycles, phase`（开曲线） |
| `spiral` | `at, r0, turns, growth, phase, ccw`（开曲线） |
| `lissajous` | `at, A, B, a, b, delta, rot` |
| `arc` | `at, r, from, to, rot`（开曲线，角度制） |

---

## 3. 从 prompt 到元素的映射表

| prompt 里出现 | 建议元素 |
|---|---|
| “圆 / 太阳 / 球” | `fourier_gen circle` 填充 |
| “星 / 齿轮 / 徽章” | `fourier_gen star` 或 `regular_polygon` |
| “波浪 / 丝带 / 下划线” | `stroke`（宽曲线）或 `wave` |
| “字 / 标题 / 标语” | `text as:"outline"`；先确认字符在子集 |
| “背景色块 / 纸张” | 最底层 `fill` |
| “描边 / 手绘线” | `stroke` + `ink_gpen` / `ink_fineliner` / `pencil_2b` |
| “粗笔 / 毛笔” | `stroke` + `ink_brush_rough` / `sumi_e` |
| “渐变” | ❌ 不直接支持；用多层半透明填充/笔触近似，并在回复里说明 |

**布局纪律**：先背景 → 大色块 → 中景 → 文本 → 细节笔画；layer `order` 或元素顺序
决定绘制次序，后画的盖住先画的。

---

## 4. 校验循环（必须做）

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"

& $PY -m fourier_prompt.cli schema --plan path\to\plan.json --strict
```

- 有 error：按 `path` 修字段，**不要**用猜的枚举值（未知 preset/gen 直接报错）；
- 只有 warning：看是否可接受（如层 visible/opacity 不参与 `.fbs` 渲染）；
- 通过后再编译：

```powershell
& $PY -m fourier_prompt.cli plan2fbs --plan path\to\plan.json --out out\my.fbs --resolution 256
& $PY -m fourier_prompt.cli draw --plan path\to\plan.json --out out\my_R256.png --resolution 256
```

---

## 5. 交付前自检

- [ ] `schema --strict` 通过（0 error / 0 warning，或 warning 已在回复中说明）；
- [ ] `draw` 成功，PNG 视觉上符合 prompt（打开图检查构图/遮挡/文字可读）；
- [ ] 文本没有缺字（缺字会编译失败，不会静默）；
- [ ] 元素数 ≤ 400，层数 ≤ 32；
- [ ] 回复里报告：plan 路径、PNG 路径、R、`.fbs`/`.fdraw` 路径、warnings；
- [ ] 没有声称“本地 LLM 生成”“字体回退”“可原地编辑”等未实现能力。

---

## 6. 参考

- 可直接编译的完整样例：`skill/examples/text_plan.json`、`skill/examples/simple_plan.json`
- 命令与限制：`skill/SKILL.md`
- 字段与错误码权威：包根 `README.md`、`fourier_prompt/schema.py`
