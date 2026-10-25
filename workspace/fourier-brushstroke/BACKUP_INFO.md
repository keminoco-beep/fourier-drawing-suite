# Skill 备份信息（BACKUP_INFO）

- 备份日期：2026-10-20 04:06:44
- 备份内容：fourier-brushstroke 技能完整目录（含源码/文档/测试/产物；排除 __pycache__、.pytest_cache、临时目录）
- 质量状态：269+ 单测全绿（275 项）、验收 36 通过 / 2 已知失败 / 1 判据修正 / 1 未实现、hygiene 0；
  训练循环已按用户指令停止（train_finelines 三个目录均 DONE，best 分别为 16.923 / 16.782 / 16.787，训练口径 1024）。
- 关键产物：
  - 纯细线训练最终 best 图：`assets/out/user_chibi/train_finelines/*/chibi_whale_best_R2048.png`
  - 一笔画傅里叶圆过程视频（含静止收尾无圆）：`assets/out/user_chibi/finelines_onestroke_ultra_final2_150s_1080p.mp4`
  - 精度/连接线对比：`assets/out/user_chibi/finelines_onestroke_connector_cmp.png`、`finelines_onestroke_precision_cmp.png`
  - 边缘糊根因诊断：`references/edge_blur_diagnosis.md`、`improvement_eval_plan.md`、`training_rules.md`

恢复方式：解压后直接使用 `scripts/fbs.py`（命令行）/ `scripts/train_loop.py`（训练）/ `references/*.md`（说明）。
