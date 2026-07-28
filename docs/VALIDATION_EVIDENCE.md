# PA_Agent 移交验证证据

> **历史快照（截至 2026-07-22；不代表当前状态）**：本文件记录当时的发布、测试和运行验证。当前代码与 Git 基线以根目录 `CONTEXT.md`、`docs/CODEX_HANDOFF.md` 和最新 GitHub `main` 为准；本文件中的历史 SHA、测试数字和运行态只在各自时间点成立。

## 2026-07-26 深夜 固定代理换节点恢复 + P1 多市场后端（Claude 会话）

- 根因与决策：好猫 US1/日本2（AnyTLS）在空闲后持续 `SSLEOFError`（当日日志 142 次），客户端重试无法消除；按 handoff 建议走"换协议/供应商节点 + 空闲耐久闸门"。快速出口扫描 12 个候选：桔子云 OKX 日本 1（VMess，出口 `216.38.169.153`）与 OKX 美国 1 已在 OKX API 白名单；SakuraCat Hysteria2 全部传输层失败（QUIC 不可用）；其余候选 `50110`。
- 耐久闸门固化：`scripts/probe_okx_fixed_proxy_node.py` 新增 `--idle-seconds`（45–600，必须超过 AnyTLS 30 秒空闲检查）与 `--idle-cycles`（0–10，激活强制 ≥1），候选与激活后复核都要求空闲后完整风险路线（含 1010 行账单分页）全过；紧密轮全过才进入耐久，失败明确 `skipped_tight_rounds_failed`。sing-box 模板默认改为运行目录 `config.json` 并新增 `--template-config`（用户当晚把 v2rayN 主核心切到 xray，其 `binConfigs/config.json` 已不是 sing-box 格式，这是第一次激活尝试失败的根因之一）。切换后 10981 复核只保留 1 个空闲周期，避免探针与 Worker 自然扫描并发打同一 API key 限频（第二次失败根因，回滚逻辑当场验证有效：自动恢复旧配置、守护进程拉起、无残留）。
- 激活证据（22:41–22:52）：候选 10982 紧密 3/3（每轮 balance/positions/143 instruments/account_config/1010 bills 全过）+ 空闲 90 秒 ×3/3；切换 10981 后紧密 3/3 + 空闲 90 秒 1/1，新 sing-box PID `8588`，metadata `桔子云 / OKX 日本 1`。切换后日志零 `SSLEOFError`。
- 自然扫描闸门（22:48–23:06）：只读观察器记录 `last_bill_scan_at` 连续推进 ≥9 次（约 2 分钟/次，后加速到 ~70 秒），`kill_activated_at` 冻结在 22:42:01（切换瞬间旧进程被杀的最后一次误伤），高水位与账户身份不变。
- 专用恢复（23:09）：命令 `0e525d57-f198-46d2-b13f-9df4f40c0ac6` `succeeded / clear_drawdown_stop_completed`；恢复前核验活动 execution 0、PENDING/RUNNING 0、租约 0、无未解决 UNCERTAIN、两库 `integrity_check=ok`。恢复后 `kill_active=0`，高水位保持 `78303.57015174496` 未重锚，身份摘要不变。
- Campaign 3 根自然 K 线（23:10–23:32）：`ab245e48-218a-4fac-8445-10c9893e868e` restart 语义恢复，冻结 `20000/10%/20x/risk_budget`，继承 `last_completed_bar_ms=1784989200000`（停机期间的中间 K 线不补跑、不冒充）。23:00 根：两阶段真实完成（Codex gpt-5.6-luna，42.4s/51.2s），Stage2 给出计划型做多，被杠杆容量硬闸门阻断 `blocked:risk:leverage:user_max_leverage_capacity_insufficient`（未截断、未强制成交）；23:10 根：自然 `blocked:no_order`（27.6s/31.2s）；23:20 根：再次真实信号被杠杆闸门阻断。3 完成 / 0 失败，进程持续 active。
- fail-closed 现场证据（不做人为断网实验的理由）：当日自然故障期（13:05–22:36）风险运行态全程保持 `kill_active=1`、无一次回退直连或系统代理（传输层硬绑定 10981，代码与测试双重保证）；22:42 切换瞬间的 `ConnectionResetError` 立即重新触发 kill，也证明故障→闭锁路径实时有效。以上为真实运行证据，优于合成断网实验，后者会无谓打断刚恢复的红线运行。
- 独立只读交叉审计（23:17，子 Agent）：无 CRITICAL——高水位/身份逐字符一致（身份同时经券商 `account_config` 实时重算比对）、kill 解除仅那一条授权命令、执行账本 `blocked 3 / canceled 22 / closed 11` 与基线一致零新增、券商侧全品种持仓 0/普通挂单 0/八类算法单全 0、`AlphaMaster` 未读未写。
- P1 多市场后端（同晚落地，均带测试）：共享合同层 `Quant/shared/market_contracts`（39 通过）；`LongbridgeSource` 分页/频控/卡顿保护/缺根校验/`latest_snapshot_for_timeframe`（长桥源与日历、市场规则、注入、时区相关定向套件合计 `142` 通过、`0` 失败：78+25+58 去重后按最终一次全绿运行为准）；`market_calendar`（XNYS/XHKG/XSHG+半日市）；市场规则块 ×4 按符号路由注入用户回合（system 字节不变，前缀缓存安全）；K 线表时间列按交易所时区渲染并标注。`pyproject` 新增 `exchange_calendars>=4.5`。全量 unit 早跑一次：历史 18 失败 + 2 个因 945→3000 上限变化的旧契约测试（已按新分页语义更新为全过）；最终全量数字以收口一节为准。
- 已知硬阻塞：长桥 access token 服务端 `401004 token invalid`（本地 exp 预检通过），P1 三标的真实两阶段验收（`scratch/p1_multimarket_acceptance.py`）等用户重签 token 后执行。

## 2026-07-25 实盘交易工作台 Stitch 视觉重构

- 设计输入：用户现状截图、长桥界面参考、现有 PyQt6/执行链代码、网页版 GPT 视觉审核和 UI/UX Pro Max 公开设计系统原则。最终本地 PRD 为 `docs/prd/04_PA_Agent_Stitch视觉重构PRD.md`。
- Stitch：通过用户本机已登录 Chrome 打开 `AI Price Action Terminal` 项目并成功提交本地修正后的 PRD；生成了健康空仓、配置编辑、持仓保护和风险阻断四种桌面状态。生成稿仍包含与现有一级页签重复的左导航，因此只采纳健康状态带、账户/风险右栏、配置编辑态、保护卡片和阻断态，不照搬重复导航。
- GUI：实盘交易继续使用现有一级页签；主视图区分最新 PA 决策、执行生命周期和人话事件；右栏明确分成当前运行参数与下次启动参数。新增配置未保存横幅、取消编辑、真实保护数量核对、临时风险读取故障专用重新检查，以及默认收起的仓库路径、代码加载时间、原始状态/ID/错误码和手动会话。
- 安全交互：不显示买入、卖出或加仓；“执行待确认计划”只保留在折叠技术详情；撤销未成交入场和主动离场按 execution 状态动态出现；账户快照不可信时主动离场隐藏。只有 `risk_runtime_BrokerApiError / BrokerTransportError / IncompleteRead / 50004` 可显示“重新检查账户状态”，回撤、身份变化和账本完整性停止不显示这个操作。
- 真图复核：`scratch/main_window_trading_1440x900.png`、`scratch/trading_workbench_1440x900.png`、`scratch/trading_workbench_1920x1080.png`、`scratch/trading_workbench_fixed_1440x900.png` 均由项目 PyQt6 在 `QT_QPA_PLATFORM=offscreen` 下渲染并人工打开检查；没有操作用户桌面和鼠标，没有创建 `pytest-qt-qapp` 窗口。
- 自动验证：最终交易执行、风险、Campaign、设置和 GUI 受影响范围共 `516` 通过、`0` 失败，pytest 退出码 0；工作台最终定向范围 `22` 通过、`0` 失败。目标文件 Ruff `E4/E7/E9/F/I/UP/B/C4`、`compileall`、`git diff --check` 均通过。
- 最终对抗审查修正：快照读取失败会废弃旧账户/执行数据并闭锁全部操作；陈旧 execution 在主卡片和表格明确标成“本地账本记录，待券商核对”；`realized_pnl` 明确为券商口径且未扣费用；Worker 原始失败码只进入技术详情；固定张数的损失或最低保证金超限会在编辑态即时标红。两位只读审查者的 P1/P2 发现均已用回归测试覆盖；同一审查者复核修正后全部 `PASS`，无新增 P0/P1。
- 当前运行阻断：08:55 当前公网 IP `188.253.121.195` 被 OKX 以 `50110` 拒绝。Worker 仍有心跳但需人工处理，Campaign 已停止，06:43:44 之后无新的私有账户真值。旧账本中的 `56862` 张空单和 OCO 只能视为陈旧证据；公开行情越过旧止损价只支持“可能已由服务器端 OCO 止损”的推断，不支持“已平仓”结论。白名单恢复前没有重启 Campaign、没有恢复风险、没有写券商。

## 2026-07-24 Demo-S 真实回归与运行反馈修复

- 受控记录使用真实 OKX 5m→10m 已收盘快照、真实 ATR14、实时 USDT 权益/合约规格/最大可开数和真实 SupervisorGate；仍由 `ExecutionController → ExecutionWorker → ExecutionService → OkxAdapter` 执行，未直接写券商、未启用 Live。
- execution `622b53fa-6d53-547f-ab4f-bf8ed3e2c9c6` 的入场限价单 `3770752241751908352` 为 `114344` 张，270 秒内成交 `0` 后由系统撤销并落为 `canceled`；这证明未成交时不静默切市价。
- execution `cf351b86-9f24-59b5-9814-ab203e0fbb19` 的入场单 `3770781223721459712` 全成 `111596` 张，均价 `4052.1281972472131626`；两张 OCO `3770781689937571840`、`3770782168524435456` 各保护 `55798` 张；主动离场前两张保护均确认撤销，减仓单 `3770785337595486208` 全成 `111596` 张，均价 `4049.0276640739811485`。执行账本最终 `closed`、`remaining_quantity=0`、已实现盈亏 `-292.8682232087172257`。
- 最终只读核验：XAU-USDT-SWAP 非零仓位 `0`、普通挂单 `0`、`conditional/oco/trigger/move_order_stop/iceberg/twap/chase/smart_iceberg` 八类算法挂单全部 `0`。这是当时的现场只读探针快照；本地执行账本可独立复核成交与关闭，但不把瞬时券商空单结果伪装成长期耐久事实。
- 真实反馈修复：正常 `PENDING` 命令不再被 Worker 周期对账误判为未解决写入；受控 Demo-S 反推信号价，使适配器应用一次 `0.50×ATR14` 后的最终限价落到实时可成交参考价；监督与风险数量使用最终有效限价；离场等待扩展到 330 秒。后续对抗审查进一步要求监督数量与 Worker 提交数量精确相等，提交前任何改量都作废旧监督；风险定仓使用 USDT 权益，账户总权益只用于资金流/回撤；异常撤单后若竞态部分成交会切换为主动离场，execution 创建即关联 Campaign，重启保留最后完成 K 线。
- 动态杠杆只读实证：两份自然记录在 20× 的 `maxBuy/maxSell=120000/120000`，25× 均降为 `55000/55000`，证明容量不单调；系统按硬门阻断并在最新状态中保存 `20x=120000 → 25x=55000`，没有猜测目标杠杆、没有截断风险数量。
- AI 连续性修复：Codex CLI 候选不存在、版本探测临时超时、确定不可用三类语义分离；10 秒探测超时属于临时错误，只让当前 10m K 线记录失败并继续下一根。成功探测的绝对路径在客户端内缓存，登录和真实调用仍继续验证。
- 定向验证：Campaign、Worker 并发、风险运行态、Codex 客户端和两阶段网络错误共 `121` 个测试通过、`0` 失败；项目 `.pytest_cache` 仍有既有 Windows 权限警告，但独立 `scratch` basetemp 下 pytest 退出码为 `0`。全量结果需以后续收口记录为准。

## 2026-07-24 Worker v4、风险基线与历史不确定命令上线验收

- 停机硬门：旧 Campaign 停止前，本地活动 execution、活动 Worker 命令、有效 `NEW_RISK` 租约、OKX Demo 非零仓位、普通挂单和八类算法挂单均为 0；没有撤销其他 GUI 会话租约，没有执行 Live 写入。
- 生产库迁移：停止 Campaign 与 `PAAgentExecutionWorker` 后创建 `records/backups/execution-control-v1-before-v4-20260724-135141.sqlite3`；大小 61,440 字节，SHA-256 `1516981E36A8D16411C36326987669C6AAA40AD0BC299636A46B9829E9C392BF`，不可变只读 `integrity_check=ok`，与源库计数一致。新 Worker 在唯一锁下把控制库从 schema v1 迁移到 v4，保留 27 条历史命令、18 条心跳和全部历史 resolution。
- 风险运行态：首次启动发现七天账单内有基线之前的 USDT、BTC、ETH、OKB 历史转入。修复后的首次基线只验证并保存最新账单 ID/时间边界，不把已经包含在当前总权益里的历史转账再次调整高水位；边界之后的新账单才分类。定向风险测试 15 个、Worker/Campaign/执行链回归 162 个均通过、0 失败。
- 回撤事件证据：`submit()` 被持久风险停止阻断时，execution 进入 `BLOCKED`，`risk_runtime_blocked` 事件 payload 记录 `code`、`drawdown_fraction` 和 `adjusted_high_water`；回撤 60% / 高水位 1000 的定向断言与 Worker/Controller 回归共 69 项通过、0 失败。该阻断发生在券商预检和写入前。
- 最终验证：非 Live 全量 1479 通过、31 失败；31 项与本轮开工前既有失败清单一致，均在旧预测 UI、严格/宽松校验、连续性、数据源切换、OpenClaw 和提示音范围。29 个本轮 Python 文件的 Ruff `E9/F`、`compileall pa_agent` 和 `git diff --check` 通过。
- 生产重启：首次迁移后 Worker PID `49048` 成功建立风险基线。补齐回撤阻断事件审计字段后再次安全重启，Worker PID `48324` 自 `2026-07-24T06:16:53Z` 起连续更新心跳与 `last_successful_reconcile_at`。第二次启动首轮一次 `BrokerTransportError` 按工单 fail-safe 规则把 `kill_active` 置为 1；随后 `totalEq`、账单边界和回撤重新成功读取，但停止状态没有自动恢复。用户显式授权后，Controller 命令 `729d5331-37a5-434e-81e4-52d314ef1d92` 由 Worker 执行为 `succeeded / clear_drawdown_stop_completed`，重锚权益与高水位为 `78970.87234383462`、回撤为 0、`kill_active=false`；该控制命令未创建订单。
- 历史不确定命令：命令 `686b6d0e-5c85-4430-a2d9-b9e069b76934` 关联 execution `8c0f83ab-fc6b-589e-8967-c4bd8f538015`，执行账本状态为 `canceled`，事件严格为 `plan_created → ready_expired`，无 client/broker order ID、无成交。使用当前 Demo 账户身份和券商只读证据确认非零仓位、普通挂单、八类算法挂单、活动 execution、有效租约均为 0，耐久裁决为 `confirmed_not_written_schema_validation`；未解决 uncertain 写入为 0，没有重提旧 READY。
- Campaign 恢复：Campaign `0d239206-c7bc-436c-9944-23e9433e34d5` 已按 `OKX Demo / XAU-USDT-SWAP / 10m / min_trade_confidence=20 / extreme_aggressive` 启动；10m 来源为严格 OKX 5m 成对聚合，1h/4h 仍只作薄背景，进出场均为 `limit_with_slippage / 0.50 × ATR14`。本条只记录恢复后首根已收盘 10m K 线为真实 `blocked:no_order` 的历史验收快照；之后的受控 execution、Worker 并发竞态和 Campaign 运行态以更晚证据及现场探针为准，不能继续把这里的 `execution 0` 解释为当前状态。

## 2026-07-24 USDT 账户内换币验收与余额真值修复

- 用户现场确认本次是其他资产换成 USDT/账户内转换。OKX Demo 只读证据：USDT `eq/cashBal/availBal=79041.2190279924`、`frozenBal=0`、账户 `totalEq=78976.40522916657`；最近换币账单为 `type=2/subType=1`、`instId=USDC-USDT`，时间 `2026-07-24T03:06:31.671Z`。分页读取 326 条 USDT 账单，识别 35 条内部换币；外部 `type=1/subType=11` 只有历史 `2026-07-17T08:04:08.320Z` 的 `5000 USDT`，没有把本次换币误记成外部资金流。
- 代码修复：`ExecutionService.monitor_once()` 无活动 execution 时仍刷新当前选定账户；工作台按 `okx-demo/okx-live` 读取正确快照并将超过 90 秒的快照标为 `UNKNOWN`；`OkxAdapter.account_snapshot()` 保存 `totalEq`、USDT `eq/cashBal/availBal/frozenBal` 与 OKX `uTime`；Campaign 明确 `usdt_equity` 为 10% 风险基数；提交前再次读取定仓，风险快照变化就作废旧计划。
- 离线验收：目标 6 个新增硬用例 `6/6` 通过；目标套件共 168 个用例，`168/168` 通过。第一次直接运行的断言全部通过但 pytest 收尾受 Windows 临时目录占用影响返回 1；改用项目外独立 basetemp 后退出码 0。`git diff --check` 通过；Ruff `E/F/I` 仅命中仓库既有一条超长行，未新增语法、未定义名或导入错误。
- 当前真实定仓在余额升高后按 USDT `eq` 重新读取，但给定当时紧止损输入产生的风险数量超过 OKX `maxBuy`，因此返回 `max_size_exceeded` 并硬阻断；没有静默按上限截断。生产资金流水游标、高水位和恢复接线仍未完成，不能把本轮验收描述成阶段 4 完整能力。
- 运行态验证：`PAAgentExecutionWorker` 已成功重启；Worker 心跳 PID 从 `5084` 切换为 `536`，`started_at=2026-07-24T03:40:46Z`。在活动 execution=0、有效新增风险租约=0 时，Worker 连续写入 `okx/okx-demo` 快照，现场复核到 `snapshot_id=7367`，心跳/最后成功对账持续更新且错误码为空。

## 本轮后续验证（2026-07-23）

- `WO-RISK-02` 定向套件：52 通过、0 失败。覆盖纯风险定仓、长短方向、费用/滑点、`lotSz`/`minSz`、最大可开张数、`net_mode` 硬门、缺失输入、Campaign 动态规格适配、监督门、计划构建和真实 `ExecutionController` / `ExecutionWorker` + FakeAdapter 离线链路。
- `ExecutionWorker` 与 `pa_agent/execution/` 本轮没有修改；风险结果通过现有设置快照进入 `ExecutionPlan.quantity`，没有新增券商写入者。
- 本轮没有连接真实券商、没有发送 Demo 或 Live 订单；`okx_demo_private_preflight()` 在没有 PA entry/stop 时只做账户/规格/容量/行情只读检查，明确报告风险数量需要入场和止损。
- 本轮全量 `tests/unit` 收集 1239 项：1219 通过、20 失败；20 项均位于本轮风险/监督/Campaign/执行定向范围之外，不能宣称全量绿。
- `WO-S2A-01` 定向套件：156 通过、0 失败。覆盖严格监督输出、主备同快照、确定性拒绝、原子落盘、重启复用、Campaign 门控、真实 `ExecutionController` / `ExecutionWorker` 离线 Demo 命令链，以及指定的 AI 档案、执行域和 OKX 数据源回归。
- 全量 `tests/unit`：本轮收集 1225 项，20 项失败；失败均位于本轮新增监督/Campaign 定向套件之外，仍按既有仓库失败处理，不能宣称全量绿。
- 新增代码选择性 Ruff（`E,F,I,UP,B,SIM`；忽略仓库既有长中文行和中文全角标点提示）、`compileall` 和 `git diff --check`：通过。
- 本轮没有连接真实券商、没有发送真实订单；监督离线测试使用内存 FakeAdapter，只验证生产 Controller/Worker 的命令接缝。

## 发布事实

- 用户当前 GitHub 用户名：`xiaojinlucky`。2026-07-22 已明确授权 `xiaojinlucky/PA_Agent` 保持 `PUBLIC` 并公开发布。本地 `origin` 已改为 `git@github.com:xiaojinlucky/PA_Agent.git`；登录账号为 `xiaojinlucky`，权限为 `ADMIN`。截至本文件记录时，`main`、`origin/main` 和 GitHub 实时分支基线 SHA 为 `1a04c144f810ffb486280ed8a1875ff0130bb070`；该 SHA 是历史证据，不代表当前基线。
- 分支：`main`
- 多模型与 Longbridge 只读行情功能提交：`3d9353f8579e6d661fd314ab6b9e91016d9fdd96`
- 该提交发布后已验证本地 `HEAD`、`origin/main` 与 GitHub 实时分支 SHA 完全一致。
- 本轮开发、测试、打包和审查没有调用真实订单接口，也没有发送订单。

## 变更相关自动测试

运行环境：Windows、Python 3.12.12、pytest 9.1.1、项目 `.venv`。

精确命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_ai_model_settings_dialog.py tests/unit/test_ai_provider_profiles.py tests/unit/test_client_factory.py tests/unit/test_cursor_agent_route.py tests/unit/test_cursor_sdk_client.py tests/unit/test_data_source_factory.py tests/unit/test_data_source_switch_transaction.py tests/unit/test_deepseek_client.py tests/unit/test_kv_prefix_cache.py tests/unit/test_longbridge_source.py tests/unit/test_main_window_source_health.py tests/unit/test_market_defaults.py tests/unit/test_mimo_compat.py tests/unit/test_provider_capabilities.py tests/unit/test_provider_probe.py tests/unit/test_settings_round_trip.py tests/unit/test_snapshot_closed_only_buffer.py -q --basetemp scratch/pytest-evidence-20260716142229
```

结果：232 通过、0 失败；退出码 0；用时 4.156 秒。

其中因安全钩子将两处测试占位符误判为真实密钥，已把占位符改为明显的短值 `dummy`，随后单独重跑 `test_ai_provider_profiles.py` 与 `test_provider_probe.py`：28 通过、0 失败。该 28 项是上述 232 项的子集复验，不应相加为 260 项。

## 编译检查

```powershell
.venv\Scripts\python.exe -m compileall -q pa_agent
```

结果：退出码 0；用时 0.240 秒。

## 非 live 全量测试

精确命令：

```powershell
.venv\Scripts\python.exe -m pytest -m "not live" --basetemp scratch/pytest-full-watchdog-20260716142759
```

watchdog 对 pytest 进程设置 `180000 ms` 硬超时。结果：

- watchdog 判定：`timeout`。
- 包含子进程终止与清理的总用时：181.002 秒。
- stdout 只产生 1 个 `F` 失败标记，未形成 pytest 最终汇总；stderr 为空。
- watchdog 终止 pytest 及其两个后代进程；结束后复核无 PA_Agent pytest/Python 残留进程。

因此这次运行只能证明“非 live 全量测试未在限时内完成，且完成前至少出现 1 个失败标记”。无法据此给出最终通过/失败数量，也不得宣称全量测试通过。用于本次改动验收的有效自动化证据仍是上述 17 文件的 232 通过、0 失败。

## 安全与文件包验证

- 44 文件功能提交的暂存快照由 Gitleaks 8.30.1 扫描：0 命中。
- Git pre-commit 安全钩子原样通过，没有使用 `--no-verify`。
- 安全源码 ZIP 排除 `.git`、真实 `.env`/配置/凭据、根目录日志与记录、行情/训练数据、模型、第三方反馈/打赏图和探针结果。
- `tests/unit/test_debug_widget_masks_key.py` 故意包含高熵合成假钥匙用于遮蔽测试，原文件保留；它不是实际凭据。为使历史 GPT 安全包达到 0 命中，仅从当时的安全 ZIP 副本排除。
- 最终 ZIP 名称、成员数和 SHA-256 以 PA_Agent 独立移交包内 `PACKAGE_MANIFEST.json` 与 `SHA256SUMS.txt` 为准。

## 2026-07-17 实盘生命周期工作区验收

### 状态与安全边界

- 本轮基线为 `main` 分支 `f08b9e696da638895c52ffc0a8b7f3562395766d`；以下实盘闭环仍是未提交工作区改动。
- `PA_AGENT_LIVE_TRADING_ENABLED` 与 `OKX_LIVE_ENABLED` 均保持 `false`，没有进行真实 Canary，也没有调用券商写接口。
- OKX 私有只读仍因缺少 Passphrase 阻断；Longbridge 两账户对 GLD.US 的只读最大可交易数量均为 0。两项均会在真实下单前硬阻断。

### 执行域回归

使用项目 `.venv`、独立 `--basetemp` 且关闭 pytest cache，对执行凭据、计划、账本、服务、两券商适配器、完整生命周期、严格持久化和最小交易窗口进行回归。

结果：91 通过、0 失败；退出码 0。

覆盖重点包括：不可变路由快照、UNKNOWN 停写后只读恢复、OKX 突破单子订单、现货基础币手续费净数量、成交量缺失核验、Longbridge 撤止损幂等、账户/持仓周期刷新和重启对账。

### 相邻模块与全量现状扫描

- 设置、数据源、主窗口、分析记录、严格持久化和非 live 两阶段集成的扩大扫描：70 通过、1 失败。失败位于旧的阶段二“无订单价格”校验路径，不在本轮执行域；该结果不计作全绿证据。
- `tests/unit` 全量现状扫描共收集 888 项：860 通过、28 失败。失败分布在旧决策连续性、预测面板、追问历史等非执行域；本轮没有把该仓库现状误报为全量通过。
- 独立严格审查线程复跑执行域：91 通过、0 失败；复跑相邻持久化、UI 和集成范围：43 通过、0 失败。两组都包含严格持久化测试，因此不相加宣称为 134 个互不重复用例。

### 静态与 SDK 兼容性

- `python -m compileall -q pa_agent`：通过 1、失败 0；退出码 0。
- 新增执行域和对应测试的 Ruff：通过 1、失败 0；退出码 0。仓库旧文件仍存在既有全量 lint 债务，本轮未越界格式化或重构。
- `git diff --check`：通过 1、失败 0；仅提示工作区 LF/CRLF 转换风险，没有空白错误。
- 当前安装的 Longbridge SDK 本地反射确认：`today_executions(symbol=None, order_id=None)`、`history_executions(symbol=None, start_at=None, end_at=None)` 可用，`Execution` 暴露 `order_id`、`trade_id`、`quantity`、`price` 等成交汇总所需字段。

### 对抗式审查

首轮审查提出 7 个阻塞项：活动路由漂移、停写后 UNKNOWN 无法只读恢复、OKX 突破单撤错对象、现货手续费导致保护超量、成交数量缺失时错误使用计划量、Longbridge 重复撤止损、账户/持仓/PnL 回写不完整。

主线程逐项修复并补充回归后，第二轮独立审查从需求完整性、逻辑正确性、边界情况、代码质量、测试覆盖和实际运行结果六方面复验，结论为 **PASS**：阻塞落地的问题 0，可选优化仅剩旧 Qt 全量 E2E 冒烟测试的历史性挂起问题。审查线程没有修改代码、配置、数据库或 Git 状态。

## 2026-07-17 Longbridge 模拟账户三档兼容验收

### 配置与身份隔离

- 本轮基线提交为 `769e40fdb2ecfaf6ab04ccbaa3c49a2b91097800`；模拟账户扩展随后作为 `2e43d9dee9eb2fbb49a8ee9c060f02b69ef7fbc0` 发布到 `main`。
- 共享 `D:\Desktop\Quant\env` 已配置 paper 独立凭据、paper 写开关及模拟/综合/日内三个账户 ID 绑定；密钥和账户 ID 未写入仓库。
- `PA_AGENT_PAPER_TRADING_ENABLED=true`，`PA_AGENT_LIVE_TRADING_ENABLED=false`。本地运行配置默认选择 Longbridge `paper` 和 `GLD.US`，但执行模块与自动执行均为关闭，PA 来源品种和数量保持空白。
- 凭据加载器在创建 Longbridge SDK 会话前解析 Legacy Token 的 `ac` / `aaid`：paper 只接受模拟类型，综合与日内只接受实盘类型，且三者必须分别匹配绑定账户 ID。Token 无法解析、类型错放、账户 ID 错配或缺失绑定值均直接阻断。
- 三档真实环境凭据均通过本地身份绑定检查；paper Token 的到期时间解码为 `2026-10-15T02:23:46+00:00`。

### 真实只读验证

- 使用 paper 凭据完成账户余额、持仓、盈亏摘要、`GLD.US` 报价、静态规格和官方最大可交易数量估算读取。
- paper 账户当前持仓为 0，`GLD.US` 最大可交易数量为非零；没有记录具体账户资金或私有账户 ID。
- 只调用查询和估算方法；券商写接口调用 0 次，模拟订单 0 笔，实盘订单 0 笔。

### 回归与静态检查

- 执行域 12 个测试文件：131 通过、0 失败。
- 设置、严格持久化、主窗口与两阶段集成相邻范围：44 通过、0 失败。该范围与执行域共享 3 个严格持久化文件，不相加冒充 175 个互不重复用例。
- `compileall pa_agent tests`、新增执行域定向 Ruff 与 `git diff --check`：均通过。
- 关键失败路径包括：paper/live Token 交换、错误/缺失账户 ID、不可解析 Token、三种未保存账户切换、保存后继续停用、paper 禁止回退实盘和禁止美股盘外交易。

### 对抗式审查循环

- 首轮独立严格审查结论为 **FAIL**，提出 2 个阻塞项：Token 仅按变量名前缀选择可能串账户；未保存下拉切换会造成界面账户与服务账户不一致。
- 主线程增加 Token 类型与账户 ID 双重绑定，以及覆盖所有交易配置控件的 dirty guard；未保存变更立即停用旧会话，并在保存前禁用启用、提交、撤单和离场。
- 第二轮同一独立审查线程复跑相关范围：83 通过、0 失败；六维结论为 **PASS**，阻塞落地的问题 0。审查线程未修改代码、配置、数据库或 Git，也未调用券商写接口。

## 2026-07-17 外部审核适配与执行安全最终验收

### 范围与安全边界

- 基线为 `main` / `origin/main` 的 `9e5c6ccd0b04136514bdd84b7ae55276b8d92a78`；本节对应的安全加固仍是未提交工作区改动。
- 网页版 GPT 的审核先按本机项目规则、Longbridge skill、实际 SDK/API、现有 SQLite 账本与运行配置重新核对；只采纳有代码证据且属于原始交易闭环范围的问题。
- 没有调用 Longbridge 或 OKX 写接口，没有发送模拟或真实订单。OKX 私有只读仍被缺失的 `OKX_PASSPHRASE` 阻断。
- 截至该轮验收（2026-07-17），生产 `records/execution.sqlite3` 为 schema v1、0 条 execution、0 条活动记录；当时 v1 → v2 迁移只在副本验证，未触碰生产账本。当前状态以本文后面的“2026-07-20”小节为准。

### 主线程回归

执行凭据、计划、生命周期、服务、账本、Longbridge 行情/交易适配器及 OKX 适配器/客户端共 9 个测试文件：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_execution_credentials.py tests/unit/test_execution_lifecycle.py tests/unit/test_execution_plan_builder.py tests/unit/test_execution_service.py tests/unit/test_execution_store.py tests/unit/test_longbridge_adapter.py tests/unit/test_longbridge_source.py tests/unit/test_okx_adapter.py tests/unit/test_okx_client.py -q -p no:cacheprovider --basetemp scratch/pytest_execution_round2_20260717_0644
```

结果：201 通过、0 失败。

全量单元测试：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit -q -o addopts='' -p no:cacheprovider --basetemp scratch/pytest_all_round2_20260717_0647
```

结果：931 通过、28 失败。28 项与本轮执行域无关且与修复前基线一致，集中在旧决策连续性、预测面板、追问历史、阶段二标准化等模块；本轮未修改或掩盖这些失败。

### 墨菲式重复故障验证

- 独立进程原子路由占用。
- 停用与在途写线性化。
- 入场结果未知且本地保存失败。
- 保护写成功但本地保存失败。
- 撤单成功但本地保存失败。
- Longbridge 撤止损与离场竞态。
- OKX 保护撤单未知且禁止重试。

上述 7 项连续运行 10 轮：70 通过、0 失败。

### 静态检查

- 变更 Python 文件定向 Ruff（忽略仓库既有中文标点 `RUF001` 与无效注释 `RUF100`）：通过。
- `compileall` 覆盖执行模块与对应测试：通过。
- `git diff --check`：通过；仅有 Windows LF/CRLF 提示，无空白错误。
- 验收结束时没有 PA_Agent / pytest Python 残留进程。

### 对抗式审查循环

三个独立只读审查线程分别侧重生命周期逻辑、并发与安全、测试与实际运行。首轮和第二轮合计发现 8 个阻塞项：

1. 执行阶段撤单拒绝被误判为整笔入场拒绝。
2. 旧活动记录缺少实际账户身份时仍允许读取资金。
3. 券商写成功后本地保存失败可能在同一运行实例重发。
4. Longbridge 目标币种缺失时可能错误选择余额行。
5. 终态缺少成交数量和明细时被错误当作零成交。
6. 部分成交缺价时生成虚假均价或盈亏。
7. OKX 重启后的首次私有身份读取没有先同步服务器时间。
8. 入场提交结果未知且 UNKNOWN 状态保存失败时，服务可能在停写前抛出。

主线程仅修复上述阻塞项并增加反例；可选重构均保留原方案。最终三个审查线程从需求完整性、逻辑正确性、边界情况、代码质量、测试覆盖和实际运行结果六方面均给出 **PASS**，阻塞落地的问题为 0。独立线程复跑的交易与交易控制范围为 159 通过、0 失败；审查线程没有修改源码、配置、数据库或 Git。

## 2026-07-20 断线任务链复核

- 模型上下文上限已改为账号/官方目录驱动的只读元数据；界面不允许手工输入。Thinking、推理强度和速度控件按模型能力显示。
- 账号目录真实刷新结果为 Codex 6、Kimi 12、DeepSeek 2；刷新失败保留原列表，不把离线兜底模型混入成功返回的账号目录。
- Codex 官方 CLI 已确认使用 ChatGPT 订阅登录。Codex、Kimi、DeepSeek 均通过 PA 的真实随机挑战探测并以仓库外凭据保存为当前有效档案。
- Codex 自由追问使用可恢复的官方持久线程，允许客户端在达到阈值时原生 Compact；正式两阶段分析是一次性无历史请求。线程恢复已验证，实际超长 Compact 事件尚未观察。
- 修复设置窗口在激活保存失败后自动重开，以及已测试但未激活候选被静默丢弃的问题。AI 设置定向回归 36 通过、0 失败；独立审查复验 3 通过、0 失败，阻塞项 0。
- 用户将 Codex 从 Sol 切换为 Terra 时出现的“测试通过、激活失败”已定位为设置 revision 并发冲突，不是订阅登录或 Terra 模型无效。激活保存现在会在同一进程锁与文件锁保护下检查 AI 状态、合并最新普通设置并原子写入；另一个窗口同时修改 AI 配置时仍明确拒绝。Terra 真实随机挑战四项通过，当时正式配置成功激活 `gpt-5.6-terra` 并重载为 revision 26；相关回归 59 通过、0 失败，独立六维审查结论为 PASS、阻塞项 0。用户后来把活动档案改为 `gpt-5.6-luna`；2026-07-22 当前持久配置 revision 为 61。
- XAUUSD 形成中 K 线取决于券商交易时段。周末 MetaQuotes-Demo 最后 tick 停在周五，程序不画过期伪 K 线；OKX `XAU-USDT-SWAP` 同一逻辑可识别真实形成中 K 线。形成棒相关回归 29 通过、0 失败。
- 本轮没有控制用户鼠标。Codex“检测登录 → 测试并保存 → 激活”的持久化路径已由真实 Terra 探测与直接运行时调用验证，PA 可见窗口当时也按已激活的 Terra 配置重启；用户后来改用 Luna。用户在界面内再次切换档案的纯手工点击体验仍需可见验收，不得描述成已经看见用户操作通过。

## 2026-07-20 独立交易后台与权限拆分验收

### 代码范围与安全边界

- GUI 和 OKX Demo 实验只通过 `ExecutionController` 创建计划、管理短期新增风险租约并写入持久命令；只有单实例 `ExecutionWorker` 构造 `ExecutionService`、连接券商和执行命令。
- `records/execution_control.sqlite3` 只保存命令、租约、后台心跳和最后成功对账；`records/execution.sqlite3` 仍是唯一交易真值。
- 本轮没有连接券商写接口，没有发送模拟或真实订单，也没有打开或迁移生产执行账本。

### 回归与故障注入

- 执行控制、Worker、生命周期、两券商适配器、OKX Demo 实验和实盘窗口共 13 个测试文件：258 通过、0 失败。
- 覆盖新增风险租约与 Worker/请求者/配置/账户路由绑定、首次成功对账门禁、心跳与对账陈旧、命令崩溃恢复、读命令可重试例外、GUI/实验不直连券商、账户快照失败、身份漂移、部分成交、保护、离场、持久停写和清标恢复。
- 定向 Ruff、`compileall` 和 `git diff --check`：通过；只有 Windows LF/CRLF 提示，没有空白错误。

### 对抗式审查

- 首轮只读审查发现：券商明确拒绝撤单后遗留旧 `entry_cancel_runtime_id`，重启会把确定拒绝误判为未知写入，导致以后无法再次撤单。
- 修复后，确定性拒绝会清除本次撤单意图和运行实例标记并写入 `rejected` 终态。Longbridge 与 OKX 的真实适配器配合 Fake Broker 均验证：重启后的对账只查询、不撤单、不产生 `write_unknown`。
- 同一独立审查线程复跑相关范围 204 通过、0 失败；Longbridge 与 OKX 两条完整“拒绝 → 重启 → 只读对账 → 清标 → 再次显式撤单”链均通过，查询阶段券商写调用增量为 0，最终六维结论为 **PASS**，阻塞落地的问题 0。审查线程未修改项目文件。

### 2026-07-20 生产账本与 WinSW 部署验收

- 生产 `records/execution.sqlite3` 实际已经是 schema v2，`PRAGMA quick_check=ok`；因此没有重复执行 v1 → v2 迁移。
- 操作前备份：`records/backups/execution-schema-v2-before-winsw-20260720-102721.sqlite3`；大小 1,155,072 字节；SHA-256 `cd8e4652a37b54c2c6ae52b7233307e1efae46ac27e2b395b271e56423a8e771`。创建时备份与生产库计数一致；当前复核时，备份仍是生产库的精确历史前缀：事件 1–128、账户快照 1–2288 逐行一致，11 条 execution 的 ID 与摘要一致，1 条路由占用记录一致；两库 schema SHA-256 均为 `9f827a2cc7acaba22d974bd5079b755a2917439a0f0675993e5a1fc0da4186ce`。
- 官方 WinSW 2.12.0 已安装为 Windows 服务 `PAAgentExecutionWorker`。可执行文件 SHA-256 为 `05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da`；服务为 `Running`、自动延迟启动、账户 `NT AUTHORITY\LocalService`。
- 安装验收当时，服务 XML 与共享环境均设置 `PA_AGENT_LIVE_TRADING_ENABLED=false`、`OKX_LIVE_ENABLED=false`、`PA_AGENT_PAPER_TRADING_ENABLED=false`。后续经用户授权只开启模拟门；PA Live 与 OKX Live 始终保持关闭。
- 安装验收当时，生产库有一条活动 OKX Demo `XAU-USDT-SWAP` execution，状态 `protecting/needs_attention`。该记录后续的权威查无与安全收口见本文最后的“历史 OKX Demo 安全收口”。
- Windows 服务重启后旧心跳进入 `stopping`、新 Worker ID/PID 接管；第二个 Worker 由单实例锁以退出码 2 拒绝。
- GUI 在已安装 WinSW 时只请求 Windows 启动服务；只有 `sc query` 明确返回 1060（服务不存在）才允许开发态 Python fallback。`sc.exe` 缺失、查询/启动超时、权限拒绝、未知错误和 1056 已运行均已覆盖并禁止绕过服务。
- 交易后端定向回归：235 通过、0 失败。Qt E2E 与切换集成：8 通过、0 失败。两组测试前后正式 `config/settings.json` 的 SHA-256 完全一致。
- 非 live 全量现状扫描：347 通过、20 失败后按上限停止；失败集中在旧预测面板、旧校验器和连续性预期，不属于本部署阶段，也未被误报为全绿。交易后端与 Qt 测试若放在同一 pytest 进程，Qt 无界面绘图资源销毁会触发 Windows 原生访问冲突，因此最终证据采用两个独立进程。

### 仍未完成

- 管理员权限脚本在修复隐藏 PowerShell 日志方式和 Windows PowerShell 5.1 语法兼容后，于 2026-07-20 16:00 成功执行；`pa_agent`、`records`、`logs` 中 `Authenticated Users: Modify/Write/FullControl` 文件复扫为 0。WinSW 服务重启后为 `Running`、自动启动、`NT AUTHORITY\LocalService`；GUI 保持响应，GUI 进程树下没有执行 Worker，服务 Worker 仍在独立 WinSW 进程树。因此旧文件 ACL 已完成，不再列为剩余阻塞项。
- 完整券商侧启动扫描、持续持仓/保护真值核对、Longbridge 私有推送和全局限速仍属于长期无人值守实盘的后续阻塞项。
- 历史活动 Demo execution 已于后续“历史 OKX Demo 安全收口”阶段关闭。长期无人值守的券商启动扫描、持续真值核对、Longbridge 私有推送和全局限速仍未完成；任何未来实盘写入仍需要新的明确授权。

## 2026-07-20 历史 OKX Demo 安全收口

### 保护单真值与代码修复

- 使用确定的客户算法订单号查询精确结果；收到 OKX `51603` 后，继续查询未触发列表以及 `effective`、`canceled`、`order_failed` 三个月历史。所有读取成功且均无记录，确认保护单没有创建。
- 适配器进入 `confirmed_absent` 后清除不确定写标记，但保持 `needs_attention`，禁止自动重发保护，只允许明确离场或重建保护。
- Worker 心跳状态与业务状态写入使用同一把锁，旧的 `reconciling` 心跳不能覆盖更新后的 `needs_attention`。

### 运行态收口

- 共享环境与 WinSW 服务仅开启 `PA_AGENT_PAPER_TRADING_ENABLED=true`；`PA_AGENT_LIVE_TRADING_ENABLED=false`、`OKX_LIVE_ENABLED=false`。
- WinSW 服务重启后加载最新代码，历史持久停写标记被成功的只读对账清除。
- 持久命令 `08c2e0a4-4eec-447e-8d22-f9921f95defa` 仅对 execution `cc94657a-d4b3-5ba3-b4d4-0d6fd62ae595` 请求减险离场；命令 `succeeded`，execution 为 `closed`、剩余数量 0、`needs_attention=false`。
- OKX Demo 券商侧最终只读核对：`XAU-USDT-SWAP` 持仓 0、普通挂单 0、条件单 0；USDT 权益 `4999.99488722`、可用权益 `4999.99488722`、未实现盈亏 0。
- 执行账本最终账户快照为无持仓，活动 execution 为 0；有效新增风险租约为 0。

### 回归

- 交易执行相关分组：281 通过、0 失败。
- AI 模型接入分组：237 通过、0 失败。
- Qt E2E 四文件隔离运行：4 通过、0 失败。
- 切换、网络超时和快照性质测试：11 通过、0 失败。
- 当前高负载 Windows 环境中一条完整两阶段界面分析实测约 14 秒；E2E 等待上限从 10 秒改为 30 秒，仍要求流程实际完成，未改变生产逻辑或断言。

## 2026-07-22 分页、后台重载与当前账户复验

### OKX 保护单分页

- 精确查询 `51603` 后，未触发列表和 `effective`、`canceled`、`order_failed` 历史均按官方单页上限 100 完整翻页。
- 第二页命中、超过 100 条后确认不存在、第二页 API 失败、重复游标四类用例均已覆盖。API 或游标异常只会保持状态未知，不会进入 `confirmed_absent`，也不会自动补发保护单。
- OKX 客户端与适配器定向回归：62 通过、0 失败。交易执行、券商、数据源与交易窗口范围：374 通过、0 失败。

### Worker 运行态

- 在活动 execution 0、风险租约 0、待执行/执行中命令 0、Live 双硬门关闭的前提下重启 WinSW 服务。
- 新 Worker 的 `started_at` 晚于 `worker.py` 修改时间；心跳状态为 `running`，`last_seen_at` 与 `last_successful_reconcile_at` 持续更新，错误码为空。
- 服务重启前后持久命令计数保持 `succeeded=4`、`failed=1`，没有新增交易命令。

### OKX Demo 当前账户真值

- 券商只读查询发现一笔外部 `XAU-USDT-SWAP` 10 张净多仓，2026-07-21 13:46（北京时间）成交，客户订单号为空。PA 本实验固定 1 张，且本地活动 execution 与新命令均为 0，因此它不属于 PA 本次运行。
- 普通挂单 0、算法挂单 0。PA 不接管该外部仓；适配器预检在发现同品种既有仓位时直接拒绝，并新增回归测试证明不会调用下单接口。

### 模型与全仓现状

- Codex 订阅、Kimi、DeepSeek、档案保存/激活、模型目录与能力范围：247 通过、0 失败；Qt E2E：4 通过、0 失败；风险指标语义：51 通过、0 失败。
- 全仓现状：1251 通过、32 失败、3 跳过。4 项为 AkShare 真实联网超时；27 项由两个独立只读审查者确认属于 HEAD 既有测试契约或旧测试数据，不是本轮回归；1 项日志遮罩测试因为日志文件为空而失败，没有发现明文密钥。不得将该结果描述为全仓全绿。
- 全部变更 Python 文件的 Ruff `E9/F`、`compileall pa_agent tests` 与 `git diff --check` 通过。

## 2026-07-24 WO-EXEC-03 Demo-A/C 真实 Demo 收口

### 独立复核

- 基线 `537eaf578fccddc62804dc672d70377f5be68dde` 上独立回查 Demo-S execution `328d0f7f-fef6-5e5e-bd39-dad92a66512b`：真实 SupervisorGate 为 `allow_entry`，origin 为 `controlled_reproducible_demo_s`，入场、两笔原生保护、主动离场和 `closed/remaining=0` 证据均在生产执行账本中。
- Demo-S 的耐久事件按时间严格覆盖：`plan_created(state=ready)` → `preflight_passed` → `submit_intent(state=submitting)` → `entry_accepted(state=entry_pending)` → `reconciled(entry_pending→protecting)` → `reconciled(protecting→open)` → `exit_intent/exit_requested(state=exit_pending)` → `reconciled(exit_pending→closed)`。对应时间为 `2026-07-24T00:46:05.955506Z` 至 `2026-07-24T00:47:37.404471Z`；中间多轮 `protecting/exit_pending` 是保护建立、保护撤销和主动离场的只读对账，不是重复提交。
- 工单列出的 23 个执行、10m 聚合、多周期、GUI、监督、定仓和资金流测试文件第一轮复跑为 392 通过、0 失败；补强 6 个硬验收用例后，最终同一 23 文件套件为 398 通过、0 失败。九组合走 Controller→Worker→Service→OkxAdapter→Fake client 的生产链；ATR 倍增、定仓滑点隔离、保护与主动离场隔离均保持硬断言。
- 两个受影响测试文件的最终复跑为 66 通过、0 失败，其中新增 6 个硬验收：缺 ATR 明确 `missing_atr_for_slippage`；ATR 为 `0/负数/NaN/Infinity` 均 `invalid_number`，没有固定基点回退；同一主周期决策在空/反向 `htf_text` 下保持 `gate_result=proceed` 且风险数量、风险占用完全一致；canary 限价入场进入 `canceled` 后立即判失败，不继续等待、不切换市价。

### Demo-A：limit → market

- execution：`4d118cb0-ec65-5340-a8c9-9f9d8583bd1e`；数量 `27440`；限价入场订单 `3770119237559996416` 成交。
- 原生保护：`3770119507314909184`、`3770119717130772480`，均在主动离场前确认建立并随后撤销。
- OKX 实时单笔市价上限为 `20000`。第一笔原始 `27440` 市价离场被券商明确拒绝，没有当作未知写入重发；修复后保留拒绝证据，按真实单笔上限提交 `3770133355453042688`（`20000`）和 `3770134044895956992`（`7440`），均成交。
- 终态：`closed`、剩余 `0`、已实现盈亏 `-73.5046000000000050`。

### Demo-C：market → limit

- execution：`89e45ac4-4bee-58eb-9686-7ac36f90db79`；数量 `15057`；市价入场订单 `3770140719510016000` 成交。
- 原生保护：`3770140997116665856`、`3770141214280949760`。一次撤单意图在调用券商前被账户身份检查打断；Worker 重启后先保留未知标记，只读确认同一两笔保护仍为 `live`，再生成新撤单意图并跨轮复核后撤销，没有盲重发。
- 限价主动离场订单 `3770160697147740160` 按原模式累计成交 `15057`；部分成交期间发现累计成交量被重复扣减，随即暂停 Worker，保持券商 reduce-only 离场单覆盖真实剩余仓位，修复为固定提交前基线后恢复。
- 终态：`closed`、剩余 `0`、已实现盈亏 `-117.0923`。

### 10% 风险数量复核与证据边界

- 两笔均使用 `ctVal×ctMult=0.001`、`lotSz=minSz=1`、风险比例 `0.10`、费用率 `0.0005`、保守滑点率 `0.0010`。Demo-C 的入场前空仓账户快照为 `8714.71942600003 USDT`，entry/stop 为 `4047.9/4002.1`；同一 `calculate_risk_size()` 得到风险预算 `871.4719426000030`、单张最坏损失 `0.05787500`、风险占用 `871.42387500`、目标 `15057`，与 execution 完全一致。
- Demo-A 在资金变化后到入场前没有紧邻的直接账户快照。耐久账本第一份成交后快照记录现金 `8843.74440770003`、入场费用 `55.557134`，可重建入场前权益 `8899.30154170003`；entry/stop 为 `4053.9/4033.6`，同一公式得到风险预算 `889.9301541700030`、单张最坏损失 `0.03243125`、风险占用 `889.91350000`、目标 `27440`，与 execution 完全一致。该权益明确标为可验证重建值，不称作直接快照。
- 历史 Demo-A/C 记录没有保存当时 `maxBuy/maxSell` 的确切数值，无法事后诚实补写。但容量结论仍可二元证明：基线代码先把真实方向容量作为 `max_sz` 传入 `calculate_risk_size()`，随后 `OkxAdapter.preflight()` 再次读取真实 `maxBuy/maxSell`，只有 `plan.quantity <= max_quantity` 才可能保存 `preflight_passed` 并进入真实提交。两笔账本均有 `preflight_passed`、券商受理和完整成交，且不含截断路径；结合上面的风险公式分别独立得到 `27440/15057`，可证明两笔当时的真实容量至少等于目标风险数量，因此 `min(risk_quantity, real_max_size)` 仍严格等于账本数量。确切容量数值本身仍标记为“未保存”，不倒填。为消除后续取证缺口，现已在所有 Campaign 候选、Demo-S 和生命周期 canary 的耐久记录中写入完整 `risk_sizing`：权益口径与数值、10% 预算、入场/止损距离、费用/滑点、`minSz/lotSz`、`maxBuy/maxSell`、风险占用和最终数量。风险超限仍硬阻断，不截断。

### 最终运行态边界

- OKX Demo 只读回查：非零 `XAU-USDT-SWAP` 仓位 `0`、普通挂单 `0`、待生效 OCO `0`；本地活动 execution `0`、有效 `NEW_RISK` 租约 `0`。
- 旧 Campaign 历史 execution 全部经耐久账本确认终态后归档；新 Campaign `52a4f507-a8f8-4975-b0bf-ddddf9ff901c` 已恢复为 `active`，固定配置为严格聚合 `10m / min_trade_confidence=20 / extreme_aggressive / Demo`。WinSW ExecutionWorker 心跳和最后成功对账正常，但 PID 536 启动早于后续动态杠杆/风险运行态工作区代码；这些后续能力尚未加载。
- 共享环境已明确设置 `PA_AGENT_LIVE_TRADING_ENABLED=false`、`OKX_LIVE_ENABLED=false`，模拟门保持 `true`。本轮没有调用 Live 路由。
- 本轮适配器、执行服务、Campaign 与生命周期定向回归：151 通过、0 失败。第一轮以无界面 Qt 平台运行完整 `not live` 套件，收集 1407 项：1376 通过、31 失败；本轮新增的 Worker 空账本账户刷新夹具、Campaign 单测误连私有预检和“历史 execution 仅终态才允许归档重启”均已通过。剩余 31 项位于本轮未修改的既有预测、严格/宽松校验、决策面板、连续性、数据源切换、OpenClaw 和音效范围，不能描述为全仓全绿。
- 补充硬验收后再次运行当前工作区非 Live 全量：1335 项中 1315 通过、20 失败、0 跳过；20 项仍全部位于上述既有范围，新增工单测试无失败。工单指定的 23 文件套件为 398 通过、0 失败。
- 本轮改动 Python 文件的 Ruff `E9/F`、`compileall pa_agent tests` 与 `git diff --check` 通过。
- 发布补漏提交 `17e836d69530850cf0b7da235707b211536506f5` 已把文档先前引用、但首个收口提交漏掉的 `pa_agent/gui/read_models.py`、`tests/unit/test_workbench_read_models.py`、`tests/unit/test_cashflow.py` 精确推送到 `origin/main`；对应 14 个测试通过、0 失败，staged gitleaks 为 0 泄漏。价格行为、多周期与提示词的并发工作区改动未进入该提交。
- 本节的运行态以后仍会变化；发布提交与远程 SHA 以本轮 Git 收口的实时核验为准。

## 2026-07-24 PA 原始资料第二轮与多周期契约复核

### 资料边界与实现

- 只读核查 `E:\QQ文件\价格行为学资料` 及其 `extracted_text.zip`；原始 PDF、OCR、数据库、日志、账户记录和凭据没有复制到公开仓库。
- 对照 Brooks 识别文本确认：高周期用于背景、结构和关键位置；主周期负责当前交易；不为了拼出第二个触发器切换低周期；只使用少量相关周期。
- `10m` 主周期仍由同一品种连续、已收盘的官方 `5m` 严格聚合；高周期使用已收盘 `1h/4h` 薄标签。新增 `结构` 与 `位置` 两个粗粒度标签，和方向、EMA20、ATR14、时间一起进入 `htf_text`；不发送整套高周期 K 线。
- 二元决策、市场诊断、K线信号、通道、尖峰、二次入场提示词均明确：高周期只作 `htf_context`/`risk_warning` 背景，不要求多周期共识，不自动改置信度、数量、价格或 gate，不用低周期拼接触发理由。Session 与成交量本轮未升级。

### 自动硬验收

- 多周期、GUI、MT5/OKX 数据源、Demo Campaign、提示词组定向测试：`107` 通过、`0` 失败；包含高周期结构/位置输出、GUI 同源读取、10m 路由、提示词残留硬闸检索和 Demo 真实路径接缝。
- 新增提示词审计会失败于“高周期确认=高可靠性”“窗口共识才可评估三价”“只在多个窗口同时识别才可路由”等旧句式；第一次复跑暴露测试变量名错误，已修正后完整复跑通过，未把失败运行计入通过数量。
- 未完成：完整原始资料逐篇人工语义审计、Session/成交量实验、日线背景、自然 PA 信号的策略绩效证明。本轮只证明代码与提示词契约，不证明盈利能力。

## 2026-07-24 动态杠杆与风险运行态离线收口

- 同一生产对象链已覆盖 `max_size_exceeded → 有界策略网格逐点容量读取 → SupervisorGate 放行 → Controller → Worker → Service → OkxAdapter 调杠杆与回读 → 原风险数量复算 → SUBMIT`；监督拒绝和 50% 回撤停止两条路径均证明券商杠杆写入与订单写入为 0。容量不单调、最大杠杆仍不足、监督证据被改写、POST 后回读不明、余额变化导致风险快照失效都保持硬阻断。
- `WorkerStore` 目标 schema 为 v4：v1/v2 迁移保留历史命令和 resolution；v3→v4 增加任意 OKX 账单 ID、时间和最近扫描时间。旧外部转账自然滑出七天查询窗口时不会误触发停止；扫描真正中断满七天才失败关闭。动态杠杆与普通新增风险在券商 POST 前都刷新并执行同一资金流/回撤门。
- 核心执行、动态杠杆、监督、Campaign、资金流和风险运行态 297 项通过、0 失败；八类 OKX 算法单的 uncertain 处置参数化测试 14 项通过、0 失败；Ruff `E9/F`、`compileall` 和 `git diff --check` 通过。非 Live 全量仍为既有 31 项失败，失败范围与本节改动无关，没有为全绿放宽交易逻辑。
- 生产运行态已完成核心部署：迁移前硬门确认 Demo 非零仓位、普通挂单、八类算法单、活动 execution 和 `NEW_RISK` 租约均为 0；控制库一致性备份后由新 Worker 持锁从 schema v1 迁移到 v4。历史 submit 命令 `686b6d0e-...` 已用账户身份绑定的只读证据裁决为未写入，未解决 uncertain 写入为 0。Worker PID `48324` 已加载本节风险代码并连续成功对账；一次只读传输故障触发的 fail-safe 停止已按用户显式授权由 Worker 重锚解除。Campaign `0d239206-...` 持续运行真实 10m 分析。

## 2026-07-24 监督数量完整性与最终运行态复验

- 对抗审查发现真实 Demo-S `cf351b86-9f24-59b5-9814-ab203e0fbb19` 的监督数量为 `116289`，旧 Worker 提交前刷新后实际提交 `111596`。该笔实际风险未超限并已完整关闭，但它证明旧链允许 Worker 静默改写监督批准的数量。现已把提交前账户刷新拆成两种明确用途：账户总权益仅用于资金流和回撤闸门，`XAU-USDT-SWAP` 风险定仓使用结算币 USDT 权益；重新计算出的数量与监督冻结数量只要不精确相等，无论增加或减少，均以 `risk_sizing_changed_after_supervision` 阻断并要求重新监督。
- 自然与受控监督快照现同时耐久保存阶段二信号价、`0.50×ATR14` 后的最终有效限价、止损、USDT 风险权益、10% 预算和技术数量。动态杠杆授权校验使用该有效限价；Demo-S 反推信号价，避免适配器再次应用 ATR 偏移。execution 在 `prepare_analysis()` 后立即关联 Campaign；Demo-S 结果同时返回监督数量与执行账本实际数量。撤单竞态若转成部分成交/持仓，清理循环会切换到一次主动离场并等待真实终态。
- Campaign 重启继承 `last_completed_bar_ms`，不得重复分析或交易同一根已收盘 K 线。新 Campaign `7c462fdc-0574-4185-ad8c-1383bbeb77aa` 已证明从 `1784881800000` 继续到下一根 `1784882400000`，没有重复上一根；配置保持 `OKX Demo / XAU-USDT-SWAP / 10m / 20 / extreme_aggressive / entry+exit limit_with_slippage 0.50×ATR14`。
- 当前受影响套件最终复跑：338 通过、0 失败。最终全量单元测试收集 1445 项：1425 通过、20 失败；20 项仍全部属于既有严格/宽松校验、预测、决策面板、连续性、数据源切换、OpenClaw 与音效契约，没有为全绿放宽交易逻辑。全量集成测试：19 通过、8 失败、3 跳过；8 项为 AkShare 真实联网、旧预测、旧决策面板和旧 `no_order` 严格校验范围。全部本轮 Python 文件 Ruff `E9/F`、`compileall pa_agent tests` 与 `git diff --check` 通过。
- 20×/25×/45× 的真实方向容量分别观测为 `120000/55000/28000`，证明 OKX Demo 容量对杠杆并不单调。最新自然计划所需 `441374/436449` 张均超过真实容量，因此仍硬阻断，不截断。错误证据会保存首个下降点，例如 `20x=120000 → 25x=55000`。
- 2026-07-24 16:57（北京时间）OKX 账单端点再次返回 `50004`，风险运行态已按设计置为 `kill_active=true / risk_runtime_50004`。17:09 后 `last_bill_scan_at` 已连续前进，证明端点恢复；持久停写仍正确保持，Campaign/Worker 继续运行，活动 execution/命令/租约、仓位和挂单均为 0。只有用户明确授权 CLEAR 后才允许由唯一 Worker 重锚恢复新增风险，不能因接口恢复自动清除。

## 2026-07-24 WO-RUN-06-PROD-01 固定资本生产切换

### 生产备份、迁移与风险基线

- 使用 SQLite 原生 backup 创建切换前控制库副本：`scratch/production-backups/execution-control-v4-before-fixed-cap-cutover-20260724-201323.sqlite3`，大小 `90112` 字节，SHA-256 `2ad8a9f73e865ac66bdffe14f7187946b3af437aba6c6414fad9fdfa7da96e47`，`PRAGMA integrity_check=ok`、schema v4；备份时含 37 条命令、1 条处置和 1 条风险状态。
- Worker 持有同一单例锁后才允许迁移和风险基线回填。生产 `v4_cutover_baseline` 标记为 `backfilled=true`、`baseline_origin=existing_v4_risk_runtime_state_snapshot`、`historical_maximum_claimed=false`；它是切换基线，不冒充历史最高净值。
- 最终 Worker 的 WinSW PID 为 `5672`、实际 Worker PID 为 `55264`；`started_at=2026-07-24 20:56:15`（北京时间），晚于最终 `okx_adapter.py` 修改时间，心跳和 `last_successful_reconcile` 持续前进。
- 临时读取故障只通过专用命令恢复：`3afac66d-1e0e-483d-86e9-4b1f892adbb2` 和最终重启后的 `d702f99d-e3e9-4355-96b7-d9133928d829` 均为 `succeeded`。两次均先完成新的完整风险读取，只清 `risk_runtime_BrokerTransportError`，高水位始终保持 `78303.57015174496`，未调用普通 drawdown CLEAR 重锚。

### 固定资本与真实 Demo-S

- 现行策略设置为风险资本上限 `20000 USDT`、单笔最坏止损风险 `10%`、最大杠杆 `20x`。定仓有效资本为 `min(20000, 最新 USDT eq)`；`totalEq` 只用于外部资金流、高水位和 50% 回撤。
- execution `957e4d70-c6bd-509a-95d6-3cfcc48718d9` 的入场前证据为：USDT eq `77702.87537394238`、totalEq `77636.82793065166`、有效资本 `20000`、风险预算 `2000`、目标数量 `80116`、方向最大容量 `120000`。
- 入场订单 `3771294118783832064` 全部成交 `80116`，均价 `4060.9`；原生 OCO `3771295355141189632`、`3771295918218113024` 各保护 `40058`，止损 `4048.1`，止盈分别为 `4078.7/4088.9`。
- 主动离场时，第一张保护撤销后单笔详情返回 `51603 / Order does not exist`，旧进程把 execution 卡在 `exit_pending/needs_attention`；券商一度出现 `80116` 全仓无保护。修复后 Worker 使用客户算法订单号、待处理单和 `effective/canceled/order_failed` 历史复核，且在最终离场 POST 前紧贴写入点再次核对真实仓位、普通单和八类算法单。
- 离场订单 `3771323397642997760` 全部成交 `80116`，均价 `4061.4953754555893957`；execution 最终 `CLOSED`、剩余 `0`、`needs_attention=false`，记录已实现盈亏 `47.6991`。真实券商收口为仓位 `0`、普通挂单 `0`、八类算法单 `0`。

### 最终门禁与恢复运行

- OKX 适配器：`94` 通过、`0` 失败。执行、OKX 与风险范围：`430` 通过、`0` 失败。
- 完整单元测试收集 `1497` 项：`1477` 通过、`20` 失败。20 项与切换前失败节点完全相同，属于既有严格/宽松校验、预测、决策面板、连续性、数据源切换、OpenClaw、价格跳动与音效范围；本工单没有新增失败。
- 最终改动范围 Ruff `E4/E7/E9/F/I`、`compileall`、`git diff --check` 通过；独立对抗审查最终为 `PASS`。
- 新 Campaign `677c70df-aa5d-442a-9809-1896b7dc2994` 已恢复 `active`，固定配置为 `OKX Demo / XAU-USDT-SWAP / 10m / min_trade_confidence=20 / extreme_aggressive / entry+exit limit_with_slippage 0.50×ATR14`，继承 `last_completed_bar_ms=1784884200000`。Live 双硬门保持关闭，本节不构成 Live 交易或策略盈利证明。

## 2026-07-24 桌面 GUI 与后台同源、Qt 测试弹窗隔离

### 根因与用户入口

- `D:\Desktop\PA_Agent.lnk` 已解析确认指向当前仓库 `D:\Desktop\Quant\PA_Agent\.venv\Scripts\pa-agent.exe`，工作目录也在同一仓库；不存在另一套 Demo 代码。此前的差异来自旧 GUI 仍保留 15:23 加载的代码，而 WinSW Worker/Campaign 已在 20:56 后加载新代码，GUI 不支持热更新。
- 最终磁盘源码会在主窗口显示版本、仓库绝对路径、本次 GUI 代码加载时间、10 分钟 Campaign 状态与进度；交易窗口分开显示“桌面手动会话”和“自动 Campaign”，并直接展示风险资本上限、单笔风险比例、最大允许杠杆。持久 GUI 行情配置已对齐为 `OKX / XAU-USDT-SWAP / 10m`。
- 在用户随后禁止 Agent 继续操控桌面和鼠标前，21:36 版曾从正式快捷方式完成一次可见核验；但 `main_window.py/read_models.py` 在 21:50–21:51 仍有最终修正，该窗口没有加载最终代码。22:15 只读进程检查确认桌面 GUI 已不再运行。最终 GUI 可见验收保持 `pending`，必须由用户自行从同一快捷方式启动并提供截图后才能改为通过；Agent 不再进行任何窗口自动化。

### 弹窗隔离与离线验证

- 截图中的窗口标题 `pytest-qt-qapp`、输入内容 `test` 来自自动化 Qt 测试，不是 PA 生产弹窗。`tests/conftest.py` 现于测试模块收集前设置 `QT_QPA_PLATFORM=offscreen`，同时把正式 `config/settings.json` 和券商共享环境路径替换到每个测试自己的临时目录，并移除进程中的 OKX/Longbridge 写入开关与凭据。需要认证语义的 AI 设置测试显式使用测试 Key，不重新读取真实券商配置。
- GUI、Campaign 只读状态、数据源切换和测试隔离定向套件：`39` 通过、`0` 失败。完整单元测试收集 `1503` 项：`1485` 通过、`18` 失败；相较切换前 `1497` 项中的 `1477` 通过、`20` 失败，新增用例全部通过，并修复 2 个既有数据源切换失败。剩余 18 项仍是既有 PA 严格/宽松校验、旧预测面板、连续性、OpenClaw、价格跳动与音效范围，没有本轮新增失败。
- 本轮文件 Ruff `E4/E7/E9/F` 与除主窗口外的导入顺序检查、`compileall pa_agent tests`、`git diff --check` 均通过。`main_window.py` 仍有 6 个本轮未触碰的局部导入排序告警，未为追求表面全绿批量格式化无关代码。

### 运行态反馈

- Campaign 连续完成前 7 根已收盘 10m K 线分析：`7` 成功、`0` 失败，最近均为自然 `blocked:no_order`，所以没有创建交易命令或强制制造成交。第 7 轮结束后 OKX Demo 仓位、普通挂单和八类算法单均为 0，风险闸门正常，Worker 持续成功对账。
- 21:44 一次 OKX K 线 `URLError` 按设计触发 `risk_runtime_BrokerTransportError`。私有只读接口恢复后，专用命令 `5287e042-fe20-43bf-9614-b6f53ccbc6f9` 先刷新完整风险数据再成功解除该临时故障；普通 CLEAR 未使用，高水位保持 `78303.57015174496`、未重锚，也没有生成订单。下一轮成功后 Campaign 的旧网络错误已清空。

## 2026-07-24 GUI/Campaign 真值与测试凭据隔离复审

### 四项阻断修复

- 测试模块收集前即设置无界面 Qt、移除 OKX/Longbridge 环境变量，并把共享凭据路径指向测试专用空目录；导入期断言和测试子进程均验证无法回到真实 `Quant\env`。
- Campaign 状态与配置指纹一起冻结并耐久保存实际使用的风险资本上限、风险比例和最大杠杆。GUI 分开显示可编辑的“GUI 配置”和后台“实际冻结参数”；不一致、旧状态缺字段或状态不新鲜时标红且不冒充已生效。
- `active / stopping / needs_attention / completed` 及未知状态统一接受时间新鲜度检查；状态陈旧、未来或无效时，进度、最近结果和冻结参数的可信度同步降为未知。
- GUI 保存先构造完整候选设置并成功落盘，之后才更新共享内存和重载服务；模拟磁盘失败时，磁盘、内存和服务均保持旧配置。

### 离线门禁

- GUI、Campaign、凭据隔离、设置保存和数据源定向回归：`188` 通过、`0` 失败。
- 完整单元测试：`1493` 通过、`18` 失败。18 项与修复前相同，属于既有 PA 严格/宽松校验、决策面板、连续性、OpenClaw、价格跳动和音效范围；本节没有新增失败。
- 本节改动范围 Ruff `E4/E7/E9/F/I`、主窗口 Ruff `E4/E7/E9/F`、`compileall pa_agent tests` 与 `git diff --check` 均通过。
- 运行中的 Campaign 是上述字段加入前启动的旧进程；在完成空仓、空挂单、无活动执行/命令、合法租约和 Worker 健康的实时硬门检查并做最短安全重启前，文档与 GUI 均不得声称当前进程已加载新冻结参数。最终桌面可见验收仍须用户自行从 `D:\Desktop\PA_Agent.lnk` 启动并提供截图，Agent 不操作桌面或鼠标。

### 22:38 实时阻断

- Campaign 最后在 22:01:33 确认正常，已完成 8 轮、失败 0、最近 `blocked:no_order`、无进行中轮次；22:16 已找不到进程，因此退出发生在 22:01:33–22:16 之间。状态文件没有退出错误，不能把时间上吻合的 IP 故障说成已证实退出原因。Worker PID `55264` 仍在，但 22:09 后 OKX 持续返回 `50110`：当前公网 IP `154.40.63.187` 不在该 API Key 白名单。
- Worker 当前为 `needs_attention / BrokerApiError`，风险运行态为 `kill_active=true / risk_runtime_BrokerTransportError`，`NEW_RISK` 租约为 0；两个 SQLite 库完整性检查均为 `ok`，本地没有活动 execution。
- 最后一份精确到秒的券商空现场证据为 21:32:53：仓位 0、普通挂单 0、八类算法单 0。IP 白名单阻断期间无法取得更新证据，因此不能把该旧快照当作当前事实，也不能启动 Campaign。恢复顺序固定为：用户恢复已授权公网 IP → 私有只读硬门 → 专用临时风险停止恢复（不重锚高水位）→ Campaign 最短重启并继承最后完成 K 线 → 再做 GUI 截图验收。

### 23:28 白名单恢复与真实 Demo 反馈

- 用户加入当前公网 IP 后，OKX Demo 私有只读恢复。仓位、普通挂单和八类算法单均为 0，活动 execution 和待处理命令为 0；专用恢复命令 `3cc99df4-8463-47da-8984-a587b2fa5490` 成功，高水位保持 `78303.57015174496`、未重锚。
- Campaign 通过正式 `python -m pa_agent.okx_demo_campaign run` 入口恢复，进程树 `51428 → 22204`，沿用 Campaign ID 和 `last_completed_bar_ms`，状态新增并冻结 `20000 / 0.10 / 20`。22:00–23:00 停机期间的中间轮次没有补跑，也未伪报完成。
- 恢复后的 23:10 K 线产生真实 PA 多单 execution `25ca0e71-0cfd-573a-94ef-b04be6edbaa1`，固定风险预算 `2000 USDT`、数量 `82630`，经 Controller→Worker 提交 OKX Demo 限价单 `3771656369780903938`。270 秒内成交 0 后按规则撤单，没有切市价；最终 `canceled / remaining=0 / attention=false`。
- 23:27:32 最终券商回查：非零仓位 0、普通挂单 0、八类算法单全 0。Campaign `active`、累计 9 成功/0 失败、最近 `execution:canceled`；Worker PID `55264` 心跳和成功对账新鲜、kill=0、未解决 uncertain=0。
- 四项 GUI/Campaign 安全阻断的独立对抗复审结论为 `PASS`。桌面可见验收仍未完成，必须由用户自行启动 `D:\Desktop\PA_Agent.lnk` 并提供截图。

## 2026-07-25 OKX 实盘工作台与常驻临时故障恢复

### GUI 与配置语义

- 主窗口新增“分析工作台 / 实盘交易”一级页签，旧“实盘交易”菜单改为直接切换工作台，不再打开后端字段堆叠的大配置弹窗。
- 实盘工作台只在主视图展示用户需要的事实：OKX Demo、Campaign/Worker、风险停止、最新 PA 决策、生命周期、账户资金、当前生效配置和下次启动配置。API 地址、账户身份摘要、命令 ID、execution ID 和原始错误码默认收进技术详情。
- 风险预算模式固定为“资金上限 + 风险比例 + 最大杠杆 → 唯一张数”；固定张数模式固定为“张数 + 最大杠杆 → 反算最坏损失和风险比例”。两种模式互斥，容量不足或超过风险上限直接阻断，不自动缩小张数。
- 离屏人工检查文件：`scratch/trading_workbench_1440x900.png`、`scratch/trading_workbench_1920x1080.png`、`scratch/trading_workbench_fixed_1440x900.png`。三张均无重叠、截断或横向拥挤；正式桌面可见验收仍由用户自行从 `D:\Desktop\PA_Agent.lnk` 重启并截图。

### 真实运行证据

- 02:54 前硬门：非零仓位 0、普通挂单 0、八类算法单全部 0、活动 execution 0、未解决写命令 0；Worker `7aad2cb0-f474-4a26-9e8b-49f9e78e5490` 心跳和成功对账新鲜。
- 临时 `risk_runtime_BrokerTransportError` 由命令 `4cc7e044-5f99-4241-a9f8-928d7b8e4870` 完成专用复核恢复。恢复后 `kill_active=0`，`adjusted_high_water_usd=78303.57015174496`，没有重锚。
- 新 Campaign `73451990-ce0f-490d-be94-bc3522d86699`、进程树 `52720 → 26936` 已真实加载自动恢复代码；首根自然 10m K 线先完成专用恢复，再得到 PA `blocked:no_order`，没有强制制造订单，Campaign 保持 `active`。
- 第二根自然 10m K 线产生真实空单 execution `aa90ddfe-08e7-5fa8-a91f-49da9170dec8`：固定有效资本 `20000 USDT`、授权风险预算 `2000 USDT`、数量 `113724` 张，OKX Demo 实际均价 `4059.4012750167071155`，20x 净空持仓 `-113724`。两张原生 OCO `3772100057026174976 / 3772100521721503744` 各覆盖 `56862` 张，止损 `4064.8`，止盈 `4057.5 / 4050.2`；保护总量等于持仓，普通挂单 0、其他算法单 0、execution=`open`、`needs_attention=false`。这是真实自然 PA 反馈，不是 canary；后续终态仍由常驻 Worker/Campaign 继续监控。
- 旧进程竞态生成的 execution `6cb04c25-9d30-597c-be8a-1900850d5c62` 被 Worker 提交前风险闸门确定阻断为 `BLOCKED`；命令 `74d09a47-c92a-4da1-bed8-301ef55bc8c5` 为 `FAILED`，无券商订单号。该失败发生后、02:54 新 Campaign 启动前的只读真值为仓位 0、普通挂单 0、八类算法单全部 0；不能把这个历史空仓快照误当成 03:05 自然空单之后的当前状态。

### 工程门禁

- 交易、风险、Worker、Campaign 与新工作台定向回归：`469` 通过、`0` 失败；临时恢复、确定性提交阻断和重启继承用例定向回归：`99` 通过、`0` 失败。
- 完整单元测试收集 `1550` 项：`1532` 通过、`18` 失败。18 项仍是既有一致性/连续性、旧预测面板、音效、价格跳动、OpenClaw 和宽松枚举映射范围，本阶段没有新增失败。
- 新工作台、Campaign、风险运行态及其测试 Ruff `E/F/I/UP/B/C4`、`compileall pa_agent tests` 与 `git diff --check` 通过。仓库其余本轮涉及的既有大文件仍有历史行长、导入顺序、RUF/SIM/中文标点和宽异常捕获问题，因此 PRD 第 16 条按严格口径仍未完全通过。

### 03:31 最终对抗复审修正

- 最终受影响范围回归：`319` 通过、`0` 失败。最终只读对抗复审结论为 `P0 / P1 / P2 全 PASS`，未发现残余阻断。
- 固定张数现在由不可变计划携带 `sizing_mode / fixed_quantity / risk_used / contract_notional / worst_case_loss_per_contract`。Worker 写前用同一模式重算并逐项比较；单张风险变化但最终张数仍相同也会在券商预检和 POST 前阻断。真实本地全链 `Controller → Worker → ExecutionService → OkxAdapter → Fake OKX POST` 已证明固定 `2` 张只形成一次 `2` 张入场 POST；固定模式杠杆授权只绑定资金上限、模式、固定张数和最大杠杆，不使用隐藏风险比例。
- 临时风险恢复把本根 K 线和唯一命令 ID 写入 Campaign 状态。超时或重启后只等待原命令；若进程崩溃在入队与命令 ID 落盘之间，本根直接跳过而不创建第二条命令。`ERROR / UNKNOWN / needs_attention` execution 不允许被普通重启路径当作安全终态跨过。
- 工作台只列出并操作 `OKX / demo` 执行；陈旧账户快照显示为未知而不显示旧金额，未知风险不标绿。风险预算与预计实际最坏损失分开显示；固定模式新增只读反算，保证金字段明确为“按最大杠杆估算的最低保证金”。未知英文错误、UUID 和后端事件码不再进入主表。
- 完整主窗口离屏图 `scratch/main_window_trading_1440x900.png` 与固定模式图 `scratch/trading_workbench_fixed_1440x900.png` 已重新生成并人工查看；一级页签、菜单、状态栏、北京时间、只读反算和完整页面滚动均正常，无横向重叠。
- 03:24 只读运行证据：自然空单第一档 `56862` 张已止盈，剩余 `56862` 张由同量 `reduceOnly` 原生 OCO 完整保护；普通挂单和其余算法单为 0，Campaign 同向持有且未重复加仓，风险停止为 0。执行账本的价格差盈利与 OKX 净已实现盈亏口径不一致，尚未核清费用/资金费前不得把前者称为净利润。
- 当前 Campaign 在最终“耐久恢复命令 ID”和“固定张数完整写前快照”修正前已经启动。因为仍有真实受保护仓位，本轮没有重启服务或 Campaign；磁盘最终代码只会在下次空仓硬门通过后的安全重启中加载。

## 2026-07-27 WO-H 接手补漏与总工单诚实性复核

### 成交量影子自动采集

- 原 `/goal` 要求“每次分析额外落一份成交量摘要”，而提交 `2e95a05` 只提供可调用函数。接手批次已在完整 Stage1 和增量 Stage1 的消息构造点各调用一次 `record_volume_shadow(frame)`；调用发生在真实 Stage1 请求前，但返回值没有参与任何消息内容。
- 生产默认输出为 `scratch/volume_shadow/<symbol>_<timeframe>.jsonl`。测试在收集期和每个用例中把 `PA_AGENT_VOLUME_SHADOW_DIR` 指向临时目录；全量测试后仓库内 `scratch/volume_shadow` 仍不存在。
- 同一 JSONL 现在由跨进程文件锁串行写入，普通写错会回滚到原长度；若进程在半行中断，下一次持锁写入会先截回最后一个完整换行。并发写入和中断半行恢复均有单测。
- 新增测试直接证明 JSONL 文件生成，同时完整消息中不存在 `relative_volume`、`baseline_volume`、`latest_volume`；完整分析和增量分析路径均被覆盖。`prompt_engineering/` 差异为空。

### 离线评分与 WO-D 入口

- `pa_agent/data/volume_shadow.py` 继续只做放量组与缩量组的后续相对振幅描述性比较。摘要没有预测方向，因此没有伪造 Wilson 方向准确率，也没有据均值差宣称统计显著或转正。
- 本机 `scratch/p1_multimarket_acceptance.py` 原先把枚举类名误记为事件名，并读取不存在的 `RecordMeta.status/error_type`，导致成功记录也无法通过。接手批次已改为记录 `event.name`，用 `RecordSaved + exception is None + Stage1/Stage2 结果存在` 判定完成，明确把内存设置来源标为 `longbridge`，并用 `finally` 释放订阅和连接。
- 该脚本通过目标 Ruff、`py_compile` 和成功/取消判定函数的离线对象检查；没有读取真实凭据或调用外部 API。真实 AAPL.US、700.HK、600519.SH 验收仍被长桥 `401004 token invalid` 硬阻塞。

### 工程门禁与范围

- `HEAD=2e95a05` 开工基线：1874 项、0 失败、0 错误；第一次运行因 AkShare 端点不可达出现 4 跳过，JUnit 明确记录 1 项条件联网跳过和 3 项缺少 KKAI Key 的固定跳过。
- 补漏与 P2 修正后的最终三套件：1880 项、0 失败、0 错误、7 项跳过；其中 3 项因未提供 KKAI 模型密钥固定跳过，4 个 AkShare 联网冒烟测试因各自行情请求当轮不可达而条件跳过。
- 改动文件 Ruff `E4,E7,E9,F,I,UP,B,C4`、`py_compile` 和 `git diff --check` 通过；`pa_agent/execution/`、`pa_agent/gui/`、`scripts/`、`records/`、`.github/` 差异为空，未操作交易运行态。
- 三名独立 Agent 覆盖交易安全/范围、数据正确性、提示词缓存和文档诚实性四个视角；无 P0/P1。确认的 5 个 P2 已修正，并由定向测试及最终三套件回归复证。
- 逐项代码审计确认 WO-F 仅完成部分严格化与 entry/stop OHLC grounding；原规格中的其余价位、K 线引用、真实 tick 和耐久阻断语义未全部实现，因此总工单已撤回“WO-F 完成”主张。

## 2026-07-27 WO-C2 Campaign 对账监控耐久化

### 故障与代码验收

- 现场状态证明原 Campaign 在 10:30:55 因“等待交易后台完成下一轮券商对账超时”进入 `needs_attention` 并退出。根因是历史 execution 不分状态统一强等下一轮全局对账，`TimeoutError` 又不属于 `run()` 的临时读取处理范围。
- 修复后会先扫描全部 owned execution：安全终态和 `READY` 不等待；普通活动态对账超时或 Worker 单轮 attention 会在重读账本后耐久记录临时阻断并重试；`UNKNOWN/ERROR`、非终态 attention、记录丢失和非法状态仍硬阻断。
- 后置监控不会覆盖刚完成 K 线的真实结果；收口阶段按剩余时间重试临时对账，真实不安全状态耐久落为 `needs_attention`。测试同时证明超时路径零提交、零撤单、零离场、零调杠杆。
- Campaign 定向套件 86 项通过、0 失败；全量 unit、property、integration 三套件合计 1886 项通过、0 失败。`compileall pa_agent tests` 与 `git diff --check` 通过。
- 完整 Ruff 在修改前后都报告同一 2 项既有债：`SIM105` 和非启用规则对应的 `RUF100`；排除这两个精确基线项后，改动文件 Ruff 通过。本轮没有顺手修改无关旧代码。
- 两轮独立只读对抗审查发现并推动修复 5 个安全语义缺口：确定未写入的终态 attention、后置监控结果覆盖、临时 Worker attention、收口重试、人工中断耐久状态。最终代码审查与测试审查均为 PASS。

### 真实运行验收

- 16:31 最终启动硬门：Worker 心跳和成功对账新鲜，两库完整，账户身份一致；活动 execution、PENDING/RUNNING 命令、未解决 UNCERTAIN、NEW_RISK 租约均为 0；OKX Demo 非零仓位、普通挂单和八类算法挂单均为 0。
- 风险账本当时只保留白名单内的 `risk_runtime_BrokerTransportError` 临时停止。Campaign 从正式 `run` 入口恢复后，只创建一次耐久恢复命令 `27613f47-f27a-44c7-ba44-5dc378e7ee4e`；Worker 完成新的账户、身份和账单读取后返回 `clear_drawdown_stop_completed`。高水位保持 `78303.57015174496`，没有重锚。
- 首根新已收盘 10m K 线 16:20–16:30 完成，`analyses_completed` 从 16 增至 17，真实结果 `blocked:no_order`；没有新增 execution、订单、仓位或租约。Campaign 保持 `active`，Worker 和全局对账继续新鲜，风险停止为 0。

## 2026-07-27 WO-F Claim Validation 完整闭环

### 实现与拒绝语义

- 新增 `pa_agent/ai/claim_validation.py`，同时校验原始模型声明与归一化后的 Stage1/Stage2 对象，防止越界价位或虚假 K 线引用在归一化过程中被洗掉。
- Stage1 覆盖支撑与阻力，Stage2 覆盖入场、止损和两档止盈；价格范围使用已收盘 OHLC 包络外扩可配置 ATR14 容差，默认 `1.0×ATR14`。
- 价格精度只认行情源写入 `KlineFrame.price_tick` 的真实最小跳动；缺失时返回 `price_tick_unavailable`，不按显示小数位猜测。
- `bar_range`、`new_closed_bars`、`entry_basis_bar` 和文本中的 K 序号必须真实存在于当前已收盘 K 线集合。错误码按固定优先级稳定选择，并耐久写入分析记录。
- 最终失败由 Campaign 映射为 `blocked:claim_validation:<code>`，完成本根后继续下一根；离线测试证明该路径不创建 execution、提交、撤单、离场或调杠杆写入。
- Campaign 恢复只接管同一 `campaign_id` 的耐久记录；ownerless 文件不会被采用，崩溃前遗留的成功分析会明确关闭为 `blocked:stale_recovered_analysis`，不会在重启后执行陈旧信号。

### 离线验证

- `tests/unit`、`tests/property`、`tests/integration` 最终为 1926 项通过、7 项跳过、0 失败；其中 3 项因未提供 KKAI 模型密钥固定跳过，4 个 AkShare 联网冒烟测试因各自端点当轮不可达而条件跳过。
- GUI E2E 为 4 项通过、0 失败。加固后的品种切换用例明确断言 Stage2 看到了取消令牌；有效订单夹具的文字价位与结构化入场、止损、止盈一致。
- 改动 Python 文件 Ruff `E4,E7,E9,F,I,UP,B,C4` 相对 `HEAD` 没有新增诊断，减少 2 条旧诊断；`compileall pa_agent tests` 与 `git diff --check` 通过。
- 两轮代码/测试对抗审查及最后增量复审均无 P0/P1。最后发现的历史文件名兼容与两个测试假绿点已修正并复跑。
- 持久记录文件名此前把分钟误写为月份。新 canonical 文件名改用真实分钟、毫秒和 Campaign ID；legacy 查找保留旧 `%m` 格式，确保历史主记录和自由追问 sidecar 不断链。
- 隐藏重定向进程里合并运行全部 `tests/` 时，Windows/Qt 的既有 `AxisItem` 析构竞态触发 access violation；工单要求的三套件与 GUI E2E 已在隔离进程分别完整运行并零失败，未把该运行器崩溃伪报成测试通过。

### OKX Demo 实盘式运行验收

- 2026-07-27 19:01–19:05 完成只读硬门：Worker、账户身份、风险态和数据库完整性均满足要求；活动 execution、活动命令、未解决 UNCERTAIN、有效 NEW_RISK 租约、仓位、普通挂单和八类算法挂单均为 0。
- 19:07:06 从正式 `run` 入口恢复同一 Campaign `6cba8d3e-44e2-447e-b51f-1254aff2a425`。
- 19:10–19:20 K 线于 19:21:37 完成，19:20–19:30 K 线于 19:31:31 完成；两根均为自然 `blocked:no_order`。对应记录复跑声明校验均为 0 issues。
- 发现文件名分钟问题后，20:01 再次确认 Campaign 空闲、两库完整、Worker/账户/风险态健康且券商空仓空单，随后只停止旧进程树并从正式 `run` 入口恢复同一 Campaign，没有使用 `restart`、没有归档状态或改变冻结参数。
- 重载后的 20:00–20:10 K 线于 20:11:35 完成，Campaign 统计变为 28 成功、2 失败，结果仍为 `blocked:no_order`。新记录 `2026-07-27_20-10-17-632_XAU-USDT-SWAP_10m_6cba8d3e-44e2-447e-b51f-1254aff2a425.json` 的分钟与 `timestamp_local_ms` 一致，`analysis_price_tick=0.1`、来源为 `okx_5m_utc_pair_aggregation`、K1 已收盘、共 100 根；原始/归一化 Stage1、Stage2 四次声明复验均为 0 issues。
- 20:13 最终回查：Campaign 保持 `active`；两库 `integrity_check=ok`，Worker 心跳与成功对账新鲜；活动 execution、PENDING/RUNNING 命令、未解决 UNCERTAIN、有效 NEW_RISK 租约、非零仓位、普通挂单和八类算法挂单均为 0。账户身份仍为 `ba9b744dc78ae3fc203980e62b854b0a0e3d44c9c6d5e446de910bea74ef1def`，高水位仍为 `78303.57015174496`，风险停止为 0。
- 新 Campaign 日志没有真实 ERROR/CRITICAL 等级行；仅重复出现已登记的 Windows `WinError 32` 日志轮转警告，没有第三类新异常。
- 这次运行验收证明正式入口已加载 WO-F 且自然样本没有被误杀。自然样本没有产生非法声明，因此不能声称现场实际触发过 `blocked:claim_validation`；非法声明拒绝与继续下一根由离线集成和 Campaign 测试证明。
- 全程仅使用 OKX Demo 模拟账户，不构成 OKX Live 实盘、策略盈利或 Longbridge 验收证明。长桥 `401004` 与 WO-E 视觉方向阻塞均未解除。

## 2026-07-27 WO-F 发布与总工单完成证据复审

### 发布收口

- WO-F 的 49 个精确目标文件以提交 `c0b58d0a859d4e0234862c785734ac860d256699` 推送到 PUBLIC `xiaojinlucky/PA_Agent` 的 `origin/main`。
- 提交前 staged gitleaks 为零泄漏；暂存快照只有 `.py`、`.md`，没有删除、重命名、忽略文件、二进制、gitlink、禁入文件或超过 50 MiB 的文件。
- 推送后本地 HEAD、`refs/remotes/origin/main` 和 `git ls-remote origin refs/heads/main` 均为同一 SHA，工作区干净。发布动作没有停止运行中的 Campaign 进程树。

### 直接回归补证

- `test_market_rules_injection.py` 不再只检查 system prompt 缺少某个子串，而是用真实 Stage1 美股和 Stage2 港股消息比较调用前后 UTF-8 bytes 完全一致，同时证明市场规则只进入 user。
- `test_okx_fixed_proxy_scripts.py` 新增回滚期间 10981 被外部可执行文件抢占的直接测试：旧配置和 metadata 恢复，候选与抢占监听者停止，无法恢复旧代理时端口明确下线并抛错。
- `test_longbridge_source.py` 新增历史页只返回重复时间戳时单次停止、美国切入/退出夏令时周末、半日市到下一交易日合法间隔及 4h 不枚举分钟的直接测试。
- `test_okx_demo_campaign.py` 新增 owned execution 记录丢失、非法状态硬阻断、普通中断在进程锁内走真实 `close_out()`、收口中断不二次进入、`stopping / needs_attention` 不自动恢复且不覆盖原始错误，以及状态补写失败仍保留原始 `KeyboardInterrupt / SystemExit` 的直接测试。
- 撤单与离场命令现在逐条等待 Worker 耐久终态；`FAILED / UNCERTAIN / TimeoutError` 均只调用一次并转 `needs_attention`。明确成功后，同一 execution 的同一种收口动作在本次收口中也最多发送一次；状态未推进就只读等待，最终超时转人工，不盲目重发。
- 四个改动测试文件合并为 220 项通过、0 失败；最终 `tests/unit`、`tests/property`、`tests/integration` 为 1956 项通过、7 项跳过、0 失败。7 项仍是 3 个 KKAI 密钥固定跳过与 4 个 AkShare 端点条件跳过，没有新增普通 skip/xfail。
- 全仓只读执行 `ruff check . --select E4,E7,E9,F,I,UP,B,C4 --output-format json --no-cache`，当前为 293 项历史诊断，其中 249 项带自动修复建议。本轮没有运行全仓 `--fix`。

### 市场制度官方来源

2026-07-27 复审只使用交易所和监管机构页面核对现有规则，没有修改提示词中的制度数值：

- A 股经互联互通交易的印花税按成交额 0.05% 向卖方收取：[HKEX Stock Connect Investor Book](https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/Getting-Started/Information-Booklet-and-FAQ/Investor_Book_En.pdf)。
- 香港上市证券通常由买卖双方各按成交额 0.1% 缴纳股票印花税：[HKEX Securities Transaction Fees](https://www.hkex.com.hk/Services/Rules-and-Forms-and-Fees/Fees/Securities-%28Hong-Kong%29/Trading/Transaction?sc_lang=en)。
- 美国多数券商证券交易自 2024-05-28 起采用 T+1 标准交收周期：[SEC T+1 implementation statement](https://www.sec.gov/newsroom/press-releases/2024-62)。
- 科创板买入申报不少于 200 股，超过 200 股的部分可按 1 股递增：[上海证券交易所投资者教育](https://edu.sse.com.cn/tib/)。

### WO-E 阶段 0/1 合同

- `docs/prd/05_多市场看盘前端设计包.md` 已补齐 `QuoteSnapshot` 的字段、来源、定点数值、真实 tick、行情模式、延迟窗口、时间与身份不变量。
- 四市场最近标的和本地自选继续使用现有 `GeneralSettings`、`revision` 冲突检查和原子保存；文档已冻结旧设置幂等迁移、兼容镜像、会话已生效但保存失败以及迟到保存结果的语义。
- 页面使用只增不减的 selection generation 绑定市场、来源、标的和周期；同一选择的报价、K 线与静态信息刷新另用各请求族只增的 request sequence，旧选择和旧刷新无论成功或失败都不得覆盖新状态。Longbridge 内 US/HK/CN 切换及 Longbridge↔OKX 跨源切换都明确了提交点、回滚、资源释放和迟到结果丢弃。
- 验收矩阵现覆盖三只 Longbridge 股票、XAU-USDT-SWAP、双向跨源、认证失效、行情陈旧、日历未知、设置冲突、保存失败、分析中切换，以及选择与同一选择刷新逆序返回，共 17 个场景。
- 本轮没有代用户选择 A/B/C，没有调用 ChatGPT Web、Stitch、图像生成或视觉审计，也没有修改 PyQt6、QSS、生产设置模型和数据源。

### 仍未闭合

- 固定代理 `metadata.json` 与实际 `config.json` 没有共同指纹。现有脚本无法把二者不一致本身识别为失败；只改测试无法忠实覆盖，而本轮明确禁止修改 `scripts`。
- 长桥最后已知外部结果仍为 `401004 token invalid`，共享 env 自 2026-07-24 后没有更新；未运行真实三标的验收。
- WO-E 用户已于 2026-07-28 按实际显示顺序选择 `1`；该选择只锁定方向，不能冒充后续网页版 ChatGPT PRD、Stitch、连续三轮图像精修、视觉审计、PyQt6 落地或桌面验收。
- `D:\Desktop\Quant\shared` 是否建立独立 Git 仓库仍等待用户决定；没有执行 `git init`。

## 2026-07-28 WO-E 方向 1 锁定与外部设计门

### 方向与文档证据

- 用户按本次对话中的实际显示顺序回复 `1`，唯一绑定到 `Dense Scan Workbench`：项目文件 `docs/prd/assets/wo_e_product_design/option-1-dense-scan-workbench.png`、原生尺寸 `1586×992`、SHA-256 `C2E4B45C62860C308B9F7EDD17825F0F72C8A96E98A742181486FCEF0AAA4C13`。
- `docs/prd/07_WO-E_方向1_多市场看盘方向绑定PRD.md` 已冻结方向骨架、候选稿删除项、六个首屏对象、数据路由、`QuoteSnapshot`、generation/request sequence、回滚、状态、文案、视觉门、M01–M17 和方向 1 补充验收 D01–D07，性质是外部 PRD 的本地输入合同。
- 当前仓库不能独立核验 Product Design `ideate` 的调用入口、提示词与三方向产物绑定；`frontend-design` 验收项 B1 按失败处理，三张 ImageGen 候选稿和用户选择不能替代该证据。
- 本次选择只通过三方向审美门。候选图仍缺精确 `1440×900`、唯一标题、完整文字+图标状态和冻结文案清理，不能进入生产实现。
- 只读代码审查确认当前仍没有按市场本地自选、页面 `QuoteSnapshot`、市场时钟或 generation-aware 刷新链；方向 1 的可见落地必然修改 `pa_agent/gui`。用户原始禁区仍有效，选择方向不等于解除代码范围。

### 外部工具真实结果

- 应用内浏览器可以打开 ChatGPT，但当前未登录；提示词填入后页面要求登录，消息没有提交。
- 已登录 Chrome 可以读取 ChatGPT 页面，但附件上传失败，随后扩展连接不可用；具体是注册、权限还是其他扩展故障尚未证明。Agent 没有自行安装、修改注册或绕过安全限制。
- `docs/prd/08_WO-E_方向1_外部设计门证据.md` 保存了预备提示词、逐文件脱敏附件清单、输入缺口和真实阻塞。没有会话 URL、外部回答、Stitch Screen/版本或下载物，因此网页版 ChatGPT、Stitch 和三轮 ImageGen 均明确记为未完成。

### 09:55–09:58 只读运行监控

- Worker 服务和心跳运行；两库 `quick_check` 均为 `ok`；活动 execution、pending/running 命令和有效 `NEW_RISK` 租约均为 0。
- OKX 私有读取故障已触发风险停止。Campaign 进程不存在，磁盘 `active` 状态已经过期，不能称为后台运行。
- 最近耐久完成的是 2026-07-27 21:50–22:00 已收盘 10m K 线：Stage1/Stage2 均存在，终点 `wait`、trade confidence 38、结果 `blocked:no_order`。
- Worker 启动早于当前 `HEAD=771c951`，没有加载本轮代码。最后本地账户快照已陈旧；本轮没有 OKX 私有接口证据，所以当前仓位、普通挂单和全部算法挂单均为实时真相阻断。
- 本轮没有启动、停止或重载 Worker/Campaign，没有读取凭据，没有写租约、execution、订单或券商。
