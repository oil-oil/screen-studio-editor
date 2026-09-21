---
name: screen-studio-editor
description: >
  剪辑和整理 Screen Studio 的 .screenstudio 工程：删除停顿、误讲、重复录制和空片段，
  合并工程，或把口播工程的屏幕轨替换为按讲述对齐的 PPT。用户提供 .screenstudio 路径、
  要求清理录屏时间线、对照手工剪辑、合并补录或替换屏幕内容时使用。
  不负责给导出的 MP4 烧录字幕；视频字幕使用 oil-subtitle。普通 MP4/MOV 粗剪使用 video-editor，
  不要因为用户只提供导出视频或只要求加字幕而触发本 Skill。
---

# Screen Studio Editor

本 Skill 只负责 Screen Studio 工程时间线和工程内屏幕素材。导出成片后的字幕交给
`oil-subtitle`。

质量剪辑分成两层：脚本先测量声音、转录时间、停顿和屏幕活动，当前 Agent 再根据完整上下文
判断哪些有声音的内容真的应该删掉，脚本最后负责坐标校验、长无声清理、语义剪辑的屏幕活动保护和
`project.json` 写入。确认没有人声的长空白由程序直接剪，不交给 Agent 做主观判断。
质量判断不调用远程语义模型；当前 Agent 就是语义判断者。自动无声剪辑只依据音频证据，屏幕活动仅写入报告供复核；只有 Agent 选中的语义删减仍受屏幕活动保护。

## 转录配置

质量剪辑可能需要转录服务，但转录和语义判断是两件事：

- `bailian`：使用百炼 FunAudio 做 ASR，只有音频会离开本机；
- `local`：使用本机 Whisper/MLX Whisper，适合希望全程离线的情况；
- 无论选哪种 ASR，重复、口误和内容取舍都由当前 Agent 判断，不需要额外的分析 Key。

第一次配置或更换 ASR Key 时，阅读[API Key 配置与业务读取](references/api-key-setup.md)。已有安全配置直接复用，缺少时由用户亲自填写固定页面，不在聊天或命令参数中传 Key。

## 初始化

```bash
SKILL_DIR="<screen-studio-editor 的绝对目录>"
PYTHON="$SKILL_DIR/.venv/bin/python3"
CONFIG="${SCREEN_STUDIO_EDITOR_CONFIG:-$HOME/.config/screen-studio-editor/config.json}"
```

首次使用：

```bash
bash "$SKILL_DIR/setup.sh"
```

可选配置：

```json
{
  "projects_root": "/optional/path/to/screen-studio-projects",
  "creator_preferences": "/optional/path/to/creator-edit-preferences.json",
  "hotwords": "/optional/path/to/hotwords.json",
  "vocabulary_cache": "/optional/path/to/vocabulary-cache.json",
  "asr_backend": "bailian",
  "smart_edit": {
    "pause_threshold_ms": 300,
    "min_pause_ms": 180,
    "asr_backend": "bailian"
  },
  "visual_defaults": {
    "enabled": false,
    "output_aspect": [4, 3],
    "background_padding_ratio": 1.02,
    "window_border_radius": 25,
    "camera_aspect_ratio": "square",
    "camera_size": 0.3,
    "camera_position": "top-right",
    "camera_position_point": {"x": 1, "y": 0},
    "improve_microphone_audio": true
  },
  "ppt": {
    "style_skill": "",
    "tone_skill": "",
    "illustration_brief": "",
    "cutout_script": ""
  }
}
```

ASR 后端按固定顺序选择：命令行显式参数优先，其次读取 `smart_edit.asr_backend`，再读取顶层
`asr_backend`，最后默认使用百炼 `bailian`。本地 Whisper 只有显式指定 `local` 时才会安装或运行，
不会因为百炼凭据暂时不可读而自动切换到本地模型。

## 模式 A：质量剪辑

### 1. 验证输入

确认工程目录存在，并包含：

- `project.json`
- `recording/`

不要手工编辑 `project.json`，除非正在修复脚本无法处理的明确问题。

### 2. 先准备本地证据

普通口播和屏幕教程只运行这一条入口：

```bash
"$PYTHON" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio"
```

这一步不写时间线。它会完成 ASR、静音/VAD、屏幕活动分析和源时间轴对齐，生成：

- `smart-edit-context.json`：转录、稳定的 `U0001` 等发言编号、停顿和屏幕活动证据；
- `review-proxy/combined-timeline.mp4`：供 Agent 在需要时核对声音和画面。

如果确实希望只用本机 ASR（这是显式选择，不是默认回退）：

```bash
"$PYTHON" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio" \
  --asr-backend local
```

### 3. 由当前 Agent 写语义计划

读取 `smart-edit-context.json`，必要时查看 `review-proxy/combined-timeline.mp4`，再在工程内写入
`smart-edit-plan.json`。计划必须原样带上 context 里的 `project_sha256` 和 `context_sha256`，所有时间都使用 source 时间轴。

推荐格式：

```json
{
  "schema_version": 1,
  "project_sha256": "从 context 复制",
  "context_sha256": "从 context 复制",
  "decisions": [
    {
      "decision": "cut",
      "confidence": "high",
      "start_ms": 1200,
      "end_ms": 2380,
      "category": "abandoned_take",
      "removed_text": "被放弃的那一遍口播",
      "kept_text": "后面留下的完整说法",
      "reason": "前一遍明确重说，后一遍完整覆盖同一信息",
      "replacement_evidence": "U0012-U0015",
      "screen_action": "redundant",
      "visual_assessment": "同一操作在后面的完整重录中再次出现"
    },
    {
      "decision": "keep",
      "confidence": "high",
      "start_ms": 3000,
      "end_ms": 4500,
      "reason": "这里有独有提醒和演示"
    }
  ]
}
```

规则分成两层：确认没有人声的空白，程序统一按 `pause_threshold_ms` 和 `min_pause_ms` 这套音频规则压掉；默认连续无声超过 300ms 才进入剪辑候选，并保留 180ms 气口。底层静音探测窗口是 250ms，只负责找候选，不能绕过 300ms 的最终门限。自动静音阈值设有 `-30.5 dB` 下限，防止少量孤立峰值把几乎无声的平直片段误判为有声；显式传入 `--silence-db` 时仍完全尊重调用者的阈值。音频确认无声时，即使 ASR 时间戳落在区间里，也只记录为复核提示，不自动阻止剪辑；只有纯粹由 ASR 词间距推断的停顿才使用文字保护。有声音但可能是重录、口误或孤立语气词时，才由 Agent 根据前后文判断。屏幕活动只作为报告中的证据，不再把无声区整段拦住。前一遍只有在后一遍明确重录并覆盖其信息时才删；不能因为文字相似就删除独有提醒、数字、警告、结果或操作。

### 4. 生成并审查 dry-run

```bash
"$PYTHON" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio"
```

它会校验计划来源，生成 `smart-edit-cuts.json`，再运行最终 dry-run，写入 `smart-edit-final-report.json`。审查至少包括：

- 每条删除的原文、保留内容、理由和 source 时间；
- 所有超过 5 秒的删除；
- 与点击、键盘输入、画面变化重叠的候选；
- 以“但是/不过/然而”等转折词开头的切口；
- 原始时长、新时长、被安全规则拦下的数量。

脚本只接受 `decision=cut` 且置信度为 `high` 或 `medium` 的计划条目；`keep`、`review`、低置信度和非法坐标都会被列入拒绝记录。自动停顿剪辑只由音频证据决定；屏幕活动会记录在 `pauses_with_activity_overlap` 中，供复核，但不会改变自动无声剪辑结果。

### 5. 应用同一批已审查决策

确认 dry-run 安全后：

```bash
"$PYTHON" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio" \
  --apply
```

`--apply` 只使用已有的 context、plan、cuts 和 final report，不再重新判断，也不访问任何模型服务。如果 Screen Studio 在审查后修改或重新保存工程，先重新准备证据和计划；不要套用旧结果。`--discard-external-edits` 只有在用户明确要求从 `project.json.bak` 重建时才可使用。

### 6. 交付预览

报告删除了多少停顿、重复和空片段，原始时长、新时长、节省时间以及被保留的风险候选。让用户在 Screen Studio 中预览工程；导出 MP4 后，需要字幕时切换到 `oil-subtitle`。

个人偏好样本是可选的。它们只能帮助 Agent 理解创作者已经确认过的取舍，不能替代当前工程的音画证据，也不能把本次待测工程的已剪答案当成训练材料。

## 模式 B：仅清理停顿

只有用户明确不需要语义剪辑时使用。质量剪辑已准备过同一份证据时，可以复用 transcript；单独运行时先 dry-run：

```bash
PROJECT="/path/to/Project.screenstudio"
WORK="$PROJECT/.screen-studio-editor"
mkdir -p "$WORK"

"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "$PROJECT" \
  --pause-threshold 300 \
  --min-pause 180 \
  --pause-source silence \
  --language zh \
  --dry-run \
  --report-output "$WORK/autoedit-report.json"
```

审查报告后再应用。不要关闭 VAD 或画面扫描，除非正在诊断具体错误；如果报告里出现 `pauses_with_activity_overlap`，先确认这些区间确实是无声，再按统一音频规则应用剪辑。

## 模式 C：合并工程

确认 base、supplement 以及追加或插入位置。默认追加：

```bash
"$PYTHON" "$SKILL_DIR/scripts/merge_projects.py" \
  --base "/path/to/Base.screenstudio" \
  --supplement "/path/to/Supplement.screenstudio"
```

插入指定 slice 后追加 `--insert-after-slice 5`。输出已存在时脚本应停止；只有用户明确确认替换后才使用 `--force`。

## 模式 D：口播工程替换为 PPT 屏幕轨

只用于“摄像头 + 麦克风口播，原屏幕是占位内容”的工程。永远在克隆工程上工作；按成片时间理解口播、设计页面和确定翻页点。准备好渲染页和计划后：

```bash
"$PYTHON" "$SKILL_DIR/scripts/auto_ppt_replace.py" \
  --project "/path/to/Clone.screenstudio" \
  --pages "/path/to/rendered_pages" \
  --plan "/path/to/plan.json"
```

完成后让用户完全退出 Screen Studio，再打开克隆工程检查屏幕比例、翻页、淡入、鼠标隐藏和缩放。

## 高级诊断与安全边界

默认流程不要直接调用旧的候选生成或仲裁脚本。语义判断只从 context 进入 Agent 计划，再进入 cuts；不会绕过这条审查链。

- `project.json` 和用户在 Screen Studio 中的调整属于用户数据；
- 自定义 cuts 必须声明坐标空间并绑定项目指纹；
- 全部语义决定先进入可审查的 JSON 计划，脚本不能自己从词表、正则或文本相似度推断“应该删”；
- 屏幕活动保护和最终 dry-run 是强制安全层；
- PPT 替换只操作克隆工程。

诊断说明见[剪辑诊断参考](reference/editing-diagnostics.md)，API 配置说明见[API Key 配置与业务读取](references/api-key-setup.md)。

## 报告格式

保持简短：删除了多少停顿、重复和空片段；原始时长、新时长、节省时间；语义决定来源为 `calling-agent`；有哪些候选被安全规则保留；用户下一步应该预览什么。
