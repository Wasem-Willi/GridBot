from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, Mock, patch
from zoneinfo import ZoneInfo

from gridbot.alerts import ControlCommand
from gridbot.main import (
    AIFilterController,
    NOTIFICATION_CATEGORIES,
    OrderPlacementError,
    RegimeController,
    _apply_control_commands,
    _build_ai_ask_transitions_text,
    _build_notify_status_text,
    _handle_order_placement_error,
    _load_ai_conversation,
    _make_ai_agent_provider,
    _make_ai_tool_executor,
    _notify_enabled,
    _refresh_symbols,
    _responsive_wait,
    _save_ai_conversation,
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
        alerter.poll_commands.return_value = (commands, 1)
        _apply_control_commands(
            alerter,
            store,
            Mock(ai_chat_history_days=2),
            False,
            Mock(return_value=""),
            Mock(return_value=""),
            Mock(return_value=(True, "")),
            ai_ask_provider,
        )

    def test_ask_with_question_sends_provider_answer_and_logs_event(self) -> None:
        store = _store()
        alerter = Mock()
        ai_ask_provider = Mock(return_value=("42 is the answer.", [{"type": "message"}]))

        self._run(store, alerter, ai_ask_provider, [ControlCommand(name="ask", arg="What is the meaning of life?")])

        ai_ask_provider.assert_called_once_with("What is the meaning of life?", [])
        alerter.send.assert_called_once_with("42 is the answer.")
        store.log_risk_event.assert_called_once_with(
            "ai_chat",
            None,
            {"question": "What is the meaning of life?", "success": 1, "source": "telegram"},
        )

    def test_ask_persists_new_turn_to_conversation_state(self) -> None:
        store = _store()
        alerter = Mock()
        new_turn = [{"role": "user", "content": "hi"}, {"type": "message"}]
        ai_ask_provider = Mock(return_value=("hello!", new_turn))

        self._run(store, alerter, ai_ask_provider, [ControlCommand(name="ask", arg="hi")])

        store.set_state.assert_any_call("ai_conversation", json.dumps([new_turn]))

    def test_ask_passes_prior_conversation_turns_to_provider(self) -> None:
        prior_turn = [{"role": "user", "content": "earlier question"}, {"type": "message"}]
        store = _store({"ai_conversation": json.dumps([prior_turn])})
        alerter = Mock()
        ai_ask_provider = Mock(return_value=("answer", [{"type": "message"}]))

        self._run(store, alerter, ai_ask_provider, [ControlCommand(name="ask", arg="follow up")])

        ai_ask_provider.assert_called_once_with("follow up", [prior_turn])

    def test_ask_without_question_shows_usage_and_does_not_call_provider(self) -> None:
        store = _store()
        alerter = Mock()
        ai_ask_provider = Mock()

        self._run(store, alerter, ai_ask_provider, [ControlCommand(name="ask", arg="")])

        ai_ask_provider.assert_not_called()
        alerter.send.assert_called_once_with("Usage: /ask <question>")
        store.log_risk_event.assert_not_called()

    def test_ask_provider_failure_sends_error_message_and_does_not_persist_conversation(self) -> None:
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
        for call in store.set_state.call_args_list:
            self.assertNotEqual(call.args[0], "ai_conversation")

    def test_resyncs_bot_halted_after_action_tool_writes_to_store(self) -> None:
        """kill_bot/resume_bot tools write bot_halted directly to the store;
        _apply_control_commands must resync its local bot_halted afterward so
        the main loop doesn't clobber it on the next store.set_state write."""
        state: dict[str, str] = {}
        store = Mock()
        store.get_state.side_effect = lambda key: state.get(key)
        store.set_state.side_effect = lambda key, value: state.__setitem__(key, value)
        alerter = Mock()
        alerter.poll_commands.return_value = ([ControlCommand(name="ask", arg="please kill the bot")], 1)

        def _provider(question: str, prior_turns: list) -> tuple[str, list]:
            state["bot_halted"] = "1"  # simulate the kill_bot tool executing
            return "Bot halted.", [{"type": "message"}]

        bot_halted, _should_stop = _apply_control_commands(
            alerter,
            store,
            Mock(ai_chat_history_days=2),
            False,
            Mock(return_value=""),
            Mock(return_value=""),
            Mock(return_value=(True, "")),
            _provider,
        )

        self.assertTrue(bot_halted)


class AskResetCommandTests(unittest.TestCase):
    def test_clears_conversation_state_and_confirms(self) -> None:
        store = _store({"ai_conversation": json.dumps([[{"role": "user", "content": "old"}]])})
        alerter = Mock()
        alerter.poll_commands.return_value = ([ControlCommand(name="ask_reset")], 1)

        _apply_control_commands(
            alerter,
            store,
            Mock(ai_chat_history_days=2),
            False,
            Mock(return_value=""),
            Mock(return_value=""),
            Mock(return_value=(True, "")),
            Mock(),
        )

        store.set_state.assert_any_call("ai_conversation", json.dumps([]))
        alerter.send.assert_called_once_with("AI assistant conversation memory cleared.")
        store.log_risk_event.assert_called_once_with("ai_chat_reset", None, {"source": "telegram"})


class LoadSaveAiConversationTests(unittest.TestCase):
    def test_load_returns_empty_list_when_no_state_saved(self) -> None:
        store = _store()

        self.assertEqual(_load_ai_conversation(store), [])

    def test_load_returns_empty_list_on_malformed_json(self) -> None:
        store = _store({"ai_conversation": "not-json"})

        self.assertEqual(_load_ai_conversation(store), [])

    def test_load_returns_empty_list_when_saved_value_is_not_a_list(self) -> None:
        store = _store({"ai_conversation": json.dumps({"not": "a list"})})

        self.assertEqual(_load_ai_conversation(store), [])

    def test_save_then_load_round_trips(self) -> None:
        store = _store()
        turns = [[{"role": "user", "content": "hi"}], [{"role": "user", "content": "again"}]]

        _save_ai_conversation(store, turns)
        saved_json = store.set_state.call_args.args[1]
        store2 = _store({"ai_conversation": saved_json})

        self.assertEqual(_load_ai_conversation(store2), turns)

    def test_save_trims_to_max_turns(self) -> None:
        store = _store()
        turns = [[{"n": i}] for i in range(10)]

        _save_ai_conversation(store, turns)

        saved_json = store.set_state.call_args.args[1]
        saved_turns = json.loads(saved_json)
        self.assertEqual(len(saved_turns), 6)
        self.assertEqual(saved_turns, turns[-6:])


class MakeAiToolExecutorTests(unittest.TestCase):
    def _executor(
        self,
        cfg: Mock | None = None,
        exchange: Mock | None = None,
        store: Mock | None = None,
        pnl_provider: Mock | None = None,
        cancel_all_provider: Mock | None = None,
    ):
        cfg = cfg if cfg is not None else Mock(dry_run=True, mode="paper", ai_chat_history_days=2)
        exchange = exchange if exchange is not None else Mock()
        store = store if store is not None else _store()
        pnl_provider = pnl_provider if pnl_provider is not None else Mock(return_value="P/L: +1.0 USDT")
        cancel_all_provider = cancel_all_provider if cancel_all_provider is not None else Mock(return_value="canceled")
        return _make_ai_tool_executor(cfg, exchange, store, pnl_provider, cancel_all_provider), store

    def test_get_bot_status_reports_running_and_mode(self) -> None:
        executor, _store_ = self._executor(cfg=Mock(dry_run=True, mode="paper", ai_chat_history_days=2))

        result = json.loads(executor("get_bot_status", {}))

        self.assertEqual(result, {"status": "RUNNING", "mode": "paper"})

    def test_get_bot_status_reports_stopped_when_halted(self) -> None:
        store = _store({"bot_halted": "1"})
        executor, _ = self._executor(store=store)

        result = json.loads(executor("get_bot_status", {}))

        self.assertEqual(result["status"], "STOPPED")

    def test_get_pnl_snapshot_delegates_to_provider(self) -> None:
        executor, _ = self._executor(pnl_provider=Mock(return_value="P/L: +5 USDT"))

        self.assertEqual(executor("get_pnl_snapshot", {}), "P/L: +5 USDT")

    def test_get_transitions_uses_given_days(self) -> None:
        store = _store()
        store.tz = ZoneInfo("UTC")
        store.get_risk_events_since.return_value = []
        executor, _ = self._executor(store=store)

        result = executor("get_transitions", {"days": 7})

        self.assertIn("last 7 day(s)", result)

    def test_get_transitions_defaults_to_cfg_history_days_when_omitted(self) -> None:
        store = _store()
        store.tz = ZoneInfo("UTC")
        store.get_risk_events_since.return_value = []
        executor, _ = self._executor(cfg=Mock(dry_run=True, mode="paper", ai_chat_history_days=3), store=store)

        result = executor("get_transitions", {})

        self.assertIn("last 3 day(s)", result)

    def test_get_open_orders_reports_paper_mode_without_calling_exchange(self) -> None:
        exchange = Mock()
        executor, _ = self._executor(cfg=Mock(dry_run=True, mode="paper", ai_chat_history_days=2), exchange=exchange)

        result = executor("get_open_orders", {})

        self.assertIn("Paper mode", result)
        exchange.get_all_open_orders.assert_not_called()

    def test_get_open_orders_all_symbols_when_live(self) -> None:
        exchange = Mock()
        exchange.get_all_open_orders.return_value = [{"symbol": "BTCUSDT"}]
        executor, _ = self._executor(cfg=Mock(dry_run=False, mode="live", ai_chat_history_days=2), exchange=exchange)

        result = executor("get_open_orders", {})

        self.assertIn("BTCUSDT", result)
        exchange.get_all_open_orders.assert_called_once()

    def test_get_open_orders_filters_by_symbol_when_live(self) -> None:
        exchange = Mock()
        exchange.get_open_orders.return_value = [{"symbol": "ETHUSDT"}]
        executor, _ = self._executor(cfg=Mock(dry_run=False, mode="live", ai_chat_history_days=2), exchange=exchange)

        result = executor("get_open_orders", {"symbol": "ETHUSDT"})

        exchange.get_open_orders.assert_called_once_with("ETHUSDT")
        self.assertIn("ETHUSDT", result)

    def test_get_account_balances_reports_paper_mode_without_calling_exchange(self) -> None:
        exchange = Mock()
        executor, _ = self._executor(cfg=Mock(dry_run=True, mode="paper", ai_chat_history_days=2), exchange=exchange)

        result = executor("get_account_balances", {})

        self.assertIn("Paper mode", result)
        exchange.get_account.assert_not_called()

    def test_get_account_balances_filters_out_zero_balances_when_live(self) -> None:
        exchange = Mock()
        exchange.get_account.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.01", "locked": "0"},
                {"asset": "DUST", "free": "0", "locked": "0"},
            ]
        }
        executor, _ = self._executor(cfg=Mock(dry_run=False, mode="live", ai_chat_history_days=2), exchange=exchange)

        result = executor("get_account_balances", {})

        self.assertIn("BTC", result)
        self.assertNotIn("DUST", result)

    def test_get_symbol_state_found(self) -> None:
        store = _store()
        store.get_symbol_state.return_value = Mock(
            symbol="BTCUSDT",
            center_price=60000.0,
            lower_bound=58000.0,
            upper_bound=62000.0,
            paused=False,
            pause_reason=None,
            updated_at="2026-08-17T12:00:00",
            risk_anchor_price=60000.0,
        )
        executor, _ = self._executor(store=store)

        result = json.loads(executor("get_symbol_state", {"symbol": "btcusdt"}))

        self.assertEqual(result["symbol"], "BTCUSDT")
        store.get_symbol_state.assert_called_once_with("BTCUSDT")

    def test_get_symbol_state_not_found(self) -> None:
        store = _store()
        store.get_symbol_state.return_value = None
        executor, _ = self._executor(store=store)

        result = executor("get_symbol_state", {"symbol": "XRPUSDT"})

        self.assertIn("No grid state recorded", result)

    def test_cancel_all_orders_delegates_and_logs_ai_action(self) -> None:
        store = _store()
        cancel_all_provider = Mock(return_value="Cancel all result: canceled=2")
        executor, _ = self._executor(store=store, cancel_all_provider=cancel_all_provider)

        result = executor("cancel_all_orders", {})

        self.assertEqual(result, "Cancel all result: canceled=2")
        store.log_risk_event.assert_called_once_with(
            "ai_action", None, {"tool": "cancel_all_orders", "result": "Cancel all result: canceled=2"}
        )

    def test_kill_bot_sets_state_and_logs(self) -> None:
        store = _store()
        executor, _ = self._executor(store=store)

        result = executor("kill_bot", {})

        store.set_state.assert_called_once_with("bot_halted", "1")
        store.log_risk_event.assert_called_once_with("ai_action", None, {"tool": "kill_bot"})
        self.assertIn("halted", result.lower())

    def test_resume_bot_sets_state_and_logs(self) -> None:
        store = _store()
        executor, _ = self._executor(store=store)

        result = executor("resume_bot", {})

        store.set_state.assert_called_once_with("bot_halted", "0")
        store.log_risk_event.assert_called_once_with("ai_action", None, {"tool": "resume_bot"})
        self.assertIn("resumed", result.lower())

    def test_set_notification_valid_category(self) -> None:
        store = _store()
        executor, _ = self._executor(store=store)

        result = executor("set_notification", {"category": "liquidation", "enabled": False})

        store.set_state.assert_called_once_with("notify_liquidation", "0")
        self.assertIn("turned OFF", result)

    def test_set_notification_all_categories(self) -> None:
        store = _store()
        executor, _ = self._executor(store=store)

        executor("set_notification", {"category": "all", "enabled": True})

        for category in NOTIFICATION_CATEGORIES:
            store.set_state.assert_any_call(f"notify_{category}", "1")

    def test_set_notification_unknown_category(self) -> None:
        executor, _ = self._executor()

        result = executor("set_notification", {"category": "bogus", "enabled": True})

        self.assertIn("Unknown notify category", result)

    def test_unknown_tool_name_returns_message(self) -> None:
        executor, _ = self._executor()

        result = executor("some_unknown_tool", {})

        self.assertIn("Unknown tool", result)

    def test_tool_exception_is_caught_and_returned_as_error_string(self) -> None:
        exchange = Mock()
        exchange.get_all_open_orders.side_effect = ValueError("boom")
        executor, _ = self._executor(cfg=Mock(dry_run=False, mode="live", ai_chat_history_days=2), exchange=exchange)

        result = executor("get_open_orders", {})

        self.assertIn("Tool 'get_open_orders' failed", result)
        self.assertIn("boom", result)


class MakeAiAgentProviderTests(unittest.TestCase):
    def test_missing_api_key_returns_not_configured_message_without_new_items(self) -> None:
        cfg = Mock(ai_api_key="", ai_model="gpt-4o-mini", ai_chat_timeout_seconds=20)

        provider = _make_ai_agent_provider(cfg, Mock(), Mock(), Mock(), Mock())
        answer, new_items = provider("anything", [])

        self.assertEqual(answer, "AI chat is not configured: set OPENAI_API_KEY in .env to enable /ask.")
        self.assertEqual(new_items, [])

    @patch("gridbot.main.run_agent_turn")
    def test_forwards_flattened_prior_turns_and_new_question(self, run_agent_turn: Mock) -> None:
        run_agent_turn.return_value = ("42.", [{"type": "message"}])
        cfg = Mock(ai_api_key="sk-test", ai_model="gpt-4o-mini", ai_chat_timeout_seconds=20)
        prior_turns = [[{"role": "user", "content": "q1"}, {"type": "message"}]]

        provider = _make_ai_agent_provider(cfg, Mock(), _store(), Mock(return_value=""), Mock(return_value=""))
        answer, new_items = provider("q2", prior_turns)

        self.assertEqual(answer, "42.")
        self.assertEqual(new_items, [{"type": "message"}])
        call_args = run_agent_turn.call_args.args
        self.assertEqual(call_args[0], "sk-test")
        self.assertEqual(call_args[1], "gpt-4o-mini")
        self.assertEqual(call_args[2], 20)
        input_items = call_args[4]
        self.assertEqual(
            input_items,
            [{"role": "user", "content": "q1"}, {"type": "message"}, {"role": "user", "content": "q2"}],
        )
    def test_no_events_reports_empty_window(self) -> None:
        store = _store()
        store.tz = ZoneInfo("UTC")
        store.get_risk_events_since.return_value = []

        text = _build_ai_ask_transitions_text(store, 2)

        self.assertEqual(text, "No transition/risk events recorded in the last 2 day(s).")

    def test_queries_with_cutoff_two_days_before_now(self) -> None:
        store = _store()
        store.tz = ZoneInfo("UTC")
        store.get_risk_events_since.return_value = []

        before = datetime.now(ZoneInfo("UTC")) - timedelta(days=2)
        _build_ai_ask_transitions_text(store, 2)
        after = datetime.now(ZoneInfo("UTC")) - timedelta(days=2)

        cutoff_arg = store.get_risk_events_since.call_args.args[0]
        cutoff_dt = datetime.fromisoformat(cutoff_arg)
        self.assertTrue(before - timedelta(seconds=5) <= cutoff_dt <= after + timedelta(seconds=5))

    def test_includes_all_events_and_count(self) -> None:
        store = _store()
        store.tz = ZoneInfo("UTC")
        events = [
            Mock(created_at="2026-08-17T12:00:00", event_type="band_liquidation", symbol="BTCUSDT", details={"band": "stop_loss"}),
            Mock(created_at="2026-08-16T09:00:00", event_type="ai_pause", symbol="ETHUSDT", details={}),
        ]
        store.get_risk_events_since.return_value = events

        text = _build_ai_ask_transitions_text(store, 2)

        self.assertIn("2 events", text)
        self.assertIn("band_liquidation", text)
        self.assertIn("BTCUSDT", text)
        self.assertIn("ai_pause", text)
        self.assertIn("ETHUSDT", text)

    def test_global_symbol_none_rendered_as_global(self) -> None:
        store = _store()
        store.tz = ZoneInfo("UTC")
        store.get_risk_events_since.return_value = [
            Mock(created_at="2026-08-17T12:00:00", event_type="manual_kill", symbol=None, details={"source": "telegram"}),
        ]

        text = _build_ai_ask_transitions_text(store, 2)

        self.assertIn("GLOBAL", text)


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
