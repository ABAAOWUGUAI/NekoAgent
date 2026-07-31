# 部署指南

[English](../DEPLOYMENT.md)

本指南用于部署全新的 NekoAgent 实例；不会迁移既有私人安装、复制他人的 Assistant、复用模型 Key，也不会开启 QQ 渠道。先以仅回环地址运行，再逐项开启可选集成。

## 1. 选择部署形态

| 形态 | 适合场景 | 默认保持关闭 |
|---|---|---|
| 本地 Bridge | 体验控制台和模型配置 | 渠道、群参与、Codex 工作执行 |
| Bridge + AstrBot | 部署者自己的聊天渠道 | 所有群访问，直至明确配置渠道与群策略 |
| Bridge + Codex | 已批准的本地办事能力 | 网络访问、Shell 访问、自动审批 |

AstrBot 和 Codex 都是可选集成，且不会改变 Assistant、对话、记忆、Goal、Run、Delivery 或 Learning 状态的归属。

## 2. 准备主机

- 使用 Python 3.10 或更高版本；非开发环境应为服务创建专用操作系统账号。
- 仓库、运行态和 Secret 要由部署者备份；可变运行态不要放在会被复制或 Fork 的源码目录中。
- 首次启动将 Bridge 绑定到 `127.0.0.1`；不要直接把 Admin API、SQLite 或 Artifact 目录暴露到互联网。
- 运行态和 Secret 目录只能让服务账号访问，Secret 文件也只能由该账号读取。

Windows 本地启动：

```powershell
git clone https://github.com/ABAAOWUGUAI/NekoAgent.git
Set-Location NekoAgent
py -3.10 -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements.lock
New-Item -ItemType Directory -Force .\var\secrets | Out-Null
[guid]::NewGuid().ToString('N') | Set-Content -NoNewline -Encoding ascii .\var\secrets\admin-token
[guid]::NewGuid().ToString('N') | Set-Content -NoNewline -Encoding ascii .\var\secrets\channel-token
Copy-Item .\deploy\bridge.env.example .\deploy\bridge.env.local
.\deploy\start-local.ps1
```

生成的值只应留在本地文件中，绝不能粘贴到 Issue、聊天、Shell 历史、仓库、截图或提示词里。

## 3. 配置一个本地运行态

以 `deploy/bridge.env.example` 为配置参考，复制为受 Git 忽略的 `deploy/bridge.env.local`，并按主机实际路径调整。启动脚本会在启动前验证 `TOKEN_PATH` 与 `CHANNEL_TOKEN_PATH` 指向两个已存在的本地文件。

在确有理由改变前，请保留这些安全默认值：

| 配置 | 安全起点 | 原因 |
|---|---|---|
| `LISTEN_HOST` | `127.0.0.1` | 避免直接公开 Admin |
| `ALLOW_PUBLIC_TOKEN_AUTH` | `0` | 避免在公开 Listener 上使用 Token 认证 |
| `OPS_BROKER_REQUIRED` | 本地首启为 `0` | 首次启动不引入可选 Ops Broker |
| `ADMIN_COOKIE_SECURE` | HTTPS 反代后为 `1` | 避免不安全的会话 Cookie |
| 数据库与 Artifact 路径 | 私有 `var/` 目录 | 使可变状态脱离源码控制 |

后续若使用反向代理，应在代理处终止 TLS、把上游限制在回环或私网、启用访问日志与限流，并始终保持 Bridge Listener 私有。反向代理不是移除应用认证或审批 Gate 的理由。

## 4. 添加集成前先验证

1. 在单独终端运行 `python tools/run_public_tests.py`。
2. 通过回环 Listener 检查本地 health 与 admin-version；进程健康不代表渠道可以交付。
3. 在控制台添加一个模型连接，读取候选模型 ID 后进行独立验证。目录项不等于部署者账号能输出可用正文。
4. 创建一个 Assistant Instance，或明确安装可选 Starter Pack。Pack 不得导入他人的对话、记忆、渠道、模型或凭据。

不要把 Provider、渠道和 executor 放在一次不可观察的配置跳跃中。分别验证每个边界，故障才有可追踪原因。

## 5. 可选 AstrBot Adapter

使用部署者自己的账号与插件生命周期安装 AstrBot。将 `remote-plugin/` 放入 AstrBot 的插件目录，再仅在部署环境中设置：

```text
ASSISTANT_PLATFORM_BRIDGE_URL=http://bridge-host:18777
ASSISTANT_PLATFORM_CHANNEL_TOKEN_PATH=/run/secrets/assistant-platform-channel-token
```

Bridge URL 只应从目标 AstrBot 网络访问。Adapter 只转换事件和 Delivery ACK，不授予渠道、群、审批或模型访问权。必须验证一条已授权入站消息直到 `Delivery confirmed ACK`，才能称渠道可用。

## 6. 可选 Codex executor

在部署者主机安装 Codex，并在本机完成登录；让服务账号的 `PATH` 能找到 `codex`，再在控制台创建显式 `codex_login` executor binding。不得把 Codex profile、Cookie、Token 或主目录复制到 NekoAgent。

Web Search 等联网工作必须经过显式 Network Policy、审批和审计。自定义模型 Provider 或任意 Shell 命令不会仅因存在 Codex 就获得网络访问。

## 7. 允许真实用户或渠道前

- 确认回环/私网绑定、TLS 终止与防火墙规则。
- 确认 Token、Provider Secret 和运行目录不会被其他主机用户或源码工具读取。
- 为 SQLite 和 Artifact 配置已测试的备份与恢复路径。
- 在准确的群、成员、策略和频率限制明确授权前，保持群参与关闭。
- 执行一条真实但低风险的端到端请求，并保留 Inbound、策略决策、Delivery ACK 与 Outcome 的证据。仅 `sent` 不是确认交付，也不是用户 Goal 达成。

例行升级、故障响应和备份/恢复规则见[中文运行与恢复指南](OPERATIONS.md)。
