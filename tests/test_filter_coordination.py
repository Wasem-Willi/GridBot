from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import Mock

from gridbot.ai_filter import AI_ACTION_BUY_ONLY, AI_ACTION_PAUSE
from gridbot.grid_engine import GridLevel, GridPlan
from gridbot.main import _plan_for_regime_resume
from gridbot.regime import REGIME_RANGING


def _grid_plan() -> GridPlan:
    return GridPlan(
        symbol="BTCUSDT",
        center_price=100.0,
        lower_bound=99.0,
        upper_bound=101.0,
        levels=[
            GridLevel(side="BUY", price=99.0, quantity=0.1),
            GridLevel(side="SELL", price=101.0, quantity=0.1),
        ],
    )


class RegimeResumeCoordinationTests(unittest.TestCase):
    def test_ai_pause_prevents_regime_resume(self) -> None:
        ai_filter = Mock()
        ai_filter.active = True
        ai_filter.last_action.return_value = AI_ACTION_PAUSE
        now = datetime.now(UTC)

        plan = _plan_for_regime_resume(
            ai_filter,
            _grid_plan(),
            "BTCUSDT",
            now,
            100.0,
            REGIME_RANGING,
        )

        self.assertIsNone(plan)
        ai_filter.refresh.assert_called_once_with(
            "BTCUSDT",
            now,
            price=100.0,
            current_position_paused=True,
            regime_verdict=REGIME_RANGING,
            force=True,
        )

    def test_ai_direction_limits_resumed_grid(self) -> None:
        ai_filter = Mock()
        ai_filter.active = True
        ai_filter.last_action.return_value = AI_ACTION_BUY_ONLY

        plan = _plan_for_regime_resume(
            ai_filter,
            _grid_plan(),
            "BTCUSDT",
            datetime.now(UTC),
            100.0,
            REGIME_RANGING,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual([level.side for level in plan.levels], ["BUY"])


if __name__ == "__main__":
    unittest.main()
