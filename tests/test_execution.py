from __future__ import annotations

import unittest
from unittest.mock import Mock

from gridbot.execution import ExecutionConfig, sync_grid_orders
from gridbot.grid_engine import GridLevel, GridPlan


class BalanceAwareExecutionTests(unittest.TestCase):
    def test_usdt_only_wallet_places_buys_and_skips_sells(self) -> None:
        exchange = Mock()
        exchange.get_open_orders.return_value = []
        exchange.get_symbol_assets.return_value = ("RVN", "USDT")
        exchange.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "100.0", "locked": "0"},
                {"asset": "RVN", "free": "0.0", "locked": "0"},
            ]
        }
        exchange.normalize_limit_order.side_effect = lambda _symbol, price, quantity: (price, quantity)
        exchange.place_limit_order.side_effect = [
            {"orderId": 1, "price": "0.0198", "origQty": "500", "status": "NEW"},
            {"orderId": 2, "price": "0.0196", "origQty": "500", "status": "NEW"},
        ]
        store = Mock()
        plan = GridPlan(
            symbol="RVNUSDT",
            center_price=0.02,
            lower_bound=0.0196,
            upper_bound=0.0204,
            levels=[
                GridLevel(side="BUY", price=0.0198, quantity=500.0),
                GridLevel(side="BUY", price=0.0196, quantity=500.0),
                GridLevel(side="SELL", price=0.0202, quantity=500.0),
                GridLevel(side="SELL", price=0.0204, quantity=500.0),
            ],
        )

        sync_grid_orders(exchange, store, plan, ExecutionConfig(dry_run=False))

        placed_sides = [call.args[1] for call in exchange.place_limit_order.call_args_list]
        self.assertEqual(placed_sides, ["BUY", "BUY"])

    def test_buy_orders_do_not_exceed_free_usdt(self) -> None:
        exchange = Mock()
        exchange.get_open_orders.return_value = []
        exchange.get_symbol_assets.return_value = ("RVN", "USDT")
        exchange.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "15.0", "locked": "0"},
                {"asset": "RVN", "free": "0.0", "locked": "0"},
            ]
        }
        exchange.normalize_limit_order.side_effect = lambda _symbol, price, quantity: (price, quantity)
        exchange.place_limit_order.return_value = {
            "orderId": 1,
            "price": "0.02",
            "origQty": "500",
            "status": "NEW",
        }
        store = Mock()
        plan = GridPlan(
            symbol="RVNUSDT",
            center_price=0.02,
            lower_bound=0.0196,
            upper_bound=0.0204,
            levels=[
                GridLevel(side="BUY", price=0.02, quantity=500.0),
                GridLevel(side="BUY", price=0.02, quantity=500.0),
            ],
        )

        sync_grid_orders(exchange, store, plan, ExecutionConfig(dry_run=False))

        self.assertEqual(exchange.place_limit_order.call_count, 1)
        store.log_risk_event.assert_called_once_with(
            "grid_levels_skipped_insufficient_balance",
            "RVNUSDT",
            {"skipped_levels": 1, "placed_orders": 1},
        )


if __name__ == "__main__":
    unittest.main()
