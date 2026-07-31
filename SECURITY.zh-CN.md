# 安全政策

[English](SECURITY.md)

请勿在公开 Issue 中报告漏洞、粘贴凭据、私聊正文、媒体、数据库文件或服务器细节。仓库管理员启用 GitHub **Settings → Security and analysis** 中的私密漏洞报告后，应使用该入口。若入口尚不可用，请不要公开敏感细节；只提交最小 Issue 请求维护者提供私密报告渠道。

在首个带标签的 Release 前，`main` 是受支持的开发版本。安全修复应尽量包含合成数据的最小复现、影响说明和回归测试。

本项目为自托管软件。部署者负责保护自己的模型 Key、渠道凭据、数据库、Artifact 存储和反向代理。首次部署时，绝不能把 Admin API 直接暴露到互联网。

部署加固、备份与事件处置见[中文部署指南](docs/zh-CN/DEPLOYMENT.md)和[中文运行指南](docs/zh-CN/OPERATIONS.md)。私密漏洞报告、分支保护和发布审查的 GitHub 设置见[中文仓库保护指南](docs/zh-CN/REPOSITORY_PROTECTION.md)。仅克隆源码不会自动启用这些 GitHub 设置，维护者必须显式完成。
