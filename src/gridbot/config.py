from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BINANCE_URL_BY_MODE = {
    "paper": "https://api.binance.com",
    "testnet": "https://testnet.binance.vision",
    "live": "https://api.binance.com",
}


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        cleaned_key = key.strip()
        cleaned_value = value.strip().strip("'").strip('"')
        os.environ[cleaned_key] = cleaned_value


def _get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BotConfig:
    mode: str
    api_key: str
    api_secret: str
    telegram_bot_token: str
    telegram_chat_id: str
    dry_run: bool
    capital_usdt: float
    daily_loss_limit_pct: float
    per_symbol_stop_loss_pct: float
    per_symbol_take_profit_pct: float
    grid_spacing_pct: float
    grid_levels: int
    symbol_refresh_minutes: int
    loop_seconds: int
    command_poll_seconds: int
    insufficient_funds_retry_minutes: int
    reconciliation_enabled: bool
    reconciliation_qty_drift_pct: float
    reconciliation_equity_drift_usdt: float
    reconciliation_clean_cycles_required: int
    reconciliation_check_on_halt: bool
    max_active_symbols: int
    max_symbol_capital_pct: float
    binance_base_url: str
    timezone_name: str
    db_path: Path
    blacklist_path: Path

    @property
    def per_symbol_capital(self) -> float:
        capped = self.capital_usdt * self.max_symbol_capital_pct
        equal_weight = self.capital_usdt / max(self.max_active_symbols, 1)
        return min(capped, equal_weight)


def load_config() -> BotConfig:
    _load_dotenv_file(Path(".env"))
    mode = _get_env("MODE", "paper").strip().lower()
    if mode not in DEFAULT_BINANCE_URL_BY_MODE:
        raise ValueError("MODE must be one of: paper, testnet, live")
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if mode in {"testnet", "live"} and (not api_key or not api_secret):
        raise ValueError("BINANCE_API_KEY and BINANCE_API_SECRET are required for MODE=testnet/live")
    base_url = _get_env("BINANCE_BASE_URL", DEFAULT_BINANCE_URL_BY_MODE[mode]).strip()
    if mode == "testnet" and "api.binance.com" in base_url:
        raise ValueError(
            "MODE=testnet but BINANCE_BASE_URL points to api.binance.com. "
            "Set BINANCE_BASE_URL=https://testnet.binance.vision or remove BINANCE_BASE_URL."
        )
    if mode == "live" and "testnet.binance.vision" in base_url:
        raise ValueError(
            "MODE=live but BINANCE_BASE_URL points to testnet. "
            "Set BINANCE_BASE_URL=https://api.binance.com or remove BINANCE_BASE_URL."
        )
    insufficient_funds_retry_minutes = int(_get_env("INSUFFICIENT_FUNDS_RETRY_MINUTES", "10"))
    if insufficient_funds_retry_minutes < 1:
        raise ValueError("INSUFFICIENT_FUNDS_RETRY_MINUTES must be >= 1")
    reconciliation_enabled = _get_bool("RECONCILIATION_ENABLED", mode in {"testnet", "live"})
    reconciliation_qty_drift_pct = float(_get_env("RECONCILIATION_QTY_DRIFT_PCT", "0.001"))
    if reconciliation_qty_drift_pct < 0:
        raise ValueError("RECONCILIATION_QTY_DRIFT_PCT must be >= 0")
    reconciliation_equity_drift_usdt = float(_get_env("RECONCILIATION_EQUITY_DRIFT_USDT", "5"))
    if reconciliation_equity_drift_usdt < 0:
        raise ValueError("RECONCILIATION_EQUITY_DRIFT_USDT must be >= 0")
    reconciliation_clean_cycles_required = int(_get_env("RECONCILIATION_CLEAN_CYCLES_REQUIRED", "3"))
    if reconciliation_clean_cycles_required < 1:
        raise ValueError("RECONCILIATION_CLEAN_CYCLES_REQUIRED must be >= 1")

    return BotConfig(
        mode=mode,
        api_key=api_key,
        api_secret=api_secret,
        telegram_bot_token=_get_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get_env("TELEGRAM_CHAT_ID"),
        dry_run=mode == "paper",
        capital_usdt=float(_get_env("CAPITAL_USDT", "200")),
        daily_loss_limit_pct=float(_get_env("DAILY_LOSS_LIMIT_PCT", "0.05")),
        per_symbol_stop_loss_pct=float(_get_env("PER_SYMBOL_STOP_LOSS_PCT", "0.03")),
        per_symbol_take_profit_pct=float(_get_env("PER_SYMBOL_TAKE_PROFIT_PCT", "0.04")),
        grid_spacing_pct=float(_get_env("GRID_SPACING_PCT", "0.006")),
        grid_levels=int(_get_env("GRID_LEVELS", "8")),
        symbol_refresh_minutes=int(_get_env("SYMBOL_REFRESH_MINUTES", "60")),
        loop_seconds=int(_get_env("LOOP_SECONDS", "60")),
        command_poll_seconds=int(_get_env("COMMAND_POLL_SECONDS", "5")),
        insufficient_funds_retry_minutes=insufficient_funds_retry_minutes,
        reconciliation_enabled=reconciliation_enabled,
        reconciliation_qty_drift_pct=reconciliation_qty_drift_pct,
        reconciliation_equity_drift_usdt=reconciliation_equity_drift_usdt,
        reconciliation_clean_cycles_required=reconciliation_clean_cycles_required,
        reconciliation_check_on_halt=_get_bool("RECONCILIATION_CHECK_ON_HALT", True),
        max_active_symbols=int(_get_env("MAX_ACTIVE_SYMBOLS", "5")),
        max_symbol_capital_pct=float(_get_env("MAX_SYMBOL_CAPITAL_PCT", "0.20")),
        binance_base_url=base_url,
        timezone_name=_get_env("TIMEZONE", "Asia/Jerusalem"),
        db_path=Path(_get_env("DB_PATH", "data/gridbot.db")),
        blacklist_path=Path(_get_env("BLACKLIST_PATH", "config/blacklist.txt")),
    )
