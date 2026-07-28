# PA_Agent 工单进度

- 本轮 WO-E 文档开工基线为 `HEAD=origin/main=86bfb8e0c668479866038dd959e2b72cefc94811`；分支为 `main`，上游为 PUBLIC `origin/main`。更早的 WO-F 与完成证据复审已分别由 `c0b58d0`、`7e6c095` 发布。
- 完成证据复审补齐了 system prompt 字节稳定、固定代理端口抢占、长桥分页/日历边界，以及 Campaign 异常耐久收口与零重复写回归。
- NEW_RISK 最小权限修复已落在 `pa_agent/okx_demo_campaign.py`：动态杠杆、正常提交和 READY 恢复提交只在 Worker 命令返回耐久终态后释放租约；等待超时、读取异常或非终态结果不提前释放，进程收口仍会撤销租约，RUNNING 命令继续阻止再次授权。下一条新增风险命令重新执行私有只读预检并申请租约。定向回归 130 项通过、0 失败，unit/property/integration 为 1966 项通过、7 项跳过、0 失败。
- 对抗审查确认当前 Campaign 路径正确，但也确认公共 `ExecutionController` / `WorkerStore` 的租约不是全局一次性令牌；把“一租约一命令”提升为执行层硬不变量需要修改本轮禁止触碰的 `pa_agent/execution`，已作为范围外风险登记。
- 全仓 Ruff 只读基线已刷新为 293 项诊断，其中 249 项带自动修复建议；这是历史债账本，不授权全仓 `--fix`。
- WO-E 设计合同已补齐 `QuoteSnapshot`、可测的 10m K 线新鲜度、四市场设置迁移、generation、Longbridge 内切换、Longbridge↔OKX 跨源回滚、脱敏输入、M01–M17 和 D01–D07；首版分析主周期固定为 10m，1h/4h 只作背景证据。Product Design B1、用户选择 B2 与 ChatGPT Web B3 已通过。完整网页回答、内容指纹和 F01–F22 本地裁决已落盘；当前没有修改 PyQt6。
- WO-A 仍有一项未闭合：固定代理 metadata 与实际配置缺共同指纹。只改测试无法证明不存在不一致，本轮又禁止修改 `scripts`。
- WO-D 的最后已知外部结果仍是长桥 `401004 token invalid`；共享 `env` 自 2026-07-24 后未更新，没有可重跑真实三标的验收的新凭据证据。
- WO-E 下一设计门是恢复已登录 Chrome 的扩展连接并进入 Stitch；随后还需连续三轮精修、R2 Taste 过程指导、R3 后独立视觉审计和最终审美确认。当前 Chrome、扩展和 Native Host 均存在，但浏览器客户端仍无法通信；按控制规则，打开新 Chrome 窗口重试前需要用户许可。生产实现仍受 `pa_agent/gui` 禁区约束，最终三标的桌面验收还依赖有效长桥凭据和用户从快捷方式启动。
- 当前最新已知运行态证据来自 2026-07-28 11:24–11:28 的只读审计：Worker 与心跳当时运行，两库健康，活动 execution、pending/running 命令和有效 NEW_RISK 租约均为 0；但 OKX 私有余额读取持续连接拒绝并触发风险停止，Campaign 不存在且磁盘 `active` 状态过期。该快照不能外推为现在仍运行；未经新的 OKX Demo 私有只读硬门和用户授权不重载。
- WO-H、WO-C2、WO-F、WO-G-1/2/3 已完成；成交量仍不进入提示词或交易判断。
- `WO-POS-05` 只有路线图级目标且会触碰当前禁止修改的执行链，本轮不越界开工。
