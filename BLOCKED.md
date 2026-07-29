# PA_Agent 硬阻塞

1. **WO-A 固定代理证据闭环**：`metadata.json` 与实际 `config.json` 没有共同指纹，现有脚本无法检测二者不一致。补齐需要修改 `scripts\probe_okx_fixed_proxy_node.py`，但本轮明确禁止触碰 `scripts`；不能用无关失败测试冒充覆盖。
2. **WO-E 视觉与真实桌面验收**：浏览器、Stitch 和 ChatGPT Images 路线已由用户终止，不再构成阻塞。后端与无界面连接层已经补齐，最终外部设计合同为 PRD11。当前只等待用户从其他大模型取得视觉样稿；样稿返回后才进入 `pa_agent/gui` 的 PyQt6 实现和同尺寸视觉审计。`LONGBRIDGE_COMPREHENSIVE` 行情档案已通过服务端只读验证，不再是凭据阻塞；AAPL.US、700.HK、600519.SH 的完整桌面矩阵仍须在后续前端验收中由用户从桌面快捷方式启动。
3. **运行态尚未加载并验收新代码**：2026-07-29 18:01 只读复核确认 Worker 与心跳运行、两库健康、活动 execution/命令、未解决 UNCERTAIN 和有效租约均为 0；但风险停止仍为 `risk_runtime_BrokerTransportError`，Campaign 无 Python 进程。运行中的 Worker 未加载本轮 schema v5 代码。任何恢复或重载前仍需重新取得 OKX Demo 私有只读空仓、空普通单、八类算法单为 0、身份一致、风险与 Worker/两库健康等硬门，并取得用户对运行态维护的明确授权；不得拿历史快照代替。
4. **P0-01 目标 SHA 的 CI 尚未完成**：schema v5、数据库/Controller/Worker 三层一次性约束、并发与迁移测试和完整 CI 配置均已完成；对抗审查发现的 Controller 提交/续租与终态续租竞态、真正写入前复核证据和迁移身份不一致回滚也已补齐。用户随后明确授权把无凭据确定性主门与真实数据源健康检查分开：本地确定性主门为 2048 项通过、0 项跳过、0 失败；独立 live 检查为 7 项跳过、0 失败，其中 4 项是 AkShare 公共端点不可达，3 项是未提供 KKAI 测试密钥。Longbridge `COMPREHENSIVE` 档案已另用官方 `QuoteContext` 真实取得沪深报价和 1h/4h/1d K 线。当前只剩精确提交、推送并验证目标 SHA 的 GitHub CI/JUnit/环境证据，绿色前不得宣称 P0 关闭。
5. **WO-G-4**：`D:\Desktop\Quant\shared` 是否建立独立 Git 仓库必须由用户决定，禁止擅自 `git init`。
6. **用户侧安全事项**：历史会话曾泄漏 OKX、长桥、模型密钥与交易密码；后台轮换只能由用户本人完成。
