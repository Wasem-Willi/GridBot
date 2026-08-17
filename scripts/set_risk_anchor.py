"""One-off tool to set a symbol's stop-loss/take-profit risk anchor to a
known real entry price, for coins already held before the anchor-price
fix was deployed. Run from the repo root so the relative .env/db_path
resolve the same way the bot itself resolves them:

    PYTHONPATH=src python scripts/set_risk_anchor.py BTCUSDT 61250.00
"""
from __future__ import annotations

import sys

from gridbot.config import load_config
from gridbot.state_store import StateStore


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: set_risk_anchor.py SYMBOL PRICE", file=sys.stderr)
        return 1

    symbol = sys.argv[1].strip().upper()
    try:
        price = float(sys.argv[2])
    except ValueError:
        print(f"PRICE must be a number, got: {sys.argv[2]!r}", file=sys.stderr)
        return 1
    if price <= 0:
        print("PRICE must be positive", file=sys.stderr)
        return 1

    cfg = load_config()
    store = StateStore(cfg.db_path, cfg.timezone_name)
    try:
        before = store.get_symbol_state(symbol)
        if before is None:
            print(f"No symbol_state row for {symbol} - nothing to update.", file=sys.stderr)
            return 1

        store.reset_risk_anchor(symbol, price)

        after = store.get_symbol_state(symbol)
        print(f"{symbol}: risk_anchor_price {before.risk_anchor_price} -> {after.risk_anchor_price}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
