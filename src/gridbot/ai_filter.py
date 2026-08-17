from __future__ import annotations

import json
from collections.abc import Callable
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


def run_agent_turn(
    api_key: str,
    model: str,
    timeout_seconds: int,
    system_prompt: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], str],
    max_tool_rounds: int = 5,
) -> tuple[str, list[dict[str, Any]]]:
    """Run one turn of an agentic OpenAI Responses API conversation that may
    involve tool (function) calls, used by the Telegram /ask command.

    `input_items` is the full conversation so far (prior turns' items, if
    any) with the new user message already appended as the last item.
    `tools` are OpenAI function-tool schemas; `tool_executor(name, args)` is
    called for each function_call the model makes and must return a plain
    string result (it should catch its own errors and return an error
    string rather than raising, since this loop does not wrap it).

    The model/tool exchange repeats until the model returns a final message
    with no further function calls, or `max_tool_rounds` is exceeded (raises
    ValueError - callers should treat this like any other AI chat failure).

    Returns (final_answer_text, new_items), where new_items are every item
    generated during this turn (the model's function_call/message items and
    the tool outputs) - callers should append these to the persisted
    conversation history, after the user message that was already the last
    item of input_items."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    working_input = list(input_items)
    turn_start_len = len(working_input)
    endpoint = "https://api.openai.com/v1/responses"
    for _ in range(max_tool_rounds):
        body = {
            "model": model,
            "instructions": system_prompt,
            "input": working_input,
            "tools": tools,
        }
        response = requests.post(endpoint, headers=headers, json=body, timeout=timeout_seconds)
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
        output = result.get("output")
        if not isinstance(output, list):
            raise ValueError("OpenAI response missing output")
        working_input.extend(output)
        function_calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
        if not function_calls:
            answer = _response_output_text(result).strip()
            return answer, working_input[turn_start_len:]
        for call in function_calls:
            name = str(call.get("name", ""))
            call_id = str(call.get("call_id", ""))
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            tool_output = tool_executor(name, args)
            working_input.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": tool_output,
                }
            )
    raise ValueError(f"AI agent exceeded {max_tool_rounds} tool-call rounds without a final answer")
