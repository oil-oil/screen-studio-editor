# screen-studio-editor

剪辑录屏工程中的停顿、误讲、重复与空片段，支持合并补录和按讲述替换屏幕内容。确认无声的长空白由程序直接清理；Agent 只判断有声音的内容取舍和少数需要保留的画面例外。

[快速开始](#快速开始) · [质量剪辑](#质量剪辑) · [四种用法](#四种用法) · [数据边界](#数据边界)

## 四种用法

- **质量剪辑：** 脚本测量声音、ASR、停顿和屏幕活动；当前 Agent 结合完整上下文写出语义剪辑计划。
- **只清停顿：** 不需要语义剪辑时，只处理静音和过长停顿。
- **合并工程：** 把补录工程追加到主工程末尾，或插到指定 slice 之后。
- **口播换 PPT：** 在克隆工程上，把占位屏幕轨换成按讲述对齐的页面。

本仓库只改 Screen Studio 工程，不给 MP4 烧录字幕。

## 快速开始

运行环境：macOS、Python 3、Homebrew。`setup.sh` 会准备独立虚拟环境，缺少 FFmpeg 时通过 Homebrew 安装。

```bash
SKILL_DIR="${SKILL_DIR:-$HOME/.local/share/skills/screen-studio-editor}"
git clone https://github.com/oil-oil/screen-studio-editor "$SKILL_DIR"
bash "$SKILL_DIR/setup.sh"
```

也可以把仓库地址交给支持 Skills 安装的宿主：

```bash
npx skills add https://github.com/oil-oil/screen-studio-editor
```

质量剪辑不需要语义模型 Key。ASR 默认使用百炼 FunAudio；只有显式选择 `local` 时才使用本机 Whisper/MLX Whisper。
如果配置了百炼，流程不会因为凭据问题自动切换到本地模型。

把工程路径告诉 Agent：

```text
帮我剪这个工程 /path/to/Tutorial.screenstudio，先出报告，不要直接写入。
```

完整执行规范见 [SKILL.md](SKILL.md)。

## 质量剪辑

第一步只准备证据，不改时间线：

```bash
.venv/bin/python3 scripts/smart_edit_workflow.py \
  --project "/path/to/Tutorial.screenstudio" \
  --asr-backend bailian
```

脚本生成 `smart-edit-context.json` 和对齐代理。当前 Agent 读取它们，判断哪些是被放弃的重录、真正的口误、孤立的语气词或空等待，并写 `smart-edit-plan.json`。Agent 计划必须绑定 context 的 `project_sha256` 与 `context_sha256`，使用 source 时间轴。

第二次运行会校验计划、生成 `smart-edit-cuts.json`，然后输出 `smart-edit-final-report.json`：

```bash
.venv/bin/python3 scripts/smart_edit_workflow.py \
  --project "/path/to/Tutorial.screenstudio"
```

审查报告和实际画面后再应用：

```bash
.venv/bin/python3 scripts/smart_edit_workflow.py \
  --project "/path/to/Tutorial.screenstudio" \
  --apply
```

`--apply` 只应用已经审查的 cuts，不会重新请求模型服务。所有确认无声的区间统一按 `pause_threshold_ms` 和 `min_pause_ms` 清理：默认连续无声超过 300ms 才剪，剪后保留 180ms 气口；250ms 只是底层探测窗口。屏幕活动只记录为复核证据，不改变自动无声剪辑结果。文字相似也不能抹掉独有提醒、数字、警告、结果或操作。

## 配置放在用户目录

个人路径、热词和偏好样本放在仓库外面，例如 `~/.config/screen-studio-editor/config.json`：

```json
{
  "projects_root": "/path/to/screen-studio-projects",
  "creator_preferences": "/path/to/creator-edit-preferences.json",
  "asr_backend": "bailian",
  "smart_edit": {
    "pause_threshold_ms": 300,
    "min_pause_ms": 180
  }
}
```

ASR 后端选择顺序固定为：命令行显式参数 > `smart_edit.asr_backend` > 顶层 `asr_backend` > 百炼默认。
`setup.sh` 也只会在后端明确为 `local`，或设置 `SCREEN_STUDIO_EDITOR_INSTALL_LOCAL_ASR=1` 时安装本地 Whisper。

## 数据边界

- 本地 Agent 读取工程、转录、声音和对齐后的屏幕代理，输出语义计划；
- 选择 `local` ASR 时，转录也在本机完成；选择 `bailian` 时，音频会发送给百炼 FunAudio；
- 语义剪辑不把全片视频或候选证据发送给远程语义模型；
- `project.json`、用户在 Screen Studio 里的修改、个人配置和偏好样本都是用户数据，不要提交进仓库；
- dry-run、报告审查和最终写入都在本机完成。PPT 替换只操作克隆工程。

## 脚本索引

| 脚本 | 作用 |
| --- | --- |
| `scripts/smart_edit_workflow.py` | 准备本地证据、校验 Agent 计划、编排 dry-run/apply |
| `scripts/process.py` | 本地分析、停顿清理、cuts 校验和时间线写入 |
| `scripts/build_review_proxy.py` | 构建源时间对齐的音画代理 |
| `scripts/merge_projects.py` | 合并 Screen Studio 工程 |
| `scripts/auto_ppt_replace.py` | 用按口播对齐的页面替换屏幕轨 |
| `scripts/local_transcribe.py` | 本地 Whisper/MLX Whisper 转录 |
| `scripts/bailian_transcribe.py` | 可选的百炼 ASR |

旧的候选生成和仲裁脚本不属于默认生产链路。

## 测试

```bash
./.venv/bin/python3 -m unittest discover -s tests
```

API 配置页面说明见 [references/api-key-setup.md](references/api-key-setup.md)。
