# PA_Agent 阶段 0 基线核验记录

核验日期：2026-07-23
核验目录：`D:\Desktop\Quant\PA_Agent`
核验目标：统一路线图阶段 0

## 已核验事实

| 项目 | 结果 |
|---|---|
| 当前分支 | `main` |
| 当前 HEAD | `1a04c144f810ffb486280ed8a1875ff0130bb070` |
| OKX 行情模块 | `pa_agent/data/okx_source.py` 已存在；本轮是审核验收，不是新增 |
| 主界面接入 | `data/factory.py` 注册 `okx`，`main_window.py` 支持 OKX 切换、订阅、重连、状态栏提示和品种/订阅失败提示 |
| 执行状态真值 | `ExecutionState` 共 13 个状态；`PROTECTING` 存在且属于 `ACTIVE_EXECUTION_STATES` |
| 券商写入边界 | `ExecutionWorker` 为唯一券商写入进程；本阶段未修改执行代码 |
| 逐笔事实账本 | `records/execution.sqlite3`，生命周期扩展只能引用和聚合 |

## OKX 行情源能力边界

`OkxSource` 通过 OKX 公共接口读取 K 线，不读取账户、订单或交易凭据。它支持现货和永续品种，订阅前在线校验品种处于 `live` 状态，支持 `1m/3m/5m/15m/30m/1h/2h/4h/1d`，单次公共 K 线最多 300 根，其中分析窗口受预热和缓冲限制为 245 根。它校验时间倒序、收盘标记、最多一根形成中 K 线、价格/成交量非负和 OHLC 合法性，并保留已收盘与形成中标记。

它不是账户执行客户端，也不替代 `ExecutionWorker → ExecutionService → OkxAdapter` 写入链路。品种动态规格、账户余额、最大可开数量、订单、成交、保护和仓位来自执行侧的独立只读/写入路径，不能从行情源推断。

## 状态机边界

当前代码的 13 个 `ExecutionState` 是单笔执行记录的真值，包含 `PROTECTING`。产品 PRD 的自动循环状态（如 `WAITING_BAR`、`ANALYZING`、`SUPERVISING`）和持仓生命周期状态（如 `FLAT`、`ADDING`、`REVERSING_ENTRY`）是目标态，不是当前枚举的替代品；后续阶段必须单独建模并写出映射。

## 工作区边界

开始核验时工作区已有上一轮 Demo/配置任务的未提交改动，涉及 `CONTEXT.md`、配置说明、执行设计、提示词、Demo 运行器及其测试，以及 `tools/setup_git_secrets.ps1` 的无关改动。本阶段只新增/修正文档，不覆盖这些改动，不将它们混入阶段 0 的文件范围。

## 结果

阶段 0 的文档基线可以建立。当前没有因 HEAD、OKX 行情模块或执行状态真值而阻塞的事实差异；旧规划文档中的旧基线号仅保留在 `docs/prd/` 原文归档的来源说明中，活动文档统一以当前 HEAD 为准。

文档目录的密钥扫描通过（0 命中）。对整个混合工作区的扫描未通过，报告 527 个既有运行记录/历史文件命中并跳过若干权限受限的测试临时目录；这些文件不属于本阶段新增文档，且本阶段没有暂存快照。真正提交前仍必须只按用户确认的文件建立 staged snapshot，再对 staged snapshot 做密钥扫描；在此之前不宣称全仓库安全扫描通过。
