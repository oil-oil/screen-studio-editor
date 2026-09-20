# API Key 配置与业务读取

当前质量剪辑的语义判断由调用本 Skill 的 Agent 完成，不需要分析模型 Key。只有选择云端 ASR 时才配置百炼 Key；选择本地 ASR、工程合并或 PPT 替换时不需要新增 Key。

## 首次配置

将当前 `SKILL.md` 所在绝对目录记为 `SKILL_DIR`。页面需要 Node.js 22.18+，首次在组件目录安装锁定依赖：

```bash
npm --prefix "$SKILL_DIR/scripts/credential-ui" ci --ignore-scripts
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" status transcription
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" setup transcription
```

先查 `status`：退出码 0 表示当前 ASR 凭据可读取，2 表示缺失，1 表示配置或系统后端失败。缺失或用户要求更换时才启动 setup，把返回的本机链接展示给用户，由用户亲自填写保存。不要自动操作真实 Key 页面，不让用户贴进聊天。

页面不回填原值；已有项留空保留，替换需要用户确认。只把 `saved` 当作全部保存成功；`partial`、超时和中断后先重新查状态，再补未完成项。配置成功仅证明保存和可读取，实际 ASR 可用性以业务调用为准。

## 服务与用途绑定

| 配置名 | 业务环境变量 | 用途 |
| --- | --- | --- |
| transcription | `DASHSCOPE_API_KEY` | 百炼 FunAudio ASR |

`default` 是只含转录凭据的兼容别名；质量剪辑不需要第二个分析凭据。

## 运行业务

使用本地 ASR 时直接运行：

```bash
"$SKILL_DIR/.venv/bin/python3" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio" \
  --asr-backend local
```

使用页面保存的百炼凭据时，通过 `transcription` 包装器运行：

```bash
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" run transcription -- \
  "$SKILL_DIR/.venv/bin/python3" "$SKILL_DIR/scripts/smart_edit_workflow.py" \
  --project "/path/to/Project.screenstudio" \
  --asr-backend bailian
```

`--` 后保留原业务参数。包装器只把当前 ASR 所需的 Key 注入可信子进程；参数、普通文件和状态输出都不含 Key。质量剪辑的第二阶段会读取本地产物，由当前 Agent 写计划，不需要再次进入凭据页面。

系统后端分别为 macOS 钥匙串、Windows 凭据管理器、Linux Secret Service。Linux 需要 secret-tool、用户 D-Bus 和已解锁的桌面凭据服务；缺少后端时停止，不自动安装、解锁或降级明文。CI、容器与远程服务器使用已有 Secret 注入，不把本机页面开放到网络。

## 验证

```bash
npm --prefix "$SKILL_DIR/scripts/credential-ui" run check
npm --prefix "$SKILL_DIR/scripts/credential-ui" run build
npm --prefix "$SKILL_DIR/scripts/credential-ui" test
```

这些检查验证凭据页面和变量读取，不验证 ASR 服务额度或语义剪辑准确率。完整质量流程的准确率只能通过当前 Agent 的计划、`smart-edit-final-report.json` 和人工预览核对。

## 数据范围

- 本地 ASR：音频和转录不离开本机；
- 百炼 ASR：只发送工程音频，不发送语义剪辑计划；
- Agent 判断：读取工程内的 context 和可选 review proxy，计划和 cuts 写回本机工程目录；
- 旧的分析凭据不会自动迁移、删除或在生产质量流程中使用。
