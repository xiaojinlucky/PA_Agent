# PA_Agent 硬阻塞

1. **WO-A 固定代理证据闭环**：`metadata.json` 与实际 `config.json` 没有共同指纹，现有脚本无法检测二者不一致。补齐需要修改 `scripts\probe_okx_fixed_proxy_node.py`，但本轮明确禁止触碰 `scripts`；不能用无关失败测试冒充覆盖。
2. **WO-E 真实桌面终验**：PRD11 已由本轮 `/goal` 拍板为新多市场页的最终规格，外部样稿不再是生产实现前置条件。离屏实现和视觉矩阵可继续；最终仍须用户从 `D:\Desktop\PA_Agent.lnk` 启动，完成真实 Longbridge 三市场、Crypto、快速切换和缩放矩阵。该用户桌面验收未过前，不得创建稳定 Release。
3. **阶段 A 的 OKX Demo 私有真相不可读**：2026-07-29 23:16 只读复核确认 Worker/心跳/对账和两库健康，活动 execution、pending/running 命令、未解决 UNCERTAIN 和有效 NEW_RISK 租约均为 0；控制库仍为 v4，风险停止仍为 `risk_runtime_BrokerTransportError`。随后使用正式 Demo 凭据和项目固定代理做私有只读硬门，第一步服务器时间读取即因 `127.0.0.1:10981` 无监听而连接被拒绝；未绕过固定代理。当前无法证明实时账户身份、空仓、空普通单和全部算法单，因此禁止迁移 v5、重载 Worker、解除风险停止或进行 Demo 写入；离线 B/C/D 阶段继续。
4. **WO-G-4**：`D:\Desktop\Quant\shared` 是否建立独立 Git 仓库必须由用户决定，禁止擅自 `git init`。
5. **用户侧安全事项**：历史会话曾泄漏 OKX、长桥、模型密钥与交易密码；后台轮换只能由用户本人完成。
