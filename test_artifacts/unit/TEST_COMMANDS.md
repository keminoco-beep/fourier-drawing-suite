# 单元/契约测试命令与结果（v1.33→v1.38）

```powershell
cd workspace\skills\fourier-multistroke-training
python -m unittest discover -s tests        # v1.38: Ran 142 tests, OK (skipped=13) / ~80-95s
$env:RUN_SLOW="1"; python -m unittest discover -s tests   # 慢测试 3/3
$env:RUN_GPU="1"; & "<ComfyUI python>" -m unittest tests.test_gpu_render -v   # 19 tests / 0 failed / 1 skipped
```

历史基线（逐版本）：见本目录 `baseline_*.md`。结论：各版本全绿；v1.38 默认 13 skip = 3 慢 + 10 GPU。
