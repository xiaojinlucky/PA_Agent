# PA_Agent 硬阻塞

1. **WO-A 固定代理证据闭环**：`metadata.json` 与实际 `config.json` 没有共同指纹，现有脚本无法检测二者不一致。补齐需要修改 `scripts\probe_okx_fixed_proxy_node.py`，但本轮明确禁止触碰 `scripts`；不能用无关失败测试冒充覆盖。
2. **WO-E 真实桌面终验**：PRD11 原生 PyQt6 实现已由 `91715ee2c8af0e6e829031b5a4f375d18f71cf52` 发布，16 张离屏状态/尺寸/缩放证据均已通过；离屏夹具不能代替真实行情和桌面。最终仍须用户从 `D:\Desktop\PA_Agent.lnk` 启动，完成 Longbridge 三市场、Crypto、快速切换和缩放矩阵。该用户桌面验收未过前，不得创建稳定 Release。
3. **阶段 A 的 OKX Demo 私有真相不可读**：只读复核确认 Worker、心跳、对账和两库健康，且没有活动 execution、待执行命令、未解决 UNCERTAIN 或有效新增风险租约；控制库仍为 v4，风险停止仍为 `risk_runtime_BrokerTransportError`。随后使用正式 Demo 凭据和项目固定代理做私有只读硬门，第一步服务器时间读取即因固定代理没有监听而连接被拒绝；未绕过固定代理。当前无法证明实时账户身份、空仓、空普通单和全部算法单，因此禁止迁移 v5、重载 Worker、解除风险停止或进行 Demo 写入；离线 B/C/D 阶段继续。
4. **WO-G-4**：`D:\Desktop\Quant\shared` 是否建立独立 Git 仓库必须由用户决定，禁止擅自 `git init`。
5. **用户侧安全事项**：历史会话曾泄漏 OKX、长桥、模型密钥与交易密码；后台轮换只能由用户本人完成。

本轮新增硬阻塞：无。`3221fe8` 的 CI 已证明 Qt 生命周期和 PowerShell 5.1
编码问题关闭；候选 workflow 随后正确拒绝源码 ZIP 中由 `.gitkeep` 带入的四个
运行态目录。首轮无锚规则会误删 `pa_agent/records`，已在提交前由对抗审查拦下；
当前根锚规则已由 `e835404` 的 CI 和候选源码归档门共同证明有效。候选 workflow
随后暴露安装脚本会把 PATH 中多个 `git.exe` 合成一个命令；当前已选择首个可执行
Git，测试会自行构造并确认两个 Git；独立审查发现的非确定性 P2 已关闭，本机
2230 项非 live、真实新装、快捷方式和受控卸载均通过。提交级 CI 与候选 workflow
仍须绑定将要发布的 SHA 运行，不从上述真实运行态与桌面阻塞中移除任何一项。
