# 安全策略

## 支持的版本

| 版本 | 支持 |
|------|------|
| `v0.1.0` 源码部署版 | GitHub Release 正式发布后支持 |
| 最新 `main` 分支 | 开发中，只接受安全修复 |
| 旧 tag / 分支 | 仅作参考，不保证修复 |

## 报告漏洞或密钥泄露

**请勿在公开 Issue 中粘贴 API Key、Cookie、token、交易密码、完整 `settings.json`、数据库、日志、账户/余额/订单信息或含个人数据的分析记录。**

请通过以下方式私下联系维护者：

- GitHub：**Security Advisories**（仓库 → Security → Report a vulnerability），或
- QQ 群（见 README）私信维护者

报告中请尽量包含：

- 问题类型（误提交密钥、本地文件权限、依赖漏洞等）
- 影响范围与复现步骤
- 是否已在公开仓库历史中暴露密钥（如是，请说明大致时间以便协助轮换）

## 用户自查清单

1. 确认 `config/settings.json` 未被 `git add`（应被 `.gitignore` 忽略）。
2. 执行 `tools\setup_git_secrets.ps1` 启用 pre-commit 拦截。
3. 若 Key 曾进入 Git 历史：在服务商处轮换 Key，并清理 Git 历史或作废旧仓库镜像。
4. 开源 fork 或制作源码包时，拒绝 `records/`、`logs/`、`experience/`、数据库、原始行情和虚拟环境中的运行内容。
5. 发布前运行 `pa-agent --self-check` 和源码 ZIP 校验；离线自检不会读取或证明任何凭据有效。

## v0.1.0 交易边界

- 交易默认关闭。
- 只发布受控 OKX Demo 工作台。
- OKX Live 和 Longbridge 交易不在 v0.1.0 支持范围内。
- 新装执行路由为 OKX Demo，交易与自动执行仍默认关闭；旧 Live/Longbridge 路由不能绕过 Controller 或 Worker 新增风险。
- 多市场页只读；股票没有权威最小变动单位时只展示，禁止价格分析。

## 免责声明

本软件为交易分析辅助工具，不提供托管服务；安全配置（API、MT5 账户）由使用者自行负责。
