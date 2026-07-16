# PA_Agent 移交验证证据

## 发布事实

- 私有仓库：`Jinqingchang/PA_Agent`
- 分支：`main`
- 多模型与 Longbridge 只读行情功能提交：`3d9353f8579e6d661fd314ab6b9e91016d9fdd96`
- 该提交发布后已验证本地 `HEAD`、`origin/main` 与 GitHub 实时分支 SHA 完全一致。
- 本轮开发、测试、打包和审查没有调用真实订单接口，也没有发送订单。

## 变更相关自动测试

运行环境：Windows、Python 3.12.12、pytest 9.1.1、项目 `.venv`。

精确命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_ai_model_settings_dialog.py tests/unit/test_ai_provider_profiles.py tests/unit/test_client_factory.py tests/unit/test_cursor_agent_route.py tests/unit/test_cursor_sdk_client.py tests/unit/test_data_source_factory.py tests/unit/test_data_source_switch_transaction.py tests/unit/test_deepseek_client.py tests/unit/test_kv_prefix_cache.py tests/unit/test_longbridge_source.py tests/unit/test_main_window_source_health.py tests/unit/test_market_defaults.py tests/unit/test_mimo_compat.py tests/unit/test_provider_capabilities.py tests/unit/test_provider_probe.py tests/unit/test_settings_round_trip.py tests/unit/test_snapshot_closed_only_buffer.py -q --basetemp scratch/pytest-evidence-20260716142229
```

结果：232 通过、0 失败；退出码 0；用时 4.156 秒。

其中因安全钩子将两处测试占位符误判为真实密钥，已把占位符改为明显的短值 `dummy`，随后单独重跑 `test_ai_provider_profiles.py` 与 `test_provider_probe.py`：28 通过、0 失败。该 28 项是上述 232 项的子集复验，不应相加为 260 项。

## 编译检查

```powershell
.venv\Scripts\python.exe -m compileall -q pa_agent
```

结果：退出码 0；用时 0.240 秒。

## 非 live 全量测试

精确命令：

```powershell
.venv\Scripts\python.exe -m pytest -m "not live" --basetemp scratch/pytest-full-watchdog-20260716142759
```

watchdog 对 pytest 进程设置 `180000 ms` 硬超时。结果：

- watchdog 判定：`timeout`。
- 包含子进程终止与清理的总用时：181.002 秒。
- stdout 只产生 1 个 `F` 失败标记，未形成 pytest 最终汇总；stderr 为空。
- watchdog 终止 pytest 及其两个后代进程；结束后复核无 PA_Agent pytest/Python 残留进程。

因此这次运行只能证明“非 live 全量测试未在限时内完成，且完成前至少出现 1 个失败标记”。无法据此给出最终通过/失败数量，也不得宣称全量测试通过。用于本次改动验收的有效自动化证据仍是上述 17 文件的 232 通过、0 失败。

## 安全与文件包验证

- 44 文件功能提交的暂存快照由 Gitleaks 8.30.1 扫描：0 命中。
- Git pre-commit 安全钩子原样通过，没有使用 `--no-verify`。
- 安全源码 ZIP 排除 `.git`、真实 `.env`/配置/凭据、根目录日志与记录、行情/训练数据、模型、第三方反馈/打赏图和探针结果。
- `tests/unit/test_debug_widget_masks_key.py` 故意包含高熵合成假钥匙用于遮蔽测试，私有仓库原文件保留；为使 GPT 安全包达到 0 命中，仅从安全 ZIP 副本排除。
- 最终 ZIP 名称、成员数和 SHA-256 以 PA_Agent 独立移交包内 `PACKAGE_MANIFEST.json` 与 `SHA256SUMS.txt` 为准。
