from __future__ import annotations

from dataclasses import dataclass

from gridbot.state_store import StateStore


@dataclass(frozen=True)
class DailyRiskResult:
    should_stop: bool
    realized_pnl: float
    threshold: float


def check_daily_loss_limit(store: StateStore, capital_usdt: float, daily_loss_limit_pct: float) -> DailyRiskResult:
    realized = store.get_realized_pnl_today()
    threshold = -abs(capital_usdt * daily_loss_limit_pct)
    return DailyRiskResult(should_stop=realized <= threshold, realized_pnl=realized, threshold=threshold)


def check_symbol_band(
    center_price: float,
    current_price: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> str | None:
    lower = center_price * (1.0 - stop_loss_pct)
    upper = center_price * (1.0 + take_profit_pct)
    if current_price <= lower:
        return "stop_loss"
    if current_price >= upper:
        return "take_profit"
    return None
