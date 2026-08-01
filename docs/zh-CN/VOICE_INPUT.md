# Owner QQ 私聊语音消息输入

[English](../VOICE_INPUT.md)

NekoAgent 可以选择性接收 Owner 在 QQ 私聊中发送的语音消息。这是 Channel
输入适配能力，不是 VoicePack、语音合成或 QQ 原生语音电话。转写文本会进入与
文字消息相同的平台主链，最终仍以文字消息回复。

```text
Owner QQ 私聊语音
  -> 已认证 AstrBot Adapter
  -> 受控 HTTPS 媒体获取
  -> 本地 ffmpeg 解码
  -> 本地 whisper.cpp 转写
  -> Continuity Turn 与 Interaction Plan
  -> 普通 Delivery 与 confirmed ACK
```

## 安全边界

语音输入默认关闭并且 fail-closed：

- 只接受已认证 QQ Channel 发来的 Owner 私聊 record 事件；群语音及其他媒体
  类型会被拒绝；
- 每个事件只允许一个语音组件，最大 10 MiB、最长 60 秒；
- 只允许 HTTPS、明确的媒体域名后缀 allowlist、DNS 校验与 peer-IP 固定，
  禁止凭据、fragment、任意端口和私网目标；
- 本地转写并发固定为 1，超时受限，CPU 线程最多 2；
- Voice Receipt 只保存来源/处理证据与哈希，不保存签名媒体 URL 或转写正文；
- 处理结束后删除下载音频和解码临时文件；
- 完整语音输入开启时，transport probe 与 controlled-fetch 诊断开关必须关闭。

正常 Conversation 会保存 Owner 的转写文本，因为它就是交给 Assistant 的用户
消息。Assistant 数据库应与文字私聊使用同样的私密保留、访问与备份策略。

## 本地运行依赖

在 Bridge 主机安装 `ffmpeg`、`whisper.cpp` CLI 和多语言模型。本仓库不包含
模型文件。可执行文件与模型应放在不可变、服务账号可读的位置，并在配置前独立
核验模型 SHA256。

数据库迁移完成后，为当前部署配置 `qq_channel_settings` 的这些字段：

- `voice_transcription_executable`
- `voice_transcription_model_path`
- `voice_transcription_model_sha256`
- `voice_transcription_ffmpeg`
- `voice_transcription_threads`（`1` 或 `2`）
- `voice_transcription_timeout_seconds`（`10` 到 `120`）
- `voice_transcription_max_duration_seconds`（`1` 到 `60`）

任何路径或哈希仍是占位值时都不得启用。公开版不会提供私有运行路径或预装模型。

## 显式启用

先使用 SQLite backup API 备份 Assistant 数据库，并确认两个诊断开关都已关闭，
再针对真实 Assistant 数据库执行预检与启用：

```text
python tools/set_voice_input.py --assistant-db <assistant-db-path> --enable --confirm-owner-private-text-reply-only
```

命令会检查 schema、可执行权限、解码器、模型文件和精确 SHA256，全部通过后才
修改功能开关。关闭时不需要确认参数：

```text
python tools/set_voice_input.py --assistant-db <assistant-db-path> --disable
```

只有部署源码发生变化时才重启 Bridge 与 AstrBot Adapter；不要为了应用这个
平台功能而重启 QQ transport。

## 验收 Gate

发送一条新的、低风险 Owner QQ 私聊语音，并证明唯一的一条完整链：

1. 一个 Voice Receipt 完成获取、转写和分发；
2. 已记录来源删除，临时目录没有音频残留；
3. 一个 Conversation Message 的 `source_type=qq_voice_transcript`，且外部事件
   标识与该语音一致；
4. Continuity Turn 与 Interaction Plan 到达真实终态；
5. 没有选择 Skill 时 Skill Plan 为 `not_applied`，否则有真实 Skill Outcome；
6. 一个 Delivery 绑定该 Turn，并到达 confirmed ACK；
7. 没有重复回复，也没有误生成 Learning candidate。

service health、插件加载、转写成功、Delivery 入队或单独的 `sent` 都不代表 Gate
通过。任一证据缺失就停在该边界修复，不能继续扩大渠道或语音能力。

语音回复是独立、显式配置的能力，见[语音输出指南](VOICE_OUTPUT.md)。群语音参与和
QQ 原生语音电话仍不在范围内。
