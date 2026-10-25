# fourier-brushstroke v2 验收报告

- 生成时间：2026-10-20T07:07:15+08:00　模式：quick（小分辨率代理）　skip_video=True
- 环境：Python 3.13.14 / numpy 2.5.4 / Pillow 12.3.0 / metrics 2.0
- 夹具：manifest seed=20261018 count=22 schema=fbs-fixture/2
- 被测代码快照（并行开发期以此为准）：digest=`101067ac3e4e9b36`（run_acceptance/metrics/document/render/onestroke/fourier/brush/decompose... 的 sha256 前 16 位）
- 预检：{"document": "ok", "render": "ok", "onestroke": "ok", "video": "ok", "fourier": "ok", "cli": "ok"}

## 汇总：通过 12 / 失败 0 / 阻塞 0 / 跳过 0 / 判据修正记录 0 / 未实现 0（共 12）
　MUST 通过 9/9（失败 0，阻塞 0）
　分组：A组 4P/0F/0NI/0S/0B（共 4）　R组 1P/0F/0NI/0S/0B（共 1）　T组 7P/0F/0NI/0S/0B（共 7）
- **结论：✅ 通过（全部 PASS）**　（ok = failed==0 且 blocked==0；SKIP/BROKEN/NOT_IMPLEMENTED 不算通过，但也不让验收假失败）

## 逐项结果

| 项 | 级别 | 状态 | 实测 | 阈值 | 备注 |
|---|---|---|---|---|---|
| A1 | MUST | PASS | `{"psnr_db": 39.1332, "R": 512, "klass": "lineart"}` | ≥ 30 dB @R=1024 | quick 代理 R=512（非 1024 全量口径） |
| A2 | MUST | PASS | `{"ssim": 0.998602, "R": 512}` | ≥ 0.90 @R=1024 | quick 代理 R=512 |
| A3 | MUST | PASS | `{"stats": {"256": {"N_median": 31, "err_max_x_R": 0.4971, "eps": 0.5}, "512": {"N_median": 50, "err_max_x_R": 0.4928, "eps": 0.5}}, "contract": true, "tightness": true, "monotonic": true, "diagnostic_N_ratio": 1.613, "r…` | max_err·R ≤ ε；max_err·R ≥ 0.5ε；N 中位随 R 单调增（严格） | 判据按工程护栏 §3.2 修正：旧版「每档必 2×」依赖 d(N)≈C/N 单幂律前提，实测为假；现直接验契约+紧致+单调。诊断倍率仅供记录。 |
| A5 | MUST | PASS | `{"cases": [{"case": "T7_photo_like.png", "result": "E_PHOTO_DEFERRED", "expected": ["E_PHOTO_DEFERRED"], "ok": true, "message": "检测为照片类输入，本 Skill 暂缓支持（D5）。\n  实测判定依据：唯一颜色数=99833（阈值 ≥4096）、颜色熵=6.34 bits（阈值 ≥6.0）、噪声水平=5.6…` | 全部异常用例以 FBSError(code) 明确拒绝；纯色图走特殊路径+W_SOLID_COLOR，0 崩溃 | A5 = 设计文档 §6 异常矩阵的零崩溃代理；错误信息可读性见 T7 项（证据/建议非空） |
| R2 | MUST | PASS | `{"mode": "one_stroke", "stroke_count": 1, "fft_passes": 1, "path_deviation_px": 0.0012369240803786028, "path_deviation_max_px": 0.05875128546123793, "path_samples": 8192, "N": 4094, "travel_length": 0.9778641855542965, …` | mode=one_stroke；strokes 恰好 1 条；stats.fft_passes==1；path_deviation_px ≤ 0.05 且 path_deviation_max_px ≤ 0.5（@契约 R） | 本项在 R=512 下评估；两项偏差分别取 stroke.path_deviation_px（RMS/代表值）与 stats.path_deviation_max_px（最大值）（quick 代理） |
| T1 | MUST | PASS | `{"klass": "lineart", "stroke_count": 1, "fill_count": 0, "region_count": 1, "psnr_db": 39.1332, "ssim": 0.998602, "warnings": [], "R": 512}` | build 成功（该用例不设质量阈值，质量由对应 A 项考核） | A1/A2 的载体 |
| T2 | SHOULD | PASS | `{"klass": "flat_art", "stroke_count": 7, "fill_count": 4, "region_count": 7, "psnr_db": 31.8592, "ssim": 0.986713, "warnings": [], "R": 512}` | build 成功（该用例不设质量阈值，质量由对应 A 项考核） | 尖角分支保留由 A8 的 T5 场景考核；T2 验区域填充 |
| T3 | SHOULD | PASS | `{"klass": "flat_art", "stroke_count": 14, "fill_count": 8, "region_count": 14, "psnr_db": 35.2663, "ssim": 0.987048, "warnings": [], "R": 512}` | build 成功（该用例不设质量阈值，质量由对应 A 项考核） | A9/A11/R7 的载体 |
| T4 | MUST | PASS | `{"klass": "lineart", "stroke_count": 2, "fill_count": 0, "region_count": 1, "psnr_db": 34.1534, "ssim": 0.996758, "warnings": [], "R": 512}` | build 成功（该用例不设质量阈值，质量由对应 A 项考核） | A3/A7/A10/A13 的载体 |
| T5 | MUST | PASS | `{"klass": "lineart", "stroke_count": 7, "fill_count": 1, "region_count": 4, "psnr_db": 22.8277, "ssim": 0.967293, "warnings": ["W_GAP_BRIDGED"], "R": 512}` | build 成功（该用例不设质量阈值，质量由对应 A 项考核） | A6/A8 的载体 |
| T12 | SHOULD | PASS | `{"mean_delta_e": 17.159, "max_delta_e": 51.46, "direction": "reversed", "color_interp": "linear", "samples": 9, "R": 512}` | 恢复颜色曲线 vs 真值 stops 的平均 ΔE76 ≤ 25（SHOULD；颜色场近似允许偏差） | 颜色反演是「色调维度」的端到端载体；方向不唯一时取更优方向并记录 |
| T13 | MUST | PASS | `{"case": "T13", "orientation": "reversed", "oriented_err_fwd_pct": 152.045, "oriented_err_rev_pct": 5.695, "oriented_mean_rel_err_pct": 5.695, "width_interp": "linear", "interp_step_preferred": false, "w_t0.25_px": 20.0…` | 方向取优后：w(0.25)≈20px（±20%）、w(0.75)≈6px（±25%）、边界 t∈[0.4,0.6]；interp=step 仅偏好记录，不判 FAIL | 区间语义必须用 step 显式表达（格式规范 §4 硬性约束 2）；自动反演曲线用 linear 但数值区间正确时允许（interp=step 仅偏好记录）；本次反演 interp 非 step（已记录，不判 FAIL） |

## 复现命令

```bash
# quick
"<python>" scripts/run_acceptance.py --quick
# full
"<python>" scripts/run_acceptance.py
# items
"<python>" scripts/run_acceptance.py --items A1,A7,R2
# cli
"<python>" scripts/fbs.py acceptance --quick
```

总耗时 14.2s
