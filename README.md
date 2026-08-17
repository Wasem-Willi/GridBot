# GridBot (Binance Spot, Risk-Guarded v1)

This project scaffolds a real-money-ready **grid trading bot** with strict safety defaults:

- Binance **spot-only**
- Dynamic symbol rotation (hourly)
- 1-minute control loop
- Daily loss stop + per-symbol stop-loss/take-profit bands (liquidates the held position at market when triggered, not just a pause)
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
- `/start_fresh` - cancel open orders, reset local bot state, and start a fresh grid cycle
- `/kill` - immediately halt trading loop
- `/resume` - resume trading manually
- `/stop` - stop the bot process
- `/status` - current bot state + latest P/L snapshot
- `/pnl` - on-demand near-live account equity P/L snapshot (testnet/live)
- `/notify` - show live notification toggle status for every category
- `/notify_on <category|all>` - turn a notification category (or all) on
- `/notify_off <category|all>` - turn a notification category (or all) off
- `/ask <question>` - ask the AI assistant a free-text question (requires `OPENAI_API_KEY`)
- `/ask_reset` - clear the AI assistant's conversation memory

### Live notification toggles

Each category of live Telegram alert can be turned on or off **at runtime**
from Telegram itself, with no redeploy or restart needed - send `/notify` to
see current status and category names, then `/notify_on <category>` or
`/notify_off <category>` to change one. Use `all` instead of a category name
to toggle every category in one shot (e.g. `/notify_off all` to go silent,
`/notify_on all` to restore everything). Toggles persist across bot restarts
(stored in the bot's local state DB) until changed again. Replies to the
commands above always send regardless of these flags - they only gate
passive/background alerts:

| Category | Controls alerts for |
| --- | --- |
| `ai_decisions` | AI filter action changes (e.g. `PAUSE -> BOTH`) |
| `regime` | Regime pause/resume (trending vs ranging) |
| `liquidation` | Stop-loss/take-profit trigger + liquidation sell results |
| `order_errors` | Order placement / cancel-all failures |
| `risk_halts` | Daily loss limit halts and reconciliation halts/gates |
| `symbol_refresh` | Active symbol rotation announcements |
| `daily_summary` | The once-a-day summary report |

Example: send `/notify_off symbol_refresh` in Telegram to stop the hourly
"symbols selected" spam while keeping liquidation and error alerts on; send
`/notify_on symbol_refresh` to turn it back on. Each category also has a
matching `NOTIFY_*` env var (e.g. `NOTIFY_SYMBOL_REFRESH=false`) that sets the
**default** on startup - the Telegram toggle always overrides that default
once you've used `/notify_on`/`/notify_off` for a category.

## 6) Rollout path (recommended)

1. Backtest with conservative fee/slippage assumptions.
2. Paper trade and verify:
   - no safety breaches
   - stable uptime
   - reproducible daily reporting
3. Go live with tiny position sizing.
4. Increase capital only after multi-week stability.

## 7) AI regime filter (ranging vs trending)

A per-symbol statistical classifier decides **when** the grid should trade a
symbol versus pause it. Grid bots bleed money when the market trends against the
grid, so this filter blocks/pauses grids on symbols that are trending and
resumes them when they return to a range. It is not a price-direction predictor
and uses no trained model. See [docs/adr/0001-ai-regime-filter.md](docs/adr/0001-ai-regime-filter.md)
and [docs/glossary.md](docs/glossary.md) for the full design and vocabulary.

How it works:

- Fetches 15m klines (~96 bars) for the shortlisted and currently-active symbols
  only (a handful of extra API calls per cycle).
- Computes **ADX** (trend strength), the **Hurst exponent** (mean-reverting vs
  trending), and **realized volatility** (a sanity band), all in pure Python.
- Verdict uses hysteresis: pause when `ADX > REGIME_ADX_ENTER` and
  `Hurst > REGIME_HURST_ENTER`; resume when `ADX < REGIME_ADX_EXIT` and
  `Hurst < REGIME_HURST_EXIT`. The gap between enter/exit prevents flapping.
- Recomputed every `REGIME_RECOMPUTE_SECONDS` and on each symbol refresh.

Modes (`REGIME_FILTER_MODE`):

- `off` - disabled.
- `shadow` (default) - computes and logs `regime_verdict` events (visible via
  `/transitions`) but does **not** affect trading. Run this first to gather
  evidence before activating.
- `active` - a trending verdict blocks new entries and pauses & flattens an
  existing grid (`pause_reason=trend_regime`); a ranging verdict auto-resumes and
  rebuilds the grid at the current price. Flips send a deduplicated Telegram
  alert, and the daily summary reports pause/resume counts.

Tune the thresholds in `.env`: `REGIME_FILTER_MODE`, `REGIME_ADX_ENTER`,
`REGIME_ADX_EXIT`, `REGIME_HURST_ENTER`, `REGIME_HURST_EXIT`,
`REGIME_MIN_VOL_PCT`, `REGIME_MAX_VOL_PCT`, `REGIME_RECOMPUTE_SECONDS`,
`REGIME_KLINE_INTERVAL`, `REGIME_KLINE_LOOKBACK`.

Run the regime unit tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

### Optional: Generative AI trading filter (OpenAI)

You can add a live generative AI decision layer that chooses one action per
symbol:

- `BUY_ONLY` - place only BUY grid levels.
- `SELL_ONLY` - place only SELL grid levels.
- `BOTH` - keep normal grid behavior.
- `PAUSE` - pause symbol and cancel open orders.

Modes (`AI_FILTER_MODE`):

- `off` - disabled.
- `shadow` (recommended first) - logs `ai_decision` events only, no action.
- `active` - applies AI actions in the live loop.

Safety behavior:

- Fail-safe by default: if OpenAI times out/errors/returns invalid JSON, bot
  logs a warning and continues deterministic flow.
- Existing risk and reconciliation guards still apply.

Env keys:

- `AI_FILTER_MODE` = `off|shadow|active`
- `AI_PROVIDER` = `openai`
- `AI_MODEL` (for example `gpt-4o-mini`)
- `OPENAI_API_KEY`
- `AI_PROMPT_PATH` (for example `docs/openai-decision-spec.md`)
- `AI_TIMEOUT_SECONDS` (default `2`)
- `AI_RECOMPUTE_SECONDS` (default `300`)
- `AI_CHAT_TIMEOUT_SECONDS` (default `20`) - per-request timeout for the `/ask` command below
- `AI_CHAT_HISTORY_DAYS` (default `2`) - how many days of history the `get_transitions` tool returns by default

### Ask the AI assistant (`/ask`)

Independent of `AI_FILTER_MODE`, send `/ask <question>` in Telegram to talk
to an AI **agent** (same OpenAI model, `AI_MODEL`, reusing `OPENAI_API_KEY`)
that can look up live bot data on its own instead of guessing, and remembers
the last few turns of your conversation so natural follow-up questions work
(e.g. "what about ETHUSDT?" after asking about BTCUSDT). Send `/ask_reset`
to clear that memory if it ever gets confused or stale.

The agent has two kinds of tools:

- **Read-only lookups** it can call any time it needs real data: bot
  status, a live P/L snapshot, transition/risk-event history (last N days,
  `AI_CHAT_HISTORY_DAYS` by default, capped at 500 events), open orders
  (all symbols or one), account balances, and a symbol's grid state
  (center price, bounds, paused/reason).
- **Actions** - `cancel_all_orders`, `kill_bot`, `resume_bot`, and
  `set_notification` - the same operations behind `/cancel_all`, `/kill`,
  `/resume`, and `/notify_on`/`/notify_off`. The system prompt instructs the
  agent to only call these when your message explicitly asks for that
  specific action, never on its own initiative or as a side effect of
  answering something else. Every action call is logged as an `ai_action`
  risk event (visible via `/transitions`) for auditability.

The agent **cannot** place, modify, or cancel individual trade orders -
that stays exclusively the deterministic grid logic's job. A multi-tool
turn can take a few OpenAI round trips, so a single `/ask` reply may take
noticeably longer than the other commands (up to `AI_CHAT_TIMEOUT_SECONDS`
per round trip, capped at 5 rounds) and will briefly block the bot's main
loop while it runs. If `OPENAI_API_KEY` is not set, `/ask` replies that AI
chat isn't configured instead of failing silently.

## 8) Current v1 scope notes

- Order placement is implemented with maker-first grid orders.
- Grid levels are checked against free wallet balances before placement. A USDT-only wallet can place affordable BUY levels; SELL levels are added only when free base-asset inventory exists.
- If no grid level is affordable, the symbol is paused and an insufficient-funds risk event records the available-balance details.
- Non-insufficient order placement failures are handled gracefully too: the symbol is paused and alert details are sent to Telegram.
- Stop-loss/take-profit bands (`PER_SYMBOL_STOP_LOSS_PCT` / `PER_SYMBOL_TAKE_PROFIT_PCT`) are measured from a per-symbol **risk anchor price**, not the grid's `center_price`. The anchor is set once when a symbol starts trading and survives grid recentering, so a sustained trend can still trip the band instead of the grid perpetually recentering out from under it. The anchor only resets after a band liquidation (fresh reference point for the next position) or a full `/start_fresh`. Triggering a band cancels open grid orders **and** sells the full free base-asset balance at market, so the band actually closes the position instead of only stopping new orders. Dust balances too small to sell are left untouched and logged as `band_liquidation_skipped_dust`. A failed liquidation attempt is logged as `band_liquidation_error` and alerted, but does not block the pause.
- Account-equity P/L snapshots are available in `testnet/live` modes via `/pnl` and included in `/status`.
- The fallback from stale maker orders to taker orders should be added next as a latency- and slippage-aware policy.
- Fill-by-fill realized PnL tracking is not wired yet; daily report field "Realized PnL today" remains 0 unless explicitly recorded.

## 9) Safety reconciliation behavior

- In `testnet/live`, the bot can run reconciliation checks against exchange balances/equity.
- If drift exceeds thresholds, the bot performs a global halt and records a `reconciliation_breach` risk event.
- After a reconciliation breach, manual `/resume` remains blocked until the configured number of consecutive clean cycles is reached.
- Threshold and recovery settings are configured via:
   - `RECONCILIATION_ENABLED`
   - `RECONCILIATION_QTY_DRIFT_PCT` (default `0.001` = 0.1%)
   - `RECONCILIATION_EQUITY_DRIFT_USDT` (default `5`)
   - `RECONCILIATION_CLEAN_CYCLES_REQUIRED` (default `3`)
   - `RECONCILIATION_CHECK_ON_HALT` (default `true`)
