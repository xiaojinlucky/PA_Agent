> **落库说明（2026-07-23）**：本文件是外部规划输入的原文归档，设计正文基于旧基线 `3ae95321eef706889cf901c63a1c293f11f7608d`。当前代码真值为 main `1a04c144f810ffb486280ed8a1875ff0130bb070`；正文与代码有出入时，以当前代码、`CONTEXT.md` 和阶段基线记录为准。本文件是工程目标与验收输入，不表示功能已实现，也不构成交易授权。除本说明外，正文保持原文。

# PA_Agent 全自动交易控制台工程移交与硬验收

## 1. 文档用途

本文件交给 GPT 或 Codex 作为工程总控输入。

任务是基于当前 `xiaojinlucky/PA_Agent` 实际源码，对现有系统进行审核、重构和扩展，不允许脱离仓库事实重新造一个新项目。

本文描述后续工程实施的准入条件。本轮文档修订不修改代码、不生成前端、不执行交易；后续任何代码阶段都必须先完成基线审计、方案冻结和测试矩阵。

## 2. 当前真实基线

执行前必须重新读取当前 HEAD，至少覆盖以下文件

### 行情与主界面

- `pa_agent/data/factory.py`
- `pa_agent/data/okx_source.py`
- `pa_agent/gui/main_window.py`
- `pa_agent/gui/chart_widget.py`
- `pa_agent/gui/ai_sidebar.py`

当前已知事实

- OKX 行情源已经存在
- 数据源工厂中已经注册 `okx`
- 主界面已经显示 `OKX 行情`
- `OkxSource` 支持现货和永续、15 分钟周期、收盘标记和品种在线验证
- 工单必须审核和验收现有链路，不得写成从零新增 OKX 行情源

### 交易执行

- `pa_agent/execution/controller.py`
- `pa_agent/execution/worker.py`
- `pa_agent/execution/service.py`
- `pa_agent/execution/okx_adapter.py`
- `pa_agent/execution/okx_client.py`
- `pa_agent/execution/models.py`
- `pa_agent/execution/store.py`
- `pa_agent/execution/worker_store.py`
- `pa_agent/execution/worker_protocol.py`
- `pa_agent/gui/trading_dialog.py`

当前已知事实

- GUI 侧 ExecutionController 不直接连接券商
- ExecutionWorker 是无界面的唯一券商写入进程
- 独立运行的 Worker 不依附 GUI 生命周期；但当前 GUI 内的持续跟踪分析会随窗口关闭而停止
- Worker 使用持久命令、心跳和对账状态
- `WorkerStore` 当前只有一个全局 `NEW_RISK` 短期授权租约，不能直接当成新的持久自动策略开关
- 当前新增风险采用短期授权租约，当前部分 Live 路径仍需要会话确认
- 当前 `codex_cli` 档案的 Live 自动提交被代码明确阻断，要求人工确认；这不是“可能存在”的行为
- 当前 Worker 协议只覆盖提交、取消入场、请求离场、刷新账户和对账；暂停、紧急清仓、加仓、减仓、反手与杠杆调整均需通过新增且受审计的 Worker 命令实现
- `execution.sqlite3` 的执行记录、路由声明和追加式事件是现有券商事实账本，后续不得另建第二套会竞争事实归属的订单账本
- 新产品决策要求重启后自动恢复全自动运行，因此授权模型需要重构，不可简单沿用短租约和 GUI 会话确认

### 既有设计文档

- `CONTEXT.md`
- `docs/LIVE_TRADING_DESIGN.md`
- `docs/FRONTEND_REDESIGN_PRD.md`
- `PA_Agent使用文档.md`

`docs/FRONTEND_REDESIGN_PRD.md` 原范围主要为 AI 模型设置与交易执行中心。本轮扩展到完整交易工作台和两个智能体，但原有账户身份、未知状态、减险权限和非 PA 仓位边界必须保留。

## 3. 总体工程目标

建立一条可持久恢复的自动交易管线

```text
OKX 已收盘 15 分钟 K 线
→ AnalysisRecord
→ SignalIntent
→ 确定性基础校验
→ SupervisorDecision
→ RiskApprovedPlan
→ PositionCoordinator
→ WorkerStore 持久命令
→ ExecutionWorker（唯一券商写入者）
→ OKX
→ 订单、成交、保护、仓位和账户快照
→ SupervisorDecision
```

必须保证

- 每根已收盘 K 线最多触发一次交易分析
- 两个智能体不直接写券商
- 所有券商写操作只有 ExecutionWorker 一条路径
- 所有动作先持久化意图，再调用券商
- 同一实际账户和品种的生命周期串行化
- 外部仓位不接管
- GUI 关闭和重启不影响后台运行
- 自动恢复不等于盲目重发未知订单

### 3.1 冻结的进程与权限拓扑

```text
GUI / ExecutionController ─┐
自动分析协调器             ├─> 受校验的持久命令 ─> ExecutionWorker ─> ExecutionService / OKX 适配器 ─> OKX
风险、监督、仓位协调模块   ─┘
```

- GUI、自动分析协调器、两个智能体、风险引擎和 `PositionCoordinator` 都是**无券商写权限**模块：不保存券商密钥、不创建客户端/适配器、不发送 API 请求。
- `ExecutionWorker` 是唯一可以创建 `ExecutionService` 和 OKX 适配器、持有券商写权限、提交/撤销订单、设置杠杆、维护保护、离场和对账的进程。
- `ExecutionController` 保留为 GUI 侧的计划创建、用户控制和命令入队入口；它不得变成第二个执行服务。
- `WorkerStore` 继续作为跨进程命令、心跳和互斥边界。新的持久自动策略状态必须有独立模型和原子校验，不能把旧的 30 秒新风险租约无限续期来冒充自动授权。
- 现有 `ExecutionStore` 保留为唯一逐笔订单/成交/保护/账户快照事实账本；新增生命周期表只能引用和汇总它，不能复制、覆盖或反向改写执行事实。

## 4. 建议新增模块

模块名可以在审计后调整，但职责不可混淆。

### 4.1 自动循环

建议

- `pa_agent/automation/trading_loop.py`
- `pa_agent/automation/scheduler.py`
- `pa_agent/automation/state_store.py`

职责

- 跟踪 XAU-USDT-SWAP 15 分钟 K 线收盘
- 持久记录最后处理的 K 线时间戳
- 触发完整分析或增量分析
- 防止同一 K 线重复触发
- 管理暂停、自动恢复和停止状态
- GUI 不存在时仍可运行

硬约束

- 自动分析协调器不得持有券商凭据或导入 `okx_client`、`okx_adapter`、`ExecutionService`。
- 每根 K 线以 `account_id + instrument + timeframe + closed_bar_ts + strategy_version` 作为唯一处理键；必须持久记录抢占、输入快照版本、成功/失败和最终信号 ID。
- 数据未确认收盘、行情/账户快照过期、重复键已完成、同一品种存在生命周期锁、暂停/停止或状态不一致时，只记录原因，不创建新风险命令。
- 重试只能恢复同一处理键中未完成的读取或分析步骤；订单命令必须复用已持久化的幂等键，不能由重试重复生成。

注意

当前行情刷新主要位于 GUI 流程。要实现关闭 GUI 后继续产生新信号，必须把自动分析调度从 `MainWindow` 生命周期中移出，或建立独立分析 Worker。仅有 ExecutionWorker 还不够，因为它当前负责执行和对账，不负责运行 PA 两阶段分析。

### 4.2 交易监督智能体

建议

- `pa_agent/agents/supervisor.py`
- `pa_agent/agents/supervisor_models.py`
- `pa_agent/agents/supervisor_prompt.py`
- `pa_agent/agents/supervisor_store.py`

职责

- 组装不可变监督快照
- 调用主模型和备用模型
- 校验结构化输出
- 保存模型、档案、提示词和输入快照
- 输出动作意图，不调用券商

当前系统只有一个全局活动模型档案；PA 与监督角色的主/备用映射、提示词版本和角色权限必须作为新增配置与持久化快照实现，不能复用一个“当前活动档案”后假定已经隔离。

建议输出动作枚举

```text
allow_entry
block_entry
hold
add_position
reduce_position
close_position
close_and_reverse
update_protection
pause_new_risk
```

每个动作至少包含

- action
- reason
- signal_id
- execution_id
- input_snapshot_digest
- requested_fraction
- requested_stop
- requested_tp1
- requested_tp2
- created_at
- model_profile
- fallback_level

### 4.3 仓位协调器

建议

- `pa_agent/execution/position_coordinator.py`
- `pa_agent/execution/protection_policy.py`

职责

- 区分空仓、同向持仓和反向持仓
- 将监督动作转为持久命令
- 串行执行加仓、减仓、统一保护和反手
- 确保旧仓未归零时不创建反向新增风险
- 确保任何保护更新都有可恢复的中间状态

`PositionCoordinator` 只把已校验的监督意图转成 Worker 命令并维护生命周期锁，不能构造券商客户端或直接操作订单。原建议的 `protection_manager.py` 容易形成第二条写入链，改为纯计算的 `protection_policy.py`；实际创建、替换、修复和撤销保护必须复用现有 `ExecutionWorker → ExecutionService → OkxAdapter` 路径。

### 4.4 风险引擎

建议

- `pa_agent/risk/position_sizer.py`
- `pa_agent/risk/leverage_manager.py`
- `pa_agent/risk/drawdown_guard.py`
- `pa_agent/risk/funding_flow_adjuster.py`

职责

- 使用 PA 子账户最新净值计算 10% 风险
- 使用合约价值、入场和止损计算风险张数
- 查询 OKX 最大可开张数
- 动态设置最低所需杠杆
- 计算同向加仓剩余风险预算
- 维护历史最高净值和 50% 回撤停止
- 读取资金流水并排除转入转出

风险模块只可使用已冻结的账户、合约和持仓快照，输出计算结果与拒绝原因；不得直接向 OKX 查询或写入。设置杠杆、查询最大可开张数和资金流水的具体接口语义，必须先以 OKX 官方文档和当前账户模式完成核验，再由 Worker 命令调用本地客户端扩展。

## 5. 数据模型扩展

## 5.1 SignalIntent

建议字段

- id
- analysis_record_id
- instrument
- timeframe
- closed_bar_ts
- direction
- entry_type
- entry_price
- stop_loss
- tp1
- tp2
- confidence
- invalidation
- analysis_digest
- created_at

## 5.2 SupervisorSnapshot

必须包含

- signal_intent
- current_kline_snapshot
- account_snapshot
- current_position
- open_orders
- protection_orders
- execution_events
- current_risk
- automation_state
- snapshot_digest
- captured_at

## 5.3 SupervisorDecision

建议使用严格枚举和禁止额外字段。

每条决定至少包含 `action`、目标 `signal_id` 或 `lifecycle_id`、`input_snapshot_digest`、`policy_revision`、`expires_at`、原因、角色档案、模型版本和备用层级。数值只允许出现在已定义字段中，且必须经过程序二次校验；快照、目标对象、生命周期锁或版本不匹配时拒绝执行。

## 5.4 RiskApprovedPlan

不要直接把模型输出变成现有 ExecutionPlan。

先建立经过账户和仓位计算的风险批准计划，确认

- 最新净值
- 10% 风险预算
- 风险张数
- 最大可开张数
- 最终张数
- 目标杠杆
- 全仓
- 净持仓
- 账户身份
- 账户快照时间
- 配置指纹

再生成不可变 ExecutionPlan。

`RiskApprovedPlan` 必须记录生命周期初始风险预算、已使用风险、费用/滑点假设、合约规格版本、向下取整后的数量和拒绝原因。它不是券商事实，只有 Worker 成功对账后才能产生执行事实。

## 5.5 交易生命周期

现有 ExecutionRecord 主要面向单次入场。支持加仓和统一保护后，建议增加父级生命周期。

建议

- `PositionLifecycle`
- `ExecutionLeg`
- `ProtectionVersion`

父级维护

- 当前净方向
- 总数量
- 加权均价
- 当前统一止损
- 当前 TP1、TP2
- 当前风险
- 活动 protection_version
- 当前状态

子事件记录

- 首仓
- 加仓
- 减仓
- TP1
- TP2
- 主动平仓
- 反手
- 保护调整

不要删除或覆盖历史事件。

新增表必须采用可回滚的增量迁移并保留已有 `execution.sqlite3` 历史。建议再新增两类独立状态，而不是把它们塞进执行记录：

- `AutomationPolicy`：用户期望状态、暂停/停止原因、`pause_until_utc`、策略版本、最后一次人工变更和生效条件。
- `BarProcessing`：K 线唯一处理键、处理权、冻结输入摘要、步骤状态、重试次数、关联信号和命令 ID。

`PositionLifecycle` 是多个逐笔执行事实的聚合，不是新的订单账本；它必须通过外键或稳定引用关联 `ExecutionRecord`、事件和保护版本。

## 6. 业务状态机

## 6.1 自动循环状态

建议

```text
STOPPED
STARTING
RECOVERING
WAITING_BAR
FETCHING_MARKET
ANALYZING
SUPERVISING
SIZING
EXECUTING
MONITORING
PAUSED_NEW_RISK
DRAWDOWN_STOPPED
EMERGENCY_STOPPED
NEEDS_ATTENTION
```

这只是自动分析协调器的状态机。`ExecutionWorker` 的进程健康、命令状态和最近成功对账时间必须单独展示，不能把“自动策略正在等待 K 线”误显示为“券商执行后台健康”。

## 6.2 持仓生命周期状态

建议

```text
FLAT
ENTRY_PLANNED
ENTRY_PENDING
OPEN
ADDING
REDUCING
UPDATING_PROTECTION
EXITING
REVERSING_EXIT
REVERSING_ENTRY
CLOSED
UNKNOWN
NEEDS_ATTENTION
```

未知状态不能被普通失败覆盖。

## 6.3 保护方案更新

必须使用版本化状态。

```text
旧保护有效
→ 持久化新保护意图
→ 提交或更新新保护
→ 验证新保护覆盖
→ 撤销旧保护
→ 将新版本设为当前版本
```

具体顺序应根据 OKX 支持的保护单能力和无裸仓原则审计后冻结。

不能先无条件撤掉旧保护，再等待大模型或网络恢复。

## 7. 关键业务规则

## 7.1 固定首发配置

- OKX
- Live 正式目标
- XAU-USDT-SWAP
- 15m
- PA 专用子账户
- USDT only
- cross
- net mode
- long and short
- no manual entry panel

## 7.2 动态仓位

```text
lifecycle_risk_budget = lifecycle_initial_confirmed_equity × 0.10
available_risk = lifecycle_risk_budget − confirmed_worst_case_loss_at_unified_stop
risk_quantity = available_risk ÷ (per_contract_stop_loss + expected_fees + conservative_slippage)
final_quantity = floor_to_lot(min(risk_quantity, max_order_size))
```

首仓创建生命周期时读取并冻结净值；同向加仓复用该生命周期预算，不得把最新净值 × 10% 重新当成独立加仓预算。反手只有在旧生命周期已确认归零后才创建新生命周期并读取新的净值。

所有数量按 `lotSz` 向下取整，不允许向上扩大风险。若小于 `minSz`、合约价值无法可靠换算、行情/账户/规格快照过期或最大可开张数查询失败，拒绝新风险。

## 7.3 动态杠杆

当前本地客户端没有设置杠杆的方法，需在 OKX 官方接口语义、账户模式和最小测试矩阵核验后，由 Worker 新增设置杠杆命令；不得凭记忆假设接口参数或成功语义。

顺序

1. 获取当前账户和品种允许的杠杆
2. 计算目标张数所需最低杠杆
3. 选择支持档位
4. 持久化杠杆调整意图
5. 调用 OKX
6. 读取并确认实际杠杆
7. 重新查询最大可开张数
8. 生成最终计划

只允许在该品种无 PA 仓位、无外部冲突仓位、无活动生命周期且无未决命令时调整杠杆。持仓期间第一阶段不自动改杠杆。杠杆结果未知时不能继续入场。

## 7.4 加仓

- 只在同方向 PA 信号和监督批准后执行
- 以整笔仓位到新统一止损的总风险为准
- 不允许每次信号机械加满购买力
- 加仓完成后更新加权均价和统一保护
- 现有 OKX 预检中「已有持仓直接阻断」需要改造成归属和动作感知逻辑
- 改造前必须先完成 PA 归属、生命周期锁、部分成交和统一保护替换的测试；不能仅删除“已有持仓”阻断来放开加仓

## 7.5 反手

- 监督动作必须为 close_and_reverse
- 先创建并持久化反手事务
- 请求旧仓退出
- 对账确认实际持仓为零
- 关闭旧生命周期
- 刷新净值、购买力和资金状态
- 重新审批新方向计划
- 创建新生命周期
- 任何一步未知时停留并对账，不跳过

## 7.6 止损和止盈

监督智能体可以收紧止损、移动保本和修改目标。

禁止单纯放宽止损扩大总风险。

若结构止损需要更远

- 重新计算允许数量
- 先减仓
- 确认减仓成交
- 再更新止损
- 总风险不得超过批准预算

## 7.7 分批方案

只允许以下预设

- 70/30
- 50/50
- 30/70
- 100/0
- TP1 小幅减仓后取消固定 TP2

比例换算必须符合合约张数步长。

## 8. 自动授权与恢复

当前 ExecutionController 使用短期新增风险租约和 GUI 会话确认。新产品决策要求

- Windows 启动后自动恢复
- GUI 关闭后继续产生新信号和开仓
- 不要求每次重启重新输入确认文字

因此需要把授权重构为持久化自动运行策略。

建议区分

### 持久目标状态

- RUN_ENABLED
- PAUSE_NEW_RISK
- DRAWDOWN_STOPPED
- EMERGENCY_STOPPED

### 运行条件

只有同时满足以下条件，Worker 才可执行新增风险

- 持久目标状态为 RUN_ENABLED
- 环境硬门允许
- 账户身份匹配
- 最近对账新鲜
- 没有外部同品种仓位
- 没有未知写操作
- 连续止损暂停未生效
- 回撤停止未生效
- 自动循环和模型状态可用
- 当前信号快照未过期

自动分析协调器在创建命令前先检查同一组条件；Worker 在原子认领命令后必须使用命令携带的策略版本、快照摘要和生命周期锁再次检查。两次检查之间任一条件变化、策略被暂停或命令过期时，命令只能转为拒绝/需关注，不能发送券商请求。

持久目标状态可以自动恢复，但环境硬门、账户身份和未知订单不能被绕过。它必须有明确的启用/暂停/紧急停止审计事件和单一生效版本，不能与旧短期租约并存为两个相互竞争的新风险授权源。

当前 `codex_cli` 的人工确认阻断不得直接删除。只有持久自动策略、角色档案、环境硬门、完整审计和回归测试已替代并覆盖该安全边界后，才可通过统一产品策略决定是否允许该档案自动交易；不允许按模型供应商绕过门禁。

## 9. GUI 改造

## 9.1 App Shell

新增稳定左侧导航和堆叠页面。

建议页面类

- `TradingWorkbenchPage`
- `SignalsPage`
- `PositionsPage`
- `ReviewPage`
- `StrategiesPage`
- `SystemStatusPage`
- `SettingsPage`

现有主窗口只负责窗口、导航和全局状态，不继续堆叠所有业务控件。

## 9.2 Read Model

GUI 不直接拼接多个数据库和线程状态。

建议建立

- `WorkbenchReadModel`
- `SystemHealthReadModel`
- `TradeReviewReadModel`

统一读取

- 行情
- 分析
- 监督
- Worker
- 执行
- 账户
- 风险
- 自动状态

每个读模型字段必须带来源、采集时间、状态版本和“计划/已确认”语义。智能体建议、风险批准计划和目标设计状态只能标记为计划；只有 Worker 对账确认的订单、成交、仓位和保护才可标记为已确认。尚未实现的目标能力在 UI 中必须显示“规划中”并禁用，不能用静态假数据伪装可用。

## 9.3 原有七个标签

原有

- 实时
- 决策树
- 决策树可视化
- 决策
- 未来走势预期
- 原始
- 调试

不应继续作为工作台右侧七个平级标签。

迁移到

- 工作台只显示核心结果
- 信号与分析页显示完整过程
- 原始和调试放入开发详情区域
- 决策树可视化保留但降级为详情，不自动抢占主视图

## 9.4 关闭窗口

实现三个选项

1. 仅关闭界面，后台继续自动交易
2. 停止自动交易并关闭
3. 取消关闭

停止自动交易并关闭只改变持久目标状态，不允许直接杀死 Worker 导致已有持仓失去管理。

需要区分

- 停止新增风险
- 停止 GUI
- 停止 Worker
- 紧急清仓

当前 GUI 关闭会停止 GUI 内持续跟踪分析；三个选项是独立自动分析协调器和持久策略状态完成后的目标交互，不得提前在现有界面宣称后台可继续产生新信号。

## 10. 暂停和紧急清仓

## 10.1 暂停新开仓

持久命令需要覆盖

- 原子设置 PAUSE_NEW_RISK 并使后续新风险命令失效
- Worker 撤销 PA 未成交入场单
- 禁止新开仓和加仓
- 分析继续
- 已有持仓继续管理
- 暂停期间信号保存为未执行
- 重启后保持

部分成交或取消结果未知时，必须优先对账并维持/修复已有保护；暂停不得制造裸仓。

## 10.2 恢复

- 不执行暂停期间旧信号
- 获取最新已收盘 K 线
- 立即新分析
- 新监督审批
- 新风险计算
- 新计划

## 10.3 紧急清仓

建议新增 Worker 命令

```text
EMERGENCY_FLATTEN
```

流程

- 持久设置 EMERGENCY_STOPPED
- Worker 取消 PA 入场挂单并拒绝后续新风险
- 对 PA 已确认管理仓位执行退出并持续对账到仓位归零
- 确认归零前保留有效保护；确认归零后才撤销剩余 PA 保护单
- 不操作外部仓位
- 未知状态不盲目重复
- 完成后不自动恢复

## 11. 外部仓位和独立子账户

正式部署要求使用 PA 专用 OKX 子账户。

仍需实现归属核对

- 本地 execution
- 账户身份
- `clOrdId`
- OKX `ordId`
- 成交明细
- 当前剩余数量
- 保护单覆盖

发现以下情况时停止相同品种新增风险

- 本地无 execution 的仓位
- 客户订单号为空
- 客户订单号属于其他系统
- PA 成交链与实际净仓数量不一致
- PA 来源疑似存在但本地链路不完整

不提供自动接管按钮。

## 12. 连续亏损与回撤

## 12.1 完整止损

```text
full_stop = confirmed_lifecycle_final_net_loss >= lifecycle_initial_risk_budget × 0.80
```

计数

- full_stop 为真，计数加一
- 下一笔非 full_stop，计数清零
- 三次连续 full_stop 后持久化暂停原因和 `pause_until_utc`，最早到下一个 UTC 日才可尝试解除
- 暂停只阻止新增风险

最终净亏损必须按完成的生命周期计算，并纳入已确认手续费、资金费和可解释的结算调整；数据不完整时不能把该交易计为“非完整止损”来错误清零。

## 12.2 回撤停止

- 保存资金调整后的高水位
- 当前净值相对高水位回撤达到 50%，进入 DRAWDOWN_STOPPED
- 必须人工明确恢复
- 重启不清除

## 12.3 资金流水

通过经 OKX 官方文档核验的资金和账单接口识别转入转出。

需要持久化

- 原始流水 ID
- 类型
- 币种
- 数量
- 时间
- 是否属于外部资金变动
- 对高水位的调整
- 去重状态

资金流水分页、重复、延迟到达、游标中断、账户范围与币种不一致必须测试；任意一项无法可信读取时，冻结在最后可信水位并告警，不自动改写高水位。

## 13. 开发顺序

### 阶段一，基线和只读工作台

- 冻结 HEAD
- 审核当前 OKX 行情链
- 建立 Read Model
- 冻结页面—字段—状态—交互契约并采集现有关键界面截图
- 完成左侧导航和高保真工作台
- 不改变券商写路径

### 阶段二，监督智能体和自动循环

- 建立独立监督档案
- 完成主模型、备用模型和确定性兜底
- 把分析调度移出 GUI
- 建立 K 线去重和持久自动状态

### 阶段三，动态仓位、杠杆和持仓协调

- 风险引擎
- 动态杠杆
- 同向加仓
- 统一保护
- 平仓反手

### 阶段四，持久恢复和停止机制

- 自动恢复授权
- 暂停新开仓
- 紧急清仓
- 连续止损
- 回撤停止
- 资金流水

### 阶段五，完整复盘和扩展准备

- 生命周期复盘
- 模型和提示词版本
- 多品种、多策略、多账户接口预留

每一阶段完成后可以集成，但不得以“Live 架构可用”替代真实验证。自动测试、可控的 OKX Demo 和最小额度真实环境验收是上线门槛，而不是永久产品形态；任何券商写能力扩大前都必须先完成对应的失败、并发、恢复和对账测试。

## 14. 测试矩阵

## 14.1 OKX 行情

- 现有 `test_okx_source.py` 全部通过
- XAU-USDT-SWAP 15m
- 品种在线验证
- 形成中 K 线和已收盘 K 线
- 收盘标记非法
- 时间倒序
- 重复 K 线
- 同一已收盘 K 线只触发一次
- 网络断开和恢复
- GUI 关闭后继续触发
- 自动分析协调器无券商凭据/客户端导入的静态检查

## 14.2 两个智能体

- 独立档案
- 主模型成功
- 主模型失败后备用模型成功
- 两个模型都失败后确定性兜底
- 输入快照在重试过程中不变化
- 结构化输出非法
- 过期信号拒绝
- 模型切换不改写已运行交易

## 14.3 仓位计算

- 10% 风险
- 最大购买力更小
- lotSz 向下取整
- 动态杠杆
- 杠杆调整失败
- 杠杆结果未知
- 加仓剩余风险
- 止损放远前先减仓
- `minSz`、`ctVal`、费用和滑点导致拒绝的边界
- 同一生命周期加仓不得重置 10% 预算

## 14.4 持仓协调

- 空仓开多
- 空仓开空
- 同向继续持有
- 同向加仓
- 加仓后统一保护
- 反向仅平仓
- 反向平仓并反手
- 旧仓未归零
- 部分成交
- TP1 后重新管理
- 保护更新未知
- 同时到达的加仓、减仓、反手、暂停与紧急清仓只能由一个生命周期锁决定顺序
- 任一非 Worker 模块尝试创建适配器或写券商必须失败

## 14.5 自动恢复

- GUI 关闭
- Windows 服务重启
- Worker 重启
- 分析 Worker 重启
- 活动订单
- 活动持仓
- 未知提交
- 保护单缺失
- 外部仓位
- 数据库迁移和备份
- 不重放旧 K 线、暂停期间信号或未知券商影响命令
- 自动策略状态、Worker 心跳、最近对账和账户归属任一不满足时只恢复监控/保护/退出

## 14.6 暂停和停止

- 暂停取消入场挂单
- 暂停不影响已有仓位
- 暂停期间分析继续
- 恢复后新分析
- 连续三次完整止损
- 非完整止损清零
- UTC 日恢复
- 50% 回撤停止
- 转入转出不触发假回撤
- 紧急清仓不碰外部仓位
- 紧急清仓确认仓位归零前不撤销最后有效保护

## 15. 硬验收

以下任何一项失败都不能宣称交付完成

1. OKX 行情链被误做成新增功能，而不是审核现有实现
2. 同一根已收盘 K 线可能重复下单
3. 监督智能体可以直接调用券商
4. GUI 关闭后停止产生新信号
5. 重启后仍必须逐笔人工确认
6. 已有外部仓位可能被 PA 自动操作
7. 加仓后存在多套相互冲突的保护
8. 反手旧仓未归零就开新方向
9. 仓位数量使用可用余额代替最大可开张数
10. 风险计算没有使用最新子账户净值
11. 止损放宽后总风险超过 10%
12. 模型失败导致裸仓或停止订单对账
13. 未知券商结果被自动重发
14. 暂停新开仓仍保留未成交入场单
15. 恢复时执行暂停期间旧信号
16. 紧急清仓可能平掉非 PA 仓位
17. 资金转出被误判为交易亏损
18. 50% 回撤停止可被重启绕过
19. 主界面仍保留人工开仓入口
20. 1920 × 1080 下核心状态需要纵向滚动才能看清
21. 除 ExecutionWorker 外存在任何可写券商的模块、进程或凭据路径
22. 新生命周期表替代、覆盖或与现有执行账本竞争订单事实
23. 动态杠杆、资金流水或合约规格在未完成官方接口核验时被宣称可用
24. UI 把计划、模型建议、目标状态或静态假数据展示为已确认的券商事实

## 16. 交付物

开发完成时必须提供

- 代码变更清单
- 数据库迁移说明和备份证据
- 新旧页面映射
- 现有页面截图清单，以及页面—字段—状态—交互映射
- 自动循环状态图
- 监督智能体输入输出契约
- 仓位计算示例
- 加仓和反手时序图
- 暂停与紧急清仓时序图
- 测试命令与真实结果
- 已知边界
- 未完成项
- “当前已实现 / 目标待实现 / 已验证上线”的能力清单
- 不得把既有失败误报为本轮回归
