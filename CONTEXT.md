# PA_Agent Context

## 当前状态（2026-07-17）

- 私有仓库：`Jinqingchang/PA_Agent`；当前 `main` / `origin/main` 基线为 `9e5c6ccd0b04136514bdd84b7ae55276b8d92a78`。本轮执行安全加固与审查修复仍是未提交工作区改动。
- 用户已终止全部前端重设计；本轮只增加交易所必需的功能配置/状态窗口，不做视觉重构。
- 已实现默认关闭的执行闭环：严格分析记录落盘、白名单计划构建、SQLite 强账本、会话/环境硬门、重启对账、Longbridge 模拟/综合/日内三档路由、OKX 现货/永续、入场/部分成交/保护/主动离场、账户资金与盈亏回写。
- Longbridge 行情数据源仍仅使用 `QuoteContext`；三个交易档案分别创建独立上下文。模拟账户绝不回退实盘且不允许盘前/盘后；日内账户只在提交前明确最大数量不足时回退综合账户，任何网络/认证/未知/已提交状态均不回退。
- OKX 不硬编码黄金；动态规格测试覆盖 `XAUT-USDT`、`XAU-USDT-SWAP`、`BTC-USDT`、`BTC-USDT-SWAP`。现货与永续有独立数量、保护和盈亏语义。
- 所有真实写操作要求 `PA_AGENT_LIVE_TRADING_ENABLED=true`、当前进程会话确认；OKX Live 还要求 `OKX_LIVE_ENABLED=true`。Longbridge 模拟写操作改用独立 `PA_AGENT_PAPER_TRADING_ENABLED` 和 `启用模拟交易`。当前实盘开关保持关闭，没有发送模拟或真实订单。
- `docs/GPT5_6SOL_HANDOFF.md` 与 `docs/LOCAL_EXECUTION_CONTEXT.md` 是开发前历史快照，已加醒目标记；当前实现真值以本文件与 `docs/LIVE_TRADING_DESIGN.md` 为准。

## 当前验证

- 主线程执行相关 9 文件回归：201 通过 / 0 失败；独立审查线程采用更窄的交易与交易控制范围复跑：159 通过 / 0 失败。覆盖 Longbridge 三档、OKX 现货/永续、入场/保护/离场、账户与盈亏、身份绑定、原子路由、UNKNOWN、重启恢复及券商写后本地保存失败。
- `tests/unit` 全量现状扫描：931 通过 / 28 失败。28 项与基线一致，均在旧决策连续性、预测面板、追问历史等非执行域；没有把仓库既有失败误报为全绿。
- 7 个并发/崩溃核心用例连续运行 10 轮：70 通过 / 0 失败。`compileall`、变更 Python 文件定向 Ruff 与 `git diff --check` 均通过。
- Longbridge 两套真实账户只读连接、余额、持仓、盈亏摘要、GLD.US 静态信息/最大数量和历史备注查询通过；两账户当前均无持仓，GLD.US 现金/融资最大数量均为 0。
- Longbridge paper Token 账户类型、余额、持仓、盈亏、GLD.US 报价与最大数量均通过真实只读验证，且容量非零；PA 当前默认选中 paper，但执行模块与自动执行仍关闭。
- OKX 公共服务器时间与四个现货/永续品种动态规格查询通过；私有只读仍因 `OKX_PASSPHRASE` 为空而阻断。
- 生产 `records/execution.sqlite3` 保持 schema v1、0 条 execution、0 条活动记录；仅对副本验证 v1 → v2 迁移，没有修改生产账本。本轮券商写调用 0 次。
- 原有 Qt E2E `tests/e2e/test_smoke_happy_path.py` 单独运行 90 秒仍无最终汇总，进程已停止；这是可选测试框架整治项，不能宣称 E2E 通过。
- 三个独立审查线程按需求完整性、逻辑正确性、边界情况、代码质量、测试覆盖、实际运行结果复验。首轮与第二轮共提出 8 个阻塞项，主线程全部修复并补测；最终三线程均为 PASS，阻塞落地的问题 0，审查线程未修改代码。
- Longbridge paper 三档扩展最终回归：执行域 131 通过 / 0 失败，相邻范围 44 通过 / 0 失败；独立审查首轮发现 Token 错放和未保存切换 2 个阻塞项，修复后复跑 83 通过 / 0 失败并给出 PASS。三档 Token 类型与账户 ID 绑定检查通过，paper 真实只读容量非零，券商写调用 0 次。

## 已知边界

- OKX 缺 Passphrase；现有 Key 曾显示含提币权限且未证明 IP 白名单，必须先改为读取+交易、无提币权限并完成私有只读预检。
- Longbridge 两账户当前 GLD.US 可交易数量为 0；在账户资金/资格变化前，真实预检会阻断。
- Longbridge paper 的撮合和现金规则与实盘不同，且美股只支持常规交易时段；模拟结果不能替代综合/日内账户的真实可交易验收。
- Longbridge Legacy Token 更新时必须来自同一绑定账户；类型或账户 ID 不一致会在创建交易会话前失败，不能通过修改档案名称绕过。
- Longbridge Legacy Token 到期仍需人工更新；账户总盈亏接口没有可靠的已实现/未实现拆分，PA 不伪造拆分。
- Longbridge 止损是券商端原生 MIT，止盈条件由 PA 软件轮询；关闭 PA 后原生保护仍在，但软件止盈和状态回写暂停。OKX 保护使用券商端 OCO。
- 最小真实 Canary 未获本轮授权；后续必须对具体券商、账户、品种、方向和数量重新单独确认。
