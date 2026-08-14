from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import requests

from gridbot.ai_filter import AI_ACTION_BOTH, OpenAIDecisionClient


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


if __name__ == "__main__":
    unittest.main()
