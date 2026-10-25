# fourier-prompt

FDSL v2.1 的**校验 / 确定性修复 / 编译器 CLI** 基座（工作包 WP-A，任务 task-12）。

当前版本 `0.1.0`，对应契约：`docs/impl/KICKOFF_v1.md` §2.1；
FDSL 语法见 `docs/文生图方案评估_v2_纯语言生图.md` §3.3 与
`docs/实现方案_v2_产品化_agent_skill_渲染端_ComfyUI.md` §4.2。

> 本批只交付 **core scaffold**：`schema` / `selftest` / `version`。
> `plan2fbs` / `pack` / `draw` / `eval` 属于批次 2–4，**未实现**；调用会抛
> `NotImplementedError`（退出码 3）并给出计划提示，不会假装成功。

## 快速开始

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"

& $PY -m fourier_prompt.cli --help
& $PY -m fourier_prompt.cli version
& $PY -m fourier_prompt.cli selftest
& $PY -m fourier_prompt.cli schema --plan path\to\plan.json
& $PY -m fourier_prompt.cli schema --plan path\to\plan.json --strict --json
```

Python API：

```python
from fourier_prompt import FDSchemaError, load_plan, repair_plan, validate_plan

plan = load_plan("plan.json")
errors = validate_plan(plan, strict=False)      # list[str]，空 = 通过
fixed, warnings = repair_plan(plan)             # (dict, list[dict])，不改输入
```

## 校验范围（v1）

| 覆盖 | 说明 |
|---|---|
| 顶层字段 | `schema` 必填，接受 `fdsl/2.1` 与兼容读取 `fdsl/2`；`canvas`/`elements` 必填；未知字段 → warning |
| `canvas` | `width`/`height` 整数 `[16, 8192]`；`background` 支持 `[r,g,b(,a)]` 或 hex/调色板名；`dpi` |
| `elements` | `kind` ∈ `fourier_gen`/`stroke`/`fill`/`text`（兼容 `type:"gen"` 别名） |
| 生成元 | `gen` ∈ circle/ellipse/arc/regular_polygon/star/rose/spiral/wave/lissajous/superellipse；`transform{center,scale,rotate,skew}`、`params` |
| 笔画/填充 | `points`≥2、`poly`≥3、`holes` 结构、点必须是有限 `[x,y]`、`closed` 布尔 |
| 笔刷 | `preset` ∈ skill 现有 20 个预设（未知 → 报错，不猜）；`width` 标量或 `[[t,w],…]` 曲线（`t` 非降、`[0,1]`）；`alpha/pressure` `[0,1]`；`shape` 枚举 |
| 颜色 | `#rrggbb`/`#rrggbbaa`（大小写不敏感）或已声明的调色板名；`#rgb`、裸 hex、`0x…` 属非规范写法，由 `repair` 展开 |
| 图层 | `layers[]` `{id,name,order,visible,opacity}`；元素 `layer` 必须指向已声明图层（或图层下标） |
| 上限 | 元素数（`constraints.max_elements`，默认 400，硬上限 2000）、32 层、64 调色板、64 文本、每元素 20000 点 |
| 文本（仅结构） | `texts[]`/`text` 元素：id `^t[1-9][0-9]{0,3}$`、`text` 非空、`as` ∈ outline/glyph、`font{id,size_px,color,alpha,align}`、`box`；字体语义留给后续 WP |

**不做**：生成元参数语义、文本排版/字形、`fdsl-edit/1` op 语义、`.fdraw` 容器、
像素级验收。这些由批次 2–4 的工作包负责。未知字段、未知字体、缺省 `font.id`
只进 warning（`--strict` 时升级为错误）。

错误对象统一为 `FP_E_*` + JSON path（如 `$.elements[1].brush.preset`），
warning 为 `FP_W_*`；`config/warnings.json` 预留 `expected/degrade/fatal` 三组白名单（初始为空）。

## 确定性修复（`repair_plan`）

只做“唯一合理答案”的修复，**从不猜**：

| 情形 | 动作 |
|---|---|
| 坐标越界 / 非有限坐标 | 越界裁剪进画布并 warning；`NaN/Inf` 点丢弃（点太少则报错） |
| 重复点 | 删除连续重复点 |
| 闭合 | `closed:true` 的笔画补回首点；**fill `poly`/`holes` 语义上闭合，不改写**（契约示例即为不闭合） |
| 数值范围 | `alpha/pressure/opacity`→`[0,1]`、`width`→`[0.1,256]`、`canvas`→`[16,8192]`、`scale`→`(0,64]`、`font.size_px`→`[1,4096]` |
| 宽度曲线 | 排序、`t` 夹取、去重、补齐 `t=0`/`t=1` 端点 |
| 颜色格式 | `#ABC`→`#AABBCC`、裸 hex 补 `#`、`0xRRGGBB`→`#rrggbb`；合法 hex 保留原大小写 |
| id | 非法/重复元素 id 按 `max+1` 重新分配（`e*`/`t*` 命名空间分离） |
| 图层/标签 | 缺失/非法 `layer` 回落到首层；非法 tag 丢弃、超过 8 个截断 |
| 数值 `params` | 非有限数替换为 `0.0` |

**不修复、直接报错**：未知 `gen`、未知 `brush.preset`/`shape`、未知颜色名、
缺失/非法 `canvas`、未知 schema 版本、无法满足的最小点数——拒绝并给 `FP_E_*` 错误码。

`repair_plan(..., strict=True)` 的语义是“拒绝而非修复”：
先按 strict 校验（warning 也算错误），再试运行一次修复；
只要需要改动就抛 `FDSchemaError(code="FP_E_STRICT")`，否则原样返回。

修复是**幂等**的：`repair_plan(repair_plan(p)[0])` 得到相同 plan 且第二次无 warning；
输入 plan 不会被就地修改。

## CLI 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（校验通过 / selftest 全绿） |
| 1 | 校验/验收失败（`FP_E_*`、`FP_W_*` 在 `--strict` 下、`repair` 拒绝） |
| 2 | 用法/输入错误（参数缺失、文件不存在/不可读、JSON 非法） |
| 3 | 内部异常（含未实现子命令的 `NotImplementedError`） |
| 130 | Ctrl+C 中断 |

`schema --json` 输出：`{ok, command, plan, schema, strict, counts{elements,layers,texts}, errors[], warnings[]}`；
每个 issue 含 `severity/code/path/message`（strict 升级项带 `promoted: true`）。

## 测试

```powershell
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/core -v
```

`tests/core/` 覆盖 schema 正反例、strict/warning、JSON 输出、CLI 退出码、修复幂等与不可猜枚举，
共 82 例（要求 ≥25）。测试只用 stdlib（`unittest` + `tempfile`），无需安装任何包。

## 目录

```
fourier-prompt/
├── pyproject.toml            # name=fourier-prompt, version=0.1.0, >=3.10, 无第三方依赖
├── README.md
├── config/warnings.json      # expected/degrade/fatal 白名单（初始为空结构）
├── fourier_prompt/
│   ├── __init__.py           # 版本 + 公共导出
│   ├── __main__.py           # python -m fourier_prompt
│   ├── schema.py             # FDSL v2.1 校验
│   ├── repair.py             # 确定性修复
│   └── cli.py                # schema/selftest/version + 预留子命令
└── tests/core/               # unittest（test_schema / test_repair / test_cli）
```

`fourier_prompt/brush/`、`assets/brush/`、`tests/brush/` 属于 WP-C；
`fourier-brushstroke/` 属于 WP-B，本包只读不改。

## 已知限制 / 契约说明

1. `plan2fbs` 等子命令尚未实现（批次 2–4），行为是显式失败而非降级。
2. 笔刷预设列表是 v1 冻结的 skill 20 预设置；WP-C 若引入 catalog 新预设，需同步更新
   `schema.BRUSH_PRESETS`（否则会被当作未知预设拒绝）。这是有意的 fail-loud。
3. `fill.poly` 不强制显式闭合（与 §3.3 示例一致）；若编译端需要闭合，由编译器在展开时处理。
4. 文本/编辑字段只做结构校验；字体 ID 白名单为 D6 的 6 个内置字体，未知字体仅 warning。
5. `constraints.max_ops/text_max` 只校验范围，不参与 v1 校验流程（编辑协议属批次 2）。
