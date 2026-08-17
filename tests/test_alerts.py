from __future__ import annotations

import unittest
from unittest.mock import Mock

from gridbot.alerts import ControlCommand, TelegramAlerter


def _alerter_with_response(messages: list[str]) -> TelegramAlerter:
    alerter = TelegramAlerter("token", "chat123")
    updates = [
        {
            "update_id": 100 + i,
            "message": {"chat": {"id": "chat123"}, "text": text},
        }
        for i, text in enumerate(messages)
    ]
    response = Mock()
    response.json.return_value = {"ok": True, "result": updates}
    response.raise_for_status.return_value = None
    alerter.session = Mock()
    alerter.session.get.return_value = response
    return alerter


class PollCommandsNotifyParsingTests(unittest.TestCase):
    def test_notify_with_no_arg_maps_to_notify_status(self) -> None:
        alerter = _alerter_with_response(["/notify"])
        commands, _ = alerter.poll_commands(0)
        self.assertEqual(commands, [ControlCommand(name="notify_status")])

    def test_notify_on_with_category_arg(self) -> None:
        alerter = _alerter_with_response(["/notify_on liquidation"])
        commands, _ = alerter.poll_commands(0)
        self.assertEqual(commands, [ControlCommand(name="notify_on", arg="liquidation")])

    def test_notify_off_with_category_arg(self) -> None:
        alerter = _alerter_with_response(["/notify_off regime"])
        commands, _ = alerter.poll_commands(0)
        self.assertEqual(commands, [ControlCommand(name="notify_off", arg="regime")])

    def test_notify_on_without_arg_has_empty_arg(self) -> None:
        alerter = _alerter_with_response(["/notify_on"])
        commands, _ = alerter.poll_commands(0)
        self.assertEqual(commands, [ControlCommand(name="notify_on", arg="")])

    def test_ask_with_question_arg(self) -> None:
        alerter = _alerter_with_response(["/ask what is the current regime?"])
        commands, _ = alerter.poll_commands(0)
        self.assertEqual(commands, [ControlCommand(name="ask", arg="what is the current regime?")])

    def test_ask_without_question_has_empty_arg(self) -> None:
        alerter = _alerter_with_response(["/ask"])
        commands, _ = alerter.poll_commands(0)
        self.assertEqual(commands, [ControlCommand(name="ask", arg="")])

    def test_ask_reset_maps_to_ask_reset_command(self) -> None:
        alerter = _alerter_with_response(["/ask_reset"])
        commands, _ = alerter.poll_commands(0)
        self.assertEqual(commands, [ControlCommand(name="ask_reset")])


if __name__ == "__main__":
    unittest.main()
