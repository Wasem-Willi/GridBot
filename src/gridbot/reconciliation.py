from __future__ import annotations

import json
from dataclasses import dataclass

from gridbot.exchange import BinanceSpotClient
from gridbot.state_store import StateStore

_LOCAL_SNAPSHOT_KEY = "reconciliation_local_snapshot"
_EPSILON = 1e-12


@dataclass(frozen=True)
class ReconciliationSnapshot:
    asset_quantities: dict[str, float]
    quote_free_usdt: float
    equity_usdt: float


@dataclass(frozen=True)
class ReconciliationResult:
    is_clean: bool
    reason: str
    max_qty_drift_pct: float
    quote_drift_usdt: float
    equity_drift_usdt: float


def _build_equity_usdt(
    asset_quantities: dict[str, float],
    quote_asset: str,
    prices_by_symbol: dict[str, float],
) -> float:
    equity_usdt = 0.0
    for asset, quantity in asset_quantities.items():
        if quantity <= 0:
            continue
        if asset == quote_asset:
            equity_usdt += quantity
            continue
        price = prices_by_symbol.get(f"{asset}{quote_asset}")
        if price is None:
            continue
        equity_usdt += quantity * price
    return equity_usdt


def build_exchange_snapshot(exchange: BinanceSpotClient, quote_asset: str = "USDT") -> ReconciliationSnapshot:
    account = exchange.get_account()
    balances_raw = account.get("balances")
    if not isinstance(balances_raw, list):
        raise ValueError("Account response missing balances list")

    asset_quantities: dict[str, float] = {}
    for row in balances_raw:
        asset = str(row.get("asset", "")).upper()
        free = float(row.get("free", 0.0))
        locked = float(row.get("locked", 0.0))
        quantity = free + locked
        if quantity <= 0:
            continue
        asset_quantities[asset] = quantity

    quote_free = 0.0
    for row in balances_raw:
        asset = str(row.get("asset", "")).upper()
        if asset != quote_asset:
            continue
        quote_free = float(row.get("free", 0.0))
        break

    prices_raw = exchange.get_all_ticker_prices()
    prices_by_symbol = {
        str(row["symbol"]): float(row["price"])
        for row in prices_raw
        if "symbol" in row and "price" in row
    }
    equity_usdt = _build_equity_usdt(asset_quantities, quote_asset, prices_by_symbol)

    return ReconciliationSnapshot(
        asset_quantities=asset_quantities,
        quote_free_usdt=quote_free,
        equity_usdt=equity_usdt,
    )


def load_local_snapshot(store: StateStore) -> ReconciliationSnapshot | None:
    raw = store.get_state(_LOCAL_SNAPSHOT_KEY)
    if raw is None:
        return None
    payload = json.loads(raw)
    return ReconciliationSnapshot(
        asset_quantities={str(k): float(v) for k, v in payload.get("asset_quantities", {}).items()},
        quote_free_usdt=float(payload.get("quote_free_usdt", 0.0)),
        equity_usdt=float(payload.get("equity_usdt", 0.0)),
    )


def save_local_snapshot(store: StateStore, snapshot: ReconciliationSnapshot) -> None:
    payload = {
        "asset_quantities": snapshot.asset_quantities,
        "quote_free_usdt": snapshot.quote_free_usdt,
        "equity_usdt": snapshot.equity_usdt,
    }
    store.set_state(_LOCAL_SNAPSHOT_KEY, json.dumps(payload, sort_keys=True))


def _compute_max_qty_drift_pct(local: dict[str, float], exchange: dict[str, float]) -> float:
    max_drift = 0.0
    assets = set(local.keys()) | set(exchange.keys())
    for asset in assets:
        local_qty = float(local.get(asset, 0.0))
        exchange_qty = float(exchange.get(asset, 0.0))
        denom = max(abs(exchange_qty), _EPSILON)
        drift_pct = abs(local_qty - exchange_qty) / denom
        if drift_pct > max_drift:
            max_drift = drift_pct
    return max_drift


def reconcile_snapshots(
    local: ReconciliationSnapshot | None,
    exchange: ReconciliationSnapshot,
    qty_drift_pct_threshold: float,
    equity_drift_usdt_threshold: float,
) -> ReconciliationResult:
    if local is None:
        return ReconciliationResult(
            is_clean=True,
            reason="bootstrap",
            max_qty_drift_pct=0.0,
            quote_drift_usdt=0.0,
            equity_drift_usdt=0.0,
        )

    max_qty_drift_pct = _compute_max_qty_drift_pct(local.asset_quantities, exchange.asset_quantities)
    quote_drift_usdt = abs(local.quote_free_usdt - exchange.quote_free_usdt)
    equity_drift_usdt = abs(local.equity_usdt - exchange.equity_usdt)

    qty_breach = max_qty_drift_pct > qty_drift_pct_threshold
    quote_breach = quote_drift_usdt > equity_drift_usdt_threshold
    equity_breach = equity_drift_usdt > equity_drift_usdt_threshold
    if qty_breach or quote_breach or equity_breach:
        parts: list[str] = []
        if qty_breach:
            parts.append(f"qty_drift_pct={max_qty_drift_pct:.6f}")
        if quote_breach:
            parts.append(f"quote_drift_usdt={quote_drift_usdt:.6f}")
        if equity_breach:
            parts.append(f"equity_drift_usdt={equity_drift_usdt:.6f}")
        return ReconciliationResult(
            is_clean=False,
            reason=", ".join(parts),
            max_qty_drift_pct=max_qty_drift_pct,
            quote_drift_usdt=quote_drift_usdt,
            equity_drift_usdt=equity_drift_usdt,
        )

    return ReconciliationResult(
        is_clean=True,
        reason="ok",
        max_qty_drift_pct=max_qty_drift_pct,
        quote_drift_usdt=quote_drift_usdt,
        equity_drift_usdt=equity_drift_usdt,
    )
