# 本地配置说明

本目录下的**运行时文件**默认已被 `.gitignore` 忽略，不会进入 Git 仓库。

仓库同样**不会上传**：`records/`（分析落盘）、`experience/`（经验库内容）、`logs/`、`trade_records/`（交易 CSV/截图）、`.env`、根目录临时图片与个人笔记等。仅源代码、`prompt_engineering/` 策略文本、`tests/` 与 `docs/` 说明文档会进入 GitHub。

## 首次使用

1. 复制模板为本地配置：

   ```cmd
   copy config\settings.example.json config\settings.json
   ```

2. 启动程序，在 **AI 模型设置** 中填写连接信息。API Key 输入框默认隐藏；点击“测试并保存”后，配置才会成为可切换档案。

   当前上游存储格式会把 Key 明文写入被 Git 忽略的 `config/settings.json`，并未实现凭据加密。不要共享该文件；若需要更高安全级别，应改用系统密钥库后再保存。

3. `config/exception_state.json` 由程序在需要时自动创建，一般无需手动复制。结构可参考 `exception_state.example.json`。

4. 如需自定义 TradingView 品种别名，复制模板：

   ```cmd
   copy config\tv_symbol_aliases.example.json config\tv_symbol_aliases.json
   ```

5. 如需使用 Longbridge，在 PA_Agent 的父目录（即 `Quant\env`）配置完整三项凭据：

   ```text
   LONGBRIDGE_APP_KEY=...
   LONGBRIDGE_APP_SECRET=...
   LONGBRIDGE_ACCESS_TOKEN=...
   ```

   程序优先读取进程环境，其次读取该共享文件；也兼容旧名 `LONGPORT_*`。凭据不会复制到 `settings.json`。Longbridge **行情数据源**仍只创建官方 `QuoteContext`；只有单独启用下述交易执行模块时，才会按所选账户创建交易上下文。主界面会显示 Token 的本地到期判断：7 天内到期会预警，已到期会在连接前阻断；无法解析 `exp` 时显示“到期日未知”，服务端撤销仍以真实连接结果为准。

6. 如需使用交易执行，在同一 `Quant\env` 中配置（不要写入仓库）：

   ```text
   # Longbridge 综合账户
   LONGBRIDGE_COMPREHENSIVE_APP_KEY=...
   LONGBRIDGE_COMPREHENSIVE_APP_SECRET=...
   LONGBRIDGE_COMPREHENSIVE_ACCESS_TOKEN=...

   # Longbridge 日内融资子账户
   LONGBRIDGE_INTRADAY_APP_KEY=...
   LONGBRIDGE_INTRADAY_APP_SECRET=...
   LONGBRIDGE_INTRADAY_ACCESS_TOKEN=...

   # OKX
   OKX_API_KEY=...
   OKX_SECRET_KEY=...
   OKX_PASSPHRASE=...

   # 所有真实写操作的总开关
   PA_AGENT_LIVE_TRADING_ENABLED=false

   # OKX Live 的独立第二道开关
   OKX_LIVE_ENABLED=false
   ```

   两个开关默认都应保持 `false`。即使改为 `true`，每次重启 PA 后仍需在 **实盘交易** 窗口输入 `启用实盘交易`；该启用状态只存在内存中。OKX 模拟交易由 `execution.okx.simulated` 控制，不需要开启 `OKX_LIVE_ENABLED`。OKX Key 应仅保留读取与交易权限，移除提币权限，并建议配置 IP 白名单。

## `settings.json` 字段说明

AI 档案由 `active_ai_profile_id` 与 `ai_profiles` 管理；`provider` 是当前活动档案的兼容镜像。其余主要组为 `general`、`prompt`、`validation`、`execution`。

### AI 模型档案

| 字段 | 类型 | 说明 |
|------|------|------|
| `active_ai_profile_id` | string | 当前运行档案 ID |
| `ai_profiles.<id>.display_name` | string | 界面显示名称 |
| `ai_profiles.<id>.provider` | object | 该档案独立的模型、地址、Key 与推理参数 |
| `ai_profiles.<id>.verification.status` | string | `untested` / `passed` / `failed`；只有与当前配置指纹一致的 `passed` 档案才能切换 |
| `ai_profiles.<id>.verification.checks` | object | 真实探测的连接认证、参数接受、有效正文与随机挑战值匹配结果 |
| `ai_profiles.<id>.verification.observations` | object | 是否观察到 reasoning 等非门禁信息；未返回可见 reasoning 不等于模型不可用 |

### provider — 当前活动档案镜像

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider.model` | string | `"deepseek-v4-flash"` | 模型名称（须与网关支持的名称一致） |
| `provider.base_url` | string | `"https://api.deepseek.com"` | OpenAI 兼容 API 根地址。DeepSeek：`https://api.deepseek.com`；MiMo：`https://api.xiaomimimo.com/v1`（程序自动处理 `enable_thinking` 与 `reasoning_content` 回放） |
| `provider.api_key` | string | `""` | 当前活动档案的 API Key；会明文持久化到本地 `settings.json`，该文件默认被 Git 忽略 |
| `provider.api_key_encrypted` | string | `""` | 兼容保留字段；当前实现不使用它提供加密存储 |
| `provider.adapter_id` | string | `"auto"` | 请求适配器：`deepseek`、`openai`、`anthropic_adaptive`、`anthropic_budget`、`minimax_m3`、`minimax_m2`、`mimo`、`cursor_agent`、`generic_openai_compatible`、`generic_reasoning_compatible`；`auto` 仅用于旧配置迁移/识别 |
| `provider.thinking` | bool | `true` | Thinking 开关；界面会按适配器决定是否可切换，固定 Thinking 模型不能关闭 |
| `provider.reasoning_effort` | string | `"high"` | 推理深度：`minimal` / `low` / `medium` / `high` / `xhigh` / `max`；界面只开放当前模型适配器声明支持的档位 |
| `provider.context_window` | int | `2000000` | 该档案独立的上下文窗口（1,024–100,000,000 tokens），用于用量提示和预警；在 AI 模型窗口按供应商官方模型文档填写，未知模型不会自动猜测 |

### general — 通用设置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `general.last_data_source` | string | `"mt5"` | K 线数据来源：`mt5` / `tradingview` / `longbridge`（GUI 下拉选项）；`akshare` / `yfinance`（仅代码支持） |
| `general.last_tradingview_exchange` | string | `""` | TradingView 交易所。空字符串 =（自动）依次探测预设列表。如 `OANDA`、`SSE`、`HKEX` 等 |
| `general.last_symbol` | string | `"XAUUSDm"` | 当前数据源的默认品种。MT5 需匹配终端名称；TradingView 如 `XAUUSD`；Longbridge 必须为 `ticker.region`，支持 `US` / `HK` / `SH` / `SZ` 后缀，如 `AAPL.US` |
| `general.last_symbols_by_source` | object | `{}` | 各数据源最后使用的品种，切换数据源时自动维护，避免把 MT5/TradingView 代码误带入 Longbridge |
| `general.last_timeframe` | string | `"15m"` | 默认周期，如 `1m`、`5m`、`15m`、`1h`、`4h`、`1d` |
| `general.analysis_bar_count` | int | `100` | 提交分析时使用的 K 线数量（通常为 2–5000；Longbridge 因单次行情上限最多 945） |
| `general.refresh_interval_ms` | int | `1000` | 图表自动刷新间隔（毫秒） |
| `general.context_warning_threshold_pct` | float | `80.0` | 上下文占用警告阈值（百分比） |
| `general.decision_stance` | string | `"balanced"` | 阶段二交易倾向：`conservative` / `balanced` / `aggressive` / `extreme_aggressive` |
| `general.incremental_max_new_bars` | int | `10` | 增量分析触发阈值：新增已收盘 K 线 ≤ 此值时自动走增量模式（0–500） |
| `general.auto_resume_chart_after_analysis` | bool | `false` | 分析结束后是否自动恢复「图表实时更新」 |
| `general.keep_analysis` | bool | `false` | 持续跟踪分析：新 K 线收盘时自动触发新一轮分析 |
| `general.cancel_keep_analysis_on_retry` | bool | `false` | 校验失败触发重试后自动关闭 `keep_analysis` |
| `general.alert_on_order_opportunity` | bool | `true` | 阶段二给出交易方案时播放警报音、弹窗提示，并自动切换到「决策」页 |
| `general.decision_flow_auto_play` | bool | `true` | 决策树可视化自动播放 |
| `general.decision_flow_play_seconds` | int | `50` | 决策树可视化自动播放时长（秒） |
| `general.decision_flow_default_zoom_pct` | int | `600` | 决策树可视化默认缩放百分比（≥10） |
| `general.stream_pane_font_pt` | int | `11` | 「实时」页等宽字体字号（pt，8–28） |
| `general.chart_seq_label_font_pt` | int | `11` | K 线图上序号标签的字号（pt，6–24） |

### prompt — Prompt 组装调优

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `prompt.stage2_load_full_strategy_library` | bool | `false` | 阶段二是否加载全部 22 个策略文件（通常仅路由匹配的策略文件） |
| `prompt.experience_max_entries` | int | `3` | 经验库最大加载条目数（0–10） |
| `prompt.experience_max_chars_per_entry` | int | `400` | 每条经验最大字符数（100–4000） |
| `prompt.stage1_inject_pattern_briefs` | bool | `true` | 阶段一是否注入模式判定表和速查 brief（减少 missed tags） |

### validation — 校验与重试

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `validation.normalization_mode` | string | `"lenient"` | 归一化模式：`strict`（严格拒绝异常值）/ `lenient`（容忍轻微偏差） |
| `validation.stage1_coherence_checks` | bool | `false` | 阶段一跨字段一致性检查（闸门 trace、逐 K 摘要、模式标签等） |
| `validation.stage2_coherence_checks` | bool | `false` | 阶段二诊断与 trace 交叉检查 |
| `validation.trace_semantic_checks` | bool | `false` | 语义一致性检查（方向/信号逻辑冲突检测） |
| `validation.strict_bar_by_bar_features` | bool | `false` | 严格逐 K 特征校验（开启后对特征字段做严格验证） |
| `validation.disable_truncation_repair` | bool | `false` | 禁用流式 JSON 截断尾部修复 |
| `validation.retry_enabled` | bool | `true` | 校验失败时是否自动重试 |
| `validation.retry_max` | int | `3` | 格式错误（category a）最大重试次数（0–5） |
| `validation.retry_max_semantic` | int | `1` | 语义错误（category c）最大重试次数（0–3） |
| `validation.retry_stage2` | bool | `true` | 阶段二校验失败时是否重试 |

### execution — 交易执行（默认关闭）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `execution.enabled` | bool | `false` | 是否允许完整分析记录生成执行计划；关闭时不能启用会话或提交既有计划 |
| `execution.auto_execute` | bool | `false` | 会话已启用时自动提交新计划；建议先保持关闭并人工确认 |
| `execution.selected_broker` | string | `"longbridge"` | 当前写操作只允许 `longbridge` 或 `okx` 之一 |
| `execution.min_trade_confidence` | int | `70` | 独立于提示弹窗的执行置信度门槛 |
| `execution.poll_interval_seconds` | float | `2.0` | 活动订单/保护/盈亏轮询间隔 |
| `execution.entry_timeout_seconds` | int | `120` | 未成交入场超时后先落盘撤单意图，再撤销未成交数量 |
| `execution.longbridge.source_symbol` | string | `""` | 必须与本次 PA 分析品种完全一致 |
| `execution.longbridge.instrument` | string | `""` | Longbridge 精确证券代码，如 `GLD.US` |
| `execution.longbridge.quantity` | string | `""` | 下单数量；按券商实时 lot size 校验 |
| `execution.longbridge.preferred_account` | string | `"comprehensive"` | `comprehensive` 或 `intraday` |
| `execution.longbridge.allow_comprehensive_fallback` | bool | `true` | 仅日内账户在提交前明确数量不足时回退综合账户；认证、网络、超时和未知状态绝不回退 |
| `execution.longbridge.allow_outside_rth` | bool | `false` | 美股是否允许盘前/盘后 |
| `execution.okx.source_symbol` | string | `""` | 必须与本次 PA 分析品种完全一致 |
| `execution.okx.instrument` | string | `""` | OKX 精确 `instId`，不限制黄金，如 `XAUT-USDT`、`BTC-USDT-SWAP` |
| `execution.okx.quantity` | string | `""` | 现货为基础币数量，永续为合约张数 |
| `execution.okx.product` | string | `"spot"` | `spot` 或 `swap`；现货不新开空仓 |
| `execution.okx.margin_mode` | string | `"cross"` | 永续 `cross` 或 `isolated`；程序只读取当前杠杆，不自动修改 |
| `execution.okx.simulated` | bool | `false` | 是否发送 OKX 模拟交易标头 |
| `execution.okx.api_base_url` | string | `"https://www.okx.com"` | 必须使用 HTTPS |

保存路由后，旧 READY 计划的配置指纹会失效，不能按旧券商/品种/数量继续提交。已经开始执行的记录只使用计划中持久化的账户、环境、API 地址、保证金模式、盘外交易开关和超时，不会被当前设置改道。券商、账户、品种、数量、保证金模式和开关全部来自本地配置；大模型输出中的同名字段会被忽略。账户接口没有可靠提供的总盈亏或已实现/未实现拆分会保持空值，不跨币种推算。

## 安全提醒

- **不要**将 `config/settings.json`、`config/exception_state.json`、`config/tv_symbol_aliases.json` 提交到 Git。
- API Key 在输入框中默认以圆点隐藏，但这只是界面遮罩，不代表磁盘加密。
- 若曾误提交 API Key，请立即在服务商处**作废并轮换**密钥。
- 建议在仓库根目录执行：`powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1`

连接测试有界面级硬截止，并把同一时刻的探测限制为一个。若第三方 SDK 无视自身超时且永久阻塞，界面会显示“前一次底层探测仍在终止”；持续出现时需重启 PA_Agent。此限制避免连续点击累积阻塞线程，但 Python 线程本身无法被安全强杀。

## Longbridge Token 更新边界

- Legacy Access Token 通常有到期时间；程序只在本地解析 JWT `exp` 并在连接时让服务端校验权限，没有“查询当前 Token 是否被撤销”的独立接口。
- Longbridge SDK 提供刷新 Access Token 的能力，但刷新后仍需安全持久化新 Token 并重建连接；PA_Agent 当前没有自动执行该流程。
- 当前使用方式仍需在 Longbridge 开放平台生成/刷新 Token 后，手动替换 `Quant\env` 中对应的行情或账户 Profile Access Token，并重新连接/重启相关上下文。
- Longbridge OAuth 2 可管理刷新与缓存，但需要单独的 OAuth 应用和首次浏览器授权，未在本次只读 Legacy 接入中启用。
- 常规中、美、港交易时段和午休分段会用于 K 线闭合判断；特殊半日市没有可靠的提前收盘时刻字段，可能延迟到常规收盘时间才标记闭合。
