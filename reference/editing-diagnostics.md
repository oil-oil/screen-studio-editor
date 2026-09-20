# 剪辑诊断参考

只在默认工作流出现漏检、误保留、缓存失效或坐标错误时读取。默认质量链路只使用本地证据和 Agent 计划。

## 关键产物

质量模式在工程旁保存：

- `baseline-report.json`：本地 ASR、静音、VAD 和活动分析；
- `baseline-report.transcript.edit.json`：源时间编辑转录稿；
- `review-proxy/combined-timeline.mp4`：源时间对齐的音画代理；
- `smart-edit-context.json`：当前 Agent 判断所需的全部证据和指纹；
- `smart-edit-plan.json`：当前 Agent 的 keep/review/cut 决策；
- `smart-edit-cuts.json`：经计划校验后的 source-time cuts；
- `smart-edit-final-report.json`：最终 dry-run 审计。

先从这些报告解释问题，再调整阈值或代码。计划、cuts 和报告必须绑定同一个 `project_sha256`；context 改变后旧计划应拒绝执行。

## `process.py` 不变量

- 首次实际写入前备份 `project.json` 为 `project.json.bak`；
- 外部修改或重新保存过的工程受到保护；
- dry-run 和写入都基于当前时间线，不自动从备份恢复已删除的片段；
- `--discard-external-edits` 会从备份重建，使用前必须告知用户；
- 编辑转录稿保留 fillers、词级时间、标点和原始句界；
- 音频确认无声时，ASR 词时间戳只用于标注和复核，不会因为时间重叠自动 veto；没有直接音频证据、仅由 ASR 词间距推断的候选仍保护已识别词；
- 默认连续无声超过 300ms 才进入自动剪辑候选，剪后保留约 180ms 气口；250ms 只是底层探测窗口；
- 点击、键盘输入和画面变化会记录在无声候选报告中，但不会拦截音频规则；它们仍会保护 Agent 选中的语义删减；
- 多 session 的 ASR、静音和画面时间会重新锚定到统一源时间轴；
- Agent 选中的语义 cuts 仍会经过坐标、屏幕活动和最终 dry-run 校验。

自动静音阈值按 session 估计。只有确认自动阈值误判时才固定 `--silence-db`：语音被裁时向 `-35` 降低，停顿残留时向 `-20` 提高。

## Agent 计划与自定义 cuts

Agent 计划使用 `smart-edit-plan.json`，至少包含：

```json
{
  "schema_version": 1,
  "project_sha256": "来自 smart-edit-context.json",
  "context_sha256": "来自 smart-edit-context.json",
  "decisions": [
    {
      "decision": "cut",
      "confidence": "high",
      "start_ms": 123000,
      "end_ms": 131500,
      "category": "abandoned_take",
      "removed_text": "被放弃的口播",
      "kept_text": "后面的完整重说",
      "reason": "后一遍明确覆盖前一遍",
      "replacement_evidence": "U0020-U0024",
      "screen_action": "redundant"
    }
  ]
}
```

也可以用 `remove_start_id`、`remove_end_id` 引用 context 中的 `U0001` 等发言编号；脚本会把它们展开成 source 时间。只有 `decision=cut` 且置信度为 `high` 或 `medium` 的条目进入 cuts。`keep`、`review`、低置信度、非法时间和指纹不匹配都会被拒绝并记录。

最终的 `smart-edit-cuts.json` 使用 schema v2：

```json
{
  "schema_version": 2,
  "coordinate_space": "source",
  "project_sha256": "当前 project.json 的 SHA-256",
  "cuts": [{
    "start_ms": 123000,
    "end_ms": 131500,
    "removed_text": "被删除的误讲",
    "reason": "abandoned_take",
    "confidence": "high"
  }]
}
```

从 `transcript.edit.json` 复制的时间属于 `source`。从导出视频取得的时间属于 `edited`，必须带当前工程指纹并通过切片映射，不能直接写成 source 时间。

## 判断问题的顺序

当前 Agent 复核每条候选时，按这个顺序判断：

1. 前后内容是不是同一件事，而不是恰好用了相似词；
2. 后一遍是否明确覆盖前一遍的完整信息；
3. 被删区间是否包含独有提醒、条件、数字、警告、结果或操作；
4. 画面是否有点击、输入、状态变化、结果展示或被邀请阅读的内容；
5. 删除后把前后两句连起来，主语、转折和句意是否完整。

停顿不能单独证明重复；“嗯/啊/呃”只有在孤立且接缝自然时才删；文字相似、固定词表、正则或字数都不能替代语义判断。无法明确证明替代关系时保留或标记 `review`。

需要画面证据时，查看对齐代理或仅抽取候选附近帧：

```bash
mkdir -p /tmp/repeat_frames
ffmpeg -i "/path/to/video.mp4" -ss 42 -t 12 -vf "fps=1" \
  /tmp/repeat_frames/frame_%04d.jpg -y
```

## 缓存诊断

正常重跑会复用本地分析。需要重做时使用质量入口的 `--force-analysis`，同步重做转录、音画代理、context 和最终分析。context 改变后必须重新写计划。本次转录失败时停止语义剪辑，不使用遗留转录文件。

验证时至少检查：Agent 选中的重复是否真的有替代、重复句之间的独有提醒是否保留、屏幕活动是否被保护、dry-run 和 apply 是否使用同一批 cuts。真实录屏的观感必须在 Screen Studio 中预览。
