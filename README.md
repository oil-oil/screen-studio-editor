# screen-studio-editor

用于剪辑和整理 Screen Studio `.screenstudio` 工程。

## 能力

- 删除停顿、空片段、误讲和重复录制；
- 使用声音、ASR、VAD、屏幕变化、输入事件和 Gemini 全片判断综合审查；
- 学习创作者手工剪辑偏好并在应用前生成 dry-run 报告；
- 合并补录工程；
- 把纯口播工程的屏幕轨替换为按讲述对齐的 PPT。

导出视频的字幕已经拆到独立的 `oil-subtitle` Skill，本项目不再负责字幕预览和烧录。

## 安装

```bash
bash /absolute/path/to/screen-studio-editor/setup.sh
```

依赖 macOS、Python 3、FFmpeg、已登录的百炼 CLI `bl`，以及用于全片判断的模型 API 配置。

## 使用

- `帮我剪这个工程 /path/Tutorial.screenstudio`
- `只清理这个工程里的停顿`
- `把补录工程合并到主工程末尾`
- `把这个口播工程的屏幕换成配套 PPT`

默认质量入口：

```bash
.venv/bin/python3 scripts/smart_edit_workflow.py \
  --project "/path/Tutorial.screenstudio"
```

审查 `smart-edit-final-report.json` 后再增加 `--apply`。不要在质量入口前额外运行一次 `process.py --dry-run`，编排脚本内部已经完成基线分析。

## 主要脚本

| 脚本 | 作用 |
|---|---|
| `scripts/smart_edit_workflow.py` | 默认质量剪辑编排 |
| `scripts/process.py` | 本地分析、停顿清理、cuts 校验和时间线写入 |
| `scripts/global_edit_planner.py` | 全片语义候选 |
| `scripts/preference_edit_arbiter.py` | 创作者偏好学习和候选仲裁 |
| `scripts/build_review_proxy.py` | 构建源时间对齐的音画代理 |
| `scripts/merge_projects.py` | 合并 Screen Studio 工程 |
| `scripts/auto_ppt_replace.py` | 用按口播对齐的页面替换屏幕轨 |

完整流程见 [SKILL.md](SKILL.md)。
