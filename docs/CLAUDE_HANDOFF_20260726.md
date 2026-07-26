# PA_Agent 交接给 Claude

> **已解决（2026-07-27 00:00 北京时间，Claude 会话）**：本文 §8 的固定代理空闲 EOF
> 阻断已按 §8 建议路线完成——换协议/供应商节点（好猫 US1 AnyTLS → 桔子云
> OKX 日本 1 VMess，出口已在白名单）+ 空闲耐久闸门固化进探针；随后完成
> ≥9 个自然扫描周期、专用风险恢复（高水位未重锚）、Campaign 恢复并完成
> 3 根自然 10m K 线。当前真值以 `CONTEXT.md` 顶部为准，本文其余内容为
> 交接时的历史快照。

更新时间：2026-07-26 16:00（北京时间）  
工作目录：`D:\Desktop\Quant\PA_Agent`  
当前主任务：Codex Goal `019f9a69-37ad-7ee1-bcd5-704e2db4e770`  
状态：**未完成，禁止按已交付收口**

## 1. Claude 接手后的第一步

先读取并遵守：

1. `D:\Desktop\Quant\PA_Agent\CLAUDE.md`
2. `D:\Desktop\Quant\PA_Agent\CONTEXT.md`
3. `D:\Desktop\Quant\PA_Agent\lessons.md`
4. `D:\Desktop\Quant\PA_Agent\docs\VALIDATION_EVIDENCE.md`
5. `D:\Desktop\Quant\PA_Agent\docs\prd\04_PA_Agent_Stitch视觉重构PRD.md`
6. 本交接文件

然后现场核对 Git、Worker、固定代理、两个 SQLite 数据库和 Campaign；运行态会变化，不能把本文件中的进程号当永久事实。

不要清理、重置、覆盖或格式化当前工作区。当前 21 个 tracked 文件和 2 个 untracked 测试文件都是本任务改动，尚未提交。

## 2. 用户原始要求

用户要求：

1. 阅读任务 `019f934a-6936-7391-8b98-97b008f1ce5e`，等待其中的规范化前端设计流程完成，然后继续 PA 系统主线。
2. 执行并落实：
   - `E:\谷歌下载\第一波下发_CODEX执行_W1-01与W1-02_重锚03c23f7.md`
   - `E:\谷歌下载\BIOMNI总控审核_整合工单_WO-DEMO-LOOP-01_重锚03c23f7.md`
3. 完成 PA_Agent 后端与前端联通，设计并落地前端 UI。
4. 在 Quant 项目中加入项目级前端设计 skills，固定以下流程：
   - 先冻结真实产品结构、字段、状态和交互。
   - Product Design 先做多种高保真方向，确认审美后再写生产代码。
   - Taste-Skill 负责字体、配色、留白、圆角、比例和视觉审计。
   - GSAP Skills 只用于适合 Web 的动效；PA_Agent 是 PyQt6 桌面程序，不能为了 GSAP 引入 WebView 或第二套前端。
   - 把脱敏材料、字段、状态和交互 PRD 交给网页版 GPT 和 Stitch；使用 GPT-Image-2/ImageGen 做视觉探索。
5. UI 硬规则：
   - 禁止副标题、eyebrow、tagline、标题下解释、灰色说明小字、括号注释、营销口号、重复说明和小号页脚。
   - 必需的字段标签、单位、时间、状态、错误和风险阻断原因必须保留，字号和对比度必须正常可读。
6. 进攻式推进，不用“谨慎”“兜底”“待验证”代替本可完成的行动；同时不能伪造数据或绕过交易安全。
7. 使用 Goal 持续推进，目标完成前不停止。
8. 完成后提交并推送，仓库始终保持公开。
9. 推送后只让网页版 GPT 审核，本次不再交 BIOMNI 总控。
10. Goal 最后必须启动多 Agent 按墨菲定律做对抗审查，修复后复验。
11. 用户已明确批准 PA_Agent 本任务所需权限，不要求重复询问；用户明确表示同意本次相关提交和推送。

## 3. 不可破坏的项目边界

- 唯一写链：

  `GUI/Campaign → ExecutionController → execution_control.sqlite3 → 单例 Worker → ExecutionService → OkxAdapter → OKX Demo`

- GUI 不得直连券商写 API，不得成为第二个写入者。
- 当前范围只含 `OKX Demo / XAU-USDT-SWAP / 严格聚合 10m`。
- Live 双硬门必须保持关闭。
- no_order 是正常结果，禁止强制交易、伪造成交或为了验收制造自然策略绩效。
- 代理失败必须关闭新增风险，不能回退到系统代理、主 v2rayN 或直连。
- `adjusted_high_water_usd=78303.57015174496` 是现有可信高水位。临时网络故障恢复不得重锚或改写它。
- 账户身份摘要必须保持：

  `ba9b744dc78ae3fc203980e62b854b0a0e3d44c9c6d5e446de910bea74ef1def`

- 当前历史 UNCERTAIN 命令已有正式处置，不能重放：
  - 命令：`686b6d0e-5c85-4430-a2d9-b9e069b76934`
  - 状态：`uncertain`
  - 处置：`confirmed_not_written_schema_validation`
- 不得提交数据库、日志、凭据、Token、Cookie、原始账户数据或 `scratch/`。
- 不得 `git add .`、`git add -A`、强推、改写历史、擅自开分支或 worktree。

## 4. 规范化前端流程任务的结论

目标任务 `019f934a-6936-7391-8b98-97b008f1ce5e` 已完成规范化前端流程。

已确认：

- Quant 共用的 `$frontend-design` 与 `$design-taste-frontend` 已同步到：
  - `.agents/skills/frontend-design`
  - `.agents/skills/design-taste-frontend`
  - `.claude/skills/frontend-design`
  - `.claude/skills/design-taste-frontend`
- 两处均为指向统一来源的 Junction，不是重复正文。
- GSAP 被正确限定为 Web 能力。PA_Agent 使用 PyQt6/QWidget/QSS/pyqtgraph，只允许 Qt 原生动画。
- 项目根 `CLAUDE.md` 已写入前端流程和“禁止副标题/说明性小字”规则。
- 当前提交 `4282f433` 的提交信息为“前端规范：禁止副标题和说明性小字”。

## 5. W1-01 / W1-02 已完成情况

详细证据：

`D:\Desktop\Quant\PA_Agent\scratch\W1-01_W1-02_执行回证_03c23f7.md`

### W1-01

- 7 个指定安全测试：7/7 存在。
- 定向安全套件：341 通过、0 失败。
- 当时全量单元测试：1561 通过、18 失败。
- 失败集合与历史 18 项完全相同，新增失败 0。
- `compileall` 和 `git diff --check` 通过。
- 全仓 Ruff 有 4963 项基线违规，因此按工单字面口径 W1-01 **不是全绿**。多数为不适合中文代码的 RUF 全角标点规则，也包含真实旧债；不能把定向 Ruff 通过冒充全仓 Ruff 通过。

### W1-02

- 工单“生产库仍是 v1”的前提已经过期；现场生产控制库是 v4。
- 使用真实历史 v1 备份派生 scratch 副本，完成锁内 v1→v4 演练。
- 历史 v1 源 SHA 前后不变。
- Controller + WorkerStore 未持锁构造后，派生主库 SHA 不变。
- 只在 scratch 锁内迁移。
- 迁移后 `integrity_check=ok`、`foreign_key_check=ok`。
- 27 条命令、18 条心跳、1 条 UNCERTAIN 原样保留，0 条租约，无认领、重排或重放。
- 真实 v1 没有 resolution/evidence 表，因此只能诚实表述为“迁移前后均为 0”，不能声称保留了 v1 原有 resolution。

## 6. 前端与前后端联通已完成的改动

### 6.1 产品和视觉设计

- 网页版 GPT/Product Design 会话：
  `https://chatgpt.com/c/6a65158f-6a80-83ee-83cc-b7ca71d38d10`
- Product Design 生成并比较三种方向：
  1. 门禁主导
  2. 执行链路
  3. 证据分层
- 最终采用“证据分层”，吸收门禁主导的六项判断和执行链路的因果时间线。
- 视觉原图与选择记录：
  - `scratch/visual/product-design-direction-1-gate-first.png`
  - `scratch/visual/product-design-direction-2-execution-chain.png`
  - `scratch/visual/product-design-direction-3-evidence-layers.png`
  - `scratch/visual/product-design-directions-20260726.md`
  - `scratch/visual/pa-trading-direction-a-gate-first-v1.png`
  - `scratch/visual/pa-trading-direction-a-gate-first-v2.png`
- 已完成人工查看。
- 严格口径下，已有三方向探索和两轮 ImageGen 修订；如果把用户要求解释为“选定方向后还需三轮连续精修”，则第三轮尚未形成单独证据，不能说已满足。

### 6.2 Stitch

- Stitch 项目：`2898475897385439253`
- 设计系统资产：`assets/15995812927483392573`
- 设计系统名称：`PA Trading Workbench — Longbridge Control`
- 设计系统创建成功。
- 屏幕生成请求发生 Stitch 服务端连接错误；按工具约束没有重复提交，项目中未出现本轮新屏幕。不能写成“Stitch 屏幕生成成功”。

### 6.3 PyQt6 实现

主要改动：

- `pa_agent/app_context.py`
  - 向主窗口注入同一套 ExecutionStore、WorkerStore、ExecutionController 和设置路径。
- `pa_agent/gui/main_window.py`
  - 主窗口与交易工作台使用同源依赖。
  - 保留“分析工作台 / 实盘交易”一级入口。
- `pa_agent/gui/read_models.py`
  - 增加专用 OKX Demo 读模型。
  - 账户、风险、Worker、Campaign、执行账本、来源、新鲜度和阻断原因都来自真实持久源。
  - 读取失败、陈旧或未知时不沿用历史绿色状态。
  - 固定校验 `okx / demo / okx / XAU-USDT-SWAP / 10m`，不跟随全局券商选择串线。
- `pa_agent/gui/trading_workbench.py`
  - 重构为证据分层的原生 Qt 工作台。
  - 展示 Worker、Campaign、风险、账户、最新 PA 决策、执行生命周期、保护和事件。
  - 区分当前生效参数与下次启动参数。
  - 支持风险预算模式与固定张数模式。
  - 撤单、主动离场只在真实可达状态出现。
  - 技术码收进折叠详情，主视图显示人话。
  - 已移除副标题、营销句和灰色解释小字；必需状态和风险原因保留正常字号。

### 6.4 真实前后端边界测试

新增：

`tests/integration/test_trading_workbench_worker_boundary.py`

该测试证明：

1. GUI 点击只调用 ExecutionController。
2. Controller 把命令耐久入队。
3. 单例 Worker 领取命令。
4. Worker 调用 ExecutionService。
5. Service 调用券商适配器。
6. GUI 没有直接构造券商写客户端，只有 Worker 写。

### 6.5 离屏真图

最终代码后重新生成：

- 健康空仓：1440×900、1920×1080
- 参数编辑：1440×900、1920×1080
- 持仓保护：1440×900、1920×1080
- 风险阻断：1440×900、1920×1080
- 125% / 150% 缩放补充图

路径：

`D:\Desktop\Quant\PA_Agent\scratch\visual\runtime\`

这些图在 2026-07-26 11:12–11:17 生成并人工检查。旧 PRD 中“进行中/未完成”的实施状态早于这些代码和截图，已经过期，必须更新。

## 7. 后端和固定代理已完成的改动

### 7.1 空仓扫描节奏

问题：

- Worker 每 2 秒对账。
- 原实现空仓时也每 2 秒完整读取账户和最近七天 1010 条账单，造成高频重型私有请求。

改动：

- `pa_agent/execution/service.py`
  - 执行对账和命令仍每 2 秒。
  - 无活动 execution 的账户完整刷新固定为 60 秒一次。
  - 首次立即读取。
  - 成功或失败都从本次尝试结束后开始冷却，避免失败后 2 秒重打。
  - 路由变化立即读取。
  - 设置重载重置冷却。
  - 显式 refresh、submit、set_leverage、recover 仍强制完整读取。
  - 活动 execution 保持高频刷新。

### 7.2 OKX 客户端诊断与只读重连

`pa_agent/execution/okx_client.py` 当前工作区包含：

- 默认请求超时从 10 秒提高到 20 秒。
- 网络错误只记录 `METHOD /api/v5/path` 和异常类型。
- 查询参数、私有 ID、头部和凭据不进入日志。
- GET 发生 `BrokerTransportError` 时最多 3 次尝试，间隔 1 秒和 2 秒。
- 每次私有 GET 重试重新生成时间戳、签名和 `expTime`。
- POST 和任何写请求保持 1 次，绝不重试。

`pa_agent/execution/worker.py` 当前工作区包含：

- 错误日志最多展示 3 层安全因果链。
- 已验证日志能显示真实失败端点，同时继续屏蔽长 Token 和常见凭据赋值。

### 7.3 固定代理探针

`scripts/probe_okx_fixed_proxy_node.py` 已加强：

- 候选节点必须走独立 `10982`，不切用户主 v2rayN `10808`。
- 激活前候选 3 轮完整读取。
- 激活后正式 `10981` 再做 3 轮完整读取。
- 每轮包含：
  - 公共时间
  - 余额
  - 持仓
  - SWAP 产品
  - 账户配置/身份
  - 最近七天完整账单分页
- 完整账单为 1010 行。
- 任一阶段失败不激活。
- 激活失败时原子恢复旧配置。
- 旧配置恢复失败时证明 `10981` 已下线，避免带着未知代理继续运行。
- AnyTLS 当前增加：
  - `idle_session_check_interval=30s`
  - `idle_session_timeout=5m`
  - `min_idle_session=1`
  - `tcp_keep_alive=30s`
  - `tcp_keep_alive_interval=30s`

### 7.4 固定代理 ACL

`scripts/provision_okx_fixed_proxy.ps1` 和新增
`tests/unit/test_okx_fixed_proxy_provision_acl.py` 已完成：

- 运行目录只允许当前用户和 SYSTEM。
- Owner、受保护 DACL、继承、Deny、重解析点逐项检查。
- 写入或复制前先收紧目录。
- 子项 ACL 重置后再复核。
- sing-box 运行前最后复核。
- Windows PowerShell 5.1 解析通过。
- UTF-8 BOM。

## 8. 当前最重要的未解决问题

### 固定代理长期自然运行仍不稳定

当前固定代理：

- 地址：`127.0.0.1:10981`
- 节点：`好猫 / US美国1`
- profile：`5635798905899418742`
- 出口 IP：`23.144.12.147`
- 进程：`sing-box.exe`，交接时 PID `30680`
- 协议：AnyTLS + TLS

已验证：

- 美国 1 曾完成“候选 3 轮 + 激活后 3 轮”，六轮全部通过。
- 每轮都完整读完 1010 条账单。
- 日本 2 `4966138000037766026` 也曾通过同样的完整闸门，出口 `151.242.165.77`。
- 两个出口都在 OKX API 白名单。
- 日本 2 和美国 1 在长期自然运行中都会出现 `SSLEOFError`。
- 三条桔子云 OKX 专用节点延迟测试仍为 `-1`，不可使用：
  - `178230800305928383120` OKX 新加坡 1
  - `17823080030561363395` OKX 美国 1
  - `178230800306042345623` OKX 日本 1
- 好猫美国 2、美国 3等出口会得到 OKX `50110`，不在白名单。

当前错误不是“没配固定代理”，而是：

- 刚启动或连续紧密探测时可能完整成功。
- 空闲后自然 Worker 扫描会在公共时间、余额或账单页出现 AnyTLS/TLS EOF。
- 3 次 GET 重连、1/2 秒间隔、AnyTLS 保活仍不能消除。
- Worker 能在某些周期成功，之后又连续失败。
- 风险运行态按设计持续 `kill_active=1`，没有偷偷放行。

交接时最近错误：

- `OKX GET /api/v5/public/time ... SSLEOFError`
- `OKX GET /api/v5/account/balance ... SSLEOFError`
- `OKX GET /api/v5/account/bills ... SSLEOFError`

因此：

- 不能恢复 Campaign。
- 不能清风险停止。
- 不能宣称固定代理长期稳定。
- 不能把六轮紧密探测当成五个自然周期。

### 建议的下一步实验

按优先级：

1. 先保留 fail-closed，不要清 kill。
2. 用独立 `10982` 对候选节点做“公共出口 + 私有完整读取 + 60 秒空闲后再完整读取”的耐久探针；当前探针只做紧密 3 轮，未覆盖空闲后会话失效。
3. 把耐久闸门固化到 `probe_okx_fixed_proxy_node.py`，激活前至少覆盖一个超过 AnyTLS 30 秒默认空闲检查的间隔；不能只靠连续 1 秒三轮。
4. 如果同一好猫 AnyTLS 节点都在空闲后 EOF，优先换不同协议/不同供应商且出口已进入白名单的节点；不要继续无限加客户端重试。
5. 若必须改 supervisor，健康检查和重启必须明确失败关闭，并证明重启过程中 `10981` 不会被其他进程接管；不能盲目每分钟杀进程。
6. 修复后要求 Worker 连续至少 5 个自然 60 秒完整扫描成功，`kill_activated_at` 不再推进。
7. 然后才使用现有专用命令恢复临时风险停止：
   `scratch/recover_transient_risk_stop.py`
8. 恢复命令必须先取得停止发生后的完整账户、身份和账单证据；高水位保持 `78303.57015174496`。

## 9. 交接时真实运行态

采样时间：2026-07-26 15:56（北京时间）

### Worker

- Windows 服务：`PAAgentExecutionWorker`
- 运行账户：LocalService
- 当前 Worker ID：`b265e687-0f4d-4c1c-8a9c-e5900c55fc26`
- PID：`40980`
- `state=running`
- 心跳和 `last_successful_reconcile_at` 仍前进；这里的“成功对账”不等于每轮风险账单刷新成功。
- 当前 Worker 已加载本工作区的 3 次 GET 重连和安全因果日志代码。

### 风险运行态

- `kill_active=1`
- `kill_reason=risk_runtime_BrokerTransportError`
- 交接时最近 `kill_activated_at=2026-07-26T07:56:06.399952+00:00`
- 最近完整账单成功时间：
  `2026-07-26T07:48:21.979673+00:00`
- 高水位：
  `78303.57015174496`
- 最近总权益：
  `76806.0725045165`
- 回撤比例约 1.91%，不是 50% 回撤停止。
- 账户身份摘要保持不变。

### 本地执行与命令

- execution：
  - blocked 3
  - canceled 22
  - closed 11
  - 活动 execution 0
- PENDING/RUNNING 命令 0。
- NEW_RISK 租约 0。
- 1 条历史 UNCERTAIN 已有正式 resolution，不是未解决写入。
- `execution.sqlite3` 和 `execution_control.sqlite3`：
  `PRAGMA integrity_check=ok`

### Campaign

- 当前没有 `pa_agent.okx_demo_campaign` 进程。
- 不得根据旧状态文件写成 Campaign 正在运行。
- 代理稳定、风险专用恢复成功前不要启动。

### Worker 重启事实

- 当前会话没有 Windows 服务重启权限，直接 `Restart-Service` 不可用。
- 此前只在“活动 execution=0、PENDING/RUNNING=0、租约=0、数据库完整”时，插入一条精确的非法维护命令，使旧 Worker 在任何券商写入前因 Pydantic 校验退出，由 WinSW 10 秒后拉起新 Worker；随后按 ID 精确删除维护行并复核完整性。
- 这是运行维护手段，不是产品功能，不应继续扩散。
- `scratch/restart_pa_worker_elevated.ps1` 是未成功获得提权的临时脚本，不要当成已验证入口。

## 10. 测试现状

### 最新完整单元测试

文件：

`scratch/pytest-unit-goal-runtime-final.xml`

结果：

- 1636 收集
- 1618 通过
- 18 失败
- 0 errors
- 0 skipped

18 个失败与历史失败集合相同：

1. `test_structural_inside_outside_mismatch_still_errors_in_strict`
2. `test_audit_relation_flip_label`
3. `test_bars_elapsed_between_parses_iso_t_separator`
4. `test_build_continuity_context_auto_cancels_after_3_bars_unfilled_limit`
5. `test_build_continuity_context_auto_cancels_on_cycle_change_unfilled_limit`
6. `test_build_continuity_context_auto_cancels_on_direction_change_unfilled_limit`
7. `test_normalize_stage2_upgrades_9_0_for_planned_limit`
8. `test_panel_no_prediction_hidden`
9. `test_panel_unpredictable_renders_gray`
10. `test_panel_bullish_renders_green`
11. `test_panel_bearish_renders_red`
12. `test_panel_neutral_renders_yellow`
13. `test_panel_clear_hides_group`
14. `test_play_order_alert_sound_uses_wav_on_windows`
15. `test_stage2_normalizer_passes_breakout_price_check`
16. `test_openclaw_cs_overrides_user_url_and_key`
17. `test_lenient_validator_maps_openclaw_enum_slips`
18. `test_lenient_validator_maps_action_and_limit_order_pending`

注意：这次完整测试早于最后加入的“3 次 GET 重连、AnyTLS 保活、因果日志”改动。最后改动后只运行了聚焦套件：

- OKX client
- ExecutionWorker
- 固定代理脚本

结果：89 通过、0 失败。

所以 Claude 修完代理后必须再跑一次完整 `tests/unit`，确认仍只有同一 18 项。

其他已通过的关键回归：

- 前端、Worker、代理、ACL、集成组合：202 通过、0 失败。
- W1 定向安全套件：341 通过、0 失败。
- 固定代理 ACL：19 通过、0 失败。

Pytest 可能因正式 `.pytest_cache` ACL 输出 cache warning；使用独立 `--basetemp scratch/...`，不要把 cache warning算成测试失败。

全仓 Ruff 不是全绿。当前只对本轮文件做了范围 Ruff；最后一次范围 Ruff因 `worker.py` 的历史 `SIM105` 返回 1，但 89 项 pytest 全过。不要写成最终 Ruff 已全绿。

## 11. Git 现场

### 分支与远程

- 分支：`main`
- HEAD：
  `4282f4334d58d3da9334fe55412ac73c3aea9723`
- HEAD 提交：
  `前端规范：禁止副标题和说明性小字`
- 上游：`origin/main`
- 本地相对 `origin/main`：ahead 1 / behind 0
- `origin/main`：
  `03c23f7692ef10d6c8b12dab8fa4a5b6808a3a43`
- origin：
  `git@github.com:xiaojinlucky/PA_Agent.git`
- origin 仓库：PUBLIC
- upstream：
  `https://github.com/rosemarycox5334-debug/PA_Agent.git`
- upstream 仓库：PUBLIC
- 本次发布目标是 origin，不得误推 upstream。

### 当前 tracked 修改

- `CONTEXT.md`
- `docs/VALIDATION_EVIDENCE.md`
- `docs/prd/04_PA_Agent_Stitch视觉重构PRD.md`
- `pa_agent/app_context.py`
- `pa_agent/execution/okx_client.py`
- `pa_agent/execution/service.py`
- `pa_agent/execution/worker.py`
- `pa_agent/gui/main_window.py`
- `pa_agent/gui/read_models.py`
- `pa_agent/gui/trading_workbench.py`
- `scripts/probe_okx_fixed_proxy_node.py`
- `scripts/provision_okx_fixed_proxy.ps1`
- `tests/conftest.py`
- `tests/unit/test_execution_controller.py`
- `tests/unit/test_execution_service.py`
- `tests/unit/test_execution_worker.py`
- `tests/unit/test_main_window_source_health.py`
- `tests/unit/test_okx_client.py`
- `tests/unit/test_okx_fixed_proxy_scripts.py`
- `tests/unit/test_trading_workbench.py`
- `tests/unit/test_workbench_read_models.py`

### 当前 untracked 正式测试

- `tests/integration/test_trading_workbench_worker_boundary.py`
- `tests/unit/test_okx_fixed_proxy_provision_acl.py`

当前差异约：

- 3469 行新增
- 751 行删除

没有暂存，没有提交，没有推送本轮主体。

## 12. 文档中已知的过期或矛盾内容

必须在最终提交前修正：

1. `docs/prd/04_PA_Agent_Stitch视觉重构PRD.md`
   - 第 10 节还写“GUI 尚未按新规则重新设计和验收”。
   - 第 11.4 节还写读模型、主窗口、工作台和测试“进行中”。
   - 实际代码和 12 张最终截图在这些文字之后完成。
2. `CONTEXT.md`
   - 顶部缺少 2026-07-26 最终前端实现、固定代理新节点和当前长期 EOF 阻断。
   - 仍把 2026-07-25 的旧代理、旧 PID 和旧 Campaign 写在最前部，容易被误读为当前运行态。
3. `docs/VALIDATION_EVIDENCE.md`
   - 已加入部分本轮证据，但最终代理耐久结果、最终全量测试和最终多 Agent 审查尚未写入。
4. 任何“Stitch 四状态生成成功”的旧句子都必须改成：
   - 旧轮次曾有结果；
   - 本轮设计系统资产成功；
   - 本轮屏幕生成失败且没有新屏幕。

## 13. Claude 接手后的建议执行顺序

1. 现场重读 Git 和运行态，保护当前 dirty worktree。
2. 先解决固定代理“空闲后 AnyTLS/TLS EOF”。
3. 为探针增加真实空闲耐久闸门，而不是继续堆即时重试。
4. 聚焦测试通过后，以安全方式让 Worker 加载最终代码。
5. 连续观察至少 5 个自然 60 秒完整风险扫描：
   - `last_bill_scan_at` 每轮推进；
   - `kill_activated_at` 不再推进；
   - Worker 心跳和成功对账新鲜；
   - 两个数据库完整。
6. 只在上述证据成立后，运行专用临时风险恢复命令。
7. 核对：
   - 账户身份不变；
   - 高水位不变；
   - 活动 execution 0；
   - PENDING/RUNNING 0；
   - 未解决写入 0；
   - 租约 0。
8. 使用正式 `restart` 语义恢复 Campaign，继承旧 `last_completed_bar_ms`，不能重复旧 K 线。
9. 观察 3 根自然关闭的 10 分钟 K 线：
   - 不使用 catch-up 根冒充自然周期；
   - no_order 正常；
   - 有真实计划才允许 Demo POST；
   - 不强制交易。
10. 停 Campaign 后做固定代理断开 fail-closed 现场证据，证明不回退直连。
11. 恢复固定代理和健康运行态。
12. 更新 PRD、CONTEXT 和 VALIDATION_EVIDENCE，删除或明确归档临时脚本。
13. 跑最终完整单元测试、集成测试、范围 Ruff、compileall 和 `git diff --check`。
14. 启动至少 3 个独立 Agent 做墨菲定律式对抗审查：
   - UI/前后端状态语义
   - Worker/风险/交易唯一写链
   - 固定代理/ACL/运行态/发布安全
15. 修复审查问题并复验。
16. 使用精确文件清单暂存，做 staged gitleaks、大小和二进制检查。
17. 中文模块化提交并显式推送 `main → origin/main`。
18. 实时确认 origin 仍为 PUBLIC，远端 SHA 与本地 HEAD 一致。
19. 把最终 PRD、代码 diff、测试数字、截图和运行证据交给现有网页版 GPT 会话终审。
20. 网页版 GPT 有实质问题就修复、复验、再次提交推送，直到没有阻塞项。

## 14. 最终完成仍缺少的证据

当前 Goal 不能完成，至少还缺：

- 固定代理 5 个自然扫描周期稳定。
- 临时风险停止专用恢复成功且不重锚。
- Campaign 3 根自然 10 分钟 K 线。
- 固定代理主动断开 fail-closed 现场证据。
- 最新代码后的完整单元测试。
- 严格的第三轮视觉精修证据（若按用户字面要求）。
- 最新 PRD/CONTEXT/VALIDATION_EVIDENCE 同步。
- 最终多 Agent 墨菲审查。
- staged 安全检查、提交、推送。
- origin PUBLIC 和远端 SHA 复核。
- 网页版 GPT 最终审核及修复闭环。

## 15. 不要做的事

- 不要把当前 Worker `state=running` 等同于风险扫描健康。
- 不要在 `kill_active=1` 时启动 Campaign。
- 不要用普通 drawdown CLEAR 解除网络故障。
- 不要重锚高水位。
- 不要用旧空仓截图或旧状态文件声称当前券商真值。
- 不要为了通过验收强制制造订单。
- 不要把连续紧密探测冒充空闲耐久运行。
- 不要把 Stitch 设计系统资产冒充 Stitch 屏幕生成。
- 不要把 89 项聚焦测试冒充最新全量测试。
- 不要把范围 Ruff 冒充全仓 Ruff。
- 不要丢失当前未提交工作区。
- 不要提交 `scratch/`、数据库、日志、凭据或运行态文件。

## 16. 交接结论

前端主工作台、结构化读模型、Controller→Worker 唯一写链联通、四状态离屏图、空仓扫描节奏、固定代理双闸门、ACL 和安全日志都已实现并有聚焦测试。

当前唯一主线硬阻断是固定代理在空闲后的 AnyTLS/TLS 会话不稳定。系统已经正确失败关闭，没有活动执行、没有新风险租约、没有 Campaign，也没有未解决的券商写入。下一任执行者应先把这条真实运行阻断解决，再继续 Campaign 三周期、墨菲审查、文档收口、提交推送和网页版 GPT 终审。
