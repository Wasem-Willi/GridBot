from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import requests

from gridbot.ai_filter import AI_ACTION_BOTH, OpenAIDecisionClient, run_agent_turn


class OpenAIDecisionClientTests(unittest.TestCase):
    @patch("gridbot.ai_filter.requests.post")
    def test_uses_bearer_authentication(self, post: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "action": AI_ACTION_BOTH,
                                    "confidence": 0.7,
                                    "reason": "ranging",
                                }
                            ),
                        }
                    ],
                }
            ]
        }
        post.return_value = response
        client = OpenAIDecisionClient("test-api-key", "gpt-4o-mini", 10, "Return JSON.")

        client.decide({"symbol": "BTCUSDT"})

        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer test-api-key",
        )
        self.assertEqual(
            post.call_args.args[0],
            "https://api.openai.com/v1/responses",
        )
        request_body = post.call_args.kwargs["json"]
        self.assertEqual(request_body["instructions"], "Return JSON.")
        self.assertEqual(request_body["text"]["format"]["type"], "json_schema")
        self.assertNotIn("temperature", request_body)

    @patch("gridbot.ai_filter.requests.post")
    def test_bad_request_includes_openai_error_message(self, post: Mock) -> None:
        response = requests.Response()
        response.status_code = 400
        response.url = "https://api.openai.com/v1/responses"
        response._content = json.dumps(
            {"error": {"message": "The requested model does not exist."}}
        ).encode()
        post.return_value = response
        client = OpenAIDecisionClient("test-api-key", "invalid-model", 10, "Return JSON.")

        with self.assertRaisesRegex(ValueError, "requested model does not exist"):
            client.decide({"symbol": "BTCUSDT"})


class RunAgentTurnTests(unittest.TestCase):
    def _message_response(self, text: str) -> Mock:
        response = Mock()
        response.json.return_value = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
        }
        return response

    def _function_call_response(self, name: str, arguments: str, call_id: str = "call_1") -> Mock:
        response = Mock()
        response.json.return_value = {
            "output": [
                {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id}
            ]
        }
        return response

    @patch("gridbot.ai_filter.requests.post")
    def test_no_tool_calls_returns_answer_immediately(self, post: Mock) -> None:
        post.return_value = self._message_response("Bot is running fine.")
        tool_executor = Mock()

        answer, new_items = run_agent_turn(
            "key", "gpt-4o-mini", 10, "sys", [{"role": "user", "content": "status?"}], [], tool_executor
        )

        self.assertEqual(answer, "Bot is running fine.")
        tool_executor.assert_not_called()
        self.assertEqual(post.call_count, 1)

    @patch("gridbot.ai_filter.requests.post")
    def test_single_tool_call_round_trip(self, post: Mock) -> None:
        post.side_effect = [
            self._function_call_response("get_bot_status", '{"foo": "bar"}', call_id="call_42"),
            self._message_response("The bot is RUNNING."),
        ]
        tool_executor = Mock(return_value="RUNNING")

        answer, new_items = run_agent_turn(
            "key", "gpt-4o-mini", 10, "sys", [{"role": "user", "content": "is it running?"}], [], tool_executor
        )

        self.assertEqual(answer, "The bot is RUNNING.")
        tool_executor.assert_called_once_with("get_bot_status", {"foo": "bar"})
        # Second request's input must include the function_call_output referencing call_42.
        second_call_input = post.call_args_list[1].kwargs["json"]["input"]
        outputs = [item for item in second_call_input if item.get("type") == "function_call_output"]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["call_id"], "call_42")
        self.assertEqual(outputs[0]["output"], "RUNNING")
        # new_items should include the function_call, its output, and the final message.
        types = [item["type"] for item in new_items]
        self.assertEqual(types, ["function_call", "function_call_output", "message"])

    @patch("gridbot.ai_filter.requests.post")
    def test_malformed_arguments_json_falls_back_to_empty_dict(self, post: Mock) -> None:
        post.side_effect = [
            self._function_call_response("get_bot_status", "not-json", call_id="call_1"),
            self._message_response("done"),
        ]
        tool_executor = Mock(return_value="ok")

        run_agent_turn("key", "gpt-4o-mini", 10, "sys", [{"role": "user", "content": "q"}], [], tool_executor)

        tool_executor.assert_called_once_with("get_bot_status", {})

    @patch("gridbot.ai_filter.requests.post")
    def test_exceeding_max_tool_rounds_raises(self, post: Mock) -> None:
        post.return_value = self._function_call_response("get_bot_status", "{}")
        tool_executor = Mock(return_value="ok")

        with self.assertRaisesRegex(ValueError, "exceeded 2 tool-call rounds"):
            run_agent_turn(
                "key", "gpt-4o-mini", 10, "sys", [{"role": "user", "content": "q"}], [], tool_executor, max_tool_rounds=2
            )

        self.assertEqual(post.call_count, 2)

    @patch("gridbot.ai_filter.requests.post")
    def test_http_error_raises_value_error_with_openai_message(self, post: Mock) -> None:
        response = requests.Response()
        response.status_code = 500
        response.url = "https://api.openai.com/v1/responses"
        response._content = json.dumps({"error": {"message": "Server error."}}).encode()
        post.return_value = response

        with self.assertRaisesRegex(ValueError, "Server error"):
            run_agent_turn("key", "gpt-4o-mini", 10, "sys", [{"role": "user", "content": "q"}], [], Mock())


if __name__ == "__main__":
    unittest.main()
