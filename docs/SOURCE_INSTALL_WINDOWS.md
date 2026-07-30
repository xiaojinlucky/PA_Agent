# Windows 源码安装与卸载

v0.1.0 只支持 Windows 10/11 + Python 3.12 源码部署。它不是 EXE、安装器或 PyPI wheel。

## 安装前

1. 从 GitHub Release 下载 `PA_Agent-v0.1.0-source.zip` 和 `SHA256SUMS`。
2. 用 `Get-FileHash -Algorithm SHA256` 核对 ZIP。
3. 把 ZIP 解压到一个新的、专用的目录。
4. 确认系统命令 `python --version` 显示 Python 3.12。
5. 确认 `git --version` 可用。项目有一个固定到完整 commit 的 Git 依赖；普通 Windows 机器必须先安装 Git for Windows。

## 自动安装

在解压目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1
```

脚本会：

- 在当前源码目录创建 `.venv`
- 以 editable 方式安装源码
- 验证 37 个提示词资源和两个命令入口
- 运行不触碰 Qt、网络和数据库的离线自检
- 在当前用户桌面创建 `PA_Agent.lnk`

若快捷方式已存在，脚本会停止；只有明确加入 `-ReplaceShortcut` 才会替换。

## 手工检查

```powershell
.\.venv\Scripts\pa-agent.exe --version
.\.venv\Scripts\pa-agent.exe --self-check
.\.venv\Scripts\pa-execution-worker.exe --version
.\.venv\Scripts\pa-execution-worker.exe --self-check
```

`--self-check` 只验证源码部署合同和默认安全值，不读取凭据、行情、账户、数据库或运行日志。

## 启动

双击桌面的 `PA_Agent.lnk`。首次启动前不要把任何凭据放进源码目录；本地配置和运行数据必须保持在 Git 忽略范围内。

v0.1.0 的交易能力默认关闭，并明确不支持 OKX Live 或 Longbridge 交易。只读多市场页不含交易按钮。

新装配置的执行路线为 OKX Demo，但 `execution.enabled` 和 `execution.auto_execute` 仍为关闭。开启 Demo 前仍必须完成 Worker、账户、空仓空单和风险停止检查。

## 卸载

卸载脚本必须显式确认：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_windows.ps1 -ConfirmUninstall
```

它只删除：

- 当前源码目录内的 `.venv`
- 指定的 `PA_Agent.lnk`

源码、配置和用户数据不会自动删除。若要删除源码目录或本地数据，请先自行备份并手工确认目标路径。
