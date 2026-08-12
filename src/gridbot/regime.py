from __future__ import annotations

import math
from dataclasses import dataclass


ADX_PERIOD = 14

# Verdict values.
REGIME_RANGING = "ranging"
REGIME_TRENDING = "trending"


@dataclass(frozen=True)
class RegimeThresholds:
    adx_enter: float
    adx_exit: float
    hurst_enter: float
    hurst_exit: float
    min_vol_pct: float
    max_vol_pct: float


@dataclass(frozen=True)
class RegimeMetrics:
    adx: float
    hurst: float
    realized_vol_pct: float
    samples: int


@dataclass(frozen=True)
class RegimeAssessment:
    verdict: str
    metrics: RegimeMetrics
    reason: str


class InsufficientDataError(ValueError):
    """Raised when there are not enough candles to assess a regime."""


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Wilder's smoothing (used by ADX). Returns one value per step after the
    first `period` seed values are averaged."""
    if len(values) < period:
        return []
    smoothed: list[float] = []
    seed = sum(values[:period])
    smoothed.append(seed)
    for value in values[period:]:
        seed = seed - (seed / period) + value
        smoothed.append(seed)
    return smoothed


def compute_adx(highs: list[float], lows: list[float], closes: list[float], period: int = ADX_PERIOD) -> float:
    """Average Directional Index (Wilder). Measures trend strength (0-100),
    independent of direction."""
    n = len(closes)
    if n < 2 * period + 1:
        raise InsufficientDataError(f"ADX needs at least {2 * period + 1} candles, got {n}")

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr: list[float] = []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        true_range = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr.append(true_range)

    tr_smoothed = _wilder_smooth(tr, period)
    plus_dm_smoothed = _wilder_smooth(plus_dm, period)
    minus_dm_smoothed = _wilder_smooth(minus_dm, period)

    dx_values: list[float] = []
    for tr_s, plus_s, minus_s in zip(tr_smoothed, plus_dm_smoothed, minus_dm_smoothed):
        if tr_s == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100.0 * (plus_s / tr_s)
        minus_di = 100.0 * (minus_s / tr_s)
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_values.append(0.0)
            continue
        dx_values.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dx_values) < period:
        raise InsufficientDataError("Not enough smoothed samples to compute ADX")

    return sum(dx_values[-period:]) / period


def compute_hurst(closes: list[float]) -> float:
    """Rescaled-range (R/S) estimate of the Hurst exponent.

    ~0.5 = random walk, < 0.5 = mean-reverting (grid-friendly),
    > 0.5 = trending/persistent (grid-hostile)."""
    n = len(closes)
    if n < 16:
        raise InsufficientDataError(f"Hurst needs at least 16 candles, got {n}")

    log_returns: list[float] = []
    for i in range(1, n):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0 or curr <= 0:
            log_returns.append(0.0)
        else:
            log_returns.append(math.log(curr / prev))

    series_len = len(log_returns)
    max_chunk = series_len // 2
    chunk_sizes: list[int] = []
    size = 8
    while size <= max_chunk:
        chunk_sizes.append(size)
        size *= 2
    if not chunk_sizes:
        raise InsufficientDataError("Series too short for Hurst R/S analysis")

    log_sizes: list[float] = []
    log_rs: list[float] = []
    for chunk in chunk_sizes:
        rs_values: list[float] = []
        num_chunks = series_len // chunk
        for c in range(num_chunks):
            segment = log_returns[c * chunk : (c + 1) * chunk]
            mean = sum(segment) / chunk
            deviations = [x - mean for x in segment]
            cumulative = []
            running = 0.0
            for d in deviations:
                running += d
                cumulative.append(running)
            spread = max(cumulative) - min(cumulative)
            variance = sum(d * d for d in deviations) / chunk
            std = math.sqrt(variance)
            if std > 0 and spread > 0:
                rs_values.append(spread / std)
        if rs_values:
            avg_rs = sum(rs_values) / len(rs_values)
            if avg_rs > 0:
                log_sizes.append(math.log(chunk))
                log_rs.append(math.log(avg_rs))

    if len(log_sizes) < 2:
        raise InsufficientDataError("Not enough R/S points to estimate Hurst")

    # Slope of log(R/S) vs log(chunk size) via least squares = Hurst exponent.
    count = len(log_sizes)
    mean_x = sum(log_sizes) / count
    mean_y = sum(log_rs) / count
    numerator = sum((log_sizes[i] - mean_x) * (log_rs[i] - mean_y) for i in range(count))
    denominator = sum((log_sizes[i] - mean_x) ** 2 for i in range(count))
    if denominator == 0:
        raise InsufficientDataError("Degenerate R/S regression for Hurst")
    return numerator / denominator


def compute_realized_vol_pct(closes: list[float]) -> float:
    """Standard deviation of simple returns, expressed as a percentage."""
    n = len(closes)
    if n < 2:
        raise InsufficientDataError("Realized vol needs at least 2 candles")
    returns: list[float] = []
    for i in range(1, n):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        returns.append((closes[i] - prev) / prev)
    if len(returns) < 2:
        raise InsufficientDataError("Not enough valid returns for realized vol")
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance) * 100.0


def compute_metrics(candles: list[dict[str, float]]) -> RegimeMetrics:
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    return RegimeMetrics(
        adx=compute_adx(highs, lows, closes),
        hurst=compute_hurst(closes),
        realized_vol_pct=compute_realized_vol_pct(closes),
        samples=len(candles),
    )


def classify_regime(
    candles: list[dict[str, float]],
    thresholds: RegimeThresholds,
    previous_verdict: str | None,
) -> RegimeAssessment:
    """Classify a symbol as ranging or trending using ADX + Hurst with
    hysteresis, plus a realized-volatility sanity band.

    `previous_verdict` carries the hysteresis state so enter/exit use different
    thresholds and the verdict does not flap on a single boundary."""
    metrics = compute_metrics(candles)

    # Volatility sanity band vetoes unsuitable symbols regardless of trend.
    if metrics.realized_vol_pct < thresholds.min_vol_pct:
        return RegimeAssessment(
            REGIME_TRENDING,
            metrics,
            f"vol {metrics.realized_vol_pct:.3f}% < min {thresholds.min_vol_pct:.3f}%",
        )
    if metrics.realized_vol_pct > thresholds.max_vol_pct:
        return RegimeAssessment(
            REGIME_TRENDING,
            metrics,
            f"vol {metrics.realized_vol_pct:.3f}% > max {thresholds.max_vol_pct:.3f}%",
        )

    was_trending = previous_verdict == REGIME_TRENDING
    if was_trending:
        # Require both metrics to fall below the exit thresholds to resume.
        cleared = metrics.adx < thresholds.adx_exit and metrics.hurst < thresholds.hurst_exit
        if cleared:
            return RegimeAssessment(
                REGIME_RANGING,
                metrics,
                f"adx {metrics.adx:.1f}<{thresholds.adx_exit} and hurst "
                f"{metrics.hurst:.3f}<{thresholds.hurst_exit}",
            )
        return RegimeAssessment(
            REGIME_TRENDING,
            metrics,
            f"still trending (adx {metrics.adx:.1f}, hurst {metrics.hurst:.3f})",
        )

    # Currently ranging/unknown: require both metrics above the enter thresholds
    # to flip to trending.
    trending = metrics.adx > thresholds.adx_enter and metrics.hurst > thresholds.hurst_enter
    if trending:
        return RegimeAssessment(
            REGIME_TRENDING,
            metrics,
            f"adx {metrics.adx:.1f}>{thresholds.adx_enter} and hurst "
            f"{metrics.hurst:.3f}>{thresholds.hurst_enter}",
        )
    return RegimeAssessment(
        REGIME_RANGING,
        metrics,
        f"ranging (adx {metrics.adx:.1f}, hurst {metrics.hurst:.3f})",
    )
