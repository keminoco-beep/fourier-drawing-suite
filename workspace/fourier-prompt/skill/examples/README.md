# 示例 plan

两个文件都是合法、可通过 `--strict` 校验、可直接编译的 FDSL v2.1 plan。
命令均从包根 `fourier-prompt/` 执行。

---

## `text_plan.json`（主 demo）

**内容**：384×256 画布；纸张底色 + 两行 outline 文本（`你好，Fourier!` / `傅里叶笔触 2026`）
+ 一条 `ink_brush_rough` 金色下划线 + 一个五角星；3 个图层（paper / type / accent）。

**覆盖能力**：文本烘焙、调色板、笔刷宽度曲线、`fourier_gen star`、图层顺序、PSD/KRA 导出。

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"

# 一键（推荐）
& $PY scripts\demo.py --plan skill\examples\text_plan.json `
    --outdir out\demo --psd --kra --resolution 256

# 分步
& $PY -m fourier_prompt.cli schema --plan skill\examples\text_plan.json --strict
& $PY -m fourier_prompt.cli plan2fbs --plan skill\examples\text_plan.json `
    --out out\demo\text_plan.fbs --resolution 256
& $PY -m fourier_prompt.cli draw --plan skill\examples\text_plan.json `
    --out out\demo\text_plan_R256.png --resolution 256
& $PY -m fourier_prompt.fdraw_cli pack --fbs out\demo\text_plan.fbs `
    --source-plan skill\examples\text_plan.json --out out\demo\text_plan.fdraw
& $PY -m fourier_prompt.layer_cli export --fdraw out\demo\text_plan.fdraw `
    --out-dir out\demo\layers --psd --kra --resolution 256
```

**预期**（本机实测，R=256）：`.fbs` ≈ 338 KiB，77 条记录（76 fills + 1 stroke，其中文本烘焙 74 fills），
PNG 256×171 ≈ 9.3 KiB，`.fdraw` ≈ 38 KiB，PSD ≈ 813 KiB，KRA ≈ 39 KiB；总耗时约 3.8 s。

**字体**：使用 `source_han_sans_sc`（映射到打包的 Noto Sans SC 子集）。示例字符均已覆盖；
换字若不在子集会 `FP_E_TEXT_GLYPH`，不会回退系统字体。

---

## `simple_plan.json`（冒烟/最小样例）

**内容**：320×320 画布；金色圆 + 蓝色六角星（均为 `fourier_gen` 填充）+ 一条 `ink_gpen`
笔刷曲线；2 个图层。**无文本、无字体依赖**，编译渲染都很快。

```powershell
# 一键
& $PY scripts\demo.py --plan skill\examples\simple_plan.json --outdir out\simple --resolution 128

# 只验证
& $PY -m fourier_prompt.cli schema --plan skill\examples\simple_plan.json --strict
```

**预期**：编译 <0.1 s；R=128 时 PNG ≈ 4 KiB 量级；适合 CI/回归和快速确认环境可用。

---

## 确定性

同一 plan + 同一 `--resolution` 重复运行 demo，以下文件字节一致（SHA-256 相同）：

- `<stem>.fbs`
- `<stem>_R<R>.png`
- `<stem>.fdraw`

demo 会移除 `.fbs` 的墙钟字段 `created`（纯元数据；`compile.normalized_fbs` 也排除它），
`.fdraw` 使用固定 ZIP 时间戳，因此可以按 hash 比较。直接调用 `cli plan2fbs` 时 `created` 存在，
但该字段不参与渲染，规范化 hash 仍稳定。

---

## 自己写 plan

从任一示例复制，按 `skill/templates/text_to_drawing.md` 的字段表和检查表修改，
先 `schema --strict` 再编译。不要手改 `.fbs` / `.fdraw` 产物。
