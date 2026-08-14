---
name: screen-studio-editor
description: >
  剪辑和整理 Screen Studio 的 .screenstudio 工程：删除停顿、误讲、重复录制和空片段，
  合并工程，或把口播工程的屏幕轨替换为按讲述对齐的 PPT。用户提供 .screenstudio 路径、
  要求清理录屏时间线、对照手工剪辑、合并补录或替换屏幕内容时使用。
  不负责给导出的 MP4 烧录字幕；视频字幕使用 oil-subtitle。
---

# Screen Studio Editor

本 Skill 只负责 Screen Studio 工程时间线和工程内屏幕素材。导出成片后的字幕交给 `oil-subtitle`。

脚本负责时间坐标、session 对齐、缓存、活动保护、波形边界和 `project.json` 写入。Agent 负责选择入口、审查候选、确认高风险删除并组织用户预览，不要手工重做脚本内部算法。

## 初始化

```bash
SKILL_DIR="<screen-studio-editor 的绝对目录>"
PYTHON="$SKILL_DIR/.venv/bin/python3"
CONFIG="${SCREEN_STUDIO_EDITOR_CONFIG:-$HOME/.config/screen-studio-editor/config.json}"
```

首次使用时运行：

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
  "model": "google/gemini-3.7-flash",
  "smart_edit": {
    "pause_threshold_ms": 700,
    "min_pause_ms": 180
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

命令行参数优先于环境变量，环境变量优先于用户配置。不要提交用户配置、API Key、个人路径、偏好样本或 benchmark 数据。

工程、合并结果、PPT 克隆和工程侧分析产物应放在 `projects_root`；未配置时放在源工程旁边。

## 模式 A：质量剪辑

### 1. 验证输入

确认工程路径存在，并包含：

- `project.json`
- `recording/`

不要手工编辑 `project.json`，除非正在修复脚本无法处理的明确问题。

### 2. 运行默认质量工作流

普通口播和屏幕教程只运行这一条入口，不要提前再跑一次 `process.py --dry-run`：

```bash
"$PYTHON" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio"
```

该命令默认不写时间线。它内部完成基线 ASR、静音/VAD、屏幕活动分析、对齐代理、Gemini 全片候选、创作者偏好仲裁、本地微剪和最终 dry-run，并复用仍然有效的缓存。

质量模式需要 `creator_preferences`。如果尚未配置，先从独立 benchmark 工程构建：

```bash
"$PYTHON" "$SKILL_DIR/scripts/preference_edit_arbiter.py" build \
  --root "/path/to/benchmark-root" \
  --output "/path/to/creator-edit-preferences.json"
```

如果没有个人偏好样本，不要套用其他人的文件；改用下面的“仅清理停顿”。

### 3. 审查结果

读取工程根目录下的 `smart-edit-final-report.json`，至少检查：

- 每一条 smart cut 的删除文本、保留文本和理由；
- 所有超过 5 秒的删除；
- 屏幕有点击、键盘输入或持续变化的候选；
- 原始时长、新时长和节省时间是否合理；
- 是否出现模型拒绝、安全拦截或坐标指纹错误。

相似措辞不等于重复。后一句增加上下文、结果、警告、操作或画面变化时必须保留。

### 4. 应用同一批已审查决策

确认安全后：

```bash
"$PYTHON" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio" \
  --apply
```

`--apply` 应复用刚才的缓存和候选。如果工程在审查后被 Screen Studio 修改或重新保存，先重新 dry-run，不能强行套用旧结果。

### 5. 交付预览

报告停顿、重复、空片段、原始时长、新时长和节省时间。让用户在 Screen Studio 中预览工程；用户确认前不要继续处理导出视频。

用户导出 MP4 后，需要字幕时切换到 `oil-subtitle`。

## 模式 B：仅清理停顿

只有用户明确不需要语义剪辑，或没有 `creator_preferences` 时才使用。

先 dry-run：

```bash
PROJECT="/path/to/Project.screenstudio"
WORK="$PROJECT/.screen-studio-editor"
mkdir -p "$WORK"

"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "$PROJECT" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh \
  --dry-run \
  --report-output "$WORK/autoedit-report.json"
```

审查报告后复用转录稿并应用：

```bash
"$PYTHON" "$SKILL_DIR/scripts/process.py" \
  --project "$PROJECT" \
  --skip-transcribe "$WORK/autoedit-report.transcript.edit.json" \
  --pause-threshold 700 \
  --min-pause 180 \
  --pause-source silence \
  --asr-backend bailian \
  --language zh
```

不要关闭 VAD、画面扫描或屏幕活动保护，除非正在诊断具体错误。

## 模式 C：合并工程

确认 base、supplement 以及追加或插入位置。默认追加：

```bash
"$PYTHON" "$SKILL_DIR/scripts/merge_projects.py" \
  --base "/path/to/Base.screenstudio" \
  --supplement "/path/to/Supplement.screenstudio"
```

插入指定 slice 后：

```bash
"$PYTHON" "$SKILL_DIR/scripts/merge_projects.py" \
  --base "/path/to/Base.screenstudio" \
  --supplement "/path/to/Supplement.screenstudio" \
  --insert-after-slice 5
```

配置了 `projects_root` 时显式传入该目录下的 `--output`。输出已存在时脚本应停止；只有用户明确确认替换后才使用 `--force`。

## 模式 D：口播工程替换为 PPT 屏幕轨

只用于“摄像头 + 麦克风口播，原屏幕是占位内容”的工程。先阅读 [工程格式说明](reference/screenstudio-project-format.md)。

原则：

- 永远在克隆工程上工作，原工程只读；
- 按成片时间理解口播、设计页面和确定翻页点；
- 页面需要真实产品或网站证据时再使用可用浏览工具，不能虚构界面；
- 竖屏默认按 3:4 设计，大字少字，一页一个重点；
- `plan.json` 使用成片时间的 `page_starts` 和可选 `zooms`。

准备好渲染页和计划后：

```bash
"$PYTHON" "$SKILL_DIR/scripts/auto_ppt_replace.py" \
  --project "/path/to/Clone.screenstudio" \
  --pages "/path/to/rendered_pages" \
  --plan "/path/to/plan.json"
```

完成后让用户完全退出 Screen Studio，再打开克隆工程检查屏幕比例、翻页、淡入、鼠标隐藏和缩放。

## 高级诊断

仅在漏检、错误保护、自定义 cuts、缓存失效或模型对比时阅读 [剪辑诊断参考](reference/editing-diagnostics.md)。默认流程不要直接调用底层 planner、arbiter 或旧实验脚本。

## 安全边界

- `project.json` 的手工修改和用户在 Screen Studio 中的调整都属于用户数据。
- 不得在未告知用户的情况下使用 `--discard-external-edits`。
- 自定义 cuts 必须声明坐标空间；导出视频时间不能冒充源工程时间。
- 全片模型只能提出候选，最终删除必须经过本地坐标、活动、边界和 dry-run 校验。
- PPT 替换只操作克隆工程。

## 报告格式

保持简短：

- 删除了多少停顿、重复和空片段；
- 原始时长、新时长、节省时间；
- 是否应用了视觉默认值；
- 有哪些候选被安全规则保留；
- 用户下一步应该预览什么。
