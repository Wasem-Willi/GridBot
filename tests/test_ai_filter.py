from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import requests

from gridbot.ai_filter import AI_ACTION_BOTH, OpenAIDecisionClient, ask_freeform


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


class AskFreeformTests(unittest.TestCase):
    @patch("gridbot.ai_filter.requests.post")
    def test_returns_plain_text_answer_without_json_schema(self, post: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "The stop-loss pauses a symbol once price drops 3% below center.",
                        }
                    ],
                }
            ]
        }
        post.return_value = response

        answer = ask_freeform("test-api-key", "gpt-4o-mini", 20, "Answer concisely.", "How does stop-loss work?")

        self.assertEqual(answer, "The stop-loss pauses a symbol once price drops 3% below center.")
        request_body = post.call_args.kwargs["json"]
        self.assertEqual(request_body["instructions"], "Answer concisely.")
        self.assertEqual(request_body["input"], "How does stop-loss work?")
        self.assertNotIn("text", request_body)

    @patch("gridbot.ai_filter.requests.post")
    def test_bad_request_includes_openai_error_message(self, post: Mock) -> None:
        response = requests.Response()
        response.status_code = 401
        response.url = "https://api.openai.com/v1/responses"
        response._content = json.dumps({"error": {"message": "Invalid API key."}}).encode()
        post.return_value = response

        with self.assertRaisesRegex(ValueError, "Invalid API key"):
            ask_freeform("bad-key", "gpt-4o-mini", 20, "Answer concisely.", "hello")


if __name__ == "__main__":
    unittest.main()
