from __future__ import annotations

import unittest
from unittest.mock import Mock

import requests

from gridbot.main import _liquidate_symbol_position


def _cfg(notify_liquidation: bool = True) -> Mock:
    return Mock(notify_liquidation=notify_liquidation)


def _store() -> Mock:
    """A store mock with no persisted /notify_on|/notify_off override, so
    _notify_enabled falls back to the cfg default."""
    store = Mock()
    store.get_state.return_value = None
    return store


class LiquidateSymbolPositionTests(unittest.TestCase):
    def test_sells_full_free_base_balance_at_market(self) -> None:
        exchange = Mock()
        exchange.get_symbol_assets.return_value = ("BTC", "USDT")
        exchange.get_account.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.01", "locked": "0"},
                {"asset": "USDT", "free": "5.0", "locked": "0"},
            ]
        }
        exchange.normalize_market_sell_quantity.return_value = 0.01
        exchange.place_market_order.return_value = {
            "orderId": 99,
            "executedQty": "0.01",
            "price": "60000.00",
            "status": "FILLED",
        }
        store = _store()
        alerter = Mock()

        _liquidate_symbol_position(exchange, store, alerter, "BTCUSDT", 60000.0, "stop_loss", _cfg())

        exchange.place_market_order.assert_called_once_with("BTCUSDT", "SELL", 0.01)
        store.write_order.assert_called_once_with(
            "99", "BTCUSDT", "SELL", 60000.0, 0.01, "FILLED"
        )
        store.log_risk_event.assert_called_once_with(
            "band_liquidation",
            "BTCUSDT",
            {"band": "stop_loss", "quantity": 0.01, "price": 60000.0},
        )
        alerter.send.assert_called_once()

    def test_no_liquidation_alert_when_notify_liquidation_disabled(self) -> None:
        exchange = Mock()
        exchange.get_symbol_assets.return_value = ("BTC", "USDT")
        exchange.get_account.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.01", "locked": "0"},
                {"asset": "USDT", "free": "5.0", "locked": "0"},
            ]
        }
        exchange.normalize_market_sell_quantity.return_value = 0.01
        exchange.place_market_order.return_value = {
            "orderId": 99,
            "executedQty": "0.01",
            "price": "60000.00",
            "status": "FILLED",
        }
        store = _store()
        alerter = Mock()

        _liquidate_symbol_position(
            exchange, store, alerter, "BTCUSDT", 60000.0, "stop_loss", _cfg(notify_liquidation=False)
        )

        exchange.place_market_order.assert_called_once_with("BTCUSDT", "SELL", 0.01)
        alerter.send.assert_not_called()

    def test_no_position_does_nothing(self) -> None:
        exchange = Mock()
        exchange.get_symbol_assets.return_value = ("BTC", "USDT")
        exchange.get_account.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0", "locked": "0"},
                {"asset": "USDT", "free": "5.0", "locked": "0"},
            ]
        }
        store = _store()
        alerter = Mock()

        _liquidate_symbol_position(exchange, store, alerter, "BTCUSDT", 60000.0, "take_profit", _cfg())

        exchange.place_market_order.assert_not_called()
        store.write_order.assert_not_called()
        store.log_risk_event.assert_not_called()

    def test_dust_balance_is_skipped_not_sold(self) -> None:
        exchange = Mock()
        exchange.get_symbol_assets.return_value = ("BTC", "USDT")
        exchange.get_account.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.0000001", "locked": "0"},
                {"asset": "USDT", "free": "5.0", "locked": "0"},
            ]
        }
        exchange.normalize_market_sell_quantity.return_value = None
        store = _store()
        alerter = Mock()

        _liquidate_symbol_position(exchange, store, alerter, "BTCUSDT", 60000.0, "stop_loss", _cfg())

        exchange.place_market_order.assert_not_called()
        store.log_risk_event.assert_called_once_with(
            "band_liquidation_skipped_dust",
            "BTCUSDT",
            {"band": "stop_loss", "available": 0.0000001},
        )

    def test_market_sell_failure_logs_and_alerts_without_raising(self) -> None:
        exchange = Mock()
        exchange.get_symbol_assets.return_value = ("BTC", "USDT")
        exchange.get_account.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.01", "locked": "0"},
                {"asset": "USDT", "free": "5.0", "locked": "0"},
            ]
        }
        exchange.normalize_market_sell_quantity.return_value = 0.01
        error = requests.HTTPError(response=Mock(json=lambda: {"code": -1013, "msg": "Filter failure"}))
        exchange.place_market_order.side_effect = error
        store = _store()
        alerter = Mock()

        _liquidate_symbol_position(exchange, store, alerter, "BTCUSDT", 60000.0, "stop_loss", _cfg())

        store.write_order.assert_not_called()
        self.assertEqual(store.log_risk_event.call_args.args[0], "band_liquidation_error")
        alerter.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
