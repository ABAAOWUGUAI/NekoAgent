# 集成说明

[English](../INTEGRATIONS.md)

## OpenAI-compatible 模型连接

在控制台中以自己的 Endpoint 和凭据创建 Connection Instance，读取候选模型后使用独立验证台测试。只有验证返回可用正文后，才把模型绑定到聊天、规划或视觉场景。模型发现只读取 Provider 目录，不能证明账号权限、额度或 Chat Completions 兼容性。

## AstrBot 渠道 Adapter

单独安装 AstrBot，再将 `remote-plugin/` 放入 AstrBot 已配置的插件目录。仅在 AstrBot 的部署环境中设置本机值，绝不要写入仓库：

```text
ASSISTANT_PLATFORM_BRIDGE_URL=http://bridge-host:18777
ASSISTANT_PLATFORM_CHANNEL_TOKEN_PATH=/run/secrets/assistant-platform-channel-token
```

Bridge 只能从预期的 AstrBot 网络访问。通过平台控制台创建匹配的 QQ Channel 和已授权群；插件本身不授予访问权。只有看到真实 Delivery confirmed ACK 后，才能称渠道集成成功。

## Codex executor

在部署者主机上安装 Codex CLI，使 `codex` 命令出现在服务账号的 `PATH` 中，再在本机完成部署者自己的登录，并在平台中配置 `codex_login` executor binding。平台不得导入、复制或显示 Codex Cookie、Token、Profile 文件或主目录。

Codex 是可选的。只需要聊天的用户可不安装 Codex，而是绑定经过验证的模型 Provider。需要 Web Search 的用户必须通过显式 `codex_login` executor 与限时 Network Policy；自定义 Provider 或原始 Shell 不会因此隐式获得联网权限。
