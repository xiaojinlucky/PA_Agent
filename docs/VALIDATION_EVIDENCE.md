# PA_Agent 移交验证证据

> **历史快照（截至 2026-07-22；不代表当前状态）**：本文件记录当时的发布、测试和运行验证。当前代码与 Git 基线以根目录 `CONTEXT.md`、`docs/CODEX_HANDOFF.md` 和最新 GitHub `main` 为准；本文件中的历史 SHA、测试数字和运行态只在各自时间点成立。

## 本轮后续验证（2026-07-23）

- `WO-S2A-01` 定向套件：156 通过、0 失败。覆盖严格监督输出、主备同快照、确定性拒绝、原子落盘、重启复用、Campaign 门控、真实 `ExecutionController` / `ExecutionWorker` 离线 Demo 命令链，以及指定的 AI 档案、执行域和 OKX 数据源回归。
- 全量 `tests/unit`：本轮收集 1225 项，20 项失败；失败均位于本轮新增监督/Campaign 定向套件之外，仍按既有仓库失败处理，不能宣称全量绿。
- 新增代码选择性 Ruff（`E,F,I,UP,B,SIM`）、`compileall` 和 `git diff --check`：通过。
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
