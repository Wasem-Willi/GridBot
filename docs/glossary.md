# GridBot Glossary

Shared vocabulary for the bot and its AI regime filter.

## Grid trading terms

- **Grid**: a ladder of buy orders below and sell orders above a center price.
  The bot profits from price oscillating within the ladder, repeatedly buying
  low and selling high across the levels.
- **Center price**: the reference price a grid is built around.
- **Recenter**: rebuild the grid around the current price after price leaves the
  grid bounds.
- **Band pause**: a symbol paused because price hit its fixed per-symbol
  stop-loss or take-profit band (a loss/target event).

## Regime filter terms

- **Regime**: the qualitative state of a market for grid purposes — **ranging**
  (mean-reverting, oscillating; good for grids) vs **trending** (persistent
  directional move; bad for grids).
- **Regime filter**: the per-symbol statistical classifier that decides whether
  a symbol is in a grid-friendly (ranging) or grid-hostile (trending) regime.
  It decides *when* to trade, not *which direction* price will go.
- **ADX (Average Directional Index)**: a 0-100 indicator of **trend strength**
  (not direction). Low ADX = weak/ranging; high ADX = strong trend. Computed
  over a standard 14-period window. Primary gate of the filter.
- **Hurst exponent**: a measure of a series' persistence. `~0.5` = random walk,
  `< 0.5` = mean-reverting (grid-friendly), `> 0.5` = trending/persistent
  (grid-hostile). Used to confirm ADX and avoid false positives in choppy
  markets.
- **Realized volatility**: recent price variability estimated from candle
  returns. Used as a **sanity band**: too low = can't earn the spread; too high
  = dangerous. Vetoes unsuitable symbols.
- **Hysteresis**: using separate, spread-apart thresholds to *enter* vs *exit* a
  state (e.g. pause when ADX > 25, resume only when ADX < 18). Prevents rapid
  flip-flopping (flapping) when a metric hovers on a single threshold, which
  would otherwise churn orders and fees.
- **trend_regime pause**: a symbol paused by the regime filter (distinct from a
  band pause), recorded with `pause_reason = "trend_regime"` so it can be
  separated from loss-driven pauses in logs and reporting.

## Operational terms

- **Regime filter mode**: `off` (disabled), `shadow` (computes and logs verdicts
  but does not act), or `active` (verdicts pause/flatten/block trading).
- **Shadow mode**: safe evaluation mode where the filter records what it *would*
  do (`regime_verdict` risk events) without affecting trading, so its quality
  can be judged on live data risk-free before activation.
- **regime_verdict event**: a logged risk event capturing a symbol's regime
  computation (ADX, Hurst, realized vol, verdict) for later analysis.
- **Kline**: a Binance OHLCV candlestick. The filter fetches 15m klines
  (~96 bars) per checked symbol.
