# PA_Agent Context

> 静态代码和文档可直接引用。进程、账户、服务、仓位、订单和券商状态只认本轮实时证据。

## 在做什么

- WO-H 已由 `0a0c5a8`、`2e95a05`、`0486002` 完成并推送：时段标签、高周期关键位置、20GB 标记、成交量影子自动采集/评分和多市场前端设计包均已落地；成交量仍不进入提示词或交易判断。
- WO-F Claim Validation 完整闭环已由 `c0b58d0` 推送：Stage1 支撑/阻力、Stage2 入场/止损/两档止盈、K 线引用和行情源声明的真实 `price_tick` 均已接入硬校验；校验只拒绝，不修正或补写模型声明。
- 声明校验最终失败会耐久保存 `claim_validation:<code>` 证据，Campaign 记录 `blocked:claim_validation:<code>` 并继续下一根已收盘 K 线，不创建 execution 或券商写命令。
- 2026-07-27 OKX Demo 已通过正式 `run` 入口加载 WO-F 并完成实盘式运行验收；20:01 又在完整空现场硬门后安全重载同一 Campaign，以加载记录文件名分钟修复。这是模拟账户生产链路验收，不是 OKX Live 实盘或策略收益证明。
- v0.1.0 正在按 PRD11 推进。B1 Longbridge 合同、B2 无 Qt Controller、C 阶段原生 PyQt6 新页和 D 阶段候选证据均已完成；截图时序最终修复 `c16f727185dfc5341ae7e939a8275f18d9fc166e` 的 CI、候选资产、全新安装、跨时区重建、扫描和 16 张下载图逐图复核均通过。稳定发布仍受阶段 A 的受控 OKX Demo 闭环和正式快捷方式四市场桌面矩阵阻塞。
- P0-01 公共执行层一次性授权已由 `c932e0113e9c4e33771d1cc5afc1f16beda46421` 完成源码与 CI 闭环；运行态随后通过 `0cfd5ca4c9c830bb543a4c61e0920ce9d41d22bc` 的切换器修复和 CI run `30567235969` 激活。生产控制库现为 schema v5，Worker 已重新启动并完成心跳与只读对账；数据库、Controller 和 Worker 共同限制每个 NEW_RISK 租约最多绑定一条新增风险命令。
- 阶段 A 的 v4→v5 正式切换已经完成。切换前新鲜 OKX Demo 私有只读门确认空仓、空普通单和空八类算法单；正式计划摘要为 `e372f38e29cea1bbdf55ee44ea1447f34929bf6f75ffe3f546aa0fa259fbe37f`。切换后档案复核、schema v5、空命令队列、Worker 心跳和只读对账均通过，迁移没有自动清除风险停止；恢复条件齐全后，旧连接故障风险停止仅通过 Controller→Worker 正式命令清除。首次受控 Demo-S 在入场阶段被 OKX 明确拒绝，最终仍为空仓空单、无活动 execution、无租约、无未解决 `UNCERTAIN`。逐单拒绝证据修复已由 `90782ab71f3742f946eead4f9c28ebc5021e4ea7` 发布，CI run `30571509003` 全绿。重载前只读门又发现停止码 `risk_runtime_50001`；OKX 官方合同将 50001 定义为临时服务不可用，且随后新鲜私有读取已证明身份一致、账单可读、空仓、空普通单和空八类算法单。现有恢复白名单漏掉 50001 的红灯已复现并转绿；手工清除仍被拒绝，只有停止发生后的完整新鲜读取才能恢复，60% 回撤仍不可清。最终非 live 2366 项通过、0 失败/错误/跳过，独立复审 P0/P1/P2 均无；尚待本提交 CI 和新 SHA Worker 重载。

## 上次停在哪

- 2026-07-31 16:50(CST) S0 只读核验实时真值:本地=远端 HEAD `726712b`,该 SHA GitHub check-run `test` = success(run 30574355712)。Worker 服务运行(PID 76216/37020),`worker_heartbeats.state=running`,last_seen 与 last_successful_reconcile 均新鲜(08:50Z 内),`account_snapshots` 持续写入(16:49 CST)。控制库 `risk_runtime_state.kill_active=1, kill_reason=risk_runtime_503`(07-30T18:23Z 激活;非 CONTEXT 上文提到的 50001),`last_bill_scan_at` 新鲜。worker_commands 空队列(2 条历史:clear_drawdown_stop succeeded / submit failed);executions 最新 rejected/canceled,无活动执行、无 NEW_RISK 租约(0 行)、无未解决 UNCERTAIN。Campaign 文件 status=active 但 expires_at 已过(07-27)、updated_at=07-30T18:07,业务循环未推进;Worker 未加载 726712b(started 07-30T17:59Z)。结论:对账层活跃、写入面干净;运行态写动作(重载/恢复/新 Campaign)未授权,不做。S0 核验与执行计划见 `docs/implementation-notes.md`。
- 2026-07-31 当前断点：Worker 正在以 schema v5 安全运行，首次 Demo-S 已确定拒绝且现场为空。`90782ab` 的逐单拒绝证据修复及 CI 已通过；当前新增的 50001 临时故障恢复分类通过本地 2366 项非 live 测试。该修复提交、CI 和 Worker 重载完成前，不恢复风险停止、不重试券商写入。
- WO-F 开工基线为 `HEAD=4e13c7f`。最终 unit/property/integration 三套件为 1926 项通过、7 项跳过、0 失败；GUI E2E 为 4 项通过、0 失败。
- 2026-07-28 NEW_RISK 修复后的 unit/property/integration 为 1966 项通过、7 项跳过、0 失败；Campaign 与离线 Controller/Worker 定向回归为 130 项通过、0 失败。全仓 Ruff 当前基线仍为 293 项，其中 249 项带自动修复建议，本轮没有批量修复历史债。
- 2026-07-29 18:01 的只读复核显示：Worker 服务和心跳运行，两库 `quick_check=ok`，活动 execution、pending/running 命令、未解决 UNCERTAIN 和有效 NEW_RISK 租约均为 0；风险停止仍为 `risk_runtime_BrokerTransportError`，Campaign 无 Python 进程，两个正式配置文件哈希与开工基线一致。只允许离线整改，本轮没有重启进程、解除风险停止、修改生产数据库或调用券商。
- 最近耐久完成的是 2026-07-27 21:50–22:00 已收盘 10m K 线：Stage1/Stage2 均存在，终点 `wait`、trade confidence 38、结果 `blocked:no_order`。Worker 自 2026-07-26 13:32:11 启动，不可能加载其后的提交。最后本地账户快照在审计时已陈旧约 13.35 小时；Worker 自动私有读取持续失败，Agent 未另行调用，因此当前券商仓位、普通挂单和全部算法挂单均为实时真相阻断。未经新的 OKX Demo 私有只读硬门和用户运行态授权不得恢复 Campaign 或重载。
- `COMPREHENSIVE` 行情档案已用同一只读 QuoteContext 真实验证 AAPL.US、700.HK、600519.SH 的报价与 10m/1h/4h；三市场服务端权限均为实时。Longbridge 未提供权威统一 tick，股票页只能展示并在 AI 前阻断价格分析；真实三市场桌面矩阵仍留给 C 阶段。
- WO-A 仍有一个真实 P2：固定代理 metadata 与实际 `config.json` 没有共同指纹，无法检测二者不一致；本轮明确禁止修改 `scripts`，因此不能用假测试冒充完成。
- WO-E 已核实 Longbridge 官方批量报价单次 500 个标的，当前 100 项自选无需循环订阅；OKX 可按 SPOT/SWAP 最多两个串行全量 ticker 快照后严格筛选。OKX 10m 最坏需 602 根 5m、最多三页；现有只读客户端缺 `after`，正确修复需要极窄解除 `pa_agent/execution/okx_client.py` 禁区。
- B2 最终本地非 live 覆盖为 2137 项通过、0 失败、0 错误、0 跳过，CI 数量下限同步提高到 2137。Controller 独占 selection/watchlist/request sequence/source/as_of/设置保存/分析状态；逆序回调、快速切换、认证恢复、保存迟到/失败、分析中切换和不完整响应均失败关闭，且源码不导入 Qt 或执行层。阶段 A 仍因固定 OKX 代理端口无监听而冻结，未迁移数据库、重载 Worker、清风险停止或发 Demo 命令。
- D 阶段的字体缺字已由 `c509516a` 关闭。`6b81574` 随后暴露旧能力索引，`7a3297f` 关闭索引问题后又由下载图人工复核暴露截图与元数据时序未封闭：元数据已是“状态未知”，像素仍是“连续交易”。当前修复先完成焦点遍历和 Qt 事件处理，再同步绘制当前控件树；截图后元数据只读，验证器同时要求每张 PNG 的哈希与元数据一致，测试数量门提高到 2248，尚待新提交级证据。

## 近期关键决定

- 成交量当前只做影子摘要和描述性后验比较，不进模型输入、不生成交易信号、不进入风险闸门，也不能据均值差宣称统计显著或通过 Wilson 方向准确率门。
- 自动影子写入用跨进程锁串行化，并在下一次写入前回收中断留下的半行；失败会记录 ERROR，但不改变提示词或交易判断。测试通过 `PA_AGENT_VOLUME_SHADOW_DIR` 把输出隔离到临时目录。
- 成交量摘要只使用已收盘 K 线。最新一根与此前最多 20 根形成基线；不足 6 根、参与计算的成交量无效或基准中位数为零时返回空结果，不猜值。
- 多市场前端首版固定 `analysis_timeframe=10m`，`display_timeframe` 只控制 10m/1h/4h 图表；报价与展示周期无关。页面异步结果统一绑定 generation 和请求族序号。Longbridge 报价接口不声明真实最小跳动时允许只读显示，但不得从小数位或市场默认值猜 tick，更不得据此生成可执行价格。PRD11 取代旧 Stitch/ImageGen 流程，外部模型只负责高保真设计和组件规格；生产实现仍必须使用原生 PyQt6/QWidget/QSS/pyqtgraph。
- Campaign 的每个 NEW_RISK 授权窗口只包住一条新增风险 Worker 命令：`set_leverage`、正常 `submit` 和 READY 恢复 `submit` 在命令创建前失败时释放；只有等待函数返回 `SUCCEEDED`、`FAILED` 或 `UNCERTAIN` 耐久终态后才释放。等待超时、命令读取异常或非终态结果不会由业务方法提前释放，进程收口仍会显式撤销租约；RUNNING 命令保持未解决并阻止再次授权。下一条新增风险命令重新执行 OKX Demo 私有只读预检并申请新租约；撤单和离场等减险命令不依赖该租约。
- 公共执行层现已把 NEW_RISK 租约升级为 schema v5 一次性令牌：租约与唯一 `command_id` 在同一 SQLite 事务中绑定并插入命令；线程、进程、调用者崩溃、插入回滚、过期边界、UNCERTAIN、跨动作复用、Controller 提交/续租与终态续租竞态、真正写入前授权撤销，以及 v4→v5 重复消费者和身份不一致迁移都有直接回归。绑定后 Controller 不再显示可授权，Worker 还会按命令、路由、申请者和配置指纹做最终核验。源码与提交级 CI 由 `c932e0113e9c4e33771d1cc5afc1f16beda46421` 闭环，生产运行态已由 `0cfd5ca4c9c830bb543a4c61e0920ce9d41d22bc` 安全切换为 schema v5。
- 市场切换分为跨数据源事务与 Longbridge 内市场事务；US、HK、CN 路由到 Longbridge，Crypto 路由到 OKX。认证失败、标的或来源不一致、行情或已收盘 K 线陈旧时失败关闭；日历未知只关闭市场时钟事实，不猜阶段，但完整且新鲜的已收盘 K 线仍可分析；不静默换源。
- 自选列表只写本地 `GeneralSettings`，不写长桥云端 watchlist。
- 市场时钟读取 `market_calendar.session_state` 的开市、午休、闭市、半日市与下一变化时间；加密显示连续交易，不伪造股票会话。
- 交易安全基线只认本地风险账本和每次操作前的实时只读硬门；PUBLIC 仓库文档不再重复账户身份摘要或精确权益高水位。
- `CLOSED/BLOCKED/CANCELED/REJECTED` 是无需新对账的安全终态；`BLOCKED/REJECTED` 可因确定未写入而带 `needs_attention=true`，不能误判为未知券商写入。`UNKNOWN/ERROR`、非终态 `needs_attention` 和记录丢失仍硬阻断。
- 对账临时故障写稳定 `blocked:reconcile:*` 证据；若本根已有 `blocked:no_order` 等结果，只更新监控错误，不覆盖该结果。收口阶段只重试临时只读对账，不重试券商写命令；状态存储可写时，撤单、离场、最终快照或中断异常会耐久写 `needs_attention` 后原样抛出；若补写失败，磁盘可能保留 `stopping`，但两种状态都硬阻断自动恢复。
- WO-F 价格包络默认使用已收盘 OHLC 外扩 `1.0×ATR14`，价格精度只认行情源声明的 `price_tick`；K 线引用必须真实存在。缺 OHLC、ATR、真实 tick 或声明越界均失败关闭，不猜值、不静默修正。
- 新记录文件名使用真实分钟、毫秒和 Campaign ID；历史秒级记录曾把分钟误写为月份，legacy 查找明确保留该旧格式，避免历史自由追问 sidecar 断链。
- `WO-POS-05` 目前只有路线图级目标且会修改执行链，不属于当前“不触碰 execution”边界内可直接开工的工作。
- 总控与历史证据继续以 `docs/WORKORDER_MASTER_20260727.md`、`docs/VALIDATION_EVIDENCE.md` 和本页链接的归档为准。
