# PA_Agent 总控工单（2026-07-27 起）

> **本文档性质**：Claude（Fable 5，总控/大脑/审查者）编写的自包含执行工单。任何接手的大模型（GPT/Codex/其他）按本文档推进，**不需要**读取 Claude 的对话历史。每张工单完成后，执行者必须回写本文档的「进度账本」一节。Claude 会话存活时由 Claude 持续更新本文档；Claude 额度耗尽后，接手者继续沿本文档执行。
>
> **接手者第一步**：先读 `CLAUDE.md`（项目铁律）→ `CONTEXT.md` 顶部（当前真值）→ 本文档全文。凡与本文档冲突的旧文档，以 `CLAUDE.md` 红线 > 本文档 > 其他为序。

---

## 0. 总架构与不可破坏红线（接手者必背）

### 0.1 系统架构一句话

PA_Agent = PyQt6 桌面产品：**结构化 K 线 + 本地预计算指标 → 两阶段 LLM 价格行为分析（Stage1 诊断 → Stage2 决策）→ 确定性风险定仓 → 唯一写链执行**。当前生产范围：`OKX Demo / XAU-USDT-SWAP / 严格聚合 10m / min_trade_confidence=20 / extreme_aggressive`。多市场（美股/港股/A股，经长桥）P1 后端已落地，前端未动。

### 0.2 唯一写链（永不改变）

```
GUI/Campaign → ExecutionController → records/execution_control.sqlite3
  → 单例 ExecutionWorker（WinSW 服务 PAAgentExecutionWorker，LocalService）
  → ExecutionService → OkxAdapter → OKX Demo
```

- GUI、Campaign、任何脚本、任何 Agent **绝不**直接写券商；只有 Worker 写。
- Live 双硬门（`PA_AGENT_LIVE_TRADING_ENABLED` + `OKX_LIVE_ENABLED`）保持关闭。
- `no_order` 是正常结果；禁止强制交易、伪造成交、放宽风险闸门、截断风险数量。

### 0.3 数值红线（改动即 CRITICAL）

| 项 | 值 |
|---|---|
| 风险高水位 `adjusted_high_water_usd` | 已从 PUBLIC 文档脱敏；任何“恢复”都不得重锚，以本地风险账本为准 |
| OKX Demo 账户身份摘要 | 已从 PUBLIC 文档脱敏；每次操作前以实时私有只读硬门逐字符核对 |
| 历史 UNCERTAIN 命令 `686b6d0e-5c85-4430-a2d9-b9e069b76934` | 已处置 `confirmed_not_written_schema_validation`，禁止重放 |
| 固定风险参数 | `20000 USDT 上限 / 10% / 20x / risk_budget` |

### 0.4 行为红线

- 代理失败必须闭锁新增风险（fail-closed）；**永不**回退直连、系统代理或用户主 v2rayN(10808)。OKX 流量只走 `127.0.0.1:10981`。
- 每根已收盘 10m K 线必须真实两阶段分析；停机期间的中间 K 线**不补跑、不冒充**。
- Campaign 重启必须用 `restart` 语义继承 `last_completed_bar_ms`。
- 禁止 `git add .` / `git add -A` / 强推 / 改写历史 / 擅自开分支或 worktree / 提交 scratch、数据库、日志、凭据。
- 用户已于 2026-07-27 对 `D:\Desktop\Quant` 顶层项目的正常精确提交与推送给出长期默认授权；`xiaojinlucky/PA_Agent` 是已登记的 PUBLIC 例外。仍须逐次列明文件与验证、只暂存本轮精确路径并执行远端与 staged 安全硬检查，出现清单外变化立即停止。
- 禁止读取/修改/运行 `D:\Desktop\Quant\AlphaMaster`（共享层 `D:\Desktop\Quant\shared` 例外，本会话线拥有写权）。
- 桌面 GUI 由用户自己从 `D:\Desktop\PA_Agent.lnk` 启动；Agent 不操作桌面和鼠标。
- 测试必须 `QT_QPA_PLATFORM=offscreen` + 凭据隔离（conftest 已做）；pytest 用 `--basetemp` 指向临时目录 + `-p no:cacheprovider`。

### 0.5 运行态证据口径

- 本文后文记录的 PID、Campaign ID、账户和订单状态都是带时间戳的历史证据，不能当成当前事实。
- 最新已登记证据以 `CONTEXT.md` 和 `docs/VALIDATION_EVIDENCE.md` 最后一节为准。2026-07-27 20:13 的历史回查证明 WO-F 安全重载后 Campaign active、Worker/两库/风险态健康且券商空仓空单；这仍不能替代下一次操作前的实时只读硬门。
- “不操作交易运行态”是 WO-H 接手批次的当轮边界。后续 WO-C2、WO-F 已在另行核验完整硬门后完成授权范围内的恢复与验收；不得把旧禁止扩大成历史未发生，也不得把后续验收扩大成新的运行态操作授权。

---

## 1. 本阶段已完成（有证据，勿重做）

| 事项 | 证据位置 |
|---|---|
| 固定代理换节点（好猫 US1 AnyTLS → 桔子云 OKX 日本 1 VMess） | `records\okx_fixed_proxy\metadata.json`；切换后日志零 SSLEOFError |
| 探针空闲耐久闸门（--idle-seconds/--idle-cycles，激活强制 ≥1 周期；模板默认改运行目录 config.json） | `scripts/probe_okx_fixed_proxy_node.py` + `tests/unit/test_okx_fixed_proxy_scripts.py`（30 过） |
| ≥9 个自然扫描周期 + 专用风险恢复（高水位未动） | `docs/VALIDATION_EVIDENCE.md` 2026-07-26 深夜一节 |
| Campaign 恢复 + 3 根自然 K 线 | 同上 + `records\okx_demo_campaign.json` |
| P1 后端：长桥分页/频控/卡顿保护/缺根校验/高周期接口 | `pa_agent/data/longbridge_source.py` + `tests/unit/test_longbridge_source.py` |
| P1 后端：market_calendar（XNYS/XHKG/XSHG+半日市） | `pa_agent/data/market_calendar.py` + `tests/unit/test_market_calendar.py` |
| P1 后端：市场规则块 ×4 + 路由 + 注入（用户回合）+ K 线表交易所时区 | `prompt_engineering/市场规则_*.txt`、`pa_agent/ai/market_rules.py`、`prompt_assembler.py`、`tests/unit/test_market_rules*.py` |
| 共享合同层（符号/K线合同/日历/双协议） | `D:\Desktop\Quant\shared\market_contracts`（39 测试全过，已 editable 装入 venv） |
| 文档同步 | `CONTEXT.md` 顶部、`docs/VALIDATION_EVIDENCE.md`、`docs/CLAUDE_HANDOFF_20260726.md` 横幅、PRD04 §10/§11.4 |
| 提交 `2e95a05` 前三套件回归：1874 项、0 失败、0 错误 | 2026-07-27 WO-H 收口复核；当次 3 项固定跳过，另有 1 个 AkShare 条件跳过 |

**测试基线（验收时的对照）**：
- `tests/unit` + `tests/property` + `tests/integration`：必须零失败；`2e95a05` 接手基线为 1874 项、0 失败、0 错误。
- 固定跳过基线为 3 项；4 个已登记的 AkShare 联网冒烟测试在各自行情请求真实不可达时可条件跳过，必须在 JUnit 中逐项保留具体原因，不能新增普通 skip/xfail。
- 旧 handoff 中的 unit 18 项、property 6 项和 integration 既有失败均已完成裁决，禁止继续当作豁免。
- 改动文件 Ruff `E4,E7,E9,F,I,UP,B,C4` 全绿；`compileall` 过；`git diff --check` 过。

---

## 2. 待执行工单

### WO-A 对抗审查（本阶段收口必需）

**状态**：四个视角均已执行，已确认问题中的 P0/P1 为 0。2026-07-27 完成证据复审又补上 system prompt UTF-8 字节稳定、10981 回滚期间外部进程抢占、长桥重复分页/DST/半日市/>1h 跳过、Campaign 记录丢失/非法状态/人工中断的直接回归。仍有 1 个 P2 未闭合：固定代理 `metadata.json` 与实际 `config.json` 没有共同指纹，现有脚本无法检测二者不一致；本轮明确禁止修改 `scripts`，不能用无关失败测试冒充完成。

**视角 1 交易安全/运行态**：攻击 `probe_okx_fixed_proxy_node.py` 激活流程失败路径（回滚失败、10981 被他进程接管、metadata 与实际配置不一致）；`_activate` 每个失败分支是否保证 10981 要么旧配置要么明确下线；耐久闸门可否被绕过（`--activate` 时 argparse 已强制 idle-cycles≥1，验证）；恢复命令是否可能重锚高水位；频控器/超时保护对 Worker 场景副作用（注意：Worker 不用 longbridge_source，无影响——验证这个论断）。

**视角 2 数据正确性**：`_fetch_rows` 分页去重键 `row.timestamp` 类型一致性、count 边界、无限循环风险（fresh 为空即 break，验证）；`_assert_no_intraday_gaps` 假阳性（午休/半日市/夏令时切换日；>1h 周期已跳过——验证 4h 不检查）；`latest_snapshot_for_timeframe` 与主订阅周期互扰（`_head_bar_is_forming` 已参数化——验证无残留 `self._timeframe` 误用）；`market_calendar` 的 `early_closes` 属性在 exchange_calendars 4.13.2 存在性（已被 12 个真实日历测试覆盖——复跑确认）；时区渲染对 MT5 naive-UTC 兼容（未归类符号走 UTC 旧路径——验证）。

**视角 3 提示词/缓存**：system prompt 必须字节不变（跑 `tests/unit/test_market_rules_injection.py::test_system_prompt_stays_market_free`）；前缀链模式不重复注入（`test_stage2_prefix_chain_mode_skips_duplicate_rules`）；加密规则块进现役 OKX Campaign 后与既有条款的冲突排查（重点读 `市场规则_加密.txt` vs `二元决策.txt` 的资金费率/强平表述）；K 线表表头变化对既有断言的影响（全量 unit 已零新增失败——已覆盖）；市场规则文件事实核对（A股印花税 0.05% 单边卖出、港股印花税 0.1% 双边、美股 T+1 交收 2024-05 起、科创板 200 股起 1 股递增——逐条查证 2026 年现状）。

**视角 4 文档诚实性**：`CONTEXT.md` 顶部三条新条目、`VALIDATION_EVIDENCE.md` 深夜一节的每句"已完成/全过"能否被文件/数据库/日志支撑；测试数字可复现性抽查。

**验收标准**：4 视角全部执行完；每个确认的 P0/P1 发现已修复并有回归测试；修复后受影响套件复跑全绿；结果回写本文档进度账本。

### WO-B 确认式提交推送（历史批次已完成）

**状态**：四个历史提交 `372cd26`、`aa1c91d`、`a5d2e19`、`b539cb5` 已进入当前主线。以下清单保留为审计记录，不再是当前待提交快照；当前提交仍须重新按确认式 Git 流程列出精确文件。

**历史前置**：WO-A 完成。该批次当时采用逐次确认；当前发布遵守第 0.4 节记录的长期默认授权，不再重复询问相同提交许可。

分批提交方案（PA_Agent 仓库，main，禁止 `git add .`，逐文件精确 `git add <path>`）：

- **Commit 1「前端工作台与执行链联通（Codex 日间批次 + 上午 CI/日志/凭据修复）」**：
  `.github/workflows/ci.yml`, `pa_agent/app_context.py`, `pa_agent/execution/credentials.py`, `pa_agent/execution/okx_client.py`, `pa_agent/execution/service.py`, `pa_agent/execution/worker.py`, `pa_agent/gui/main_window.py`, `pa_agent/gui/read_models.py`, `pa_agent/gui/trading_workbench.py`, `pa_agent/util/logging.py`, `scripts/provision_okx_fixed_proxy.ps1`, `tests/conftest.py`, `tests/unit/test_execution_controller.py`, `tests/unit/test_execution_service.py`, `tests/unit/test_execution_worker.py`, `tests/unit/test_main_window_source_health.py`, `tests/unit/test_okx_client.py`, `tests/unit/test_trading_workbench.py`, `tests/unit/test_workbench_read_models.py`, `tests/unit/test_okx_fixed_proxy_provision_acl.py`, `tests/integration/test_trading_workbench_worker_boundary.py`
- **Commit 2「固定代理空闲耐久闸门与换节点」**：
  `scripts/probe_okx_fixed_proxy_node.py`, `tests/unit/test_okx_fixed_proxy_scripts.py`
- **Commit 3「P1 多市场后端：长桥正式化+市场日历+市场规则块+交易所时区」**：
  `pa_agent/data/longbridge_source.py`, `pa_agent/data/market_calendar.py`, `pa_agent/data/datetime_ts.py`, `pa_agent/ai/market_rules.py`, `pa_agent/ai/prompt_assembler.py`, `prompt_engineering/市场规则_A股.txt`, `prompt_engineering/市场规则_港股.txt`, `prompt_engineering/市场规则_美股.txt`, `prompt_engineering/市场规则_加密.txt`, `pyproject.toml`, `tests/unit/test_longbridge_source.py`, `tests/unit/test_market_calendar.py`, `tests/unit/test_market_rules.py`, `tests/unit/test_market_rules_injection.py`, `tests/unit/test_datetime_ts.py`, `tests/unit/test_data_source_factory.py`, `tests/unit/test_data_source_switch_transaction.py`
- **Commit 4「文档：代理恢复证据、P1 后端、工单」**：
  `CONTEXT.md`, `docs/VALIDATION_EVIDENCE.md`, `docs/prd/04_PA_Agent_Stitch视觉重构PRD.md`, `docs/CLAUDE_HANDOFF_20260726.md`, `docs/WORKORDER_MASTER_20260727.md`

**安全检查（每个 commit 的 staged snapshot 上执行）**：
```
C:\Users\Administrator\.codex-shared\tools\gitleaks\v8.30.1\gitleaks.exe git --staged --no-banner --redact
```
零泄漏才可提交；staged 中不得出现数据库/日志/凭据/scratch/二进制大文件；staged 出现删除或重命名必须另获用户授权。

**远程核验（历史）**：`origin=git@github.com:xiaojinlucky/PA_Agent.git`；**本仓库为用户明确要求的 PUBLIC 例外**。当时的 `03c23f7`/`4282f433` 基线已经过期，仅用于还原该批次；任何新推送必须重新 fetch 并核对实时 SHA。不得误推 `upstream`，钩子失败禁止 `--no-verify`。

**验收标准**：4 个 commit 逐个通过 gitleaks；push 后 `git rev-parse origin/main` == 本地 HEAD；向用户报告最终 SHA。

### WO-C Campaign 空仓安全重启（历史验收完成）

**状态**：2026-07-27 16:31 北京时间的历史验收中，原 Campaign 从正式 `run` 入口恢复并加载 WO-C2 修复，沿用 Campaign ID、完成水位和冻结风险参数。16:20–16:30 首根新 K 线完成为 `blocked:no_order`，当时 Campaign 为 `active`；这不能替代当前状态的实时核验。

**硬门（全部满足才可动）**：活动 execution=0、PENDING/RUNNING 命令=0、未解决 UNCERTAIN=0、`NEW_RISK` 租约=0、OKX Demo 仓位=0、普通挂单=0、八类算法单=0、Worker 心跳/成功对账新鲜、两库 `integrity_check=ok`、账户身份一致。风险停止必须为 0；唯一例外是 `RECOVERABLE_TRANSIENT_RISK_STOP_REASONS` 中的临时读取停止，此时只允许 Campaign 针对新 K 线创建一次耐久的专用恢复命令，新账户/身份/账单读取完整前保持停写，禁止直接清除或重锚高水位。

**恢复方式**：同配置、旧状态未完成且当前无进程时使用正式 `python -m pa_agent.okx_demo_campaign run` 继续原 Campaign；只有需要归档旧 Campaign 并开始新 24 小时窗口时才使用 `restart`。进程已存在时先重新核验全部硬门，再按最短维护窗口处理，禁止重复启动争抢 `CampaignProcessLock`。

**验收标准**：Campaign active、继承正确、冻结参数不变、专用临时恢复有完整命令证据且高水位未重锚、首根自然 K 线完成；Worker 保持健康，券商现场继续由实时只读证据确认。

### WO-C2 Campaign 对账监控耐久化（2026-07-27 完成）

**根因**：旧 `_monitor_owned_executions()` 只要历史 `execution_ids` 非空，就先强制等待一个时间戳更晚的新对账；即使全部 execution 已是安全终态，30 秒无新时间戳也会抛 `TimeoutError`，越过 `run()` 并把 Campaign 写成 `needs_attention` 后退出。

**实现**：
- 扫描全部 owned execution；`CLOSED/BLOCKED/CANCELED/REJECTED` 和 `READY` 不等待新对账。`BLOCKED/REJECTED` 的 `needs_attention=true` 表示确定未写入或券商明确拒绝，不再误判为未知写入。
- 普通活动态才等待对账；`TimeoutError` 或 Worker 单轮 attention 会先重读耐久账本，仍为普通活动态时记录 `blocked:reconcile:*`、跳过本轮轮询并重试，不创建或重试券商写入。
- `UNKNOWN/ERROR`、非终态 `needs_attention`、记录丢失和非法状态继续硬阻断。监控错误不得覆盖本根既有 `blocked:no_order`、风险阻断等结果。
- `close_out()` 在剩余窗口内重试临时只读对账；撤单/离场命令逐条等待耐久终态，同一 execution 的同一种写动作最多一次。状态存储可写时，命令失败、结果不明、等待超时或真实不安全状态会耐久写 `needs_attention` 并退出；补写失败时磁盘可能保留 `stopping`，但 `stopping / needs_attention` 都硬阻断自动恢复，人工中断不会二次收口。

**验证**：Campaign 定向 86 项通过、0 失败；三套件 1886 项通过、0 失败。两轮独立对抗审查最终 PASS。真实恢复命令 `27613f47-f27a-44c7-ba44-5dc378e7ee4e` 完成新的账户与账单读取，`kill_active` 由白名单临时停止合法恢复为 0，高水位未重锚；首根新 K 线完成且零新增 execution/订单/仓位。

### WO-D P1 三标的真实验收（行情凭据已可用，完整验收待后续工单）

**当前证据**：旧默认档案仍会返回 `401004 token invalid`，但这不代表 Longbridge 不可用。2026-07-29 已在同一共享 `D:\Desktop\Quant\env` 找到完整的 `LONGBRIDGE_COMPREHENSIVE_*` 行情档案，并用官方 `QuoteContext` 真实取得沪深报价和 1h/4h/1d K 线。PA_Agent 通过非机密选择项显式使用该档案，禁止猜测、跨组拼接或输出凭据。当前不再要求用户为 WO-D 重签 token。

**后续执行**：`.venv\Scripts\python.exe scratch\p1_multimarket_acceptance.py`。2026-07-27 接手审计已离线修正该本机脚本的事件名、成功判定、Longbridge 来源标记和异常释放。脚本对 AAPL.US/700.HK/600519.SH 各跑一次真实两阶段分析，10m 主周期 + 1h/4h 高周期背景，记录写 `scratch/p1_acceptance_records`。本轮 P0-01 只确认行情档案可用，不把这组三标的两阶段验收冒充已完成。

**交接限制**：该验收器按原工单保存在被 Git 忽略的 `scratch/`，当前本机文件存在且可解析，但远端克隆不能仅靠仓库重建它。它不阻塞本机最终验收；若要求跨机器复现，需要另行决定受控入口位置，不能在当前“禁止修改 scripts”边界下擅自迁移。

**验收标准**：3 个符号 `record_status=complete`、无 error；人工抽查 Stage1 输出里 K 线时间为交易所当地时间、市场规则块出现在提示词（记录文件里有完整 prompt）；`acceptance_pass: true`。

### WO-E P1 前端（市场切换 + 自选 + 市场时钟）——按 PRD11 直接实现

**当前拍板**：PRD11 是新多市场页的最终实现规格，覆盖旧 Stitch、ImageGen、外部样稿和历史图片精修门。B2 无 Qt Controller 和 C 原生 PyQt6 离线实现均已通过提交级 CI；16 张离屏尺寸/状态/缩放矩阵已复核，最后只认正式快捷方式的真实桌面验收。

**页面合同真值**：`docs/prd/05_多市场看盘前端设计包.md` 已完成阶段 0/1，并冻结以下内容：`QuoteSnapshot` 精确字段与新鲜度、可测 K 线新鲜度、四市场最近标的/本地自选及旧设置迁移、selection generation、Longbridge 内 US/HK/CN 事务、Longbridge↔OKX 跨源提交/回滚/迟到结果、界面文案、脱敏输入和 M01–M17 二元验收。方向 1 的 `docs/prd/07_WO-E_方向1_多市场看盘方向绑定PRD.md` 另冻结 D01–D07，包括高密自选集合、同 generation 设置保存、只分析不执行、完整壳层尺寸、认证优先级和异步暂存提交。下列摘要只作索引，不替代两份合同：
- 主问题：当前看的是哪个市场的哪个标的、市场开没开、数据新不新鲜。
- 新增控件：市场切换（US/HK/CN/Crypto，与行情源联动路由）、多市场自选列表（本地持久化 `GeneralSettings`，不接长桥云端 watchlist 写 API）、市场时钟（US/HK/CN 使用 `market_calendar.session_state`；Crypto 使用连续市场规则与 UTC）。
- 已有基座：`_symbol_combo`、图表、设置 revision、Longbridge/OKX 已验证的只读能力可按合同复用；现有 `_switch_data_source` 只可参考事务意图，不能直接复用其提交点或同步界面线程调用。
- 验收：同一桌面窗口展示 AAPL.US、700.HK、600519.SH，并对 XAU-USDT-SWAP 完成 Crypto 正常路径与 Longbridge↔OKX 双向切换；股票无权威 tick 时必须保持“仅展示，价格分析不可用”，不得强跑两阶段分析。M01–M17 与 D01–D07 全部通过，最终桌面窗口由用户从 `.lnk` 启动并截图。

**三方向图稿、选择与 ChatGPT Web**：第一组历史候选稿保留在 `docs/prd/06_WO-E_Product_Design_三方向证据.md`，不再驱动后续设计。Product Design `ideate` 已按正式入口重跑；`docs/prd/09_WO-E_Product_Design_ideate_重跑证据.md` 登记插件版本、完整提示词、三个独立产物标识、项目 PNG、SHA-256、人工视觉复核和用户选择。用户于 2026-07-28 按本轮实际显示顺序选择 `1`，唯一绑定为 `Scan Rail Workbench`；B1、B2 已通过。应用内 ChatGPT 已真实接收四附件和完整提示词，B3 通过；完整回答、指纹与 F01–F22 本地裁决见 `docs/prd/10_WO-E_方向1_ChatGPT_Web_PRD.md`，Stitch 的真实阻塞见 PRD08。

### WO-F Claim Validation 反幻觉层（2026-07-27 完成）

**状态：已完成。** 本地实现、离线三套件、对抗审查和 OKX Demo 正式入口运行验收均已通过。

**已落地**：新增 `pa_agent/ai/claim_validation.py`，同时检查原始模型声明与归一化后的结构化对象，防止非法声明被 normalizer 洗成合法结果。Stage1 校验 `support_levels`、`resistance_levels`；Stage2 校验 `entry_price`、`stop_loss_price`、`take_profit_price`、`take_profit_price_2`。价位必须落在已收盘 OHLC 包络外扩可配置 ATR14 容差内，且必须是行情源声明的真实 `price_tick` 整数倍；缺少真实 tick 时直接拒绝，不按显示小数位猜测。`bar_range`、`new_closed_bars`、`entry_basis_bar` 及文本中的 K 序号必须存在于当前已收盘 K 线集合。

**耐久闭环**：声明校验只拒绝，不修改模型输出。带反馈重试仍失败时，分析记录耐久保存 `exception.type=claim_validation` 和稳定错误码；Campaign 写入 `blocked:claim_validation:<code>` 后继续下一根，不创建 execution 或券商写命令。Campaign 恢复只认同一 `campaign_id` 的耐久记录，重启时不会接管 ownerless 文件，也不会执行崩溃前的陈旧成功分析。

**验收结果**：unit/property/integration 1926 项通过、7 项跳过、0 失败；GUI E2E 4 项通过、0 失败。OKX Demo 正式 `run` 入口加载新代码后，两根目标 10m K 线均自然完成为 `blocked:no_order`，记录复跑声明校验为 0 issues。20:01 在完整空现场硬门后安全重载同一 Campaign；20:00–20:10 K 线于 20:11:35 完成相同结果，新文件名分钟与原始/归一化 Stage1、Stage2 四次声明复验均通过，Campaign 保持 active。

> **2026-07-27 09:20 基线更新（重要）**：`tests/unit` + `tests/property` 全量 **零失败**，
> 交接时的历史 18 项已全部裁决清零（3 个真实代码缺陷 + 其余为生产演进后测试滞后 / 夹具
> 与真实市场语义不符），过程未放宽任何生产语义。**后续验收闸门改为「全量必须零失败」**，
> 不再有"历史 18 项"豁免。已推送 `origin/main = 20773d6`（3 commit，逐 commit gitleaks 零泄漏）。
> 集成测试的历史失败已完成裁决；AkShare 联网冒烟用例在端点不可达时按已登记原因条件跳过，端点恢复时仍会真实运行。

### WO-H 时间/多周期/成交量因素接入（2026-07-27 研究裁决与执行）

**依据**：用户两个 GPT 对话（TP1/TP2+RR 批判、实盘冻结决策+因素接入建议）+ 原始资料
`E:\QQ文件\价格行为学资料\文件18-多时间周期分析.txt`（Brooks MTA：嵌套原理、HTF 设置+主周期
决策、只看 2-3 周期、反对多周期共识与低周期拼触发）。RR 1:1 上限批判已被当前代码采纳
（`max_risk_reward_ratio()` 返回 None，结构三价保持原样，仅保留 RR≥1 下限）。

**裁决**：
- 多周期：已按资料正统落地（10m 主 + 1h/4h 薄背景，长桥高周期接口本轮已补）。
  **禁止**多周期投票/共识硬闸、禁止加 1m/5m 触发周期。增量方向：HTF 关键位置语义做实
  （资料的设置优先级表）、20GB 趋势强度标记（可程序判定）。
- 时间：加密只用连续市场语义（已在加密规则块）；股票品种增量 = 分析帧加
  "session 阶段"薄标签（开盘/午盘/尾盘，由 market_calendar 判定），不做时段硬闸。
- 成交量：Brooks 体系几乎不用（资料实证），仅股票市场有补充价值。若做：
  relative_volume + expanding/contracting 轻摘要，只解释不信号不进闸门，
  **必须先影子运行**并用评测门禁（方向准确率 Wilson 下界>50%）证明增益才转正。
- 横切纪律：新因素一律先做可程序校验的薄标签并影子运行；只有通过各自转正门后才进入用户回合（保提示词缓存）。成交量当前未转正，因此仍不进提示词。

**执行状态**：
- 任务 1 股票交易时段标签与任务 2 高周期关键位置、20GB 标记已由提交 `0a0c5a8`
  推送；全量基线 1823 项、0 失败、0 错误。
- 任务 3 已完成成交量摘要、JSONL 追加记录、离线评分核心、本地命令行入口和测试。
  接手审计发现原 `/goal` 要求“每次分析额外记录”，因此又在完整 Stage1 和增量 Stage1
  构造点补上自动落盘，并为同一 JSONL 增加跨进程串行追加和中断半行恢复；测试写入
  隔离到临时目录，成交量字段仍未进入提示词。
- 当前评分只比较放量与缩量两组的后续相对振幅，属于描述性结果；摘要没有预测方向，
  因而不能计算 Wilson 方向准确率门，也不能据此批准成交量转正。
- 提交 `2e95a05` 的三套件为 1874 项、0 失败、0 错误；当前补漏批次的最终数字以进度账本
  最新一条为准。`prompt_engineering/`、执行、GUI 和脚本目录保持无差异。
- 任务 4 是 `CONTEXT.md` 一页化与多市场前端设计包：一页化归入 WO-G-3，设计包归入
  WO-E 前置阶段。2026-07-27 完成证据复审又补齐精确读模型、设置、并发、跨源、脱敏和
  Crypto 验收合同；这仍不是 WO-E 生产实现。

### WO-G 遗留专项（优先级低）

1. 历史 18 个 unit 失败逐项裁决（**已完成，全部清零**）：
   - **已修（6 项，UI 迁移）**：`test_decision_panel.py` 的预测组 6 个测试——预测 UI 已整体迁至
     `FutureTrendPanel`（`pa_agent/gui/future_trend_panel.py`），旧测试断言的 `_prediction_group`
     等成员已不存在。裁决：迁移测试而非恢复旧控件，新建 `tests/unit/test_future_trend_panel.py`
     覆盖同样六条渲染契约（隐藏/灰/绿/红/黄/clear），旧文件删除对应用例并留迁移注释；
     断言文案按现行格式（"阳线的概率为70%"）更新。
   - **已修（5 项，decision_continuity）**：
     `test_bars_elapsed_between_parses_iso_t_separator` 是**真时区不一致**——测试助手 `_ms`
     把本地墙钟 ISO 当 UTC 构造，而生产 `_parse_local_iso_ms` 按本机时区解析（生产自洽正确：
     `timestamp_local_iso` 是本机墙钟 naive、`snapshot_ts_local_ms` 是真 epoch）；修助手用
     naive `.timestamp()`，任意时区环境都成立。其余 4 项是**夹具与真实市场语义不符**：
     ①`test_audit_relation_flip_label` 的 prev 记录时间比帧快照早 8 天，必然先触发挂单超时
     自动取消（标签变已失效）→ 记录时间贴近快照；②三个 auto-cancel 用例把"高于市价的买入
     限价"当作未触发，但买入限价高于市价按真实市场语义会立即成交（生产 `low <= entry` 判定
     正确）→ 入场价改到帧最低价之下。**均未放宽生产语义**。
   - **已修（7 项，2026-07-27 08:5x 完成，历史 18 项全部清零）**：
     ①`test_validation_lenient_fixes` OpenClaw 枚举 ×2 —— `_normalize_stage2_enum_aliases`
     漏了 entry_bar 的 strength/freshness 滑写（pending / limit_order_pending），该函数
     本就是枚举滑写统一入口，补上调用（与主流程幂等重复无害）。
     ②`test_order_opportunity` 音效 —— 夹具 FakeWinsound 缺 `SND_ASYNC`，属性异常被吞后
     误跌到 MessageBeep 兜底；补常量（生产用异步播放避免阻塞 GUI 线程，正确）。
     ③`test_provider_override_by_model` OpenClaw CS —— Cursor 路由已从 QClaw 网关改为
     **SDK 直连**（不用 base_url、要 `crsr_` key），旧测试仍断言网关行为；按现行语义重写为
     3 个测试（清空 base_url 保留用户 key / 缺 key 明确报错 / 子模型原样保留）。
     ④`test_coherence_checks` 结构型矛盾 —— 生产逻辑退化：只剩多空对立判定，
     `_COMPATIBLE_PAIRS`（outside_* 与 trend_* 兼容）已成死代码。结构型 inside/outside 是
     客观几何且提示词明示优先级高于 doji/trend，**恢复**结构型矛盾判定（仅 strict 模式，
     生产默认关闭，不影响现役 Campaign）。
     ⑤`test_decision_nodes_judges` §9.0 —— 真因同⑥（夹具缺 TP2）。**过程记录（诚实性）**：
     曾一度只读 `_fix_background_limit_trace`（只插 §9.0P、不改 §9.0）就断定"生产改为保持
     §9.0=否"，据此把断言改成 `== 否` —— **该结论错误**：§9.x 实际由 `DecisionNodeEngine`
     按真实 K 线几何程序回填，会覆盖模型的 §9.0=否。全量回归当场暴露该误判，已改回原断言
     `== 是` 并补充 §9.0P 断言。教训：判断"生产语义是否变更"必须追完所有写入方，
     单看一个函数就下结论会把自己的误读写成契约。
     ⑥`test_price_tick` + ⑤的夹具 —— 均缺 `take_profit_price_2`（TP2 现为下单必填），
     决策先被正确降级为不下单，导致测不到目标行为；补齐夹具字段。
2. 集成 4 个既有失败已逐项裁决：`next_bar_prediction` ×3 是功能默认关闭而测试未显式
   开启，以及耐久写入方法名演进，修复证据在 `c5f71f9`；`no_order_with_prices` ×1 是旧
   normalizer 静默清空提示词明示价位，WO-F 现改为保留原始声明并交给 schema/claim
   validation 拒绝矛盾，修复证据在 `c0b58d0`。最新三套件均为零失败。
3. `CONTEXT.md` 一页化：**已完成**。当前文件 40 行；旧流水账完整归档到
   `docs/archive/CONTEXT_full_history_through_20260727_wo_h.md`。
4. `D:\Desktop\Quant\shared` 尚无 Git 版本管理：需用户拍板是否建仓（禁止擅自 git init）。
5. 全仓 Ruff 历史债务：`4963` 是旧历史数字，已不能当当前精确基线。2026-07-27 完成证据
   复审用 `ruff check . --select E4,E7,E9,F,I,UP,B,C4 --output-format json --no-cache`
   只读扫描，当前完整仓库为 293 项诊断，其中 249 项带自动修复建议；没有修改文件。
   该专项不在常规工单批量改，尤其禁止直接全仓 `ruff --fix`。

---

## 3. 审查方案模板（所有后续工单通用）

1. **实现者自查**：改动文件 Ruff（`--select E4,E7,E9,F,I,UP,B,C4`）+ `compileall` + 定向 pytest 全绿。
2. **对抗审查**：按 WO-A 的 4 视角模板（交易安全/数据正确性/提示词缓存/文档诚实）逐视角列发现；每个发现用"默认怀疑、能实证才算"的标准二次验证。
3. **回归闸门**：全量 `tests/unit` + `tests/property` + `tests/integration` 必须零失败；数量不得低于 1874。固定跳过不得高于 3；4 个已登记的 AkShare 联网冒烟用例可在各自端点真实不可达时条件跳过，必须逐项保留具体原因。其他数量变化必须有书面解释。
4. **运行态闸门**：任何触碰执行链/提示词的改动，交付前核验 Campaign/Worker/风险态三件套 + `pa_agent.log` 无新 ERROR 类别。
5. **文档闸门**：CONTEXT.md 顶部与本文档进度账本同步更新；主张必须可被证据文件支撑。

## 4. 验收标准总表（快速核对）

| 工单 | 硬验收 |
|---|---|
| WO-A | 4 视角完成，P0/P1 修复+回归，账本回写；当前剩余 metadata/config 共同指纹 P2，未闭单 |
| WO-B | 历史 4 commit 逐个 gitleaks 零泄漏，push 后远端 SHA==本地；2026-07-27 起按用户长期授权执行精确暂存、安全检查和直接发布，无需逐批重复确认 |
| WO-C | 历史硬门全过后重启，继承正确，≥1 根自然 K 线，无新 ERROR；当前状态另行实时核验 |
| WO-C2 | 安全终态不误等新对账；临时对账错误耐久记录且不重试写命令；UNKNOWN/ERROR 等继续硬阻断；异常收口零重复写 |
| WO-D | 3 标的 complete、时区/规则块人工抽查过、acceptance_pass=true |
| WO-E | 设计流程全走完+用户审美确认，零副标题，真实读写链接通；三标的桌面验收及 Crypto/跨源/失败/竞态矩阵通过 |
| WO-F | Stage1/Stage2 受管价位、真实 K 线引用和行情源 tick 硬校验；稳定 `blocked:claim_validation:<code>` 耐久化并继续下一根；三套件零失败；OKX Demo 正式入口加载与自然样本无误杀验收通过 |
| WO-G-1/2 | 历史 unit/property/integration 失败逐项裁决并清零，不保留“历史失败”豁免 |
| WO-G-3 | `CONTEXT.md` 保持一页，历史流水账完整归档且可还原 |
| WO-G-4 | 用户明确决定 `shared/` 是否独立建仓；未决定前不得 `git init` |
| WO-G-5 | 记录可复现的全仓 Ruff 当前基线；修复作为独立专项，不在常规工单批量 `--fix` |
| WO-H-1 | 股票分析帧有可程序验证的 Session 薄标签，不作交易硬闸 |
| WO-H-2 | 1h/4h 关键位置与 20GB 趋势极强标记完成，不做多周期投票或低周期触发 |
| WO-H-3 | 每次完整或增量分析自动落成交量影子摘要，并可离线描述性评分；不进提示词、不生成信号、不宣称转正 |
| WO-H-4 | `CONTEXT.md` 一页化与多市场设计包完成；不把前置设计冒充 WO-E 生产实现 |

## 5. 进度账本（每个重要步骤后回写）

- [x] 2026-07-26 22:52 固定代理切换桔子云 OKX 日本 1，激活闸门全过
- [x] 2026-07-26 23:09 专用风险恢复成功，高水位未动
- [x] 2026-07-26 23:32 Campaign 3 根自然 K 线完成（阻断/no_order/阻断，0 失败）
- [x] 2026-07-27 00:10 P1 后端全部落地，最终全量 unit=历史 18 失败零新增
- [x] 2026-07-27 00:20 文档同步（CONTEXT/VALIDATION/handoff 横幅/PRD04）
- [x] 2026-07-27 07:15 WO-A 对抗审查（Claude 主循环顺序执行，子 Agent 因额度未用；时间为北京时间，此前误记 00:40）：
  ①交易安全——激活失败回滚已被 22:26 真实失败现场实证（旧配置恢复、守护拉起、无残留）；耐久闸门绕过被 argparse（--activate 强制 idle-cycles≥1）+ 测试双重封死；恢复命令未重锚高水位（DB + 独立只读审计双向核验）；频控器/SDK executor 改动不进交易链（execution/ 仅 longbridge_adapter.py 引用 normalize_longbridge_symbol 纯函数，本轮未改，且 OKX 生产链不经过它）。
  ②数据正确性——分页终止条件（fresh 空即 break）与去重在案并有测试；缺根校验对 >1h 周期显式跳过；DST 切换周末推演（美股 spring-forward/fall-back 两向）枚举槽均落在非交易分钟，无假阳性；港股半日市下午间隔被 xcals session_close=12:00 判定为合法；latest_snapshot_for_timeframe 不改主订阅周期（测试覆盖）。
  ③提示词/缓存——system 字节不变与前缀链去重均有测试（test_market_rules_injection 7 项）；规则块事实核对通过（A股印花税 0.05% 单边卖出、港股 0.1% 双边、美股 T+1 交收 2024-05 起、科创板 200 股起 1 股递增、LULD/三级熔断）；加密块"资金费不是交易信号"与既有 lessons 条款同语义无冲突。
  ④文档诚实性——CONTEXT/VALIDATION 新增主张逐条对应本轮实测数字与命令 ID。
  结论：无 P0/P1 实锤；全量 unit 复核 = 历史 18 失败零新增（最终权威一轮）。
- [x] 2026-07-27 07:25 WO-B 提交推送完成（用户已确认"是，提交并推送"；此前误记 00:55）：4 个模块化 commit
  `372cd26`（前端工作台与执行链联通，21 文件）→ `aa1c91d`（代理耐久闸门，2 文件）→
  `a5d2e19`（P1 多市场后端，17 文件）→ `b539cb5`（文档+总控工单，5 文件）；
  每个 commit staged gitleaks 零泄漏；push 后 origin/main == 本地 HEAD == `b539cb5`；
  工作区已净（仅剩 scratch/ 等忽略项）。
- [x] 2026-07-27 07:30 前后 8 小时无人值守运行证据（23:10–07:25）：Campaign `ab245e48`
  完成 **50 根自然 K 线、0 失败**，一根不落；北京 05:31 自然信号真实提交
  execution `ed7711be-93f7-52eb-a544-adf9969f19bb`（submit 命令 `9dc7eafb` succeeded，
  真实 OKX Demo 限价单），270 秒未成交后按规则正常撤销（canceled，未切市价）——
  完整自然交易生命周期实证。当时同时发现 P2：该笔交易的 NEW_RISK 租约 `4cf57283`
  由 Campaign 的 Controller 在交易终态后仍滚动续期约 2 小时；根因与修复见本账本后续
  “NEW_RISK 最小权限收口”条目。
- [x] 2026-07-27 07:43 WO-C 完成：硬门核验后停旧树、租约自然过期、07:33 restart 语义重启，
  新 Campaign `6cba8d3e` 继承 `last_completed_bar_ms` 正确、首根自然 K 线完成（no_order，0 失败）；
  **新代码生效实证**：最新耐久分析记录（records/pending/2026-07-27_07-07-36_...json）包含
  `市场制度规则块 · 加密货币`、`时间（UTC）` 表头与资金费率条款。
- [x] 2026-07-27 08:25 WO-F 第一批严格化与 entry/stop grounding 落地（Claude 主循环）：
  ①`ValidationSettings.disable_truncation_repair` 默认 False→True（截断输出默认按语法失败
  走带反馈重试；宽松修复只能产出 gate_result=unknown）；
  ②`trace_normalize._repair_gate_result` 增加 AUTO 桩守卫——截断注入的 unknown 绝不再被
  洗成 proceed（伪造闸门通过链路铲除）；
  ③`stage2_normalizer` 不下单分支停止清空 entry/tp/tp2/sl/direction 五个提示词明示字段
  （结构化矛盾主张交给 schema 拒绝 category c + 重试；未告知的 basis/win_rate 仍规范化；
  标量 wait 决策构造属程序字典、显式置空合法）；
  ④删除 `_coerce_breakout_without_basis`——突破单缺挂单依据不再静默降级限价单，
  schema 突破单分支拒绝并在 missing_fields 指明；
  ⑤next_bar argmax 平局改确定性规范序首胜（R3.3 幂等可审计）；
  ⑥新增 `validate_price_grounding`：entry/stop 必须落在已收盘 K 线 OHLC 包络
  ±max(ATR14, 3×tick) 内（反幻觉核心；TP 允许投射到区间外、由 RR/几何经 entry 传递锚定），
  接线进 `validate_order_trade_metrics`，4 项新测试。
  **结果**：property 6 个历史失败 → 0（54/54 全绿）；集成既有失败 8→7（no_order 测试转绿）；
  全量 unit 基线保持历史 18 项零新增（openclaw 枚举 2 项列入 WO-G 裁决）。
  测试夹具修订：5 处 trace_normalize + 1 处 lenient_fixes 补 take_profit_price_2=None
  （夹具意图本就是全空，此前靠清洗兜底）；突破单降级测试改写为不降级断言。
  **注意**：新校验随下次 Campaign 空仓安全重启生效（当前 6cba8d3e 进程仍为 07:33 代码）。
- [x] 2026-07-27 08:40 CI 时区失败修复（AlphaMaster 会话跨会话转交定位，run 30225188125）：
  `trading_workbench._local_time` 从取本机时区改为显式固定 Asia/Shanghai（产品验收语义
  "UTC 运行时间转换为北京时间"，方案 1），测试补 -04:00 与无时区两个断言；UTC 环境模拟
  （TZ=UTC）输出 03:21:54 证明 CI 将转绿。同文件扫描：其余三处 astimezone() 均为
  "本机墙钟显示"语义（GUI 启动/代码加载/刷新时刻），无固定偏移断言，保留。
  批次门禁：受影响套件全绿；全量 unit+property = 历史 18 项零新增；compileall/diff-check 过；
  范围 Ruff 有 16 项**既有**历史债（settings 旧签名引号 UP037×11、C420×1、旧 import 排序
  I001×4，均非本轮新增行），按"不顺手清理"纪律保留并如实记录。
- [x] 2026-07-27 14:11 WO-H 任务 3、4 第一轮收口：成交量影子摘要、JSONL 记录、严格本地
  后验评分核心和命令行入口完成，但当时尚未接自动调用链；`CONTEXT.md` 一页化并
  完整归档，多市场前端页面合同和三个方向完成。两轮多 Agent 对抗审查发现均已修复；
  三文件完整 Ruff、compileall、diff-check 通过，定向 51 项和三套件 1874 项均零失败。
- [x] 2026-07-27 15:10 完成工单逐项复审与 WO-H-3 补漏：完整和增量 Stage1 均自动把
  成交量摘要写入 `scratch/volume_shadow/`，测试写入隔离到临时目录，提示词没有成交量
  字段；WO-D 本机验收脚本的成功判定、Longbridge 来源标记和资源释放已离线修正。三路
  独立静态审查无 P0/P1，确认的 5 个 P2 已全部修正。三套件 1880 项、0 失败、0 错误、
  7 项跳过（3 固定 + 4 个 AkShare 条件跳过）；WO-F 因原规格未完整落地改回“部分完成”。
- [ ] WO-D：`LONGBRIDGE_COMPREHENSIVE` 行情档案已真实可用；AAPL.US、700.HK、600519.SH 三标的两阶段验收尚未执行，留给后续工单
- [x] WO-F：全部受管价位、K 线引用、真实 tick、稳定错误码和 Campaign 继续下一根语义完成；离线回归、对抗审查与 OKX Demo 正式入口运行验收通过
- [x] WO-G-1、WO-G-2、WO-G-3：历史测试失败裁决完成，`CONTEXT.md` 一页化完成
- [ ] WO-G-4：`D:\Desktop\Quant\shared` 是否建立独立 Git 仓库仍等用户决定
- [x] WO-G-5：全仓 Ruff 当前基线 293 项、其中 249 项带修复建议已记录；历史债修复仍是独立专项
- [x] WO-H 任务 1、2：提交 `0a0c5a8` 已推送
- [x] WO-H 任务 3：成交量影子摘要、自动分析落盘、离线评分脚本和测试完成；未进提示词
- [x] WO-H 任务 4：`CONTEXT.md` 一页化和多市场前端设计包完成
- [x] WO-E 前置设计合同：`docs/prd/05_多市场看盘前端设计包.md` 已冻结读模型、设置、并发、跨源、脱敏和四市场验收矩阵
- [x] WO-E Product Design 阶段 2：`ideate` 正式入口、三个独立方向、完整提示词、产物标识、文件指纹、人工视觉复核和用户按本轮实际显示顺序选择 `1` 均已登记；B1、B2 通过
- [x] WO-E ChatGPT Web 阶段 3：四个脱敏附件与完整提示词已真实提交；会话 URL、完整回答、内容指纹和 F01–F22 本地裁决已登记，B3 通过
- [x] 2026-07-28 WO-E Chrome 官方恢复、附件与参考规则核查：用户授权后打开新的 Profile 1 窗口，扩展单次重试恢复，登录态 Stitch 已进入 `Web` 模式；五个附件存在且指纹核对通过，用户已确认附件显示。`D:\Desktop\Quant\前端设计` 的 Longbridge-first 结构、令牌、密度和负面约束已进入 PRD08 冻结提示词；用户已手动提交冻结提示词，本机 Chrome 标签页元数据确认 Stitch 项目 `8039115259020616674` 已创建（`https://stitch.withgoogle.com/projects/8039115259020616674`，标题 `Stitch - Projects`）。用户已直接审美否决该结果，Stitch 稿作废且不进入实现。官方合同确认 Longbridge 批量报价单次最多 500 个标的；OKX 自选可按 SPOT/SWAP 最多两个串行 ticker 快照。10m 最坏 602 根 5m 需要最多三页，现有客户端缺 `after`；正确修复需极窄解除 `pa_agent/execution/okx_client.py` 禁区，未用削减窗口或私有调用绕过
- [x] WO-E 原生 PyQt6 离线实现：`91715ee2c8af0e6e829031b5a4f375d18f71cf52`
  已发布且 CI 全绿；16 张离屏状态/尺寸/缩放图逐张复核通过
- [ ] WO-E 真实桌面验收：从正式快捷方式完成 Longbridge 三市场、Crypto、快速切换与缩放矩阵
- [x] 2026-07-27 16:33 WO-C2：Campaign 对账监控耐久化完成；三套件 1886 项通过、
  0 失败，两轮对抗审查 PASS。原 Campaign 从正式 `run` 入口恢复，白名单临时风险停止
  经专用命令合法解除且高水位未重锚，首根新 10m K 线完成为 `blocked:no_order`。
- [x] 2026-07-27 20:01 WO-F 完整闭环：Stage1/Stage2 全部受管价位、真实 K 线引用和
  行情源声明 tick 硬校验完成；最终失败耐久写 `blocked:claim_validation:<code>` 并继续
  下一根，零 execution/券商写命令。unit/property/integration 1926 项通过、7 项跳过、
  0 失败，GUI E2E 4 项通过、0 失败；两轮代码审查及最后增量复审无 P0/P1。OKX Demo
  两根目标 10m K 线自然完成为 `blocked:no_order`，复跑声明校验 0 issues；随后通过空现场
  硬门安全重载同一 Campaign。重载后 20:00–20:10 K 线于 20:11:35 完成 `blocked:no_order`；
  新记录文件名分钟和四次声明复验均通过，账户身份、高水位和冻结风险参数未改变。
- [x] 2026-07-27 20:20 WO-F 发布：49 个精确文件以 `c0b58d0` 推送到 PUBLIC
  `origin/main`；staged gitleaks、文件类型/大小/gitlink/忽略项检查通过，本地 HEAD、
  远端跟踪分支和 GitHub main SHA 完全一致，发布后工作区干净。
- [x] 2026-07-27 完成证据复审：补 system prompt 字节稳定、10981 外部进程抢占、
  Longbridge 重复分页/DST/半日市/>1h 跳过、Campaign 丢失/非法状态，以及撤单/中断异常
  耐久收口与零重复写直接回归；四文件定向 220 项通过、0 失败，三套件 1956 项通过、7 项跳过、0 失败。全仓 Ruff
  只读基线刷新为 293 项，其中 249 项带修复建议。WO-E 阶段 0/1 合同补齐 Crypto 与
  跨源/失败/竞态矩阵。
- [x] 2026-07-28 NEW_RISK 最小权限收口：根因是 Campaign 在新增风险 Worker 命令耐久
  终态后没有 `disarm()`，Controller 监控线程因此继续滚动续租。最小修复只改
  `pa_agent/okx_demo_campaign.py` 和测试，不改 `pa_agent/execution`：动态杠杆、正常
  submit、READY 恢复 submit 在命令创建前失败时释放；命令创建成功后，只有等待函数返回
  `SUCCEEDED`、`FAILED` 或 `UNCERTAIN` 耐久终态才释放。等待超时、命令读取异常或非终态
  结果不由业务方法提前释放，进程收口仍会显式撤销租约；RUNNING 命令保持未解决并阻止
  再次授权。下一条新增风险命令重新执行 OKX Demo 私有只读预检并申请新租约，减险命令
  不受影响。定向回归 130 项通过、0 失败；unit/property/integration 1966 项通过、7 项
  跳过、0 失败。
- [x] NEW_RISK 公共层一次性约束：schema v5 已把租约耐久绑定到唯一
  `command_id`；`enqueue()` 在同一个 `BEGIN IMMEDIATE` 事务内完成绑定与插入，
  非空 NEW_RISK 租约另有数据库部分唯一索引。Controller 消费后不再显示可授权，
  Worker 按命令、路由、申请者和配置指纹复核唯一消费者；v4 历史库若已有同租约多条
  新增风险命令则原样保留并失败关闭。线程、进程、崩溃、回滚、过期、UNCERTAIN、
  跨动作复用、Controller 提交/续租与终态续租竞态、真正写入前授权撤销，以及重复消费者/身份
  不一致迁移均已回归；相关七文件 269 项通过，最终复审无 P0/P1/P2。用户已明确授权把
  确定性主门与真实数据源健康检查分开，所有测试仍分别运行。本地确定性主门 2048 项
  通过、0 失败；live 检查 7 项跳过、0 失败，并单独保存 JUnit 与运行状态。
  `LONGBRIDGE_COMPREHENSIVE` 已用官方只读接口真实取得沪深报价和 1h/4h/1d K 线。
  实现提交 `c932e0113e9c4e33771d1cc5afc1f16beda46421` 已推送到 `origin/main`；
  GitHub Actions run `30447988360` 全绿。远端确定性 JUnit 共 2048 项，其中 2047 项
  通过、0 失败、0 错误，1 项因 CI 主机为 UTC 按既有条件跳过；证据包同时保存完整
  Git SHA、Python 3.12.10、91 行 `pip freeze`、live JUnit 与 `health_status=unavailable`。
  P0 源码与提交级证据至此关闭；运行中的旧 Worker 未重载，运行态激活仍按下一项处理。
- [ ] Campaign 新代码运行态激活：2026-07-27 21:33 的历史只读审计当时没有活动
  execution、未解决写命令或有效租约；审计对象是 20:01 启动、早于 `7e6c095` 和租约修复
  的进程，确定未热加载。当前运行态以下一项 2026-07-28 实时复核为准。
- [ ] 2026-07-28 11:24–11:28 运行态复核：Worker 与心跳仍运行，两库 `quick_check` 为
  `ok`，活动 execution、pending/running 命令和有效 `NEW_RISK` 租约均为 0；OKX 私有
  `/account/balance` 持续连接拒绝并触发风险停止，Campaign 进程不存在且磁盘 `active`
  状态已过期。最近完成的 21:50–22:00 K 线为 `blocked:no_order`。Worker 自
  2026-07-26 13:32:11 启动，不可能加载其后的提交；最后本地账户快照在审计时已陈旧约
  13.35 小时；Worker 自动私有读取没有成功证据且 Agent 未另行查询，所以当前仓位、普通挂单和全部算法挂单仍未知。
  未取得私有只读硬门与用户运行态授权前不恢复
  Campaign、不重载。
- [ ] WO-A 复审余项：固定代理 metadata 与实际配置缺共同指纹；当前禁止修改 `scripts`，
  因此不能闭单。
- [x] 2026-07-29 WO-E 后端与无界面连接层：新增不可变页面合同、generation/请求族序号
  门禁、100 项批量自选、K 线证据和独立只分析结果投影；Longbridge 复用单一
  QuoteContext 批量 quote，OKX 按 SPOT/SWAP 批量 ticker，并用最多三页 5m 覆盖默认
  10m 分析与 55 根指标预热。全仓 2030 项通过、3 项跳过、0 失败；未修改 GUI、未调用
  execution service 或券商写接口。
- [x] 2026-07-29 WO-E 外部设计移交：PRD11 成为唯一版前端设计合同。旧 Stitch、
  ChatGPT Images 和浏览器自动化路线降为历史，不再是开发门。
- [x] WO-E 视觉生产实现：B2 无 Qt Controller 后的原生 PyQt6 页面已由
  `91715ee2c8af0e6e829031b5a4f375d18f71cf52` 发布，GitHub Actions run
  `30492305516` 全绿；16 张离屏状态/尺寸/缩放图和对抗审查均通过。
- [ ] WO-E 真实桌面矩阵：使用已验证的 Longbridge 行情档案完成 AAPL.US、700.HK、
  600519.SH 与 Crypto 的正式快捷方式验收；无权威 tick 的股票只展示、不运行价格分析。

## 6. 用户侧待办（只有用户能做）

1. **密钥轮换（长期提醒，未完成）**：Codex 会话日志（`C:\Users\Administrator\.codex\sessions\`）曾明文泄漏 OKX/长桥/模型密钥与交易密码；OKX 与长桥后台轮换只能由用户本人操作，轮换后记得同步仓库外的 `D:\Desktop\Quant\env`。当前 `LONGBRIDGE_COMPREHENSIVE` 行情档案已真实可用，不要求为本工单重签。
2. WO-E 真实桌面终验：生产实现只认 PRD11，不等待外部样稿。Agent 先完成 PyQt6 与离屏矩阵；用户最后从 `D:\Desktop\PA_Agent.lnk` 启动并完成真实 Longbridge/Crypto 快速切换与缩放验收。
3. `shared/` 是否建 Git 仓库的决定。
4. 是否解除本轮 `scripts` 禁止修改边界，以便给固定代理 metadata/config 增加共同指纹并完成 WO-A 最后一项 P2。
