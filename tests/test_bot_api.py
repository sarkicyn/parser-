import unittest
from unittest.mock import patch

import httpx

from vacancy_bot.bot_api import BotAPI, BotError


class BotAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = BotAPI("secret-test-token")

    async def asyncTearDown(self):
        await self.bot.close()

    async def replace_transport(self, handler):
        await self.bot.http.aclose()
        self.bot.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_credentials_redacted_from_network_errors(self):
        def handler(request):
            raise httpx.ConnectError("https://api.telegram.org/botsecret-test-token/", request=request)
        await self.replace_transport(handler)
        with self.assertRaises(BotError) as caught:
            await self.bot.call("getMe")
        self.assertNotIn("secret-test-token", str(caught.exception))

    async def test_telegram_errors_handled(self):
        await self.replace_transport(lambda request: httpx.Response(400, json={"ok": False, "description": "Bad request"}))
        with self.assertRaisesRegex(BotError, "Bad request"):
            await self.bot.call("sendMessage", chat_id=1, text="text")

    async def test_large_unicode_messages_are_split(self):
        import json
        texts = []
        def handler(request):
            texts.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        await self.replace_transport(handler)
        original = "🟡" * 5000 + "\n" + "Текст" * 1000
        with patch("vacancy_bot.bot_api.asyncio.sleep", return_value=None):
            await self.bot.send(1, original)
        self.assertEqual("".join(texts), original)
        self.assertTrue(all(len(text.encode("utf-16-le")) <= 8192 for text in texts))

    async def test_retry_after_is_recorded(self):
        await self.replace_transport(lambda request: httpx.Response(
            429, json={"ok": False, "parameters": {"retry_after": 60}, "description": "Too Many Requests"}))
        with self.assertRaises(BotError):
            await self.bot.call("sendMessage", chat_id=1, text="text")
        self.assertGreater(self.bot._retry_until, 0)

    async def test_reply_keyboard_exposes_telegram_and_response_commands(self):
        import json
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        await self.replace_transport(handler)
        with patch("vacancy_bot.bot_api.asyncio.sleep", return_value=None):
            await self.bot.send(1, "Главное меню", keyboard=True)
        keys = payloads[0]["reply_markup"]["keyboard"]
        self.assertEqual(keys[0], ["Вакансии ТГ"])
        self.assertIn(["/scan"], keys)
        self.assertIn(["Поиск по HR", "/stats"], keys)
        self.assertIn(["Вакансии специалистов", "/proscan"], keys)
        self.assertIn(["Статистика откликов"], keys)

    async def test_group_headers_bold_with_unicode_offsets_and_literal_source_text(self):
        import json
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        await self.replace_transport(handler)
        text = ("🟡 Текст <b>Python & C++</b>\nГруппа 1 — Content & Creative\nОписание\n"
                "Группа 5 — Продукты и проекты · /pro 5\nКатегория: Группа 2 — Аналитика")
        with patch("vacancy_bot.bot_api.asyncio.sleep", return_value=None):
            await self.bot.send(1, text)
        self.assertEqual(payloads[0]["text"], text)
        self.assertNotIn("parse_mode", payloads[0])
        encoded = text.encode("utf-16-le")
        labels = [encoded[entity["offset"] * 2:(entity["offset"] + entity["length"]) * 2].decode("utf-16-le")
                  for entity in payloads[0]["entities"]]
        self.assertEqual(labels, ["Группа 1 — Content & Creative", "Группа 5 — Продукты и проекты", "Группа 2 — Аналитика"])

    async def test_bold_entities_remain_valid_after_message_splitting(self):
        import json
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        await self.replace_transport(handler)
        text = "🟡" * 1900 + "\nГруппа 3 — Маркетинг\n" + "Текст\n" * 1000 + "Группа 1 — Разработка и IT"
        with patch("vacancy_bot.bot_api.asyncio.sleep", return_value=None):
            await self.bot.send(1, text)
        self.assertEqual("".join(item["text"] for item in payloads), text)
        labels = []
        for item in payloads:
            encoded = item["text"].encode("utf-16-le")
            for entity in item.get("entities", []):
                labels.append(encoded[entity["offset"] * 2:(entity["offset"] + entity["length"]) * 2].decode("utf-16-le"))
        self.assertEqual(labels, ["Группа 3 — Маркетинг", "Группа 1 — Разработка и IT"])
