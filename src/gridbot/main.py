from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from gridbot.alerts import TelegramAlerter
from gridbot.ai_filter import (
    AI_ACTION_BOTH,
    AI_ACTION_BUY_ONLY,
    AI_ACTION_PAUSE,
    AI_ACTION_SELL_ONLY,
    OpenAIDecisionClient,
    ask_freeform,
)
from gridbot.config import BotConfig, load_config
from gridbot.exchange import (
    BinanceSpotClient,
    extract_binance_error_detail,
    free_balance,
    is_unknown_order_error,
)
from gridbot.execution import ExecutionConfig, InsufficientFundsError, OrderPlacementError, sync_grid_orders
from gridbot.grid_engine import GridLevel, GridPlan, build_grid, is_outside_grid
from gridbot.pnl import compute_live_pnl_snapshot
from gridbot.reconciliation import (
    ReconciliationResult,
    build_exchange_snapshot,
    load_local_snapshot,
    reconcile_snapshots,
    save_local_snapshot,
)
from gridbot.regime import (
    REGIME_RANGING,
    REGIME_TRENDING,
    InsufficientDataError,
    RegimeThresholds,
    classify_regime,
)
from gridbot.reporting import build_daily_summary
from gridbot.risk_guard import check_daily_loss_limit, check_symbol_band
from gridbot.selector import select_symbols
from gridbot.state_store import StateStore


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


NOTIFICATION_CATEGORIES: dict[str, str] = {
    "ai_decisions": "AI filter action changes",
    "regime": "Regime pause/resume",
    "liquidation": "Stop-loss/take-profit + liquidation",
    "order_errors": "Order placement / cancel-all failures",
    "risk_halts": "Daily loss limit + reconciliation halts",
    "symbol_refresh": "Active symbol rotation announcements",
    "daily_summary": "Daily summary report",
}


def _notify_enabled(store: StateStore, cfg: BotConfig, category: str) -> bool:
    """Live-toggleable notification check. A Telegram /notify_on|/notify_off
    override (persisted in the state store) always wins; otherwise falls
    back to the NOTIFY_* env var default from BotConfig."""
    override = store.get_state(f"notify_{category}")
    if override is not None:
        return override == "1"
    return bool(getattr(cfg, f"notify_{category}"))


def _build_notify_status_text(store: StateStore, cfg: BotConfig) -> str:
    lines = ["GridBot live notification toggles:"]
    for category, label in NOTIFICATION_CATEGORIES.items():
        state = "ON" if _notify_enabled(store, cfg, category) else "OFF"
        lines.append(f"[{state}] {category} - {label}")
    lines.append("")
    lines.append("Use /notify_on <category> or /notify_off <category> to change.")
    lines.append("Use 'all' as the category to toggle every category at once.")
    return "\n".join(lines)


def _load_blacklist(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"Blacklist file not found: {path}")
    values = {line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    return values


def _apply_control_commands(
    alerter: TelegramAlerter,
    store: StateStore,
    cfg: BotConfig,
    bot_halted: bool,
    pnl_provider: Callable[[], str],
    cancel_all_provider: Callable[[], str],
    start_fresh_provider: Callable[[], tuple[bool, str]],
    ai_ask_provider: Callable[[str, str], str],
) -> tuple[bool, bool]:
    offset_raw = store.get_state("telegram_offset")
    offset = int(offset_raw) if offset_raw is not None else 0
    commands, next_offset = alerter.poll_commands(offset)
    if next_offset != offset:
        store.set_state("telegram_offset", str(next_offset))
    should_stop = False
    for command in commands:
        if command.name in {"status", "pnl"}:
            try:
                pnl_text = pnl_provider()
            except (requests.RequestException, ValueError) as error:
                logging.warning("P/L snapshot failed while handling command %s: %s", command.name, error)
                pnl_text = "P/L update unavailable right now (exchange/API error)."
        else:
            pnl_text = ""
        if command.name == "kill":
            bot_halted = True
            alerter.send("GridBot: manual kill switch activated.")
            store.log_risk_event("manual_kill", None, {"source": "telegram"})
        elif command.name == "resume":
            bot_halted = False
            alerter.send("GridBot: manual resume acknowledged.")
            store.log_risk_event("manual_resume", None, {"source": "telegram"})
        elif command.name == "status":
            status = "STOPPED" if bot_halted else "RUNNING"
            alerter.send(f"GridBot status: {status}\n{pnl_text}")
        elif command.name == "pnl":
            alerter.send(pnl_text)
        elif command.name == "help":
            alerter.send(_build_help_text())
        elif command.name == "stop":
            bot_halted = True
            should_stop = True
            alerter.send("GridBot: stop command received. Shutting down process.")
            store.log_risk_event("manual_stop", None, {"source": "telegram"})
        elif command.name == "transitions":
            alerter.send(_build_transitions_text(store))
        elif command.name == "cancel_all":
            try:
                cancel_message = cancel_all_provider()
            except (requests.RequestException, ValueError) as error:
                cancel_message = f"Cancel all failed: {error}"
            alerter.send(cancel_message)
            store.log_risk_event("manual_cancel_all", None, {"source": "telegram", "result": cancel_message})
        elif command.name == "start_fresh":
            try:
                success, fresh_message = start_fresh_provider()
            except (requests.RequestException, ValueError) as error:
                success = False
                fresh_message = f"Start fresh failed: {error}"
            if success:
                bot_halted = False
                store.set_state("bot_halted", "0")
            alerter.send(fresh_message)
            store.log_risk_event(
                "manual_start_fresh",
                None,
                {"source": "telegram", "success": 1 if success else 0, "result": fresh_message},
            )
        elif command.name == "notify_status":
            alerter.send(_build_notify_status_text(store, cfg))
        elif command.name in {"notify_on", "notify_off"}:
            category = (command.arg or "").strip().lower()
            enabled = command.name == "notify_on"
            state_word = "ON" if enabled else "OFF"
            if category == "all":
                for name in NOTIFICATION_CATEGORIES:
                    store.set_state(f"notify_{name}", "1" if enabled else "0")
                alerter.send(f"All notification categories turned {state_word}.")
                store.log_risk_event(
                    "notify_toggle",
                    None,
                    {"category": "all", "enabled": 1 if enabled else 0, "source": "telegram"},
                )
            elif category not in NOTIFICATION_CATEGORIES:
                valid = ", ".join([*NOTIFICATION_CATEGORIES, "all"])
                alerter.send(f"Unknown notify category '{category}'. Valid: {valid}")
            else:
                store.set_state(f"notify_{category}", "1" if enabled else "0")
                alerter.send(f"Notifications for '{category}' turned {state_word}.")
                store.log_risk_event(
                    "notify_toggle",
                    None,
                    {"category": category, "enabled": 1 if enabled else 0, "source": "telegram"},
                )
        elif command.name == "ask":
            question = (command.arg or "").strip()
            if not question:
                alerter.send("Usage: /ask <question>")
            else:
                context = _build_ai_ask_context(store, bot_halted, pnl_provider)
                try:
                    answer = ai_ask_provider(question, context)
                    success = True
                except (requests.RequestException, ValueError) as error:
                    answer = f"AI chat failed: {error}"
                    success = False
                alerter.send(answer)
                store.log_risk_event(
                    "ai_chat",
                    None,
                    {"question": question, "success": 1 if success else 0, "source": "telegram"},
                )
    return bot_halted, should_stop


def _refresh_symbols(
    cfg: BotConfig,
    exchange: BinanceSpotClient,
    blacklist: set[str],
    alerter: TelegramAlerter,
    store: StateStore,
) -> list[str]:
    logging.info("Refreshing symbol shortlist...")
    ranked = select_symbols(exchange, blacklist, cfg.max_active_symbols)
    symbols = [r.symbol for r in ranked]
    logging.info("Selected symbols: %s", ", ".join(symbols) if symbols else "none")
    if _notify_enabled(store, cfg, "symbol_refresh"):
        alerter.send("GridBot symbol refresh: " + (", ".join(symbols) if symbols else "none"))
    return symbols


def _handle_insufficient_funds(
    store: StateStore,
    _alerter: TelegramAlerter,
    symbol: str,
    error: InsufficientFundsError,
) -> None:
    store.set_symbol_paused(symbol, True, "insufficient_funds")
    store.log_risk_event(
        "insufficient_funds",
        symbol,
        {"reason": "insufficient_funds", "details": error.details},
    )


def _cancel_all_orders_ignoring_missing(exchange: BinanceSpotClient, symbol: str) -> None:
    """Cancel all open orders, tolerating Binance's -2011 'Unknown order
    sent' response, which just means there was nothing left to cancel
    (already filled or removed by a prior cycle)."""
    try:
        exchange.cancel_all_orders(symbol)
    except requests.HTTPError as error:
        if is_unknown_order_error(error):
            return
        raise


def _liquidate_symbol_position(
    exchange: BinanceSpotClient,
    store: StateStore,
    alerter: TelegramAlerter,
    symbol: str,
    price: float,
    band_trigger: str,
    cfg: BotConfig,
) -> None:
    """Sell any held base-asset balance at market after a stop_loss/take_profit
    band trigger, so the band actually closes the position instead of just
    stopping new grid orders. Best-effort: logs and alerts on failure rather
    than raising, since the symbol is already paused either way."""
    try:
        base_asset, _quote_asset = exchange.get_symbol_assets(symbol)
        account = exchange.get_account()
        available = free_balance(account, base_asset)
    except (requests.HTTPError, ValueError) as error:
        details = extract_binance_error_detail(error) if isinstance(error, requests.HTTPError) else str(error)
        store.log_risk_event(
            "band_liquidation_error",
            symbol,
            {"band": band_trigger, "details": details, "stage": "balance_lookup"},
        )
        if _notify_enabled(store, cfg, "liquidation"):
            alerter.send(f"Liquidation lookup failed for {symbol} ({band_trigger}): {details}")
        return

    if available <= 0:
        return

    normalized_qty = exchange.normalize_market_sell_quantity(symbol, price, float(available))
    if normalized_qty is None:
        store.log_risk_event(
            "band_liquidation_skipped_dust",
            symbol,
            {"band": band_trigger, "available": float(available)},
        )
        return

    try:
        order = exchange.place_market_order(symbol, "SELL", normalized_qty)
    except requests.HTTPError as error:
        details = extract_binance_error_detail(error)
        store.log_risk_event(
            "band_liquidation_error",
            symbol,
            {"band": band_trigger, "details": details, "stage": "market_sell"},
        )
        if _notify_enabled(store, cfg, "liquidation"):
            alerter.send(f"Liquidation SELL failed for {symbol} ({band_trigger}): {details}")
        return

    order_qty = float(order.get("executedQty", normalized_qty))
    order_price = float(order.get("price") or price)
    store.write_order(
        str(order["orderId"]),
        symbol,
        "SELL",
        order_price,
        order_qty,
        str(order.get("status", "FILLED")),
    )
    store.log_risk_event(
        "band_liquidation",
        symbol,
        {"band": band_trigger, "quantity": order_qty, "price": price},
    )
    if _notify_enabled(store, cfg, "liquidation"):
        alerter.send(f"Liquidated {symbol}: sold {order_qty} (market) after {band_trigger} at ~{price:.8f}")


def _handle_order_placement_error(
    store: StateStore,
    alerter: TelegramAlerter,
    symbol: str,
    error: OrderPlacementError,
    cfg: BotConfig,
) -> None:
    store.set_symbol_paused(symbol, True, "order_placement_error")
    store.log_risk_event("order_placement_error", symbol, {"details": error.details})
    if _notify_enabled(store, cfg, "order_errors"):
        alerter.send(f"Symbol paused: {symbol}, reason=order_placement_error, details={error.details}")


def _responsive_wait(
    wait_seconds: int,
    poll_seconds: int,
    alerter: TelegramAlerter,
    store: StateStore,
    cfg: BotConfig,
    bot_halted: bool,
    pnl_provider: Callable[[], str],
    cancel_all_provider: Callable[[], str],
    start_fresh_provider: Callable[[], tuple[bool, str]],
    ai_ask_provider: Callable[[str, str], str],
) -> tuple[bool, bool]:
    step = max(poll_seconds, 1)
    while remaining > 0:
        sleep_for = min(step, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for
        bot_halted, should_stop = _apply_control_commands(
            alerter,
            store,
            cfg,
            bot_halted,
            pnl_provider,
            cancel_all_provider,
            start_fresh_provider,
            ai_ask_provider,
        )
        store.set_state("bot_halted", "1" if bot_halted else "0")
        if should_stop:
            return bot_halted, True
    return bot_halted, False


def _format_signed(value: float) -> str:
    return f"{value:+.4f}"


def _make_pnl_provider(cfg: BotConfig, exchange: BinanceSpotClient, store: StateStore) -> Callable[[], str]:
    if cfg.mode == "paper":
        return lambda: "P/L: paper mode (simulated orders, no account equity)."

    def _provider() -> str:
        snapshot = compute_live_pnl_snapshot(exchange, store)
        message = (
            f"P/L update ({cfg.mode}):\n"
            f"Equity (USDT): {snapshot.equity_usdt:.4f}\n"
            f"Day P/L: {_format_signed(snapshot.day_pnl_usdt)} USDT ({snapshot.day_pnl_pct:+.2f}%)\n"
            f"Since start P/L: {_format_signed(snapshot.since_start_pnl_usdt)} USDT ({snapshot.since_start_pnl_pct:+.2f}%)\n"
            f"Tracked assets: {snapshot.tracked_assets}, Skipped assets: {snapshot.skipped_assets}"
        )
        return message

    return _provider


def _make_cancel_all_provider(cfg: BotConfig, exchange: BinanceSpotClient) -> Callable[[], str]:
    if cfg.mode == "paper":
        return lambda: "Cancel all: paper mode (no real open orders to cancel)."

    def _provider() -> str:
        open_orders = exchange.get_all_open_orders()
        if not open_orders:
            return "Cancel all: no open orders found."
        symbols = sorted({str(order.get("symbol", "")) for order in open_orders if order.get("symbol")})
        canceled_total = 0
        failures: list[str] = []
        for symbol in symbols:
            try:
                canceled = exchange.cancel_all_orders(symbol)
                canceled_total += len(canceled)
            except requests.HTTPError as error:
                failures.append(f"{symbol} ({extract_binance_error_detail(error)})")
        summary = (
            f"Cancel all result: symbols={len(symbols)}, open_orders_before={len(open_orders)}, "
            f"canceled={canceled_total}"
        )
        if failures:
            summary += "\nFailures: " + "; ".join(failures)
        return summary

    return _provider


def _make_start_fresh_provider(
    cfg: BotConfig,
    exchange: BinanceSpotClient,
    store: StateStore,
) -> Callable[[], tuple[bool, str]]:
    def _provider() -> tuple[bool, str]:
        if cfg.mode == "paper":
            store.reset_for_fresh_start()
            return True, "Start fresh completed: paper mode state was reset."

        open_orders = exchange.get_all_open_orders()
        symbols = sorted({str(order.get("symbol", "")) for order in open_orders if order.get("symbol")})
        canceled_total = 0
        failures: list[str] = []
        for symbol in symbols:
            try:
                canceled = exchange.cancel_all_orders(symbol)
                canceled_total += len(canceled)
            except requests.HTTPError as error:
                failures.append(f"{symbol} ({extract_binance_error_detail(error)})")

        if failures:
            failure_text = "; ".join(failures)
            return (
                False,
                "Start fresh aborted: cancel all failed for one or more symbols. "
                f"Local state was not reset. Failures: {failure_text}",
            )

        store.reset_for_fresh_start()
        return (
            True,
            f"Start fresh completed: canceled_open_orders={canceled_total}, symbols={len(symbols)}, local_state_reset=true.",
        )

    return _provider


_AI_CHAT_SYSTEM_PROMPT = (
    "You are GridBot's AI assistant, reachable by the bot operator via Telegram /ask. "
    "GridBot is a rule-based Binance spot grid trading bot. Each question is accompanied by "
    "a 'Bot context' block containing the live bot status, a P/L snapshot, and the most "
    "recent risk/transition events, all pulled directly from the bot at the moment of the "
    "question - use that data to answer status/P&L/event questions accurately instead of "
    "guessing. If the operator asks about something not covered by the context (e.g. "
    "specific open orders or per-symbol grid levels), say so plainly and suggest /status, "
    "/pnl, or /transitions for more detail rather than inventing an answer. Answer clearly "
    "and concisely."
)


def _build_ai_ask_context(
    store: StateStore,
    bot_halted: bool,
    pnl_provider: Callable[[], str],
) -> str:
    """Snapshot of live bot data handed to the AI assistant alongside the
    operator's /ask question, so answers are grounded in the bot's actual
    current state rather than guessed."""
    try:
        pnl_text = pnl_provider()
    except (requests.RequestException, ValueError) as error:
        pnl_text = f"P/L snapshot unavailable ({error})"
    status = "STOPPED" if bot_halted else "RUNNING"
    return (
        f"Bot status: {status}\n"
        f"{pnl_text}\n\n"
        f"{_build_transitions_text(store)}"
    )


def _make_ai_ask_provider(cfg: BotConfig) -> Callable[[str, str], str]:
    if not cfg.ai_api_key:
        return lambda _question, _context: "AI chat is not configured: set OPENAI_API_KEY in .env to enable /ask."

    def _provider(question: str, context: str) -> str:
        return ask_freeform(
            cfg.ai_api_key,
            cfg.ai_model,
            cfg.ai_chat_timeout_seconds,
            _AI_CHAT_SYSTEM_PROMPT,
            question,
            context=context,
        )

    return _provider


def _build_help_text() -> str:
    return (
        "GridBot commands:\n"
        "/help - show this command list\n"
        "/status - bot status and latest P/L snapshot\n"
        "/pnl - on-demand P/L snapshot\n"
        "/transitions - latest transition events\n"
        "/cancel_all - cancel all open spot orders\n"
        "/start_fresh - cancel all open orders, reset local state, and restart fresh\n"
        "/kill - halt trading loop\n"
        "/resume - resume trading\n"
        "/stop - stop bot process\n"
        "/notify - show live notification toggle status\n"
        "/notify_on <category|all> - turn a notification category (or all) on\n"
        "/notify_off <category|all> - turn a notification category (or all) off\n"
        "/ask <question> - ask the AI assistant a question"
    )


def _build_transitions_text(store: StateStore, limit: int = 10) -> str:
    events = store.get_recent_risk_events(limit=limit)
    if not events:
        return "No transition events recorded yet."
    lines = ["Latest transitions:"]
    for event in events:
        symbol = event.symbol if event.symbol is not None else "GLOBAL"
        lines.append(f"{event.created_at} | {event.event_type} | {symbol} | {event.details}")
    return "\n".join(lines)


def _should_retry_insufficient_funds(state_updated_at: str, now: datetime, retry_minutes: int) -> bool:
    try:
        updated_at = datetime.fromisoformat(state_updated_at)
    except ValueError:
        return True
    return now - updated_at >= timedelta(minutes=retry_minutes)


def _build_reconciliation_status(result: ReconciliationResult) -> str:
    if result.is_clean:
        return (
            "Reconciliation clean: "
            f"qty_drift_pct={result.max_qty_drift_pct:.6f}, "
            f"quote_drift_usdt={result.quote_drift_usdt:.6f}, "
            f"equity_drift_usdt={result.equity_drift_usdt:.6f}"
        )
    return f"Reconciliation breach: {result.reason}"


def _run_reconciliation_check(cfg: BotConfig, exchange: BinanceSpotClient, store: StateStore) -> ReconciliationResult:
    exchange_snapshot = build_exchange_snapshot(exchange)
    local_snapshot = load_local_snapshot(store)
    result = reconcile_snapshots(
        local_snapshot,
        exchange_snapshot,
        qty_drift_pct_threshold=cfg.reconciliation_qty_drift_pct,
        equity_drift_usdt_threshold=cfg.reconciliation_equity_drift_usdt,
    )
    save_local_snapshot(store, exchange_snapshot)
    return result


def _run_startup_safety_gate(
    cfg: BotConfig,
    exchange: BinanceSpotClient,
    store: StateStore,
    symbols: list[str],
) -> None:
    if cfg.dry_run or not cfg.reconciliation_enabled:
        return

    result = _run_reconciliation_check(cfg, exchange, store)
    if not result.is_clean and result.reason != "bootstrap":
        raise RuntimeError(f"Startup reconciliation failed: {_build_reconciliation_status(result)}")

    for symbol in symbols:
        price = exchange.get_ticker_price(symbol)
        state = store.get_symbol_state(symbol)
        center = state.center_price if state is not None else price
        build_grid(symbol, center, cfg.grid_spacing_pct, cfg.grid_levels, cfg.per_symbol_capital)


def _load_reconciliation_gate_state(
    store: StateStore,
    clean_cycles_key: str,
    resume_block_key: str,
) -> tuple[int, bool]:
    clean_cycles_raw = store.get_state(clean_cycles_key)
    clean_cycles = int(clean_cycles_raw) if clean_cycles_raw is not None else 0
    resume_block_active = store.get_state(resume_block_key) == "1"
    return clean_cycles, resume_block_active


class RegimeController:
    """Per-symbol ranging-vs-trending regime filter.

    Modes:
      - off: disabled, no-op.
      - shadow: computes and logs verdicts but never acts on trading.
      - active: trending verdict blocks entry / pauses & flattens; ranging
        verdict auto-resumes.
    """

    def __init__(
        self,
        cfg: BotConfig,
        exchange: BinanceSpotClient,
        store: StateStore,
        alerter: TelegramAlerter,
    ) -> None:
        self._cfg = cfg
        self._exchange = exchange
        self._store = store
        self._alerter = alerter
        self._thresholds = RegimeThresholds(
            adx_enter=cfg.regime_adx_enter,
            adx_exit=cfg.regime_adx_exit,
            hurst_enter=cfg.regime_hurst_enter,
            hurst_exit=cfg.regime_hurst_exit,
            min_vol_pct=cfg.regime_min_vol_pct,
            max_vol_pct=cfg.regime_max_vol_pct,
        )
        self._state: dict[str, dict[str, object]] = {}
        self.pauses_today = 0
        self.resumes_today = 0
        self._counter_day: str | None = None

    @property
    def enabled(self) -> bool:
        return self._cfg.regime_filter_mode in {"shadow", "active"}

    @property
    def active(self) -> bool:
        return self._cfg.regime_filter_mode == "active"

    def roll_day(self, day_key: str) -> None:
        if day_key != self._counter_day:
            self._counter_day = day_key
            self.pauses_today = 0
            self.resumes_today = 0

    def last_verdict(self, symbol: str) -> str | None:
        entry = self._state.get(symbol)
        return str(entry["verdict"]) if entry else None

    def _is_due(self, symbol: str, now: datetime) -> bool:
        entry = self._state.get(symbol)
        if entry is None:
            return True
        last_ts = entry["last_ts"]
        assert isinstance(last_ts, datetime)
        return (now - last_ts).total_seconds() >= self._cfg.regime_recompute_seconds

    def refresh(self, symbol: str, now: datetime, force: bool = False) -> None:
        """Recompute the regime verdict for a symbol if due (or forced).

        Fails open: on data/API errors the cached verdict is left unchanged and
        no trading action is taken."""
        if not self.enabled:
            return
        if not force and not self._is_due(symbol, now):
            return
        try:
            candles = self._exchange.get_klines(
                symbol,
                self._cfg.regime_kline_interval,
                self._cfg.regime_kline_lookback,
            )
            assessment = classify_regime(candles, self._thresholds, self.last_verdict(symbol))
        except (InsufficientDataError, requests.RequestException, ValueError, KeyError) as error:
            logging.warning("Regime assessment failed for %s: %s", symbol, error)
            return
        self._state[symbol] = {"verdict": assessment.verdict, "last_ts": now}
        self._store.log_risk_event(
            "regime_verdict",
            symbol,
            {
                "mode": self._cfg.regime_filter_mode,
                "verdict": assessment.verdict,
                "adx": round(assessment.metrics.adx, 3),
                "hurst": round(assessment.metrics.hurst, 4),
                "vol_pct": round(assessment.metrics.realized_vol_pct, 4),
                "reason": assessment.reason,
            },
        )

    def note_pause(self, symbol: str) -> None:
        self.pauses_today += 1
        if _notify_enabled(self._store, self._cfg, "regime"):
            self._alerter.send(f"GridBot regime: {symbol} paused (trending regime).")

    def note_resume(self, symbol: str) -> None:
        self.resumes_today += 1
        if _notify_enabled(self._store, self._cfg, "regime"):
            self._alerter.send(f"GridBot regime: {symbol} resumed (ranging regime).")


def _plan_for_ai_action(plan: GridPlan, action: str) -> GridPlan:
    if action == AI_ACTION_BOTH:
        return plan
    if action == AI_ACTION_BUY_ONLY:
        levels = [level for level in plan.levels if level.side == "BUY"]
    elif action == AI_ACTION_SELL_ONLY:
        levels = [level for level in plan.levels if level.side == "SELL"]
    else:
        levels = []
    if not levels:
        return plan
    return GridPlan(
        symbol=plan.symbol,
        center_price=plan.center_price,
        lower_bound=plan.lower_bound,
        upper_bound=plan.upper_bound,
        levels=[GridLevel(side=level.side, price=level.price, quantity=level.quantity) for level in levels],
    )


class AIFilterController:
    def __init__(
        self,
        cfg: BotConfig,
        store: StateStore,
        alerter: TelegramAlerter,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._alerter = alerter
        system_prompt = cfg.ai_prompt_path.read_text(encoding="utf-8").strip()
        if not system_prompt:
            raise ValueError(f"AI prompt file is empty: {cfg.ai_prompt_path}")
        self._client = OpenAIDecisionClient(
            cfg.ai_api_key,
            cfg.ai_model,
            cfg.ai_timeout_seconds,
            system_prompt=system_prompt,
        )
        self._state: dict[str, dict[str, object]] = {}

    @property
    def enabled(self) -> bool:
        return self._cfg.ai_filter_mode in {"shadow", "active"}

    @property
    def active(self) -> bool:
        return self._cfg.ai_filter_mode == "active"

    def _is_due(self, symbol: str, now: datetime) -> bool:
        entry = self._state.get(symbol)
        if entry is None:
            return True
        last_ts = entry["last_ts"]
        assert isinstance(last_ts, datetime)
        return (now - last_ts).total_seconds() >= self._cfg.ai_recompute_seconds

    def last_action(self, symbol: str) -> str:
        entry = self._state.get(symbol)
        if entry is None:
            return AI_ACTION_BOTH
        return str(entry["action"])

    def refresh(
        self,
        symbol: str,
        now: datetime,
        *,
        price: float,
        current_position_paused: bool,
        regime_verdict: str | None,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        if not force and not self._is_due(symbol, now):
            return
        payload = {
            "symbol": symbol,
            "price": round(price, 8),
            "regime_verdict": regime_verdict or "unknown",
            "symbol_is_paused": current_position_paused,
            "instructions": "Choose one action: BUY_ONLY, SELL_ONLY, BOTH, or PAUSE.",
        }
        try:
            decision = self._client.decide(payload)
        except (requests.RequestException, ValueError) as error:
            logging.warning("AI decision failed for %s: %s", symbol, error)
            fallback_action = AI_ACTION_PAUSE if self.active else self.last_action(symbol)
            if self.active:
                self._state[symbol] = {"action": fallback_action, "last_ts": now}
            self._store.log_risk_event(
                "ai_decision_error",
                symbol,
                {
                    "mode": self._cfg.ai_filter_mode,
                    "action": fallback_action,
                    "details": str(error),
                },
            )
            return
        previous = self.last_action(symbol)
        self._state[symbol] = {"action": decision.action, "last_ts": now}
        self._store.log_risk_event(
            "ai_decision",
            symbol,
            {
                "mode": self._cfg.ai_filter_mode,
                "action": decision.action,
                "confidence": round(decision.confidence, 4),
                "reason": decision.reason,
            },
        )
        if self.active and previous != decision.action and _notify_enabled(self._store, self._cfg, "ai_decisions"):
            self._alerter.send(
                f"GridBot AI: {symbol} action {previous} -> {decision.action} "
                f"(confidence={decision.confidence:.2f})"
            )


def _plan_for_regime_resume(
    ai_filter: AIFilterController,
    plan: GridPlan,
    symbol: str,
    now: datetime,
    price: float,
    regime_verdict: str | None,
) -> GridPlan | None:
    ai_filter.refresh(
        symbol,
        now,
        price=price,
        current_position_paused=True,
        regime_verdict=regime_verdict,
        force=True,
    )
    ai_action = ai_filter.last_action(symbol)
    if ai_filter.active and ai_action == AI_ACTION_PAUSE:
        return None
    return _plan_for_ai_action(plan, ai_action if ai_filter.active else AI_ACTION_BOTH)


def run() -> None:
    _setup_logging()
    cfg = load_config()
    logging.info("Starting GridBot mode=%s dry_run=%s base_url=%s", cfg.mode, cfg.dry_run, cfg.binance_base_url)

    exchange = BinanceSpotClient(cfg.api_key, cfg.api_secret, cfg.binance_base_url)
    store = StateStore(cfg.db_path, cfg.timezone_name)
    alerter = TelegramAlerter(cfg.telegram_bot_token, cfg.telegram_chat_id)
    pnl_provider = _make_pnl_provider(cfg, exchange, store)
    cancel_all_provider = _make_cancel_all_provider(cfg, exchange)
    start_fresh_provider = _make_start_fresh_provider(cfg, exchange, store)
    ai_ask_provider = _make_ai_ask_provider(cfg)
    regime = RegimeController(cfg, exchange, store, alerter)
    ai_filter = AIFilterController(cfg, store, alerter)
    logging.info("Regime filter mode=%s", cfg.regime_filter_mode)
    logging.info("AI filter mode=%s model=%s prompt=%s", cfg.ai_filter_mode, cfg.ai_model, cfg.ai_prompt_path)

    blacklist = _load_blacklist(cfg.blacklist_path)
    tz = ZoneInfo(cfg.timezone_name)

    exchange.ping()
    if not cfg.dry_run:
        try:
            exchange.get_account()
        except requests.HTTPError as error:
            details = extract_binance_error_detail(error)
            raise RuntimeError(
                "Authenticated Binance check failed. "
                f"Details: {details}. "
                "For MODE=testnet, verify testnet key/secret pair and endpoint."
            ) from error
    active_symbols = _refresh_symbols(cfg, exchange, blacklist, alerter, store)
    _run_startup_safety_gate(cfg, exchange, store, active_symbols)
    next_symbol_refresh = datetime.now(tz) + timedelta(minutes=cfg.symbol_refresh_minutes)
    last_report_day = store.get_state("last_report_day")
    bot_halted = store.get_state("bot_halted") == "1"
    clean_cycles_key = "reconciliation_clean_cycles"
    resume_block_key = "reconciliation_resume_block"
    clean_cycles, resume_block_active = _load_reconciliation_gate_state(
        store,
        clean_cycles_key,
        resume_block_key,
    )

    alerter.send("GridBot boot complete.")
    try:
        while True:
            now = datetime.now(tz)
            bot_halted, should_stop = _apply_control_commands(
                alerter,
                store,
                cfg,
                bot_halted,
                pnl_provider,
                cancel_all_provider,
                start_fresh_provider,
                ai_ask_provider,
            )
            if should_stop:
                break
            clean_cycles, resume_block_active = _load_reconciliation_gate_state(
                store,
                clean_cycles_key,
                resume_block_key,
            )

            if now >= next_symbol_refresh:
                active_symbols = _refresh_symbols(cfg, exchange, blacklist, alerter, store)
                next_symbol_refresh = now + timedelta(minutes=cfg.symbol_refresh_minutes)

            regime.roll_day(store.get_day_key())

            risk = check_daily_loss_limit(store, cfg.capital_usdt, cfg.daily_loss_limit_pct)
            if risk.should_stop and not bot_halted:
                bot_halted = True
                store.log_risk_event(
                    "daily_loss_limit_hit",
                    None,
                    {"realized_pnl": risk.realized_pnl, "threshold": risk.threshold},
                )
                if _notify_enabled(store, cfg, "risk_halts"):
                    alerter.send(
                        f"GridBot halted: daily loss limit hit. pnl={risk.realized_pnl:.4f}, threshold={risk.threshold:.4f}"
                    )

            store.set_state("bot_halted", "1" if bot_halted else "0")

            if cfg.reconciliation_enabled and (not bot_halted or cfg.reconciliation_check_on_halt) and not cfg.dry_run:
                rec_result = _run_reconciliation_check(cfg, exchange, store)
                logging.info(_build_reconciliation_status(rec_result))
                if rec_result.is_clean:
                    clean_cycles += 1
                    store.set_state(clean_cycles_key, str(clean_cycles))
                    if resume_block_active and clean_cycles >= cfg.reconciliation_clean_cycles_required:
                        resume_block_active = False
                        store.set_state(resume_block_key, "0")
                        if _notify_enabled(store, cfg, "risk_halts"):
                            alerter.send("Reconciliation recovery gate satisfied. Manual /resume now allowed.")
                else:
                    clean_cycles = 0
                    store.set_state(clean_cycles_key, "0")
                    resume_block_active = True
                    store.set_state(resume_block_key, "1")
                    if not bot_halted:
                        bot_halted = True
                        store.set_state("bot_halted", "1")
                        store.log_risk_event(
                            "reconciliation_breach",
                            None,
                            {
                                "reason": rec_result.reason,
                                "qty_drift_pct": rec_result.max_qty_drift_pct,
                                "quote_drift_usdt": rec_result.quote_drift_usdt,
                                "equity_drift_usdt": rec_result.equity_drift_usdt,
                            },
                        )
                        if _notify_enabled(store, cfg, "risk_halts"):
                            alerter.send(f"GridBot halted: {_build_reconciliation_status(rec_result)}")

            if resume_block_active and clean_cycles < cfg.reconciliation_clean_cycles_required:
                if not bot_halted:
                    bot_halted = True
                    store.set_state("bot_halted", "1")
                    remaining = cfg.reconciliation_clean_cycles_required - clean_cycles
                    if _notify_enabled(store, cfg, "risk_halts"):
                        alerter.send(
                            "Resume blocked by reconciliation gate. "
                            f"Required clean cycles remaining: {remaining}."
                        )

            if bot_halted:
                if cfg.reconciliation_enabled and cfg.reconciliation_check_on_halt and not cfg.dry_run:
                    required = cfg.reconciliation_clean_cycles_required
                    if clean_cycles < required:
                        remaining = required - clean_cycles
                        logging.info("Halt recovery pending clean cycles: %s remaining", remaining)
                bot_halted, should_stop = _responsive_wait(
                    cfg.loop_seconds,
                    cfg.command_poll_seconds,
                    alerter,
                    store,
                    cfg,
                    bot_halted,
                    pnl_provider,
                    cancel_all_provider,
                    start_fresh_provider,
                    ai_ask_provider,
                )
                if should_stop:
                    break
                continue

            for symbol in active_symbols:
                bot_halted, should_stop = _apply_control_commands(
                    alerter,
                    store,
                    cfg,
                    bot_halted,
                    pnl_provider,
                    cancel_all_provider,
                    start_fresh_provider,
                    ai_ask_provider,
                )
                if cfg.reconciliation_enabled and bot_halted and not cfg.dry_run:
                    clean_cycles, resume_block_active = _load_reconciliation_gate_state(
                        store,
                        clean_cycles_key,
                        resume_block_key,
                    )
                    required = cfg.reconciliation_clean_cycles_required
                    if clean_cycles < required:
                        bot_halted = True
                        store.set_state("bot_halted", "1")
                store.set_state("bot_halted", "1" if bot_halted else "0")
                if should_stop or bot_halted:
                    break
                price = exchange.get_ticker_price(symbol)
                state = store.get_symbol_state(symbol)

                if state is None:
                    regime.refresh(symbol, now, force=True)
                    if regime.active and regime.last_verdict(symbol) == REGIME_TRENDING:
                        plan = build_grid(
                            symbol, price, cfg.grid_spacing_pct, cfg.grid_levels, cfg.per_symbol_capital
                        )
                        store.upsert_symbol_state(
                            symbol,
                            center_price=plan.center_price,
                            lower_bound=plan.lower_bound,
                            upper_bound=plan.upper_bound,
                            paused=True,
                            pause_reason="trend_regime",
                        )
                        store.log_risk_event("regime_block_entry", symbol, {"price": price})
                        regime.note_pause(symbol)
                        continue
                    plan = build_grid(symbol, price, cfg.grid_spacing_pct, cfg.grid_levels, cfg.per_symbol_capital)
                    ai_filter.refresh(
                        symbol,
                        now,
                        price=price,
                        current_position_paused=False,
                        regime_verdict=regime.last_verdict(symbol),
                        force=True,
                    )
                    ai_action = ai_filter.last_action(symbol)
                    if ai_filter.active and ai_action == AI_ACTION_PAUSE:
                        store.upsert_symbol_state(
                            symbol,
                            center_price=plan.center_price,
                            lower_bound=plan.lower_bound,
                            upper_bound=plan.upper_bound,
                            paused=True,
                            pause_reason="ai_pause",
                        )
                        store.log_risk_event("ai_pause", symbol, {"price": price})
                        continue
                    effective_plan = _plan_for_ai_action(plan, ai_action if ai_filter.active else AI_ACTION_BOTH)
                    store.upsert_symbol_state(
                        symbol,
                        center_price=plan.center_price,
                        lower_bound=plan.lower_bound,
                        upper_bound=plan.upper_bound,
                        paused=False,
                        pause_reason=None,
                    )
                    try:
                        sync_grid_orders(exchange, store, effective_plan, ExecutionConfig(dry_run=cfg.dry_run))
                    except InsufficientFundsError as error:
                        _handle_insufficient_funds(store, alerter, symbol, error)
                    except OrderPlacementError as error:
                        _handle_order_placement_error(store, alerter, symbol, error, cfg)
                    continue

                if state.paused:
                    if state.pause_reason == "trend_regime":
                        regime.refresh(symbol, now)
                        if regime.active and regime.last_verdict(symbol) == REGIME_RANGING:
                            recentered = build_grid(
                                symbol, price, cfg.grid_spacing_pct, cfg.grid_levels, cfg.per_symbol_capital
                            )
                            effective_recentered = _plan_for_regime_resume(
                                ai_filter,
                                recentered,
                                symbol,
                                now,
                                price,
                                regime.last_verdict(symbol),
                            )
                            if effective_recentered is None:
                                store.set_symbol_paused(symbol, True, "ai_pause")
                                store.log_risk_event("ai_pause", symbol, {"price": price})
                                continue
                            store.upsert_symbol_state(
                                symbol,
                                center_price=recentered.center_price,
                                lower_bound=recentered.lower_bound,
                                upper_bound=recentered.upper_bound,
                                paused=False,
                                pause_reason=None,
                            )
                            store.log_risk_event("regime_resume", symbol, {"price": price})
                            regime.note_resume(symbol)
                            try:
                                sync_grid_orders(
                                    exchange,
                                    store,
                                    effective_recentered,
                                    ExecutionConfig(dry_run=cfg.dry_run),
                                )
                            except InsufficientFundsError as error:
                                _handle_insufficient_funds(store, alerter, symbol, error)
                            except OrderPlacementError as error:
                                _handle_order_placement_error(store, alerter, symbol, error, cfg)
                        continue
                    if state.pause_reason == "ai_pause":
                        ai_filter.refresh(
                            symbol,
                            now,
                            price=price,
                            current_position_paused=True,
                            regime_verdict=regime.last_verdict(symbol),
                        )
                        if ai_filter.active and ai_filter.last_action(symbol) != AI_ACTION_PAUSE:
                            store.set_symbol_paused(symbol, False, None)
                            state = store.get_symbol_state(symbol)
                            if state is None:
                                continue
                        else:
                            continue
                    if (
                        state.pause_reason == "insufficient_funds"
                        and _should_retry_insufficient_funds(
                            state.updated_at,
                            now,
                            cfg.insufficient_funds_retry_minutes,
                        )
                    ):
                        store.set_symbol_paused(symbol, False, None)
                        store.log_risk_event(
                            "insufficient_funds_retry",
                            symbol,
                            {"retry_minutes": cfg.insufficient_funds_retry_minutes},
                        )
                        state = store.get_symbol_state(symbol)
                        if state is None:
                            continue
                    else:
                        continue

                regime.refresh(symbol, now)
                if regime.active and regime.last_verdict(symbol) == REGIME_TRENDING:
                    store.set_symbol_paused(symbol, True, "trend_regime")
                    if not cfg.dry_run:
                        try:
                            _cancel_all_orders_ignoring_missing(exchange, symbol)
                        except requests.HTTPError as error:
                            details = extract_binance_error_detail(error)
                            store.log_risk_event(
                                "cancel_all_orders_error",
                                symbol,
                                {"context": "trend_regime", "details": details},
                            )
                            if _notify_enabled(store, cfg, "order_errors"):
                                alerter.send(
                                    f"Cancel all failed while pausing {symbol} for trend_regime: {details}"
                                )
                    store.log_risk_event("regime_pause", symbol, {"price": price})
                    regime.note_pause(symbol)
                    continue
                ai_filter.refresh(
                    symbol,
                    now,
                    price=price,
                    current_position_paused=False,
                    regime_verdict=regime.last_verdict(symbol),
                )
                ai_action = ai_filter.last_action(symbol)
                if ai_filter.active and ai_action == AI_ACTION_PAUSE:
                    store.set_symbol_paused(symbol, True, "ai_pause")
                    if not cfg.dry_run:
                        try:
                            _cancel_all_orders_ignoring_missing(exchange, symbol)
                        except requests.HTTPError as error:
                            details = extract_binance_error_detail(error)
                            store.log_risk_event(
                                "cancel_all_orders_error",
                                symbol,
                                {"context": "ai_pause", "details": details},
                            )
                            if _notify_enabled(store, cfg, "order_errors"):
                                alerter.send(f"Cancel all failed while pausing {symbol} for ai_pause: {details}")
                    store.log_risk_event("ai_pause", symbol, {"price": price})
                    continue

                band_trigger = check_symbol_band(
                    center_price=state.center_price,
                    current_price=price,
                    stop_loss_pct=cfg.per_symbol_stop_loss_pct,
                    take_profit_pct=cfg.per_symbol_take_profit_pct,
                )
                if band_trigger is not None:
                    store.set_symbol_paused(symbol, True, band_trigger)
                    if not cfg.dry_run:
                        try:
                            _cancel_all_orders_ignoring_missing(exchange, symbol)
                        except requests.HTTPError as error:
                            details = extract_binance_error_detail(error)
                            store.log_risk_event(
                                "cancel_all_orders_error",
                                symbol,
                                {"context": "symbol_band_trigger", "details": details},
                            )
                            if _notify_enabled(store, cfg, "order_errors"):
                                alerter.send(
                                    f"Cancel all failed while pausing {symbol} for {band_trigger}: {details}"
                                )
                        _liquidate_symbol_position(exchange, store, alerter, symbol, price, band_trigger, cfg)
                    store.log_risk_event("symbol_band_trigger", symbol, {"band": band_trigger, "price": price})
                    if _notify_enabled(store, cfg, "liquidation"):
                        alerter.send(f"Symbol paused: {symbol}, reason={band_trigger}, price={price:.8f}")
                    continue

                current_plan = build_grid(
                    symbol,
                    state.center_price,
                    cfg.grid_spacing_pct,
                    cfg.grid_levels,
                    cfg.per_symbol_capital,
                )
                if is_outside_grid(price, current_plan):
                    if not cfg.dry_run:
                        try:
                            _cancel_all_orders_ignoring_missing(exchange, symbol)
                        except requests.HTTPError as error:
                            details = extract_binance_error_detail(error)
                            _handle_order_placement_error(
                                store,
                                alerter,
                                symbol,
                                OrderPlacementError(symbol, f"cancel_all_orders failed: {details}"),
                                cfg,
                            )
                            continue
                    recentered = build_grid(symbol, price, cfg.grid_spacing_pct, cfg.grid_levels, cfg.per_symbol_capital)
                    effective_recentered = _plan_for_ai_action(
                        recentered,
                        ai_action if ai_filter.active else AI_ACTION_BOTH,
                    )
                    store.upsert_symbol_state(
                        symbol,
                        center_price=recentered.center_price,
                        lower_bound=recentered.lower_bound,
                        upper_bound=recentered.upper_bound,
                        paused=False,
                        pause_reason=None,
                    )
                    try:
                        sync_grid_orders(exchange, store, effective_recentered, ExecutionConfig(dry_run=cfg.dry_run))
                    except InsufficientFundsError as error:
                        _handle_insufficient_funds(store, alerter, symbol, error)
                    except OrderPlacementError as error:
                        _handle_order_placement_error(store, alerter, symbol, error, cfg)
                else:
                    effective_current_plan = _plan_for_ai_action(
                        current_plan,
                        ai_action if ai_filter.active else AI_ACTION_BOTH,
                    )
                    try:
                        sync_grid_orders(exchange, store, effective_current_plan, ExecutionConfig(dry_run=cfg.dry_run))
                    except InsufficientFundsError as error:
                        _handle_insufficient_funds(store, alerter, symbol, error)
                    except OrderPlacementError as error:
                        _handle_order_placement_error(store, alerter, symbol, error, cfg)

            if should_stop:
                break

            day_key = store.get_day_key()
            if day_key != last_report_day:
                try:
                    pnl_status_line = pnl_provider()
                except (requests.RequestException, ValueError) as error:
                    logging.warning("P/L snapshot failed for daily report: %s", error)
                    pnl_status_line = "P/L update unavailable (snapshot error)."
                summary = build_daily_summary(
                    store,
                    cfg.timezone_name,
                    active_symbols,
                    bot_halted,
                    pnl_status_line,
                    regime_mode=cfg.regime_filter_mode,
                    regime_pauses=regime.pauses_today,
                    regime_resumes=regime.resumes_today,
                )
                if _notify_enabled(store, cfg, "daily_summary"):
                    alerter.send(summary)
                store.set_state("last_report_day", day_key)
                last_report_day = day_key

            bot_halted, should_stop = _responsive_wait(
                cfg.loop_seconds,
                cfg.command_poll_seconds,
                alerter,
                store,
                cfg,
                bot_halted,
                pnl_provider,
                cancel_all_provider,
                start_fresh_provider,
                ai_ask_provider,
            )
            if should_stop:
                break
    finally:
        store.close()


if __name__ == "__main__":
    run()
