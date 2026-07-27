# PA_Agent 硬阻塞

1. **WO-A 固定代理证据闭环**：`metadata.json` 与实际 `config.json` 没有共同指纹，现有脚本无法检测二者不一致。补齐需要修改 `scripts\probe_okx_fixed_proxy_node.py`，但本轮明确禁止触碰 `scripts`；不能用无关失败测试冒充覆盖。
2. **WO-D 真实三标的验收**：最后已知服务端结果为 `401004 token invalid`，且共享 `env` 自 2026-07-24 后未更新。只有用户能在长桥后台重签并自行更新 `D:\Desktop\Quant\env`；不得在对话中发送 token，也不得绕过认证。
3. **WO-E 生产实现**：设计包要求用户先确认 A、B、C 方向，当前推荐 B“证据优先”。最终桌面验收还需要有效长桥 token，并由用户从桌面快捷方式启动。
4. **WO-G-4**：`D:\Desktop\Quant\shared` 是否建立独立 Git 仓库必须由用户决定，禁止擅自 `git init`。
5. **用户侧安全事项**：历史会话曾泄漏 OKX、长桥、模型密钥与交易密码；后台轮换只能由用户本人完成。
