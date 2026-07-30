from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import pytest

from pa_agent.execution.credentials import OkxCredentials
from pa_agent.execution.errors import BrokerApiError, BrokerTransportError
from pa_agent.execution.okx_client import (
    OKX_FIXED_PROXY_URL,
    OKX_PENDING_ALGO_ORDER_TYPES,
    OKX_READ_RECONNECT_DELAYS_SECONDS,
    OKX_READ_TRANSPORT_ATTEMPTS,
    OKX_REQUEST_TIMEOUT_SECONDS,
    HttpResponse,
    OkxRestClient,
    UrlLibTransport,
    okx_fixed_proxy_label,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(payload: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode("utf-8"))


def _client(transport, *, simulated=False):
    return OkxRestClient(
        OkxCredentials("api-key", "secret", "passphrase"),
        transport=transport,
        simulated=simulated,
        now_ms=lambda: 1_700_000_000_000,
    )


def _algo_rows(count: int, *, start: int = 0) -> list[dict[str, str]]:
    return [
        {
            "algoId": f"algo-{index}",
            "algoClOrdId": f"other-client-{index}",
        }
        for index in range(start, start + count)
    ]


def test_urllib_incomplete_read_is_normalized_as_read_only_transport_failure():
    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"{", 10)

    opener = Mock()
    opener.open.return_value = _Response()
    with pytest.raises(BrokerTransportError) as caught:
        UrlLibTransport(opener=opener).request(
            "GET",
            "https://www.okx.com/api/v5/account/bills",
            headers={},
            body=None,
            timeout=10,
        )

    assert caught.value.write_may_have_reached is False
    assert "IncompleteRead" in str(caught.value)
    assert "/api/v5/account/bills" in str(caught.value)


def test_urllib_transport_logs_only_endpoint_and_safe_nested_error_type():
    opener = Mock()
    opener.open.side_effect = urllib.error.URLError(TimeoutError())

    with pytest.raises(BrokerTransportError) as caught:
        UrlLibTransport(opener=opener).request(
            "GET",
            "https://www.okx.com/api/v5/account/bills?after=private-id",
            headers={},
            body=None,
            timeout=10,
        )

    message = str(caught.value)
    assert "GET /api/v5/account/bills" in message
    assert "URLError:TimeoutError" in message
    assert "private-id" not in message


def test_urllib_transport_binds_fixed_proxy_even_when_no_proxy_is_wildcard(
    monkeypatch,
):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:65530")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:65530")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.delenv("PA_AGENT_OKX_PROXY_URL", raising=False)
    opener = Mock()
    response = Mock()
    response.status = 200
    response.read.return_value = b"{}"
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener.open.return_value = response

    with patch(
        "pa_agent.execution.okx_client.urllib.request.build_opener",
        return_value=opener,
    ) as build_opener:
        transport = UrlLibTransport()
        transport.request(
            "GET",
            "https://www.okx.com/api/v5/public/time",
            headers={},
            body=None,
            timeout=10,
        )

    proxy_handler = build_opener.call_args.args[0]
    assert proxy_handler.proxies == {
        "http": OKX_FIXED_PROXY_URL,
        "https": OKX_FIXED_PROXY_URL,
    }
    assert transport.proxy_url == OKX_FIXED_PROXY_URL
    request = urllib.request.Request(
        "https://www.okx.com/api/v5/public/time"
    )
    with patch(
        "pa_agent.execution.okx_client.urllib.request.proxy_bypass",
        side_effect=AssertionError("不得读取系统 bypass"),
    ):
        proxy_handler.proxy_open(
            request,
            OKX_FIXED_PROXY_URL,
            "https",
        )
    assert request.host == "127.0.0.1:10981"
    assert request._tunnel_host == "www.okx.com"  # noqa: SLF001


@pytest.mark.parametrize(
    "proxy_url",
    (
        "https://proxy.example:8443",
        "http://192.0.2.1:8080",
        "http://user:pass@127.0.0.1:10981",
    ),
)
def test_urllib_transport_rejects_non_loopback_or_credential_proxy(proxy_url):
    with pytest.raises(ValueError, match="OKX 独立代理"):
        UrlLibTransport(proxy_url=proxy_url)


def test_fixed_proxy_label_reads_activated_runtime_metadata(
    tmp_path,
    monkeypatch,
):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "node_label": "新固定节点",
                "listen_host": "127.0.0.1",
                "listen_port": 10981,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pa_agent.execution.okx_client.OKX_FIXED_PROXY_METADATA_PATH",
        metadata_path,
    )
    monkeypatch.delenv("PA_AGENT_OKX_PROXY_LABEL", raising=False)

    assert okx_fixed_proxy_label() == "新固定节点"


def test_private_request_signature_uses_exact_path_query_and_compact_body():
    transport = FakeTransport(
        [_response({"code": "0", "data": [{"ordId": "1", "sCode": "0"}], "msg": ""})]
    )
    client = _client(transport)

    client.place_order(
        {
            "instId": "BTC-USDT",
            "ordType": "limit",
            "px": "100",
            "side": "buy",
            "sz": "1",
            "tdMode": "cash",
        }
    )

    call = transport.calls[0]
    timestamp = "2023-11-14T22:13:20.000Z"
    body = call["body"].decode("utf-8")
    expected = base64.b64encode(
        hmac.new(
            b"secret",
            (timestamp + "POST/api/v5/trade/order" + body).encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    assert call["headers"]["OK-ACCESS-SIGN"] == expected
    assert call["headers"]["OK-ACCESS-PASSPHRASE"] == "passphrase"
    assert call["timeout"] == OKX_REQUEST_TIMEOUT_SECONDS
    assert call["headers"]["expTime"] == "1700000005000"


def test_private_get_reconnects_with_fresh_signature(monkeypatch):
    transport = FakeTransport(
        [
            BrokerTransportError(
                "first tunnel failed",
                write_may_have_reached=False,
            ),
            _response({"code": "0", "data": [{"totalEq": "1"}], "msg": ""}),
        ]
    )
    times = iter(
        [
            1_700_000_000_000,
            1_700_000_000_000,
            1_700_000_001_000,
            1_700_000_001_000,
        ]
    )
    client = OkxRestClient(
        OkxCredentials("api-key", "secret", "passphrase"),
        transport=transport,
        now_ms=lambda: next(times),
    )
    delays: list[float] = []
    monkeypatch.setattr(
        "pa_agent.execution.okx_client.time.sleep",
        delays.append,
    )

    assert client.balance() == [{"totalEq": "1"}]
    assert len(transport.calls) == 2
    assert delays == [OKX_READ_RECONNECT_DELAYS_SECONDS[0]]
    assert (
        transport.calls[0]["headers"]["OK-ACCESS-TIMESTAMP"]
        != transport.calls[1]["headers"]["OK-ACCESS-TIMESTAMP"]
    )
    assert (
        transport.calls[0]["headers"]["OK-ACCESS-SIGN"]
        != transport.calls[1]["headers"]["OK-ACCESS-SIGN"]
    )


def test_get_stops_after_bounded_reconnects_and_raises_last_failure(
    monkeypatch,
):
    transport = FakeTransport(
        [
            BrokerTransportError(
                "first tunnel failed",
                write_may_have_reached=False,
            ),
            BrokerTransportError(
                "second tunnel failed",
                write_may_have_reached=False,
            ),
            BrokerTransportError(
                "third tunnel failed",
                write_may_have_reached=False,
            ),
        ]
    )
    delays: list[float] = []
    monkeypatch.setattr(
        "pa_agent.execution.okx_client.time.sleep",
        delays.append,
    )

    with pytest.raises(BrokerTransportError, match="third tunnel failed"):
        _client(transport).balance()

    assert len(transport.calls) == OKX_READ_TRANSPORT_ATTEMPTS
    assert delays == list(OKX_READ_RECONNECT_DELAYS_SECONDS)


def test_post_transport_failure_is_never_retried():
    transport = FakeTransport(
        [
            BrokerTransportError(
                "write outcome unknown",
                write_may_have_reached=True,
            ),
            _response(
                {
                    "code": "0",
                    "data": [{"ordId": "1", "sCode": "0"}],
                    "msg": "",
                }
            ),
        ]
    )

    with pytest.raises(BrokerTransportError, match="write outcome unknown"):
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert len(transport.calls) == 1


def test_simulated_header_is_explicit():
    transport = FakeTransport(
        [_response({"code": "0", "data": [{"ts": "1700000000000"}], "msg": ""})]
    )
    client = _client(transport, simulated=True)

    client.sync_server_time()

    assert transport.calls[0]["headers"]["x-simulated-trading"] == "1"
    assert "OK-ACCESS-KEY" not in transport.calls[0]["headers"]


def test_top_level_and_item_errors_are_both_rejected():
    top_transport = FakeTransport(
        [_response({"code": "50102", "data": [], "msg": "timestamp expired"})]
    )
    with pytest.raises(BrokerApiError) as top:
        _client(top_transport).balance()
    assert top.value.code == "50102"

    item_transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [{"ordId": "", "sCode": "51000", "sMsg": "bad order"}],
                    "msg": "",
                }
            )
        ]
    )
    with pytest.raises(BrokerApiError) as item:
        _client(item_transport).place_order({"instId": "BTC-USDT"})
    assert item.value.code == "51000"


def test_all_failed_trade_response_preserves_item_rejection_reason():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "1",
                    "data": [
                        {
                            "clOrdId": "private-client-order-id",
                            "ordId": "",
                            "sCode": "51008",
                            "sMsg": "insufficient available balance",
                        }
                    ],
                    "msg": "All operations failed",
                }
            )
        ]
    )

    with pytest.raises(BrokerApiError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.code == "51008"
    assert caught.value.message == "insufficient available balance"
    assert "private-client-order-id" not in str(caught.value)


@pytest.mark.parametrize(
    "top_code",
    [
        pytest.param("0", id="top-level-success"),
        pytest.param("1", id="top-level-all-failed"),
    ],
)
def test_item_rejection_redacts_order_identifiers_echoed_in_message(top_code):
    private_ids = (
        "private-regular-order-id",
        "private-regular-client-id",
        "private-algo-order-id",
        "private-algo-client-id",
    )
    transport = FakeTransport(
        [
            _response(
                {
                    "code": top_code,
                    "data": [
                        {
                            "ordId": private_ids[0],
                            "clOrdId": private_ids[1],
                            "algoId": private_ids[2],
                            "algoClOrdId": private_ids[3],
                            "sCode": "51008",
                            "sMsg": " ".join(("rejected", *private_ids)),
                        }
                    ],
                    "msg": "All operations failed" if top_code == "1" else "",
                }
            )
        ]
    )

    with pytest.raises(BrokerApiError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.code == "51008"
    assert "[订单标识已脱敏]" in caught.value.message
    assert all(private_id not in str(caught.value) for private_id in private_ids)


def test_whitespace_padded_success_code_still_requires_reconciliation():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "1",
                    "data": [{"sCode": " 0 ", "sMsg": ""}],
                    "msg": "All operations failed",
                }
            )
        ]
    )

    with pytest.raises(BrokerTransportError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.write_may_have_reached is True


def test_null_item_code_falls_back_to_top_level_rejection():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "1",
                    "data": [{"sCode": None, "sMsg": "unknown item result"}],
                    "msg": "All operations failed",
                }
            )
        ]
    )

    with pytest.raises(BrokerApiError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.code == "1"
    assert caught.value.message == "All operations failed"


def test_numeric_zero_item_code_still_requires_reconciliation():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "1",
                    "data": [{"sCode": 0, "sMsg": ""}],
                    "msg": "All operations failed",
                }
            )
        ]
    )

    with pytest.raises(BrokerTransportError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.write_may_have_reached is True


def test_top_level_error_redacts_identifier_known_only_from_request_body():
    private_id = "private-request-client-id"
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "1",
                    "data": [],
                    "msg": f"Order {private_id} rejected",
                }
            )
        ]
    )

    with pytest.raises(BrokerApiError) as caught:
        _client(transport).place_order(
            {"instId": "BTC-USDT", "clOrdId": private_id}
        )

    assert "[订单标识已脱敏]" in caught.value.message
    assert private_id not in str(caught.value)


@pytest.mark.parametrize(
    "top_code",
    [
        pytest.param("0", id="top-level-success"),
        pytest.param("1", id="top-level-all-failed"),
    ],
)
def test_item_error_redacts_identifier_known_only_from_request_body(top_code):
    private_id = "private-request-client-id"
    transport = FakeTransport(
        [
            _response(
                {
                    "code": top_code,
                    "data": [
                        {
                            "sCode": "51008",
                            "sMsg": f"Order {private_id} rejected",
                        }
                    ],
                    "msg": "All operations failed" if top_code == "1" else "",
                }
            )
        ]
    )

    with pytest.raises(BrokerApiError) as caught:
        _client(transport).place_order(
            {"instId": "BTC-USDT", "clOrdId": private_id}
        )

    assert "[订单标识已脱敏]" in caught.value.message
    assert private_id not in str(caught.value)


def test_read_error_redacts_identifier_known_only_from_query_params():
    private_id = "private-query-order-id"
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "1",
                    "data": [],
                    "msg": f"Order {private_id} was not found",
                }
            )
        ]
    )

    with pytest.raises(BrokerApiError) as caught:
        _client(transport).get_order(
            instrument="BTC-USDT",
            order_id=private_id,
        )

    assert "[订单标识已脱敏]" in caught.value.message
    assert private_id not in str(caught.value)


@pytest.mark.parametrize(
    ("method", "status", "payload", "expected_code"),
    [
        pytest.param(
            "POST",
            200,
            {
                "code": "1",
                "data": [
                    {"sCode": "51008", "sMsg": "same reason"},
                    {"sCode": "51008", "sMsg": "same reason"},
                ],
                "msg": "All operations failed",
            },
            "51008",
            id="same-item-reason",
        ),
        pytest.param(
            "POST",
            200,
            {
                "code": "1",
                "data": [
                    {"sCode": "51008", "sMsg": "first reason"},
                    {"sCode": "51009", "sMsg": "second reason"},
                ],
                "msg": "All operations failed",
            },
            "1",
            id="different-item-reasons",
        ),
        pytest.param(
            "POST",
            200,
            {"code": "1", "data": [], "msg": "All operations failed"},
            "1",
            id="empty-items",
        ),
        pytest.param(
            "GET",
            200,
            {"code": "2", "data": [], "msg": "read failed"},
            "2",
            id="read-code-two",
        ),
        pytest.param(
            "POST",
            400,
            {"code": "2", "data": [], "msg": "write rejected"},
            "2",
            id="http-four-hundred-write",
        ),
    ],
)
def test_error_response_scope_matrix(method, status, payload, expected_code):
    transport = FakeTransport([_response(payload, status=status)])

    with pytest.raises(BrokerApiError) as caught:
        if method == "GET":
            _client(transport).balance()
        else:
            _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.code == expected_code


def test_partially_successful_trade_response_requires_reconciliation():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "2",
                    "data": [
                        {
                            "ordId": "private-broker-order-id",
                            "sCode": "0",
                            "sMsg": "",
                        },
                        {
                            "ordId": "",
                            "sCode": "51008",
                            "sMsg": "insufficient available balance",
                        },
                    ],
                    "msg": "Partial success",
                }
            )
        ]
    )

    with pytest.raises(BrokerTransportError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.write_may_have_reached is True
    assert "部分成功" in str(caught.value)
    assert "private-broker-order-id" not in str(caught.value)


def test_partial_success_code_requires_reconciliation_when_items_are_incomplete():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "2",
                    "data": [],
                    "msg": "Partial success",
                }
            )
        ]
    )

    with pytest.raises(BrokerTransportError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.write_may_have_reached is True
    assert "部分成功" in str(caught.value)


@pytest.mark.parametrize(
    "items",
    [
        pytest.param(
            [
                {
                    "ordId": "private-broker-order-id",
                    "sCode": "0",
                    "sMsg": "",
                },
                {"unexpected": "shape"},
            ],
            id="success-before-malformed",
        ),
        pytest.param(
            [
                {"unexpected": "shape"},
                {
                    "ordId": "private-broker-order-id",
                    "sCode": "0",
                    "sMsg": "",
                },
            ],
            id="success-after-malformed",
        ),
    ],
)
def test_write_response_never_loses_success_evidence_from_malformed_peer(items):
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "1",
                    "data": items,
                    "msg": "All operations failed",
                }
            )
        ]
    )

    with pytest.raises(BrokerTransportError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.write_may_have_reached is True
    assert "部分成功" in str(caught.value)
    assert "private-broker-order-id" not in str(caught.value)


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_post_uncertain_http_status_is_never_treated_as_definite_rejection(
    status,
):
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "50011",
                    "data": [],
                    "msg": "temporarily unavailable",
                },
                status=status,
            )
        ]
    )

    with pytest.raises(BrokerTransportError) as caught:
        _client(transport).place_order({"instId": "BTC-USDT"})

    assert caught.value.write_may_have_reached is True


def test_query_is_sorted_and_signed_as_transmitted():
    transport = FakeTransport(
        [_response({"code": "0", "data": [{"maxBuy": "2"}], "msg": ""})]
    )
    client = _client(transport)

    client.max_order_size(
        instrument="BTC-USDT-SWAP",
        trade_mode="cross",
        price="100",
    )

    call = transport.calls[0]
    assert call["url"].endswith(
        "/api/v5/account/max-size?instId=BTC-USDT-SWAP&px=100&tdMode=cross"
    )


def test_public_instruments_does_not_send_private_headers():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [{"instId": "XAU-USDT-SWAP", "state": "live"}],
                    "msg": "",
                }
            )
        ]
    )
    client = _client(transport)

    rows = client.public_instruments("SWAP", instrument="XAU-USDT-SWAP")

    assert rows[0]["state"] == "live"
    assert "OK-ACCESS-KEY" not in transport.calls[0]["headers"]


def test_candles_use_public_endpoint_and_bounded_limit():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [
                        [
                            "1784304000000",
                            "4000",
                            "4010",
                            "3990",
                            "4005",
                            "10",
                            "0.01",
                            "40050",
                            "1",
                        ]
                    ],
                    "msg": "",
                }
            )
        ]
    )
    client = _client(transport, simulated=True)

    rows = client.candles(
        instrument="XAU-USDT-SWAP",
        bar="30m",
        limit=999,
    )

    assert rows[0][4] == "4005"
    call = transport.calls[0]
    assert call["url"].endswith(
        "/api/v5/market/candles"
        "?bar=30m&instId=XAU-USDT-SWAP&limit=300"
    )
    assert "OK-ACCESS-KEY" not in call["headers"]


def test_candles_forwards_after_cursor_only_when_provided():
    transport = FakeTransport(
        [
            _response({"code": "0", "data": [], "msg": ""}),
            _response({"code": "0", "data": [], "msg": ""}),
        ]
    )
    client = _client(transport)

    client.candles(instrument="BTC-USDT", bar="5m", limit=10)
    client.candles(
        instrument="BTC-USDT",
        bar="5m",
        limit=10,
        after="1700000000000",
    )

    assert transport.calls[0]["url"].endswith(
        "/api/v5/market/candles?bar=5m&instId=BTC-USDT&limit=10"
    )
    assert transport.calls[1]["url"].endswith(
        "/api/v5/market/candles"
        "?after=1700000000000&bar=5m&instId=BTC-USDT&limit=10"
    )


@pytest.mark.parametrize("after", ["", "-1", "12.5", "not-a-timestamp"])
def test_candles_rejects_invalid_after_cursor(after):
    with pytest.raises(BrokerTransportError, match="after 游标"):
        _client(FakeTransport([])).candles(
            instrument="BTC-USDT",
            bar="5m",
            limit=10,
            after=after,
        )


@pytest.mark.parametrize("inst_type", ["SPOT", "swap"])
def test_tickers_uses_public_endpoint_without_private_headers(inst_type):
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [{"instId": "BTC-USDT", "last": "100"}],
                    "msg": "",
                }
            )
        ]
    )

    rows = _client(transport, simulated=True).tickers(inst_type)

    assert rows == [{"instId": "BTC-USDT", "last": "100"}]
    call = transport.calls[0]
    assert call["url"].endswith(
        f"/api/v5/market/tickers?instType={inst_type.upper()}"
    )
    assert "OK-ACCESS-KEY" not in call["headers"]


def test_tickers_rejects_unsupported_instrument_type():
    with pytest.raises(BrokerTransportError, match="SPOT 或 SWAP"):
        _client(FakeTransport([])).tickers("FUTURES")


def test_leverage_info_uses_read_only_private_endpoint():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "mgnMode": "cross",
                            "lever": "5",
                        }
                    ],
                    "msg": "",
                }
            )
        ]
    )
    client = _client(transport)

    rows = client.leverage_info(
        instrument="BTC-USDT-SWAP",
        margin_mode="cross",
    )

    assert rows[0]["lever"] == "5"
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith(
        "/api/v5/account/leverage-info?instId=BTC-USDT-SWAP&mgnMode=cross"
    )


def test_max_order_size_can_evaluate_one_candidate_leverage():
    transport = FakeTransport(
        [_response({"code": "0", "data": [{"maxBuy": "120000"}], "msg": ""})]
    )

    row = _client(transport).max_order_size(
        instrument="XAU-USDT-SWAP",
        trade_mode="cross",
        price="4000",
        leverage="25",
    )

    assert row["maxBuy"] == "120000"
    assert transport.calls[0]["url"].endswith(
        "/api/v5/account/max-size"
        "?instId=XAU-USDT-SWAP&leverage=25&px=4000&tdMode=cross"
    )


def test_leverage_adjustment_info_uses_official_read_only_estimate_endpoint():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [
                        {
                            "estMaxAmt": "120000",
                            "existOrd": False,
                            "maxLever": "50",
                            "minLever": "0.01",
                        }
                    ],
                    "msg": "",
                }
            )
        ]
    )

    row = _client(transport).leverage_adjustment_info(
        instrument_type="SWAP",
        margin_mode="cross",
        leverage="25",
        instrument="XAU-USDT-SWAP",
    )

    assert row["maxLever"] == "50"
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith(
        "/api/v5/account/adjust-leverage-info"
        "?instId=XAU-USDT-SWAP&instType=SWAP&lever=25"
        "&mgnMode=cross&posSide=net"
    )


def test_set_leverage_uses_private_post_with_exact_cross_net_body():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [
                        {
                            "instId": "XAU-USDT-SWAP",
                            "lever": "25",
                            "mgnMode": "cross",
                            "posSide": "",
                            "sCode": "0",
                        }
                    ],
                    "msg": "",
                }
            )
        ]
    )

    row = _client(transport, simulated=True).set_leverage(
        instrument="XAU-USDT-SWAP",
        margin_mode="cross",
        leverage="25",
    )

    call = transport.calls[0]
    assert row["lever"] == "25"
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/v5/account/set-leverage")
    assert json.loads(call["body"]) == {
        "instId": "XAU-USDT-SWAP",
        "lever": "25",
        "mgnMode": "cross",
    }
    assert call["headers"]["x-simulated-trading"] == "1"


def test_set_leverage_accepts_official_success_item_without_scode():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [
                        {
                            "instId": "XAU-USDT-SWAP",
                            "lever": "25",
                            "mgnMode": "cross",
                            "posSide": "",
                        }
                    ],
                    "msg": "",
                }
            )
        ]
    )

    row = _client(transport, simulated=True).set_leverage(
        instrument="XAU-USDT-SWAP",
        margin_mode="cross",
        leverage="25",
    )

    assert row["lever"] == "25"


def test_pending_orders_use_official_current_orders_endpoint():
    transport = FakeTransport(
        [_response({"code": "0", "data": [], "msg": ""})]
    )

    rows = _client(transport, simulated=True).pending_orders(
        instrument="XAU-USDT-SWAP",
    )

    assert rows == []
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith(
        "/api/v5/trade/orders-pending?instId=XAU-USDT-SWAP"
    )


@pytest.mark.parametrize("order_type", OKX_PENDING_ALGO_ORDER_TYPES)
def test_pending_algo_orders_support_every_verified_official_type(order_type):
    transport = FakeTransport(
        [_response({"code": "0", "data": [], "msg": ""})]
    )

    rows = _client(transport, simulated=True).pending_algo_orders(
        instrument="XAU-USDT-SWAP",
        order_type=order_type,
    )

    assert rows == []
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith(
        "/api/v5/trade/orders-algo-pending"
        f"?instId=XAU-USDT-SWAP&ordType={order_type}"
    )


def test_pending_algo_orders_reject_unknown_type_without_querying_broker():
    transport = FakeTransport([])

    with pytest.raises(BrokerTransportError) as caught:
        _client(transport, simulated=True).pending_algo_orders(
            instrument="XAU-USDT-SWAP",
            order_type="future_unknown_algo",
        )

    assert caught.value.write_may_have_reached is False
    assert transport.calls == []


def test_account_bills_read_all_pages_with_bill_id_cursor():
    first_page = [
        {
            "billId": str(1000 - index),
            "type": "2",
            "subType": "1",
            "ccy": "USDT",
        }
        for index in range(100)
    ]
    second_page = [
        {
            "billId": "900",
            "type": "1",
            "subType": "11",
            "ccy": "USDT",
        }
    ]
    transport = FakeTransport(
        [
            _response({"code": "0", "data": first_page, "msg": ""}),
            _response({"code": "0", "data": second_page, "msg": ""}),
        ]
    )

    rows = _client(transport, simulated=True).account_bills(
        currency="USDT",
        begin_ms=100,
        end_ms=200,
    )

    assert len(rows) == 101
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith(
        "/api/v5/account/bills"
        "?begin=100&ccy=USDT&end=200&limit=100"
    )
    assert "after=901" in transport.calls[1]["url"]


def test_account_bills_duplicate_id_fails_closed():
    first_page = [
        {"billId": str(1000 - index)}
        for index in range(100)
    ]
    second_page = [{"billId": "901"}]
    transport = FakeTransport(
        [
            _response({"code": "0", "data": first_page, "msg": ""}),
            _response({"code": "0", "data": second_page, "msg": ""}),
        ]
    )

    with pytest.raises(BrokerTransportError) as exc:
        _client(transport, simulated=True).account_bills()

    assert exc.value.write_may_have_reached is False
    assert "ID 缺失或重复" in str(exc.value)


def test_account_bill_types_use_read_only_private_endpoint():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "0",
                    "data": [
                        {
                            "type": "1",
                            "typeDesc": "Transfer",
                            "subTypeDetails": [
                                {
                                    "subType": "11",
                                    "subTypeDesc": "Transfer in",
                                }
                            ],
                        }
                    ],
                    "msg": "",
                }
            )
        ]
    )

    rows = _client(transport, simulated=True).account_bill_types()

    assert rows[0]["type"] == "1"
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith("/api/v5/account/subtypes")


def test_algo_lookup_confirms_absence_only_after_all_read_queries_succeed():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "51603",
                    "data": [],
                    "msg": "Order does not exist",
                }
            ),
            _response({"code": "0", "data": [], "msg": ""}),
            _response({"code": "0", "data": [], "msg": ""}),
            _response({"code": "0", "data": [], "msg": ""}),
            _response({"code": "0", "data": [], "msg": ""}),
        ]
    )
    client = _client(transport, simulated=True)

    found = client.find_algo_order_by_client_id(
        client_algo_id="protection-client",
        order_type="oco",
        instrument="XAU-USDT-SWAP",
    )

    assert found is None
    assert [call["method"] for call in transport.calls] == ["GET"] * 5
    assert "/api/v5/trade/orders-algo-pending?" in transport.calls[1]["url"]
    assert all(
        "/api/v5/trade/orders-algo-history?" in call["url"]
        for call in transport.calls[2:]
    )


def test_algo_lookup_finds_match_on_second_pending_page():
    first_page = _algo_rows(100)
    target = {
        "algoId": "algo-target",
        "algoClOrdId": "protection-client",
        "state": "live",
    }
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "51603",
                    "data": [],
                    "msg": "Order does not exist",
                }
            ),
            _response({"code": "0", "data": first_page, "msg": ""}),
            _response({"code": "0", "data": [target], "msg": ""}),
        ]
    )

    found = _client(transport, simulated=True).find_algo_order_by_client_id(
        client_algo_id="protection-client",
        order_type="oco",
        instrument="XAU-USDT-SWAP",
    )

    assert found == target
    assert len(transport.calls) == 3
    assert "after=algo-99" in transport.calls[2]["url"]
    assert "limit=100" in transport.calls[2]["url"]


def test_algo_lookup_confirms_absence_after_more_than_one_hundred_rows():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "51603",
                    "data": [],
                    "msg": "Order does not exist",
                }
            ),
            _response({"code": "0", "data": _algo_rows(100), "msg": ""}),
            _response({"code": "0", "data": _algo_rows(1, start=100), "msg": ""}),
            _response({"code": "0", "data": [], "msg": ""}),
            _response({"code": "0", "data": [], "msg": ""}),
            _response({"code": "0", "data": [], "msg": ""}),
        ]
    )

    found = _client(transport, simulated=True).find_algo_order_by_client_id(
        client_algo_id="protection-client",
        order_type="oco",
        instrument="XAU-USDT-SWAP",
    )

    assert found is None
    assert len(transport.calls) == 6
    assert "after=algo-99" in transport.calls[2]["url"]


def test_algo_lookup_second_page_failure_never_confirms_absence():
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "51603",
                    "data": [],
                    "msg": "Order does not exist",
                }
            ),
            _response({"code": "0", "data": _algo_rows(100), "msg": ""}),
            _response(
                {
                    "code": "50011",
                    "data": [],
                    "msg": "temporarily unavailable",
                }
            ),
        ]
    )

    with pytest.raises(BrokerApiError) as caught:
        _client(transport, simulated=True).find_algo_order_by_client_id(
            client_algo_id="protection-client",
            order_type="oco",
            instrument="XAU-USDT-SWAP",
        )

    assert caught.value.code == "50011"
    assert len(transport.calls) == 3


def test_algo_lookup_repeated_cursor_never_confirms_absence():
    first_page = _algo_rows(100)
    repeated_page = _algo_rows(99, start=100) + [first_page[-1]]
    transport = FakeTransport(
        [
            _response(
                {
                    "code": "51603",
                    "data": [],
                    "msg": "Order does not exist",
                }
            ),
            _response({"code": "0", "data": first_page, "msg": ""}),
            _response({"code": "0", "data": repeated_page, "msg": ""}),
        ]
    )

    with pytest.raises(BrokerTransportError) as caught:
        _client(transport, simulated=True).find_algo_order_by_client_id(
            client_algo_id="protection-client",
            order_type="oco",
            instrument="XAU-USDT-SWAP",
        )

    assert caught.value.write_may_have_reached is False
    assert "游标缺失或重复" in str(caught.value)
    assert len(transport.calls) == 3
