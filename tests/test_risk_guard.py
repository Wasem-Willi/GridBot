from __future__ import annotations

import unittest

from gridbot.risk_guard import check_symbol_band


class CheckSymbolBandTests(unittest.TestCase):
    def test_no_trigger_within_band(self) -> None:
        result = check_symbol_band(
            anchor_price=100.0, current_price=101.0, stop_loss_pct=0.03, take_profit_pct=0.04
        )
        self.assertIsNone(result)

    def test_stop_loss_triggers_at_lower_bound(self) -> None:
        result = check_symbol_band(
            anchor_price=100.0, current_price=97.0, stop_loss_pct=0.03, take_profit_pct=0.04
        )
        self.assertEqual(result, "stop_loss")

    def test_take_profit_triggers_at_upper_bound(self) -> None:
        result = check_symbol_band(
            anchor_price=100.0, current_price=104.0, stop_loss_pct=0.03, take_profit_pct=0.04
        )
        self.assertEqual(result, "take_profit")

    def test_measures_from_anchor_price_not_a_different_reference(self) -> None:
        # A price that would be within band relative to 100 but breaches stop_loss
        # relative to a stale anchor of 105 must trigger off the anchor, not 100.
        result = check_symbol_band(
            anchor_price=105.0, current_price=101.0, stop_loss_pct=0.03, take_profit_pct=0.04
        )
        self.assertEqual(result, "stop_loss")
