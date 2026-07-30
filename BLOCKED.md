# PA_Agent 硬阻塞

1. **WO-A 固定代理证据闭环**：`metadata.json` 与实际 `config.json` 没有共同指纹，现有脚本无法检测二者不一致。补齐需要修改 `scripts\probe_okx_fixed_proxy_node.py`，但本轮明确禁止触碰 `scripts`；不能用无关失败测试冒充覆盖。
2. **WO-E 真实桌面终验**：PRD11 原生 PyQt6 实现已由 `91715ee2c8af0e6e829031b5a4f375d18f71cf52` 发布，16 张离屏状态/尺寸/缩放证据均已通过；离屏夹具不能代替真实行情和桌面。最终仍须用户从 `D:\Desktop\PA_Agent.lnk` 启动，完成 Longbridge 三市场、Crypto、快速切换和缩放矩阵。该用户桌面验收未过前，不得创建稳定 Release。
3. **阶段 A 的 OKX Demo 私有真相不可读**：只读复核确认 Worker、心跳、对账和两库健康，且没有活动 execution、待执行命令、未解决 UNCERTAIN 或有效新增风险租约；控制库仍为 v4，风险停止仍为 `risk_runtime_BrokerTransportError`。随后使用正式 Demo 凭据和项目固定代理做私有只读硬门，第一步服务器时间读取即因固定代理没有监听而连接被拒绝；未绕过固定代理。当前无法证明实时账户身份、空仓、空普通单和全部算法单，因此禁止迁移 v5、重载 Worker、解除风险停止或进行 Demo 写入；离线 B/C/D 阶段继续。
4. **WO-G-4**：`D:\Desktop\Quant\shared` 是否建立独立 Git 仓库必须由用户决定，禁止擅自 `git init`。
5. **用户侧安全事项**：历史会话曾泄漏 OKX、长桥、模型密钥与交易密码；后台轮换只能由用户本人完成。

本轮新增硬阻塞：无。路径误报修复提交 `025becf` 的 CI run `30513486939` 已全绿，
但下载后加严复核发现离线与实时 JUnit 的跳过文本仍分别含 1 处和 7 处 GitHub
Runner 绝对路径；因此该证据不能进入发布，候选 workflow 未触发。当前新增的 JUnit
脱敏只替换绝对路径，测试结果合同保持 2232/0/0/1 与 7/0/0/7；真实 Runner 证据
复现后的 6 文件目录通过路径和密钥扫描。本地非 live 2233 项通过、0 失败。提交级
CI 与候选 workflow 仍须绑定将要发布的 SHA 运行，不从上述真实运行态与桌面阻塞中
移除任何一项。
