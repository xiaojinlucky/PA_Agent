"""Small OKX V5 REST client using the official signing contract."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pa_agent.execution.credentials import OkxCredentials
from pa_agent.execution.errors import BrokerApiError, BrokerTransportError

OKX_PENDING_ALGO_ORDER_TYPES = (
    "conditional",
    "oco",
    "trigger",
    "move_order_stop",
    "iceberg",
    "twap",
    "chase",
    "smart_iceberg",
)
OKX_FIXED_PROXY_URL = "http://127.0.0.1:10981"
OKX_FIXED_PROXY_LABEL = "固定节点（身份未确认）"
OKX_REQUEST_TIMEOUT_SECONDS = 20.0
OKX_READ_TRANSPORT_ATTEMPTS = 3
OKX_READ_RECONNECT_DELAYS_SECONDS = (1.0, 2.0)
OKX_FIXED_PROXY_METADATA_PATH = (
    Path(__file__).resolve().parents[2]
    / "records"
    / "okx_fixed_proxy"
    / "metadata.json"
)
_OKX_PROXY_URL_ENV = "PA_AGENT_OKX_PROXY_URL"
_OKX_PROXY_LABEL_ENV = "PA_AGENT_OKX_PROXY_LABEL"
_OKX_ORDER_IDENTIFIER_FIELDS = (
    "ordId",
    "clOrdId",
    "algoId",
    "algoClOrdId",
)


def _okx_item_code(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    raw_code = item.get("sCode")
    return "" if raw_code is None else str(raw_code).strip()


def _redact_order_identifiers(message: object, *sources: object) -> str:
    text = str(message)
    identifiers: set[str] = set()
    for source in sources:
        items = source if isinstance(source, list) else [source]
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in _OKX_ORDER_IDENTIFIER_FIELDS:
                identifier = str(item.get(field) or "").strip()
                if identifier:
                    identifiers.add(identifier)
    for identifier in sorted(identifiers, key=len, reverse=True):
        text = text.replace(identifier, "[订单标识已脱敏]")
    return text


@dataclass(frozen=True)
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
    """绑定本机代理，但永不读取系统代理绕过规则。"""

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


class UrlLibTransport:
    """只通过 PA Agent 独立的本机 OKX 代理联网。"""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        opener: Any | None = None,
    ) -> None:
        self._proxy_url = self._validated_proxy_url(
            os.environ.get(_OKX_PROXY_URL_ENV, OKX_FIXED_PROXY_URL)
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
            raise ValueError(
                "OKX 独立代理必须是无凭据的本机 HTTP 地址"
            )
        return text

    @property
    def proxy_url(self) -> str:
        return self._proxy_url

    @staticmethod
    def _safe_failure_type(exc: BaseException) -> str:
        """只暴露异常类型，不把代理、主机或请求内容写入日志。"""

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
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        if request.type not in {"http", "https"}:
            raise ValueError("OKX 请求只允许 HTTP(S) 地址")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(int(response.status), response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(int(exc.code), exc.read())
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            endpoint = urllib.parse.urlsplit(url).path or "/"
            raise BrokerTransportError(
                f"OKX {method.upper()} {endpoint} 网络请求失败"
                f"（{self._safe_failure_type(exc)}）",
                write_may_have_reached=method.upper() != "GET",
            ) from exc


def okx_fixed_proxy_label() -> str:
    """返回 GUI 可展示的固定节点名称。"""
    environment_label = os.environ.get(_OKX_PROXY_LABEL_ENV, "").strip()
    if environment_label:
        return environment_label
    try:
        metadata = json.loads(
            OKX_FIXED_PROXY_METADATA_PATH.read_text(encoding="utf-8")
        )
        metadata_label = str(metadata.get("node_label") or "").strip()
        metadata_host = str(metadata.get("listen_host") or "").strip()
        metadata_port = int(metadata.get("listen_port"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return OKX_FIXED_PROXY_LABEL
    if (
        metadata_label
        and metadata_host in {"127.0.0.1", "localhost", "::1"}
        and metadata_port == urllib.parse.urlsplit(
            okx_fixed_proxy_url()
        ).port
    ):
        return metadata_label
    return OKX_FIXED_PROXY_LABEL


def okx_fixed_proxy_url() -> str:
    """返回经过同一安全约束校验的固定本机代理地址。"""
    return UrlLibTransport._validated_proxy_url(
        os.environ.get(_OKX_PROXY_URL_ENV, OKX_FIXED_PROXY_URL)
    )


class OkxRestClient:
    """Authenticated/private and public OKX V5 requests with bounded timeouts."""

    _ALGO_PAGE_LIMIT = 100
    _MAX_ALGO_PAGES = 100
    _BILL_PAGE_LIMIT = 100
    _MAX_BILL_PAGES = 100

    def __init__(
        self,
        credentials: OkxCredentials,
        *,
        base_url: str = "https://www.okx.com",
        simulated: bool = False,
        transport: HttpTransport | None = None,
        timeout: float = OKX_REQUEST_TIMEOUT_SECONDS,
        now_ms=None,
    ) -> None:
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")
        self._simulated = bool(simulated)
        self._transport = transport or UrlLibTransport()
        self._timeout = float(timeout)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._clock_offset_ms = 0

    @property
    def simulated(self) -> bool:
        return self._simulated

    @staticmethod
    def _compact_json(body: dict[str, Any] | list[Any] | None) -> str:
        if body is None:
            return ""
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _query_string(params: dict[str, Any] | None) -> str:
        if not params:
            return ""
        pairs: list[tuple[str, str]] = []
        for key in sorted(params):
            value = params[key]
            if value is None or value == "":
                continue
            rendered = ("true" if value else "false") if isinstance(value, bool) else str(value)
            pairs.append((str(key), rendered))
        return urllib.parse.urlencode(pairs, safe=",")

    def _corrected_now_ms(self) -> int:
        return int(self._now_ms()) + self._clock_offset_ms

    def _timestamp(self) -> str:
        dt = datetime.fromtimestamp(self._corrected_now_ms() / 1000, tz=UTC)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _signature(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}".encode()
        digest = hmac.new(
            self._credentials.secret_key.encode("utf-8"),
            message,
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        private: bool,
    ) -> dict[str, Any]:
        method = method.upper()
        query = self._query_string(params)
        request_path = path + (f"?{query}" if query else "")
        body_text = self._compact_json(body)
        body_bytes = body_text.encode("utf-8") if body_text else None
        attempts = OKX_READ_TRANSPORT_ATTEMPTS if method == "GET" else 1
        for attempt in range(attempts):
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "PA_Agent/0.1",
            }
            if private:
                timestamp = self._timestamp()
                headers.update(
                    {
                        "OK-ACCESS-KEY": self._credentials.api_key,
                        "OK-ACCESS-PASSPHRASE": self._credentials.passphrase,
                        "OK-ACCESS-TIMESTAMP": timestamp,
                        "OK-ACCESS-SIGN": self._signature(
                            timestamp,
                            method,
                            request_path,
                            body_text,
                        ),
                        "expTime": str(self._corrected_now_ms() + 5_000),
                    }
                )
            if self._simulated:
                headers["x-simulated-trading"] = "1"
            try:
                response = self._transport.request(
                    method,
                    self._base_url + request_path,
                    headers=headers,
                    body=body_bytes,
                    timeout=self._timeout,
                )
                break
            except BrokerTransportError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(OKX_READ_RECONNECT_DELAYS_SECONDS[attempt])
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BrokerTransportError(
                f"OKX 返回非 JSON 响应（HTTP {response.status}）",
                write_may_have_reached=method != "GET",
            ) from exc
        if not isinstance(payload, dict):
            raise BrokerTransportError(
                "OKX 返回结构不是对象",
                write_may_have_reached=method != "GET",
            )
        code = str(payload.get("code", ""))
        uncertain_write_status = (
            method != "GET"
            and (
                response.status in {408, 429}
                or response.status >= 500
            )
        )
        if uncertain_write_status:
            raise BrokerTransportError(
                f"OKX 写请求返回不确定 HTTP {response.status}",
                write_may_have_reached=True,
            )
        if response.status < 200 or response.status >= 300 or code != "0":
            if (
                method != "GET"
                and 200 <= response.status < 300
                and code == "2"
            ):
                raise BrokerTransportError(
                    "OKX 写响应报告部分成功，必须逐项对账",
                    write_may_have_reached=True,
                )
            data = payload.get("data")
            if (
                method != "GET"
                and 200 <= response.status < 300
                and isinstance(data, list)
                and data
            ):
                if any(_okx_item_code(item) == "0" for item in data):
                    raise BrokerTransportError(
                        "OKX 写响应包含部分成功项，必须逐项对账",
                        write_may_have_reached=True,
                    )
                item_results: list[tuple[str, str]] = []
                for item in data:
                    if not isinstance(item, dict):
                        item_results = []
                        break
                    item_code = _okx_item_code(item)
                    if not item_code:
                        item_results = []
                        break
                    item_results.append(
                        (
                            item_code,
                            _redact_order_identifiers(
                                item.get("sMsg") or "订单请求失败",
                                data,
                                body,
                                params,
                            ),
                        )
                    )
                failed_items = {
                    (item_code, item_message)
                    for item_code, item_message in item_results
                    if item_code and item_code != "0"
                }
                if code == "1" and len(failed_items) == 1:
                    item_code, item_message = next(iter(failed_items))
                    raise BrokerApiError(item_code, item_message)
            raise BrokerApiError(
                code or str(response.status),
                _redact_order_identifiers(
                    payload.get("msg") or "请求失败",
                    data if isinstance(data, list) else [],
                    body,
                    params,
                ),
            )
        data = payload.get("data")
        if data is not None and not isinstance(data, list):
            raise BrokerTransportError(
                "OKX data 字段不是数组",
                write_may_have_reached=method != "GET",
            )
        return payload

    @staticmethod
    def require_item_success(
        payload: dict[str, Any],
        *,
        request_context: object = None,
    ) -> list[dict[str, Any]]:
        data = payload.get("data") or []
        for item in data:
            if not isinstance(item, dict):
                raise BrokerTransportError(
                    "OKX data 项不是对象",
                    write_may_have_reached=True,
                )
            if "sCode" not in item:
                code = "0"
            else:
                code = _okx_item_code(item)
                if not code:
                    raise BrokerTransportError(
                        "OKX data 项缺少有效 sCode",
                        write_may_have_reached=True,
                    )
            if code != "0":
                raise BrokerApiError(
                    code,
                    _redact_order_identifiers(
                        item.get("sMsg") or "订单请求失败",
                        data,
                        request_context,
                    ),
                )
        return data

    def sync_server_time(self) -> int:
        before = int(self._now_ms())
        payload = self._request(
            "GET",
            "/api/v5/public/time",
            private=False,
        )
        after = int(self._now_ms())
        data = payload.get("data") or []
        if not data or not isinstance(data[0], dict) or not str(data[0].get("ts", "")).isdigit():
            raise BrokerTransportError(
                "OKX 服务器时间响应无效",
                write_may_have_reached=False,
            )
        midpoint = (before + after) // 2
        self._clock_offset_ms = int(data[0]["ts"]) - midpoint
        return self._clock_offset_ms

    def account_config(self) -> dict[str, Any]:
        payload = self._request("GET", "/api/v5/account/config", private=True)
        data = payload.get("data") or []
        return dict(data[0]) if data else {}

    def instruments(self, inst_type: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v5/account/instruments",
            params={"instType": inst_type},
            private=True,
        )
        return [dict(item) for item in payload.get("data") or []]

    def public_instruments(
        self,
        inst_type: str,
        *,
        instrument: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": inst_type, "instId": instrument},
            private=False,
        )
        return [dict(item) for item in payload.get("data") or []]

    def candles(
        self,
        *,
        instrument: str,
        bar: str,
        limit: int,
        after: str | None = None,
    ) -> list[list[str]]:
        params: dict[str, Any] = {
            "instId": instrument,
            "bar": bar,
            "limit": max(1, min(int(limit), 300)),
        }
        if after is not None:
            clean_after = str(after).strip()
            if not clean_after.isdigit():
                raise BrokerTransportError(
                    "OKX K 线 after 游标必须是非负毫秒时间戳",
                    write_may_have_reached=False,
                )
            params["after"] = clean_after
        payload = self._request(
            "GET",
            "/api/v5/market/candles",
            params=params,
            private=False,
        )
        rows: list[list[str]] = []
        for item in payload.get("data") or []:
            if not isinstance(item, list) or len(item) < 9:
                raise BrokerTransportError(
                    "OKX K 线响应格式无效",
                    write_may_have_reached=False,
                )
            rows.append([str(value) for value in item[:9]])
        return rows

    def max_order_size(
        self,
        *,
        instrument: str,
        trade_mode: str,
        price: str | None = None,
        leverage: str | None = None,
    ) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/api/v5/account/max-size",
            params={
                "instId": instrument,
                "tdMode": trade_mode,
                "px": price,
                "leverage": leverage,
            },
            private=True,
        )
        data = payload.get("data") or []
        return dict(data[0]) if data else {}

    def leverage_info(
        self,
        *,
        instrument: str,
        margin_mode: str,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v5/account/leverage-info",
            params={"instId": instrument, "mgnMode": margin_mode},
            private=True,
        )
        return [dict(item) for item in payload.get("data") or []]

    def leverage_adjustment_info(
        self,
        *,
        instrument_type: str,
        margin_mode: str,
        leverage: str,
        instrument: str,
        position_side: str = "net",
    ) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/api/v5/account/adjust-leverage-info",
            params={
                "instType": instrument_type,
                "mgnMode": margin_mode,
                "lever": leverage,
                "instId": instrument,
                "posSide": position_side,
            },
            private=True,
        )
        data = payload.get("data") or []
        return dict(data[0]) if data else {}

    def set_leverage(
        self,
        *,
        instrument: str,
        margin_mode: str,
        leverage: str,
    ) -> dict[str, Any]:
        request_body = {
            "instId": instrument,
            "lever": leverage,
            "mgnMode": margin_mode,
        }
        payload = self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body=request_body,
            private=True,
        )
        data = self.require_item_success(
            payload,
            request_context=request_body,
        )
        return dict(data[0]) if data else {}

    def balance(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v5/account/balance", private=True)
        return [dict(item) for item in payload.get("data") or []]

    def account_bill_types(self) -> list[dict[str, Any]]:
        """读取当前账户实际启用的账单类型映射。"""
        payload = self._request(
            "GET",
            "/api/v5/account/subtypes",
            private=True,
        )
        return [dict(item) for item in payload.get("data") or []]

    def account_bills(
        self,
        *,
        currency: str | None = None,
        begin_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """完整读取最近七天交易账户账单, 任何分页异常都失败关闭。"""
        rows: list[dict[str, Any]] = []
        seen_bill_ids: set[str] = set()
        after = ""
        for _page in range(self._MAX_BILL_PAGES):
            payload = self._request(
                "GET",
                "/api/v5/account/bills",
                params={
                    "ccy": currency,
                    "begin": begin_ms,
                    "end": end_ms,
                    "after": after,
                    "limit": self._BILL_PAGE_LIMIT,
                },
                private=True,
            )
            page = payload.get("data") or []
            if not page:
                return rows
            for item in page:
                if not isinstance(item, dict):
                    raise BrokerTransportError(
                        "OKX 资金账单项不是对象",
                        write_may_have_reached=False,
                    )
                bill_id = str(item.get("billId") or "").strip()
                if not bill_id or bill_id in seen_bill_ids:
                    raise BrokerTransportError(
                        "OKX 资金账单分页 ID 缺失或重复",
                        write_may_have_reached=False,
                    )
                seen_bill_ids.add(bill_id)
                rows.append(dict(item))
            if len(page) < self._BILL_PAGE_LIMIT:
                return rows
            next_after = str(page[-1].get("billId") or "").strip()
            if not next_after or next_after == after:
                raise BrokerTransportError(
                    "OKX 资金账单分页游标缺失或重复",
                    write_may_have_reached=False,
                )
            after = next_after
        raise BrokerTransportError(
            "OKX 资金账单分页超过安全上限",
            write_may_have_reached=False,
        )

    def positions(self, *, instrument: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v5/account/positions",
            params={"instId": instrument},
            private=True,
        )
        return [dict(item) for item in payload.get("data") or []]

    def pending_orders(
        self,
        *,
        instrument: str,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v5/trade/orders-pending",
            params={"instId": instrument},
            private=True,
        )
        return [dict(item) for item in payload.get("data") or []]

    def pending_algo_orders(
        self,
        *,
        instrument: str,
        order_type: str = "oco",
    ) -> list[dict[str, Any]]:
        clean_order_type = str(order_type).strip()
        if clean_order_type not in OKX_PENDING_ALGO_ORDER_TYPES:
            raise BrokerTransportError(
                "OKX 算法单类型不在当前已核验清单，禁止据此确认空挂单",
                write_may_have_reached=False,
            )
        payload = self._request(
            "GET",
            "/api/v5/trade/orders-algo-pending",
            params={"instId": instrument, "ordType": clean_order_type},
            private=True,
        )
        return [dict(item) for item in payload.get("data") or []]

    def ticker(self, instrument: str) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/api/v5/market/ticker",
            params={"instId": instrument},
            private=False,
        )
        data = payload.get("data") or []
        return dict(data[0]) if data else {}

    def tickers(self, inst_type: str) -> list[dict[str, Any]]:
        """读取一个产品类型的全量公共报价，不需要账户认证。"""

        clean_type = str(inst_type).strip().upper()
        if clean_type not in {"SPOT", "SWAP"}:
            raise BrokerTransportError(
                "PA 多市场看盘只允许读取 OKX SPOT 或 SWAP 全量报价",
                write_may_have_reached=False,
            )
        payload = self._request(
            "GET",
            "/api/v5/market/tickers",
            params={"instType": clean_type},
            private=False,
        )
        return [dict(item) for item in payload.get("data") or []]

    def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = self._request("POST", "/api/v5/trade/order", body=body, private=True)
        data = self.require_item_success(payload, request_context=body)
        return dict(data[0]) if data else {}

    def get_order(
        self,
        *,
        instrument: str,
        order_id: str = "",
        client_order_id: str = "",
    ) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/api/v5/trade/order",
            params={
                "instId": instrument,
                "ordId": order_id,
                "clOrdId": client_order_id if not order_id else "",
            },
            private=True,
        )
        data = payload.get("data") or []
        return dict(data[0]) if data else {}

    def cancel_order(
        self,
        *,
        instrument: str,
        order_id: str = "",
        client_order_id: str = "",
    ) -> dict[str, Any]:
        request_body = {
            "instId": instrument,
            **({"ordId": order_id} if order_id else {"clOrdId": client_order_id}),
        }
        payload = self._request(
            "POST",
            "/api/v5/trade/cancel-order",
            body=request_body,
            private=True,
        )
        data = self.require_item_success(
            payload,
            request_context=request_body,
        )
        return dict(data[0]) if data else {}

    def place_algo_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/api/v5/trade/order-algo",
            body=body,
            private=True,
        )
        data = self.require_item_success(payload, request_context=body)
        return dict(data[0]) if data else {}

    def get_algo_order(
        self,
        *,
        algo_id: str = "",
        client_algo_id: str = "",
    ) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/api/v5/trade/order-algo",
            params={
                "algoId": algo_id,
                "algoClOrdId": client_algo_id if not algo_id else "",
            },
            private=True,
        )
        data = payload.get("data") or []
        return dict(data[0]) if data else {}

    def find_algo_order_by_client_id(
        self,
        *,
        client_algo_id: str,
        order_type: str,
        instrument: str,
    ) -> dict[str, Any] | None:
        """查找算法单；全部权威只读查询成功后，None 才表示确认不存在。"""
        try:
            found = self.get_algo_order(client_algo_id=client_algo_id)
        except BrokerApiError as exc:
            if exc.code != "51603":
                raise
        else:
            if found:
                return found

        pending = self._find_algo_order_in_pages(
            path="/api/v5/trade/orders-algo-pending",
            params={"ordType": order_type, "instId": instrument},
            client_algo_id=client_algo_id,
        )
        if pending is not None:
            return pending

        for state in ("effective", "canceled", "order_failed"):
            history = self._find_algo_order_in_pages(
                path="/api/v5/trade/orders-algo-history",
                params={
                    "ordType": order_type,
                    "state": state,
                    "instId": instrument,
                },
                client_algo_id=client_algo_id,
            )
            if history is not None:
                return history
        return None

    def _find_algo_order_in_pages(
        self,
        *,
        path: str,
        params: dict[str, Any],
        client_algo_id: str,
    ) -> dict[str, Any] | None:
        """完整翻页查找算法单；任何分页异常都不能降级成“查无”。"""
        after = ""
        seen_cursors: set[str] = set()
        for _page_number in range(self._MAX_ALGO_PAGES):
            page_params = dict(params)
            page_params.update({"limit": self._ALGO_PAGE_LIMIT, "after": after})
            payload = self._request(
                "GET",
                path,
                params=page_params,
                private=True,
            )
            rows = payload.get("data") or []
            if len(rows) > self._ALGO_PAGE_LIMIT:
                raise BrokerTransportError(
                    "OKX 算法单分页返回数量超过官方上限",
                    write_may_have_reached=False,
                )
            for item in rows:
                if not isinstance(item, dict):
                    raise BrokerTransportError(
                        "OKX 算法单分页项目不是对象",
                        write_may_have_reached=False,
                    )
                if str(item.get("algoClOrdId") or "") == client_algo_id:
                    return dict(item)
            if len(rows) < self._ALGO_PAGE_LIMIT:
                return None

            cursor = str(rows[-1].get("algoId") or "").strip()
            if not cursor or cursor in seen_cursors:
                raise BrokerTransportError(
                    "OKX 算法单分页游标缺失或重复",
                    write_may_have_reached=False,
                )
            seen_cursors.add(cursor)
            after = cursor

        raise BrokerTransportError(
            "OKX 算法单分页超过安全上限，不能确认订单不存在",
            write_may_have_reached=False,
        )

    def cancel_algo_orders(self, orders: list[dict[str, str]]) -> list[dict[str, Any]]:
        payload = self._request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=orders,
            private=True,
        )
        return self.require_item_success(payload, request_context=orders)

    def fills(
        self,
        *,
        instrument_type: str | None = None,
        instrument: str | None = None,
        order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v5/trade/fills",
            params={
                "instType": instrument_type,
                "instId": instrument,
                "ordId": order_id,
            },
            private=True,
        )
        return [dict(item) for item in payload.get("data") or []]
