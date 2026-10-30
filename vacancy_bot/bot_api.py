from __future__ import annotations

import asyncio
import json
import time

import httpx

from .formatting import group_entities


class BotError(Exception):
    """Contains a safe Telegram description, never a URL with the bot token."""


class BotAPI:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15))
        self._retry_until = 0.0

    async def close(self):
        await self.http.aclose()

    async def call(self, method: str, **payload):
        delay = self._retry_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            response = await self.http.post(self._base + method, json=payload)
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            # Do not stringify httpx exceptions: their URLs contain credentials.
            raise BotError(f"Telegram Bot API: {type(exc).__name__}") from None
        if not data.get("ok"):
            retry = data.get("parameters", {}).get("retry_after")
            if retry is not None:
                self._retry_until = time.monotonic() + max(1, int(retry))
                raise BotError(f"Telegram просит подождать {retry} секунд")
            raise BotError(str(data.get("description", "Ошибка Telegram")))
        return data["result"]

    async def send(self, owner: int, text: str, keyboard: bool = False):
        # Telegram uses UTF-16 code units for length limits. Split by paragraphs first.
        chunks, chunk = [], ""
        for line in text.splitlines(keepends=True):
            while len(line.encode("utf-16-le")) > 7600:
                if chunk:
                    chunks.append(chunk)
                    chunk = ""
                chunks.append(line[:1800])
                line = line[1800:]
            if len((chunk + line).encode("utf-16-le")) > 7600:
                chunks.append(chunk)
                chunk = ""
            chunk += line
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            payload = {"chat_id": owner, "text": chunk,
                       "link_preview_options": {"is_disabled": True}}
            entities = group_entities(chunk)
            if entities:
                payload["entities"] = entities
            if keyboard:
                payload["reply_markup"] = {"keyboard": [["Вакансии ТГ"],
                    ["Вакансии специалистов", "/proscan"],
                    ["/jobs", "/categories"], ["Поиск по HR", "/stats"],
                    ["Статистика откликов"],
                    ["/scan"], ["/profile", "/help"]], "resize_keyboard": True}
            await self.call("sendMessage", **payload)
            await asyncio.sleep(1.1)
