from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pa_agent.data.okx_public_client import (
    HttpResponse,
    OkxPublicApiError,
    OkxPublicClient,
)


class _Transport:
    def __init__(self, payload: dict, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.calls: list[tuple[str, str, bytes | None]] = []

    def request(self, method, url, *, headers, body, timeout):
        del headers, timeout
        self.calls.append((method, url, body))
        return HttpResponse(
            status=self.status,
            body=json.dumps(self.payload).encode("utf-8"),
        )


def test_public_client_only_issues_get_requests() -> None:
    transport = _Transport(
        {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT",
                    "state": "live",
                    "tickSz": "0.01",
                }
            ],
        }
    )
    client = OkxPublicClient(transport=transport)

    rows = client.public_instruments(
        "SPOT",
        instrument="BTC-USDT",
    )

    assert rows[0]["instId"] == "BTC-USDT"
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][2] is None
    assert "instId=BTC-USDT" in transport.calls[0][1]


def test_public_client_rejects_provider_error() -> None:
    client = OkxPublicClient(
        transport=_Transport(
            {"code": "51000", "msg": "bad request", "data": []}
        )
    )

    with pytest.raises(OkxPublicApiError) as exc_info:
        client.tickers("SPOT")

    assert exc_info.value.code == "51000"


def test_new_market_read_path_has_no_execution_import() -> None:
    root = Path(__file__).parents[2]
    paths = (
        root / "pa_agent" / "data" / "market_workspace_runtime.py",
        root / "pa_agent" / "data" / "okx_public_client.py",
        root / "pa_agent" / "data" / "okx_source.py",
        root / "pa_agent" / "data" / "longbridge_source.py",
    )
    imported: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    assert not any(
        name == "pa_agent.execution"
        or name.startswith("pa_agent.execution.")
        for name in imported
    )
