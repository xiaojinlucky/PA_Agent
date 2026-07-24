# PA_Agent Codex 交接总账

更新时间：2026-07-24
用途：供网页版 GPT 通过 GitHub 读取项目事实、冻结需求、编写工单和硬验收；本地 Codex 再按真实 Windows 环境、skills、memory、进程和测试结果适配执行。

## 1. 交接范围

本文件把本项目当前可提交的事实集中到一个入口，但不复制机器全局配置、Codex 登录信息、API Key、Cookie、数据库、日志或运行态文件。

本轮整理来源包括：

- Codex 对话：`019f89bf-b250-74d3-a127-d5eac14b45e2`、`019f8b6c-0c66-7133-b614-d0a239661145`。
- 项目根 `CONTEXT.md`、`lessons.md`、`README.md`。
- 项目级规则：`docs/CODEX_PROJECT_RULES.md`。
- 需求和修改账：`docs/REQUIREMENTS_CHANGELOG.md`。
- 历史移交材料：`docs/LOCAL_EXECUTION_CONTEXT.md`、`docs/GPT5_6SOL_HANDOFF.md`、`docs/VALIDATION_EVIDENCE.md`。
- 当前统一路线图及安全边界：`docs/ROADMAP.md`、`docs/SAFETY_INVARIANTS.md`、`docs/BASELINE_AUDIT.md`、`docs/READ_MODEL_MAPPING.md`。
- 规划归档：`docs/prd/` 下的产品 PRD、Stitch 设计提示词和工程移交/硬验收。

对话原文不写入仓库；仓库只保存从对话中确认并经过本地代码/文档核对后的决定。不能从仓库证明的内容必须标为“待本机核验”。

## 2. Git 发布边界

发布前本地基线：

- 仓库：`xiaojinlucky/PA_Agent`
- 目标远程：`origin = git@github.com:xiaojinlucky/PA_Agent.git`
- 可见性：GitHub 实时核验为 `public`
- 公开性是用户明确的长期决定：网页版 GPT 可以读取，BioMNI 读取仓库也依赖公开访问；给外部总控的指令必须明确这一点，不得要求把仓库改回私有。
- 默认分支：`main`
- 发布前 `main` 与 `origin/main`：`1a04c144f810ffb486280ed8a1875ff0130bb070`
- 上一次交接包发布基线：`38876644430302f0e2ac3310f072b31f95252469`；本轮后续改动的本地/远程 SHA 必须重新实时核对，不能沿用该旧值。
- 当前 GitHub 身份：`xiaojinlucky`；权限：`ADMIN` / `push`

本次发布包包含：

- 当前工作区中属于 PA_Agent 的代码、测试、配置说明、项目规则、上下文、需求账、路线图、验收文档和 PRD 归档。
- 上一轮 OKX Demo/配置改动和本轮只读工作台改动，因为用户要求把本项目当前内容和已完成修改统一交接，均纳入同一发布范围。

明确排除：

- `config/settings.json`、`.env`、凭据、私钥、Token、Cookie、数据库、WAL/SHM、日志、原始行情、交易/账户运行记录、模型文件和临时目录。
- 未经本轮确认的其他项目、其他仓库、上游仓库和机器级配置。

项目规则禁止在混合工作区使用 `git add .` 或 `git add -A`；发布时必须按最终文件清单显式暂存，并以 staged snapshot 为准做密钥和大文件检查。

## 3. 用户已确认的方向

- PA_Agent 作为独立项目维护，不与其他量化项目共享执行层。
- 第一目标是黄金研究和可验证的交易执行链，但 OKX 品种必须动态支持，不能把黄金代码永久硬编码为唯一品种。
- AI 只生成结构化交易意图；账户、品种、方向、数量、杠杆、保证金、TIF、客户订单号、风控和幂等由确定性代码负责。
- 全自动的目标是日常不逐笔等待人工确认，不等于取消三道刹车：首次真实交易人工启用，断网/账户身份不一致停止新增风险，订单未知先对账且禁止盲目重发。
- 外部模型只能做总控和规划；它看不到本机 skills、memory、环境变量、Windows 服务、未提交工作区和真实券商状态。外部工单必须先由本地 Codex 适配，再执行。
- 前端改版必须先有 PRD 和状态真值，再进入 Stitch/Pencil/Figma 设计；未实现后端能力只能显示“规划中”，不能画成可点击的真实交易功能。
- 开发采用剃刀原则：先实现最小完整链路，问题尽早暴露，不用静默降级、猜字段或大规模预留抽象掩盖缺口。

## 4. 已发现的问题类别

完整历史记录见 `docs/REQUIREMENTS_CHANGELOG.md`。当前需要外部总控重点关注的类别：

| 类别 | 已发现的真实问题/约束 |
|---|---|
| 分析与执行边界 | `AnalysisRecord` 不是完整订单事实；AI 结果不能直接下单；计划、已提交、成交、保护和对账必须分层。 |
| 账户与路由 | Longbridge paper/综合/日内必须独立；不能按档案名代替真实账户身份；未知、超时、部分成交时禁止跨账户重发。 |
| OKX | 现货与永续产品语义不同；`XAU-USDT-SWAP` 规格、数量、杠杆、持仓模式和保护参数必须在写前动态读取；不确定 HTTP 结果不能直接当确定拒绝。 |
| 订单与账本 | 意图必须先持久化；客户订单号唯一；写入结果未知先对账；本地保存失败不能触发同一进程重发；终态缺成交数量/价格时不能伪造零、均价或 PnL。 |
| 保护与恢复 | 保护缺失、保护单未知、部分成交、撤单拒绝、重启和外部仓位都要保留活动状态并停止危险动作；不能自动补发未知保护单。 |
| 并发 | GUI、自动循环和 Worker 不能争抢券商写权限；租约、持久路由占用、心跳和停写标记必须跨线程/跨进程一致。 |
| 前端 | 当前主窗口是旧工作台；目标是 7 项左侧导航和真实状态分层；任何目标状态稿都必须标记“规划中/当前未实现”。 |
| 验证 | Windows pytest 临时目录可能清理失败；全仓历史失败不能被少量定向测试覆盖；测试退出码、断言结果、运行态和远程 SHA 必须分开报告。 |

## 5. 已完成修改摘要

### 5.1 基础与 AI

- 增加/修复 Longbridge 只读行情、Token 状态和数据源工厂接入。
- 支持多套 AI 模型档案、真实连接测试、模型能力目录、Thinking/推理强度/速度/上下文元数据和安全切换。
- Codex ChatGPT 订阅通过官方 CLI 登录；正式两阶段分析不复用历史，追问使用官方持久线程。
- 凭据、配置、日志、分析记录、数据库和临时文件保持仓库外或 Git 忽略。

### 5.2 执行安全

- SQLite 执行账本、意图先落盘、唯一客户订单号、未知状态对账、重启恢复和会话停写。
- `ExecutionController` 只创建计划、管理权限租约和持久命令；独立 `ExecutionWorker` 是唯一券商写入者。
- Longbridge paper/综合/日内和 OKX 现货/永续使用独立路由与账户语义。
- 动态读取 OKX 产品规格、持仓模式、杠杆、资金、数量和保护参数；支持 OCO/主动离场/永续 PnL 读取。
- 对抗审查已经覆盖会话写门、HTTP 不确定结果、实际账户身份、严格额度、SQLite 路由竞争、终态成交核验、保存失败防重发、资金币种边界、私有身份校时和 UNKNOWN 停写。
- 保护单未知结果使用客户算法订单号查询精确结果、未触发列表和历史分页；查不完整就保持未知，禁止自动补发。

### 5.3 OKX Demo 与当前工作台

> 本节的运行态字段来自上一轮现场快照；本轮 WO-S2A-01 只重新验证 Demo/离线代码链路，不能据此断言当前进程、账户、仓位或券商状态。

- 旧版 5 分钟 Demo 实验、7 笔限价单和生命周期 canary 已有历史证据；它们不能被解释成当前 15 分钟产品配置或实盘绩效。
- 当前本地安全配置字段（不代表交易授权）：数据源 `okx`、品种 `XAU-USDT-SWAP`、周期 `15m`、决策 stance `extreme_aggressive`、通用决策置信度 `40`；Campaign 运行器强制执行门槛 `40`、OKX Demo、入场/主动离场均为市价单。普通交易界面已支持入场和主动离场分别选择限价、限价+滑点、市价，滑点单位为 bp。
- 新增 `pa_agent/gui/read_models.py`：只读组合行情连接/订阅、Worker 心跳与对账、账户快照和执行账本；不发网络请求、不建计划、不入队命令、不写券商。
- `AppContext` 复用现有 `ExecutionStore` 和 `WorkerStore`；主窗口状态条显示“已确认/计划/规划/未知”，数据源事务切换和回滚会同步读取对象。
- 当前只完成读取层基础和状态条；完整导航页面、订单/保护/分析统一读取模型、截图和 OKX 端到端验收仍未完成。

### 5.4 WO-S2A-01 双智能体 Demo 入场门控

- `pa_agent/agents/supervisor_models.py` 定义严格 `SupervisorDecision`、frozen 输入快照和可追溯结论记录；只允许 `allow_entry` / `block_entry`。
- `pa_agent/agents/supervisor.py` 读取已验证的监督主档案和可选备用档案。若备用档案明确绑定，主模型失败时备用模型使用完全相同输入快照；两者失败、未配置备用或活动 execution 已存在时确定性 `block_entry`。
- `pa_agent/records/supervisor_writer.py` 按 `campaign_id + closed_bar_ts_open_ms + analysis_digest` 原子保存，重启复用同一结论，冲突和损坏直接失败。
- `pa_agent/okx_demo_campaign.py` 在 `prepare_analysis()` 前接入监督门；拒绝不创建 execution、不入队命令，放行继续沿用既有 Controller/Worker。新增提交后崩溃恢复，避免同一 K 线重建第二笔 execution。
- 设置增加 PA 主/备、监督主/备四个档案绑定字段。当前 Campaign 使用 PA 主档案和已配置的监督主档案；监督备用需在本机设置明确绑定，PA 备用档案先独立验证，完整 PA 分析备用切换属于后续工单。
- `WO-S2A-01` 已完成：本轮未修改 `pa_agent/execution/`，未触碰 OKX Live。

### 5.5 WO-RISK-02 正式风险定仓

- 新增 `pa_agent/risk/sizing.py`：纯 `Decimal` 风险计算器，输入账户权益、风险比例、PA 入场价/止损价、方向、`ctVal`、`ctMult`、`lotSz`、`minSz`、最大可开张数、费用率和滑点率，输出向下取整后的目标张数或明确 `RiskCalculationFailure`。
- 单张最坏损失明确包含：入场到止损的价格损失、入场/止损双边费用、入场/止损双边保守滑点；当前 Demo 冻结 10% 风险比例、`0.0005` 费用率、`0.0010` 滑点率，后两项是定仓假设，不冒充账户实时成交费。
- `pa_agent/okx_demo_campaign.py` 在监督和 `ExecutionController.prepare_analysis()` 前读取当前 Demo 权益、`XAU-USDT-SWAP` 动态合约规格及多空最大可开张数；没有 PA 入场/止损时只做私有只读预检，不计算数量、不回退旧固定数量。
- 风险结果写入本次内存执行路由的 `quantity`，由现有 `ExecutionController → ExecutionWorker` 链路持久化到 `ExecutionPlan.quantity`；AI 返回的 `quantity`、杠杆或路由字段仍被忽略。`ExecutionWorker` 和 `ExecutionState` 未修改。
- 若本次会话已经持有新增风险租约，风险数量写入后会先撤销旧租约、再按新配置指纹重新授权；旧数量的计划不能借用新数量的租约提交。
- 已完成离线长短方向、费用/滑点、最小数量、最大数量限制、缺失输入、真实 Controller/Worker + FakeAdapter 命令链测试；没有连接真实券商、没有发送订单。

### 5.6 本轮 Demo 运行修复与真实闭环证据

- 现场发现：旧 Campaign 在唯一 `NEW_RISK` 租约距离到期只剩数秒时启动，`ExecutionController.arm()` 返回“已有其他会话持有租约”，运行器把它当成致命错误退出，导致 `0` 根分析。这是租约竞争的短暂运行故障，不是 PA 信号“不下单”。
- 新增 `NewRiskLeaseUnavailable` 明确错误类型；只有该类型会被 OKX Demo Campaign 转成可记录、可重试的暂时错误，并把首次启门也放入循环。共享 env 未开启、Worker 对账陈旧、券商预检失败和未知写结果不会被这条重试路径吞掉。
- 用户已明确授权本轮进行 Demo 实际交易。`okx_demo_lifecycle_canary` 通过真实 `ExecutionController → ExecutionWorker → OKX Demo` 链路完成一次 `15358` 张市价入场、两笔原生保护和主动离场；execution `3f57bed9-6997-58ff-9e6f-e3b6cd1554c2` 最终 `closed`，券商只读回查净仓/普通挂单/待生效算法单均为 `0`。该记录不属于策略绩效。
- 修复后的 Campaign 已重新启动并完成一根真实 `15m` 分析；模型因低位支撑、十字星和空头跟随不足返回 `no_order`，没有伪造订单。后续必须继续现场核验，不得用这一根“不下单”宣称策略已产生绩效。

### 5.7 入场 / 主动离场方式与 Demo 模式验收

- `ExecutionPlan` 现在持久化 `entry_order_mode`、`exit_order_mode`、分析时捕获的 `entry_atr` 以及入场/离场 ATR 滑点倍数；旧计划缺少这些新字段时按默认配置读取。
- `limit` 不改 PA 产生的入场限价；`limit_with_slippage` 按分析时 ATR14 × 倍数把入场价或主动离场最新价移动到成交侧；`market` 直接发送市价单。入场和主动离场字段互不耦合，可组成 9 种组合；`signal` 仅是旧配置兼容项。选择 ATR 滑点而记录没有 ATR14 时直接阻断，不退回固定基点或默认价。
- OKX 原生止盈止损仍由交易所算法保护托管，主动离场字段不重写保护价。Demo canary 支持 `--entry-mode`、`--exit-mode`、`--entry-slippage-atr`、`--exit-slippage-atr`，可逐组合验证真实 Demo 下单构造与成交回读。

### 5.8 PA 原始资料与多周期首轮实验

- 本轮按用户提供的 `E:\QQ文件\价格行为学资料` 做了本地只读核查，并将可复核结论记录在 `docs/PA_METHOD_SOURCE_REVIEW.md`；原始 PDF、识别文本、数据库、日志和凭据没有复制进公开仓库。
- 原始资料支持“高周期给背景、主周期做当前判断”的嵌套视角，也提醒同时查看过多周期会分散注意力。因此首轮只把 `15m` 主周期接到同一 OKX 品种的 `1h` 和 `4h` 已收盘背景，生成薄的 EMA20、ATR14、收盘相对均线、方向标签和时间，不把完整高周期 K 线表塞进模型。
- 高周期文本写入 `AnalysisRecord.htf_text`，进入阶段一和阶段二提示词；它是背景证据，不是置信度、数量或价格的硬闸门。日线暂不接入，Session 暂不扩展，成交量维持现状。普通 GUI 行情源尚未自动读取高周期，本轮真实运行路径先覆盖 OKX Demo Campaign。
- 本轮定向测试 `209` 通过、`0` 失败；执行主链回归 `97` 通过、`0` 失败。新版 Campaign 已启动，但被本机 GUI 会话持有的 `NEW_RISK` 短租约挡在首轮分析前，尚未产生本轮新的真实订单；这证明代码路径和离线接缝，不等于 Demo 已经交易，租约释放后仍需单独回传真实运行证据。

### 5.9 2026-07-24 现役 10m 配置与 WO-EXEC-03 Demo-S

- 本节取代 5.3、5.6、5.8 中关于“当前配置/当前运行态”的历史快照；历史订单和旧 Campaign 仍只作审计证据。
- 现役配置为 `OKX / XAU-USDT-SWAP / 10m / extreme_aggressive / min_trade_confidence=20`，入场与主动离场均为 `limit_with_slippage / 0.50 × ATR14`。OKX 没有原生 10m，主周期由两根连续、同一 UTC 10 分钟边界且均已收盘的官方 5m K 线严格聚合；高周期继续使用原生 1h/4h 薄背景。
- GUI 已把行情源、执行品种、Demo 环境、进出场模式、ATR 倍数和高周期背景贯通并持久化；计划构建器拒绝把 MT5 价格用于 OKX 执行。
- `max_size_exceeded` 不再让 Campaign 退出，而是记录明确风险阻断并继续下一根已收盘 K 线；风险数量仍必须完整通过 10% 止损风险公式和实时最大可开数，不允许静默截断。
- `controlled_reproducible` 是显式输入模式，不是自动放行。受控阶段一与方向、三价、真实 10m K 线、ATR 和风险容量保持一致，原自然分析只留在 `base_*` 审计字段；真实 SupervisorGate 仍可阻断。
- 2026-07-24 Demo-S 真实执行证据：SupervisorGate 返回 `allow_entry`；execution `328d0f7f-fef6-5e5e-bd39-dad92a66512b` 经 Controller→Worker→Service→OkxAdapter 做空 `14921` 张，耐久事件从 `ready→submitting→entry_pending→protecting→open→exit_pending→closed` 完整推进；入场订单 `3769893223932182528` 成交，建立两笔原生保护，主动离场订单 `3769895836211822592` 成交，账本最终 `closed`、剩余数量 `0`。收口只读回查为空仓、无普通挂单、无条件/OCO 挂单；这不是自然策略绩效，也不证明 Live 可用。
- 九种入场/离场组合已通过生产链离线 Fake client 测试；ATR 2→4 价差翻倍、定仓保守滑点率不改变委托价、保护价与主动离场模式隔离均有硬验收。Demo-A/C 的后续真实收口见 5.10。

### 5.10 2026-07-24 WO-EXEC-03 Demo-A/C 收口

- Demo-A execution `4d118cb0-ec65-5340-a8c9-9f9d8583bd1e` 经 Controller→Worker→Service→OkxAdapter 以 `limit → market` 完成真实 OKX Demo 往返：`27440` 张限价入场订单 `3770119237559996416` 成交，两笔原生保护 `3770119507314909184`、`3770119717130772480` 建立后撤销；主动离场按实时 `maxMktSz=20000` 分成市价订单 `3770133355453042688`（`20000` 张）与 `3770134044895956992`（`7440` 张）成交。账本最终 `closed`、剩余 `0`、已实现盈亏 `-73.5046000000000050`。
- Demo-C execution `89e45ac4-4bee-58eb-9686-7ac36f90db79` 经同一生产链以 `market → limit` 完成真实 OKX Demo 往返：`15057` 张市价入场订单 `3770140719510016000` 成交，两笔原生保护 `3770140997116665856`、`3770141214280949760` 建立后撤销；限价主动离场订单 `3770160697147740160` 累计成交 `15057` 张。账本最终 `closed`、剩余 `0`、已实现盈亏 `-117.0923`。
- 实证暴露并修复三项真实边界：市价减仓按实时单笔上限分块；部分成交用固定提交前基线减券商累计成交量；保护撤单未知只有在券商确认同一保护仍 `live` 后才生成新意图并跨轮复核。没有把限价改市价、没有截断风险数量、没有盲重发未知写入。
- 定仓复核用同一生产公式精确重现两笔数量：Demo-C 的入场前空仓权益 `8714.71942600003` 直接得到 `15057`；Demo-A 用首份成交后账户现金加已扣入场费用重建当时权益 `8899.30154170003`，得到 `27440`。Demo-A 的权益属于账本可验证重建值，不冒充直接快照；历史两笔的确切 `maxBuy/maxSell` 未耐久保存。后续 Campaign 与受控 Demo 记录已增加完整 `risk_sizing` 快照，持久保存权益、风险预算、合约规格、容量和目标数量。
- 历史容量的确切数字不能倒填，但两笔均有 `preflight_passed` 和真实成交；基线代码只有在真实方向 `maxBuy/maxSell >= plan.quantity` 时才允许通过预检。结合风险公式独立算得的 `27440/15057`，足以证明当时 `min(risk_quantity, real_max_size)` 没有发生容量截断。
- 最终只读对账：非零仓位 `0`、普通挂单 `0`、待生效 OCO `0`、活动 execution `0`。Demo-S/A/C 合计恰好覆盖入场与主动离场的 `limit_with_slippage`、`limit`、`market` 三种模式；canary 不进入自然 PA 策略绩效，也不证明 OKX Live 可用。

### 5.11 2026-07-24 USDT 账户内换币与提交前风险复核

- 用户现场确认本次余额增加来自其他资产换成 USDT/账户内转换。OKX 只读复核显示最新换币账单为 `type=2/subType=1`、`USDC-USDT`，不是外部转入；历史外部转入账单与本次事件分开。内部换币不计入交易盈亏或外部资金流高水位。
- 修复 `ExecutionService.monitor_once()` 无活动 execution 时停止刷新账户的问题；工作台改读准确的 `okx-demo/okx-live` 路由，快照超过 90 秒变为 `UNKNOWN`。OKX 快照保留账户 `totalEq`、USDT `eq/cashBal/availBal/frozenBal` 和 `uTime`，Campaign 明确使用 USDT `eq` 作为 10% 止损风险基数。
- Campaign 在提交前重新读取定仓。USDT 权益、风险预算、数量、止损距离、合约容量等风险输入任一变化，旧 `READY` execution 直接作废，不提交旧数量；不自动把超过 OKX `maxBuy` 的数量截断为“看似通过”。
- 目标套件 `168/168` 通过，包含无活动账户刷新、正确 Demo 路由、陈旧快照、快照字段、内部换币分类和余额变化后旧计划失效。生产资金流账本游标、高水位耐久化及恢复接线仍属阶段 4，未在本轮假装完成。管理员已成功重启 Worker，PID `536` 于 `2026-07-24T03:40:46Z` 启动；在活动 execution=0 时连续写入 `okx/okx-demo` 新快照，最新 `snapshot_id=7341`，心跳和对账错误码为空。

### 5.12 2026-07-24 原始资料第二轮：主周期唯一交易主线

- 本轮重新对照 `E:\QQ文件\价格行为学资料` 的 Brooks 识别文本与 `docs/PA_METHOD_SOURCE_REVIEW.md`：资料支持少量相关周期的嵌套解释，但提醒过度切换周期会分散主图判断；高周期可帮助解释结构和关键位置，主周期仍负责当前交易。
- 修正了提示词中的隐性冲突：二元决策、市场诊断、K线信号、上涨通道、极速上涨/下跌和二次入场文件统一规定，`10m` 主周期的已收盘 K 线独立裁定 `direction`、信号棒、入场、止损、目标和下单；`1h/4h` 只能进入 `htf_context`/`risk_warning`，不要求共识，不因高周期冲突自动 wait、降置信度、改数量或改价格，也不切换低周期拼接触发理由。
- `pa_agent/data/multi_timeframe.py` 的高周期薄文本新增粗粒度 `结构` 与 `位置` 标签；不发送整套高周期 K 线，不生成第二套交易计划。GUI 与 OKX Demo 仍通过同一行情源读取已收盘 `1h/4h`，未新增 Session 或成交量硬闸。
- 本轮多周期、GUI、MT5/OKX 来源、Demo Campaign、提示词组定向测试 `107/107` 通过、0 失败；其中新增提示词审计会直接拦截“高周期确认=高可靠性”“窗口共识才可评估三价”等旧句式。原始 PDF/OCR 未复制到公开仓库。
- 未完成：完整原始资料的逐篇人工语义审计、Session 标签实验、成交量实验、日线背景、基于自然 PA 信号的策略绩效评估；本轮只完成最小可执行多周期契约和硬回归，不把它们写成已证明的盈利能力。

### 5.12 2026-07-24 WO-RUN-06：资金流、高水位、50% 停止与动态定仓

- 外部工单原本只描述目标，没有看到本机 `ExecutionWorker` 单写入者、`WorkerStore` 控制库、OKX `totalEq` 现有快照字段和本机 10% 止损定仓函数；本地实现据此收敛为最小接缝，不改交易账本，不绕过 Worker，不把余额变化转换成交易信号。
- `pa_agent/risk/runtime.py` 负责把 OKX 总权益和已分类资金流转换为持久状态；`WorkerStore` v4 在 Worker 单例锁内迁移并保存状态。连续扫描边界使用最近一次任意 OKX 账单 ID、时间和扫描时间，不再把最后一笔外部转账冒充分页游标；旧转账自然滑出七天窗口不误停，真正断扫满七天才关闭新增风险。
- `ExecutionService.submit()` 在 OKX 新风险券商预检前刷新运行态、执行 `>=50%` 停止闸门，再调用 `OkxAdapter.calculate_risk_size()` 以最新 `totalEq`、真实止损和 `maxBuy/maxSell` 计算目标数量；动态杠杆写入同样必须先过该闸门。数量超过容量直接 `BLOCKED`，不截断。减险路径不被该新增风险闸门阻断。
- `CLEAR_DRAWDOWN_STOP` 是控制库命令，不需要新的券商写租约；Worker 先重新读取账户和账单，只有成功后才人工重锚最新总权益并解除停止。系统不会自动清除 50% 停止。
- 本轮离线硬验收：风险运行态、v1/v2/v3→v4 持锁迁移、资金流分类、七天滚窗、提交前动态定仓、动态杠杆回撤阻断、50% 持久停止和 OKX 合约规格定仓全部通过。生产控制库仍为 v1，Worker PID 536 启动早于这些代码；迁移、历史 uncertain 只读处置、Worker/Campaign 重启和现场验收未完成，外部总控不得把代码通过写成已部署。

## 6. 当前验证证据

本次本地阶段 1 定向验证：

- 读取层测试：3 通过 / 0 失败。
- 主窗口数据源状态测试：2 通过 / 0 失败。
- ExecutionController + WorkerStore：33 通过 / 0 失败。
- ExecutionStore + ExecutionService + 生命周期：56 通过 / 0 失败。
- 合计：94 通过 / 0 失败。
- 新增读取层和测试的 Ruff 规则检查：通过。
- `git diff --check`：通过；仅有 Git 的 LF/CRLF 提示。
- 活动文档旧基线号检查：通过。
- `docs/` 密钥扫描：0 命中。
- 本轮 `WO-S2A-01` 定向套件为 156 通过 / 0 失败，包含监督模型、监督落盘、Campaign、AI 档案、Controller、Worker、ExecutionService、OKX 数据源和真实 Controller/Worker 离线链路。
- 本轮 `WO-RISK-02` 定向套件为 52 通过 / 0 失败，包含纯风险公式、Campaign 动态规格适配、`net_mode` 硬门、计划数量接缝、数量变化后的租约重建和真实 Controller/Worker + FakeAdapter 离线链路。
- 本轮租约竞争修复定向套件为 55 通过 / 0 失败，包含 Campaign、Controller、Worker 租约接缝；`compileall` 通过，`git diff --check` 通过。pytest 使用项目内临时目录时仅有既有 `.pytest_cache` 写权限警告，不影响断言结果。
- 本轮全量 `tests/unit` 收集 1239 项，1219 通过、20 失败；20 项仍集中在旧的连续性、数据源切换替身、决策面板、价格/提示归一化和 openclaw 兼容契约，风险/监督/Campaign/执行定向套件没有失败。全量不能描述为全绿。
- 新增代码的 `ruff --select E,F,I,UP,B,SIM`、`compileall` 和 `git diff --check` 通过；本轮没有连接真实券商和发送真实订单。

历史全仓测试和运行态证据必须以 `CONTEXT.md`、`docs/VALIDATION_EVIDENCE.md` 的具体记录为准；本次没有重新把全仓结果或自动 Campaign 运行状态说成当前已确认。此前运行态探针曾超时，因此 Web GPT 不得据旧快照断言策略仍在运行。

## 7. 网页版 GPT 的总控边界

网页版 GPT 应该：

1. 先读取本文件和 `docs/WEB_GPT_CONTROLLER_PROMPT.md`，再按规定顺序读取代码/测试。
2. 用 GitHub 证明已完成、候选、未实现和待本机核验的内容。
3. 只写需求冻结、路线图、工单和二元硬验收，不直接修改代码、不调用券商私有接口、不发送订单；审核完成时必须给出下一张可直接执行的工单和硬验收指令。
4. 每张工单写清前置、范围内/外、代码路径、禁止行为、失败路径、实际证据、独立审查、回退和用户确认点。
5. 把外部计划交回本地 Codex；本地 Codex 必须重新核对当前 checkout、规则、配置、进程、权限、测试和远程 SHA。

本地执行闭环：用户已授权的 PA Demo 改动在本机验证通过后，由 Codex 按精确文件范围自动提交并推送 `xiaojinlucky/PA_Agent` 的公开 `main`；外部总控每次只负责读取公开仓库、审核结果并生成下一步工单，不得要求私有仓库权限。

网页版 GPT 不得：

- 使用旧提交、上游仓库、搜索摘要或历史快照替代当前 `main`。
- 把规划中自动策略、加仓、反手、动态杠杆、生命周期聚合或关闭 GUI 后运行写成当前能力。
- 把公共行情可用、模型连接成功或 Demo canary 当作实盘准备或交易成功。
- 要求用户粘贴密钥，或把真实凭据、私有配置、数据库和日志写入工单。

启动指令的完整文本见：[WEB_GPT_CONTROLLER_PROMPT.md](<D:/Desktop/Quant/PA_Agent/docs/WEB_GPT_CONTROLLER_PROMPT.md>)。
