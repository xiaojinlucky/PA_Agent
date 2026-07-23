# PA_Agent Codex 交接总账

更新时间：2026-07-23
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
- 当前本地安全配置字段（不代表交易授权）：revision `63`、数据源 `okx`、品种 `XAU-USDT-SWAP`、周期 `15m`、决策 stance `aggressive`、最低置信度 `30`、OKX Demo、自动执行配置开启、数量 `10`。
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
- 本轮全量 `tests/unit` 收集 1239 项，1219 通过、20 失败；20 项仍集中在旧的连续性、数据源切换替身、决策面板、价格/提示归一化和 openclaw 兼容契约，风险/监督/Campaign/执行定向套件没有失败。全量不能描述为全绿。
- 新增代码的 `ruff --select E,F,I,UP,B,SIM`、`compileall` 和 `git diff --check` 通过；本轮没有连接真实券商和发送真实订单。

历史全仓测试和运行态证据必须以 `CONTEXT.md`、`docs/VALIDATION_EVIDENCE.md` 的具体记录为准；本次没有重新把全仓结果或自动 Campaign 运行状态说成当前已确认。此前运行态探针曾超时，因此 Web GPT 不得据旧快照断言策略仍在运行。

## 7. 网页版 GPT 的总控边界

网页版 GPT 应该：

1. 先读取本文件和 `docs/WEB_GPT_CONTROLLER_PROMPT.md`，再按规定顺序读取代码/测试。
2. 用 GitHub 证明已完成、候选、未实现和待本机核验的内容。
3. 只写需求冻结、路线图、工单和二元硬验收，不直接修改代码、不调用券商私有接口、不发送订单。
4. 每张工单写清前置、范围内/外、代码路径、禁止行为、失败路径、实际证据、独立审查、回退和用户确认点。
5. 把外部计划交回本地 Codex；本地 Codex 必须重新核对当前 checkout、规则、配置、进程、权限、测试和远程 SHA。

网页版 GPT 不得：

- 使用旧提交、上游仓库、搜索摘要或历史快照替代当前 `main`。
- 把规划中自动策略、加仓、反手、动态杠杆、生命周期聚合或关闭 GUI 后运行写成当前能力。
- 把公共行情可用、模型连接成功或 Demo canary 当作实盘准备或交易成功。
- 要求用户粘贴密钥，或把真实凭据、私有配置、数据库和日志写入工单。

启动指令的完整文本见：[WEB_GPT_CONTROLLER_PROMPT.md](<D:/Desktop/Quant/PA_Agent/docs/WEB_GPT_CONTROLLER_PROMPT.md>)。
