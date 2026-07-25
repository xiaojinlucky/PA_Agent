# PA Agent 实盘交易工作台 Stitch 视觉重构 PRD

## 1. 文档状态

- 日期：2026-07-25
- 输入：用户提供的现状截图、长桥界面参考、GitHub 源码、现有工程 PRD、网页版 GPT 视觉审核
- 目标实现：现有 PyQt6 桌面程序，不新增第二套前端
- 当前范围：只打通 `OKX Demo / XAU-USDT-SWAP / 10m`

## 2. 一句话目标

让用户在 5 秒内看清三件事：

1. 自动交易系统现在是否正常工作；
2. 为什么这一轮下单或不下单；
3. 当前仓位是否已经被止盈和止损完整保护。

## 3. 本地适配决定

网页版 GPT 的方向可以采用，但以下内容必须按真实代码修正：

1. **不新增左侧导航。** PA Agent 已有“分析工作台 / 实盘交易”一级页签，再加左侧导航会重复占宽。实盘页直接使用现有一级页签。
2. **保存配置不会热切换。** 保存后的固定文案为：“保存成功；当前运行任务不变，下一次启动自动交易后生效。”
3. **止盈档数不写死。** 界面按每笔真实 ExecutionPlan 动态显示 N 档止盈和止损，不假设固定三档。
4. **账户重新检查必须受风险语义约束。** 只有临时账户读取故障才能显示“重新检查账户状态”；回撤、身份变化和账本完整性停止不能通过这个按钮解除。
5. **盈亏口径不得混写。** 只有来源和费用口径明确时才显示“净盈亏”；否则分别标出“价格差口径”和“券商已实现盈亏”。
6. **总权益与 USDT 权益分工不变。** 账户总权益只用于资金流、高水位和 50% 回撤；USDT 权益只用于定仓。
7. **不新增买入、卖出、加仓按钮。** 常规交易由脚本执行；主界面只允许显示当前状态，以及在真实可达状态下显示撤销未成交入场或主动离场。
8. **不在主界面暴露手动会话开关。** 原“桌面手动会话”移入默认收起的技术详情，自动 Campaign 不依赖它。

## 4. 信息架构

### 4.1 顶部：运行状态带

固定显示：

- `OKX 模拟盘`
- 自动交易：运行 / 暂停 / 状态未知
- 交易服务：运行 / 停止 / 状态未知
- 风险闸门：允许新增风险 / 已阻断 / 状态未知
- 账户核对：最新 / 过期 / 失败
- 最近更新时间

未知状态必须写“状态未知”或“等待读取”，不能显示绿色。

### 4.2 中央主区：决策与交易生命周期

上半区显示最新 PA 决策：

- 方向：做多 / 做空 / 不下单 / 等待
- 置信度（有真实证据时才显示）
- 用户能看懂的原因
- 合约张数
- 入场、止损、止盈
- 授权风险
- 执行状态

下半区显示最近 OKX Demo 执行：

- 时间
- 方向
- 张数
- 入场 / 成交
- 保护 / 离场
- 状态
- 结果

选中一笔执行后，底部事件时间线只显示用户事件，例如“已提交入场”“已成交”“保护已建立”“第一档止盈已完成”“已离场”，不显示数据库事件名。

### 4.3 右栏：账户、当前配置和下次启动配置

账户卡片显示：

- 账户总权益（回撤口径）
- USDT 权益（定仓口径）
- 可用 USDT
- 未实现盈亏
- 当前仓位
- 当前保护是否完整

配置卡片明确分成两层：

- **当前运行参数：** 当前 Campaign 冻结的真实参数，只读；
- **下次启动参数：** 用户可以编辑并保存的参数。

只提供两种定仓模式：

1. **按风险预算自动算张数**
   - 可编辑：资金上限、单笔风险比例、最大杠杆；
   - 只读：预计张数、预计最坏损失、预计最低保证金；
   - 数量由资金、风险距离和合约规格唯一计算，不允许用户同时输入数量。
2. **用户固定合约张数**
   - 可编辑：固定张数、资金上限、最大杠杆；
   - 只读：反算最坏损失、风险比例、名义价值、预计最低保证金；
   - 超过资金或杠杆上限时整笔阻断，不自动减张。

任何编辑立即显示黄色未保存提示。保存和取消并列；保存成功后仍明确显示当前运行任务没有变化。

### 4.4 技术详情

默认收起，只放：

- 仓库路径和代码加载时间；
- Campaign / Worker 原始状态；
- execution ID、客户单号、券商单号；
- 原始失败码、风险停止码；
- 手动模拟写会话状态和开关。

技术详情不能抢占正常用户的主视线。

## 5. 四种核心界面状态

### A. 正常监控、当前空仓

- 顶部状态全部明确；
- 最新决策显示“不下单 / 等待”和人话原因；
- 账户卡片显示新鲜资金；
- 仓位显示“当前空仓，不需要保护”；
- 不显示任何买卖按钮。

### B. 编辑风险配置

- 页面内出现黄色横幅：“配置正在编辑；当前运行任务不变。”
- 当前运行参数保持只读；
- 下次启动参数进入编辑态；
- 显示保存、取消；
- 输入超过真实范围时就地显示红色原因，不静默修正。

### C. 持仓与分档止盈

- 清楚显示方向、剩余张数、入场价、现价和可确认的盈亏口径；
- 清楚显示“保护完整 / 保护不完整 / 状态未知”；
- 按真实计划动态展示每一档止盈及共同止损；
- 只在当前 execution 可安全离场时显示“主动离场”。

### D. 风险阻断或账户数据不确定

- 顶部和主卡片使用红色阻断状态；
- 人话解释：“账户状态无法确认，系统不会新增风险。”
- 临时读取故障且满足专用恢复条件时，显示唯一主操作“重新检查账户状态”；
- 回撤、身份变化或账本完整性停止只显示原因和处理要求，不显示普通恢复按钮；
- 不提供“强制继续”。

## 6. 视觉令牌

参考网页版 GPT 方案和 UI/UX Pro Max 的公开设计系统原则，使用克制的专业交易终端风格：

- 页面背景：`#090B10`
- 面板背景：`#10131A`
- 抬高层：`#151922`
- 边框：`#252B36`
- 主文字：`#E7ECF4`
- 次文字：`#9AA3B2`
- 弱文字：`#646D7C`
- 正常：`#18B26B`
- 警告：`#E6A23C`
- 阻断：`#F05252`
- 主操作：`#2F8DFF`
- 中文字体：`Microsoft YaHei UI` / `PingFang SC`
- 数字字体：`JetBrains Mono` / `Cascadia Mono` / `Consolas`
- 页面标题：22 px
- 模块标题：15 px
- 正文：13 px
- 辅助文字：12 px，禁止更小
- 间距：4 / 8 / 12 / 16 / 24 px
- 表格行高：32 px
- 普通按钮最小高：36 px
- 主操作最小高：40 px
- 边框：1 px

禁止大面积渐变、发光、玻璃拟态、巨型阴影和仅靠颜色表达状态。

## 7. 尺寸与布局

### 1920 × 1080

- 页面外边距：16 px
- 顶部标题和操作：56 px
- 运行状态带：56–64 px
- 主体横向比例：中央 72%，右栏 28%
- 右栏目标宽度：430–470 px
- 事件时间线：160–190 px

### 1440 × 900

- 页面外边距：12 px
- 顶部标题和操作：48–52 px
- 主体横向比例：中央约 70%，右栏约 30%
- 右栏最小宽度：360 px
- 只允许右栏和事件列表内部滚动，不允许顶部运行状态被滚走

## 8. Stitch 生成提示词

Create a high-fidelity desktop trading operations workspace for “PA Agent”, implemented later in PyQt6. This is the existing top-level tab called “实盘交易”; do not add a left navigation rail. The interface is for OKX Demo only and must help a user answer within five seconds: whether automation is healthy, why the latest cycle traded or did not trade, and whether any position is fully protected.

Use a restrained professional dark trading-terminal design. Background #090B10, panels #10131A, raised panels #151922, borders #252B36, primary text #E7ECF4, secondary #9AA3B2, muted #646D7C, healthy #18B26B, warning #E6A23C, blocked #F05252, primary action #2F8DFF. Use Microsoft YaHei UI for Chinese text and JetBrains Mono or Consolas for numbers. Avoid gradients, glow, glassmorphism, large shadows, tiny fonts, and decorative charts.

Layout:
1. A fixed top header with “实盘交易”, a concise subtitle, and “刷新显示”.
2. A fixed health strip for OKX 模拟盘, 自动交易, 交易服务, 风险闸门, 账户核对, and 更新时间. Unknown data must visibly say 状态未知 and must never look green.
3. A 70/30 horizontal workspace. The central column contains a latest PA decision card, an OKX Demo execution lifecycle table, contextual safety actions, and a human-readable event timeline. The right column contains account/risk facts, current running parameters, editable next-start parameters, and collapsed technical details.
4. Never show buy, sell, or add-position buttons. Normal trade execution is scripted. Only show “撤销未成交入场” when an entry order is actually cancellable and “主动离场” when a real position is safely eligible for exit.
5. Current running parameters and next-start parameters must be visually separate. Editing shows a warning banner: “配置正在编辑；当前运行任务不变。” Saving says: “保存成功；当前运行任务不变，下一次启动自动交易后生效。”
6. Support two configuration modes. Risk-budget mode edits capital cap, worst-case risk percentage, and maximum leverage while quantity, loss, and margin are read-only. Fixed-quantity mode edits contract quantity, capital cap, and maximum leverage while worst-case loss, risk percentage, notional, and margin are read-only. Never silently resize an invalid quantity.
7. For an active position, dynamically render the actual number of take-profit stages and one or more real stop-loss protections from the execution plan. Clearly show protection complete, incomplete, or unknown.
8. For risk blocked or stale account data, show a red explanation that no new risk will be added. Only transient account-read failures may show one primary action “重新检查账户状态”. Never show “force continue”.
9. Put repository path, code load time, raw worker/campaign state, IDs, raw error codes, and manual simulation session controls inside collapsed “技术详情”.

Generate four coherent desktop states at both 1920×1080 and 1440×900: A) healthy flat monitoring, B) editing next-start risk configuration, C) active protected position with dynamic staged take-profit, D) risk blocked or account data unknown. All states must use the same component system and meet WCAG AA contrast. Text and state icons must communicate status without relying on color alone.

## 9. 二元验收

1. 实盘交易仍是现有主窗口一级页签，没有重复左侧导航。
2. 1440×900 下标题、状态带、决策、生命周期、账户和配置均可读。
3. 正常视图不显示买入、卖出、加仓和“执行待确认计划”。
4. 撤销和主动离场按钮只在对应真实状态下可见。
5. 主视图不显示“桌面手动会话”。
6. 当前运行参数和下次启动参数有不同标题和视觉层级。
7. 编辑任意参数后立即出现未保存横幅。
8. 取消编辑恢复已保存值。
9. 保存文案明确说明当前任务不变、下次启动才生效。
10. 风险预算模式不允许编辑合约张数。
11. 固定张数模式不允许编辑风险比例。
12. 固定张数超限时不静默减张。
13. 新鲜度未知时不显示旧资金和旧仓位。
14. 风险未知时不显示绿色。
15. 止盈档数来自真实计划，不写死为三档。
16. 技术 ID、原始状态码和手动会话只在技术详情中。
17. 所有正文不小于 12 px，普通按钮不低于 36 px。
18. 1440×900 和 1920×1080 截图均无重叠、截断或横向滚动。
19. GUI 配置保存、重载和 Campaign 当前冻结值测试通过。
20. 离线测试不得访问真实 OKX/Longbridge 凭据或在桌面弹出 Qt 测试窗口。

## 10. 2026-07-25 实施状态

- 已完成：网页版 GPT 审核、本地语义修正、Stitch 四状态生成、PyQt6 主工作台落地、配置编辑态、风险阻断态、保护状态、技术详情收纳、1440×900 / 1920×1080 真图复核。
- 已完成：双 Agent 对抗审查发现的旧快照闭锁、陈旧账本标识、盈亏口径、失败码收纳和固定张数超限提示均已修正；工作台定向 `22` 通过、`0` 失败，受影响范围 `516` 通过、`0` 失败；目标 Ruff、`compileall`、`git diff --check` 通过。
- 未完成：当前运行 GUI 尚未重新加载本次磁盘代码。
- 硬阻断：当前公网 IP `188.253.121.195` 不在 OKX API 白名单，Campaign 已停止，06:43:44 之后的真实仓位和保护单无法确认。必须先恢复私有只读权限，再完成仓位/挂单/算法单硬门；只有确认安全后才能决定是否重启 Worker 或 Campaign。
