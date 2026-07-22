# PA_Agent 交易执行加固计划

## 结论

PA 当前已有真实交易闭环，不需要推倒重写：

`分析记录 → 确定性执行计划 → 本地执行账本 → Longbridge/OKX 下单 → 成交 → 保护 → 监控 → 离场 → 账户快照`

当前真正阻止长期无人值守的不是“缺少下单接口”，而是 GUI 退出后的监控、启动对账、券商真值核对、权限拆分和限流。

截至 2026-07-20，P1 独立后台与 P3 的权限拆分基础已经进入代码：

- GUI 和 24 小时 OKX Demo 实验只创建执行计划、写入持久命令，不再构造券商适配器；
- 只有独立 `ExecutionWorker` 能构造 `ExecutionService` 并进入 Longbridge/OKX 写接口；
- 命令使用 `pending/running/succeeded/failed/uncertain` 五态，崩溃遗留的
  券商写命令只会转成 `uncertain`，不会自动重放；只读账户刷新会转成明确失败，
  允许人工再次读取，因为它不会产生订单；
- 新增风险使用绑定 Worker、请求者、账户路由和配置指纹的短租约；撤单、保护、减仓和离场不依赖该租约；
- 心跳和最后成功对账分别记录，能识别“进程还活着、对账已经停住”；
- 实盘窗口分别展示交易后台心跳和最近一次成功对账，不再用单一“已启用”文案冒充后台健康；
- 控制命令单独保存在 `records/execution_control.sqlite3`，交易真值仍只有
  `records/execution.sqlite3`。

WinSW 守护已于 2026-07-20 安装，GUI 绕过服务的异常路径已经按失败关闭修复，旧运行态对象的 ACL 也已完成收紧。Live 与 OKX Live 硬门继续关闭；模拟硬门只在明确授权的 Demo 收口期间开启。历史 OKX Demo 仓位已通过持久减险命令完成显式离场和最终账户对账，当前活动 execution 为 0。2026-07-22 账户又出现一笔客户订单号为空的外部 10 张 `XAU-USDT-SWAP` 仓位；PA 不接管该仓，预检会阻断同品种新开仓。当前服务级剩余阻塞是 Longbridge 私有推送/全局限速、更完整的券商启动扫描和持续持仓/保护真值核对。

## 必须修复的五个问题

1. **监控依赖 GUI**
   - 风险：GUI 关闭或崩溃后，软件止盈、保护修复、离场和资金快照停止。
   - 修复：把执行循环移到由 WinSW 监管的独立 `ExecutionWorker`。
2. **新增风险和减险共用同一开关**
   - 风险：重启后即使发现保护缺失，也可能因为会话未启用而无法补保护。
   - 修复：分开“新增风险权限”和“仅撤单/补保护/减仓/平仓权限”。
3. **本地数量没有持续核对券商真值**
   - 风险：人工改仓、强平、漏成交或券商修正后，PA 可能按错误数量补保护或离场。
   - 修复：每轮比较本地剩余数量、成交、实际持仓和保护覆盖量。
4. **启动恢复只看 SQLite**
   - 风险：券商已经接收、但本地写盘前进程崩溃时，产生 PA 看不见的订单。
   - 修复：启动时同时扫描 PA 确定性客户订单号/备注、成交和持仓；无法确认归属的持仓只告警，不接管。
5. **Longbridge 没有全局交易限速**
   - 风险：多个活动订单按 2 秒轮询会超过官方限制。
   - 修复：全账户共享限速器，私有订单推送为主、REST 对账为辅。

## 成熟工具复用

### 采用原则（2026-07-20 社区与开源复核）

公开社区、项目 issue 和官方文档反复出现的实盘故障高度一致：

- HTTP 请求成功不等于订单已经生效、成交或撤销完成；
- 超时后直接重试会造成重复下单；
- WebSocket 重连不会自动补齐断线期间的完整订单状态；
- 部分成交、重复回报和乱序回报会造成错误的剩余数量；
- 重启时若没有先核对挂单、成交、持仓和保护，可能重复开仓或错误平仓；
- “进程仍在”不等于监控健康，必须另有心跳和最后对账时间。

因此，本阶段不把 PA 整体迁移到第三方交易平台，也不复制社区小项目代码。
采用深度如下：

| 对象 | 采用深度 | 原因 |
|---|---|---|
| Vibe-Trading | 治理边界与故障用例参考 | MIT 且覆盖心跳、停机、对账和审计；但 Longbridge 实盘只读、OKX 仅基础现货，冷启动还会把既有外部仓位直接当作正常基线，不能替换 PA |
| Longbridge 官方 SDK | 直接依赖 | 唯一与 Longbridge 账户和订单语义完全匹配的官方实现 |
| WinSW | 独立部署依赖 | 负责 Windows 开机启动、进程退出重启和优雅停止，不参与交易真值 |
| NautilusTrader | 对账和订单状态语义参考 | 成熟但体量大、LGPL，且没有 Longbridge；不引入第二套执行账本 |
| Freqtrade | 重启恢复和故障用例参考 | GPL 且只覆盖加密市场，不引入代码 |
| Hummingbot | 订单跟踪和丢单测试参考 | 没有 Longbridge，OKX 重启持仓语义与 PA 目标冲突 |
| vn.py | Gateway、事件和事前风控参考 | Windows 生态成熟，但没有官方 OKX/Longbridge Gateway |
| CCXT | 公开规格和诊断参考 | 不足以统一 OKX 普通单、算法单、保护单和原始错误 |

最终仍保留一个交易真值：PA 的 `ExecutionStore`。外部框架不得同时写入同一账户。

### 中文社区的真实踩坑

本轮先检索小红书、微信公开文章索引和公开视频社区。抖音没有稳定的公开正文检索接口，
因此只使用能回到原页面或公开视频的内容，不把搜索摘要当成事实。

反复出现、且已转成 PA 验收约束的痛点：

- 启动时必须先同步账户、挂单、持仓和保护，不能直接开始下单；
- 网络超时、403、断线重连不能触发盲目重试；
- 模拟盘和实盘的行情、成交和权限差异必须明确展示；
- 部分成交、崩溃恢复、孤儿订单和保护单丢失必须有独立状态；
- 用户需要看到“后台是否活着”和“最近一次成功对账”，而不是只有一个绿色开关；
- 交易系统最重要的是可靠收口和可审计，不是堆叠策略面板或视觉效果。

公开证据：

- 小红书交易系统启动与账户同步讨论：
  <https://www.xiaohongshu.com/explore/69dc91f8000000001b022131>
- 小红书 Longbridge 接入讨论：
  <https://www.xiaohongshu.com/explore/681cae8d0000000023013bd2>
- 小红书 OKX 接入与 403/网络问题讨论：
  <https://www.xiaohongshu.com/explore/68235774000000001101e8dc>
- Bilibili OKX 自动交易实践：
  <https://www.bilibili.com/video/BV1JUWUz7Ee7>
- FMZ 关于订单、重试与恢复的工程讨论：
  <https://www.fmz.com/bbs-topic/4145>

这些社区内容只用于发现问题和补充故障用例。框架选型、API 语义和正式实现仍以代码、
许可证和官方文档核验为准。

### Vibe-Trading 代码级核验

核验提交：`HKUDS/Vibe-Trading@7d42de944466e1a1f12f0df3933624fe665dee3c`。

可以借鉴：

- 文件级停止开关、Worker 心跳和最后对账时间；
- 真实写请求不自动重试；
- 把未知成交、孤儿订单和崩溃时结果不明分开；
- 先对账、后运行的固定启动顺序。

不能直接移植为 PA 执行内核：

- Longbridge 连接器明确只允许模拟盘下单，实盘只读；
- OKX 连接器只覆盖基础现货，没有永续、保证金模式、`reduceOnly` 和保护单；
- 冷启动测试允许把已有持仓和挂单直接保存成“正常基线”，这会让 PA 静默接管不属于自己的资产；
- 对账没有覆盖 PA 的确定性客户订单号、Longbridge 备注、OKX 算法保护单和逐 execution 生命周期；
- 数量使用二进制浮点数，不能替换 PA 基于 `Decimal` 和交易所步长的数量语义。

因此本阶段只复用其故障分类和运行治理思路；PA 已有适配器、确定性标识和强账本继续作为
交易真值。新增 Worker 是把现有 PA 执行核心移出 GUI 的必要适配层，不再另造一套交易引擎。

代码证据：

- <https://github.com/HKUDS/Vibe-Trading/blob/7d42de944466e1a1f12f0df3933624fe665dee3c/agent/src/live/runtime/reconcile.py>
- <https://github.com/HKUDS/Vibe-Trading/blob/7d42de944466e1a1f12f0df3933624fe665dee3c/agent/tests/test_runtime_reconcile.py>
- <https://github.com/HKUDS/Vibe-Trading/blob/7d42de944466e1a1f12f0df3933624fe665dee3c/agent/src/trading/connectors/longbridge/profiles.py>
- <https://github.com/HKUDS/Vibe-Trading/blob/7d42de944466e1a1f12f0df3933624fe665dee3c/agent/src/trading/connectors/okx/sdk.py>

### Longbridge

继续使用官方 SDK，不换执行框架。

- 官方交易接口限制：不超过 30 次/30 秒，相邻调用至少间隔 0.02 秒；SDK 不会自动限制 `TradeContext`。
- 使用官方私有订单推送获取及时状态。
- 使用 REST 做启动恢复、断线补偿和定期真值核对。

官方资料：

- <https://open.longbridge.com/docs>
- <https://open.longbridge.com/docs/qa/trade>

### OKX

先用 NautilusTrader 做独立 Demo 兼容实验，不直接替换当前生产路径。

选择原因：

- 已有 OKX 现货、永续、Demo 和私有 WebSocket 适配；
- 已有启动与持续对账、外部订单分类、模糊提交等待对账；
- 已有条件单、OCO、清算和自动减仓事件处理。

资料：

- <https://nautilustrader.io/docs/latest/concepts/live/>
- <https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/integrations/okx.md>
- <https://app.okx.com/docs-v5/en>

Hummingbot 只借鉴 `UserStreamTracker` 和 `ClientOrderTracker` 的职责分离，不嵌入整个框架：

- <https://hummingbot.org/connectors/connectors/architecture/>

CCXT 只用于公开行情、元数据或诊断，不用于 PA 的正式下单和保护单。

LEAN 没有当前所需的官方 OKX/Longbridge 券商适配，只借鉴重启同步思路，不采用完整运行时：

- <https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading/trading-and-orders>

## 分阶段落地

### P0：同一价格语义与明确风险

- 分析品种和执行品种必须完全相同；没有明确基差换算前禁止跨市场绝对价格下单。
- 固定数量必须同时展示最大计划亏损，不能把“固定 1 张”叫作风险仓位。
- 所有价格、数量和交易所规格必须在提交前验证。

验收：不同价格语义被硬拒绝；用户能看见数量、止损距离和最大计划亏损。

### P1：独立 ExecutionWorker

状态：核心代码与独立对抗审查循环已完成。生产账本检查时已经是 schema v2，先备份后没有重复迁移；WinSW 已安装为 `LocalService`、自动延迟启动并通过只读运行、重启和单实例验收。全部交易写入硬门仍为关闭。

- GUI 不再拥有监控线程。
- Worker 单独打开执行账本和券商连接。
- GUI 通过本地受限命令接口读取状态和发出授权/操作。
- Worker 使用单实例锁，崩溃后由 WinSW 重启。
- 命令接口只传 `execution_id`、动作和必要的非密钥参数；Worker 从持久账本重新读取计划。
- 命令状态为 `pending/running/succeeded/failed/uncertain`。Worker 在执行券商动作前先把命令原子改为
  `running`；若进程中断，可能产生券商写入的旧命令改为 `uncertain`，绝不自动重放。
  `refresh_account` 是唯一的只读例外，会记为 `failed/worker_restarted_read_retryable`，
  允许重新读取但不会自动执行。
- GUI 的新增风险授权使用短期租约。GUI 关闭或停止续租后，Worker 自动停止新入场，但继续管理
  已有 PA 执行的撤单、保护和离场。
- Worker 心跳必须同时记录进程状态、最后成功对账时间和最近错误；WinSW 只负责进程退出，
  PA 自己负责识别“进程仍在但对账已停止”。

验收：GUI 关闭后，Demo 活动执行继续对账；重复启动第二个 Worker 被阻断；重启不重复入场。

### P2：启动和持续对账

- 启动扫描本地活动执行、PA 标记订单、成交和持仓。
- 每轮核对券商真实剩余仓位和保护覆盖量。
- 不明归属订单/持仓进入 `NEEDS_ATTENTION`，不自动接管。

验收：覆盖“券商接受后本地崩溃”“人工改仓”“漏成交”“数据库记录缺失”。

### P3：权限拆分

- 新入场必须有新增风险授权。
- PA 已确认订单的撤单、补保护和减仓不依赖新增风险授权。
- 所有减险请求必须证明不会增加仓位。
- `PA_AGENT_PAPER_TRADING_ENABLED`、`PA_AGENT_LIVE_TRADING_ENABLED` 和
  `OKX_LIVE_ENABLED` 仍是环境级硬门；短期租约只控制新增风险，不能绕过这些硬门。
- OKX 减仓使用交易所 `reduceOnly` 语义；Longbridge 在每次退出写入前重新读取可用持仓并把
  数量限制在券商真值内，查询失败时禁止写入，不能把失败当作空仓。

验收：关闭新增风险后仍能安全撤单、补保护和离场；超过真实持仓的请求被拒绝。

### P4：Longbridge 推送与限速

- 一个账户一个共享限速器。
- 私有推送驱动订单状态。
- REST 根据状态分级刷新。
- 日内账户只在确定性资格/购买力不足时于提交前回退综合账户。

验收：限流、断线、推送丢失、认证失败、未知提交和回退矩阵全部通过。

### P5：NautilusTrader OKX Demo 兼容实验

- 使用独立 Demo 账户或独占的账户/品种。
- 现货和永续分别验证。
- 验证部分成交、保护、主动离场、断线和启动恢复。
- 当前适配器与 Nautilus 不能同时写同一账户。

验收：所有契约通过后才讨论切换；失败则保留当前适配器，不做半切换。

### P6：单写入者切换

- 一个账户同一时刻只有一个写入者。
- 切换前撤销或接管所有活动执行。
- 旧适配器只读影子核对。
- 出现差异时停止新增风险，不自动双写回退。

验收：写入权唯一，订单和仓位真值一致，回滚不会产生重复订单。

## 故障验收

最终使用 Hypothesis 状态机覆盖状态组合，使用 Toxiproxy 注入断网、延迟和连接重置，并验证：

- GUI/Worker 任意时点崩溃；
- 请求超时但券商已接收；
- 429 限流；
- 私有推送断开或乱序；
- 人工改仓；
- 部分成交后崩溃；
- 保护单被外部取消；
- 账户身份变化；
- SQLite 文件不可写或记录缺失；
- Demo/Live 配置误切换。

只有 Demo 连续运行、所有活动执行安全收口、账户最终快照可读，并通过独立对抗审查后，才进入小规模实盘。

## 2026-07-20 当前运行结论

- 历史 OKX Demo `XAU-USDT-SWAP` execution 已通过独立 Worker 的持久减险命令关闭；本地状态为 `closed`、剩余数量 0，活动 execution 为 0。
- 券商侧只读复核为持仓 0、普通挂单 0、条件单 0；最终 USDT 权益为 `4999.99488722`、未实现盈亏 0，并已保存无持仓账户快照。
- PA Live 与 OKX Live 硬门继续关闭；模拟门仅用于用户已授权的 Demo 路径。
- 这次收口证明了已有 execution 的监控和减险链路，但不替代 P2、P4 的长期无人值守验收，也不授权实盘 Canary。
