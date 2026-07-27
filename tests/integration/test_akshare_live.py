"""Live AkShare smoke tests (network required). Baostock fallback disabled."""
from __future__ import annotations

import time

import pytest

from pa_agent.data.akshare_source import AkShareSource
from pa_agent.data.base import DataSourceTransientError

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _source() -> AkShareSource:
    s = AkShareSource()
    s.connect()
    assert s._baostock_ok is False, "tests must not use Baostock fallback"
    return s


@pytest.fixture(scope="module")
def akshare_available() -> None:
    """要求 akshare 已安装且东财行情端点真实可达。

    这些是明确标记 live 的联网冒烟测试。无外网、被代理拦截或端点限流时
    应当明确跳过（环境不满足），而不是报告为代码失败——但只跳过"连不上"
    这一类传输故障；一旦连接成功，数据本身的任何问题仍必须失败。
    """
    pytest.importorskip("akshare")


def _snapshot(source: AkShareSource, count: int):
    """拉取快照；仅在传输层不可达时跳过，数据问题仍照常失败。"""
    try:
        return source.latest_snapshot(count)
    except DataSourceTransientError as exc:
        message = str(exc)
        transport_markers = (
            "Max retries exceeded",
            "ProxyError",
            "ConnectionError",
            "Connection aborted",
            "timed out",
            "Timeout",
        )
        if any(marker in message for marker in transport_markers):
            pytest.skip(f"行情端点当前不可达，跳过联网冒烟测试：{message[:120]}")
        raise


def test_live_stock_1h(akshare_available: None) -> None:
    s = _source()
    s.subscribe("000001", "1h")
    bars = _snapshot(s, 20)
    assert len(bars) >= 10
    assert bars[0].close > 0
    assert bars[0].high >= bars[0].low


def test_live_stock_1d(akshare_available: None) -> None:
    time.sleep(2)
    s = _source()
    s.subscribe("600519", "1d")
    bars = _snapshot(s, 30)
    assert len(bars) >= 20
    assert all(b.high >= b.low for b in bars[:5])


def test_live_stock_4h(akshare_available: None) -> None:
    time.sleep(2)
    s = _source()
    s.subscribe("000001", "4h")
    bars = _snapshot(s, 15)
    assert len(bars) >= 5


def test_live_three_snapshots_stable(akshare_available: None) -> None:
    """Three consecutive fetches (simulates RefreshLoop) without fallback."""
    time.sleep(2)
    s = _source()
    s.subscribe("000001", "1h")
    for _ in range(3):
        bars = _snapshot(s, 10)
        assert len(bars) == 10
        time.sleep(1.2)
