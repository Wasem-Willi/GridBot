from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from gridbot.state_store import StateStore


def build_daily_summary(
    store: StateStore,
    timezone_name: str,
    active_symbols: list[str],
    bot_stopped: bool,
    pnl_status_line: str,
) -> str:
    now = datetime.now(ZoneInfo(timezone_name))
    pnl = store.get_realized_pnl_today()
    status = "STOPPED" if bot_stopped else "RUNNING"
    joined = ", ".join(active_symbols) if active_symbols else "none"
    return (
        f"[GridBot Daily] {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Status: {status}\n"
        f"Active symbols: {joined}\n"
        f"Realized PnL today (USDT): {pnl:.4f}\n"
        f"{pnl_status_line}"
    )
