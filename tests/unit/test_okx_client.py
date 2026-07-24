from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from pa_agent.execution.credentials import OkxCredentials
from pa_agent.execution.errors import BrokerApiError, BrokerTransportError
from pa_agent.execution.okx_client import HttpResponse, OkxRestClient


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
        return self.responses.pop(0)


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
    assert call["headers"]["expTime"] == "1700000005000"


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
