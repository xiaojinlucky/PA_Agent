# PA_Agent Context

## 当前状态（2026-07-22）

- 用户当前 GitHub 用户名为 `xiaojinlucky`；2026-07-22 已明确授权 `xiaojinlucky/PA_Agent` 保持 `PUBLIC` 并公开发布。本地 `origin` 已改为规范地址 `git@github.com:xiaojinlucky/PA_Agent.git`。实时核对确认登录账号为 `xiaojinlucky`、权限为 `ADMIN`，本地 `main`、`origin/main` 与 GitHub API 返回的基线 SHA 均为 `3ae95321eef706889cf901c63a1c293f11f7608d`。交易执行与大模型供应商接入是同一批待发布工作区改动，但环境文件、密钥、数据库、日志和运行态记录一律排除。
- 用户已重新授权前端改版，但明确要求先由 Gemini Stitch 设计。`docs/FRONTEND_REDESIGN_PRD.md` 已完成，生产视觉改版、Pencil 状态流和 Figma 组件库尚未完成；OmicOS 只参考“订阅登录/API 接入”分组与信息层级，不照抄品牌风格。
- 已把交易写入从 GUI 和 24 小时实验中移出：`ExecutionController` 只创建计划、发放短期新增风险租约和写入持久命令，单实例 `ExecutionWorker` 是唯一构造 `ExecutionService`、连接券商和执行命令的进程。命令状态、后台心跳与最后成功对账分别落在 `records/execution_control.sqlite3`，交易真值仍只有 `records/execution.sqlite3`；减险写入不依赖新增风险租约，但继续受环境硬门、不可变账户路由和持久停写标记约束。生产账本检查时已是 schema v2，因此没有重复迁移；已先创建一致性备份。WinSW 服务已安装为 `LocalService`、自动延迟启动并保持运行。当前共享环境与服务均保持 PA Live、OKX Live 为 `false`；经用户授权，模拟写入门 `PA_AGENT_PAPER_TRADING_ENABLED` 为 `true`。GUI 检测到该服务时只请求 Windows 启动服务；除系统明确返回服务不存在（1060）外，服务控制程序缺失、超时、权限或状态不明均禁止备用 Python Worker。
- Longbridge 行情数据源仍仅使用 `QuoteContext`；三个交易档案分别创建独立上下文。模拟账户绝不回退实盘且不允许盘前/盘后；日内账户只在提交前明确最大数量不足时回退综合账户，任何网络/认证/未知/已提交状态均不回退。
- OKX 不硬编码黄金；动态规格测试覆盖 `XAUT-USDT`、`XAU-USDT-SWAP`、`BTC-USDT`、`BTC-USDT-SWAP`。现货与永续有独立数量、保护和盈亏语义。
- 所有真实写操作要求 `PA_AGENT_LIVE_TRADING_ENABLED=true`、当前进程会话确认；OKX Live 还要求 `OKX_LIVE_ENABLED=true`。Longbridge 与 OKX Demo 的模拟写操作改用独立 `PA_AGENT_PAPER_TRADING_ENABLED` 和 `启用模拟交易`。当前实盘开关保持关闭；本轮只对历史 OKX Demo execution 执行了用户授权的减险离场，没有新增风险订单。
- `docs/GPT5_6SOL_HANDOFF.md` 与 `docs/LOCAL_EXECUTION_CONTEXT.md` 是开发前历史快照，已加醒目标记；当前实现真值以本文件与 `docs/LIVE_TRADING_DESIGN.md` 为准。
- AI 模型首轮范围已按用户最新决定收敛为 Codex ChatGPT 订阅、Kimi API、DeepSeek API；小米 MiMo 暂不纳入本轮可用性验收。当前配置有 Codex Luna、Codex Terra、Kimi、DeepSeek 四个已验证档案，活动档案为 `codex-subscription` / `gpt-5.6-luna`；Luna 与 Terra 是同一 Codex 订阅通道下的两个模型档案，不是新增供应商。
- Codex 登录故障根因是程序误选了 WindowsApps 中存在但不可执行的无后缀资源。现在每个 `.exe` 候选都必须实际通过 `--version`，当前使用 `C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\codex.exe`；ChatGPT 登录状态和独立随机挑战均通过。
- 模型下拉框采用“基础目录 + 当前账号刷新”：刷新成功后只展示账号接口返回模型，基础目录只补同 ID 能力；刷新失败保留原列表。2026-07-20 真实目录为 Codex 6、Kimi 12、DeepSeek 2，当前三个选中模型都在各自账号目录内。
- API Key 显示/隐藏已修复，切换供应商或档案会强制重新遮罩；模型目录任务按适配器、地址和 Key 哈希隔离迟到结果。Thinking、推理强度、速度与上下文行按模型能力显示，未知上下文全链路保持 `None` / “尚未确认”，目录能力变化会强制重新测试。
- 上下文上限已改为模型目录驱动的只读元数据。Codex 原生持久线程与恢复用于自由追问，并允许官方客户端在达到模型阈值时自动 Compact；正式两阶段分析仍是无历史复用的一次性请求。线程恢复已验证，尚未用超长真实会话触发并观测一次 Compact 事件。
- Codex、Kimi、DeepSeek 三条正式通道已完成真实随机挑战；当前四个档案的持久验证指纹均有效。活动档案后来由用户配置为 `codex-subscription` / `gpt-5.6-luna`，Terra 档案仍保留并有效；API Key 不写入仓库配置，而是按需从仓库外共享环境读取。
- 已定位用户将 Codex 从 Sol 改为 Terra 后“测试通过但激活失败”的直接原因：日志在 2026-07-20 11:14 两次记录 `SettingsConflictError`，不是登录或 Terra 模型无效。模型测试期间普通设置已推进磁盘 revision，旧候选因此被整体拒绝。现在 revision 判断、快照读取、AI 状态比较、合并与原子替换处于同一临界区；进程内 `RLock` 串行线程，文件锁串行外部进程。磁盘 AI 档案未被并发修改时保留最新普通设置并安全合并已验证候选；另一窗口也改 AI 档案时仍失败关闭。账号实时目录确认 `gpt-5.6-terra` 存在，独立真实随机挑战四项全部通过。该修复完成后 Terra 曾成功激活；用户后来又把活动档案改为 `gpt-5.6-luna`，当前持久配置 revision 为 61。

## 当前验证

- 2026-07-22 交易执行、OKX/Longbridge、数据源和交易窗口相关分组为 374 通过、0 失败；Codex/Kimi/DeepSeek、档案、目录和模型能力相关分组为 247 通过、0 失败；四个 Qt E2E 为 4 通过、0 失败；取消程序擅自扩大止损的直接风险指标回归为 51 通过、0 失败。全仓现状为 1251 通过、32 失败、3 跳过；其中 4 项是 AkShare 真实联网超时，27 项经两名独立审查者确认属于 HEAD 既有测试契约/旧输入而非本轮回归，1 项日志遮罩性质测试是日志为空而非明文泄漏。不能把全仓描述为全绿。
- 生产 `records/execution.sqlite3` 已是 schema v2，`quick_check=ok`；共 11 条历史 execution。历史 OKX Demo execution `cc94657a-d4b3-5ba3-b4d4-0d6fd62ae595` 的保护单经精确查询、未触发列表和三个月历史确认不存在；系统没有自动重发保护。2026-07-20 使用持久命令 `08c2e0a4-4eec-447e-8d22-f9921f95defa` 只执行减险离场，命令为 `succeeded`，execution 已为 `closed`、剩余数量 0、`needs_attention=false`，当前活动 execution 为 0。2026-07-22 又补齐算法单未触发/历史接口的完整分页：只有精确查询和全部分页都成功且没有命中时才能标为 `confirmed_absent`；第二页失败、游标缺失/重复或超过安全上限均保持未知并禁止自动补发。安装前备份大小 1,155,072 字节、SHA-256 为 `cd8e4652a37b54c2c6ae52b7233307e1efae46ac27e2b395b271e56423a8e771`，已复核为生产账本的精确历史前缀。
- WinSW 服务当前为 `Running`、自动延迟启动、`NT AUTHORITY\LocalService`；2026-07-22 Worker 已重启加载本轮最新代码，心跳与最后成功对账持续更新。PA 活动 execution 为 0、有效新增风险租约为 0、待执行/执行中命令为 0。OKX Demo 券商侧现在另有一笔不属于 PA 的 `XAU-USDT-SWAP` 10 张净多仓：2026-07-21 13:46（北京时间）成交，客户订单号为空，而 PA 本实验固定为 1 张且本地没有相应 execution/命令。账户普通挂单 0、条件单 0。PA 不接管该外部仓，且 OKX 预检会因既有同品种持仓拒绝新开仓；用户自行关闭或明确处理该仓前，PA 无法在同账户继续开该永续品种。
- 权限目录层和旧文件已按“代码/WinSW 只读执行、共享 env 只读、records/logs 可写”收紧。管理员脚本先修复了隐藏 PowerShell 不支持终端录屏、Windows PowerShell 5.1 不支持换行管道两项兼容问题，于 2026-07-20 16:00 成功完成；`pa_agent`、`records`、`logs` 中保留 `Authenticated Users: Modify/Write/FullControl` 的文件复扫为 0。GUI 不承载执行 Worker；WinSW 服务与 GUI 保持独立进程边界。2026-07-22 实时复核服务仍为 `Running`、自动启动，账户仍为 `NT AUTHORITY\LocalService`。
- Longbridge 综合、日内和 paper 三档均完成真实只读账户/行情/容量检查；综合与日内当前无持仓且 GLD.US 最大数量为 0，paper 容量非零。OKX Demo 私有只读和四个动态品种规格可用，Live 硬门关闭。
- Codex、Kimi、DeepSeek 三条通道均通过真实随机挑战；当前活动模型后来改为 Codex Luna，Terra 仍为另一个已验证档案。Codex 已完成一次隔离的 PA 两阶段真实分析，未连接执行服务、未下单。模型目录、API Key 遮罩、按模型显示能力、只读上下文上限和 Codex 持久追问线程均已实现；用户可在 Luna/Terra 之间切换，但纯手工点击链路仍由用户完成，Codex 不控制鼠标。
- Terra 激活并发回归与设置持久化回归共 59 通过、0 失败；静态未定义名/语法检查通过。独立只读复验六维全部通过、阻塞问题 0。更宽的 AI 现状套件另有 1 个与本次无关的既有 `openclaw_cs` 路由失败，未纳入本次 Terra 修复。
- OKX Demo 巡检已修复价格步长浮点尾差、程序改写模型止损、恢复门禁、过期 `READY` 计划补提、保护单权威查无和 Worker 心跳覆盖新状态的问题；实验始终限定 Demo、`XAU-USDT-SWAP`、30m、cross、1 张。历史仓位已经按上述减险命令安全收口。
- MT5 是否显示形成中 XAUUSD K 线取决于券商当前是否开市；2026-07-20 周末无新 tick 时不画过期虚线属于正确行为，仍需开市后可见复验。上游仅 AkShare 字符串时间解析修复值得以后小范围移植，不能整体合并。

## 已知边界

- OKX Demo Passphrase 已配置且私有只读链路可用；24 小时实验只允许 Demo 环境。用户提供的 API 页面曾显示提币权限且未设置 IP 白名单，因此任何未来 OKX Live 启用仍必须重新核对权限并移除提币权限。
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
- 当前持久交易配置后来变为 OKX、自动执行开启，但品种为空且数量字段含非法文本，因此计划构建会失败关闭；它不是可用交易配置。用户实际启用前必须在界面保存明确的券商、环境、品种和数量，系统不会猜测或修复下单参数。Longbridge paper 的共享模拟门和 Worker 健康链路此前已用一份不落盘的有效路由完成“启用 → 停用”复验。
- 当前 OKX Demo 外部 10 张 `XAU-USDT-SWAP` 仓位没有 PA 客户订单号且无保护算法单。PA 不会自动接管、补保护或离场；同品种新计划会在预检阶段被拒绝。这是账户当前状态，不是 PA execution，若要处理必须由用户明确授权具体动作。
