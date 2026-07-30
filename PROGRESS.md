# PA_Agent 工单进度

- 2026-07-29 多市场后端与无界面连接层已补强：新增 `market_workspace` 纯合同、Longbridge/OKX 批量报价、OKX K 线 `after` 分页与 10m 三页聚合、类型化 Longbridge 认证失败、K 线证据和独立只分析结果投影。全仓 2030 项通过、3 项跳过、0 失败；没有交易写入或运行态修改。
- 前端实现只认 `docs/prd/11_多市场看盘前端最终PRD_外部设计交付版.md`。Stitch、ChatGPT Web Images、浏览器自动化和外部样稿都不再是开发门；原生 PyQt6 四市场只读页已由 `91715ee2c8af0e6e829031b5a4f375d18f71cf52` 发布，旧 OKX Demo 工作台保留。
- 本轮 WO-E 文档开工基线为 `HEAD=origin/main=86bfb8e0c668479866038dd959e2b72cefc94811`；分支为 `main`，上游为 PUBLIC `origin/main`。更早的 WO-F 与完成证据复审已分别由 `c0b58d0`、`7e6c095` 发布。
- 完成证据复审补齐了 system prompt 字节稳定、固定代理端口抢占、长桥分页/日历边界，以及 Campaign 异常耐久收口与零重复写回归。
- NEW_RISK 最小权限修复已落在 `pa_agent/okx_demo_campaign.py`：动态杠杆、正常提交和 READY 恢复提交只在 Worker 命令返回耐久终态后释放租约；等待超时、读取异常或非终态结果不提前释放，进程收口仍会撤销租约，RUNNING 命令继续阻止再次授权。下一条新增风险命令重新执行私有只读预检并申请租约。定向回归 130 项通过、0 失败，unit/property/integration 为 1966 项通过、7 项跳过、0 失败。
- P0-01 已完成源码与提交级 CI 闭环：`c932e0113e9c4e33771d1cc5afc1f16beda46421` 把公共 `ExecutionController` / `WorkerStore` 租约升级为 schema v5 一次性令牌，数据库、Controller 和 Worker 共同限制每租约最多一条新增风险命令；本地确定性主门 2048 项通过、0 失败，GitHub Actions run `30447988360` 全绿，远端确定性门 2047 项通过、0 失败，另有 1 项 UTC 主机条件跳过。运行中的旧 Worker 尚未重载，运行态激活不属于本项。
- 全仓 Ruff 只读基线已刷新为 293 项诊断，其中 249 项带自动修复建议；这是历史债账本，不授权全仓 `--fix`。
- WO-E 设计合同已补齐 `QuoteSnapshot`、可测的 10m K 线新鲜度、四市场设置迁移、generation、Longbridge 内切换、Longbridge↔OKX 跨源回滚、脱敏输入、M01–M17 和 D01–D07；首版分析主周期固定为 10m，1h/4h 只作可缺失背景。原生 PyQt6 已接通 Controller 与独立只读运行层，不导入或调用执行层。
- WO-A 仍有一项未闭合：固定代理 metadata 与实际配置缺共同指纹。只改测试无法证明不存在不一致，本轮又禁止修改 `scripts`。
- 长桥旧默认档案仍无效，但完整的 `COMPREHENSIVE` 行情档案已经服务端真实验证；沪深报价和 1h/4h/1d K 线均可读取。该档案只通过非机密选择项启用，凭据没有进入代码、日志、测试或 Git。
- WO-E 的浏览器、Stitch 和 ChatGPT Images 路线已由用户终止。后端、无界面连接层和 PyQt6 离线实现已经补齐；当前剩余工作是 D 发布证据/安装链，以及使用已验证的 Longbridge 行情档案完成三股票市场与 Crypto 真实桌面验收。
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

## 2026-07-29 v0.1.0 发布工单

- 目标：交付 Windows + Python 3.12 源码部署版 v0.1.0；保留 OKX Demo 工作台，新增四市场只读看盘页，并让运行 Worker 加载 schema v5。
- 顺序：任务 0 基线 → A 运行态 v5 → B1 Longbridge 合同 → B2 Controller → C 原生 PyQt6 页面与桌面验收 → D 发布证据 → tag/Release。
- 固定基线：本地、`origin/main` 与 GitHub `main` 均为 `53c2267468f997956475e0934b2c9e0f2a20cda9`；暂存区和受跟踪文件无改动，既有 `.agents/`、`.claude/` 不触碰。
- 远端门：PUBLIC `xiaojinlucky/PA_Agent` 可写；远端当前不存在 `v0.1.0` tag 或 Release。
- 基线实测：确定性非 live 门为 2048 项通过、0 失败、0 错误、0 跳过；JUnit 位于忽略目录 `scratch/validation/baseline.xml`。
- 运行态只读门：Worker/心跳/对账与两库健康，活动 execution、活动命令、未解决 UNCERTAIN 和有效 NEW_RISK 租约均为 0；控制库仍为 v4，风险停止仍开启，券商实时空仓空单尚未证明。
- 最大风险：没有新鲜 OKX Demo 私有真相时绝不能迁移、重载、清风险停止或发 Demo 命令；Longbridge tick/权限/时间语义不能靠猜测补齐。
- 发布硬门：A/B/C 真实验收、全新 Python 3.12 安装、CI、脱敏证据和源码包均通过后，main、tag 与 Release 才可指向同一 SHA。
- B1 红灯覆盖时间解释、服务端权限、批量重复/额外标的、分页冲突、统一截止时间、半日市、高周期降级和 tick 能力门；最终相关 153 项通过、0 失败。
- B1 真实只读门：同一个 `COMPREHENSIVE` QuoteContext 已取得 AAPL.US、700.HK、600519.SH 的报价和 10m/1h/4h；三市场均由服务端证明为实时，所有周期绑定各自唯一 `analysis_as_of`，未创建交易上下文或调用 AI。
- Longbridge 当前没有给出可追溯的统一 `price_tick`，因此股票市场明确为 `display_only`；这是诚实能力边界，不阻塞只读页面，但会在两阶段 AI 之前阻断价格分析。
- B1 全仓非 live 门最终为 2082 项通过、0 失败、0 错误、0 跳过；CI 数量下限同步固定为 2082。权限套餐与行情级别矛盾或级别未知会失败关闭，失败重连不暴露旧权限；实时和延迟权限证据最多缓存 5 分钟，到期后原子重读服务端且失败不沿用旧证据。报价收到时间由数据源在响应后盖章，半日市提前收盘后仍保留半日市标签。
- staged gitleaks 首次把测试中的公开 Longbridge 套餐标识误判成通用 API key；暂存区已立即清空，测试改为运行时拼接同一公开值。修改后的 Windows 单进程全量连续两次触发既有 pyqtgraph `AxisItem` 析构崩溃，没有测试断言失败；改用 4 个 E2E 各自独立进程加其余 2078 项的机器 JUnit 汇总，合计仍为 2082 项通过、0 失败。目标提交的 GitHub CI 继续运行原始单进程命令，不降低门槛。
- B1 实现提交 `1b0f6c9eacd54326975fd11ba8cb86e78a4b1daf` 已精确推送到 `origin/main`；GitHub Actions run `30477680216` 全绿。远端原始单进程确定性门为 2082 项、0 失败、0 错误、1 项既有 UTC 主机条件跳过；独立 live 健康检查 7 项均跳过并明确记录 `health_status=unavailable`。证据包 `ci-evidence-1b0f6c9eacd54326975fd11ba8cb86e78a4b1daf` 的完整 SHA、Python 3.12.10、JUnit 和环境文件均已下载核对。
- B2 已实现无 Qt `MarketWorkspaceController`、独立 `MarketWorkspaceSettings` 保存合同和 `AppContext` 接线。Controller 是 selection generation、watchlist generation、请求序号、来源/截止时间、设置保存与分析状态的唯一所有者；不导入 Qt 或执行层。
- B2 反例覆盖逆序回调、快速切市场、共享认证失败与恢复、报价/K 线陈旧、Crypto 连续市场、保存迟到/失败/冲突、分析中切换、不完整返回值，以及超过 32 个迟到回调仍保留认证与审计事实。三轮对抗审查的 P1 反例均已闭合；不再用静默淘汰旧请求换取内存上限，长期无回调时的请求登记表资源上限留作 P2 后续治理。
- B2 扩展定向测试 230 项通过、0 失败；最终本地非 live 为 2137 项通过、0 失败、0 错误、0 跳过，JUnit 为 `scratch/validation/b2-final.xml`，CI 最低测试数同步提高到 2137。
- B2 实现提交 `18951dc53a5d2b075bda0759676a68dd62dca172` 已精确推送到 `origin/main`；GitHub Actions run `30484797101` 全绿。远端确定性门共 2137 项、0 失败、0 错误、1 项既有 UTC 主机条件跳过；独立 live 健康检查 7 项均跳过并记录 `health_status=unavailable`。证据包中的完整 SHA、Python 3.12.10、JUnit 和 91 行依赖快照均已核对。

## 2026-07-30 v0.1.0 阶段 C：原生 PyQt6 多市场页

- 新页复用 PRD11 三列结构并只消费 `MarketWorkspaceController`；Longbridge 与 OKX 使用彼此独立的只读运行层。旧 OKX Demo 工作台、交易执行层和默认关闭的交易安全门均保留，新页没有买入、卖出、下单、撤单或平仓控件。
- 行情报价先固定唯一 `analysis_as_of`，再读取 10m/1h/4h；10m 是分析硬输入，1h/4h 缺失不会终止 10m。K 线新鲜度使用来源计算的真实收盘时间，禁止把开盘时间冒充收盘时间。股票没有权威 tick 时仍可展示，但分析按钮明确关闭；成交量继续不进入提示词。
- 红灯直接证明旧分析回调可清空新分析状态、旧 CancelToken 未取消、K 线开盘时间会把有效数据误判过期；最小修复后全部转绿。对抗审查复核后无 P0/P1/P2。
- 本地相关定向测试 334 项通过、0 失败；最终非 live 为 2164 项通过、0 失败、0 错误、0 跳过，CI 数量下限同步提高到 2164。实现提交 `91715ee2c8af0e6e829031b5a4f375d18f71cf52` 已推送，GitHub Actions run `30492305516` 全绿。
- 远端证据包的完整 SHA 与提交一致，Python 为 3.12.10；确定性 JUnit 共 2164 项、0 失败、0 错误、1 项既有 UTC 主机条件跳过。live JUnit 共 7 项、0 失败、0 错误、7 项跳过，明确记录 `health_status=unavailable`。
- 已用目标提交重新生成并逐张打开 16 张离屏图：1440×900 九种状态、1920×1080 正常态，以及 100%/125%/150% × 两种逻辑尺寸。机器门为 16 张通过、0 失败、界面运行态读取 0；完整 SHA 可见。离屏夹具不冒充真实桌面，正式快捷方式的四市场快速切换与缩放验收仍为 Release 硬门。

## 2026-07-30 v0.1.0 阶段 D：源码部署与发布链

- 已完成本地实现：`0.1.0` 单一版本真值；不会启动 Qt、网络或数据库的 `pa-agent` / `pa-execution-worker --version` 与 `--self-check`；Windows + Python 3.12 源码安装、快捷方式和受控卸载；源码 ZIP、证据 ZIP、精确 manifest、SHA256SUMS 与五层能力索引合同。
- 新安装的默认交易状态固定关闭；新增风险路由仅允许 OKX Demo。旧设置保存时若仍指向 Longbridge 或 OKX Live，会修复为 OKX Demo；Controller、Worker 路径和 Worker 三层均拒绝其他新增风险路由。
- 候选 Release workflow 只生成和上传候选包，不创建 tag 或稳定 Release。稳定发布脚本固定 PUBLIC `xiaojinlucky/PA_Agent`，要求干净且实时同步的 `origin/main`、同 SHA 的 CI 与候选 workflow 均绿色、外部证据新鲜且完整；先创建草稿并逐个下载复核资产，公开后再重复核对名称、大小和 SHA256，任一失败都不会输出发布成功。
- 发布材料会扫描源码解包目录、整个外部证据目录，以及待上传的源码/证据/manifest/SHA256SUMS/CHANGELOG 集合。manifest 只允许固定字段和固定嵌套结构；“附加敏感字段并重算校验和”的反例先为 1 项失败，修复后转绿。
- D 主体提交 `4c9d16fe29611a2c35eaa725e2e843d2e745885b` 已推送；本地、`origin/main` 与 GitHub `main` 当时一致。GitHub Actions run `30502065534` 在首个界面冒烟用例结束后触发 pyqtgraph/Qt 坐标轴销毁访问冲突，JUnit 因进程硬崩未生成，工作流按设计变红；没有把重跑当修复，也没有启动候选 workflow。
- 红灯证明 `PlotWidget.close()` 不能重复调用、图表定时器在窗口关闭后仍活动，且工作区绕过 `closeEvent` 直接销毁时缺少可靠的顶层 Python 所有者。修复让可见顶层主窗口保持到真实关闭事件，先停止图表定时器和绘制，再由原 Qt 父对象完成销毁，不再提前拆坐标轴或把图表脱离所有权树。
- 新增 3 条生命周期回归；相关 43 项界面套件连续 5 轮通过，发布/离线入口与 E2E 定向 61 项通过。最终原样 `-m "not live"` 为 2227 项通过、0 失败、0 错误、0 跳过，另有 7 项真实联网用例按门未运行；CI 与候选 workflow 数量门同步提高到 2227。
- Qt 生命周期修复已由 `ed8c52ee61fa2ff379b561f554774c3ac4da619e` 推送。其 GitHub Actions run `30504823117` 完整跑到 100%，没有再发生 Qt 底层访问冲突；结果为 2223 项通过、3 项失败、1 项宿主机条件跳过，3 项失败都来自 Windows PowerShell 5.1 把无 BOM 的中文发布脚本按旧编码读取，没有启动候选 workflow。
- PowerShell 5.1 兼容反例已固化：三份发布脚本必须以 UTF-8 BOM 开头。补齐编码标记后，Windows PowerShell 5.1 和 PowerShell 7 均可解析三份脚本，发布/离线入口与 E2E 定向 61 项通过，最终非 live 2227 项通过、0 失败、0 错误、0 跳过。
- 独立只读对抗审查确认本轮无 P0/P1：BOM 修复覆盖真实 Windows PowerShell 5.1 根因，测试没有改用 PowerShell 7 或放宽失败路径，文档没有把本地结果冒充提交级、安装或稳定发布证据。
- 编码修复提交 `3221fe88769a844bb4a2c0a55681f98ed2446122` 已推送；GitHub Actions CI run `30506024918` 全绿，远端确定性门为 2227 项通过、0 失败、0 错误、1 项宿主机条件跳过。证据包完整绑定该 SHA、Python 3.12.10 和 91 行依赖快照；独立 live 健康检查 7 项中 1 项通过、6 项因外部条件跳过、0 失败，诚实记录为部分可用。
- 同 SHA 的候选 Release run `30506362460` 已通过仓库密钥扫描、两个离线入口、2227 项发布测试、数量门和证据索引；随后 `git archive` 扫描正确拒绝 `experience/logs/records/trade_records`。根因是 21 个受版本控制的 `.gitkeep` 让这些运行态目录进入 ZIP，后续全新安装和资产上传因此没有执行。
- 首轮 `records export-ignore` 被独立对抗审查发现会递归误删 `pa_agent/records` 的 7 个核心模块，该误绿证据已撤回且未提交。最终四条规则使用仓库根锚；真实归档回归同时证明根运行态目录不存在、7 个记录模块完整保留。扫描器取消 `.gitkeep` 例外，手工 ZIP 中四种运行态占位文件均被拒绝；发布必需源码清单也显式纳入 7 个记录模块。
- 新增两条回归后，发布/离线/E2E 定向 63 项通过，最终非 live 2229 项通过、0 失败、0 错误、0 跳过；CI 与候选 workflow 数量门同步提高到 2229。真实 `git archive` 仍含 37 个提示词、两个入口和完整 SHA，禁止项为 0。
- 修正后由同一独立审查者复核，确认根锚、7 个记录模块、真实归档、`.gitkeep` 拒绝和文档状态均闭合，无 P0/P1/P2。
- 归档修复提交 `e8354044a5b261c65a1eef772f58163da7887f22` 已推送；CI run `30507586401` 全绿，远端确定性门为 2229 项通过、0 失败、0 错误、1 项宿主机条件跳过，证据包绑定完整 SHA、Python 3.12.10 和 91 行依赖快照。
- 候选 Release run `30507852571` 通过密钥扫描、两个离线入口、2229 项发布测试、数量门、证据索引和真实源码归档；随后全新安装在约 2 秒时失败，因为 GitHub Runner 的 PATH 有三个 `git.exe`，脚本把三个 `.Source` 合成一个不可执行字符串，后续离屏证据和资产上传未运行。
- 多 Git 反例已在本机相同的三个 Git 环境复现并固化；安装脚本现明确选择首个 Application。独立审查没有发现 P0/P1，但指出回归依赖宿主机有多个 Git 的 P2；测试随后改为在临时 PATH 中自行放置两个 Git，先断言数量与首项，再运行安装脚本，P2 已关闭。发布/离线/E2E 定向 64 项通过，最终非 live 2230 项通过、0 失败、0 错误、0 跳过。独立源码解压目录中的真实 Python 3.12 新装已完成：37 个提示词、两个入口、默认交易关闭、完整 SHA、临时快捷方式均通过；受控卸载只移除 `.venv` 和快捷方式，源码保留。
- 下一门是让安装修复对应的发布 SHA 通过提交级 CI 和候选 workflow；候选通过后再下载并独立核对四个资产。阶段 A 与正式快捷方式桌面矩阵仍是稳定 Release 硬阻塞。
