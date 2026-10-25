# 模板：编辑已有绘图（**当前未实现**）

> ⚠️ **重要：`fourier-prompt` 目前没有编辑 op（`fdsl-edit/1` 未实现）。**
> 任何“改颜色 / 移动元素 / 删图层 / 局部重绘”的请求，都**不能**通过本包原地完成。
> 不要向用户承诺可编辑 `.fdraw`，也不要把 `source.fdsl.json` 当成可写对象。

---

## 当前能做到什么

| 请求 | 现状 | 正确做法 |
|---|---|---|
| 改某个元素的颜色/位置/文字 | ❌ 无编辑 op | 修改源 plan JSON → 重新 `schema` → 重新编译 |
| 在已有图上加一笔 | ❌ 无编辑 op | 在 plan 的 `elements` 里新增元素 → 重新编译 |
| 删掉某层/某元素 | ❌ 无编辑 op | 从 plan 删除 → 重新编译；或新建派生 plan |
| 只改分辨率/画质重新出图 | ✅ | 用同一 `.fbs`/`.fdraw` 重新 `render`（`--resolution` / `--quality`） |
| 导出分层后到 PS/Krita 里改 | ⚠️ 外部流程 | `layer_cli export --psd/--kra` 后在外部软件编辑（本包不再读回） |
| 读取 `.fdraw` 的源 plan | ✅ 只读 | `fdraw_cli unpack --out-dir D` 得到 `source.fdsl.json` 作为修改起点 |

---

## 编辑请求模板（用于向用户澄清/记录）

```text
目标：<想改什么>
输入：<plan 路径 / .fdraw 路径>
操作：<新增 / 删除 / 移动 / 改色 / 改文字>
对象：<element id 或 layer id / 描述>
新值：<颜色 / 坐标 / 文本 / 笔刷参数>
约束：<分辨率、是否保持其他元素不变、产出格式>
```

**注意**：即使填完模板，本包当前也只能：
1. `unpack` 取出 `source.fdsl.json`；
2. 由 agent/人修改这份 plan；
3. 重新走 `schema → plan2fbs → draw → fdraw`，生成**新的**文件。

---

## 为什么不能原地编辑

- `.fdraw` 是**只读容器**：`render.json` 是渲染权威，`source.fdsl.json` 只是随包携带的源计划；
- `fdsl-edit/1` 的 op 语义（目标寻址、局部重编译、diff/patch、回滚）在
  `docs/`/`analysis/` 中仍是设计稿，**代码里没有实现**；
- 直接手改 `.fbs` 会破坏量化、fingerprint 和 root hash；`fdraw_cli validate` 会拒绝，
  即使绕过校验也不能保证渲染一致。**禁止手改产物**。

---

## 错误示范（不要做）

```powershell
# ❌ 不存在这些命令/参数
python -m fourier_prompt.cli edit --fdraw X.fdraw --set color=red
python -m fourier_prompt.cli patch --plan plan.json --op replace
python -m fourier_prompt.fdraw_cli edit --fdraw X.fdraw
```

遇到这类请求，应明确回复：

> “当前版本没有编辑 op，不能原地修改 `.fdraw`。我可以基于源 plan 生成一份新的
> 派生 plan，重新编译出新的 PNG/PSD/KRA；原文件保持不变。”

---

## 未来接口（设计占位，勿调用）

若后续批次实现 `fdsl-edit/1`，预计形态是“对源 plan 的受限 op 列表”，例如：

```json
{
  "schema": "fdsl-edit/1",
  "base": "source.fdsl.json",
  "ops": [
    {"op": "set_color", "target": "e3", "color": "#3f6d8c"},
    {"op": "move", "target": "e3", "delta": [12, -8]}
  ]
}
```

**该格式当前不被任何命令接受**；上例仅用于说明未来方向。在此之前，唯一可靠路径是
“改源 plan → 重新编译”。

---

## 参考

- 只读解包：`python -m fourier_prompt.fdraw_cli unpack --fdraw X.fdraw --out-dir D`
- 分层外部编辑：`python -m fourier_prompt.layer_cli export --fdraw X.fdraw --out-dir D --psd --kra`
- 重新编译：`skill/SKILL.md` 的“标准工作流”
