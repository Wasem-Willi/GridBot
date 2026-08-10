# GridBot (Binance Spot, Risk-Guarded v1)

This project scaffolds a real-money-ready **grid trading bot** with strict safety defaults:

- Binance **spot-only**
- Dynamic symbol rotation (hourly)
- 1-minute control loop
- Daily loss stop + per-symbol stop-loss/take-profit bands
- Reconciliation safety gate (position/quote/equity drift checks with global halt)
- Telegram alerts + `/kill` and `/resume` commands
- SQLite state store
- Docker Compose deployment

## 1) Safety-first setup

1. Create Binance API key with:
   - trading enabled
   - withdrawals disabled
   - IP allowlist enabled (your VPS IP)
2. Create Telegram bot and capture:
   - bot token
   - your chat ID
3. Copy env template:

```bash
cp .env.example .env
```

4. Fill secrets in `.env`.
5. Set `MODE` in `.env`:
   - `MODE=paper` (simulation only)
   - `MODE=testnet` (Binance testnet fake funds, real order flow)
   - `MODE=live` (real funds)
6. If `MODE=testnet`, ensure `BINANCE_BASE_URL` is unset or `https://testnet.binance.vision`.
7. In `testnet/live` modes, bot runs a signed auth preflight at startup; if credentials are wrong, it exits early with a clear error.

## 2) Files you should edit first

- `config/blacklist.txt` - static symbol blacklist
- `.env` - risk and runtime config
- `MODE` in `.env` is the one-line environment switch: `paper|testnet|live`
- `COMMAND_POLL_SECONDS` in `.env` controls Telegram command responsiveness (default 5 seconds)
- `INSUFFICIENT_FUNDS_RETRY_MINUTES` controls auto-retry cooldown for symbols paused due to insufficient funds
- `RECONCILIATION_*` values control startup/loop drift checks and clean-cycle recovery gate after a breach

## 3) Run locally (Python)

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m gridbot.main
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m gridbot.main
```

## 4) Run with Docker Compose

```bash
docker compose up -d --build
docker compose logs -f gridbot
```

## 5) Telegram commands

- `/help` - show available commands
- `/transitions` - show latest transition/risk events
- `/cancel_all` - cancel all open spot orders
- `/kill` - immediately halt trading loop
- `/resume` - resume trading manually
- `/stop` - stop the bot process
- `/status` - current bot state + latest P/L snapshot
- `/pnl` - on-demand near-live account equity P/L snapshot (testnet/live)

## 6) Rollout path (recommended)

1. Backtest with conservative fee/slippage assumptions.
2. Paper trade and verify:
   - no safety breaches
   - stable uptime
   - reproducible daily reporting
3. Go live with tiny position sizing.
4. Increase capital only after multi-week stability.

## 7) Current v1 scope notes

- Order placement is implemented with maker-first grid orders.
- Insufficient-funds order failures are handled gracefully: the symbol is paused silently and a risk event is recorded (no Telegram alert).
- Non-insufficient order placement failures are handled gracefully too: the symbol is paused and alert details are sent to Telegram.
- Account-equity P/L snapshots are available in `testnet/live` modes via `/pnl` and included in `/status`.
- The fallback from stale maker orders to taker orders should be added next as a latency- and slippage-aware policy.
- Fill-by-fill realized PnL tracking is not wired yet; daily report field "Realized PnL today" remains 0 unless explicitly recorded.

## 8) Safety reconciliation behavior

- In `testnet/live`, the bot can run reconciliation checks against exchange balances/equity.
- If drift exceeds thresholds, the bot performs a global halt and records a `reconciliation_breach` risk event.
- After a reconciliation breach, manual `/resume` remains blocked until the configured number of consecutive clean cycles is reached.
- Threshold and recovery settings are configured via:
   - `RECONCILIATION_ENABLED`
   - `RECONCILIATION_QTY_DRIFT_PCT` (default `0.001` = 0.1%)
   - `RECONCILIATION_EQUITY_DRIFT_USDT` (default `5`)
   - `RECONCILIATION_CLEAN_CYCLES_REQUIRED` (default `3`)
   - `RECONCILIATION_CHECK_ON_HALT` (default `true`)
