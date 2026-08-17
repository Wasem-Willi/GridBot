from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SymbolState:
    symbol: str
    center_price: float
    lower_bound: float
    upper_bound: float
    paused: bool
    pause_reason: str | None
    updated_at: str
    risk_anchor_price: float


@dataclass(frozen=True)
class RiskEvent:
    event_type: str
    symbol: str | None
    details: str
    created_at: str


class StateStore:
    def __init__(self, db_path: Path, timezone_name: str) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.tz = ZoneInfo(timezone_name)
        self._init_schema()
        self._migrate_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS symbol_state (
                symbol TEXT PRIMARY KEY,
                center_price REAL NOT NULL,
                lower_bound REAL NOT NULL,
                upper_bound REAL NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0,
                pause_reason TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                symbol TEXT,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                day_key TEXT PRIMARY KEY,
                realized_pnl REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_key TEXT NOT NULL,
                equity_usdt REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def _migrate_schema(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(symbol_state)").fetchall()}
        if "risk_anchor_price" not in columns:
            self.conn.execute("ALTER TABLE symbol_state ADD COLUMN risk_anchor_price REAL")
            self.conn.execute("UPDATE symbol_state SET risk_anchor_price = center_price WHERE risk_anchor_price IS NULL")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert_symbol_state(
        self,
        symbol: str,
        center_price: float,
        lower_bound: float,
        upper_bound: float,
        paused: bool,
        pause_reason: str | None,
        risk_anchor_price: float | None = None,
    ) -> None:
        """risk_anchor_price defaults to the existing row's anchor (or
        center_price for a brand-new symbol) so grid recentering never
        resets the stop-loss/take-profit reference point. Pass it
        explicitly only to intentionally start a new risk anchor (e.g. a
        fresh position after the old one was liquidated)."""
        if risk_anchor_price is None:
            existing = self.get_symbol_state(symbol)
            risk_anchor_price = existing.risk_anchor_price if existing is not None else center_price
        now = datetime.now(self.tz).isoformat()
        self.conn.execute(
            """
            INSERT INTO symbol_state(symbol, center_price, lower_bound, upper_bound, paused, pause_reason, updated_at, risk_anchor_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                center_price=excluded.center_price,
                lower_bound=excluded.lower_bound,
                upper_bound=excluded.upper_bound,
                paused=excluded.paused,
                pause_reason=excluded.pause_reason,
                updated_at=excluded.updated_at,
                risk_anchor_price=excluded.risk_anchor_price
            """,
            (symbol, center_price, lower_bound, upper_bound, 1 if paused else 0, pause_reason, now, risk_anchor_price),
        )
        self.conn.commit()

    def get_symbol_state(self, symbol: str) -> SymbolState | None:
        row = self.conn.execute("SELECT * FROM symbol_state WHERE symbol = ?", (symbol,)).fetchone()
        if row is None:
            return None
        raw_anchor = row["risk_anchor_price"]
        return SymbolState(
            symbol=row["symbol"],
            center_price=float(row["center_price"]),
            lower_bound=float(row["lower_bound"]),
            upper_bound=float(row["upper_bound"]),
            paused=bool(row["paused"]),
            pause_reason=row["pause_reason"],
            updated_at=str(row["updated_at"]),
            risk_anchor_price=float(raw_anchor) if raw_anchor is not None else float(row["center_price"]),
        )

    def set_symbol_paused(self, symbol: str, paused: bool, reason: str | None) -> None:
        state = self.get_symbol_state(symbol)
        if state is None:
            return
        self.upsert_symbol_state(
            symbol=symbol,
            center_price=state.center_price,
            lower_bound=state.lower_bound,
            upper_bound=state.upper_bound,
            paused=paused,
            pause_reason=reason,
            risk_anchor_price=state.risk_anchor_price,
        )

    def reset_risk_anchor(self, symbol: str, anchor_price: float) -> None:
        """Start a fresh stop-loss/take-profit reference point, e.g. after a
        band liquidation flattens the position."""
        state = self.get_symbol_state(symbol)
        if state is None:
            return
        self.upsert_symbol_state(
            symbol=symbol,
            center_price=state.center_price,
            lower_bound=state.lower_bound,
            upper_bound=state.upper_bound,
            paused=state.paused,
            pause_reason=state.pause_reason,
            risk_anchor_price=anchor_price,
        )

    def write_order(
        self, order_id: str, symbol: str, side: str, price: float, quantity: float, status: str
    ) -> None:
        now = datetime.now(self.tz).isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO orders(order_id, symbol, side, price, quantity, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, symbol, side, price, quantity, status, now),
        )
        self.conn.commit()

    def log_risk_event(self, event_type: str, symbol: str | None, details: dict[str, str | float | int]) -> None:
        now = datetime.now(self.tz).isoformat()
        self.conn.execute(
            """
            INSERT INTO risk_events(event_type, symbol, details, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, symbol, json.dumps(details, sort_keys=True), now),
        )
        self.conn.commit()

    def get_day_key(self) -> str:
        return datetime.now(self.tz).strftime("%Y-%m-%d")

    def add_realized_pnl(self, amount: float) -> None:
        day_key = self.get_day_key()
        self.conn.execute(
            """
            INSERT INTO daily_pnl(day_key, realized_pnl) VALUES (?, ?)
            ON CONFLICT(day_key) DO UPDATE SET realized_pnl = realized_pnl + excluded.realized_pnl
            """,
            (day_key, amount),
        )
        self.conn.commit()

    def get_realized_pnl_today(self) -> float:
        day_key = self.get_day_key()
        row = self.conn.execute("SELECT realized_pnl FROM daily_pnl WHERE day_key = ?", (day_key,)).fetchone()
        if row is None:
            return 0.0
        return float(row["realized_pnl"])

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO bot_state(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def get_recent_risk_events(self, limit: int = 10) -> list[RiskEvent]:
        rows = self.conn.execute(
            """
            SELECT event_type, symbol, details, created_at
            FROM risk_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            RiskEvent(
                event_type=str(row["event_type"]),
                symbol=row["symbol"],
                details=str(row["details"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def get_risk_events_since(self, cutoff_iso: str, limit: int = 500) -> list[RiskEvent]:
        """All risk/transition events with created_at >= cutoff_iso (an
        isoformat() string in this store's configured timezone), newest
        first, capped at `limit` rows as a safety net against unbounded
        history. Used by the /ask AI assistant to see the full recent
        transition history instead of just the last N events."""
        rows = self.conn.execute(
            """
            SELECT event_type, symbol, details, created_at
            FROM risk_events
            WHERE created_at >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (cutoff_iso, limit),
        ).fetchall()
        return [
            RiskEvent(
                event_type=str(row["event_type"]),
                symbol=row["symbol"],
                details=str(row["details"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def record_equity_snapshot(self, equity_usdt: float) -> None:
        now = datetime.now(self.tz).isoformat()
        day_key = self.get_day_key()
        self.conn.execute(
            """
            INSERT INTO equity_snapshots(day_key, equity_usdt, created_at)
            VALUES (?, ?, ?)
            """,
            (day_key, equity_usdt, now),
        )
        self.conn.commit()

    def get_first_equity_for_day(self, day_key: str) -> float | None:
        row = self.conn.execute(
            """
            SELECT equity_usdt
            FROM equity_snapshots
            WHERE day_key = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (day_key,),
        ).fetchone()
        if row is None:
            return None
        return float(row["equity_usdt"])

    def reset_for_fresh_start(self) -> None:
        self.conn.execute("DELETE FROM orders")
        self.conn.execute("DELETE FROM symbol_state")
        self.conn.execute(
            """
            DELETE FROM bot_state
            WHERE key IN (
                'bot_halted',
                'reconciliation_clean_cycles',
                'reconciliation_resume_block',
                'reconciliation_local_snapshot'
            )
            """
        )
        self.conn.commit()
