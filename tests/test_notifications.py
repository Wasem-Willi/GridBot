from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import ANY, Mock, patch

from gridbot.alerts import ControlCommand
from gridbot.main import (
    AIFilterController,
    NOTIFICATION_CATEGORIES,
    OrderPlacementError,
    RegimeController,
    _apply_control_commands,
    _build_ai_ask_context,
    _build_notify_status_text,
    _handle_order_placement_error,
    _make_ai_ask_provider,
    _notify_enabled,
    _refresh_symbols,
    _responsive_wait,
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
            Mock(return_value=""),
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

    def test_notify_off_all_disables_every_category(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock()

        self._run(store, alerter, cfg, [ControlCommand(name="notify_off", arg="all")])

        for category in NOTIFICATION_CATEGORIES:
            store.set_state.assert_any_call(f"notify_{category}", "0")
        alerter.send.assert_called_once_with("All notification categories turned OFF.")

    def test_notify_on_all_enables_every_category(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock()

        self._run(store, alerter, cfg, [ControlCommand(name="notify_on", arg="all")])

        for category in NOTIFICATION_CATEGORIES:
            store.set_state.assert_any_call(f"notify_{category}", "1")
        alerter.send.assert_called_once_with("All notification categories turned ON.")

    def test_notify_off_all_is_case_insensitive(self) -> None:
        store = _store()
        alerter = Mock()
        cfg = Mock()

        self._run(store, alerter, cfg, [ControlCommand(name="notify_off", arg="ALL")])

        for category in NOTIFICATION_CATEGORIES:
            store.set_state.assert_any_call(f"notify_{category}", "0")


class ApplyControlCommandsAskTests(unittest.TestCase):
    def _run(self, store: Mock, alerter: Mock, ai_ask_provider: Mock, commands: list[ControlCommand]) -> None:
        store.get_recent_risk_events.return_value = []
        alerter.poll_commands.return_value = (commands, 1)
        _apply_control_commands(
            alerter,
            store,
            Mock(),
            False,
            Mock(return_value=""),
            Mock(return_value=""),
            Mock(return_value=(True, "")),
            ai_ask_provider,
        )

    def test_ask_with_question_sends_provider_answer_and_logs_event(self) -> None:
        store = _store()
        alerter = Mock()
        ai_ask_provider = Mock(return_value="42 is the answer.")

        self._run(store, alerter, ai_ask_provider, [ControlCommand(name="ask", arg="What is the meaning of life?")])

        ai_ask_provider.assert_called_once_with("What is the meaning of life?", ANY)
        alerter.send.assert_called_once_with("42 is the answer.")
        store.log_risk_event.assert_called_once_with(
            "ai_chat",
            None,
            {"question": "What is the meaning of life?", "success": 1, "source": "telegram"},
        )

    def test_ask_without_question_shows_usage_and_does_not_call_provider(self) -> None:
        store = _store()
        alerter = Mock()
        ai_ask_provider = Mock()

        self._run(store, alerter, ai_ask_provider, [ControlCommand(name="ask", arg="")])

        ai_ask_provider.assert_not_called()
        alerter.send.assert_called_once_with("Usage: /ask <question>")
        store.log_risk_event.assert_not_called()

    def test_ask_provider_failure_sends_error_message(self) -> None:
        store = _store()
        alerter = Mock()
        ai_ask_provider = Mock(side_effect=ValueError("OpenAI request failed (status=401): invalid api key"))

        self._run(store, alerter, ai_ask_provider, [ControlCommand(name="ask", arg="hello")])

        alerter.send.assert_called_once_with(
            "AI chat failed: OpenAI request failed (status=401): invalid api key"
        )
        store.log_risk_event.assert_called_once_with(
            "ai_chat",
            None,
            {"question": "hello", "success": 0, "source": "telegram"},
        )


class BuildAiAskContextTests(unittest.TestCase):
    def test_includes_status_pnl_and_transitions(self) -> None:
        store = _store()
        event = Mock(created_at="2026-08-17T12:00:00", event_type="band_liquidation", symbol="BTCUSDT", details={"band": "stop_loss"})
        store.get_recent_risk_events.return_value = [event]

        context = _build_ai_ask_context(store, False, Mock(return_value="P/L: +1.23 USDT"))

        self.assertIn("Bot status: RUNNING", context)
        self.assertIn("P/L: +1.23 USDT", context)
        self.assertIn("band_liquidation", context)
        self.assertIn("BTCUSDT", context)

    def test_reports_halted_status(self) -> None:
        store = _store()
        store.get_recent_risk_events.return_value = []

        context = _build_ai_ask_context(store, True, Mock(return_value=""))

        self.assertIn("Bot status: STOPPED", context)

    def test_pnl_provider_failure_is_reported_instead_of_raising(self) -> None:
        store = _store()
        store.get_recent_risk_events.return_value = []

        def _failing_pnl() -> str:
            raise ValueError("exchange unreachable")

        context = _build_ai_ask_context(store, False, _failing_pnl)

        self.assertIn("P/L snapshot unavailable", context)
        self.assertIn("exchange unreachable", context)


class MakeAiAskProviderTests(unittest.TestCase):
    def test_missing_api_key_returns_not_configured_message_without_calling_openai(self) -> None:
        cfg = Mock(ai_api_key="", ai_model="gpt-4o-mini", ai_chat_timeout_seconds=20)

        provider = _make_ai_ask_provider(cfg)

        self.assertEqual(
            provider("anything", "some context"),
            "AI chat is not configured: set OPENAI_API_KEY in .env to enable /ask.",
        )

    @patch("gridbot.main.ask_freeform")
    def test_forwards_question_and_context_to_ask_freeform_with_cfg_settings(self, ask_freeform: Mock) -> None:
        ask_freeform.return_value = "Grid trading works best in ranging markets."
        cfg = Mock(ai_api_key="sk-test", ai_model="gpt-4o-mini", ai_chat_timeout_seconds=20)

        provider = _make_ai_ask_provider(cfg)
        answer = provider("How does grid trading work?", "Bot status: RUNNING")

        self.assertEqual(answer, "Grid trading works best in ranging markets.")
        ask_freeform.assert_called_once_with(
            "sk-test",
            "gpt-4o-mini",
            20,
            ANY,
            "How does grid trading work?",
            context="Bot status: RUNNING",
        )


class ResponsiveWaitTests(unittest.TestCase):
    """Regression coverage for _responsive_wait's polling loop itself -
    previously untested, which let an UnboundLocalError on `remaining` slip
    through a full green test run and crash the live bot on startup."""

    @patch("gridbot.main.time.sleep")
    @patch("gridbot.main._apply_control_commands")
    def test_polls_until_wait_seconds_elapsed(self, apply_control_commands: Mock, sleep: Mock) -> None:
        apply_control_commands.return_value = (False, False)

        bot_halted, should_stop = _responsive_wait(
            10, 3, Mock(), Mock(), Mock(), False, Mock(), Mock(), Mock(), Mock()
        )

        self.assertEqual(apply_control_commands.call_count, 4)  # ceil(10 / 3)
        self.assertFalse(bot_halted)
        self.assertFalse(should_stop)

    @patch("gridbot.main.time.sleep")
    @patch("gridbot.main._apply_control_commands")
    def test_stops_early_when_should_stop_returned(self, apply_control_commands: Mock, sleep: Mock) -> None:
        apply_control_commands.return_value = (True, True)

        bot_halted, should_stop = _responsive_wait(
            60, 5, Mock(), Mock(), Mock(), False, Mock(), Mock(), Mock(), Mock()
        )

        apply_control_commands.assert_called_once()
        self.assertTrue(bot_halted)
        self.assertTrue(should_stop)

    @patch("gridbot.main.time.sleep")
    @patch("gridbot.main._apply_control_commands")
    def test_zero_wait_seconds_returns_without_polling(self, apply_control_commands: Mock, sleep: Mock) -> None:
        bot_halted, should_stop = _responsive_wait(
            0, 5, Mock(), Mock(), Mock(), False, Mock(), Mock(), Mock(), Mock()
        )

        apply_control_commands.assert_not_called()
        self.assertFalse(should_stop)


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
