# PA_Agent Context

> 静态代码和文档可直接引用。进程、账户、服务、仓位、订单和券商状态只认本轮实时证据。

## 在做什么

- WO-H 已由 `0a0c5a8`、`2e95a05`、`0486002` 完成并推送：时段标签、高周期关键位置、20GB 标记、成交量影子自动采集/评分和多市场前端设计包均已落地；成交量仍不进入提示词或交易判断。
- WO-F Claim Validation 完整闭环已由 `c0b58d0` 推送：Stage1 支撑/阻力、Stage2 入场/止损/两档止盈、K 线引用和行情源声明的真实 `price_tick` 均已接入硬校验；校验只拒绝，不修正或补写模型声明。
- 声明校验最终失败会耐久保存 `claim_validation:<code>` 证据，Campaign 记录 `blocked:claim_validation:<code>` 并继续下一根已收盘 K 线，不创建 execution 或券商写命令。
- 2026-07-27 OKX Demo 已通过正式 `run` 入口加载 WO-F 并完成实盘式运行验收；20:01 又在完整空现场硬门后安全重载同一 Campaign，以加载记录文件名分钟修复。这是模拟账户生产链路验收，不是 OKX Live 实盘或策略收益证明。
- 完成证据复审与 Campaign 异常收口已由 `7e6c095` 推送；Campaign 的 NEW_RISK 最小权限修复已完成并发布。WO-E Product Design B1 与用户选择 B2 已通过，用户按重跑结果的实际显示顺序选择 `1`，当前方向为 `Scan Rail Workbench`；网页版 ChatGPT、Stitch、三轮精修、视觉审计和生产实现仍未完成。

## 上次停在哪

- WO-F 开工基线为 `HEAD=4e13c7f`。最终 unit/property/integration 三套件为 1926 项通过、7 项跳过、0 失败；GUI E2E 为 4 项通过、0 失败。
- 2026-07-28 NEW_RISK 修复后的 unit/property/integration 为 1966 项通过、7 项跳过、0 失败；Campaign 与离线 Controller/Worker 定向回归为 130 项通过、0 失败。全仓 Ruff 当前基线仍为 293 项，其中 249 项带自动修复建议，本轮没有批量修复历史债。
- 2026-07-28 09:55–09:58 的本轮只读审计显示：Worker 服务和心跳仍运行，两库 `quick_check` 均为 `ok`，活动 execution、pending/running 命令和有效 `NEW_RISK` 租约均为 0；但 OKX 私有读取故障已触发风险停止，Campaign 进程不存在，磁盘 `active` 状态已过期，不能冒充实际运行。
- 最近耐久完成的是 2026-07-27 21:50–22:00 已收盘 10m K 线：Stage1/Stage2 均存在，终点 `wait`、trade confidence 38、结果 `blocked:no_order`。Worker 进程启动时间早于审计时 `HEAD=771c951`，也不可能加载其后的提交。最后本地账户快照已陈旧；本轮未调用 OKX 私有接口，因此当前券商仓位、普通挂单和全部算法挂单均为实时真相阻断。未经新的 OKX Demo 私有只读硬门和用户运行态授权不得恢复 Campaign或重载。
- 长桥最后已知结果仍是服务端 `401004 token invalid`；共享 `env` 自 2026-07-24 后未更新。本轮没有把旧错误冒充实时网络复验，AAPL.US、700.HK、600519.SH 的真实两阶段验收继续阻塞。
- WO-A 仍有一个真实 P2：固定代理 metadata 与实际 `config.json` 没有共同指纹，无法检测二者不一致；本轮明确禁止修改 `scripts`，因此不能用假测试冒充完成。

## 近期关键决定

- 成交量当前只做影子摘要和描述性后验比较，不进模型输入、不生成交易信号、不进入风险闸门，也不能据均值差宣称统计显著或通过 Wilson 方向准确率门。
- 自动影子写入用跨进程锁串行化，并在下一次写入前回收中断留下的半行；失败会记录 ERROR，但不改变提示词或交易判断。测试通过 `PA_AGENT_VOLUME_SHADOW_DIR` 把输出隔离到临时目录。
- 成交量摘要只使用已收盘 K 线。最新一根与此前最多 20 根形成基线；不足 6 根、参与计算的成交量无效或基准中位数为零时返回空结果，不猜值。
- 多市场前端已冻结 `QuoteSnapshot`、四市场本地设置、generation 与刷新序号、Longbridge 内切换、Longbridge↔OKX 跨源回滚、脱敏输入和 M01–M17；方向 1 本地合同另冻结 D01–D07，覆盖高密自选集合、同 generation 保存、只分析不执行、完整壳层尺寸、认证优先级和异步暂存提交。Product Design 重跑与选择证据见 `docs/prd/09_WO-E_Product_Design_ideate_重跑证据.md`，剩余外部设计门见 `docs/prd/08_WO-E_方向1_外部设计门证据.md`；ChatGPT Web、Stitch、三轮精修、视觉审计和范围授权未完成前不写 PyQt6、不宣称 WO-E 完成。
- Campaign 的每个 NEW_RISK 授权窗口只包住一条新增风险 Worker 命令：`set_leverage`、正常 `submit` 和 READY 恢复 `submit` 在命令创建前失败时释放；只有等待函数返回 `SUCCEEDED`、`FAILED` 或 `UNCERTAIN` 耐久终态后才释放。等待超时、命令读取异常或非终态结果不会由业务方法提前释放，进程收口仍会显式撤销租约；RUNNING 命令保持未解决并阻止再次授权。下一条新增风险命令重新执行 OKX Demo 私有只读预检并申请新租约；撤单和离场等减险命令不依赖该租约。
- 上述是 Campaign 调用路径的不变量，不是 `ExecutionController` / `WorkerStore` 的全局一次性令牌保证；公共层目前允许同一有效租约排入多条新增风险命令。把该性质提升为执行层硬约束需要修改本轮禁止触碰的 `pa_agent/execution`，已在 `BLOCKED.md` 如实登记。
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
