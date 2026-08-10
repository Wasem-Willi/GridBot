from __future__ import annotations

from dataclasses import dataclass

import requests

from gridbot.exchange import BinanceSpotClient
from gridbot.exchange import extract_binance_error_detail, is_insufficient_balance_error
from gridbot.grid_engine import GridPlan
from gridbot.state_store import StateStore


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool


class InsufficientFundsError(Exception):
    def __init__(self, symbol: str, details: str) -> None:
        super().__init__(f"Insufficient funds for {symbol}: {details}")
        self.symbol = symbol
        self.details = details


class OrderPlacementError(Exception):
    def __init__(self, symbol: str, details: str) -> None:
        super().__init__(f"Order placement failed for {symbol}: {details}")
        self.symbol = symbol
        self.details = details


def sync_grid_orders(
    exchange: BinanceSpotClient,
    store: StateStore,
    plan: GridPlan,
    execution_config: ExecutionConfig,
) -> None:
    if execution_config.dry_run:
        for level in plan.levels:
            synthetic_id = f"dry-{plan.symbol}-{level.side}-{level.price:.8f}"
            store.write_order(synthetic_id, plan.symbol, level.side, level.price, level.quantity, "PLANNED")
        return

    try:
        open_orders = exchange.get_open_orders(plan.symbol)
    except requests.HTTPError as error:
        details = extract_binance_error_detail(error)
        if is_insufficient_balance_error(error):
            raise InsufficientFundsError(plan.symbol, details) from error
        raise OrderPlacementError(plan.symbol, details) from error
    if open_orders:
        for order in open_orders:
            order_id = int(order["orderId"])
            try:
                exchange.cancel_order(plan.symbol, order_id)
            except requests.HTTPError as error:
                details = extract_binance_error_detail(error)
                if is_insufficient_balance_error(error):
                    raise InsufficientFundsError(plan.symbol, details) from error
                raise OrderPlacementError(plan.symbol, details) from error
            store.write_order(str(order_id), plan.symbol, order["side"], float(order["price"]), float(order["origQty"]), "CANCELED")

    placed_orders = 0
    skipped_levels = 0
    for level in plan.levels:
        normalized = exchange.normalize_limit_order(plan.symbol, level.price, level.quantity)
        if normalized is None:
            skipped_levels += 1
            continue
        normalized_price, normalized_quantity = normalized
        try:
            order = exchange.place_limit_order(plan.symbol, level.side, normalized_quantity, normalized_price, post_only=True)
        except requests.HTTPError as error:
            details = extract_binance_error_detail(error)
            if is_insufficient_balance_error(error):
                raise InsufficientFundsError(plan.symbol, details) from error
            raise OrderPlacementError(plan.symbol, details) from error

        order_price = float(order.get("price", normalized_price))
        order_qty = float(order.get("origQty", normalized_quantity))
        order_status = str(order.get("status", "NEW"))
        store.write_order(
            str(order["orderId"]),
            plan.symbol,
            level.side,
            order_price,
            order_qty,
            order_status,
        )
        placed_orders += 1

    if placed_orders == 0:
        raise OrderPlacementError(
            plan.symbol,
            f"All grid levels rejected by symbol filters (skipped_levels={skipped_levels})",
        )
