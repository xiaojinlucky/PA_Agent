"""AI 档案真实连接探测服务。

本模块只返回连接/认证、请求参数接受情况、有效正文和挑战值匹配情况，
并把 reasoning 是否被观察到作为非门禁信息。
它不保存提示词或响应，不把耗时解释为速度，也不把 reasoning 的有无解释为智能水平。
"""

from __future__ import annotations

import importlib
import logging
import queue
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.deepseek_client import CancelledError
from pa_agent.ai.provider_capabilities import resolve_provider_capability
from pa_agent.config.settings import AIProviderSettings
from pa_agent.util.threading import CancelToken

_PROBE_MAX_OUTPUT_TOKENS = 2_048
_PARAMETER_HTTP_STATUSES = frozenset({400, 404, 405, 413, 415, 422})
_PROBE_SLOT = threading.Lock()


class ProbeBusyError(RuntimeError):
    """前一次底层探测尚未退出，拒绝累积新的阻塞线程。"""


class ProbeStatus(StrEnum):
    """可判定检查的三态结果。"""

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderProbeResult:
    """不包含提示词、响应正文或凭证的探测结果。"""

    adapter_id: str
    tested_at: str
    connection_auth: ProbeStatus
    parameter_acceptance: ProbeStatus
    reasoning_observed: bool | None
    response_observed: bool | None = None
    challenge_matched: bool | None = None
    cancelled: bool = False
    error_code: str = ""
    message: str = ""
    http_status: int | None = None

    @property
    def verification_passed(self) -> bool:
        """reasoning 未观察到不等于连接验证失败。"""
        return (
            self.connection_auth is ProbeStatus.PASSED
            and self.parameter_acceptance is ProbeStatus.PASSED
            and self.response_observed is True
            and self.challenge_matched is True
        )

    def verification_checks(self) -> dict[str, bool]:
        """返回可直接持久化的必需检查，不把 reasoning 当成门禁。"""
        return {
            "connection_auth": self.connection_auth is ProbeStatus.PASSED,
            "parameter_acceptance": self.parameter_acceptance is ProbeStatus.PASSED,
            "response_observed": self.response_observed is True,
            "challenge_matched": self.challenge_matched is True,
        }


def _load_exception_types(
    module_name: str, names: tuple[str, ...]
) -> tuple[type[BaseException], ...]:
    """可选 SDK 不存在时保持 probe 模块可导入。"""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return ()
    result: list[type[BaseException]] = []
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, type) and issubclass(value, BaseException):
            result.append(value)
    return tuple(result)


_AUTH_ERROR_TYPES = _load_exception_types(
    "openai",
    ("AuthenticationError", "PermissionDeniedError"),
) + _load_exception_types(
    "cursor_sdk",
    ("AuthenticationError", "PermissionDeniedError"),
)
_TIMEOUT_ERROR_TYPES = (
    (TimeoutError,)
    + _load_exception_types(
        "openai",
        ("APITimeoutError",),
    )
    + _load_exception_types(
        "cursor_sdk",
        ("APITimeoutError",),
    )
)
_NETWORK_ERROR_TYPES = (
    (ConnectionError,)
    + _load_exception_types(
        "openai",
        ("APIConnectionError",),
    )
    + _load_exception_types(
        "cursor_sdk",
        ("NetworkError",),
    )
)
_PARAMETER_ERROR_TYPES = _load_exception_types(
    "openai",
    ("BadRequestError", "NotFoundError", "UnprocessableEntityError"),
) + _load_exception_types(
    "cursor_sdk",
    ("BadRequestError", "ConfigurationError", "NotFoundError"),
)
_RATE_LIMIT_ERROR_TYPES = _load_exception_types(
    "openai",
    ("RateLimitError",),
) + _load_exception_types(
    "cursor_sdk",
    ("RateLimitError",),
)


def _tested_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_probe_challenge() -> str:
    """生成一次性挑战值；不记录、不持久化。"""
    return f"PA_AGENT_PROBE_{secrets.token_hex(8).upper()}"


def _silent_client_logger() -> logging.Logger:
    """阻止底层客户端在探测期间记录请求或供应商错误正文。"""
    logger = logging.Logger("pa_agent.ai.provider_probe.client")
    logger.disabled = True
    logger.propagate = False
    return logger


def _request_with_deadline(
    provider: AIProviderSettings,
    *,
    prompt: str,
    cancel_token: CancelToken,
    timeout_s: float,
) -> Any:
    """用守护线程提供包含客户端创建阶段在内的端到端探测截止时间。"""
    if not _PROBE_SLOT.acquire(blocking=False):
        raise ProbeBusyError("Previous provider probe is still stopping")
    results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            client = create_ai_client(
                provider.model_copy(deep=True),
                logger_=_silent_client_logger(),
            )
            reply = client.stream_chat(
                [{"role": "user", "content": prompt}],
                thinking=provider.thinking,
                reasoning_effort=provider.reasoning_effort,
                cancel_token=cancel_token,
                timeout_s=float(timeout_s),
                max_output_tokens=_PROBE_MAX_OUTPUT_TOKENS,
            )
            results.put_nowait(("reply", reply))
        except Exception as exc:  # noqa: BLE001 - 交给主探测线程统一脱敏分类
            try:
                results.put_nowait(("error", exc))
            except queue.Full:
                pass
        finally:
            _PROBE_SLOT.release()

    worker = threading.Thread(
        target=_run,
        name="provider_probe_request",
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        _PROBE_SLOT.release()
        raise
    deadline = time.monotonic() + float(timeout_s)
    while True:
        if cancel_token.is_set():
            raise CancelledError("Provider probe cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancel_token.set()
            raise TimeoutError("Provider probe timed out")
        try:
            kind, value = results.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if kind == "error":
            raise value
        return value


def _http_status(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _result(
    *,
    adapter_id: str,
    tested_at: str,
    connection_auth: ProbeStatus,
    parameter_acceptance: ProbeStatus,
    reasoning_observed: bool | None = None,
    response_observed: bool | None = None,
    challenge_matched: bool | None = None,
    cancelled: bool = False,
    error_code: str = "",
    message: str = "",
    http_status: int | None = None,
) -> ProviderProbeResult:
    return ProviderProbeResult(
        adapter_id=adapter_id,
        tested_at=tested_at,
        connection_auth=connection_auth,
        parameter_acceptance=parameter_acceptance,
        reasoning_observed=reasoning_observed,
        response_observed=response_observed,
        challenge_matched=challenge_matched,
        cancelled=cancelled,
        error_code=error_code,
        message=message,
        http_status=http_status,
    )


def _exception_result(
    exc: BaseException,
    *,
    adapter_id: str,
    tested_at: str,
) -> ProviderProbeResult:
    """只按异常类型和状态码分类，绝不返回原始异常正文。"""
    status = _http_status(exc)
    if isinstance(exc, ProbeBusyError):
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.UNKNOWN,
            error_code="probe_busy",
            message="前一次底层探测仍在终止，请稍后再试；若持续出现请重启 PA_Agent。",
        )
    if status in (401, 403) or isinstance(exc, _AUTH_ERROR_TYPES):
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.FAILED,
            parameter_acceptance=ProbeStatus.UNKNOWN,
            error_code="authentication_failed",
            message="连接已建立，但认证或权限校验失败。",
            http_status=status,
        )
    if isinstance(exc, _TIMEOUT_ERROR_TYPES):
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.UNKNOWN,
            error_code="timeout",
            message="探测请求超时，无法确认认证和参数。",
            http_status=status,
        )
    if isinstance(exc, _NETWORK_ERROR_TYPES):
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.FAILED,
            parameter_acceptance=ProbeStatus.UNKNOWN,
            error_code="connection_failed",
            message="未能建立连接，无法确认认证和参数。",
            http_status=status,
        )
    if (
        status in _PARAMETER_HTTP_STATUSES
        or isinstance(exc, (*_PARAMETER_ERROR_TYPES, ValueError))
    ):
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.FAILED,
            error_code="parameters_rejected",
            message="供应商或本地适配器拒绝了当前请求参数。",
            http_status=status,
        )
    if status == 429 or isinstance(exc, _RATE_LIMIT_ERROR_TYPES):
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.UNKNOWN,
            error_code="rate_limited",
            message="请求受到限流，无法确认认证和参数。",
            http_status=status,
        )
    return _result(
        adapter_id=adapter_id,
        tested_at=tested_at,
        connection_auth=ProbeStatus.UNKNOWN,
        parameter_acceptance=ProbeStatus.UNKNOWN,
        error_code="provider_error",
        message="供应商返回未分类错误，未保留原始错误正文。",
        http_status=status,
    )


def probe_ai_provider(
    provider: AIProviderSettings,
    *,
    cancel_token: CancelToken | None = None,
    timeout_s: float = 20.0,
) -> ProviderProbeResult:
    """发起一次最短流式请求并返回可供 GUI 线程消费的结构化结果。

    ``timeout_s`` 会传递给现有客户端。OpenAI-compatible 客户端使用 HTTP
    超时，Cursor SDK 适配器使用本地定时器取消运行。
    """
    tested_at = _tested_at()
    attempted_adapter = str(provider.adapter_id or "auto").strip().lower() or "auto"
    if cancel_token is not None and cancel_token.is_set():
        return _result(
            adapter_id=attempted_adapter,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.UNKNOWN,
            cancelled=True,
            error_code="cancelled",
            message="探测已取消。",
        )

    try:
        capability = resolve_provider_capability(provider)
    except ValueError as exc:
        return _exception_result(
            exc,
            adapter_id=attempted_adapter,
            tested_at=tested_at,
        )
    adapter_id = capability.adapter_id

    if timeout_s <= 0:
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.FAILED,
            error_code="invalid_timeout",
            message="探测超时必须大于 0 秒。",
        )
    if not provider.api_key.strip():
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.FAILED,
            parameter_acceptance=ProbeStatus.UNKNOWN,
            error_code="credential_missing",
            message="当前档案没有可供客户端使用的 API key。",
        )
    if capability.client_kind == "openai_chat" and not provider.base_url.strip():
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.FAILED,
            error_code="base_url_missing",
            message="OpenAI-compatible 档案缺少 base URL。",
        )
    if capability.client_kind == "openai_chat" and not provider.model.strip():
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.FAILED,
            error_code="model_missing",
            message="OpenAI-compatible 档案缺少模型 ID。",
        )

    try:
        challenge = _new_probe_challenge()
        request_cancel_token = cancel_token or CancelToken()
        reply = _request_with_deadline(
            provider,
            prompt=(
                "Reply with exactly this text and nothing else: "
                f"{challenge}"
            ),
            cancel_token=request_cancel_token,
            timeout_s=float(timeout_s),
        )
    except CancelledError:
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.UNKNOWN,
            parameter_acceptance=ProbeStatus.UNKNOWN,
            cancelled=True,
            error_code="cancelled",
            message="探测已取消。",
        )
    except Exception as exc:  # noqa: BLE001 - 转换所有供应商 SDK 异常为脱敏结果
        return _exception_result(
            exc,
            adapter_id=adapter_id,
            tested_at=tested_at,
        )

    reasoning = getattr(reply, "reasoning_content", "")
    reasoning_observed = bool(str(reasoning or "").strip())
    content = getattr(reply, "content", "")
    content_text = str(content or "").strip()
    response_observed = bool(content_text)
    if not response_observed:
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.PASSED,
            parameter_acceptance=ProbeStatus.PASSED,
            response_observed=False,
            challenge_matched=False,
            reasoning_observed=reasoning_observed,
            error_code="empty_response",
            message="连接与参数已接受，但模型没有返回有效正文。",
        )
    challenge_matched = content_text == challenge
    if not challenge_matched:
        return _result(
            adapter_id=adapter_id,
            tested_at=tested_at,
            connection_auth=ProbeStatus.PASSED,
            parameter_acceptance=ProbeStatus.PASSED,
            response_observed=True,
            challenge_matched=False,
            reasoning_observed=reasoning_observed,
            error_code="challenge_mismatch",
            message="模型返回了正文，但没有准确回传本次随机挑战值。",
        )
    return _result(
        adapter_id=adapter_id,
        tested_at=tested_at,
        connection_auth=ProbeStatus.PASSED,
        parameter_acceptance=ProbeStatus.PASSED,
        response_observed=True,
        challenge_matched=True,
        reasoning_observed=reasoning_observed,
    )
