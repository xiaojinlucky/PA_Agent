# PA_Agent 项目协作规则

本文件继承 `D:\Desktop\Quant\AGENTS.md`。冲突时，以更严格的交易安全边界和本文件更具体的 PA_Agent 运行规则为准。

## OKX Demo 常驻交易与反馈闭环

1. 当前自动交易范围固定为 `OKX Demo / XAU-USDT-SWAP / 10m / min_trade_confidence=20 / extreme_aggressive`。只允许模拟账户；严禁启用或写入任何 Live 账户，严禁读取、修改或运行 `D:\Desktop\Quant\AlphaMaster`。
2. 执行开发、测试、文档或审查任务前，先只读核验 `PAAgentExecutionWorker`、OKX Demo Campaign、Worker 心跳和最近成功对账、Campaign 配置与最近已收盘 10m K 线进度、活动 execution/命令、`NEW_RISK` 租约、OKX Demo 仓位、普通挂单及全部算法挂单。只要安全门满足，Worker 与 Campaign 必须保持运行；不得为了离线开发或测试长时间停止自动交易和监控。
3. 只有生产代码重载、数据库迁移、同一 `CampaignProcessLock` 下的受控闭环验收等确实需要独占时，才允许最短维护停机。停机前必须确认无活动 execution、无仓位、无普通或算法挂单、Worker 可对账且 `NEW_RISK` 租约合法；维护完成后必须先恢复 Worker 和 Campaign、确认 `active / 10m / 20 / extreme_aggressive`，再继续其他离线工作。
4. 每根已收盘 10m K 线都必须继续运行真实 PA 两阶段分析。自然信号有效且真实 `SupervisorGate` 返回 `allow_entry` 时，必须经 `ExecutionController → ExecutionWorker → ExecutionService → OkxAdapter` 提交 OKX Demo，并持续跟踪入场、成交、仓位、原生保护、主动离场、对账与最终状态。`ExecutionWorker` 是唯一券商写入者，其他进程、GUI、Agent 和脚本不得直接写券商。
5. 自然结果为 `no_order`、监督阻断、风险阻断或数据失败时，必须耐久保存真实原因并继续下一根已收盘 10m K 线；不得伪造自然信号、放宽监督、截断风险数量、把计划或命令冒充成交，也不得因单次阻断让 Campaign 退出。
6. 只要本阶段修改或验收交易执行链，就不能因自然信号持续 `no_order` 而整阶段没有真实 Demo 交易反馈。应在不伪装成自然策略绩效的前提下，及时运行明确标记为 `controlled_reproducible` 的 Demo 输入闭环。受控记录必须使用真实 OKX 5m→10m 聚合 K 线、实时 ATR、真实账户权益、合约规格、容量和合法三价；仍由真实 `SupervisorGate` 独立判断，并完整经过 Controller→Worker。禁止 deterministic allow、canary 冒充策略信号或直接写券商。受控闭环与 Campaign 共用锁时只允许最短独占窗口，结束后立即恢复 Campaign。
7. 任何 Agent 都不得撤销、抢占或绕过其他 GUI 或进程持有的合法 `NEW_RISK` 租约。租约冲突只记录为暂时阻断并继续监控；未知提交先只读对账，禁止盲目重提。
8. 主 Agent 工作期间必须并行安排至少一个子 Agent 做只读运行监控和反馈审查；子 Agent 不得写券商、修改租约、启用 Live 或触碰 AlphaMaster。每个复杂阶段完成后再做多 Agent 对抗性审查，把真实运行反馈映射为最小代码、GUI、提示词和测试修复；不能用审查替代持续交易。
9. 每次阶段回报必须分别列出：Campaign/Worker 实时状态、最近已收盘 10m 分析结果、真实 execution/订单/成交/仓位/保护/离场证据、本轮真实修改、测试通过/失败数、硬阻塞和下一张工单。不得只说“后台在跑”。
