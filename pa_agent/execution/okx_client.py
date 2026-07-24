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

    def candles(
        self,
        *,
        instrument: str,
        bar: str,
        limit: int,
    ) -> list[list[str]]:
        payload = self._request(
            "GET",
            "/api/v5/market/candles",
            params={
                "instId": instrument,
                "bar": bar,
                "limit": max(1, min(int(limit), 300)),
            },
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
        payload = self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body={
                "instId": instrument,
                "lever": leverage,
                "mgnMode": margin_mode,
            },
            private=True,
        )
        data = self.require_item_success(payload)
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
