# PA_Agent 移交验证证据

## 发布事实

- 私有仓库：`Jinqingchang/PA_Agent`
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
- `tests/unit/test_debug_widget_masks_key.py` 故意包含高熵合成假钥匙用于遮蔽测试，私有仓库原文件保留；为使 GPT 安全包达到 0 命中，仅从安全 ZIP 副本排除。
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
- 生产 `records/execution.sqlite3` 保持 schema v1、0 条 execution、0 条活动记录；v1 → v2 迁移只在副本验证，未触碰生产账本。

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
