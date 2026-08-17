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


def _openai_error_message(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "unknown error"
    if not isinstance(body, dict):
        return "unknown error"
    error = body.get("error")
    if not isinstance(error, dict):
        return "unknown error"
    message = error.get("message")
    return str(message).strip() if message else "unknown error"


def _response_output_text(result: dict[str, Any]) -> str:
    output = result.get("output")
    if not isinstance(output, list):
        raise ValueError("OpenAI response missing output")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                return text
    raise ValueError("OpenAI response output text missing")


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
        self._endpoint = "https://api.openai.com/v1/responses"

    def decide(self, payload: dict[str, Any]) -> AIDecision:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "instructions": self._system_prompt,
            "input": json.dumps(payload, separators=(",", ":")),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "gridbot_decision",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": sorted(ALLOWED_AI_ACTIONS),
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["action", "confidence", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
        }
        response = requests.post(
            self._endpoint,
            headers=headers,
            json=body,
            timeout=self._timeout_seconds,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise ValueError(
                f"OpenAI request failed (status={response.status_code}): "
                f"{_openai_error_message(response)}"
            ) from error
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("OpenAI response must be an object")
        content = _response_output_text(result)
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


def ask_freeform(
    api_key: str,
    model: str,
    timeout_seconds: int,
    system_prompt: str,
    question: str,
    context: str | None = None,
) -> str:
    """Send a freeform question to the OpenAI Responses API and return the
    plain-text answer. Unlike OpenAIDecisionClient.decide, the response is
    not constrained to a JSON schema, so this is used for interactive
    chat (e.g. a Telegram /ask command) rather than bot decisions.

    If context is provided (a snapshot of live bot data such as P/L and
    recent risk events), it is included alongside the question so the model
    can ground its answer in the bot's actual current state."""
    input_text = question if not context else f"Bot context:\n{context}\n\nOperator question: {question}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "instructions": system_prompt,
        "input": input_text,
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers=headers,
        json=body,
        timeout=timeout_seconds,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise ValueError(
            f"OpenAI request failed (status={response.status_code}): "
            f"{_openai_error_message(response)}"
        ) from error
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("OpenAI response must be an object")
    return _response_output_text(result).strip()
