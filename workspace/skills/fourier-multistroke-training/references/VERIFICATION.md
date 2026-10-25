# 交付校验报告

校验脚本：`scripts/verify_delivery.py`（每个交付目录内同时写入 `verification.json`）。
校验内容：`.fbs` mode / fill_count / stroke_count / 体积、render_contract、`.fdraw` validate / 体积、
渲染 PNG 的 PSNR/SSIM 复算与 SHA256 前缀。

## 结论

| 交付 | 笔画数 | fills | `.fbs` | `.fdraw` | validate | 校验 |
|---|---|---|---|---|---|---|
| 画质优先 `final_r21` | 21,694 | **0** | 449.7MB | 47.1MB | 通过 | **all_ok=true** |
| **默认 `final_lean_fix_r21`** | **17,693** | **0** | **286.0MB** | **32.2MB** | 通过 | **all_ok=true** |
| 精简 `final_lean_r21` | 17,228 | **0** | 279.3MB | 31.4MB | 通过 | **all_ok=true** |
| 极简 `final_minimal_r21` | 14,839 | **0** | 237.1MB | 26.8MB | 通过 | **all_ok=true** |

所有 `.fbs` 均满足 R=1536 / ε=0.08 / max_curves≤40000 / 体积<1GiB；所有 `.fdraw` 均满足
render.json≤1GiB、元素数≤100000；均**没有区域填充元素**。

## 渲染指标（同分辨率与源图比较）

| 交付 | 1536 PSNR/SSIM | 2048 PSNR/SSIM | 4096 PSNR/SSIM |
|---|---|---|---|
| 完整 `final_r21` | 18.4505 / 0.53311 | 18.3553 / 0.53538 | 18.1059 / 0.58735 |
| **默认 `final_lean_fix_r21`** | **18.3701 / 0.53045** | **18.2770 / 0.53362** | — |
| 精简 `final_lean_r21` | 18.2110 / 0.52685 | 18.1249 / 0.53079 | — |
| 极简 `final_minimal_r21` | 18.0275 / 0.52385 | 17.9554 / 0.52895 | — |

> PSNR 绑定分辨率；不同分辨率之间不直接横向比较。4096 为展示分辨率。

## 关键 SHA256 前缀（PNG）

- `final_r21`：1536 `9573baf884377b8c`，2048 `0fb48781e2a073cd`，4096 `31b02429db0f9932`
- `final_lean_fix_r21`：1536 `58b1605895f11252`，2048 `808c3c8f0dce8e3e`
- `final_lean_r21`：1536 `e49c078654192896`，2048 `4e074e7d3cd1b8ac`
- `final_minimal_r21`：1536 `ac5d8494407af483`，2048 `e1aadfce7149f72e`

完整字段见各目录 `verification.json`。

## 每轮训练产物审计

[`ROUNDS_AUDIT.md`](ROUNDS_AUDIT.md)：4 个 run 目录共 **132 轮**记录，
**每轮都有 PNG 与 report，缺失 0**；包括 `run_1408_q` 51 轮、`run_1024_q` 43 轮、
`run_1024` 31 轮、`run_1408_explore` 7 轮。

## 官方四维度量（R=1536，`fbs.py eval`）

详见工作区 `training/eval/SUMMARY.md`（该目录不随 skill 包分发）。三档结构 IoU 0.835–0.843、精度 0.900–0.902，
`wrong_pct=0`；线宽比 2.0、墨量比 1.02–1.04。官方 SSIM 与项目自算 SSIM 实现不同，
以官方值为准（精简版 0.5164 vs 自算 0.5308）。
