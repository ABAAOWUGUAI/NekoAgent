# 运行与恢复指南

[English](../OPERATIONS.md)

NekoAgent 是有状态的平台。Assistant 身份、关系、对话、记忆、Goal、Run、Delivery 与 Learning 是部署者拥有的数据，不是可以跟随模型或渠道随意替换的缓存。

## 每日运行检查

通过私有 Listener 检查本地 health 与 admin-version，再查看脱敏服务日志和控制台诊断。服务 active、插件 loaded 或任务显示 done，都不是端到端 Outcome 的证据。

对一条真实渠道请求，保留最小关联证据：

```text
Inbound -> Identity/Access -> Turn -> policy decision -> Goal/Run/Task
-> Delivery -> confirmed ACK -> Outcome -> Learning（如已准入）
```

任何一环缺失时，应记录实际停止点，不能从看似更靠后的状态推断成功。

## 安全启动与停止

本地 Windows 可运行：

```powershell
.\deploy\start-local.ps1
```

进程必须保持绑定到配置的私有地址。使用拥有该进程的 supervisor 停止它；交互式本地运行可使用 `Ctrl+C`。Bridge 变更不能成为重启无关渠道 Runtime 的理由。

托管部署请使用主机惯用的服务 supervisor，并配置专用服务账号、重启策略与日志轮转。supervisor 只是运行包装层：不得把模型凭据放入命令行，或公开运行目录。

## 事件排查

| 现象 | 首先检查 | 不要做 |
|---|---|---|
| Bridge 无法启动 | 本地配置路径、两个 Token 文件是否存在、脱敏启动错误 | 替换运行数据库或新建 Assistant |
| 控制台无法连接 | 私有 Listener、反代路由、已认证会话 | 为测试而把 Admin API 暴露公网 |
| 模型验证失败 | 所选连接、模型 ID、Provider 响应类别、账号侧权限 | 没有非空验证正文就把目录模型标成可用 |
| 渠道显示已发送 | Delivery attempt、confirmed ACK、Outcome 关联 | 仅凭 `sent` 或插件日志称请求完成 |
| 群行为不对 | 精确群策略、授权、冷却与决策证据 | 调高全局参与性或绕过群策略强制回复 |
| 图片/媒体失败 | 已配置的媒体 Gate、接受的类型、Provider 能力 | 将原始媒体 URL/文件转发给任意 Provider |

Issue 只使用合成或脱敏复现数据；绝不附上私聊、媒体、SQLite、Key、Cookie 或渠道凭据。

## 备份与恢复

将 Assistant 数据库、Task 数据库和 Artifact 存储作为一个逻辑整体备份。使用应用的 SQLite backup 路径或等价的一致性快照；不要只复制部分 WAL 文件来拼接在线 SQLite。备份应放在 checkout 外，以运行态同等权限保护，并定期在隔离主机中演练恢复。

恢复演练只有同时满足以下条件才算成功：

1. 能用恢复的数据库与 Artifact 启动；
2. 保留 Assistant 身份和预期状态边界；
3. 能处理一个低风险验证请求，且不会重放旧 Delivery。

不要为了重试失败的 Delivery 而恢复备份。先判断原结果是 sent、acknowledged、uncertain 还是 failed，再通过 Delivery Policy 重试。

## 升级步骤

1. 阅读目标版本的 changelog 与部署说明。
2. 制作逐文件 diff 或 hash 清单；绝不从工作 checkout 覆盖整个生产目录。
3. Schema 或运行态变更前，对 SQLite/Artifact 做一致性备份。
4. 在隔离副本执行 `python tools/run_public_tests.py`。
5. 只部署受影响文件，只重启受影响服务，然后回读私有 health、配置版本和脱敏错误。
6. 按风险进行真实验收：渠道要有新的 Delivery confirmed ACK；Learning 要有精确作用域的 candidate、审批/应用与 feedback/undo 证据。
7. 记录版本、变更文件、验证结果与回滚路径。

## 日志、指标与隐私

日志应只保留排障所需的标识符、决策类别和脱敏错误类别，不能变成私聊正文、媒体 URL、Token 或 Provider 凭据的第二存储。控制台、日志、备份和 Artifact 只允许有正当运维理由的人员访问。过期本地文件应依据运行态保留策略处理，不能对 checkout 或主目录做宽泛删除。

## 升级与故障的停止规则

停在失败 Gate。记录证据、根因范围和最小安全修复路径。不能为了缺失的 ACK、模型验证结果或审批而扩大网络权限、关闭策略，或虚构成功 Outcome。
