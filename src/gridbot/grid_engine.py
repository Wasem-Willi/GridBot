from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GridLevel:
    side: str
    price: float
    quantity: float


@dataclass(frozen=True)
class GridPlan:
    symbol: str
    center_price: float
    lower_bound: float
    upper_bound: float
    levels: list[GridLevel]


def build_grid(symbol: str, center_price: float, spacing_pct: float, levels: int, quote_capital: float) -> GridPlan:
    if center_price <= 0:
        raise ValueError("center_price must be positive")
    if levels <= 0:
        raise ValueError("levels must be positive")
    if spacing_pct <= 0:
        raise ValueError("spacing_pct must be positive")

    half = max(levels // 2, 1)
    per_order_quote = quote_capital / levels
    built_levels: list[GridLevel] = []
    for i in range(1, half + 1):
        buy_price = center_price * (1.0 - spacing_pct * i)
        buy_qty = per_order_quote / buy_price
        built_levels.append(GridLevel(side="BUY", price=buy_price, quantity=buy_qty))

    for i in range(1, half + 1):
        sell_price = center_price * (1.0 + spacing_pct * i)
        sell_qty = per_order_quote / sell_price
        built_levels.append(GridLevel(side="SELL", price=sell_price, quantity=sell_qty))

    lower = center_price * (1.0 - spacing_pct * half)
    upper = center_price * (1.0 + spacing_pct * half)
    return GridPlan(
        symbol=symbol,
        center_price=center_price,
        lower_bound=lower,
        upper_bound=upper,
        levels=built_levels,
    )


def is_outside_grid(price: float, plan: GridPlan) -> bool:
    return price < plan.lower_bound or price > plan.upper_bound
