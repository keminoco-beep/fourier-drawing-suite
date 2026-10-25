# VERDICT v1.37 — 剪影感知笔触生成（生成期裁剪，非后期修图）（task-32 / verifier）

**结论（先看 6 行）**

1. **总评：PASS**。用户诉求“用绘制算法修复轮廓溢出、禁止后期修图”**已达成**：修复是**生成期笔触几何裁剪**
   （端点回缩 + 逐点宽钳制 + 丢细笔，位于 hard-cap/paint_order 之前），最终仅有一次**渲染合成羽化**（规格允许）；
   代码中**不存在**渲染后裁图/描边/模糊/涂白类 post-process。
2. **几何越界（我独立复算，复现 Lead 全部数字）**：旧 chibi 1280 **5,677 笔 >0.5px / 3,223 笔 >2px / max 16.973px**
   （完全一致）；新版**非底笔 0 / 0 / max 0.25px**（34,053 笔；41 条 >0.5px 全部是设计上跳过裁剪的 wash 底笔）；
   painted outside sil **13,325px → 0**；确定角色 mask（max≤0.88+opening）外被涂 **3.54% → 2.55%**。
   `--silhouette off` 的 strokes SHA 与旧版相同、PNG SHA **等于旧 ComfyUI 原始渲染**（逐字节等价）。
3. **视觉 PASS**：我自渲染 1280/2048 并自裁 3 处边缘 4×（左发/鳍、右鲸尾、头部蕾丝）：旧版边缘毛刺/飞边明显，
   新版轮廓干净、贴合剪影，整图主体（发/尾/蕾丝）完整、无新伪影；残余仅为“细特征略变细”（已文档化）。
4. **质量**：PSNR（同参考 resize 口径）**25.8273 → 25.3655（−0.4618 dB）**、SSIM 0.88795→0.88562；
   下降已解释并可复算（羽化 + 4,630 丢笔/3,893 裁剪笔，主要发生在光晕/细缝边）；report 内部 PSNR 25.0158→25.3636（+0.35）。
   **fill=0 经真实 ComfyUI 重建的 `.fbs` 独立验证**（stroke_count 34,107 / fill_count 0）；笔数 34,107 ≤100k。
5. **兼容/测试/包**：legacy v1.33 `30CDCE74…`、v1.34 `69820EEF…` 在 1536 **逐字节不变**（我自跑；silhouette 自动 off）；
   全量 **121 tests OK (skipped=3) / 85.9 s**（我复跑；含新 `test_silhouette.py` 15 例），`RUN_SLOW=1` **3/3 OK / 10.4 s**；
   v1.37 zip **52 文件 / 7,972,412 B / SHA256 `A877DDF9…E6BE539F`**，README 一致、52/52 逐文件 sha==源树、
   绝对路径 0、死链 0、旧包未动。
6. **发布建议**：可发布 v1.37（剪影修复 + 白底插画自动启用），发布说明须写清：PSNR −0.46dB 的羽化代价、
   `base_toned` 底色笔不裁剪（41 条由合成遮住）、细特征可能变细、依赖背景可分（auto 条件）；**不得**写成“完全无溢出/质量无损”。

> 方法：所有数字由 verifier 自己复跑（`work/verify6/`），未采信 fixer/analyst 自述。

---

## 1. 生成期修复确认（读 diff，逐条核）

| 检查项 | 结论 | 证据（`layered_base.py`，v1.37 冻结版） |
|---|---|---|
| 裁剪对象是 **stroke 几何**（points/widths），不是渲染图 | ✅ | `_clip_strokes_to_silhouette()` L1394–1469：`pts`/`wds` 端点回缩、`new_wds = min(wds, 2*max(d-margin,0))`、`max(new_wds)<min_width → drop`；颜色/gradient 数据不改 |
| 裁剪时机 = **生成后、hard-cap/paint_order 之前** | ✅ | 调用块 L1982–2010（注释明写 BEFORE hard cap and paint order）；hard cap 在 L2012，paint_order 在 L2090 |
| 端点回缩 / 宽钳制 / 丢笔 三要素 | ✅ | 回缩循环 L1431–1449；钳制 L1451；丢笔 L1452–1454；统计 clipped/trimmed/dropped/overshoot before-after |
| 唯一的图像空间操作 = 渲染合成羽化 | ✅ | `_composite_silhouette()` L1472–1481：`alpha=clip(1−m/feather)`，`ren=ren*alpha+1*(1−alpha)`，在最终渲染后调用 L2125；**无** PIL ImageFilter/sharpen/blur/描边/裁图 |
| 无“渲染后裁图/涂白/描边”伪装 | ✅ | 全文件 grep：PIL 仅用于底色 coverage 统计（L823/844）与最后保存 PNG（L2183）；无 post-process 函数 |
| 渲染合成属规格允许项 | ✅ | task 规格 E：“最终合成改剪影羽化（不是后期修图，是渲染合成的一部分）” |
| 独立旁证 | ✅ | analyst `work/silhouette_eval/no_postprocess_check.json`：交付 PNG 与“strokes 原始渲染 + 文档所述羽化” PSNR 32.57–37.53（差异仅边缘带），与文档一致，无额外处理 |

## 2. 几何越界（我自己实现，两套口径）

| 指标 | 旧 v1.36 | 新 v1.37 | 声明核对 |
|---|---|---|---|
| 全部笔 >0.5px（fg=0.945，控制点口径） | **5,677** | **41**（全部 `base_toned`） | ✅ 与 Lead 完全一致（41 条为未裁 wash 底笔，文档已写） |
| 全部笔 >2px | **3,223** | **41**（同上） | ✅ |
| **非底色笔**（n=38,683→34,053）>0.5 / >2px | **5,636 / 3,182** | **0 / 0** | ✅ |
| 非底色 max overshoot | 10.00px | **0.25px**（=margin 上限） | ✅ |
| 全部笔 max overshoot | **16.973px** | 16.973px（仅 54 条 wash 底笔） | ✅ |
| sil 口径越界（edt(fg)>1）全部笔 | **7,190 / 4,173 / 16.973** | 42 / 41 / 16.973（41 base） | ✅ 复现 Lead 的 7,190/4,173 |
| sil 口径非底色笔 | 7,149 / 4,132 / 9.999 | **1 / 0 / 0.572px** | ⚠️ 1 条 0.572px 残差（可忽略，见 §6-P3） |
| 渲染污染：painted outside **sil** | **13,325 px** | **0 px** | ✅ |
| 渲染污染：outside **fg(0.945)** | 0 px（旧硬切） | 0 px | ✅ |
| 渲染污染：outside **char(max≤0.88+opening)** | **56,821 px / 3.54%** | **40,976 px / 2.55%** | ✅（残余为角色半透明抗锯齿边） |
| `--silhouette off` 等价性 | — | strokes SHA **= 旧版** `FBAADF42…`；PNG SHA **= 旧 ComfyUI 原渲染** `92C73EE6…` | ✅ 逐字节等价（我自己重跑同命令） |

- **补充（我发现的残差）**：上述“越界≈0”是**控制点口径**（Lead/fixer 口径）。我按**稠密路径采样**（0.3–0.5px 步长、
  宽度线性插值）复核：非底笔仍有 **133 笔 >0.5px、18 笔 >2px、max 9.83px** 越出 fg——原因是几何钳制只约束折线控制点，
  **渲染器在控制点之间插值的曲线/圆头仍可外鼓**（多在鳍/发丝凹口）。但最终合成羽化把 sil 外全部置白，
  故 **painted outside sil=0**：表现为“曲线段被合成裁掉（局部笔触变细）”，**不是可见溢出**。见 §6-P3。

## 3. 视觉（我自己渲染 + 自裁，1280 与 2048）

| 图 | 内容 | 判定 |
|---|---|---|
| `work/verify6/visual/edge_A_left_hair_fin_4x.png`、`edge2048_A_*` | 左发/鳍：旧版边缘毛刺、飞边；新版贴合剪影、无碎屑 | ✅ 明显干净 |
| `work/verify6/visual/edge_B_right_whale_tail_4x.png`、`edge2048_B_*` | 右鲸尾：旧版轮廓外锯齿/褐色碎边；新版轮廓连续、干净 | ✅ |
| `work/verify6/visual/edge_C_head_lace_4x.png`、`edge2048_C_*` | 头部蕾丝：旧版边缘雾状毛边；新版蕾丝轮廓清晰 | ✅ |
| `work/verify6/visual/full_compare_chibi_1280.png`、`full_compare_2048.png` | 整图 source / old / new 并排：主体完整（发/尾/蕾丝/裙摆），无新伪影 | ✅ |
| 交叉核对 | fixer `work/silhouette/edge_before_after_4x.png`、`chibi_fix_R2048.png`、Lead `comfy_node_check/comfy_render_R2048.png` 与我的独立渲染一致（无溢出） | ✅ |

- 残余：**细特征略变细**（shrink=1.0 + 端点回缩所致，文档已列）；3 处边缘对照未见可见溢出或新伪影；
  未发现需要圈出的残留溢出处（若后续要更严格，可对比 shrink=0.5/1.0 的细鳍宽度）。

## 4. 质量 / 契约

| 指标 | 旧 v1.36（off） | 新 v1.37（auto→on） | 说明 |
|---|---|---|---|
| PSNR（同参考 resize 口径，我自己算） | **25.8273** | **25.3655（−0.4618 dB）** | 超出“Δ≤0.3dB”的建议线，需解释：13,325px 光晕越界笔不再绘制 + 羽化 + 4,630 丢笔 |
| SSIM | 0.88795 | 0.88562（−0.0023） | 基本不变 |
| 内部区 PSNR（距角色边界≥8px，我额外算） | 26.413 | 25.660（−0.75） | 丢笔主要发生在细缝/光晕边，内部也被少量影响 |
| 边缘环带（char 边界±3px）PSNR | 17.505 | 16.836 | 数值降但视觉更干净（羽化/去毛刺） |
| report 内部 PSNR/SSIM | 25.0158 / 0.88195 | 25.3636 / 0.88560 | +0.35dB（内部统计口径，参考同图 resize） |
| 笔数 / ≤100k / cap | 38,737 | **34,107**（clipped 3,893、trimmed 2,013 点、dropped 4,630） | ✅ ≤100k |
| **fill=0** | — | **真实 ComfyUI 重建 .fbs：stroke_count 34,107 / fill_count 0**（158.6MB） | ✅ 容器级独立验证 |
| auto 语义 | — | v1.36 风格（wash+region+白底 56.36%）→ **on**；legacy 模式 → **off**（我两个 legacy 重跑均 `silhouette_on=False`）；显式 off = 旧行为逐字节 | ✅ |
| report 字段 | — | 13 个 silhouette 字段 + white_background_frac + fill_count 齐全 | ✅ |

## 5. legacy / 测试 / 包 / 文档

| 项 | 结果 |
|---|---|
| v1.33 canonical（gate off，1536，我自跑 131s） | PNG SHA **`30CDCE74F11A8E2C0…EB9A01`** = doc 原图 ✅（81,815 笔 / 23.1883 / 0.61788；silhouette_on=False） |
| v1.34 推荐（1536，我自跑 122.5s） | PNG SHA **`69820EEF69386B8D…C265C3`** ✅（99,929 笔 / 24.3628 / 0.62545；silhouette_on=False） |
| 测试（我复跑 task-31 冻结版） | **121 tests OK (skipped=3) / 85.9 s / rc=0**；`RUN_SLOW=1` **3/3 OK / 10.4 s**；新 `test_silhouette.py` 15 例 |
| v1.37 包 | `dist/…v1.37-silhouette-aware.zip` = **52 文件 / 7,972,412 B / SHA256 `A877DDF9597BB8AC30229D1DCCD3BCDCE6920CD9EECD4AE2B20B8D80E6BE539F`**；`dist/README_v1.37.md` 声明一致；testzip=None；平铺 SKILL+scripts(31)/references(7)/assets(2)/tests(**11**)；**52/52 逐文件 sha==源树**；无 training/dist/.dsh/`__pycache__`；旧 v1.32–v1.36 包未动 |
| 可移植性 | 50 个文本文件扫描 `<path>`、`<path>`、`<path>`、`<path>` → **0 命中**；markdown 相对链接 **0 失效** |
| 文档诚实性 | SKILL.md「剪影感知笔触生成（v1.37）」数字与实测一致（5,677/3,223/16.97、非底 5,636→0、outside sil 13,325→0、char 3.54→2.55%、PSNR 25.8273→25.3655 −0.46 已解释、41 条 base 未裁、细特征/auto 条件/耗时 +14s 均列为已知限制）；**无“完全无溢出/质量无损/已彻底解决”类过度宣称** |

## 6. 独立发现的问题（均为 P3，不阻塞发布）

| # | 级别 | 问题 | 证据 | 建议 |
|---|---|---|---|---|
| 1 | P3 | **曲线段残差**：几何裁剪只约束折线控制点，渲染器在控制点间插值的曲线/圆头可外鼓；我稠密采样发现非底笔 133 笔 >0.5px（18 笔 >2px，max 9.83px）越出 fg。被合成羽化掩盖（painted outside sil=0），表现为局部笔触被裁、细特征变细 | 本报告 §2 补充；`work/verify6/overshoot_new.json`（dense_path） | 未来可在裁剪中按稠密路径采样钳制/重投影，而不只控制点；当前不影响可见溢出 |
| 2 | P3 | **PSNR 下降超建议线**：同参考口径 −0.4618 dB（建议 ≤0.3），内部区 −0.75dB；主因丢笔 4,630 + 裁剪 3,893 + 羽化；已文档化解释 | 本报告 §4；SKILL 已知限制 | 若需保真可用 `--silhouette off`（旧行为）或更小 shrink/feather；文档已给出 |
| 3 | P3 | **base_toned 54 条 wash 底笔不裁剪**：41 条自身越界 >0.5px（max 16.97），由合成遮住；report `overshoot_after_max=0.0` 只统计被裁剪笔 | 本报告 §2；report 字段；文档已注明 | 文档已写；如需彻底可去掉 skip（成本 +54 笔） |
| 4 | P3 | sil 口径下 1 条非底笔控制点 overshoot 0.572px（>0.5 一点点，margin 0.25） | 本报告 §2 | 可忽略；如需严格可把 margin 提到 0.3 |
| 5 | P3 | 确定角色 mask 外仍有 2.55% 被涂（before 3.54%）：残余是角色自身半透明抗锯齿边（char 38.49% < sil 42.63%），非越界笔 | 本报告 §2 | 属合理边缘；文档已说明 |
| 6 | P3 | 耗时 63.4s（旧 49.5s，+14s / +28%）：几何裁剪+EDT 直接 ~2–3s，余为环境波动 | FIX_REPORT；report seconds | 已文档化；34k 笔可接受 |
| 7 | P3 | `white_px_in_fg` 41,681（旧 20,142）：羽化后 halo 环未涂按设计（测试 D-2 已按公式断言），非白点伪影 | report；tests5 报告 D-2 | 文档可补一句“该值上升是按设计” |
| 8 | P3 | 节点重建 `.fbs` 158.6MB（节点摘要写 151.3MB，为 MiB/MB 换算差）；`.fbs` 未纳入本次 zip（属工作区交付物） | `comfy_node_check/build/…fbs.json`；node_result.json | 无 |

## 7. 裁决理由

- **用户核心要求达成**：溢出修复来自**生成期几何裁剪**（代码位置/逻辑已逐条核验），渲染端只有规格允许的剪影羽化，
  无任何“渲染后修图”；旧版越界笔 5,677/3,223/max16.97 → 非底笔 **0/0/0.25px**，painted outside sil **13,325→0**，
  `--silhouette off` 与旧版逐字节等价，legacy v1.33/v1.34 逐字节不变。
- **视觉确认**：1280/2048 三处边缘 4× 对照与整图并排均显示毛刺/飞边明显消失、主体完整；残余仅“细特征略变细”（文档化）。
- **质量代价如实**：PSNR −0.46dB（超出 ≤0.3 建议线）已解释（去光晕越界笔+羽化+丢笔），SSIM 基本不变，
  且 report 内部口径 +0.35dB；fill=0 经真实容器验证；文档无过度宣称。
- **结论：PASS**（残余均为 P3/已文档化，不阻塞发布）；发布说明按 §6 写清质量代价与限制。

## 8. 证据索引

| 内容 | 路径 |
|---|---|
| 旧/新越界复算（fg / sil / char 三口径、by-kind、dense） | `work/verify6/overshoot_old.json`、`overshoot_new.json`、`logs/overshoot_*.log` |
| 视觉对照（1280/2048，3 边缘 4× + 整图） | `work/verify6/visual/edge_*_4x.png`、`edge2048_*_4x.png`、`full_compare_*.png`、`visual_chibi_1280.json` |
| 我独立渲染的 2048 新旧图 | `work/verify6/visual/old_render_R2048.png`、`new_render_R2048.png` |
| `--silhouette off` 逐字节等价证据 | `work/verify6/runs/chibi_off1280/`（strokes SHA = 旧 `FBAADF42…`；PNG SHA = 旧 ComfyUI `92C73EE6…`） |
| legacy 1536 复现 | `work/verify6/runs/legacy_v133/`、`runs/legacy_v134/` |
| 测试日志（121 tests / slow） | `work/verify6/logs/tests_full.log`、`tests_slow.log` |
| 包校验 | `work/verify6/package_validation_v137.json`、`work/verify6/pkg_v137/`、`logs/pkg_validation.log` |
| fill=0 容器验证扫描 | `work/verify6/scan_fbs.py`（`comfy_node_check/build/source_v136.fbs.json`：stroke_count 34,107 / fill_count 0） |
| 复算脚本 | `work/verify6/overshoot_metrics.py`、`visual_compare.py`、`render2048.py`、`validate_pkg_v137.py`、`scan_fbs.py` |
