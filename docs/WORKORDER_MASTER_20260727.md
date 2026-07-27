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
| 风险高水位 `adjusted_high_water_usd` | `78303.57015174496`（任何"恢复"都不得重锚） |
| OKX Demo 账户身份摘要 | `ba9b744dc78ae3fc203980e62b854b0a0e3d44c9c6d5e446de910bea74ef1def` |
| 历史 UNCERTAIN 命令 `686b6d0e-5c85-4430-a2d9-b9e069b76934` | 已处置 `confirmed_not_written_schema_validation`，禁止重放 |
| 固定风险参数 | `20000 USDT 上限 / 10% / 20x / risk_budget` |

### 0.4 行为红线

- 代理失败必须闭锁新增风险（fail-closed）；**永不**回退直连、系统代理或用户主 v2rayN(10808)。OKX 流量只走 `127.0.0.1:10981`。
- 每根已收盘 10m K 线必须真实两阶段分析；停机期间的中间 K 线**不补跑、不冒充**。
- Campaign 重启必须用 `restart` 语义继承 `last_completed_bar_ms`。
- 禁止 `git add .` / `git add -A` / 强推 / 改写历史 / 擅自开分支或 worktree / 提交 scratch、数据库、日志、凭据。
- 禁止读取/修改/运行 `D:\Desktop\Quant\AlphaMaster`（共享层 `D:\Desktop\Quant\shared` 例外，本会话线拥有写权）。
- 桌面 GUI 由用户自己从 `D:\Desktop\PA_Agent.lnk` 启动；Agent 不操作桌面和鼠标。
- 测试必须 `QT_QPA_PLATFORM=offscreen` + 凭据隔离（conftest 已做）；pytest 用 `--basetemp` 指向临时目录 + `-p no:cacheprovider`。

### 0.5 运行态证据口径

- 本文后文记录的 PID、Campaign ID、账户和订单状态都是带时间戳的历史证据，不能当成当前事实。
- 最新已登记的只读现场见 `CONTEXT.md`：2026-07-27 北京时间约 13:00，Worker 健康、两库完整、风险停止为 0、券商侧空仓空挂单；Campaign 已于 10:30:55 因等待对账超时退出并漏过后续 K 线。
- 当前 WO-H 接手任务明确禁止操作交易运行态，因此不重启、不恢复、不补跑，也不把旧现场冒充实时状态。后续若解除边界，仍须重新执行完整只读硬门。

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

**状态**：2026-07-27 早期四个子 Agent 因额度限制全部失败，后由 Claude 主循环完成一轮顺序自查。当前接手批次已用三名独立 Agent 覆盖下面四个静态视角：交易安全/范围、数据正确性、提示词缓存和文档诚实性。没有确认 P0/P1；5 个 P2（JSONL 并发与中断、完整路径记录时点、WO-D 摘要字段、CONTEXT 行数、跳过分类）已全部修正。运行态和外部制度事实因本轮边界未重新验证。

**视角 1 交易安全/运行态**：攻击 `probe_okx_fixed_proxy_node.py` 激活流程失败路径（回滚失败、10981 被他进程接管、metadata 与实际配置不一致）；`_activate` 每个失败分支是否保证 10981 要么旧配置要么明确下线；耐久闸门可否被绕过（`--activate` 时 argparse 已强制 idle-cycles≥1，验证）；恢复命令是否可能重锚高水位；频控器/超时保护对 Worker 场景副作用（注意：Worker 不用 longbridge_source，无影响——验证这个论断）。

**视角 2 数据正确性**：`_fetch_rows` 分页去重键 `row.timestamp` 类型一致性、count 边界、无限循环风险（fresh 为空即 break，验证）；`_assert_no_intraday_gaps` 假阳性（午休/半日市/夏令时切换日；>1h 周期已跳过——验证 4h 不检查）；`latest_snapshot_for_timeframe` 与主订阅周期互扰（`_head_bar_is_forming` 已参数化——验证无残留 `self._timeframe` 误用）；`market_calendar` 的 `early_closes` 属性在 exchange_calendars 4.13.2 存在性（已被 12 个真实日历测试覆盖——复跑确认）；时区渲染对 MT5 naive-UTC 兼容（未归类符号走 UTC 旧路径——验证）。

**视角 3 提示词/缓存**：system prompt 必须字节不变（跑 `tests/unit/test_market_rules_injection.py::test_system_prompt_stays_market_free`）；前缀链模式不重复注入（`test_stage2_prefix_chain_mode_skips_duplicate_rules`）；加密规则块进现役 OKX Campaign 后与既有条款的冲突排查（重点读 `市场规则_加密.txt` vs `二元决策.txt` 的资金费率/强平表述）；K 线表表头变化对既有断言的影响（全量 unit 已零新增失败——已覆盖）；市场规则文件事实核对（A股印花税 0.05% 单边卖出、港股印花税 0.1% 双边、美股 T+1 交收 2024-05 起、科创板 200 股起 1 股递增——逐条查证 2026 年现状）。

**视角 4 文档诚实性**：`CONTEXT.md` 顶部三条新条目、`VALIDATION_EVIDENCE.md` 深夜一节的每句"已完成/全过"能否被文件/数据库/日志支撑；测试数字可复现性抽查。

**验收标准**：4 视角全部执行完；每个确认的 P0/P1 发现已修复并有回归测试；修复后受影响套件复跑全绿；结果回写本文档进度账本。

### WO-B 确认式提交推送（历史批次已完成）

**状态**：四个历史提交 `372cd26`、`aa1c91d`、`a5d2e19`、`b539cb5` 已进入当前主线。以下清单保留为审计记录，不再是当前待提交快照；当前提交仍须重新按确认式 Git 流程列出精确文件。

**前置**：WO-A 完成。**执行者注意：提交推送必须先向用户列清单并得到明确"是"，一次一问。**

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

### WO-C Campaign 空仓安全重启（历史动作完成，当前运行目标已退出）

**状态**：2026-07-27 07:43 的历史重启已完成并证明市场规则块和 UTC 表头生效；较新的 `CONTEXT.md` 记录该 Campaign 已于 10:30:55 退出。本轮禁止操作运行态，不能把历史完成写成当前 active。

**硬门（全部满足才可动）**：活动 execution=0、PENDING/RUNNING 命令=0、`NEW_RISK` 租约=0、OKX Demo 仓位=0、普通挂单=0、八类算法单=0、Worker 心跳新鲜、两库 `integrity_check=ok`、`kill_active=0`。

**步骤**：① 只读核验硬门；② 停当前 Campaign 进程（先 `taskkill /PID <pid>`，PID 用 `Get-CimInstance Win32_Process` 查 `okx_demo_campaign` 命令行）；③ `Start-Process .venv\Scripts\python.exe -ArgumentList "-m","pa_agent.okx_demo_campaign","restart" -WorkingDirectory <repo> -WindowStyle Hidden -RedirectStandardOutput logs\okx_demo_campaign_restart_<ts>_stdout.log -RedirectStandardError ...`；④ 核对新 status：继承 `last_completed_bar_ms`、冻结参数不变；⑤ 观察 ≥1 根自然 K 线完成。

**验收标准**：新 Campaign active、继承正确、首根自然 K 线完成且 `pa_agent.log` 无新 ERROR 类别；Worker 不需要重启（其代码本轮未再改）。

### WO-D P1 三标的真实验收（被长桥 token 阻塞）

**阻塞**：长桥 access token 服务端 `401004 token invalid`。**只有用户能在长桥后台重签 token 并更新 `D:\Desktop\Quant\env` 的 `LONGBRIDGE_*`/`LONGPORT_*` 三件套**。

**解除后执行**：`.venv\Scripts\python.exe scratch\p1_multimarket_acceptance.py`。2026-07-27 接手审计已离线修正该本机脚本的事件名、成功判定、Longbridge 来源标记和异常释放；仍不得在 token 无效时运行或绕过认证。脚本对 AAPL.US/700.HK/600519.SH 各跑一次真实两阶段分析，10m 主周期 + 1h/4h 高周期背景，记录写 `scratch/p1_acceptance_records`。

**验收标准**：3 个符号 `record_status=complete`、无 error；人工抽查 Stage1 输出里 K 线时间为交易所当地时间、市场规则块出现在提示词（记录文件里有完整 prompt）；`acceptance_pass: true`。

### WO-E P1 前端（市场切换 + 自选 + 市场时钟）——必须走 GUI 强制设计流程

**不可跳过的流程**（`CLAUDE.md` GUI 设计强制流程）：页面功能/字段/状态/交互合同 → Product Design 三方向 → **用户审美确认** → 网页版 ChatGPT PRD → Stitch → ≥3 轮图片精修 → `$design-taste-frontend` 审计 → 才可写 PyQt6 生产代码。零副标题门禁。

**页面合同草案（供设计流程起点）**：
- 主问题：当前看的是哪个市场的哪个标的、市场开没开、数据新不新鲜。
- 新增控件：市场切换（US/HK/CN/Crypto，与行情源联动路由）、多市场自选列表（本地持久化 `GeneralSettings`，不接长桥云端 watchlist 写 API）、状态栏市场时钟（开市/午休/闭市/半日市 + 下一变化时间，数据源 `pa_agent/data/market_calendar.py::session_state`）。
- 已有基座：`_symbol_combo`（main_window.py:653）、`_switch_data_source` 事务（:1738）、`last_symbols_by_source` 持久化、`EnhancedStatusBar`（gui/widgets/status_bar.py，未挂载）。
- 验收：同一桌面窗口对 AAPL.US、700.HK、600519.SH 各完成一次真实两阶段分析（用户从 .lnk 启动后截图）。

### WO-F Claim Validation 反幻觉层（roadmap P1 项）

**当前状态：部分完成，禁止标成整张工单完成。** 已落地截断/归一化严格化和 entry/stop 的 OHLC 包络校验，历史 property 失败也已裁决；但下面原规格中的 TP 与支撑阻力价位、K 线引用边界、真实品种 tick、稳定错误码和 `blocked:claim_validation:<code>` 后继续下一根仍未全部实现。完整接线会越过当前 WO-H 白名单并需要运行验收。

**目标**：LLM 输出的每个价位必须落在真实 OHLC 范围内、引用的 K 线特征可回溯到具体 bar，否则拒绝输出（Let it crash，不静默修正）。

**实现要点**：新模块 `pa_agent/ai/claim_validation.py`；校验点挂在 Stage1/Stage2 JSON 归一化之后、执行计划构建之前（`orchestrator/two_stage.py` 与 `ai/json_validator.py` 的衔接处）；校验项：① `entry/stop/tp` 与 `support/resistance_levels` 必须在 `[min(low)-容差, max(high)+容差]` 内（容差 = 当前 ATR14 的可配置倍数，默认 1.0×，防止合法的突破位被误杀）；② `bar_range`/`new_closed_bars` 引用的 K 序号必须 ≤ 实际根数；③ 价位精度必须符合品种 tick size。失败 → 该轮分析标记 `blocked:claim_validation:<code>` 耐久记录并继续下一根（与现有 blocked 语义一致，不让 Campaign 退出）。
**顺带**：清理 `tests/property` 6 个历史失败（它们正是 AI 输出校验语义级失败——先逐个读失败原因，判断是测试契约过期还是校验器缺陷，再决定改测试还是改校验器；不许为了绿而放宽校验）。

**验收标准**：新模块单测覆盖越界价/越界 bar 引用/合法突破位不误杀三类；property 6 项失败清零或每项有书面裁决（测试契约过期 → 更新测试并说明）；全量 unit 无新增失败。

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
- 横切纪律：新因素一律薄标签、进用户回合（保提示词缓存）、可程序校验、先影子后转正。

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
2. 集成 4 个既有失败（next_bar_prediction ×3、no_order_with_prices ×1）：同上裁决。
3. `CONTEXT.md` 一页化：**已完成**。当前文件 34 行；旧流水账完整归档到
   `docs/archive/CONTEXT_full_history_through_20260727_wo_h.md`。
4. `D:\Desktop\Quant\shared` 尚无 Git 版本管理：需用户拍板是否建仓（禁止擅自 git init）。
5. 全仓 Ruff 历史债务：`4963` 是旧历史数字，已不能当当前精确基线。2026-07-27 在明确排除
   `pa_agent/execution`、`pa_agent/gui`、`scripts` 后重扫允许范围为 421 项，其中 269 项可机械
   修复；完整仓库当前数仍未知。该专项不在常规工单批量改，尤其禁止直接全仓 `ruff --fix`。

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
| WO-A | 4 视角完成，P0/P1 修复+回归，账本回写 |
| WO-B | 4 commit 逐个 gitleaks 零泄漏，push 后远端 SHA==本地，用户确认在先 |
| WO-C | 硬门全过后重启，继承正确，≥1 根自然 K 线，无新 ERROR |
| WO-D | 3 标的 complete、时区/规则块人工抽查过、acceptance_pass=true |
| WO-E | 设计流程全走完+用户审美确认，零副标题，真实读写链接通，三标的桌面验收 |
| WO-F | 三类校验单测过，property 6 项清零或书面裁决，unit 零新增失败 |
| WO-G-3 | `CONTEXT.md` 保持一页，历史流水账完整归档且可还原 |
| WO-H-3 | 每次完整或增量分析自动落成交量影子摘要，并可离线描述性评分；不进提示词、不生成信号、不宣称转正 |

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
  完整自然交易生命周期实证。P2 观察项：该笔交易的 NEW_RISK 租约 `4cf57283`
  由 Campaign 的 Controller 在交易终态后仍滚动续期约 2 小时（不阻塞运行，
  随进程消亡；接手者可在 WO-G 里排查 Controller 租约释放时机）。
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
- [ ] WO-D：**被长桥 token 401004 阻塞，等用户重签**
- [ ] WO-F：只完成严格化、entry/stop grounding 和历史失败裁决；原规格其余部分仍待完成
- [x] WO-G-1、WO-G-2、WO-G-3：历史测试失败裁决完成，`CONTEXT.md` 一页化完成
- [x] WO-H 任务 1、2：提交 `0a0c5a8` 已推送
- [x] WO-H 任务 3：成交量影子摘要、自动分析落盘、离线评分脚本和测试完成；未进提示词
- [x] WO-E 前置设计包：`docs/prd/05_多市场看盘前端设计包.md` 已完成
- [ ] WO-E 生产实现：停在三个视觉方向待用户选择；PyQt6、Stitch 和桌面验收均未开始

## 6. 用户侧待办（只有用户能做）

1. **长桥 token 重签**（阻塞 WO-D）：长桥后台生成新 access token → 更新 `D:\Desktop\Quant\env`。
2. **密钥轮换（长期提醒，未完成）**：Codex 会话日志（`C:\Users\Administrator\.codex\sessions\`）曾明文泄漏 OKX/长桥/模型密钥与交易密码；OKX 与长桥后台轮换只能由用户本人操作。本次长桥 401004 很可能就是轮换后 env 未更新所致——轮换后记得同步 env 文件。
3. WO-E 的审美确认环节：A/B/C 三选一，当前推荐 B“证据优先”。
4. `shared/` 是否建 Git 仓库的决定。
