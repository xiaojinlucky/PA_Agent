# PA_Agent 硬阻塞

1. **WO-A 固定代理证据闭环**：`metadata.json` 与实际 `config.json` 没有共同指纹，现有脚本无法检测二者不一致。补齐需要修改 `scripts\probe_okx_fixed_proxy_node.py`，但本轮明确禁止触碰 `scripts`；不能用无关失败测试冒充覆盖。
2. **WO-E 视觉与真实桌面验收**：浏览器、Stitch 和 ChatGPT Images 路线已由用户终止，不再构成阻塞。后端与无界面连接层已经补齐，最终外部设计合同为 PRD11。当前只等待用户从其他大模型取得视觉样稿；样稿返回后才进入 `pa_agent/gui` 的 PyQt6 实现和同尺寸视觉审计。`LONGBRIDGE_COMPREHENSIVE` 行情档案已通过服务端只读验证，不再是凭据阻塞；AAPL.US、700.HK、600519.SH 的完整桌面矩阵仍须在后续前端验收中由用户从桌面快捷方式启动。
3. **运行态尚未加载并验收新代码**：2026-07-29 18:01 只读复核确认 Worker 与心跳运行、两库健康、活动 execution/命令、未解决 UNCERTAIN 和有效租约均为 0；但风险停止仍为 `risk_runtime_BrokerTransportError`，Campaign 无 Python 进程。运行中的 Worker 未加载本轮 schema v5 代码。任何恢复或重载前仍需重新取得 OKX Demo 私有只读空仓、空普通单、八类算法单为 0、身份一致、风险与 Worker/两库健康等硬门，并取得用户对运行态维护的明确授权；不得拿历史快照代替。
4. **WO-G-4**：`D:\Desktop\Quant\shared` 是否建立独立 Git 仓库必须由用户决定，禁止擅自 `git init`。
5. **用户侧安全事项**：历史会话曾泄漏 OKX、长桥、模型密钥与交易密码；后台轮换只能由用户本人完成。
