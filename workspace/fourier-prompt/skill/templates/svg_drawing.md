# 复杂图像：agent 手写 SVG 模板（fourier-prompt）

> 用途：让**在线 agent**（不是本地模型）直接写出可被 `svg_import` 无损导入的复杂 SVG
> （人物/风景/动物/机械/静物/建筑），再走确定性 `FDSL → FFT → PNG` 链路。
> 本文件是写作规范 + 质量门禁 + 自查清单，**不是模型 prompt**。
> 所有产物请在 SVG 内注明 `agent-authored, not model output`。

## 0. 一句话

**黑白线稿优先、白底 underlay、主体占满画布、30–120 个形状、400–3000 个点、
无渐变/text/image/filter/clip/mask；写完先跑质量门禁，再进 `svg_import`。**

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m fourier_prompt.svg_quality --svg ..\poc\agent_svg\svg\01_portrait.svg --json
& $PY -m fourier_prompt.svg_import --svg ..\poc\agent_svg\svg\01_portrait.svg `
    --out ..\poc\agent_svg\plans\01_portrait.json --png ..\poc\agent_svg\png\01_portrait.png `
    --target-size 384 --resolution 384 --json
```

## 1. 画布与构图

| 项 | 规则 | 理由 |
|---|---|---|
| 根元素 | `<svg xmlns=... viewBox="0 0 W H" width="W" height="H">`，**必须有 viewBox** | 无 viewBox 的 SVG（如 SVGThinker 输出）会被按像素猜画布，容易只占 0.6% |
| 画布 | 人物 480×640；风景/场景 640×420；方形图案/建筑 560×560；动物 560×520；静物 520×620 | 长边 ≤ 640，导入后长边 384 |
| 满幅 | **主体 ink bbox 占画布 ≥ 55%**（硬门禁 30%），四周留 4–8% 边距 | WP-U 的 P2（bbox 0.6%）就是构图失败 |
| 白底 | 第一层放 `<rect x="0" y="0" width="W" height="H" fill="#ffffff"/>` | 防相邻 fill 抗锯齿白缝（WP-T：Gandhi 白色裂缝） |
| 主体位置 | 中心/三分法；人物头顶 y≈8%H、肩线 y≈80%H | 半身像不要出现“漂浮的头” |
| 裁切 | 允许肩、树、建筑有意出画；不要整体缩小后居中 | 门禁按 ink bbox 判定 |

## 2. 结构/图层

推荐 4 层（用 `<g>` 分组，导入后成为 FDSL layer；**只用顶层 g，嵌套 g 的样式会继承**）：

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 640" width="480" height="640">
<!-- agent-authored, not model output -->
<g id="underlay">   <rect ... fill="#ffffff"/> </g>
<g id="mass" fill="#ffffff" stroke="#1a1a1a" stroke-width="2.4"
   stroke-linecap="round" stroke-linejoin="round">  <!-- 大块剪影/衣/头发 --> </g>
<g id="ink" fill="none" stroke="#1a1a1a" stroke-width="1.6"
   stroke-linecap="round" stroke-linejoin="round">    <!-- 轮廓/五官/纹理 --> </g>
<g id="detail" fill="none" stroke="#1a1a1a" stroke-width="0.9" opacity="0.75">
   <!-- 排线/毛发/砖缝/铆钉 --> </g>
</svg>
```

- **填充 + 描边分开写**：大块用 `fill="#fff" stroke="#1a1a1a"`；线条用 `fill="none"`。
- 一个 `<path>` 可以带多个子路径（`M...Z M...Z`），但**不要把 100 条线塞进 1 个 path**：
  门禁按 path/shape 数计数，且导入器按子路径拆，点预算照样消耗。
- 排线优先用**多条短 `M L`** 或 `<path d="M.. L..">`；不要用 pattern。
- 重复元素（齿轮齿、花瓣、砖、栏杆、星星）用脚本循环生成，坐标保留 1 位小数。

## 3. 路径语法与预算

**允许**：`M L H V C S Q T A Z`（绝对/相对均可，建议绝对）、`polygon/polyline/rect/circle/ellipse/line`、
`g` + `transform`（matrix/translate/scale/rotate）、`fill/stroke/stroke-width/opacity`。

**禁止**（导入器只降级或跳过，直接导致门禁失败）：

| 特性 | 结果 | 替代 |
|---|---|---|
| `<linearGradient>/<radialGradient>` | 取平均色 | 用 2–3 档纯色块分层 |
| `<text>` | **跳过** | 需要文字就画字形轮廓（或本 skill 的 FDSL text） |
| `<image>` / `<foreignObject>` / `<use>` | 跳过 | 展开成 path |
| `filter=` / `clip-path=` / `mask=` / `marker-*` | 忽略 | 直接画可见轮廓 |
| `<pattern>` | 近似平均色 | 循环画单元 |
| `stroke-dasharray` | 变实线 | 画短线段 |
| CSS 动画/`<style>` 复杂选择器 | 不保证 | 全部用 presentation 属性 |

**预算（复杂图）**：

| 指标 | 建议 | 硬上限/门禁 |
|---|---|---|
| 绘制形状（path + 基本图元） | 30–120 | `< 10` 拒绝 |
| 导入后路径（fill/stroke 子路径） | 30–300 | `< 10` 拒绝；`> 600` 被 trim |
| 总点数 | 400–3000 | `< 150` 拒绝；`> 200 000` 拒绝 |
| 颜色 | 2–6（黑/白/1–2 灰） | `> 32` 量化；`> 64` 拒绝 |
| 单 path 点数 | ≤ 800 | `> 4096` 简化 |
| 线宽（480 宽画布） | 主轮廓 2.2–3.0；细节 0.8–1.6 | `< 0.6` 导入后可能变 1 px 以下，渲染发虚 |
| 文件大小 | < 200 KB | `> 32 MB` 拒绝 |

## 4. 画法要点（按题材）

- **人物半身线稿**：先画头/发/肩三大块白色剪影，再画发际线、刘海分束、眉/眼（上眼睑粗、
  下眼睑细）、鼻（只画鼻梁+鼻翼 2 笔）、嘴（上唇 1 笔 + 下唇 1 笔）、耳、颈线、衣领、锁骨；
  发丝 10–20 条曲线，末端收细；最后 5–10 组排线压暗下颌/颈/衣褶。
- **风景**：远/中/近三层；地平线在 55–65% 高；山用折线剪影，水面用 5–10 条水平短线，
  树用 3–5 层三角/云朵状轮廓，云用闭合曲线，鸟用 2 笔 V 形；近景草丛用短弧线。
- **动物**：先整体剪影（头/耳/躯干/腿/尾），再补眼、鼻、口、爪、毛流；毛发用**顺着体块**
  的短曲线，不要全图乱线；阴影用排线而非渐变。
- **机械/机甲**：硬边直线 + 圆/圆弧；面板分缝用 `M L`，铆钉用 `<circle r="2">`（10–20 个），
  齿轮用脚本生成齿形 polygon，液压杆用双线；保持左右近似对称，透视线不超过 2 个灭点。
- **静物**：桌面用一条横线 + 2–3 条透视线；主体（瓶/花/果）先剪影后高光/暗部排线；
  花用 5–8 瓣的闭合花瓣（脚本生成），叶脉 3–5 笔；布褶用 4–8 条 S 曲线。
- **图案/建筑**：先定中轴与对称；建筑用拱/柱/窗/檐的重复单元；玫瑰窗/齿轮窗用脚本生成
  同心圆 + 放射线；边框用二方连续单元（10–20 个）；最后用排线表现砖石。

## 5. 质量门禁（写盘前必跑）

`python -m fourier_prompt.svg_quality --svg X.svg --json` 输出 `ok/score/metrics/checks`：

| 指标 | 默认阈值 | 说明 |
|---|---|---|
| `paths`（可绘制路径） | ≥ 10 | 导入后 fill+stroke 子路径数；低于此判“空/剪影” |
| `shapes`（源形状） | ≥ 10 | path/polygon/rect/circle/ellipse/line |
| `points` | ≥ 150 | 导入后总点数 |
| `ink_ratio` | 1%–92% | 非背景像素占比（栅格近似）；线稿典型 3–15% |
| `bbox_ratio` | ≥ 30% | ink bbox 面积 / 画布面积 |
| `colors` | ≤ 64 | 导入调色板数 |
| `unsupported` | ≤ 2 | gradient/text/image/filter/clip/mask 等出现次数 |
| `empty` / `degenerate` | 必须 false | 无元素 / 点数或 ink 近 0 |
| `dropped_ratio` | ≤ 35% | 导入 trim 掉的路径比例，高则细节丢失 |

失败处理顺序：**先修 bbox（缩放/平移）→ 再修 ink（加排线/加粗轮廓/减少大面积实心）→
再修形状数（拆 path/加细节）→ 最后清 unsupported**。不要靠调门禁通过。

## 6. 自查清单

- [ ] 有 `viewBox`，白底 underlay 是第一层；
- [ ] 主体 ink bbox ≥ 55% 画布，无大面积空白；
- [ ] 形状 ≥ 30、点数 ≥ 400、颜色 ≤ 6；
- [ ] 无 gradient/text/image/filter/clipPath/mask/pattern/use/dasharray；
- [ ] 每条 stroke 都有 `stroke-width`，`fill="none"` 的组不会误填充；
- [ ] 所有 fill 闭合（`Z`），孔洞用反向子路径或白 fill 覆盖；
- [ ] 线宽 ≥ 0.8（480 画布），细节层 opacity ≥ 0.5；
- [ ] 质量门禁 `ok=true`，`score ≥ 80`；
- [ ] `svg_import` 0 error，warnings 可解释；
- [ ] 注释写明 `agent-authored, not model output`；
- [ ] 目检 PNG：结构可辨（不是“抽象线条集合”）。

## 7. 骨架示例（可直接改）

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 640" width="480" height="640">
<!-- agent-authored, not model output -->
<g id="underlay"><rect x="0" y="0" width="480" height="640" fill="#ffffff"/></g>

<g id="mass" fill="#ffffff" stroke="#1a1a1a" stroke-width="2.6"
   stroke-linecap="round" stroke-linejoin="round">
  <!-- 头 -->
  <path d="M240 70 C190 70 160 105 158 160 C156 205 170 245 200 268
           C215 280 265 280 280 268 C310 245 324 205 322 160
           C320 105 290 70 240 70 Z"/>
  <!-- 颈 + 肩（出画） -->
  <path d="M212 258 L208 310 C150 330 110 360 92 420 L80 640 L400 640
           L388 420 C370 360 330 330 272 310 L268 258 Z"/>
</g>

<g id="ink" fill="none" stroke="#1a1a1a" stroke-width="1.7"
   stroke-linecap="round" stroke-linejoin="round">
  <path d="M178 150 C200 135 225 132 240 132 C255 132 280 135 302 150"/> <!-- 刘海 -->
  <path d="M186 178 C196 168 216 168 226 178"/> <!-- 眉 -->
  <path d="M188 186 C198 196 216 196 224 186"/> <!-- 上眼睑 -->
  <path d="M254 186 C262 196 280 196 290 186"/>
  <path d="M240 196 C238 210 236 220 240 226"/> <!-- 鼻 -->
  <path d="M224 244 C234 250 246 250 256 244"/> <!-- 嘴 -->
  <path d="M158 210 C150 300 145 380 150 460"/> <!-- 侧发 -->
  <path d="M322 210 C330 300 335 380 330 460"/>
</g>

<g id="detail" fill="none" stroke="#1a1a1a" stroke-width="0.9" opacity="0.7">
  <path d="M172 220 C166 300 164 380 168 440"/>
  <path d="M308 220 C314 300 316 380 312 440"/>
  <path d="M206 292 C214 300 226 304 240 304"/>
</g>
</svg>
```

## 8. 与流水线的关系

```text
agent 写 SVG（本模板） → svg_quality（门禁） → svg_import（确定性解析）
  → FDSL v2.1 → compile_plan（numpy FFT）→ PNG/.fdraw → 分层/视频
```

- 门禁不过就改 SVG，**不要**改 `svg_import` 阈值来“通过”；
- 渲染 R ≤ 384（本批次约定）；复杂图 30–300 元素，编译 1–5 s、渲染 0.2–5 s；
- 60 个元素以上的图优先单层 fill，减少 Fourier stroke（stroke 密集时渲染显著变慢）；
- 本模板只约束“可导入/可判分”，美术上限仍取决于 agent 的构图能力，不承诺照片级。
