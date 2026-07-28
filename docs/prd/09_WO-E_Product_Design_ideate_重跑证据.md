# WO-E Product Design `ideate` 重跑证据

## 1. 状态

- 执行日期：2026-07-28。
- 调用入口：Product Design 插件的 `product-design:ideate`。
- 插件版本：`0.1.52`。
- Skill 文件 SHA-256：`649E8782CA93D5AECE712964FAE5BE2B111CC3143577E1507B98DF7F9D8E6BDE`。
- 执行结果：三个独立方向已真实生成，Product Design B1 证据门通过。
- 用户选择：用户随后按本轮实际显示顺序回复 `1`，唯一绑定为 `Scan Rail Workbench`；B2 通过。
- 本轮 `ideate` 阶段只生成和登记设计方向，没有调用 Stitch、没有开始连续三轮精修、没有写 PyQt6、没有触碰交易运行态。

重跑原因：旧三张候选稿能证明三次 ImageGen 和用户偏好，但仓库不能独立证明它们来自 Product Design `ideate` 的真实调用。为满足 `frontend-design` 验收项 B1，本轮重新执行正式入口，并保留输入、完整提示词、产物标识、项目文件、哈希和人工视觉复核。

## 2. 输入与安全

### 2.1 实际附加

| 文件 | 用途 | 来源 | 是否合成 | 安全复核 | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `docs/prd/assets/wo_e_product_design/option-1-dense-scan-workbench.png` | 只提供已确认的深色工作站语言、密度和三栏扫描节奏 | 旧候选稿 | 是 | 无凭据、账户、订单、持仓、日志、绝对路径或内部 URL；行情为合成示例 | `C2E4B45C62860C308B9F7EDD17825F0F72C8A96E98A742181486FCEF0AAA4C13` |

没有附加真实运行截图、账户截图、数据库、日志、配置、凭据或环境文件。提示词只使用公开产品名、公开标的代码和合成行情。

### 2.2 共同硬约束

三个调用均要求：

1. 完整 PyQt6 `MainWindow`，目标 `1440×900`，包含菜单栏、一级页签、页面和状态栏。
2. 页面标题只能是“多市场看盘”，主动作只能是“开始分析”。
3. 四市场只读路由：US/HK/CN 使用 Longbridge，Crypto 使用 OKX。
4. 状态使用图标与文字，不只靠颜色。
5. 禁止买卖、订单、持仓、账户、资产、盈亏、杠杆、风险预算、自动交易、执行和券商写控件。
6. 禁止副标题、眉题、标签语、标题解释、灰色辅助小字、括号注释、营销文案和小号页脚。
7. 禁止 OHLC 条、今日/昨收、成交量、RSI、均线、置信度和未冻结周期。
8. 所有可见行情和时间均为锚定 2026-07-28 的合成示例。

## 3. 三次独立调用

### 3.1 实际显示顺序 1

预命名方向：`Scan Rail Workbench`。

产物标识：`call_TRaeEW9gjx8NGlz3rYlacsyQ`。

完整提示词：

```text
Create a realistic, production-quality native desktop UI concept named “Scan Rail Workbench” for PA_Agent. This is one independent Product Design ideation direction, not a refinement round and not a web page. Use the attached screenshot only as structural and visual grounding; do not copy its erroneous labels, fields, icons, or exact composition.

Target: a complete PyQt6 MainWindow at exactly 1440×900, app content only, no device bezel, no browser chrome, no marketing presentation. Include a compact native application shell: 28px menu bar, 32px primary tabs with “分析工作台” selected and “实盘交易” inactive, 24px bottom status bar showing only neutral page status. Inside the selected tab, show the unique page title “多市场看盘” and no subtitle.

Concept: preserve the dense vertical scanner rhythm. Use a 240px left watchlist rail, an 860px central evidence-and-analysis workspace, and a 340px right evidence rail. The page header is 48px. Dark professional workstation style using #090B10 background, #10131A main surface, #151922 secondary surface, #252B36 separators, #2F8DFF primary accent. Use spacing, alignment, typography and thin dividers before borders; no nested card grid, no glass, neon, gradients, oversized rounded cards, shadows, or decorative animation. Body and all necessary status text must be visually at least 14px. Use monospaced numerals.

Core flow: switch 美股/港股/A股/加密 → scan current market local watchlist → select symbol and 10m timeframe → verify market clock, quote freshness and latest closed K-line → press the single primary action “开始分析”. Supporting actions may only be “刷新行情”, add/remove/reorder local watchlist, “重新载入”, “再次保存”, and cancel analysis when running.

Left rail: a dense table-like local watchlist with symbol, name, last, signed change percentage, update time, and a status conveyed by icon plus explicit Chinese text. Show about 9 synthetic public symbols. One row is selected and fresh; one different row is “行情已过期”. No count like 8/50, no cloud watchlist wording, no pencil/edit action.

Center: selected symbol identity, source, 10m selector with 1h/4h background evidence, a large clean candlestick chart, latest closed K-line time, and a compact two-stage analysis area. Do not show OHLC strip, 今日/昨收, volume chart, RSI, moving averages, indicators, confidence score, or unfrozen timeframes. Show analysis as “未开始” with the one blue “开始分析” button.

Right rail: four clearly separated fact groups titled “市场状态”, “行情状态”, “K线证据”, “分析状态”. Each group uses an icon plus explicit text, never color alone. Show a plausible stock-market example where quote and K-line are fresh but the calendar is “状态未知”; keep the analysis button enabled because the latest closed K-line is complete and fresh. Do not mix these states.

Hard exclusions: no buy, sell, order, cancel order, positions, account, assets, P&L, leverage, risk budget, auto-trading, execution, broker-write controls, order book, trade ticket, marketing copy, subtitles, eyebrow labels, taglines, title explanations, gray helper microcopy, parenthetical annotations, small footer notes, decorative badges, emoji, company branding, or real private data. All prices, names and timestamps are synthetic examples anchored to 2026-07-28. Chinese UI text must be legible and correctly aligned. No clipped text, no overlapping panels, no horizontal scrollbar.
```

### 3.2 实际显示顺序 2

预命名方向：`Market Matrix Workbench`。

产物标识：`call_vK3dEUnUsSY9iUKMZK2V2JFS`。

完整提示词：

```text
Create a realistic, production-quality native desktop UI concept named “Market Matrix Workbench” for PA_Agent. This is a second independent Product Design ideation direction, not a refinement round and not a web page. Use the attached screenshot only for its disciplined dark workstation language; do not copy its exact layout, erroneous fields, icons, or text.

Target: a complete PyQt6 MainWindow at exactly 1440×900, app content only, no device bezel, browser chrome, or marketing frame. Include a compact 28px native menu bar, a 32px primary tab bar with “分析工作台” selected and “实盘交易” inactive, and a 24px bottom status bar showing only neutral page status. Inside the selected tab, show the unique page title “多市场看盘” with no subtitle.

Distinct concept: prioritize fast cross-market orientation and scanning without a full-height left rail. Under the 48px page header, place a compact four-market switch at left, then a single dense horizontal watchlist matrix across the top of the page, approximately 118px high, showing two rows of compact sortable instruments from the active market. Below it, use a broad 1020px central chart-and-analysis workspace and a fixed 420px evidence rail. The chart must be significantly wider than in the reference. Use a selected instrument cell with explicit “行情正常” text and icon, and another cell with “行情已过期” text and icon. Make this direction meaningfully different from a three-column scanner while keeping the same user task.

Dark professional workstation style: #090B10 background, #10131A main surface, #151922 secondary surface, #252B36 thin dividers, #2F8DFF primary action. Let spacing, alignment, type and lightweight row separators create structure; avoid a card mosaic, nested cards, glass, neon, gradients, shadows, huge rounded corners, or decorative animation. All necessary text must appear visually at least 14px; use monospaced numerals.

Core flow: switch 美股/港股/A股/加密 → scan the active market’s local watchlist matrix → select symbol and 10m timeframe → verify market clock, quote freshness and latest closed K-line → use the one primary action “开始分析”. Supporting actions may only be “刷新行情”, add/remove/reorder local watchlist, “重新载入”, “再次保存”, and cancel analysis when running.

Main workspace: selected public symbol with synthetic name/source, 10m selected with 1h/4h background evidence, large clean candlestick chart, latest closed K-line time, and a compact two-stage analysis region. Do not show OHLC strip, 今日/昨收, volume, RSI, moving averages, indicators, confidence, or unfrozen timeframes. Show “未开始” and one blue “开始分析” button.

Evidence rail: four vertically stacked fact sections titled “市场状态”, “行情状态”, “K线证据”, “分析状态”. Use an icon plus explicit Chinese text for every status. Show a plausible Crypto example sourced only from OKX: market state “连续交易”, timezone UTC, fresh quote, latest 10m closed K-line, analysis not started. Do not display stock session concepts for Crypto.

Hard exclusions: no buy, sell, orders, cancel order, positions, account, assets, P&L, leverage, risk budget, auto-trading, execution, broker-write controls, order book, trade ticket, cloud watchlist, marketing copy, subtitles, eyebrow text, taglines, title explanations, gray helper microcopy, parenthetical annotations, tiny footnotes, decorative badges, emoji, branded logos, or private data. All prices and timestamps are synthetic public examples anchored to 2026-07-28. Chinese text must be crisp and aligned. No clipped content, overlap, or horizontal scrollbar.
```

### 3.3 实际显示顺序 3

预命名方向：`Evidence Gate Workbench`。

产物标识：`call_aNFJ44rd212f04DhG3gnx5Hj`。

完整提示词：

```text
Create a realistic, production-quality native desktop UI concept named “Evidence Gate Workbench” for PA_Agent. This is a third independent Product Design ideation direction, not a refinement round and not a web page. Use the attached screenshot only for dark workstation grounding; do not copy its exact layout, incorrect fields, controls, or wording.

Target: a complete PyQt6 MainWindow at exactly 1440×900, app content only, without device bezel, browser chrome, or marketing presentation. Include a compact 28px menu bar, a 32px primary tab bar with “分析工作台” selected and “实盘交易” inactive, and a 24px bottom status bar containing neutral page state only. The selected tab has the unique page title “多市场看盘” and no subtitle.

Distinct concept: make evidence gating the dominant reading path. Keep a compact 220px left local-watchlist scanner. Across the top of the remaining 1220px, place a single 88px horizontal evidence gate split into four aligned segments: 市场状态, 行情状态, K线证据, 分析状态. Below the gate, use a wide central chart and a 300px right detail ledger that expands the currently selected gate segment. Put the two-stage analysis region directly below the chart. This must feel meaningfully different from both a conventional three-column evidence rail and a horizontal watchlist matrix.

Dark professional workstation style using #090B10, #10131A, #151922, #252B36 and #2F8DFF. Prefer spacing, alignment, typography and thin dividers; no nested card grid, glass, neon, gradients, large rounded cards, shadows, or decorative animation. All necessary text must appear at least 14px. Use monospaced numerals. Keep density high but readable.

Core flow: switch 美股/港股/A股/加密 → scan current market’s local watchlist → select symbol and 10m timeframe → read the four-segment evidence gate from left to right → press the single primary action “开始分析” only when evidence permits. Supporting actions may only be “刷新行情”, add/remove/reorder local watchlist, “重新载入”, “再次保存”, and cancel analysis when running.

Left scanner: symbol, short name, last, signed change percentage, and icon plus explicit status text. Show one selected fresh row and one “行情已过期” row. No count like 8/50, no cloud watchlist, no pencil/edit action.

Evidence gate scenario: a Longbridge Hong Kong symbol with market state “午休”, quote state “行情已过期”, K-line evidence “10m 已收盘” and analysis state “不可开始”. The action must be visibly disabled with a direct reason in normal-size text, while the most recent same-identity authoritative quote keeps its original timestamp. Status cannot rely on color alone. The right ledger expands quote freshness facts without mixing calendar, quote, K-line and analysis states.

Chart and analysis: selected public symbol, Longbridge source, 10m selected with 1h/4h background evidence, large clean candlestick chart and latest closed K-line time. Do not show OHLC strip, 今日/昨收, volume, RSI, moving averages, indicators, confidence score, or unfrozen timeframes. Show both analysis stages as “未开始”.

Hard exclusions: no buy, sell, orders, cancel order, positions, account, assets, P&L, leverage, risk budget, auto-trading, execution, broker-write controls, order book, trade ticket, marketing copy, subtitle, eyebrow, tagline, title explanation, gray helper microcopy, parentheses, tiny footnotes, decorative badges, emoji, logos, or private data. All quotes and timestamps are synthetic public examples anchored to 2026-07-28. Chinese text must be crisp and aligned. No clipping, overlap, or horizontal scrollbar.
```

## 4. 产物与指纹

编号只按本轮三张图在对话中的实际显示顺序绑定：

`call_*` 按 `frontend-design` B1 的“产物标识”要求作为审计句柄保留；它们不是 URL，也未被 gitleaks 识别为凭据。可公开复核的实体仍以项目相对路径、原生尺寸和 SHA-256 为准。

| 显示顺序 | 方向 | 产物标识 | 项目文件 | 原生尺寸 | SHA-256 |
| ---: | --- | --- | --- | --- | --- |
| 1 | `Scan Rail Workbench` | `call_TRaeEW9gjx8NGlz3rYlacsyQ` | `docs/prd/assets/wo_e_product_design/ideate-20260728-1-scan-rail-workbench.png` | `1586×992` | `7D445535CD292C11DE963421EF99A46882600A997F4D4FDB0DA110FBC8B34805` |
| 2 | `Market Matrix Workbench` | `call_vK3dEUnUsSY9iUKMZK2V2JFS` | `docs/prd/assets/wo_e_product_design/ideate-20260728-2-market-matrix-workbench.png` | `1586×992` | `BA38ADB6A49B465D6099BB7177B17966D4C06BBDF8847EC10234E0F3FB79EDEB` |
| 3 | `Evidence Gate Workbench` | `call_aNFJ44rd212f04DhG3gnx5Hj` | 原始输出未进入 PUBLIC Git；公开安全副本见下文 | `1586×992` | `8A693FFF65B10025509DB46AEBEFE638BB0666DEA30502082C1241A8E2F7929B` |

三张图都是约 `1.599:1`，接近 `1440×900` 的 `1.6:1`，只适合比较方向。它们不是精确画布验收；用户选择后，R1 仍必须输出并核验精确 `1440×900`。

第三张原始输出的左侧列表含第三方品牌图标，不符合本项目 PUBLIC 仓库的品牌安全边界，因此没有进入 Git。用户完成选择后，另以原图为唯一输入执行一次只用于公开归档的脱敏编辑，删除品牌图标、把 `0700.HK` 修正为 `700.HK`、把英文窗口标题替换为“多市场看盘”，并加入“合成示例”。这次编辑不构成第四个方向、不改变前三张的实际显示顺序，也不重开用户选择门：

| 用途 | 产物标识 | 项目文件 | 原生尺寸 | SHA-256 |
| --- | --- | --- | --- | --- |
| 第三方向 PUBLIC 安全副本 | `call_rLMB0nAdKPnrPwelCby7hnpT` | `docs/prd/assets/wo_e_product_design/ideate-20260728-3-evidence-gate-workbench-public-safe.png` | `1586×992` | `ABF5C82A8A0ACAE93C563CA1ABCF32BE9EB15FEECFEC00E572C5C450C6330423` |

## 5. 人工视觉复核

| 显示顺序 | 主轴 | 足以选择的证据 | 选中后必须修正 |
| ---: | --- | --- | --- |
| 1 | 左侧纵向扫描轨 + 中央图表 + 右侧四组证据 | 页面标题、当前标的、正常/过期行、市场/行情/K 线/分析四组事实均可辨认；没有交易控件 | 删除英文窗口标题；按真实 `quote_mode` 显示实时或延迟，不能固定写“实时行情”；复核所有表头和时间文字是否达到 14px；输出精确 1440×900 |
| 2 | 顶部两行横向自选矩阵 + 超宽图表 + 右侧证据 | 与纵向扫描轨形成实质不同的信息层级；Crypto 连续交易、UTC、OKX 来源清楚；没有股票会话语义 | 删除英文窗口标题；把验收标的改为 `XAU-USDT-SWAP`；降低横向单屏标的数量，保证必要文字不小于 14px；输出精确 1440×900 |
| 3 | 左侧扫描 + 顶部四段证据门 + 右侧所选证据明细 | 午休、行情过期、K 线已收盘和不可分析四种状态分离；主动作阻断原因明显；没有交易控件 | 原始输出的品牌图标、英文标题和 `0700.HK` 已在 PUBLIC 安全副本中清除；仍需删除重复说明、复核顶部状态文字和右栏密度，并输出精确 1440×900 |

三张图均无明显裁切、重叠或横向滚动，但 PNG 不能证明真实 Qt 字号、键盘焦点、无障碍名称、对比度或交互。它们只通过 Product Design 三方向门，不通过 R1/R2/R3、Taste 审计或生产验收。

## 6. 选择门

用户已按本轮实际显示顺序回复 `1`。选择只绑定：

- 方向：`Scan Rail Workbench`；
- 产物标识：`call_TRaeEW9gjx8NGlz3rYlacsyQ`；
- 项目文件：`docs/prd/assets/wo_e_product_design/ideate-20260728-1-scan-rail-workbench.png`；
- 原生尺寸：`1586×992`；
- SHA-256：`7D445535CD292C11DE963421EF99A46882600A997F4D4FDB0DA110FBC8B34805`。

旧候选稿的回复 `1` 只保留为历史偏好；本节记录的是用户对本轮实际显示顺序的新选择。B1、B2 已通过，但这不代表 ChatGPT Web、Stitch、R1/R2/R3、Taste 审计、最终审美确认或生产实现完成。
