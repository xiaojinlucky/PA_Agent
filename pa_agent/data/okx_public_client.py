"""OKX V5 公开行情 GET 客户端。

它只有公开读取方法，不接受密钥，也没有任何私有或写请求入口。
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

OKX_PUBLIC_BASE_URL = "https://www.okx.com"
OKX_PUBLIC_FIXED_PROXY_URL = "http://127.0.0.1:10981"
OKX_PUBLIC_TIMEOUT_SECONDS = 20.0
OKX_PUBLIC_READ_ATTEMPTS = 3
OKX_PUBLIC_RETRY_DELAYS_SECONDS = (1.0, 2.0)
_OKX_PROXY_URL_ENV = "PA_AGENT_OKX_PROXY_URL"


class OkxPublicError(RuntimeError):
    """公开行情客户端的稳定错误基类。"""


class OkxPublicTransportError(OkxPublicError):
    """网络、HTTP 或响应结构不可用。"""


class OkxPublicApiError(OkxPublicError):
    """OKX 明确返回非零业务错误码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(str(message or "OKX 公开行情请求失败"))


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class _FixedLocalProxyHandler(urllib.request.ProxyHandler):
    """绑定项目的本机代理，不读取系统代理绕过规则。"""

    def proxy_open(self, request, proxy, request_type):
        original_type = request.type
        parsed = urllib.parse.urlsplit(proxy)
        assert parsed.hostname is not None
        assert parsed.port is not None
        host_port = (
            f"[{parsed.hostname}]:{parsed.port}"
            if ":" in parsed.hostname
            else f"{parsed.hostname}:{parsed.port}"
        )
        request.set_proxy(host_port, parsed.scheme or request_type)
        if original_type == (parsed.scheme or request_type) or (
            original_type == "https"
        ):
            return None
        return self.parent.open(request, timeout=request.timeout)


class UrlLibPublicTransport:
    """只经无凭据的本机固定代理读取 OKX 公开接口。"""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        opener: Any | None = None,
    ) -> None:
        self._proxy_url = self._validated_proxy_url(
            os.environ.get(
                _OKX_PROXY_URL_ENV,
                OKX_PUBLIC_FIXED_PROXY_URL,
            )
            if proxy_url is None
            else proxy_url
        )
        self._opener = opener or urllib.request.build_opener(
            _FixedLocalProxyHandler(
                {
                    "http": self._proxy_url,
                    "https": self._proxy_url,
                }
            )
        )

    @staticmethod
    def _validated_proxy_url(value: object) -> str:
        text = str(value or "").strip().rstrip("/")
        try:
            parsed = urllib.parse.urlsplit(text)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("OKX 独立代理地址无效") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OKX 独立代理必须是无凭据的本机 HTTP 地址")
        return text

    @staticmethod
    def _safe_failure_type(exc: BaseException) -> str:
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return (
            type(exc).__name__
            if cause is exc
            else f"{type(exc).__name__}:{type(cause).__name__}"
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        if method.upper() != "GET" or body is not None:
            raise ValueError("OKX 公开行情客户端只允许无正文 GET")
        request = urllib.request.Request(
            url,
            headers=headers,
            method="GET",
        )
        if request.type != "https":
            raise ValueError("OKX 公开行情只允许 HTTPS")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(
                    int(response.status),
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(int(exc.code), exc.read())
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise OkxPublicTransportError(
                "OKX 公开行情网络请求失败"
                f"（{self._safe_failure_type(exc)}）"
            ) from exc


class OkxPublicClient:
    """只有三个公开 GET 端点的有界客户端。"""

    def __init__(
        self,
        *,
        base_url: str = OKX_PUBLIC_BASE_URL,
        transport: HttpTransport | None = None,
        timeout: float = OKX_PUBLIC_TIMEOUT_SECONDS,
    ) -> None:
        normalized = str(base_url or "").strip().rstrip("/")
        parsed = urllib.parse.urlsplit(normalized)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OKX 公开行情地址必须是无凭据 HTTPS 地址")
        self._base_url = normalized
        self._transport = transport or UrlLibPublicTransport()
        self._timeout = float(timeout)
        if self._timeout <= 0:
            raise ValueError("OKX 公开行情超时必须为正数")

    @staticmethod
    def _query_string(params: dict[str, Any]) -> str:
        pairs = [
            (str(key), str(value))
            for key, value in sorted(params.items())
            if value is not None and str(value) != ""
        ]
        return urllib.parse.urlencode(pairs)

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any],
    ) -> list[Any]:
        if not path.startswith("/api/v5/"):
            raise ValueError("OKX 公开行情路径不在允许范围")
        query = self._query_string(params)
        url = self._base_url + path + (f"?{query}" if query else "")
        response: HttpResponse | None = None
        for attempt in range(OKX_PUBLIC_READ_ATTEMPTS):
            try:
                response = self._transport.request(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "PA_Agent/0.1",
                    },
                    body=None,
                    timeout=self._timeout,
                )
                break
            except OkxPublicTransportError:
                if attempt + 1 >= OKX_PUBLIC_READ_ATTEMPTS:
                    raise
                time.sleep(OKX_PUBLIC_RETRY_DELAYS_SECONDS[attempt])
        if response is None:
            raise OkxPublicTransportError("OKX 公开行情没有返回响应")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OkxPublicTransportError(
                f"OKX 返回非 JSON 响应（HTTP {response.status}）"
            ) from exc
        if not isinstance(payload, dict):
            raise OkxPublicTransportError("OKX 返回结构不是对象")
        code = str(payload.get("code", ""))
        if response.status < 200 or response.status >= 300 or code != "0":
            raise OkxPublicApiError(
                code or str(response.status),
                str(payload.get("msg") or "OKX 公开行情请求失败"),
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise OkxPublicTransportError("OKX data 字段不是数组")
        return data

    def public_instruments(
        self,
        inst_type: str,
        *,
        instrument: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._get(
            "/api/v5/public/instruments",
            params={"instType": inst_type, "instId": instrument},
        )
        if any(not isinstance(row, dict) for row in rows):
            raise OkxPublicTransportError("OKX 品种响应格式无效")
        return [dict(row) for row in rows]

    def tickers(self, inst_type: str) -> list[dict[str, Any]]:
        rows = self._get(
            "/api/v5/market/tickers",
            params={"instType": inst_type},
        )
        if any(not isinstance(row, dict) for row in rows):
            raise OkxPublicTransportError("OKX 报价响应格式无效")
        return [dict(row) for row in rows]

    def candles(
        self,
        *,
        instrument: str,
        bar: str,
        limit: int,
        after: str | None = None,
    ) -> list[list[str]]:
        clean_after: str | None = None
        if after is not None:
            clean_after = str(after).strip()
            if not clean_after.isdigit():
                raise OkxPublicTransportError(
                    "OKX K 线 after 游标必须是非负毫秒时间戳"
                )
        rows = self._get(
            "/api/v5/market/candles",
            params={
                "instId": instrument,
                "bar": bar,
                "limit": max(1, min(int(limit), 300)),
                "after": clean_after,
            },
        )
        normalized: list[list[str]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                raise OkxPublicTransportError("OKX K 线响应格式无效")
            normalized.append([str(value) for value in row[:9]])
        return normalized
