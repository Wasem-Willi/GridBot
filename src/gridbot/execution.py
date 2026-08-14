from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import requests

from gridbot.exchange import BinanceSpotClient
from gridbot.exchange import extract_binance_error_detail, is_insufficient_balance_error
from gridbot.grid_engine import GridLevel, GridPlan
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


def _free_balance(account: dict[str, Any], asset: str) -> Decimal:
    balances = account.get("balances")
    if not isinstance(balances, list):
        raise ValueError("Account response does not contain a balances list")
    for balance in balances:
        if isinstance(balance, dict) and balance.get("asset") == asset:
            return Decimal(str(balance.get("free", "0")))
    return Decimal("0")


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

    try:
        base_asset, quote_asset = exchange.get_symbol_assets(plan.symbol)
        account = exchange.get_account()
        remaining_base = _free_balance(account, base_asset)
        remaining_quote = _free_balance(account, quote_asset)
    except requests.HTTPError as error:
        details = extract_binance_error_detail(error)
        if is_insufficient_balance_error(error):
            raise InsufficientFundsError(plan.symbol, details) from error
        raise OrderPlacementError(plan.symbol, details) from error
    except ValueError as error:
        raise OrderPlacementError(plan.symbol, str(error)) from error

    # Normalize desired levels up front so they can be matched against orders
    # already resting on the book, instead of blindly cancelling and
    # re-placing everything every cycle.
    desired_levels: list[tuple[str, Decimal, Decimal, GridLevel]] = []
    skipped_levels = 0
    for level in plan.levels:
        normalized = exchange.normalize_limit_order(plan.symbol, level.price, level.quantity)
        if normalized is None:
            skipped_levels += 1
            continue
        normalized_price, normalized_quantity = normalized
        desired_levels.append(
            (level.side, Decimal(str(normalized_price)), Decimal(str(normalized_quantity)), level)
        )

    existing_by_key: dict[tuple[str, Decimal], dict[str, Any]] = {}
    for order in open_orders:
        key = (str(order["side"]), Decimal(str(order["price"])))
        existing_by_key.setdefault(key, order)

    kept_order_ids: set[int] = set()
    levels_to_place: list[tuple[str, Decimal, Decimal, GridLevel]] = []
    for side, price_dec, quantity_dec, level in desired_levels:
        existing = existing_by_key.get((side, price_dec))
        if existing is not None:
            kept_order_ids.add(int(existing["orderId"]))
            continue
        levels_to_place.append((side, price_dec, quantity_dec, level))

    # Cancel only orders that are no longer part of the desired grid (e.g.
    # the AI/regime filter dropped a side, or spacing changed).
    for order in open_orders:
        order_id = int(order["orderId"])
        if order_id in kept_order_ids:
            continue
        try:
            exchange.cancel_order(plan.symbol, order_id)
        except requests.HTTPError as error:
            details = extract_binance_error_detail(error)
            if is_insufficient_balance_error(error):
                raise InsufficientFundsError(plan.symbol, details) from error
            raise OrderPlacementError(plan.symbol, details) from error
        store.write_order(str(order_id), plan.symbol, order["side"], float(order["price"]), float(order["origQty"]), "CANCELED")

    placed_orders = 0
    balance_skipped_levels = 0
    for side, price_dec, quantity_dec, level in levels_to_place:
        if side == "BUY":
            required_quote = price_dec * quantity_dec
            if required_quote > remaining_quote:
                balance_skipped_levels += 1
                continue
        elif side == "SELL":
            if quantity_dec > remaining_base:
                balance_skipped_levels += 1
                continue
        normalized_price = float(price_dec)
        normalized_quantity = float(quantity_dec)
        try:
            order = exchange.place_limit_order(plan.symbol, side, normalized_quantity, normalized_price, post_only=True)
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
            side,
            order_price,
            order_qty,
            order_status,
        )
        placed_orders += 1
        if side == "BUY":
            remaining_quote -= price_dec * quantity_dec
        elif side == "SELL":
            remaining_base -= quantity_dec

    active_orders = len(kept_order_ids) + placed_orders
    if active_orders == 0:
        if balance_skipped_levels > 0:
            raise InsufficientFundsError(
                plan.symbol,
                f"no affordable levels (free {quote_asset}={remaining_quote}, free {base_asset}={remaining_base})",
            )
        raise OrderPlacementError(
            plan.symbol,
            f"All grid levels rejected by symbol filters (skipped_levels={skipped_levels})",
        )
    if balance_skipped_levels > 0:
        store.log_risk_event(
            "grid_levels_skipped_insufficient_balance",
            plan.symbol,
            {
                "skipped_levels": balance_skipped_levels,
                "placed_orders": placed_orders,
            },
        )
