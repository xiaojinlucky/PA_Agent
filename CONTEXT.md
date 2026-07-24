# PA_Agent Context

## 当前状态（2026-07-24）

> 静态代码、规则和文档事实可直接引用；进程、账户、服务、Live/Demo 配置、仓位和券商状态若没有本轮明确证据，均按历史快照处理，必须在本机重新核验。

- 2026-07-24 当前正式配置已统一为：`OKX / XAU-USDT-SWAP / 10m / extreme_aggressive / min_trade_confidence=20`，入场与主动离场均为 `limit_with_slippage`，滑点为各自 `0.50 × 主周期 ATR14`；`10m` 不是 OKX 原生周期，而是两根连续、同一 UTC 10 分钟边界且均已收盘的官方 `5m` K 线严格聚合。高周期继续使用 OKX 原生已收盘 `1h/4h` 薄背景，只解释结构，不机械否决主周期；Session 与成交量本轮不变。GUI 已持久化并显式显示行情源、Demo 环境、进出场模式、ATR 倍数和高周期背景，OKX 执行拒绝接收 MT5 价格源。
- 2026-07-24 `WO-EXEC-03` 已完成三种真实 OKX Demo 模式覆盖。Demo-A execution `4d118cb0-ec65-5340-a8c9-9f9d8583bd1e` 以 `limit → market` 完成 `27440` 张入场、两笔原生保护和按 OKX 实时 `maxMktSz=20000` 拆成 `20000 + 7440` 的两笔市价减仓，最终 `closed`、剩余 `0`、已实现盈亏 `-73.5046000000000050`。Demo-C execution `89e45ac4-4bee-58eb-9686-7ac36f90db79` 以 `market → limit` 完成 `15057` 张入场、两笔原生保护和一笔完整限价主动离场，最终 `closed`、剩余 `0`、已实现盈亏 `-117.0923`。收口只读回查为非零仓位 `0`、普通挂单 `0`、条件/OCO 挂单 `0`、活动 execution `0`；Demo-S/A/C 合计恰好覆盖入场与主动离场的 `limit_with_slippage`、`limit`、`market` 三种模式。它们只证明 Demo 技术链路，不是 PA 策略绩效。
- 2026-07-24 Demo-A/C 风险数量已按耐久账本复算：`ctVal×ctMult=0.001`、`lotSz=minSz=1`、风险比例 `10%`、费率 `0.0005`、保守滑点率 `0.0010`。Demo-C 使用入场前空仓账户快照权益 `8714.71942600003`，精确得到 `15057` 张；Demo-A 当时缺少紧邻入场前的直接账户快照，只能用成交后首份账户现金 `8843.74440770003` 加已扣入场费用 `55.557134` 重建权益 `8899.30154170003`，精确得到 `27440` 张。历史两笔的确切 `maxBuy/maxSell` 没有耐久保存，不能事后伪造；现已让所有后续 Campaign/Demo 验收记录在监督与计划前耐久保存权益、10% 预算、三价风险、费用/滑点、`minSz/lotSz`、`maxBuy/maxSell` 和最终数量。
- 2026-07-24 Demo-A/C 实证发现并修复两处生产缺陷：OKX 市价减仓现在按实时单笔 `maxMktSz` 分块，但绝不截断总风险数量；主动离场的部分成交按“下单前剩余仓位 − 券商累计成交量”计算，不再每轮重复扣减。另补上保护撤单未知恢复：只有券商权威查询确认原保护单仍为 `live`，才先耐久生成新撤单意图，下一轮再次复核后由唯一 Worker 写券商；没有盲目重发。共享环境的 `PA_AGENT_LIVE_TRADING_ENABLED` 与 `OKX_LIVE_ENABLED` 已明确关闭，模拟门保持开启。
- 2026-07-24 已重新读取用户提供的网页版 GPT 共享对话并核对正式资金规则：资金转入、转出、充值、提现和账户划转应由 PA 逐页读取 OKX 资金流水自动识别，不能计为交易盈利或亏损，也不能虚假抬高或压低资金调整后的历史最高净值；识别结果、流水水位和基准调整必须耐久留痕。资金变化本身不是交易信号，不触发订单；下一次真实有效入场仍须重新读取最新账户权益与最大可开量，按既定 10% 风险公式重新定仓。本轮已验真 Demo 的 `account/bills` 与 `account/subtypes` 只读权限，新增最近七天严格分页、USDT 转入/转出分类、账户总权益资金流校正纯计算和硬失败测试；同时保留权益变化后的 10% 定仓重算与 `no_order` 不进入定仓/下单。资金流/高水位的生产账本持久化、七天以上归档补链、Funding 充值/提现、非 USDT 时点换算、`totalEq` 运行态接线、50% 停止与人工恢复仍属于路线图阶段 4，尚未实现，不能把本轮余额验收或纯函数说成完整能力已完成。
- 2026-07-24 用户本轮现场确认是“其他资产换成 USDT / 账户内转换”，不是外部转入。真实 OKX Demo 只读复核显示 USDT `eq/cashBal/availBal=79041.2190279924`、`frozenBal=0`、账户 `totalEq=78976.40522916657`；最近账单中最新换币为 `type=2/subType=1` 的 `USDC-USDT`，时间为 `2026-07-24T03:06:31.671Z`，历史唯一 `type=1/subType=11` 外部转入为 `2026-07-17T08:04:08.320Z` 的 `5000 USDT`。本轮已修复：无活动 execution 时 Worker 仍刷新选定账户；GUI 正确读取 `okx-demo` 并把超过 90 秒的快照标为 `UNKNOWN`；OKX 快照保存 `totalEq/USDT eq/cashBal/availBal/frozenBal/uTime` 来源；Campaign 明确以 `usdt_equity` 为 10% 风险基数；提交前重新定仓，余额或风险输入变化时旧 `READY` 计划直接作废，不提交。当前 10% 风险公式仍遇到真实 `max_size_exceeded` 就硬阻断，不静默截断；生产资金流游标/高水位耐久账本仍是阶段 4 未完成项。Worker 已重启加载最终 Service/Adapter，心跳 PID `536` 于 `2026-07-24T03:40:46Z` 启动；无活动 execution 时已连续写入 `okx/okx-demo` 新快照，现场复核到 `snapshot_id=7367`，心跳与对账错误码为空。
- 2026-07-24 `WO-EXEC-03 Demo-S` 已完成真实 OKX Demo 单仓闭环：受控输入显式标记 `controlled_reproducible`，真实 SupervisorGate 使用 `codex-subscription / gpt-5.6-luna / medium` 返回 `allow_entry`；execution `328d0f7f-fef6-5e5e-bd39-dad92a66512b` 通过 `ExecutionController → ExecutionWorker → ExecutionService → OkxAdapter` 做空 `14921` 张，入场订单 `3769893223932182528` 全部成交，建立两笔原生保护后按主动离场流程撤销保护，离场订单 `3769895836211822592` 全部成交，账本最终 `closed`、剩余数量 `0`、已实现盈亏 `-21.3546079887406998`。收口只读回查为非零仓位 `0`、普通挂单 `0`、条件/OCO 挂单 `0`；这只证明 Demo 技术闭环，不是自然 PA 信号或 Live 绩效。
- 2026-07-24 最终 Campaign `52a4f507-a8f8-4975-b0bf-ddddf9ff901c` 已在空仓、空单、无活动 execution/命令/租约硬门下显式归档旧状态并恢复为 `active`，运行配置为 `10m / 20 / extreme_aggressive`；历史归属 execution 只有在耐久账本确认 `closed/canceled/rejected` 后才允许换配置，缺失、活动、`unknown/error` 均继续阻断。WinSW `PAAgentExecutionWorker` 已重启加载最终修复并恢复健康对账。进程 PID、账户和券商状态仍会变化，后续必须重新现场核验。旧 execution `8c0f83ab-fc6b-589e-8967-c4bd8f538015` 因旧 Worker 在券商写入前无法解析新增计划字段，已通过 Controller 明确作废为 `canceled`，原 `uncertain` 命令作为审计证据保留，未盲目重提。
- 2026-07-24 失败语义已补强：`max_size_exceeded` 记录为 `blocked:risk:max_size_exceeded` 并继续下一根已收盘 K 线；Worker 在券商调用前发现执行记录 schema 不兼容时明确返回 `failed:execution_record_invalid`；Demo 生命周期等待器允许 Worker 单次全局对账异常后继续读取执行账本，但 execution 自身进入 `needs_attention/unknown/error` 时仍立即硬阻断。九种入场/离场组合已通过真实生产链离线 Fake client 验收，ATR `2→4` 的价差翻倍、风险滑点率不改委托价、保护与主动离场模式隔离均有测试覆盖。
- 用户当前 GitHub 用户名为 `xiaojinlucky`；已明确要求 `xiaojinlucky/PA_Agent` 保持 `PUBLIC` 并公开发布，因为网页版 GPT 可以读取私有仓库，但 BioMNI 读取本项目依赖公开访问。给网页版 GPT 或 BioMNI 的启动指令必须明确这一点，不得要求改回 private。本地 `origin` 已改为规范地址 `git@github.com:xiaojinlucky/PA_Agent.git`。上一次交接包实时核对确认登录账号为 `xiaojinlucky`、权限为 `ADMIN`，当时本地 `main`、`origin/main` 与 GitHub API 返回的基线 SHA 均为 `38876644430302f0e2ac3310f072b31f95252469`；本轮提交/推送状态必须重新按 Git 实时核验。交易执行与大模型供应商接入已纳入该发布基线；环境文件、密钥、数据库、日志和运行态记录一律排除。
- 2026-07-23 历史基线曾冻结为 `15m` 主周期 + `1h`/`4h` 薄背景；该主周期已被 2026-07-24 的 `10m` 正式决定取代，资料对“嵌套结构、少量相关周期、高周期只作背景”的约束继续有效。
- 2026-07-23 历史实现已完成入场和主动离场独立三选项及 ATR 滑点；2026-07-24 现役 Demo-S 配置进一步固定为进出场均 `limit_with_slippage / 0.50 × ATR14`，主周期改为严格聚合 `10m`。
- 2026-07-23 本轮离线验证：多周期/ATR/提示词/适配器/运行器定向套件 `209` 通过、`0` 失败；ExecutionService/Controller/Worker/Store/生命周期回归 `97` 通过、`0` 失败；`compileall`、改动范围未定义名/语法检查和 `git diff --check` 通过。新版 Campaign `e396d8eb-e3bf-498a-83c6-8654c0528fbb` 已启动，但当前被本机 GUI 会话持有的 `NEW_RISK` 短租约阻塞在首轮分析前，尚未产生本轮新的真实订单证据；不撤销 GUI 租约、不把等待说成交易。普通 GUI 行情源尚未接入自动高周期读取，本轮先把真实 Demo 主运行路径打通。
- 2026-07-23 `WO-RISK-02` 已完成本机适配：新增纯 `Decimal` 风险计算器，按 Demo USDT 权益的 10% 风险预算、PA entry/stop、`ctVal`/`ctMult`、费用、滑点、`lotSz`/`minSz` 和方向最大可开张数计算首仓张数；风险结果在现有 `ExecutionController → ExecutionWorker` 链路中进入 `ExecutionPlan.quantity`。已有新增风险租约时，定仓改量会先撤销旧租约并按新配置指纹重新授权。没有 PA entry/stop 时只做只读预检、不计算数量、不回退 `equity_10pct_notional`；本轮未修改 `pa_agent/execution/`，未连接真实券商，未发送 Demo/Live 订单。
- 用户已重新授权前端改版，但明确要求先由 Gemini Stitch 设计。`docs/FRONTEND_REDESIGN_PRD.md` 已完成，生产视觉改版、Pencil 状态流和 Figma 组件库尚未完成；OmicOS 只参考“订阅登录/API 接入”分组与信息层级，不照抄品牌风格。
- 已把交易写入从 GUI 与自动循环中移出：`ExecutionController` 只创建计划、发放短期新增风险租约和写入持久命令，单实例 `ExecutionWorker` 是唯一构造 `ExecutionService`、连接券商和执行命令的进程。命令状态、后台心跳与最后成功对账分别落在 `records/execution_control.sqlite3`，交易真值仍只有 `records/execution.sqlite3`；减险写入不依赖新增风险租约，但继续受环境硬门、不可变账户路由和持久停写标记约束。生产账本检查时已是 schema v2，因此没有重复迁移；已先创建一致性备份。WinSW 服务已安装为 `LocalService`、自动延迟启动并保持运行。Live 开关和容量不是本轮证据，不能据历史记录断言已打开或关闭；本轮只验证 Demo/离线链路，未提交 Live 订单。GUI 检测到该服务时只请求 Windows 启动服务；除系统明确返回服务不存在（1060）外，服务控制程序缺失、超时、权限或状态不明均禁止备用 Python Worker。
- Longbridge 行情数据源仍仅使用 `QuoteContext`；三个交易档案分别创建独立上下文。模拟账户绝不回退实盘且不允许盘前/盘后；日内账户只在提交前明确最大数量不足时回退综合账户，任何网络/认证/未知/已提交状态均不回退。
- OKX 不硬编码黄金；动态规格测试覆盖 `XAUT-USDT`、`XAU-USDT-SWAP`、`BTC-USDT`、`BTC-USDT-SWAP`。现货与永续有独立数量、保护和盈亏语义。
- 所有真实写操作要求 `PA_AGENT_LIVE_TRADING_ENABLED=true`、当前进程会话确认；OKX Live 还要求 `OKX_LIVE_ENABLED=true`。Longbridge 与 OKX Demo 的模拟写操作改用独立 `PA_AGENT_PAPER_TRADING_ENABLED` 和 `启用模拟交易`。历史记录曾记录 Live 开关和容量，但本轮未重新核验；Demo 5 分钟自动循环的提交记录属于历史证据，不能替代本轮 WO-S2A-01 的离线验收。
- 2026-07-23 02:47，旧版 5 分钟 Demo 运行器已完成 7 笔 `10` 张限价空单的真实 OKX Demo 提交；7 笔均被券商接受、成交量均为 `0`，在 `270` 秒内未回到挂单价后均已撤销，当前无仓位。为验证即时执行路径，已将运行器专用阶段二提示切为 `market_when_valid`：仅当能在最新已收盘 K1 附近构造合法三价时输出市价单，否则如实不下单；该规则不影响 GUI 和日常 PA 分析。新 campaign `c928e2bf...` 已连续完成 8 根 5 分钟 K 线的真实分析（0 失败、0 新 execution）；均因尖峰追单/下沿支撑/无回撤确认或区间转换被模型判为不下单。Runner、`ExecutionWorker`、心跳、账本和 OKX Demo 私有回查一致且正常。策略运行器短暂停止后，已通过同一 Controller/Worker 链路完成独立 `okx_demo_lifecycle_canary`：Demo `10` 张市价入场、成交回读、两笔原生保护、受控离场，最终 `closed`、剩余 `0`，Demo 无非零仓位；随后策略运行器已恢复。
- 2026-07-23 22:26-22:41 的现场运行已重新核验：旧 Campaign 在唯一 `NEW_RISK` 租约到期前 7 秒启动，因租约竞争直接结束且只完成 `0` 根分析；本轮新增 `NewRiskLeaseUnavailable` 类型错误，Campaign 把这一个明确的短暂租约竞争放回循环重试，硬门关闭和其他真实错误仍直接失败。修复后的定向 Campaign/Controller 测试为 `55` 通过、`0` 失败，`compileall` 和 `git diff --check` 通过。
- 同一现场按用户授权完成一次真实 OKX Demo 生命周期 canary：execution `3f57bed9-6997-58ff-9e6f-e3b6cd1554c2`，`15358` 张市价入场，成交回读后建立两笔原生保护，主动离场成交，账本最终 `closed`、`needs_attention=false`；随后 OKX Demo 只读回查为净仓 `0`、普通挂单 `0`、待生效算法单 `0`。该 canary 明确标记为 `okx_demo_lifecycle_canary`，不计入 PA 策略绩效。
- 历史 15 分钟 Campaign `d6936271-7d86-49b7-8e94-e069ed5bac2f` 已由当前 10 分钟 Campaign 取代；该条只保留为旧现场快照。
- `docs/GPT5_6SOL_HANDOFF.md` 与 `docs/LOCAL_EXECUTION_CONTEXT.md` 是开发前历史快照，已加醒目标记；当前实现真值以本文件与 `docs/LIVE_TRADING_DESIGN.md` 为准。
- AI 模型首轮范围已按用户最新决定收敛为 Codex ChatGPT 订阅、Kimi API、DeepSeek API；小米 MiMo 暂不纳入本轮可用性验收。当前配置有 Codex Luna、Codex Terra、Kimi、DeepSeek 四个已验证档案，活动档案为 `codex-subscription` / `gpt-5.6-luna`；Luna 与 Terra 是同一 Codex 订阅通道下的两个模型档案，不是新增供应商。
- Codex 登录故障根因是程序误选了 WindowsApps 中存在但不可执行的无后缀资源。现在每个 `.exe` 候选都必须实际通过 `--version`，当前使用 `C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\codex.exe`；ChatGPT 登录状态和独立随机挑战均通过。
- 模型下拉框采用“基础目录 + 当前账号刷新”：刷新成功后只展示账号接口返回模型，基础目录只补同 ID 能力；刷新失败保留原列表。2026-07-20 真实目录为 Codex 6、Kimi 12、DeepSeek 2，当前三个选中模型都在各自账号目录内。
- API Key 显示/隐藏已修复，切换供应商或档案会强制重新遮罩；模型目录任务按适配器、地址和 Key 哈希隔离迟到结果。Thinking、推理强度、速度与上下文行按模型能力显示，未知上下文全链路保持 `None` / “尚未确认”，目录能力变化会强制重新测试。
- 上下文上限已改为模型目录驱动的只读元数据。Codex 原生持久线程与恢复用于自由追问，并允许官方客户端在达到模型阈值时自动 Compact；正式两阶段分析仍是无历史复用的一次性请求。线程恢复已验证，尚未用超长真实会话触发并观测一次 Compact 事件。
- Codex、Kimi、DeepSeek 三条正式通道已完成真实随机挑战；当前四个档案的持久验证指纹均有效。活动档案后来由用户配置为 `codex-subscription` / `gpt-5.6-luna`，Terra 档案仍保留并有效；API Key 不写入仓库配置，而是按需从仓库外共享环境读取。
- 已定位用户将 Codex 从 Sol 改为 Terra 后“测试通过但激活失败”的直接原因：日志在 2026-07-20 11:14 两次记录 `SettingsConflictError`，不是登录或 Terra 模型无效。模型测试期间普通设置已推进磁盘 revision，旧候选因此被整体拒绝。现在 revision 判断、快照读取、AI 状态比较、合并与原子替换处于同一临界区；进程内 `RLock` 串行线程，文件锁串行外部进程。磁盘 AI 档案未被并发修改时保留最新普通设置并安全合并已验证候选；另一窗口也改 AI 档案时仍失败关闭。账号实时目录确认 `gpt-5.6-terra` 存在，独立真实随机挑战四项全部通过。该修复完成后 Terra 曾成功激活；用户后来又把活动档案改为 `gpt-5.6-luna`，当前持久配置 revision 为 61。

## 历史现场验证快照（截至 2026-07-22/23；本轮未重新核验）

> 本节记录上一轮现场核验结果；本轮只新增 WO-S2A-01 的 Demo/离线验证，不重新证明这些运行态结论。

- 2026-07-22 交易执行、OKX/Longbridge、数据源和交易窗口相关分组为 374 通过、0 失败；Codex/Kimi/DeepSeek、档案、目录和模型能力相关分组为 247 通过、0 失败；四个 Qt E2E 为 4 通过、0 失败；取消程序擅自扩大止损的直接风险指标回归为 51 通过、0 失败。全仓现状为 1251 通过、32 失败、3 跳过；其中 4 项是 AkShare 真实联网超时，27 项经两名独立审查者确认属于 HEAD 既有测试契约/旧输入而非本轮回归，1 项日志遮罩性质测试是日志为空而非明文泄漏。不能把全仓描述为全绿。
- 生产 `records/execution.sqlite3` 已是 schema v2，`quick_check=ok`；截至 2026-07-22 有 11 条历史 execution。历史 OKX Demo execution `cc94657a-d4b3-5ba3-b4d4-0d6fd62ae595` 的保护单经精确查询、未触发列表和三个月历史确认不存在；系统没有自动重发保护。2026-07-20 使用持久命令 `08c2e0a4-4eec-447e-8d22-f9921f95defa` 只执行减险离场，命令为 `succeeded`，execution 已为 `closed`、剩余数量 0、`needs_attention=false`。2026-07-23 的 5 分钟 Demo 循环已完成首次 AI→计划→Worker→OKX Demo 提交：第一笔 10 张限价空单因旧的 120 秒超时未成交而撤销，随即把快速循环的入场有效期改为 270 秒。新运行器随后提交第二笔 10 张限价空单，先越过旧的 120 秒节点保持 `live`，随后在约 270 秒后未成交撤销；券商只读回查与账本一致。后续成交、保护和离场以执行账本的实时状态为准。2026-07-22 又补齐算法单未触发/历史接口的完整分页：只有精确查询和全部分页都成功且没有命中时才能标为 `confirmed_absent`；第二页失败、游标缺失/重复或超过安全上限均保持未知并禁止自动补发。安装前备份大小 1,155,072 字节、SHA-256 为 `cd8e4652a37b54c2c6ae52b7233307e1efae46ac27e2b395b271e56423a8e771`，已复核为生产账本的精确历史前缀。
- WinSW 服务当前为 `Running`、自动延迟启动、`NT AUTHORITY\LocalService`；2026-07-23 Worker 心跳与最后成功对账持续更新。用户已明确授权关闭 Demo 中不属于 PA 的 `XAU-USDT-SWAP` 10 张净多仓，市价平仓后只读回查确认该外部仓已不存在。Demo 自动循环现在独占 PA 自己创建的订单与执行账本，不接管外部仓位。
- 权限目录层和旧文件已按“代码/WinSW 只读执行、共享 env 只读、records/logs 可写”收紧。管理员脚本先修复了隐藏 PowerShell 不支持终端录屏、Windows PowerShell 5.1 不支持换行管道两项兼容问题，于 2026-07-20 16:00 成功完成；`pa_agent`、`records`、`logs` 中保留 `Authenticated Users: Modify/Write/FullControl` 的文件复扫为 0。GUI 不承载执行 Worker；WinSW 服务与 GUI 保持独立进程边界。2026-07-22 实时复核服务仍为 `Running`、自动启动，账户仍为 `NT AUTHORITY\LocalService`。
- Longbridge 综合、日内和 paper 三档的账户/行情/容量结论均是历史只读证据；本轮未重新核验。OKX Demo 私有读和动态品种结论也不替代本轮测试；OKX Live 状态待现场重新核验。
- Codex、Kimi、DeepSeek 三条通道均通过真实随机挑战；当前活动模型后来改为 Codex Luna，Terra 仍为另一个已验证档案。Codex 已完成一次隔离的 PA 两阶段真实分析，未连接执行服务、未下单。模型目录、API Key 遮罩、按模型显示能力、只读上下文上限和 Codex 持久追问线程均已实现；用户可在 Luna/Terra 之间切换，但纯手工点击链路仍由用户完成，Codex 不控制鼠标。
- Terra 激活并发回归与设置持久化回归共 59 通过、0 失败；静态未定义名/语法检查通过。独立只读复验六维全部通过、阻塞问题 0。更宽的 AI 现状套件另有 1 个与本次无关的既有 `openclaw_cs` 路由失败，未纳入本次 Terra 修复。
- OKX Demo 巡检已修复价格步长浮点尾差、程序改写模型止损、恢复门禁、过期 `READY` 计划补提、保护单权威查无和 Worker 心跳覆盖新状态的问题；自动循环始终限定 Demo、`XAU-USDT-SWAP`、5m、cross、10 张，限价入场有效期 270 秒。历史仓位已经按上述减险命令安全收口。
- MT5 是否显示形成中 XAUUSD K 线取决于券商当前是否开市；2026-07-20 周末无新 tick 时不画过期虚线属于正确行为，仍需开市后可见复验。上游仅 AkShare 字符串时间解析修复值得以后小范围移植，不能整体合并。

## 已知边界

- OKX Demo 私有只读链路和 5 分钟自动循环属于历史运行证据；本轮代码只允许 Demo/离线验收。Live 状态必须每次按现场配置、实时容量和券商预检重新核验，不能从旧文档推断。
- Longbridge 两账户当前 GLD.US 可交易数量为 0；在账户资金/资格变化前，真实预检会阻断。
- Longbridge paper 的撮合和现金规则与实盘不同，且美股只支持常规交易时段；模拟结果不能替代综合/日内账户的真实可交易验收。
- Longbridge Legacy Token 更新时必须来自同一绑定账户；类型或账户 ID 不一致会在创建交易会话前失败，不能通过修改档案名称绕过。
- Longbridge Legacy Token 到期仍需人工更新；账户总盈亏接口没有可靠的已实现/未实现拆分，PA 不伪造拆分。
- Longbridge 止损是券商端原生 MIT，止盈条件由 PA 软件轮询；关闭 PA 后原生保护仍在，但软件止盈和状态回写暂停。OKX 保护使用券商端 OCO。
- 最小真实 Canary 未获本轮授权；后续必须对具体券商、账户、品种、方向和数量重新单独确认。
- Codex 订阅不产生单独的 OpenAI API 按 Token 账单，但仍受 ChatGPT 套餐用量和频率限制；PA 以纯文本、禁工具、禁技能说明、临时空目录和清理后的环境调用，不读取或复制 Codex 登录凭据。
- PA 后端信息架构只有在提交边界核清并安全推送后，才交给网页版 ChatGPT 通过 GitHub 只读审查；仓库公开性已由用户明确接受。网页版工单仍必须由本机 Codex 按真实 skills、memory、运行态和交易安全边界校正后才能执行。
- 用户给出的网页版 ChatGPT 项目对话需要登录态；本机内置浏览器当前被引导到登录页，因此本轮无法重新读取原文。已有 PRD/加固计划只可视为之前整理出的结果，不能冒充本轮已经重新核对过该对话。
- 当前代码和 WinSW 验收不等于可以打开实盘。数据库备份、schema v2 核对、独立守护、GUI/Worker 进程树隔离、旧运行态文件 ACL 和历史 Demo execution 安全收口已经完成；更完整的券商启动扫描、持续持仓/保护真值核对、Longbridge 私有推送与全局限速仍未完成，因此不能宣称长期无人值守实盘已完成。
- 当前持久交易配置经本地 `config/settings.json` 核对为 revision `71`：OKX Demo、`XAU-USDT-SWAP`、严格聚合 `10m`、`extreme_aggressive`、决策与执行最低置信度均为 `20`、进出场均 `limit_with_slippage / 0.50 × ATR14`。数量不再把持久固定值当风险真值，每次候选计划按实时权益、止损距离、合约规格和最大可开数重新计算。
- 2026-07-23 已按用户授权关闭原有的外部 Demo 10 张仓位；当前由 PA 自动循环创建、监控和收口自己的订单，不再存在阻塞同品种新计划的外部仓位。

## 上一轮接手现场快照（2026-07-23 UTC；本轮未重新核验）

- 上一轮曾把 PA 模式固定为 `aggressive / 30 / 15m`；该历史配置已被 2026-07-24 的 `extreme_aggressive / 20 / 10m` 正式决定取代，仍只限定 OKX Demo，不改变任何 OKX Live 路由。
- 在启动新运行器前已完成只读核验：Demo 无非零 XAU-USDT-SWAP 仓位、无普通挂单、无 OCO/触发保护单；本地无活动 execution/命令；`PAAgentExecutionWorker` 服务运行且心跳、最后成功对账均正常。实时预检成功，最近一次按约 5,000 USDT 权益、约 4,136 USDT 价格解析为 `120` 张；这是容量计算结果，不是策略下单。
- 上次只读快照记录 Campaign `6c7bc424-6d00-4bd8-9c67-fca1cfa62b39` 于 `2026-07-22T20:12:56Z` 启动，并完成前两根 15 分钟 K 线真实两阶段分析（2 成功、0 失败、0 strategy execution）：第一根交易置信度 45、价格在下方支撑附近、信号 K 线无效且没有可定义的即时止损；第二根交易置信度 35、外包阳线没有确认跟随。两次订单类型都明确为“不下单”，Controller 均因 `no_order` 正确未创建计划。这不是漏单，也不是 canary 或策略绩效；本轮实时状态探针超时，当前是否仍 active 待重查。
- 自动巡检 `okx-24` 已迁移为“PA Agent OKX Demo 5 分钟只读巡检”，绑定当前 PA 审查线程，到本 Campaign 到期前只读检查 Campaign、进程、Worker、账本和 Demo 仓位/订单/保护摘要。它不得写券商、改配置、启停进程、运行 canary 或修复代码。
- 2026-07-23 已完成统一路线图阶段 0 的压缩真值冻结：阶段 0 发布基线为 `38876644430302f0e2ac3310f072b31f95252469`；三份外部 PRD 原文归档到 `docs/prd/`，并明确“当前能力以代码/当前证据为准、目标行为以已确认 PRD 为准”；`docs/ROADMAP.md` 已切换为无界面主脊柱优先路线，`docs/SAFETY_INVARIANTS.md` 和 `docs/BASELINE_AUDIT.md` 继续提供安全和状态边界。阶段 0 未修改 `pa_agent/execution/` 下任何 Python 行为，未执行交易；历史验证证据保留原 SHA 并加历史标记。本轮提交/推送状态以 Git 实时核验为准。
- 2026-07-23 已推进统一路线图阶段 1 的第一块可验收实现：`pa_agent/gui/read_models.py` 只读组合行情连接/订阅、Worker 心跳与对账、账户快照和执行账本；`AppContext` 复用现有两个 SQLite 存储，主窗口状态条显示每个字段的“已确认/计划/规划/未知”语义，数据源事务切换后读取对象同步更新；字段映射见 `docs/READ_MODEL_MAPPING.md`。读取层不发网络请求、不创建计划、不写券商；完整导航页面、截图和 OKX 端到端链路验收仍未完成。读取层 3 项、主窗口状态 2 项、执行控制器/Worker 33 项、账本/服务/生命周期 56 项定向测试已随当前发布基线验证通过。
- 2026-07-23 已新增 GitHub 交接总账 `docs/CODEX_HANDOFF.md` 和网页版 GPT 启动指令 `docs/WEB_GPT_CONTROLLER_PROMPT.md`，用于把本项目规则、历史需求、已发现问题、已完成修改、未完成事项和硬验收边界交给外部总控；它们不包含凭据、数据库、日志或运行态文件。
- 2026-07-23 已推进阶段 1 首张工单 `WO-S2A-01`：新增严格 `SupervisorDecision`、不可变监督输入快照、主/备监督模型和确定性 `block_entry`，监督结论按 Campaign + 已收盘 K 线 + PA 分析摘要原子落盘；同一结论重启复用，拒绝不调用 `prepare_analysis()`，放行继续使用现有 `ExecutionController` / `ExecutionWorker`。角色档案已增加 PA 主/备、监督主/备四个绑定字段；当前 Campaign 实际使用 PA 主档案和已配置的监督主档案，监督备用档案只有在本机设置明确绑定且验证通过时才启用，空绑定直接确定性阻断，PA 备用档案先做独立验证并留给后续 PA 工单。新增离线硬验收使用真实 Controller/Worker 加内存 FakeAdapter，证明第一根 K 线拒绝时命令数为 0，第二根放行时恰好一条 Demo `SUBMIT`，重启不重复模型调用或命令；本轮未连接真实券商、未发送真实订单，`pa_agent/execution/` 未修改。
