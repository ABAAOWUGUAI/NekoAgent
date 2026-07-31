# 仓库保护指南

[English](../REPOSITORY_PROTECTION.md)

本指南把仓库安全要求转化为 GitHub 管理员可核对的设置。提交本文件、工作流或 `.github/CODEOWNERS` **不会**自动启用 GitHub 设置。

## 1. 保持漏洞报告私密

对公开上游仓库或 Fork，请在 **Settings → Security and analysis** 中启用 GitHub 私密漏洞报告。报告者应能提交漏洞而无需公开 Issue 或披露 PoC；处理流程见[中文安全政策](../../SECURITY.zh-CN.md)。

若当前账号或计划不可用私密报告，不要要求报告者公开利用细节；应按 `SECURITY.zh-CN.md` 的方式提供私密联系通道。

## 2. 保护默认分支

在接受外部变更前，为默认分支创建 GitHub Ruleset 或 Branch protection rule。最低要求：

1. 合并必须经 Pull Request。
2. 外部贡献者的变更至少需要一个批准 Review。
3. 变更路径命中 [`.github/CODEOWNERS`](../../.github/CODEOWNERS) 时需要 Code Owner Review。
4. 要求 [`.github/workflows/verify.yml`](../../.github/workflows/verify.yml) 中的 `Verify public source / public-source-gate`。以 GitHub 页面实际显示的检查名称为准。
5. 禁止 force push 和删除分支。
6. 除非已有有时限的紧急流程，否则规则也应对管理员生效。

单维护者仓库的自审没有独立意义。仍应保留必需 CI、禁止 force push，并在敏感路径变更前引入另一位可信维护者或经过审查的发行清单。

## 3. 审查自动化与凭据

- 工作流权限保持最小；当前验证工作流只需要读取仓库。
- 不得在仓库 Secret、工作流日志、样例文件或 Issue 附件中放入 Provider Key、QQ 凭据、Codex 登录态、Cookie、SQLite 数据或生产 manifest。
- 审查所有可访问仓库的 GitHub App、Action、deploy key 和 personal access token，移除不再需要的访问。
- 添加第三方 Action 前，应锁定或审查版本；优先使用项目现有的官方 `actions/checkout` 与 `actions/setup-python`。
- 可用时开启 Dependabot alerts 与 secret scanning。告警表示需要调查，不是修改生产配置的自动授权。

## 4. 保护发行

发布 Tag、Release 或容器镜像前：

1. 审查导出的文件 manifest 和 Secret 扫描结果。
2. 在**完全相同的导出目录**运行 `python tools/run_public_tests.py`。
3. 运行 `python tools/install_starter_pack.py --pack-dir <公开-pack-目录> --dry-run`；它只能检查所选可选 Pack，不能导入运行态。
4. 确认 Release notes 区分已验证行为与可选/未验证集成。
5. 从受保护默认分支上的已审查提交发布；不得 force-update 公开 Release Tag。

## 5. 定期审计

至少每季度一次，或维护者变更后：

- 审查分支规则和 Code Owner；
- 审查安全报告设置与联系信息；
- 移除过期协作者、App、deploy key 与 workflow secret；
- 检查近期 Actions 是否出现意外写权限或 Artifact；
- 在分发新快照前重新运行公开导出 Gate。

这些控制保护的是公开源码仓库，不能替代产品运行时的 Capability、Approval、Network、Delivery ACK 与审计边界。
