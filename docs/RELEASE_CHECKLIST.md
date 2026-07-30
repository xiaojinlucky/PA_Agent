# PA_Agent v0.1.0 发布检查表

只有所有“稳定发布硬门”通过，才允许创建 `v0.1.0` tag 和 GitHub Release。

## 版本和源码包

- [ ] `pa_agent.__version__` 为唯一版本真值，值为 `0.1.0`
- [ ] Python 合同为 `>=3.12,<3.13`
- [ ] `pa-agent --version` 与 `pa-execution-worker --version` 显示同一完整 SHA
- [ ] 两个入口的 `--self-check` 均通过，且不启动 Qt、网络或数据库
- [ ] `git archive` 源码 ZIP 通过敏感文件、运行态文件、路径穿越、大文件和资源数量检查
- [ ] 同一目标 SHA 在 UTC 与非 UTC 主机重建的源码 ZIP 字节和 SHA-256 完全一致
- [ ] 源码 ZIP 只表示源码部署，不含 EXE、安装器、wheel 或虚拟环境

## 测试和安装

- [ ] 本地非 live 全量测试 0 失败、0 错误，数量不低于当前 CI 门
- [ ] 目标 SHA 的 GitHub CI 全绿并上传 JUnit、完整 SHA、Python 版本和依赖快照
- [ ] 全新目录和全新 Python 3.12 虚拟环境完成 editable 安装
- [ ] 37 个提示词资源存在
- [ ] 两个命令入口存在
- [ ] 默认交易关闭
- [ ] 创建并验证 `PA_Agent.lnk`
- [ ] 卸载脚本只在显式确认后删除该源码目录内的 `.venv` 和指定快捷方式

## 真实运行与桌面

- [ ] OKX Demo 私有只读真相证明账户、仓位、普通单和算法单满足安全门
- [ ] Worker 由正式代码完成 v4→v5，重启后显示完整 SHA、schema v5 和新启动时间
- [ ] 一次 controlled_reproducible OKX Demo 闭环结束为空仓、空普通单、空算法单
- [ ] 正式快捷方式完成 AAPL.US、700.HK、600519.SH 和 Crypto 桌面矩阵
- [ ] 100%/125%/150% 缩放与 1440×900/1920×1080 均通过
- [ ] 离屏证据字体覆盖核心中文与全部界面必需符号，16 张下载图逐张确认无方框、裁切或重叠
- [ ] 离屏证据使用 `synchronous-widget-render-v1` 同步渲染当前控件树；每张 PNG 的 SHA-256 与同名元数据一致
- [ ] 股票无权威 tick 时保持“仅展示，价格分析不可用”

## 发布证据

- [ ] 目标代码 SHA 已先固定并推送；稳定证据随后生成在 `scratch/release/evidence/` 或仓库外，不写回该 Git 提交
- [ ] 候选 `capability-index.json` 位于证据根部并绑定候选完整 SHA；源码发布 code/tests/runtime 三层只在最终 JUnit 与两个全新安装自检通过后标记为 `verified`
- [ ] 候选证据 ZIP 压缩后执行 `validate-candidate-archive`，确认 ZIP 文件集合、唯一索引、声明哈希、完整 16 图合同、2295 项测试门和完整安装合同逐字节一致
- [ ] 外部 `capability-index.json` 的 `as_of_git_sha` 等于目标代码 SHA，五层状态全部为 `verified` 或 `not_applicable`
- [ ] 外部证据中的运行态、行情、正式快捷方式和源码部署 JSON 都绑定同一目标代码 SHA，并有真实 SHA-256
- [ ] 使用目标 SHA 执行下列稳定门，结果通过：

  ```powershell
  .\.venv\Scripts\python.exe scripts\release_pipeline.py validate-index `
    --stable `
    --path capability-index.json `
    --evidence-root scratch\release\evidence `
    --schema-root docs\evidence\schemas `
    --source-archive scratch\release\PA_Agent-v0.1.0-source.zip `
    --evidence-archive scratch\release\PA_Agent-v0.1.0-evidence.zip `
    --release-manifest scratch\release\release-manifest.json `
    --checksums scratch\release\SHA256SUMS `
    --require-fresh-now `
    --sha <40位目标SHA>
  ```

- [ ] `stable_release_ready=true`
- [ ] `release-manifest.json`、`SHA256SUMS` 和脱敏证据 ZIP 已生成并复核
- [ ] 源码 ZIP 只来自目标 SHA 的 `git archive`，不含凭据、账号、余额、订单号、数据库、日志或原始行情
- [ ] 证据 ZIP、manifest 和 SHA256SUMS 不含上述敏感内容或本机私有路径；源码中允许保留产品固定路径和通用安装示例
- [ ] `main`、`v0.1.0` tag 和 GitHub Release 指向同一 SHA
- [ ] GitHub Release URL 可访问

`.github/workflows/release.yml` 只生成候选源码包和离屏证据，不会自动创建稳定 Release。这样可以避免在真实 Worker、Longbridge 桌面矩阵或用户验收未完成时误发布。只有上述外部稳定门通过后，才允许人工创建 tag 和 Release。

稳定门通过后，由同一发布提交中已经固定的 `scripts\publish_release.ps1` 完成最后发布。脚本会重新核对干净 `main`、实时 `origin/main`、目标 SHA 的绿色 CI 和绿色候选 Release workflow、远端 tag/Release 不存在、整个外部证据目录与待上传集合的密钥/本机路径扫描，以及上面的稳定门。随后先创建草稿 Release，核对 tag、四个资产的名称和大小，重新下载四个资产逐一比较 SHA256；全部一致且 `origin/main` 仍未移动时才公开草稿。公开后会再次核对 main、tag、Release 状态，并再次下载四个公开资产核对精确名称、大小和 SHA256；此时失败会明确报告“Release 已公开但最终资产验收失败”，绝不输出发布成功。必须显式传入 `-ConfirmStableRelease`，不能用候选 workflow 代替：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\publish_release.ps1 `
  -ReleaseRoot .\scratch\release `
  -ConfirmStableRelease
```

## 明确排除

- OKX Live
- Longbridge paper/综合/日内交易
- EXE、安装器和 PyPI wheel
- 收益、策略有效性或无人值守生产交易承诺
