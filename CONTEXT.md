# PA_Agent Context

## 当前状态（2026-07-17）

- 私有仓库：`Jinqingchang/PA_Agent`；完整实盘闭环已提交为 `769e40fdb2ecfaf6ab04ccbaa3c49a2b91097800`，Longbridge 模拟账户三档兼容已提交为 `2e43d9dee9eb2fbb49a8ee9c060f02b69ef7fbc0`。
- 用户已终止全部前端重设计；本轮只增加交易所必需的功能配置/状态窗口，不做视觉重构。
- 已实现默认关闭的执行闭环：严格分析记录落盘、白名单计划构建、SQLite 强账本、会话/环境硬门、重启对账、Longbridge 模拟/综合/日内三档路由、OKX 现货/永续、入场/部分成交/保护/主动离场、账户资金与盈亏回写。
- Longbridge 行情数据源仍仅使用 `QuoteContext`；三个交易档案分别创建独立上下文。模拟账户绝不回退实盘且不允许盘前/盘后；日内账户只在提交前明确最大数量不足时回退综合账户，任何网络/认证/未知/已提交状态均不回退。
- OKX 不硬编码黄金；动态规格测试覆盖 `XAUT-USDT`、`XAU-USDT-SWAP`、`BTC-USDT`、`BTC-USDT-SWAP`。现货与永续有独立数量、保护和盈亏语义。
- 所有真实写操作要求 `PA_AGENT_LIVE_TRADING_ENABLED=true`、当前进程会话确认；OKX Live 还要求 `OKX_LIVE_ENABLED=true`。Longbridge 模拟写操作改用独立 `PA_AGENT_PAPER_TRADING_ENABLED` 和 `启用模拟交易`。当前实盘开关保持关闭，没有发送模拟或真实订单。
- `docs/GPT5_6SOL_HANDOFF.md` 与 `docs/LOCAL_EXECUTION_CONTEXT.md` 是开发前历史快照，已加醒目标记；当前实现真值以本文件与 `docs/LIVE_TRADING_DESIGN.md` 为准。

## 当前验证

- 主线程最终执行域回归：91 通过 / 0 失败；独立严格审查线程复跑执行域 91 通过 / 0 失败、相邻持久化/UI/集成范围 43 通过 / 0 失败。两组有严格持久化测试重叠，不相加冒充独立用例总数。覆盖 OKX 现货/永续、Longbridge 日内不足回退综合账户，以及入场、部分成交、保护、主动退出、UNKNOWN 和重启恢复。
- 扩大相邻扫描：70 通过 / 1 失败；`tests/unit` 全量现状扫描：860 通过 / 28 失败。失败均未作为本轮通过证据，集中在旧阶段二校验、决策连续性、预测面板和追问历史等非执行域；详见 `docs/VALIDATION_EVIDENCE.md`。
- `compileall pa_agent`、新增执行域定向 Ruff 与 `git diff --check` 均通过。
- Longbridge 两套真实账户只读连接、余额、持仓、盈亏摘要、GLD.US 静态信息/最大数量和历史备注查询通过；两账户当前均无持仓，GLD.US 现金/融资最大数量均为 0。
- Longbridge paper Token 账户类型、余额、持仓、盈亏、GLD.US 报价与最大数量均通过真实只读验证，且容量非零；PA 当前默认选中 paper，但执行模块与自动执行仍关闭。
- OKX 公共服务器时间与四个现货/永续品种动态规格查询通过；私有只读仍因 `OKX_PASSPHRASE` 为空而阻断。
- 原有 Qt E2E `tests/e2e/test_smoke_happy_path.py` 单独运行 90 秒仍无最终汇总，进程已停止；这是可选测试框架整治项，不能宣称 E2E 通过。
- 独立六维严格审查首轮提出 7 个阻塞项，主线程全部修复并补测；第二轮复验结论为 PASS，阻塞落地的问题为 0，审查线程未修改代码。
- Longbridge paper 三档扩展最终回归：执行域 131 通过 / 0 失败，相邻范围 44 通过 / 0 失败；独立审查首轮发现 Token 错放和未保存切换 2 个阻塞项，修复后复跑 83 通过 / 0 失败并给出 PASS。三档 Token 类型与账户 ID 绑定检查通过，paper 真实只读容量非零，券商写调用 0 次。

## 已知边界

- OKX 缺 Passphrase；现有 Key 曾显示含提币权限且未证明 IP 白名单，必须先改为读取+交易、无提币权限并完成私有只读预检。
- Longbridge 两账户当前 GLD.US 可交易数量为 0；在账户资金/资格变化前，真实预检会阻断。
- Longbridge paper 的撮合和现金规则与实盘不同，且美股只支持常规交易时段；模拟结果不能替代综合/日内账户的真实可交易验收。
- Longbridge Legacy Token 更新时必须来自同一绑定账户；类型或账户 ID 不一致会在创建交易会话前失败，不能通过修改档案名称绕过。
- Longbridge Legacy Token 到期仍需人工更新；账户总盈亏接口没有可靠的已实现/未实现拆分，PA 不伪造拆分。
- Longbridge 止损是券商端原生 MIT，止盈条件由 PA 软件轮询；关闭 PA 后原生保护仍在，但软件止盈和状态回写暂停。OKX 保护使用券商端 OCO。
- 最小真实 Canary 未获本轮授权；后续必须对具体券商、账户、品种、方向和数量重新单独确认。
