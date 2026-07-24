# PA_Agent 移交验证证据

> **历史快照（截至 2026-07-22；不代表当前状态）**：本文件记录当时的发布、测试和运行验证。当前代码与 Git 基线以根目录 `CONTEXT.md`、`docs/CODEX_HANDOFF.md` 和最新 GitHub `main` 为准；本文件中的历史 SHA、测试数字和运行态只在各自时间点成立。

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
