# PA_Agent Context

> 静态代码和文档可直接引用。进程、账户、服务、仓位、订单和券商状态只认本轮实时证据。

## 在做什么

- 当前主线是 WO-H 后半段。任务 1 的股票交易时段标签和任务 2 的高周期关键位置、20GB 标记已由提交 `0a0c5a8` 推送，禁止重做。
- 任务 3 已完成可调用的成交量影子框架：`pa_agent/data/volume_shadow.py` 生成相对成交量摘要并追加 JSONL；`scratch/score_volume_shadow.py` 用本地 CSV 比较放量组与缩量组的后续相对振幅；成交量没有进入提示词。
- 任务 3 没有接入自动分析调用链。更具体的三文件白名单不允许修改调用点，因此当前只能称为可调用框架，不能称为正在自动采集。
- 任务 4 已完成文档交付：本文件压缩为一页，旧流水账完整归档到 `docs/archive/CONTEXT_full_history_through_20260727_wo_h.md`；多市场前端设计包见 `docs/prd/05_多市场看盘前端设计包.md`。
- 本轮不修改 `pa_agent/execution/`、`pa_agent/gui/`、`scripts/`，不操作交易运行态，不读写 AlphaMaster。

## 上次停在哪

- 开工基线在 `HEAD=0a0c5a8` 上复核为 1823 项、0 失败、0 错误、7 跳过。
- 任务 3 定向测试 51 项全过；最终 unit+property+integration 为 1874 项、0 失败、0 错误、3 跳过。
- 评分脚本已用本地 JSONL 和 CSV 验证两条路径：样本够时输出两组样本数与平均后续振幅；数据不足时明确输出“样本不足，暂不给结论”。
- `prompt_engineering/`、`pa_agent/ai/`、`pa_agent/execution/`、`pa_agent/gui/`、`scripts/` 在任务 3 阶段的 Git 差异均为空。
- 长桥 access token 仍被服务端以 `401004 token invalid` 拒绝。AAPL.US、700.HK、600519.SH 的真实两阶段验收继续阻塞；不得改凭据或绕过认证。
- Git 收口状态以当前仓库提交和远端 SHA 为准。`scratch/score_volume_shadow.py` 受 `.gitignore` 的 `scratch/` 规则保护，只作为本地交付。

## 近期关键决定

- 成交量当前只做影子摘要和描述性后验比较，不进模型输入、不生成交易信号、不进入风险闸门，也不能据均值差宣称统计显著或通过 Wilson 方向准确率门。
- 成交量摘要只使用已收盘 K 线。最新一根与此前最多 20 根形成基线；不足 6 根、参与计算的成交量无效或基准中位数为零时返回空结果，不猜值。
- 多市场前端本轮只交页面合同和三个文字方向，停在用户选择方向之前；不写 PyQt6，不宣称 WO-E 完成。
- 市场切换分为跨数据源事务与 Longbridge 内市场事务；US、HK、CN 路由到 Longbridge，Crypto 路由到 OKX。认证失败、标的或来源不一致、行情或已收盘 K 线陈旧时失败关闭；日历未知只关闭市场时钟事实，不猜阶段，但完整且新鲜的已收盘 K 线仍可分析；不静默换源。
- 自选列表只写本地 `GeneralSettings`，不写长桥云端 watchlist。
- 市场时钟读取 `market_calendar.session_state` 的开市、午休、闭市、半日市与下一变化时间；加密显示连续交易，不伪造股票会话。
- 交易安全真值保持不变：高水位 `78303.57015174496`，账户身份摘要 `ba9b744dc78ae3fc203980e62b854b0a0e3d44c9c6d5e446de910bea74ef1def`。
- 2026-07-27 北京时间约 13:00 的只读现场：Worker 健康、两库完整、风险停止为 0、券商侧空仓空挂单；Campaign 已于 10:30:55 因等待对账超时退出并漏过后续 K 线。本轮按用户边界不重启、不恢复、不补跑。
- 总控与历史证据继续以 `docs/WORKORDER_MASTER_20260727.md`、`docs/VALIDATION_EVIDENCE.md` 和本页链接的归档为准。
