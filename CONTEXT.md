# PA_Agent Context

> 静态代码和文档可直接引用。进程、账户、服务、仓位、订单和券商状态只认本轮实时证据。

## 在做什么

- WO-H 已由 `0a0c5a8`、`2e95a05`、`0486002` 完成并推送：时段标签、高周期关键位置、20GB 标记、成交量影子自动采集/评分和多市场前端设计包均已落地；成交量仍不进入提示词或交易判断。
- 最近完成的主线是 WO-C2 Campaign 对账监控耐久化。`pa_agent/okx_demo_campaign.py` 已改为先读 execution 耐久终态，只有普通活动态才等待新对账；临时超时或 Worker 短暂 attention 不再杀死 Campaign，也不覆盖刚完成 K 线的结果。
- 2026-07-27 16:31 北京时间从正式 `run` 入口恢复原 Campaign；专用恢复命令完成新的账户与账单读取后合法解除白名单内的临时风险停止，高水位未重锚。
- 下一张开发工单是 WO-F 完整 Claim Validation；之后进入 `WO-POS-05` 多段仓位生命周期。多市场前端可并行继续方向 B 的高保真设计，但生产 PyQt6 等后端合同稳定后再落地。

## 上次停在哪

- WO-C2 基线为 `HEAD=0486002`、工作区干净。最终三套件为 1886 项通过、0 失败；定向 Campaign 套件 86 项通过、0 失败。
- 多 Agent 两轮对抗审查发现的终态 `needs_attention`、后置监控覆盖新 K 线结果、收口重试和人工中断耐久状态问题均已修正，最终复审 PASS。
- 现场首根新 K 线 16:20–16:30 已完成，`analyses_completed` 由 16 增至 17，真实结果 `blocked:no_order`；Campaign 保持 `active`，没有新增 execution、订单或仓位。
- 16:33 后只读现场：Worker 心跳与成功对账新鲜，风险停止为 0，活动 execution/命令、未解决 UNCERTAIN 和 NEW_RISK 租约均为 0；OKX Demo 空仓、空普通单、八类算法单全空。
- 长桥 access token 仍被服务端以 `401004 token invalid` 拒绝。AAPL.US、700.HK、600519.SH 的真实两阶段验收继续阻塞；不得改凭据或绕过认证。

## 近期关键决定

- 成交量当前只做影子摘要和描述性后验比较，不进模型输入、不生成交易信号、不进入风险闸门，也不能据均值差宣称统计显著或通过 Wilson 方向准确率门。
- 自动影子写入用跨进程锁串行化，并在下一次写入前回收中断留下的半行；失败会记录 ERROR，但不改变提示词或交易判断。测试通过 `PA_AGENT_VOLUME_SHADOW_DIR` 把输出隔离到临时目录。
- 成交量摘要只使用已收盘 K 线。最新一根与此前最多 20 根形成基线；不足 6 根、参与计算的成交量无效或基准中位数为零时返回空结果，不猜值。
- 多市场前端本轮只交页面合同和三个文字方向，停在用户选择方向之前；不写 PyQt6，不宣称 WO-E 完成。
- 市场切换分为跨数据源事务与 Longbridge 内市场事务；US、HK、CN 路由到 Longbridge，Crypto 路由到 OKX。认证失败、标的或来源不一致、行情或已收盘 K 线陈旧时失败关闭；日历未知只关闭市场时钟事实，不猜阶段，但完整且新鲜的已收盘 K 线仍可分析；不静默换源。
- 自选列表只写本地 `GeneralSettings`，不写长桥云端 watchlist。
- 市场时钟读取 `market_calendar.session_state` 的开市、午休、闭市、半日市与下一变化时间；加密显示连续交易，不伪造股票会话。
- 交易安全真值保持不变：高水位 `78303.57015174496`，账户身份摘要 `ba9b744dc78ae3fc203980e62b854b0a0e3d44c9c6d5e446de910bea74ef1def`。
- `CLOSED/BLOCKED/CANCELED/REJECTED` 是无需新对账的安全终态；`BLOCKED/REJECTED` 可因确定未写入而带 `needs_attention=true`，不能误判为未知券商写入。`UNKNOWN/ERROR`、非终态 `needs_attention` 和记录丢失仍硬阻断。
- 对账临时故障写稳定 `blocked:reconcile:*` 证据；若本根已有 `blocked:no_order` 等结果，只更新监控错误，不覆盖该结果。收口阶段在剩余窗口内重试临时对账，真实不安全状态耐久写 `needs_attention`。
- WO-F 当前只有部分反幻觉校验落地，原工单要求的全部价位、K 线引用、真实最小报价单位和耐久 `blocked:claim_validation` 语义仍未完成，禁止继续标成整张工单完成。
- 总控与历史证据继续以 `docs/WORKORDER_MASTER_20260727.md`、`docs/VALIDATION_EVIDENCE.md` 和本页链接的归档为准。
