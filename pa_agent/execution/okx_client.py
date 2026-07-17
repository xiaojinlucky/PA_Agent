"""Small OKX V5 REST client using the official signing contract."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pa_agent.execution.credentials import OkxCredentials
from pa_agent.execution.errors import BrokerApiError, BrokerTransportError


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


class UrlLibTransport:
    """urllib transport kept injectable for deterministic adapter tests."""

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
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(int(response.status), response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(int(exc.code), exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrokerTransportError(
                f"OKX 网络请求失败（{type(exc).__name__}）",
                write_may_have_reached=method.upper() != "GET",
            ) from exc


class OkxRestClient:
    """Authenticated/private and public OKX V5 requests with bounded timeouts."""

    def __init__(
        self,
        credentials: OkxCredentials,
        *,
        base_url: str = "https://www.okx.com",
        simulated: bool = False,
        transport: HttpTransport | None = None,
        timeout: float = 10.0,
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

        response = self._transport.request(
            method,
            self._base_url + request_path,
            headers=headers,
            body=body_bytes,
            timeout=self._timeout,
        )
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
            raise BrokerApiError(code or str(response.status), str(payload.get("msg") or "请求失败"))
        data = payload.get("data")
        if data is not None and not isinstance(data, list):
            raise BrokerTransportError(
                "OKX data 字段不是数组",
                write_may_have_reached=method != "GET",
            )
        return payload

    @staticmethod
    def require_item_success(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data") or []
        for item in data:
            if not isinstance(item, dict):
                raise BrokerTransportError(
                    "OKX data 项不是对象",
                    write_may_have_reached=True,
                )
            code = str(item.get("sCode", "0"))
            if code != "0":
                raise BrokerApiError(code, str(item.get("sMsg") or "订单请求失败"))
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

    def max_order_size(
        self,
        *,
        instrument: str,
        trade_mode: str,
        price: str | None = None,
    ) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/api/v5/account/max-size",
            params={"instId": instrument, "tdMode": trade_mode, "px": price},
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

    def balance(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v5/account/balance", private=True)
        return [dict(item) for item in payload.get("data") or []]

    def positions(self, *, instrument: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v5/account/positions",
            params={"instId": instrument},
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

    def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = self._request("POST", "/api/v5/trade/order", body=body, private=True)
        data = self.require_item_success(payload)
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
        payload = self._request(
            "POST",
            "/api/v5/trade/cancel-order",
            body={
                "instId": instrument,
                **({"ordId": order_id} if order_id else {"clOrdId": client_order_id}),
            },
            private=True,
        )
        data = self.require_item_success(payload)
        return dict(data[0]) if data else {}

    def place_algo_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/api/v5/trade/order-algo",
            body=body,
            private=True,
        )
        data = self.require_item_success(payload)
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

    def cancel_algo_orders(self, orders: list[dict[str, str]]) -> list[dict[str, Any]]:
        payload = self._request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=orders,
            private=True,
        )
        return self.require_item_success(payload)

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
