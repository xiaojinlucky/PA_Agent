# Implementation Notes — PA_Agent 多标的/多周期/时间戳阶段(S0 起)

> 本文件记录实现过程中的「计划外 edge case / 偏离原计划 / prior art / 权限语义不确定 / 数据历史与兼容性 / 保守选择」。
> 每条:① 原计划是什么;② 代码/运行态实际揭示了什么;③ 做了什么保守选择;④ 需要用户之后决定什么。
> 执行工单:`PA_Agent项目设计规划/04_PA_Agent_多标的多周期时间戳_Implementation_Prompt.md`。日期均以 UTC 标注时间戳时注明。

---

## N1. 风险停止码与工单假设不一致(50001 → 503)

- **原计划**:工单第 0 节以 Codex 交接线索为起点,假设运行态断点 = `risk_runtime_50001` 恢复分类待 CI+重载。
- **代码/运行态揭示**:`records/execution_control.sqlite3` 的 `risk_runtime_state` 表显示 `kill_active=1, kill_reason='risk_runtime_503', kill_activated_at=2026-07-30T18:23:07Z`。`worker_commands` 历史显示 07-30T18:04 有一条 `clear_drawdown_stop`(reason `recover_transient_risk_read_failure`)已 `succeeded`,随后 18:06 的 Demo-S `submit` `failed(failure_code=submit_result_needs_attention)`——即停止码在交接后已从 50001 演进到 503。CONTEXT.md 顶部记载的"50001 恢复分类"只覆盖 50001,未提 503。
- **保守选择**:S0 只记录事实,不执行任何恢复、不清除停止、不重载、不写券商。一切判断以控制库实时只读查询为准。
- **需用户决定**:① 726712b 的 50001 恢复分类是否覆盖 503(需核对恢复白名单逻辑);② 是否授权"Worker 重载 + 正式恢复命令"流程,以及此时机。

## N2. Worker 未加载最新提交,但 CI 已绿

- **原计划**:工单假设"SHA 提交级 CI 转绿 → Worker 重载"是 S0 前置门。
- **代码/运行态揭示**:本地 HEAD = 远端 HEAD = `726712b`(git 一致);`726712b` 的 GitHub check-run `test` = `completed/success`(run 30574355712,完成于 2026-07-30T19:32Z)。但 `worker_heartbeats.started_at=2026-07-30T17:59:58Z` 早于该提交推送,Worker(PID 37020 对应 `PAAgentExecutionWorker.ex` 服务进程)不可能加载 726712b。
- **保守选择**:不重载 Worker(重载属运行态写动作,需用户授权与最短停机窗口);S1 只读监测开发与运行态并行不冲突。
- **需用户决定**:是否现在安排 Worker 重载(需确认当前无活动 execution/仓位/挂单/租约——本轮只读核验已确认这些均为 0/终态,重载窗口满足)。

## N3. Campaign 文件状态自相矛盾(active 但已过期 4 天)

- **原计划**:假设 Campaign 处于 active 推进。
- **代码/运行态揭示**:`records/okx_demo_campaign.json`:`status=active` 但 `expires_at=2026-07-27T23:33:34Z`(已过期 4 天)、`updated_at=2026-07-30T18:07Z`(22h+ 未更新)、`last_plan_result=execution:rejected`、`last_error='Demo-S 入场命令失败: submit_result_needs_attention'`。而 Worker 心跳 `last_seen_at=2026-07-31T08:50:07Z`、`last_successful_reconcile_at` 同期、`account_snapshots` 最新 08:49:57Z(均为刚刚,北京时间 16:49)→ 对账层活跃,Campaign 业务循环未推进。
- **保守选择**:以控制库/执行账本为实时事实,把 Campaign 文件当作历史/参考;不修改 Campaign 文件。
- **需用户决定**:过期 Campaign + `submit_result_needs_attention` 是否需要在恢复流程中一并处置(创建新 Campaign?清理旧记录?)。

## N4. 配置指纹是项目特定算法,不是文件哈希(prior art)

- **原计划**:S0 拟用 `config/settings.json` 的文件 sha256 与 Campaign `config_fingerprint` 比对以确认配置未变。
- **代码揭示**:项目存在**两个同名不同义**的指纹函数——`okx_demo_campaign.campaign_config_fingerprint()`(对 symbol/timeframe/market_data_source/instrument/product/margin_mode/sizing 的规范化 payload 哈希)与 `config.settings.provider_config_fingerprint()`(AI provider 能力指纹)。两者都不是文件哈希。Campaign 文件里的 `e83fb7dd…` 属前者。
- **保守选择**:放弃文件哈希比对;后续如需核对配置一致性,用官方 `campaign_config_fingerprint()` 在凭据隔离环境计算并比对,不手动复刻哈希算法。
- **需用户决定**:无(方法学修正)。

## N5. 执行账本与控制库是两套版本体系

- **原计划**:工单统一称"生产控制库 schema v5"。
- **代码揭示**:`execution_control.sqlite3` 为 v5(`worker_meta` 含 `worker_safe_cutover` 等 v5 痕迹);`execution.sqlite3` 的 `execution_meta.schema_version=2`(执行账本自己的版本)。两库版本号独立,不能混用。
- **保守选择**:S1/S2 只读开发一律不写这两个库;若监测/报警需要持久化,先评估复用现有缓存,不足时另立独立只读文件,并单独声明版本。
- **需用户决定**:无。

## N6. 生产日志被 filelock DEBUG 刷屏,业务状态只能查库

- **原计划**:从 `logs/pa_agent.log` 确认业务心跳/风险/对账。
- **代码揭示**:当前日志文件 21,336 行 100% 为 `filelock` DEBUG(栅栏锁循环,每 ~1s 获取/释放 `execution_databases.fence.lock`),无 INFO/WARN/ERROR 业务行;滚动文件同样如此。
- **保守选择**:业务状态一律以控制库/执行账本只读查询为准,不把日志文本当事实来源(与"只认本轮实时证据"一致);本阶段不调整日志级别。
- **需用户决定**:可选——日志级别调优属维护项,不在本阶段范围。

## N7. git 未跟踪目录 .agents/ .claude/

- **原计划**:确认工作区干净。
- **代码揭示**:`git status` 显示 `?? .agents/`、`?? .claude/`(工具配置目录,非代码)。
- **保守选择**:不提交、不删除、不加入本阶段改动。
- **需用户决定**:是否在 `docs/` 或根目录决策是否 `.gitignore`(非本阶段)。

## N8. MT5 数据源已存在(工单假设需新建)

- **原计划**:工单 S3 假设 MT5 只读源"接入"需新建适配器(`MT5 桥接进程,broker-time 归一`)。
- **代码揭示**:`pa_agent/data/mt5.py` 已存在完整 `MT5Source`(约数百行),接口含 `connect()/subscribe(symbol, tf)/latest_snapshot(n)`,docstring 明确"`bars[0] = forming bar`",已含 `_TF_MAP` 与 `normalize_kline_bar`。S3 的正确路径是先审计/复用/补齐它,而不是从零新建。
- **保守选择**:S1 阶段不触碰 mt5.py;S3 工单计划改为"审计现有 MT5Source → 补 broker-time 归一与对时校验 → 补测试",把桥接进程部分单独评估。
- **需用户决定**:S3 是否只做"现有源审计+加固",不做"新桥接进程"。

## N9. 测试基线复现(2026-07-31 16:5x)

- **原计划**:S0 验收要求"非 live 测试以开工时可复现基线为准且 0 失败"。
- **运行态揭示**:在 venv + offscreen + 隔离 basetemp 下复跑 `tests/unit tests/property tests/integration`,结果见本轮报告(通过数/跳过数/失败数)。
- **保守选择**:基线数字写入 CONTEXT 与实施计划,作为 S1 测试门"只升不降"的对照。
- **需用户决定**:无。

## N10. "重复即拒批"在 watchlist 批量报价路径已存在(工单假设为残留缺陷)

- **原计划**:工单第 0 节列出已知残留"行情服务重复返回同一标的时当前取最后一条,必须改为重复即拒批",作为本阶段顺带关闭项。
- **代码揭示**:`pa_agent/data/market_workspace.py` 的 `WatchlistQuoteSet.__post_init__` 已实现重复即拒批(重复 symbol 抛 ValueError),且同时校验 generation/request_sequence/market/source/集合一致性;`WatchlistRequest` 也校验自选去重与 1..100 上限。批量报价主路径并非"取最后一条"。
- **保守选择**:S1 开工时先写/跑针对性红灯测试精确定位是否还有残留路径(如非 watchlist 的 quote 合并路径)再决定改动;不假设残留一定存在。
- **需用户决定**:无。

## N11. 运行态 503 停止需要新修复(726712b 只覆盖 50001)

- **原计划**(用户确认"三件都办"后):726712b 已使 50001 可复核恢复,预期恢复当前停止。
- **代码/运行态揭示**:`risk_runtime_state.kill_reason=risk_runtime_503`;`runtime.py` 的 `RECOVERABLE_TRANSIENT_RISK_STOP_REASONS` 只有 {BrokerApiError, BrokerTransportError, IncompleteRead, 50001, 50004},**无 503**;`recover_transient_read_failure` 第一道检查即拒(`risk_recovery_reason_not_allowed`)。`runtime.py:402` 证明停止码由异常 code 动态拼接(`f"risk_runtime_{exc.code}"`),503=OKX HTTP Service Unavailable,与 50001 同类临时故障。
- **保守选择**:按 726712b 同模式修复——① 先把 `risk_runtime_503` 加入 3 个参数化测试;② 跑出红灯(`test_..._recovery_...[risk_runtime_503]` FAILED,证实缺口);③ 白名单加 `"risk_runtime_503"`;④ 全文件 43/43 绿灯。未触碰其他恢复逻辑;`preserve_existing_reason` 保证 503 不会覆盖非临时停止(如 drawdown)。
- **需用户决定**:无(已在"三件都办"授权内;提交/推送/CI/重载/正式复核恢复按流程继续)。

## N12. 全量测试基线复跑超时/被并发干扰

- **原计划**:S0 验收复现 2366 项非 live 基线。
- **运行态揭示**:全量 unit+property+integration 后台跑 46 分钟未完成(疑似个别网络冒烟长等待);期间并行跑单文件 pytest 时,单文件进程输出异常(只输出一个点即退出),停掉后台全量后单文件恢复正常——并行 pytest 实例存在干扰(疑 conftest 临时凭据/共享资源)。
- **保守选择**:后台全量基线已停止;后续以"先单文件/分组,再全量串行"方式跑测试;全量仅在无其他 pytest 运行时执行。
- **需用户决定**:无。

---

## 下一步 Prompt 应加入的规则建议

1. **运行态断点必须每轮实时核验,不得沿用上一轮结论**:停止码、Worker 加载 SHA、Campaign 状态都可能在本轮之前演变;核验矩阵(HEAD/CI/Worker/Campaign/对账/命令/租约/停止)写入工单固定模板,每阶段开工重跑。
2. **恢复类命令的覆盖范围要逐码核对**:恢复白名单按 error code 展开,不能凭 commit 标题推断覆盖 50001 就覆盖 503;新增停止码要先红灯复现再谈恢复。
3. **两个 schema 版本体系必须显式区分**:`execution_control`(v5)与 `execution`(schema_version=2)独立;文档中不得再混称"控制库 v5"覆盖两库。
4. **配置指纹一律走项目官方函数,禁止文件哈希/自算**:同名函数 `campaign_config_fingerprint` 与 `provider_config_fingerprint` 语义不同,引用前先确认归属。
5. **生产日志不作为业务状态来源**:filelock DEBUG 刷屏时,以控制库只读查询为准;若某阶段要依赖日志,必须先修日志级别(另立工单)。
6. **运行态写动作(重载/恢复/新建 Campaign)必须单独授权**:实施工单本身不构成授权;每个此类动作需当轮列出安全门与用户确认。
7. **S1 只读开发的落库边界**:任何新增持久化先走"复用现有缓存→红灯证明不足→最小独立只读文件"三步,禁止直接写控制库/执行账本。
