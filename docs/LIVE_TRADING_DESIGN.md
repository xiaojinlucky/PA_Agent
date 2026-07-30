# PA_Agent v0.1.0 执行安全边界

本文件只描述 v0.1.0 的当前执行边界。旧版 Longbridge 交易、OKX Live、账户回退和历史试验细节可从 Git 历史查阅，但不是本版本支持能力或启用说明。

## 支持范围

- 唯一允许新增风险的路线：`OKX / demo`。
- 交易默认关闭，自动执行默认关闭。
- 已有 OKX Demo 工作台保留入场、部分成交、券商原生保护、主动离场、重启对账和风险停止。
- 多市场工作台只读，不含交易控件，也不访问执行层。

## 明确排除

- OKX Live。
- Longbridge paper、综合账户、日内账户的任何交易写入。
- 绕过 Worker 的桌面、脚本、Adapter 或数据库直写。
- 无人值守生产交易、收益承诺或策略有效性承诺。

旧配置中仍可能保留 Longbridge 或 OKX Live 字段，以便读取历史记录和失败关闭。字段存在不代表路线可用。Controller、ExecutionService 的 Worker 路径和 Worker 都会拒绝这些路线新增风险。

## 唯一写入链

```text
已可靠落盘的分析结果
  → ExecutionController
  → WorkerStore 持久命令
  → 唯一 ExecutionWorker
  → ExecutionService
  → OKX Demo Adapter
```

任何券商写入都必须经过 Worker。桌面只负责提交命令和展示状态，不能直接调用券商写接口。

## 一次性新增风险授权

Worker schema v5 把新增风险租约耐久绑定到唯一 `command_id`。`enqueue()` 在同一数据库事务中完成：

1. 确认租约未被消费；
2. 绑定候选命令；
3. 插入命令。

任一步失败都会整体回滚。数据库保证每个非空租约最多关联一条 `submit` 或 `set_leverage` 命令；Worker 还会再次核对命令、路由、请求者和配置指纹。`UNCERTAIN` 不会自动重放。撤单、减仓、保护和主动离场属于降低风险操作，继续按现有规则处理。

## 运行态迁移

v4→v5 只能由持有唯一 Worker 锁的正式代码执行。迁移前必须证明：

- OKX Demo 私有只读账户可读；
- 账户路线正确；
- 空仓、空普通单、空算法单；
- 无活动 execution、待处理或运行中命令、未解决 `UNCERTAIN` 和有效新增风险租约；
- 两个 SQLite 库 `quick_check=ok`。

迁移必须先备份数据库、最短停止 Worker、由正式启动路径完成迁移，再证明新启动时间、完整 Git SHA、schema v5、心跳和只读对账。迁移不得自动清除风险停止。

若上述任一事实不可读，运行态迁移和 Demo 写入立即停止。离线代码、测试、文档和只读行情工作可继续。

## 受控 Demo 闭环

稳定发布前只允许一次已授权的 `controlled_reproducible` 闭环：

```text
入场命令
  → 券商确认成交
  → 至少两条原生保护
  → 主动离场
  → CLOSED
  → 最终空仓、空普通单、空算法单
```

证据必须证明该闭环只消费一条新增风险租约并产生一条新增风险命令，Worker 完成路由、请求者和配置指纹核验。证据需脱敏，不得包含账号、精确余额、订单号、数据库或原始日志。

## 当前状态

- 离线 schema v5、Controller 和 Worker 安全门已有测试证据。
- 当前固定 OKX 网络入口不可用，尚不能取得新鲜私有只读真相。
- 因此运行 Worker 尚未迁移，受控 Demo 闭环尚未执行，v0.1.0 仍是发布候选，不是稳定 Release。
