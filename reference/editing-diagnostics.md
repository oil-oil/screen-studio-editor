# 剪辑诊断参考

只在默认工作流出现漏检、误保留、缓存失效、坐标错误或需要模型对比时读取。

## 关键产物

质量模式在工程旁保存：

- `baseline-report.json`：本地 ASR、静音、VAD 和活动分析；
- `baseline-report.transcript.edit.json`：源时间编辑转录稿；
- `review-proxy/combined-timeline.mp4`：源时间对齐的音画代理；
- `global-video-planner-v11.json`：全片候选；
- `smart-edit-report.json`：偏好仲裁结果；
- `smart-edit-cuts.json`：待应用 cuts；
- `smart-edit-final-report.json`：最终 dry-run 审计。

先从这些报告解释问题，再调整阈值或代码。

## `process.py` 不变量

- 首次实际写入前备份 `project.json` 为 `project.json.bak`；
- 外部修改或重新保存过的工程受到保护；
- 完整重跑若会覆盖外部编辑则拒绝执行；
- `--discard-external-edits` 会从备份重建，使用前必须告知用户；
- 编辑转录稿保留 fillers、词级时间、标点和原始句界；
- 确定性停顿删除不会覆盖 ASR 已识别词；
- 点击、键盘输入和画面变化默认保护静默区间；
- 多 session 的 ASR、静音和画面时间会重新锚定到统一源时间轴。

自动静音阈值按 session 估计。只有确认自动阈值误判时才固定 `--silence-db`：语音被裁时向 `-35` 降低，停顿残留时向 `-20` 提高。

## 自定义 cuts

新 cuts 使用 schema v2：

```json
{
  "schema_version": 2,
  "coordinate_space": "source",
  "project_sha256": null,
  "cuts": [
    {
      "start_ms": 123000,
      "end_ms": 131500,
      "removed_text": "被删除的误讲",
      "reason": "false_start",
      "confidence": "high",
      "kept_text": "后面的正确版本"
    }
  ]
}
```

从 `transcript.edit.json` 复制的时间使用 `source`。从导出视频取得的时间属于 `edited`，必须带当前工程指纹并通过切片映射，不能直接写成源时间。

先 dry-run：

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "/path/to/Project.screenstudio" \
  --skip-transcribe "/path/to/Project.screenstudio/transcript.edit.json" \
  --cuts-file "/path/to/cuts.json" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh \
  --dry-run
```

## 候选判断

高置信可删：

- 未完成的开头和紧接着的重说；
- 明确自我纠正；
- 完全重复的结尾；
- 同一句重复录制，后一次明显更完整；
- 不承载必要画面动作的重复解释。

必须保留：

- 后一段增加条件、结果、故障排查或警告；
- 相似措辞对应不同屏幕状态；
- 重复段包含真实点击、命令、文件修改、生成结果或 UI 切换；
- 模型无法指出明确替代关系的“可能重录”。

需要画面证据时，仅抽取候选附近帧：

```bash
mkdir -p /tmp/repeat_frames
ffmpeg -i "/path/to/video.mp4" -ss 42 -t 12 -vf "fps=1" \
  /tmp/repeat_frames/frame_%04d.jpg -y
```

## 缓存诊断

planner、arbiter、代理和最终审计都按工程、转录稿、候选、偏好、模型和代码指纹缓存。重跑未命中时依次检查：

1. `project.json` 是否被 Screen Studio 重新保存；
2. 转录稿或偏好文件是否变化；
3. 模型、停顿阈值或代码版本是否变化；
4. 报告里的 signature 与当前输入是否一致。

不要为了命中缓存绕过指纹校验。

## 模型比较

升级模型前使用 `scripts/model_bakeoff.py` 和隐藏答案的标注 manifest，比较：

- 自动决策是否准确；
- 是否增加危险误剪；
- 覆盖率、精确率、F1；
- 平均耗时和调用成本。

不要根据单个视频的观感更换默认模型。

## 实验脚本

`session_edit_planner.py`、`consensus_edit_candidates.py` 和 `candidate_recall_experiment.py` 不属于默认生产链路。只有在明确做 benchmark 或回归分析时使用，不能把实验结果直接写入用户工程。
