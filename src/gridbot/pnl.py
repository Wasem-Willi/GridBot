from __future__ import annotations

from dataclasses import dataclass

from gridbot.exchange import BinanceSpotClient
from gridbot.state_store import StateStore


@dataclass(frozen=True)
class PnlSnapshot:
    equity_usdt: float
    day_pnl_usdt: float
    day_pnl_pct: float
    since_start_pnl_usdt: float
    since_start_pnl_pct: float
    tracked_assets: int
    skipped_assets: int


def _pct_delta(current: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return ((current - baseline) / baseline) * 100.0


def compute_live_pnl_snapshot(exchange: BinanceSpotClient, store: StateStore) -> PnlSnapshot:
    account = exchange.get_account()
    balances_raw = account.get("balances")
    if not isinstance(balances_raw, list):
        raise ValueError("Account response missing balances list")

    price_rows = exchange.get_all_ticker_prices()
    prices = {str(row["symbol"]): float(row["price"]) for row in price_rows if "symbol" in row and "price" in row}

    equity_usdt = 0.0
    tracked_assets = 0
    skipped_assets = 0

    for row in balances_raw:
        asset = str(row.get("asset", "")).upper()
        free = float(row.get("free", 0.0))
        locked = float(row.get("locked", 0.0))
        quantity = free + locked
        if quantity <= 0.0:
            continue
        tracked_assets += 1
        if asset == "USDT":
            equity_usdt += quantity
            continue
        symbol = f"{asset}USDT"
        price = prices.get(symbol)
        if price is None:
            skipped_assets += 1
            continue
        equity_usdt += quantity * price

    store.record_equity_snapshot(equity_usdt)
    day_key = store.get_day_key()
    day_start_equity = store.get_first_equity_for_day(day_key)
    if day_start_equity is None:
        day_start_equity = equity_usdt

    startup_equity_raw = store.get_state("startup_equity_usdt")
    if startup_equity_raw is None:
        store.set_state("startup_equity_usdt", f"{equity_usdt:.12f}")
        startup_equity = equity_usdt
    else:
        startup_equity = float(startup_equity_raw)

    day_pnl_usdt = equity_usdt - day_start_equity
    since_start_pnl_usdt = equity_usdt - startup_equity

    return PnlSnapshot(
        equity_usdt=equity_usdt,
        day_pnl_usdt=day_pnl_usdt,
        day_pnl_pct=_pct_delta(equity_usdt, day_start_equity),
        since_start_pnl_usdt=since_start_pnl_usdt,
        since_start_pnl_pct=_pct_delta(equity_usdt, startup_equity),
        tracked_assets=tracked_assets,
        skipped_assets=skipped_assets,
    )
