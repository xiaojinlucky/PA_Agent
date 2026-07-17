"""Thin, testable wrapper around the installed Longbridge Python SDK."""
from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pa_agent.execution.credentials import LongbridgeAccountCredentials
from pa_agent.execution.errors import BrokerApiError, BrokerTransportError


def _public_value(value: object) -> object:
    """Convert SDK values to JSON-safe primitives without credential data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


class LongbridgeSession:
    """One account's quote, trade, portfolio and raw HTTP contexts."""

    def __init__(self, credentials: LongbridgeAccountCredentials) -> None:
        try:
            from longbridge import openapi as sdk
        except ImportError as exc:
            raise BrokerTransportError(
                "未安装 Longbridge SDK",
                write_may_have_reached=False,
            ) from exc
        try:
            config = sdk.Config.from_apikey(
                credentials.app_key,
                credentials.app_secret,
                credentials.access_token,
                enable_print_quote_packages=False,
            )
            self._trade = sdk.TradeContext(config)
            self._quote = sdk.QuoteContext(config)
            self._portfolio = sdk.PortfolioContext(config)
            self._http = sdk.HttpClient.from_apikey(
                credentials.app_key,
                credentials.app_secret,
                credentials.access_token,
            )
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        self._sdk = sdk
        self._lock = threading.RLock()
        self._account_identity = credentials.account_identity

    @property
    def account_identity(self) -> str:
        return self._account_identity

    def _translate(self, exc: Exception, *, write: bool) -> Exception:
        sdk = getattr(self, "_sdk", None)
        api_error = getattr(sdk, "OpenApiException", None) if sdk is not None else None
        if api_error is not None and isinstance(exc, api_error):
            code = str(getattr(exc, "code", "") or "")
            message = str(getattr(exc, "message", "") or type(exc).__name__)
            kind = str(getattr(exc, "kind", "") or "")
            if code and code != "0":
                return BrokerApiError(code, message)
            return BrokerTransportError(
                f"Longbridge 请求失败（{kind or type(exc).__name__}）",
                write_may_have_reached=write,
            )
        return BrokerTransportError(
            f"Longbridge 请求失败（{type(exc).__name__}）",
            write_may_have_reached=write,
        )

    def static_info(self, symbol: str) -> dict[str, Any]:
        try:
            with self._lock:
                rows = self._quote.static_info([symbol])
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        if not rows:
            return {}
        row = rows[0]
        return {
            "symbol": str(getattr(row, "symbol", "") or ""),
            "lot_size": str(getattr(row, "lot_size", "") or ""),
            "currency": str(getattr(row, "currency", "") or ""),
            "name": str(
                getattr(row, "name_cn", "")
                or getattr(row, "name_en", "")
                or getattr(row, "name_hk", "")
                or ""
            ),
        }

    def estimate_max_quantity(
        self,
        *,
        symbol: str,
        side: str,
        price: Decimal,
    ) -> dict[str, object]:
        order_side = self._sdk.OrderSide.Buy if side == "buy" else self._sdk.OrderSide.Sell
        try:
            with self._lock:
                response = self._trade.estimate_max_purchase_quantity(
                    symbol=symbol,
                    order_type=self._sdk.OrderType.LO,
                    side=order_side,
                    price=price,
                    fractional_shares=False,
                )
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        return {
            "cash_max_qty": _public_value(getattr(response, "cash_max_qty", None)),
            "margin_max_qty": _public_value(
                getattr(response, "margin_max_qty", None)
            ),
        }

    def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        try:
            with self._lock:
                response = self._trade.stock_positions(
                    symbols=[symbol] if symbol else None
                )
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        positions: list[dict[str, Any]] = []
        for channel in getattr(response, "channels", []) or []:
            account_channel = str(getattr(channel, "account_channel", "") or "")
            for item in getattr(channel, "positions", []) or []:
                positions.append(
                    {
                        "account_channel": account_channel,
                        "symbol": str(getattr(item, "symbol", "") or ""),
                        "quantity": str(getattr(item, "quantity", "") or "0"),
                        "available_quantity": str(
                            getattr(item, "available_quantity", "") or "0"
                        ),
                        "cost_price": str(getattr(item, "cost_price", "") or ""),
                        "currency": str(getattr(item, "currency", "") or ""),
                        "market": str(getattr(item, "market", "") or ""),
                        "name": str(getattr(item, "symbol_name", "") or ""),
                    }
                )
        return positions

    def submit_order(self, body: dict[str, Any]) -> str:
        try:
            with self._lock:
                response = self._http.request(
                    "post",
                    "/v1/trade/order",
                    body=body,
                )
        except Exception as exc:
            raise self._translate(exc, write=True) from exc
        if not isinstance(response, dict):
            raise BrokerTransportError(
                "Longbridge 下单响应结构无效",
                write_may_have_reached=True,
            )
        code = str(response.get("code", "0"))
        if code not in {"", "0"}:
            raise BrokerApiError(code, str(response.get("message") or "下单失败"))
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        order_id = str(data.get("order_id") or data.get("orderId") or "")
        if not order_id:
            raise BrokerTransportError(
                "Longbridge 下单响应缺少 order_id",
                write_may_have_reached=True,
            )
        return order_id

    def cancel_order(self, order_id: str) -> None:
        try:
            with self._lock:
                self._trade.cancel_order(order_id)
        except Exception as exc:
            raise self._translate(exc, write=True) from exc

    def order(self, order_id: str) -> dict[str, Any]:
        try:
            with self._lock:
                detail = self._trade.order_detail(order_id)
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        status = getattr(detail, "status", None)
        sdk = self._sdk
        if status == sdk.OrderStatus.Filled:
            normalized = "filled"
        elif status == sdk.OrderStatus.PartialFilled:
            normalized = "partially_filled"
        elif status in {
            sdk.OrderStatus.Canceled,
            sdk.OrderStatus.Expired,
            sdk.OrderStatus.PartialWithdrawal,
        }:
            normalized = "canceled"
        elif status == sdk.OrderStatus.Rejected:
            normalized = "rejected"
        elif status == sdk.OrderStatus.Unknown:
            normalized = "unknown"
        else:
            normalized = "pending"
        executed_quantity = getattr(detail, "executed_quantity", None)
        if executed_quantity is None:
            executed_quantity = getattr(detail, "executed_qty", None)
        return {
            "order_id": str(getattr(detail, "order_id", "") or order_id),
            "state": normalized,
            "quantity": str(
                getattr(detail, "quantity", None)
                or getattr(detail, "submitted_quantity", None)
                or "0"
            ),
            "filled_quantity": (
                str(executed_quantity)
                if executed_quantity is not None
                else ""
            ),
            "average_fill_price": str(
                getattr(detail, "executed_price", None) or ""
            ),
            "price": str(getattr(detail, "price", None) or ""),
            "message": str(getattr(detail, "msg", "") or ""),
            "remark": str(getattr(detail, "remark", "") or ""),
            "side": str(getattr(detail, "side", "") or ""),
            "order_type": str(getattr(detail, "order_type", "") or ""),
        }

    def executions(
        self,
        *,
        symbol: str,
        order_id: str,
        start_at: datetime,
    ) -> list[dict[str, Any]]:
        """Return exact fills for one order across today's and historical APIs."""
        try:
            with self._lock:
                today = list(
                    self._trade.today_executions(
                        symbol=symbol,
                        order_id=order_id,
                    )
                    or []
                )
                history = [
                    item
                    for item in (
                        self._trade.history_executions(
                            symbol=symbol,
                            start_at=start_at,
                            end_at=datetime.now(UTC),
                        )
                        or []
                    )
                    if str(getattr(item, "order_id", "") or "") == order_id
                ]
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        deduplicated: dict[str, dict[str, Any]] = {}
        for index, item in enumerate([*today, *history]):
            trade_id = str(getattr(item, "trade_id", "") or "")
            key = trade_id or (
                f"{index}:"
                f"{getattr(item, 'quantity', '')}:"
                f"{getattr(item, 'price', '')}"
            )
            deduplicated[key] = {
                "order_id": str(getattr(item, "order_id", "") or ""),
                "trade_id": trade_id,
                "quantity": str(getattr(item, "quantity", "") or "0"),
                "price": str(getattr(item, "price", "") or ""),
            }
        return list(deduplicated.values())

    def find_today_order_by_remark(
        self,
        *,
        symbol: str,
        remark: str,
    ) -> dict[str, Any] | None:
        return self.find_order_by_remark(symbol=symbol, remark=remark)

    def find_order_by_remark(
        self,
        *,
        symbol: str,
        remark: str,
        start_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Find an exact execution remark across today and persisted history."""
        try:
            with self._lock:
                orders = self._trade.today_orders(symbol=symbol)
                order_id = next(
                    (
                        str(getattr(item, "order_id", "") or "")
                        for item in orders
                        if str(getattr(item, "remark", "") or "") == remark
                    ),
                    "",
                )
                if not order_id:
                    history = self._trade.history_orders(
                        symbol=symbol,
                        start_at=start_at,
                        end_at=datetime.now(UTC),
                    )
                    order_id = next(
                        (
                            str(getattr(item, "order_id", "") or "")
                            for item in history
                            if str(getattr(item, "remark", "") or "") == remark
                        ),
                        "",
                    )
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        return self.order(order_id) if order_id else None

    def current_price(self, symbol: str) -> Decimal:
        try:
            with self._lock:
                rows = self._quote.quote([symbol])
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        if not rows:
            raise BrokerTransportError(
                f"Longbridge 未返回 {symbol} 实时报价",
                write_may_have_reached=False,
            )
        try:
            return Decimal(str(rows[0].last_done))
        except Exception as exc:
            raise BrokerTransportError(
                f"Longbridge {symbol} 最新价无效",
                write_may_have_reached=False,
            ) from exc

    def account_balances(self) -> list[dict[str, Any]]:
        try:
            with self._lock:
                rows = self._trade.account_balance()
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        fields = (
            "currency",
            "total_cash",
            "max_finance_amount",
            "remaining_finance_amount",
            "risk_level",
            "margin_call",
            "net_assets",
            "buy_power",
            "init_margin",
            "maintenance_margin",
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = {
                field: _public_value(getattr(row, field, None))
                for field in fields
            }
            item["cash_infos"] = [
                {
                    field: _public_value(getattr(cash, field, None))
                    for field in (
                        "currency",
                        "available_cash",
                        "withdraw_cash",
                        "frozen_cash",
                        "settling_cash",
                    )
                }
                for cash in (getattr(row, "cash_infos", None) or [])
            ]
            result.append(item)
        return result

    def profit_summary(self) -> dict[str, Any]:
        try:
            with self._lock:
                analysis = self._portfolio.profit_analysis()
        except Exception as exc:
            raise self._translate(exc, write=False) from exc
        summary = getattr(analysis, "summary", None)
        if summary is None:
            return {}
        fields = (
            "currency",
            "current_total_asset",
            "ending_asset_value",
            "initial_asset_value",
            "invest_amount",
            "sum_profit",
            "sum_profit_rate",
        )
        return {
            field: _public_value(getattr(summary, field, None)) for field in fields
        }
