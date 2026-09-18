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
- dry-run 和写入都基于当前时间线，不自动从备份恢复已删除的片段；
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

默认流程由 AI 阅读全片转录、听声音并看画面提出语义候选，再由 AI 仲裁、执行 Agent 复核。模型对画面的描述可能不准确，争议删点需要查看实际画面。禁止用文本相似度、正则、固定词表或字数筛选替代语义判断，也不能通过这些规则擅自改动模型选定的范围。静音、词时间和输入活动属于测量证据，仍由程序处理。

不要仅因重录超过固定时长、同组停顿多或候选类别名称不同，否决 AI 已指出替代关系的判断。复核时把切点前后连起来读，尤其检查主语、转折和条件：结巴句的开头也可能承载必要信息。

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

正常重跑会复用现有分析。需要重做时使用质量入口的 `--force-analysis`，同步重做转录、音画代理、模型候选和最终分析。本次转录失败时停止语义剪辑，不使用遗留转录文件。

修改剪辑算法后，验证：AI 保留的相同措辞不会被规则删除；AI 确认的不同措辞重复能进入 cuts；重复句之间的独有提醒仍在成片中；已剪工程再次预览和应用的结果一致。使用临时工程和受控模型响应验证调用链，真实录屏的观感另行检查。

## 模型比较

用已剪工程的独立副本恢复完整录制时间线，运行真实模型，再与人工剪辑结果比较。人工切点只用于评分，不提供给模型，也不作为本次偏好样本。

检查重复口播是否删净、独有信息和演示是否保留、接缝是否完整，并记录模型、耗时和调用量。时间区间的精确率、召回率和 F1 只衡量与人工切点的重合，不能直接称为语义准确率。需要多案例对比时再使用 `scripts/model_bakeoff.py`。

受控响应的回归测试只证明流程正常；真实模型测试才能报告剪辑效果。单个视频的结果不能推导整体准确率。

## 实验脚本

`structured_edit_candidates.py`、`gemini_edit_candidates.py` 的规则候选入口，以及 `session_edit_planner.py`、`consensus_edit_candidates.py`、`candidate_recall_experiment.py` 不属于默认生产链路。旧规则仅供 benchmark 对照，不能把它们的候选或本地删除结论直接写入用户工程。
