from __future__ import annotations

import logging
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class ControlCommand:
    name: str
    arg: str | None = None


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.session = requests.Session()

    def send(self, message: str) -> None:
        payload = {"chat_id": self.chat_id, "text": message}
        response = self.session.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
        response.raise_for_status()

    def poll_commands(self, offset: int) -> tuple[list[ControlCommand], int]:
        params = {"timeout": 0, "offset": offset}
        response = self.session.get(f"{self.base_url}/getUpdates", params=params, timeout=10)
        response.raise_for_status()
        body = response.json()
        if body.get("ok") is not True:
            raise ValueError(f"Telegram getUpdates failed: {body}")

        commands: list[ControlCommand] = []
        next_offset = offset
        for update in body.get("result", []):
            update_id = int(update["update_id"])
            next_offset = max(next_offset, update_id + 1)
            message = update.get("message") or {}
            from_chat = str((message.get("chat") or {}).get("id", ""))
            if from_chat != self.chat_id:
                continue
            text = str(message.get("text", "")).strip()
            if text == "/kill":
                commands.append(ControlCommand(name="kill"))
            elif text == "/resume":
                commands.append(ControlCommand(name="resume"))
            elif text == "/status":
                commands.append(ControlCommand(name="status"))
            elif text == "/pnl":
                commands.append(ControlCommand(name="pnl"))
            elif text == "/help":
                commands.append(ControlCommand(name="help"))
            elif text == "/stop":
                commands.append(ControlCommand(name="stop"))
            elif text == "/transitions":
                commands.append(ControlCommand(name="transitions"))
            elif text == "/cancel_all":
                commands.append(ControlCommand(name="cancel_all"))
            elif text == "/start_fresh":
                commands.append(ControlCommand(name="start_fresh"))
            elif text == "/notify":
                commands.append(ControlCommand(name="notify_status"))
            elif text.startswith("/notify_on"):
                commands.append(ControlCommand(name="notify_on", arg=text[len("/notify_on"):].strip()))
            elif text.startswith("/notify_off"):
                commands.append(ControlCommand(name="notify_off", arg=text[len("/notify_off"):].strip()))
            elif text.startswith("/ask"):
                commands.append(ControlCommand(name="ask", arg=text[len("/ask"):].strip()))
            else:
                logging.info("Ignoring Telegram command text=%s", text)
        return commands, next_offset
