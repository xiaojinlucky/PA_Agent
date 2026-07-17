from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.config.settings import Settings
from pa_agent.execution.models import AccountSnapshot, PositionSnapshot
from pa_agent.gui.trading_dialog import TradingDialog


class EmptyStore:
    def list_recent(self, limit=30):
        return []

    def get(self, _execution_id):
        return None


class FakeService:
    is_armed = False
    store = EmptyStore()

    def latest_execution(self):
        return None


def test_dialog_round_trips_longbridge_route_without_credentials(qtbot):
    settings = Settings()
    dialog = TradingDialog(
        settings=settings,
        service=FakeService(),
    )
    qtbot.addWidget(dialog)

    dialog._enabled.setChecked(True)
    dialog._auto_execute.setChecked(True)
    dialog._broker.setCurrentIndex(dialog._broker.findData("longbridge"))
    dialog._lb_source.setText("GLD.US")
    dialog._lb_instrument.setText("GLD.US")
    dialog._lb_quantity.setText("10")
    dialog._lb_account.setCurrentIndex(
        dialog._lb_account.findData("intraday")
    )
    dialog._lb_fallback.setChecked(True)
    dialog._apply_widgets()

    assert settings.execution.enabled is True
    assert settings.execution.auto_execute is True
    assert settings.execution.longbridge.instrument == "GLD.US"
    assert settings.execution.longbridge.quantity == "10"
    assert settings.execution.longbridge.preferred_account == "intraday"
    assert settings.execution.longbridge.allow_comprehensive_fallback is True


def test_dialog_switches_okx_product_and_margin_controls(qtbot):
    settings = Settings()
    dialog = TradingDialog(settings=settings, service=FakeService())
    qtbot.addWidget(dialog)

    dialog._broker.setCurrentIndex(dialog._broker.findData("okx"))
    dialog._okx_product.setCurrentIndex(dialog._okx_product.findData("spot"))
    dialog._sync_okx_margin_enabled()
    assert dialog._route_stack.currentIndex() == 1
    assert dialog._okx_margin.isEnabled() is False

    dialog._okx_product.setCurrentIndex(dialog._okx_product.findData("swap"))
    dialog._sync_okx_margin_enabled()
    assert dialog._okx_margin.isEnabled() is True


def test_invalid_route_edit_does_not_partially_mutate_saved_settings(qtbot):
    settings = Settings()
    dialog = TradingDialog(settings=settings, service=FakeService())
    qtbot.addWidget(dialog)
    dialog._enabled.setChecked(True)
    dialog._okx_base_url.setText("http://insecure.example")

    with pytest.raises(ValueError, match="https"):
        dialog._apply_widgets()

    assert settings.execution.enabled is False
    assert settings.execution.okx.api_base_url == "https://www.okx.com"


def test_account_update_renders_and_clears_positions(qtbot):
    settings = Settings()
    dialog = TradingDialog(settings=settings, service=FakeService())
    qtbot.addWidget(dialog)
    snapshot = AccountSnapshot(
        broker="okx",
        account_profile="okx-live",
        base_currency="USDT",
        positions=[
            PositionSnapshot(
                instrument="XAUT",
                direction="long",
                quantity=Decimal("1.5"),
                available_quantity=Decimal("0.5"),
                currency="XAUT",
                raw={"kind": "spot_balance"},
            ),
            PositionSnapshot(
                instrument="BTC-USDT-SWAP",
                direction="short",
                quantity=Decimal("2"),
                unrealized_pnl=Decimal("3"),
                currency="USDT",
            ),
        ],
    )

    dialog._on_account_update(snapshot)

    assert dialog._positions_table.rowCount() == 2
    assert dialog._positions_table.item(0, 1).text() == "持币"
    assert dialog._positions_table.item(1, 1).text() == "空"

    dialog._on_account_update(
        snapshot.model_copy(update={"positions": []})
    )
    assert dialog._positions_table.rowCount() == 0
