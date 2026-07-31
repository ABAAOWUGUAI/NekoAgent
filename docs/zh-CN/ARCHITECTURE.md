# 架构说明

[English](../ARCHITECTURE.md)

平台是由 Python 和 SQLite 组成的模块化应用，不是 AstrBot 分叉，也不是 Codex 的封装器。

```text
Web / AstrBot / 其他 Channel Adapter
  → Bridge 应用与领域服务
  → Capability、Approval 与 Network Policy
  → 模型连接或可选 Codex executor
  → SQLite repository、Artifact storage 与 Delivery Outbox
```

- **AstrBot** 拥有自己的事件 Runtime 与插件生命周期。可选插件只转换结构化事件和 Delivery ACK。
- **Codex** 拥有自己的登录和本机 Runtime。平台只保存路由元数据，并在已批准工作前检查 executor profile。
- **模型 Provider** 是连接实例。模型目录不是权限授予，也不能承诺每个账号都能正常调用每个模型。
- **Assistant Instance** 拥有身份和关系连续性。Persona、PetPack、模型和渠道绑定都是可替换资源。

公开版本没有默认 QQ 账号、固定角色、记忆、群策略、已学习偏好、私有 PetPack 或真实媒体包。
