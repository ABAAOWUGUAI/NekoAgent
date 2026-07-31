# NekoAgent（简体中文）

[English](README.md)

NekoAgent 是一个 Owner 优先、可自托管的私人 AI 助手平台。它不是普通 QQ 机器人、ChatGPT Clone、AstrBot 分叉或 Codex 外壳：Assistant 的身份、关系、对话、记忆、目标、运行、交付和学习状态应独立于任一模型、渠道或执行器。

项目以 Apache-2.0 发布，可商业使用、修改和再分发；请同时遵守 [LICENSE](LICENSE)、[第三方声明](THIRD_PARTY_NOTICES.md) 和 Starter Pack 的单独素材声明。

## 三个可替换层

1. **Platform Core**：Bridge、SQLite 状态、控制台、交付与服务端策略 Gate。
2. **AstrBot Adapter（可选）**：`remote-plugin/` 接收结构化渠道事件并回报 Delivery ACK；AstrBot 仍由部署者独立安装和管理。
3. **Codex Executor（可选）**：部署者本机已登录的 Codex CLI 可执行已批准的工作；普通对话也可只使用经过验证的 OpenAI-compatible 模型连接。

公开源码不包含生产数据库、聊天/媒体正文、Provider Key、QQ 凭据、服务器地址、用户专属 Assistant、关系和私有 PetPack。首次启动后，请自行创建 Assistant，或明确选择安装可选 Starter Pack。

`0.1.1` 收口了“立即触发”的后续动作链：服务端先从引用回执或近期 Automation 上下文绑定对象，再执行已授权动作；未经服务端校验的模型动作会 fail-closed，不能退化成聊天中的虚假执行承诺；操作回执会保留后续引用所需的有限对象标识。

`0.1.0` 增加了对象绑定的 Automation 对话：引用 confirmed Delivery 时解析唯一 Automation，澄清轮继续保留待执行计划，更新与立即运行按依赖顺序执行，Evidence 通过版本化输出契约呈现。目标歧义、配置版本冲突或上游步骤失败都会 fail-closed，不会静默执行错误或过期任务。

## 产品主链

```text
Inbound → Identity/Access → Continuity Turn → Interaction Plan → Skill Plan
→ Capability/Approval/Network Policy → Goal/Run/Task → Evidence/Artifact
→ Delivery/ACK → Outcome → Learning → Assistant Home
```

## 快速开始（本地；不需要渠道或模型）

需要 Python 3.10 或更高版本。以下命令只创建本地运行态；生成的 `var/` 和 Token 文件不得提交到 Git。

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

启动脚本只把 `bridge.env.local` 中的本地路径和值导入当前 PowerShell 进程，并将 Bridge 绑定到 `127.0.0.1`；不会上传文件、启用渠道或创建模型连接。

启动后：

1. 在控制台添加一个模型连接，读取候选模型后对选中的模型做独立验证；模型目录不是“该令牌可用”的证明。
2. 按需接入 AstrBot 或 Codex，见[中文集成说明](docs/zh-CN/INTEGRATIONS.md)。
3. 随时运行公开源码 Gate：

```powershell
python tools/run_public_tests.py
```

## 可选 Starter Pack

Starter Pack 是显式声明的资源，不是隐藏默认值。随仓库提供的 `starter-packs/xiaofei/` 是 Owner 授权公开的可选模板，包含展示名称、Persona、PetPack 和五个表情资源；“小菲”不是平台品牌、模型、QQ 渠道、官方账号或真人，也不携带任何历史与私有运行态。

先无写入校验，再在你明确选择后应用：

```powershell
python tools/install_starter_pack.py --pack-dir starter-packs/xiaofei --dry-run
python tools/install_starter_pack.py --pack-dir starter-packs/xiaofei --base-url http://127.0.0.1:18777 --token-file <本地-token-文件> --apply-to-current
```

第二条命令只会为当前 Assistant 创建 Persona Version 并导入已声明的视觉资源；不会创建或激活第二个 Assistant，也绝不导入关系、对话、记忆、知识、Goal、Run、Delivery、Learning、渠道、模型或凭据。

公开版默认不启用 QQ 渠道、群主动参与、网络下载 Skill、原始 Shell 联网或群/全局策略自动学习。每项能力都必须经既有策略和审批控制显式开启。

## 文档与安全

- [中文架构说明](docs/zh-CN/ARCHITECTURE.md)
- [中文集成说明](docs/zh-CN/INTEGRATIONS.md)
- [中文部署指南](docs/zh-CN/DEPLOYMENT.md)
- [中文运行与恢复指南](docs/zh-CN/OPERATIONS.md)
- [中文仓库保护指南](docs/zh-CN/REPOSITORY_PROTECTION.md)
- [安全政策](SECURITY.zh-CN.md)
- [隐私边界](PRIVACY.md)
- [第三方声明](THIRD_PARTY_NOTICES.md)
- [贡献指南](CONTRIBUTING.md)

提交 Issue、PR 或 Fork 前，切勿包含 Provider Key、QQ 凭据、Codex 登录态、运行 SQLite、私聊正文或真实媒体。互联网可访问的部署必须先完成[中文部署指南](docs/zh-CN/DEPLOYMENT.md)和[中文仓库保护指南](docs/zh-CN/REPOSITORY_PROTECTION.md)的相应步骤。
