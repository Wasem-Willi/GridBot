# ADR 0001: AI Regime Filter for GridBot

- Status: Accepted
- Date: 2026-08-13
- Deciders: Bot owner

## Context

GridBot is a deterministic, rule-based grid trading bot (Binance spot). Every
decision today is a fixed formula:

- `selector.py` ranks USDT pairs by liquidity/spread/mid-range volatility.
- `grid_engine.py` builds a symmetric grid around the current price.
- `main.py` recenters when price leaves the grid and pauses on fixed
  stop-loss / take-profit bands.

Grid bots make money in range-bound markets and bleed money when the market
trends hard against the grid. Nothing in the bot currently distinguishes a
range from a trend, and no historical candle (kline) data is fetched at all.

The owner asked to "implement AI to make better buy/sell decisions." This ADR
records the design agreed through a structured design interview.

## Decision

Add a per-symbol **regime filter**: a statistical classifier that decides
*when* the grid should trade a symbol versus pause it. It does **not** predict
price direction and is **not** a trained ML model.

### Design decisions (Q1-Q13)

1. **Target (Q1):** Regime/timing filter — decide *when* to run the grid, the
   highest-leverage, lowest-risk place to add intelligence to a grid bot.
2. **Kind of "AI" (Q2):** Statistical/indicator classifier (ADX, Hurst
   exponent, realized volatility). Explainable, backtestable, no training data,
   no overfitting risk. Can be upgraded to a learned model later once verdicts
   are logged.
3. **Scope (Q3):** Per-symbol. Fits the existing per-symbol selector and grid.
   A global (BTC/market-wide) kill-switch is deferred to a later version.
4. **Action on bad regime (Q4):** Block new entry **and** pause & flatten an
   existing grid (cancel open orders), reusing the existing
   `set_symbol_paused` + `cancel_all_orders` machinery.
5. **Data (Q5):** 15-minute candles, ~96 bars (~24h lookback). Enough samples
   for ADX(14)/Hurst; filters 1-minute noise; reacts within a couple hours.
6. **Cadence (Q6):** Recompute every 5 minutes and on each symbol refresh, with
   **hysteresis** (separate enter/exit thresholds) to prevent pause/resume
   flapping and the fee churn it causes.
7. **Decision rule (Q7):** ADX as the primary gate, Hurst as confirmation,
   realized-vol as a sanity band:
   - Pause when `ADX > ADX_ENTER (~25)` **and** `Hurst > HURST_ENTER (~0.55)`.
   - Resume when `ADX < ADX_EXIT (~18)` **and** `Hurst < HURST_EXIT (~0.50)`.
   - Vol band vetoes symbols whose realized vol is too low (can't earn the
     spread) or too high (dangerous).
   These numbers are starting points to be tuned during shadow evaluation.
8. **Re-entry (Q8):** Auto-resume when the regime clears, tagged with a distinct
   `pause_reason = "trend_regime"`; rebuild the grid fresh at the current price
   (the old center is stale after a trend).
9. **Pipeline placement (Q9):** Regime-check the top-N selected shortlist and
   currently-active symbols only (~5-10 kline calls/cycle) — not every ranked
   candidate (which would be hundreds of API calls).
10. **Dependencies (Q10):** Pure-Python `regime.py`, zero new dependencies,
    unit-tested. Keeps the bot's tiny, auditable footprint for real-money code.
11. **Validation (Q11):** Shadow/log-only mode first. A
    `REGIME_FILTER_MODE = off | shadow | active` flag; in `shadow`, the
    classifier runs and logs verdicts but does **not** act. Flip to `active`
    once verdicts demonstrably correlate with grid-hurting trends.
12. **Config (Q12):** Expose thresholds, mode, and cadence in `.env`; hardcode
    the indicator periods (ADX=14, Hurst window internals).
13. **Observability (Q13):** Shadow mode logs silently (`regime_verdict` risk
    event, visible via `/transitions`). Active mode sends a deduplicated
    Telegram alert only on a flip (ranging->trend pause, trend->ranging resume),
    and the daily summary gains a regime pause/resume count.

## Consequences

**Positive**

- Directly addresses the main way grid bots lose money (trending into a grid).
- Explainable and safe-by-default (ships in `shadow`, acts only after proof).
- No new dependencies; small, auditable, testable surface.
- Reuses existing pause/flatten/alert/logging machinery.

**Negative / trade-offs**

- Adds a new kline data path and ~5-10 API calls per cycle.
- Threshold tuning is manual until an offline backtester is built (deferred).
- Per-symbol only: a fast market-wide crash may be caught slower than a
  dedicated global filter would (deferred to a later version).

## Deferred (future work)

- Global/market-wide regime kill-switch (BTC dumping).
- Adaptive grids (widen/skew) instead of only pause.
- Multi-timeframe regime confirmation.
- Offline grid backtest harness comparing with/without the filter.
- Optional learned model trained on the logged `regime_verdict` data.
- `/regime` Telegram command for on-demand current verdicts.
