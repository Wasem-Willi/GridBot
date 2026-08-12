from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests


AI_ACTION_BUY_ONLY = "BUY_ONLY"
AI_ACTION_SELL_ONLY = "SELL_ONLY"
AI_ACTION_BOTH = "BOTH"
AI_ACTION_PAUSE = "PAUSE"
ALLOWED_AI_ACTIONS = {AI_ACTION_BUY_ONLY, AI_ACTION_SELL_ONLY, AI_ACTION_BOTH, AI_ACTION_PAUSE}


@dataclass(frozen=True)
class AIDecision:
    action: str
    confidence: float
    reason: str


class OpenAIDecisionClient:
    def __init__(self, api_key: str, model: str, timeout_seconds: int, system_prompt: str) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._system_prompt = system_prompt
        self._endpoint = "https://api.openai.com/v1/chat/completions"

    def decide(self, payload: dict[str, Any]) -> AIDecision:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": self._system_prompt,
                },
                {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
            ],
        }
        response = requests.post(
            self._endpoint,
            headers=headers,
            json=body,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        result = response.json()
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenAI response missing choices")
        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("OpenAI response content missing")
        parsed = json.loads(content)
        action = str(parsed.get("action", "")).upper()
        if action not in ALLOWED_AI_ACTIONS:
            raise ValueError(f"Invalid AI action: {action}")
        confidence_raw = parsed.get("confidence", 0.0)
        confidence = float(confidence_raw)
        if confidence < 0.0 or confidence > 1.0:
            raise ValueError(f"Invalid AI confidence: {confidence}")
        reason = str(parsed.get("reason", "")).strip()
        if not reason:
            reason = "no_reason_provided"
        return AIDecision(action=action, confidence=confidence, reason=reason)
