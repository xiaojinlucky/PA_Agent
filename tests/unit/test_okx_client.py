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
