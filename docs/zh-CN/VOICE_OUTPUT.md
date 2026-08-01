# Owner QQ 私聊语音消息输出

[English](../VOICE_OUTPUT.md)

NekoAgent 可以选择性地用本地 VoicePack 渲染一条已经生成的 Assistant 回复，并
通过 QQ 发送为语音消息。这是回复模态，不是第二套 Conversation、新 Assistant
身份或 QQ 原生实时电话。

```text
Interaction Plan 情绪与 Owner 请求
  -> 服务端回复模态策略
  -> 本地 VoicePack 与离线 TTS
  -> 不可变 Audio Artifact
  -> 既有 Outbox lease
  -> QQ record 消息
  -> Delivery confirmed ACK
```

## 回复模态

当前 Assistant 只有一条版本化策略：

- `text_only`：始终只用文字；
- `explicit_only`：仅 Owner 明确要求时使用语音；
- `emotion_auto`：明确请求可用语音，选定情绪达到置信度阈值时可受限自动使用；
- `always`：Owner 私聊普通聊天也优先语音。

自动语音受冷却时间和每日预算约束；明确“不要语音”始终优先，明确语音请求不
消耗自动预算。模型可以提出 affect/意图，但不能授予权限：服务端仍必须校验
Owner 私聊、chat dispatch、功能开关、active VoicePack、TTS 与 Artifact Gate。

## 本地运行与许可

本仓库不包含 TTS 模型、音色资产或私有 VoicePack。部署者必须自行选择本地 TTS
Adapter 和适合使用场景的许可，固定运行资源与精确 SHA256，并确保推理离线。禁止
让首次请求临时下载模型，也不得把回复文字发送给未声明的外部语音服务。

通过既有 Assistant Resource API 或管理台创建并绑定 VoicePack。启用
`voice_output_v1` 前必须验证可执行文件、模型/配置资产、语言、采样率、格式和
哈希；本地渲染与 lease-bound Artifact 读取都通过后，才启用
`voice_delivery_v1`。公开源码不会提供生产路径或预选音色。

管理台可以修改模式、情绪类型、最低置信度、冷却时间和每日上限；保存时携带
expected version，过期写入会被拒绝。

## 安全边界

- 只有认证后的 Owner QQ 私聊 chat 可以选择语音；群聊和其他 Actor 回退文字；
- TTS 只渲染已经批准的回复，不得改写事实、代码、命令、日志、引用、Artifact
  哈希或执行状态；
- Audio Artifact 不可变，Adapter 只能凭当前 Delivery lease 读取；本地路径和
  lease 材料不得发往 QQ；
- 合成或投递失败时诚实回退文字，不得声称语音已发送；
- 不确定发送进入既有 reconciliation，不能在无证据时再次发送第二条语音。

## 验收 Gate

使用 SQLite backup API 备份并精确部署后，发送一条新的 Owner 私聊明确语音请求；
配置 `emotion_auto` 时也可发送自然情绪表达。必须证明唯一完整链：

1. Interaction Plan 只记录有限 affect/触发元数据；
2. 一个不可变 Audio Artifact 具有已验证的媒体属性与哈希；
3. 一个 `assistant_voice_reply` Outbox 绑定该 Artifact 与 Turn；
4. Adapter 在当前 lease 下开始发送；
5. 存在平台消息标识且 Delivery 到达 confirmed ACK；
6. Owner 确认 QQ 语音可播放；
7. 没有重复 Delivery、临时文件泄漏或误生成 Learning candidate。

只有 TTS 文件、服务健康、Adapter loaded、Outbox 入队或 `sent` 都不代表 Gate
通过。任一证据缺失就停在该边界。

语音输入见[语音输入指南](VOICE_INPUT.md)。群语音参与与 QQ 原生实时电话仍是
独立、默认关闭的后续能力。
