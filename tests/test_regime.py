from __future__ import annotations

import random
import unittest

from gridbot.regime import (
    REGIME_RANGING,
    REGIME_TRENDING,
    InsufficientDataError,
    RegimeThresholds,
    classify_regime,
    compute_adx,
    compute_hurst,
    compute_realized_vol_pct,
)


def _candles(closes: list[float]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for i, close in enumerate(closes):
        prev = closes[i - 1] if i > 0 else close
        out.append(
            {
                "open_time": float(i),
                "open": prev,
                "high": max(close, prev) * 1.002,
                "low": min(close, prev) * 0.998,
                "close": close,
                "volume": 1.0,
            }
        )
    return out


def _trending_closes() -> list[float]:
    random.seed(42)
    price = 100.0
    closes: list[float] = []
    for _ in range(120):
        price *= 1 + 0.004 + random.uniform(-0.002, 0.002)
        closes.append(price)
    return closes


def _ranging_closes() -> list[float]:
    random.seed(7)
    level = 100.0
    value = 100.0
    closes: list[float] = []
    for _ in range(120):
        value += -0.5 * (value - level) + random.uniform(-1.5, 1.5)
        closes.append(value)
    return closes


DEFAULT_THRESHOLDS = RegimeThresholds(
    adx_enter=25.0,
    adx_exit=18.0,
    hurst_enter=0.55,
    hurst_exit=0.50,
    min_vol_pct=0.05,
    max_vol_pct=50.0,
)


class RealizedVolTests(unittest.TestCase):
    def test_flat_series_is_zero_vol(self) -> None:
        self.assertAlmostEqual(compute_realized_vol_pct([100.0] * 10), 0.0, places=9)

    def test_known_series(self) -> None:
        vol = compute_realized_vol_pct([100.0, 110.0, 100.0])
        self.assertGreater(vol, 9.0)
        self.assertLess(vol, 10.0)

    def test_too_few_points_raises(self) -> None:
        with self.assertRaises(InsufficientDataError):
            compute_realized_vol_pct([100.0])


class AdxTests(unittest.TestCase):
    def test_trend_has_high_adx(self) -> None:
        candles = _candles(_trending_closes())
        adx = compute_adx(
            [c["high"] for c in candles],
            [c["low"] for c in candles],
            [c["close"] for c in candles],
        )
        self.assertGreater(adx, 25.0)

    def test_range_has_low_adx(self) -> None:
        candles = _candles(_ranging_closes())
        adx = compute_adx(
            [c["high"] for c in candles],
            [c["low"] for c in candles],
            [c["close"] for c in candles],
        )
        self.assertLess(adx, 25.0)

    def test_too_few_candles_raises(self) -> None:
        with self.assertRaises(InsufficientDataError):
            compute_adx([1.0] * 5, [1.0] * 5, [1.0] * 5)


class HurstTests(unittest.TestCase):
    def test_trend_is_persistent(self) -> None:
        self.assertGreater(compute_hurst(_trending_closes()), 0.55)

    def test_range_is_mean_reverting(self) -> None:
        self.assertLess(compute_hurst(_ranging_closes()), 0.50)


class ClassifyRegimeTests(unittest.TestCase):
    def test_trending_series_classified_trending(self) -> None:
        assessment = classify_regime(_candles(_trending_closes()), DEFAULT_THRESHOLDS, None)
        self.assertEqual(assessment.verdict, REGIME_TRENDING)

    def test_ranging_series_classified_ranging(self) -> None:
        assessment = classify_regime(_candles(_ranging_closes()), DEFAULT_THRESHOLDS, None)
        self.assertEqual(assessment.verdict, REGIME_RANGING)

    def test_hysteresis_keeps_trending_in_the_band(self) -> None:
        thresholds = RegimeThresholds(
            adx_enter=25.0,
            adx_exit=8.0,
            hurst_enter=0.55,
            hurst_exit=0.50,
            min_vol_pct=0.05,
            max_vol_pct=50.0,
        )
        candles = _candles(_ranging_closes())
        from_none = classify_regime(candles, thresholds, None)
        from_trend = classify_regime(candles, thresholds, REGIME_TRENDING)
        self.assertEqual(from_none.verdict, REGIME_RANGING)
        self.assertEqual(from_trend.verdict, REGIME_TRENDING)

    def test_ranging_resumes_when_below_exit(self) -> None:
        assessment = classify_regime(_candles(_ranging_closes()), DEFAULT_THRESHOLDS, REGIME_TRENDING)
        self.assertEqual(assessment.verdict, REGIME_RANGING)

    def test_low_vol_vetoed_to_trending(self) -> None:
        thresholds = RegimeThresholds(
            adx_enter=25.0,
            adx_exit=18.0,
            hurst_enter=0.55,
            hurst_exit=0.50,
            min_vol_pct=5.0,
            max_vol_pct=50.0,
        )
        assessment = classify_regime(_candles(_ranging_closes()), thresholds, None)
        self.assertEqual(assessment.verdict, REGIME_TRENDING)
        self.assertIn("vol", assessment.reason)

    def test_high_vol_vetoed_to_trending(self) -> None:
        thresholds = RegimeThresholds(
            adx_enter=25.0,
            adx_exit=18.0,
            hurst_enter=0.55,
            hurst_exit=0.50,
            min_vol_pct=0.01,
            max_vol_pct=0.05,
        )
        assessment = classify_regime(_candles(_ranging_closes()), thresholds, None)
        self.assertEqual(assessment.verdict, REGIME_TRENDING)
        self.assertIn("vol", assessment.reason)


if __name__ == "__main__":
    unittest.main()
