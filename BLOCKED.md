# PA_Agent 硬阻塞

1. **WO-A 固定代理证据闭环**：`metadata.json` 与实际 `config.json` 没有共同指纹，现有脚本无法检测二者不一致。补齐需要修改 `scripts\probe_okx_fixed_proxy_node.py`，但本轮明确禁止触碰 `scripts`；不能用无关失败测试冒充覆盖。
2. **WO-E 真实桌面终验**：PRD11 原生 PyQt6 实现已由 `91715ee2c8af0e6e829031b5a4f375d18f71cf52` 发布，16 张离屏状态/尺寸/缩放证据均已通过；离屏夹具不能代替真实行情和桌面。最终仍须用户从 `D:\Desktop\PA_Agent.lnk` 启动，完成 Longbridge 三市场、Crypto、快速切换和缩放矩阵。该用户桌面验收未过前，不得创建稳定 Release。
3. **阶段 A 的受控 Demo 尚未闭环**：`0cfd5ca4c9c830bb543a4c61e0920ce9d41d22bc` 的 CI 已全绿，生产控制库已由正式切换器安全升级为 schema v5，档案、Worker 心跳和只读对账均通过。首次 Demo-S 入场被 OKX 确定拒绝，最终仍为空仓空单、无活动 execution、无租约、无未解决 `UNCERTAIN`；客户端只保存了顶层“全部操作失败”，未保留逐单真实拒绝原因。当前离线修复已完成 14 条红灯转绿，相关回归 388 项和最终非 live 2363 项均通过，独立复审 P0/P1/P2 均无；目标 SHA 的 CI 和新 SHA Worker 重载通过前，禁止第二次券商写入。迁移档案仍只承诺本机只读 ACL 与哈希可检测，不承诺断电级目录持久性或 WORM。
4. **WO-G-4**：`D:\Desktop\Quant\shared` 是否建立独立 Git 仓库必须由用户决定，禁止擅自 `git init`。
5. **用户侧安全事项**：历史会话曾泄漏 OKX、长桥、模型密钥与交易密码；后台轮换只能由用户本人完成。

截图时序最终修复 `c16f727185dfc5341ae7e939a8275f18d9fc166e` 的 CI 与候选
workflow 已全绿；下载资产、全新安装、跨时区源码重建、扫描和 16 张 PNG 逐图复核
均通过，阶段 D 候选证据已经关闭。当前稳定 Release 仍有两个硬门：阶段 A 的受控
OKX Demo 入场—保护—离场闭环尚未完成；用户还需从正式快捷方式
完成四市场、快速切换和缩放桌面验收。任一未过都不得创建稳定 `v0.1.0` tag 或
GitHub Release。
