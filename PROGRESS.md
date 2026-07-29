# PA_Agent 工单进度

- 2026-07-29 多市场后端与无界面连接层已补强：新增 `market_workspace` 纯合同、Longbridge/OKX 批量报价、OKX K 线 `after` 分页与 10m 三页聚合、类型化 Longbridge 认证失败、K 线证据和独立只分析结果投影。全仓 2030 项通过、3 项跳过、0 失败；没有交易写入或运行态修改。
- 外部前端设计只认 `docs/prd/11_多市场看盘前端最终PRD_外部设计交付版.md`。Stitch、ChatGPT Web Images 和浏览器自动化记录降为历史，不再是开发门；`pa_agent/gui` 尚未修改，PyQt6 视觉实现仍待外部样稿返回后开始。
- 本轮 WO-E 文档开工基线为 `HEAD=origin/main=86bfb8e0c668479866038dd959e2b72cefc94811`；分支为 `main`，上游为 PUBLIC `origin/main`。更早的 WO-F 与完成证据复审已分别由 `c0b58d0`、`7e6c095` 发布。
- 完成证据复审补齐了 system prompt 字节稳定、固定代理端口抢占、长桥分页/日历边界，以及 Campaign 异常耐久收口与零重复写回归。
- NEW_RISK 最小权限修复已落在 `pa_agent/okx_demo_campaign.py`：动态杠杆、正常提交和 READY 恢复提交只在 Worker 命令返回耐久终态后释放租约；等待超时、读取异常或非终态结果不提前释放，进程收口仍会撤销租约，RUNNING 命令继续阻止再次授权。下一条新增风险命令重新执行私有只读预检并申请租约。定向回归 130 项通过、0 失败，unit/property/integration 为 1966 项通过、7 项跳过、0 失败。
- P0-01 已完成源码与提交级 CI 闭环：`c932e0113e9c4e33771d1cc5afc1f16beda46421` 把公共 `ExecutionController` / `WorkerStore` 租约升级为 schema v5 一次性令牌，数据库、Controller 和 Worker 共同限制每租约最多一条新增风险命令；本地确定性主门 2048 项通过、0 失败，GitHub Actions run `30447988360` 全绿，远端确定性门 2047 项通过、0 失败，另有 1 项 UTC 主机条件跳过。运行中的旧 Worker 尚未重载，运行态激活不属于本项。
- 全仓 Ruff 只读基线已刷新为 293 项诊断，其中 249 项带自动修复建议；这是历史债账本，不授权全仓 `--fix`。
- WO-E 设计合同已补齐 `QuoteSnapshot`、可测的 10m K 线新鲜度、四市场设置迁移、generation、Longbridge 内切换、Longbridge↔OKX 跨源回滚、脱敏输入、M01–M17 和 D01–D07；首版分析主周期固定为 10m，1h/4h 只作背景证据。Product Design B1、用户选择 B2 与 ChatGPT Web B3 已通过。完整网页回答、内容指纹和 F01–F22 本地裁决已落盘；当前没有修改 PyQt6。
- WO-A 仍有一项未闭合：固定代理 metadata 与实际配置缺共同指纹。只改测试无法证明不存在不一致，本轮又禁止修改 `scripts`。
- 长桥旧默认档案仍无效，但完整的 `COMPREHENSIVE` 行情档案已经服务端真实验证；沪深报价和 1h/4h/1d K 线均可读取。该档案只通过非机密选择项启用，凭据没有进入代码、日志、测试或 Git。
- WO-E 的浏览器、Stitch 和 ChatGPT Images 路线已由用户终止。后端与无界面连接层已经补齐；外部视觉设计只认 PRD11。当前剩余工作是用户取得外部样稿后实现 PyQt6，并使用已验证的 Longbridge 行情档案完成三股票市场桌面验收。
- 当前最新已知运行态证据来自 2026-07-29 15:17 的只读审计：Worker 与心跳运行，两库健康，活动 execution、pending/running 命令、未解决 UNCERTAIN 和有效 NEW_RISK 租约均为 0；但风险停止为 `risk_runtime_BrokerTransportError`，Campaign 不存在且磁盘 `active` 状态过期。运行中 Worker 仍是旧 schema v4 代码；未经新的 OKX Demo 私有只读硬门和用户授权不重载。
- WO-H、WO-C2、WO-F、WO-G-1/2/3 已完成；成交量仍不进入提示词或交易判断。
- `WO-POS-05` 只有路线图级目标且会触碰当前禁止修改的执行链，本轮不越界开工。

## 2026-07-29 P0-01 一次性 NEW_RISK 授权整改

- 目标：把“一次授权最多产生一条新增风险命令”做成数据库、Controller、Worker 的共同硬约束；本轮不处理其他审查项。
- 顺序：固定基线与只读运行态门 → 先补红灯测试 → schema v5 与三层实现 → 全量测试与 CI/证据 → 精确提交、推送并等待目标 SHA 的 CI。
- 固定基线：`main/e815d42268efac5b83842a33b7e24c9054329c78`，`origin/main` 同步，暂存区为空；既有未跟踪 `.agents/`、`.claude/` 不触碰。
- 基线实测：`2030 passed, 3 skipped, 0 failed`；JUnit 已写入 `scratch/wo-review-p0-baseline.xml`。
- 最大风险：v4 历史库可能已有同租约多条新增风险命令；迁移必须原样保留并失败关闭，禁止静默挑选一条。
- 运行态门：Worker 活着但券商连接失败且风险停止已启用；Campaign 无进程，当前无活动 execution、命令或租约，只允许离线整改。
- 红绿证据：新增安全场景先为 13 项失败；schema v5 与三层实现后同一组 13 项全部通过，证明原漏洞存在且修复生效。
- 用户已明确授权同步更新 `tests/unit/test_risk_runtime.py` 两处直接相关的 schema 版本断言；实现、测试和完整 CI 配置已恢复。
- 首轮本地全仓为 `2043 passed, 3 skipped, 0 failed`。独立对抗审查随后发现 Controller 提交后续租竞态、终态续租夹缝和两项证据缺口；四条新增回归均先证明相应旧缺口，修复后全绿，相关七文件共 269 项通过，最终复审无 P0/P1/P2。
- 旧式混合全仓运行曾为 `2043 passed, 7 skipped, 0 failed`；新增的 4 个跳过均来自既有 AkShare 联网冒烟测试访问 `push2his.eastmoney.com` 时被远端断开。经当前代理时为 `ProxyError/RemoteDisconnected`，隔离子进程临时直连仍为 `RemoteDisconnected`；域名解析与 443 端口正常。单个用例曾在 17:38 恢复并通过，但随后整组 4 项再次全部跳过，本机到该接口的 HTTPS 访问链路仍不稳定；未修改系统代理、测试或行情结果。
- 18:01 再次只读核对运行态：Worker 服务与心跳正常，两库 `quick_check=ok`，活动 execution、pending/running 命令、未解决 UNCERTAIN 和有效租约均为 0；风险停止保持激活，Campaign 无 Python 进程，正式配置哈希未变。
- 18:05 最终单用例探针仍因同一 AkShare HTTPS 连接错误跳过；同一外部阻塞已连续出现三个目标轮次，按任务规则停止重复请求并等待外部状态变化。
- 用户明确授权把真实数据源健康检查从无凭据确定性主门分离，避免公共端点波动决定交易安全代码能否发布；这不是删除测试，所有用例仍分别由 `-m "not live"` 与 `-m live` 覆盖。确定性门强制测试数不低于当前 2048、失败/错误为 0、跳过不高于 3；live 结果单独保存运行状态，JUnit 在进程正常结束时上传，硬崩或启动失败时明确记录为 `missing`，不掩盖主门失败也不反向阻塞主门。
- 用户随后明确授权改用已在 `Quant\env` 找到并经服务端真实验证的 `LONGBRIDGE_COMPREHENSIVE_*` 只读行情档案，并把真实联网检查与无凭据主 CI 分开。新增档案选择契约首轮为 1 项通过、3 项失败，证明当前加载器仍会忽略命名档案、继续使用旧默认凭据；实现尚未开始时没有修改或输出任何 token。
- 显式行情档案选择实现后，原有同组 4 项全部通过；独立审查补出的 `INTRADAY` 正向隔离用例也已通过，Longbridge 数据源单文件 73 项通过。`Quant\env` 只新增非机密的 `PA_AGENT_LONGBRIDGE_QUOTE_PROFILE=COMPREHENSIVE`，没有修改任何凭据；PA_Agent 正常加载路径已真实取得两只沪深股票报价，并成功读取 1h、4h、1d K 线。
- 首次确定性全量在第 3 个界面冒烟用例附近发生一次 Qt 底层销毁竞态；4 个界面冒烟用例单独复跑全部通过，最终同一确定性命令原样完成 2048 项通过、0 项跳过、0 失败，JUnit 为 `scratch/wo-review-p0-deterministic-final.xml`，没有修改 GUI 或放宽测试。
- 独立 live 命令收集 7 项：4 项因 AkShare 公共端点不可达跳过，3 项因未提供 KKAI 测试密钥跳过，0 失败；JUnit 为 `scratch/wo-review-p0-live-provider.xml`。实现提交 `c932e0113e9c4e33771d1cc5afc1f16beda46421` 已精确推送到 `origin/main`；GitHub Actions run `30447988360` 全绿并上传同 SHA 的 JUnit 与环境证据，P0 源码与 CI 闭环已关闭。
- 最新独立审查先后报告的 CI 边界均已整改：live JUnit 缺失或损坏分别记录 `missing` / `invalid`，不会反向阻塞主门；数量门提升到 2048；Git SHA、Python、`pip freeze` 使用 `always()` 采集，任一失败先写标准 `unavailable` 再让 CI 变红；`INTRADAY` 正向隔离用例已补齐。三条 PowerShell 成功、损坏 XML 和 pip 失败分支均已本地执行验证。
- 远端证据包 `ci-evidence-c932e0113e9c4e33771d1cc5afc1f16beda46421` 已下载核对：完整 Git SHA 与目标提交一致，Python 为 3.12.10，`pip freeze` 为 91 行；确定性 JUnit 共 2048 项、0 失败、0 错误、1 项因 CI 主机为 UTC 跳过，live JUnit 共 7 项、0 失败、0 错误、7 项跳过并明确记录 `health_status=unavailable`。唯一注解是 GitHub 托管运行器把部分官方 Action 从已弃用的 Node.js 20 强制切到 Node.js 24，不影响本轮结论。
