from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import Mock

from gridbot.alerts import ControlCommand
from gridbot.main import (
    AIFilterController,
    NOTIFICATION_CATEGORIES,
    OrderPlacementError,
    RegimeController,
    _apply_control_commands,
    _build_notify_status_text,
    _handle_order_placement_error,
    _notify_enabled,
    _refresh_symbols,
)


def _store(overrides: dict[str, str] | None = None) -> Mock:
    """A store mock. get_state returns the given override for
    notify_<category> keys, or None (meaning "fall back to cfg default")."""
    overrides = overrides or {}
    store = Mock()
    store.get_state.side_effect = lambda key: overrides.get(key)
    return store


class NotifyEnabledTests(unittest.TestCase):
    def test_falls_back_to_cfg_default_when_no_override(self) -> None:
        cfg = Mock(notify_liquidation=True)
        self.assertTrue(_notify_enabled(_store(), cfg, "liquidation"))

        cfg = Mock(notify_liquidation=False)
        self.assertFalse(_notify_enabled(_store(), cfg, "liquidation"))

    def test_persisted_override_wins_over_cfg_default(self) -> None:
        cfg = Mock(notify_liquidation=True)
        store = _store({"notify_liquidation": "0"})
        self.assertFalse(_notify_enabled(store, cfg, "liquidation"))

        cfg = Mock(notify_liquidation=False)
        store = _store({"notify_liquidation": "1"})
        self.assertTrue(_notify_enabled(store, cfg, "liquidation"))


class NotifyStatusTextTests(unittest.TestCase):
    def test_lists_every_category_with_current_state(self) -> None:
        cfg = Mock(
            notify_ai_decisions=True,
            notify_regime=True,
            notify_liquidation=False,
            notify_order_errors=True,
            notify_risk_halts=True,
            notify_symbol_refresh=True,
            notify_daily_summary=True,
        )
        store = _store({"notify_regime": "0"})

        text = _build_notify_status_text(store, cfg)

        self.assertIn("[ON] ai_decisions", text)
        self.assertIn("[OFF] regime", text)
        self.assertIn("[OFF] liquidation", text)
        for category in NOTIFICATION_CATEGORIES:
            self.assertIn(category, text)


class ApplyControlCommandsNotifyTests(unittest.TestCase):
    def _run(self, store: Mock, alerter: Mock, cfg: Mock, commands: list[ControlCommand]) -> None:
        alerter.poll_commands.return_value = (commands, 1)
        _apply_control_commands(
            alerter,
            store,
            cfg,
            False,
            Mock(return_value=""),
            Mock(return_value=""),
            Mock(return_value=(True, "")),
        )

    def test_notify_status_command_sends_status_text(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock(
            notify_ai_decisions=True,
            notify_regime=True,
            notify_liquidation=True,
            notify_order_errors=True,
            notify_risk_halts=True,
            notify_symbol_refresh=True,
            notify_daily_summary=True,
        )

        self._run(store, alerter, cfg, [ControlCommand(name="notify_status")])

        alerter.send.assert_called_once()
        self.assertIn("live notification toggles", alerter.send.call_args.args[0])

    def test_notify_off_persists_override_and_confirms(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock()

        self._run(store, alerter, cfg, [ControlCommand(name="notify_off", arg="liquidation")])

        store.set_state.assert_any_call("notify_liquidation", "0")
        alerter.send.assert_called_once_with("Notifications for 'liquidation' turned OFF.")

    def test_notify_on_persists_override_and_confirms(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock()

        self._run(store, alerter, cfg, [ControlCommand(name="notify_on", arg="liquidation")])

        store.set_state.assert_any_call("notify_liquidation", "1")
        alerter.send.assert_called_once_with("Notifications for 'liquidation' turned ON.")

    def test_unknown_category_reports_error_without_persisting(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock()

        self._run(store, alerter, cfg, [ControlCommand(name="notify_off", arg="bogus")])

        for call in store.set_state.call_args_list:
            self.assertFalse(str(call.args[0]).startswith("notify_bogus"))
        alerter.send.assert_called_once()
        self.assertIn("Unknown notify category", alerter.send.call_args.args[0])


class NotificationFlagTests(unittest.TestCase):
    def test_symbol_refresh_alert_suppressed_when_disabled(self) -> None:
        cfg = Mock(max_active_symbols=5, notify_symbol_refresh=False)
        exchange = Mock()
        alerter = Mock()
        store = _store()
        from gridbot import main as main_module

        main_module.select_symbols = Mock(return_value=[])

        _refresh_symbols(cfg, exchange, set(), alerter, store)

        alerter.send.assert_not_called()

    def test_symbol_refresh_alert_sent_when_enabled(self) -> None:
        cfg = Mock(max_active_symbols=5, notify_symbol_refresh=True)
        exchange = Mock()
        alerter = Mock()
        store = _store()
        from gridbot import main as main_module

        main_module.select_symbols = Mock(return_value=[])

        _refresh_symbols(cfg, exchange, set(), alerter, store)

        alerter.send.assert_called_once()

    def test_order_placement_error_alert_suppressed_when_disabled(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock(notify_order_errors=False)

        _handle_order_placement_error(store, alerter, "BTCUSDT", OrderPlacementError("BTCUSDT", "boom"), cfg)

        store.set_symbol_paused.assert_called_once_with("BTCUSDT", True, "order_placement_error")
        alerter.send.assert_not_called()

    def test_order_placement_error_alert_sent_when_enabled(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock(notify_order_errors=True)

        _handle_order_placement_error(store, alerter, "BTCUSDT", OrderPlacementError("BTCUSDT", "boom"), cfg)

        alerter.send.assert_called_once()

    def test_regime_pause_resume_alerts_suppressed_when_disabled(self) -> None:
        controller = object.__new__(RegimeController)
        controller._cfg = Mock(notify_regime=False)
        controller._store = _store()
        controller._alerter = Mock()
        controller.pauses_today = 0
        controller.resumes_today = 0

        controller.note_pause("BTCUSDT")
        controller.note_resume("BTCUSDT")

        controller._alerter.send.assert_not_called()
        self.assertEqual(controller.pauses_today, 1)
        self.assertEqual(controller.resumes_today, 1)

    def test_regime_pause_resume_alerts_sent_when_enabled(self) -> None:
        controller = object.__new__(RegimeController)
        controller._cfg = Mock(notify_regime=True)
        controller._store = _store()
        controller._alerter = Mock()
        controller.pauses_today = 0
        controller.resumes_today = 0

        controller.note_pause("BTCUSDT")
        controller.note_resume("BTCUSDT")

        self.assertEqual(controller._alerter.send.call_count, 2)

    def test_ai_decision_alert_suppressed_when_disabled(self) -> None:
        controller = object.__new__(AIFilterController)
        controller._cfg = Mock(ai_filter_mode="active", notify_ai_decisions=False, ai_recompute_seconds=60)
        controller._store = _store()
        controller._alerter = Mock()
        controller._client = Mock()
        controller._client.decide.return_value = Mock(action="BOTH", confidence=0.8, reason="ranging")
        controller._state = {"BTCUSDT": {"action": "PAUSE", "last_ts": None}}

        controller.refresh(
            "BTCUSDT",
            datetime.now(UTC),
            price=100.0,
            current_position_paused=False,
            regime_verdict=None,
            force=True,
        )

        controller._alerter.send.assert_not_called()

    def test_ai_decision_alert_sent_when_enabled(self) -> None:
        controller = object.__new__(AIFilterController)
        controller._cfg = Mock(ai_filter_mode="active", notify_ai_decisions=True, ai_recompute_seconds=60)
        controller._store = _store()
        controller._alerter = Mock()
        controller._client = Mock()
        controller._client.decide.return_value = Mock(action="BOTH", confidence=0.8, reason="ranging")
        controller._state = {"BTCUSDT": {"action": "PAUSE", "last_ts": None}}

        controller.refresh(
            "BTCUSDT",
            datetime.now(UTC),
            price=100.0,
            current_position_paused=False,
            regime_verdict=None,
            force=True,
        )

        controller._alerter.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
