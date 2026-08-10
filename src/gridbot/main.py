from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from gridbot.alerts import TelegramAlerter
from gridbot.config import BotConfig, load_config
from gridbot.exchange import BinanceSpotClient, extract_binance_error_detail
from gridbot.execution import ExecutionConfig, InsufficientFundsError, OrderPlacementError, sync_grid_orders
from gridbot.grid_engine import build_grid, is_outside_grid
from gridbot.pnl import compute_live_pnl_snapshot
from gridbot.reporting import build_daily_summary
from gridbot.risk_guard import check_daily_loss_limit, check_symbol_band
from gridbot.selector import select_symbols
from gridbot.state_store import StateStore


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _load_blacklist(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"Blacklist file not found: {path}")
    values = {line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    return values


def _apply_control_commands(
    alerter: TelegramAlerter,
    store: StateStore,
    bot_halted: bool,
    pnl_provider: Callable[[], str],
    cancel_all_provider: Callable[[], str],
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
    return bot_halted, should_stop


def _refresh_symbols(
    cfg: BotConfig,
    exchange: BinanceSpotClient,
    blacklist: set[str],
    alerter: TelegramAlerter,
) -> list[str]:
    logging.info("Refreshing symbol shortlist...")
    ranked = select_symbols(exchange, blacklist, cfg.max_active_symbols)
    symbols = [r.symbol for r in ranked]
    logging.info("Selected symbols: %s", ", ".join(symbols) if symbols else "none")
    alerter.send("GridBot symbol refresh: " + (", ".join(symbols) if symbols else "none"))
    return symbols


def _handle_insufficient_funds(
    store: StateStore,
    _alerter: TelegramAlerter,
    symbol: str,
    _error: InsufficientFundsError,
) -> None:
    store.set_symbol_paused(symbol, True, "insufficient_funds")
    store.log_risk_event("insufficient_funds", symbol, {"reason": "insufficient_funds"})


def _handle_order_placement_error(
    store: StateStore,
    alerter: TelegramAlerter,
    symbol: str,
    error: OrderPlacementError,
) -> None:
    store.set_symbol_paused(symbol, True, "order_placement_error")
    store.log_risk_event("order_placement_error", symbol, {"details": error.details})
    alerter.send(f"Symbol paused: {symbol}, reason=order_placement_error, details={error.details}")


def _responsive_wait(
    wait_seconds: int,
    poll_seconds: int,
    alerter: TelegramAlerter,
    store: StateStore,
    bot_halted: bool,
    pnl_provider: Callable[[], str],
    cancel_all_provider: Callable[[], str],
) -> tuple[bool, bool]:
    remaining = max(wait_seconds, 0)
    step = max(poll_seconds, 1)
    while remaining > 0:
        sleep_for = min(step, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for
        bot_halted, should_stop = _apply_control_commands(
            alerter,
            store,
            bot_halted,
            pnl_provider,
            cancel_all_provider,
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


def _build_help_text() -> str:
    return (
        "GridBot commands:\n"
        "/help - show this command list\n"
        "/status - bot status and latest P/L snapshot\n"
        "/pnl - on-demand P/L snapshot\n"
        "/transitions - latest transition events\n"
        "/cancel_all - cancel all open spot orders\n"
        "/kill - halt trading loop\n"
        "/resume - resume trading\n"
        "/stop - stop bot process"
    )


def _build_heartbeat_text(status: str, active_symbols: list[str], pnl_text: str) -> str:
    symbols = ", ".join(active_symbols) if active_symbols else "none"
    return f"[Heartbeat]\nStatus: {status}\nActive symbols: {symbols}\n{pnl_text}"


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


def run() -> None:
    _setup_logging()
    cfg = load_config()
    logging.info("Starting GridBot mode=%s dry_run=%s base_url=%s", cfg.mode, cfg.dry_run, cfg.binance_base_url)

    exchange = BinanceSpotClient(cfg.api_key, cfg.api_secret, cfg.binance_base_url)
    store = StateStore(cfg.db_path, cfg.timezone_name)
    alerter = TelegramAlerter(cfg.telegram_bot_token, cfg.telegram_chat_id)
    pnl_provider = _make_pnl_provider(cfg, exchange, store)
    cancel_all_provider = _make_cancel_all_provider(cfg, exchange)

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
    active_symbols = _refresh_symbols(cfg, exchange, blacklist, alerter)
    next_symbol_refresh = datetime.now(tz) + timedelta(minutes=cfg.symbol_refresh_minutes)
    next_heartbeat = datetime.now(tz) + timedelta(minutes=cfg.heartbeat_minutes)
    last_report_day = store.get_state("last_report_day")
    bot_halted = store.get_state("bot_halted") == "1"

    alerter.send("GridBot boot complete.")
    try:
        while True:
            now = datetime.now(tz)
            bot_halted, should_stop = _apply_control_commands(
                alerter,
                store,
                bot_halted,
                pnl_provider,
                cancel_all_provider,
            )
            if should_stop:
                break

            if now >= next_symbol_refresh:
                active_symbols = _refresh_symbols(cfg, exchange, blacklist, alerter)
                next_symbol_refresh = now + timedelta(minutes=cfg.symbol_refresh_minutes)

            risk = check_daily_loss_limit(store, cfg.capital_usdt, cfg.daily_loss_limit_pct)
            if risk.should_stop and not bot_halted:
                bot_halted = True
                store.log_risk_event(
                    "daily_loss_limit_hit",
                    None,
                    {"realized_pnl": risk.realized_pnl, "threshold": risk.threshold},
                )
                alerter.send(
                    f"GridBot halted: daily loss limit hit. pnl={risk.realized_pnl:.4f}, threshold={risk.threshold:.4f}"
                )

            if now >= next_heartbeat:
                status = "STOPPED" if bot_halted else "RUNNING"
                try:
                    pnl_text = pnl_provider()
                except (requests.RequestException, ValueError) as error:
                    logging.warning("P/L snapshot failed for heartbeat: %s", error)
                    pnl_text = "P/L update unavailable right now (exchange/API error)."
                alerter.send(_build_heartbeat_text(status, active_symbols, pnl_text))
                next_heartbeat = now + timedelta(minutes=cfg.heartbeat_minutes)

            store.set_state("bot_halted", "1" if bot_halted else "0")
            if bot_halted:
                bot_halted, should_stop = _responsive_wait(
                    cfg.loop_seconds,
                    cfg.command_poll_seconds,
                    alerter,
                    store,
                    bot_halted,
                    pnl_provider,
                    cancel_all_provider,
                )
                if should_stop:
                    break
                continue

            for symbol in active_symbols:
                bot_halted, should_stop = _apply_control_commands(
                    alerter,
                    store,
                    bot_halted,
                    pnl_provider,
                    cancel_all_provider,
                )
                store.set_state("bot_halted", "1" if bot_halted else "0")
                if should_stop or bot_halted:
                    break
                price = exchange.get_ticker_price(symbol)
                state = store.get_symbol_state(symbol)

                if state is None:
                    plan = build_grid(symbol, price, cfg.grid_spacing_pct, cfg.grid_levels, cfg.per_symbol_capital)
                    store.upsert_symbol_state(
                        symbol,
                        center_price=plan.center_price,
                        lower_bound=plan.lower_bound,
                        upper_bound=plan.upper_bound,
                        paused=False,
                        pause_reason=None,
                    )
                    try:
                        sync_grid_orders(exchange, store, plan, ExecutionConfig(dry_run=cfg.dry_run))
                    except InsufficientFundsError as error:
                        _handle_insufficient_funds(store, alerter, symbol, error)
                    except OrderPlacementError as error:
                        _handle_order_placement_error(store, alerter, symbol, error)
                    continue

                if state.paused:
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
                            exchange.cancel_all_orders(symbol)
                        except requests.HTTPError as error:
                            details = extract_binance_error_detail(error)
                            store.log_risk_event(
                                "cancel_all_orders_error",
                                symbol,
                                {"context": "symbol_band_trigger", "details": details},
                            )
                            alerter.send(
                                f"Cancel all failed while pausing {symbol} for {band_trigger}: {details}"
                            )
                    store.log_risk_event("symbol_band_trigger", symbol, {"band": band_trigger, "price": price})
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
                            exchange.cancel_all_orders(symbol)
                        except requests.HTTPError as error:
                            details = extract_binance_error_detail(error)
                            _handle_order_placement_error(
                                store,
                                alerter,
                                symbol,
                                OrderPlacementError(symbol, f"cancel_all_orders failed: {details}"),
                            )
                            continue
                    recentered = build_grid(symbol, price, cfg.grid_spacing_pct, cfg.grid_levels, cfg.per_symbol_capital)
                    store.upsert_symbol_state(
                        symbol,
                        center_price=recentered.center_price,
                        lower_bound=recentered.lower_bound,
                        upper_bound=recentered.upper_bound,
                        paused=False,
                        pause_reason=None,
                    )
                    try:
                        sync_grid_orders(exchange, store, recentered, ExecutionConfig(dry_run=cfg.dry_run))
                    except InsufficientFundsError as error:
                        _handle_insufficient_funds(store, alerter, symbol, error)
                    except OrderPlacementError as error:
                        _handle_order_placement_error(store, alerter, symbol, error)
                else:
                    try:
                        sync_grid_orders(exchange, store, current_plan, ExecutionConfig(dry_run=cfg.dry_run))
                    except InsufficientFundsError as error:
                        _handle_insufficient_funds(store, alerter, symbol, error)
                    except OrderPlacementError as error:
                        _handle_order_placement_error(store, alerter, symbol, error)

            if should_stop:
                break

            day_key = store.get_day_key()
            if day_key != last_report_day:
                try:
                    pnl_status_line = pnl_provider()
                except (requests.RequestException, ValueError) as error:
                    logging.warning("P/L snapshot failed for daily report: %s", error)
                    pnl_status_line = "P/L update unavailable (snapshot error)."
                summary = build_daily_summary(store, cfg.timezone_name, active_symbols, bot_halted, pnl_status_line)
                alerter.send(summary)
                store.set_state("last_report_day", day_key)
                last_report_day = day_key

            bot_halted, should_stop = _responsive_wait(
                cfg.loop_seconds,
                cfg.command_poll_seconds,
                alerter,
                store,
                bot_halted,
                pnl_provider,
                cancel_all_provider,
            )
            if should_stop:
                break
    finally:
        store.close()


if __name__ == "__main__":
    run()
