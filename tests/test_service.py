import asyncio
import json
import re
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from vacancy_bot.config import Config
from vacancy_bot.categories import category_label
from vacancy_bot.matcher import classify
from vacancy_bot.profile import Profile
from vacancy_bot.service import Service, card, is_recent
from vacancy_bot.storage import Storage
from vacancy_bot.professionals import PRO_GROUPS, pro_group_label


ROOT = Path(__file__).resolve().parent.parent
TEXT = "Вакансия: ассистент. Удалённо, 4 часа в день. Оклад 50 000 ₽. Без опыта."
SOURCE = dict(peer_id=-1000000000010, raw_id=10, kind="channel", access_hash=1,
              title="Вакансии", username="test_jobs", protected=False)


def message(message_id, text=TEXT, days_ago=0):
    return SimpleNamespace(id=message_id, message=text, noforwards=False,
                           date=datetime.now(timezone.utc) - timedelta(days=days_ago))


class FakeClient:
    def __init__(self):
        self.messages = []
        self.forwarded = []
        self.protected = False
        self.requests = []

    async def iter_messages(self, peer, **kwargs):
        self.requests.append(kwargs)
        messages = sorted(self.messages, key=lambda m: m.id, reverse=not kwargs.get("reverse", False))
        messages = [m for m in messages if m.id > kwargs.get("min_id", 0)]
        for item in messages[:kwargs.get("limit", 100)]:
            yield item

    async def get_entity(self, peer):
        return SimpleNamespace(noforwards=self.protected)

    async def forward_messages(self, destination, messages, from_peer):
        self.forwarded.extend(messages)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send(self, owner, text, keyboard=False):
        self.sent.append(text)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.path = Path(self.tmp.name)
        self.patch = patch("vacancy_bot.service.ROOT", self.path)
        self.patch.start()
        self.db = Storage(self.path / "test.sqlite3")
        self.db.sync_sources([SOURCE])
        self.client, self.bot = FakeClient(), FakeBot()
        async def resolve_response_folder(client, selection):
            return SimpleNamespace(id=51, title="Удаленки"), getattr(client, "dialogs", [])
        self.response_folder_patch = patch("vacancy_bot.service.response_folder_dialogs", side_effect=resolve_response_folder)
        self.response_folder_patch.start()
        self.service = Service(self.client, self.bot, self.db, Config(api_id=1, api_hash="x", owner=111))
        self.service.bot_entity = "my_test_bot"

    async def asyncTearDown(self):
        self.db.close()
        self.patch.stop()
        self.response_folder_patch.stop()
        self.tmp.cleanup()

    async def read(self, history=False):
        await self.service.read_source(self.db.source(SOURCE["peer_id"]), history)

    def command(self, text, user_id=111, **extra):
        return {"from": {"id": user_id}, "chat": {"id": user_id, "type": "private"}, "text": text, **extra}


    async def test_response_statistics_command_reads_chats_and_repeated_scan_keeps_counts(self):
        from test_responses import FakeClient as ResponseClient, dialog, message as response_message
        self.client.messages = [message(1, TEXT + " Контакт: @test_hr")]
        await self.read()
        current = datetime.now(timezone.utc)
        outgoing = response_message(1, "Хочу откликнуться на вакансию ассистента")
        outgoing.date = current
        personal = dialog()
        personal.date = current
        self.service.client = ResponseClient([personal], {123: [outgoing]})
        with patch("vacancy_bot.service.utc_now", return_value=current):
            for text in ("/responses", "Статистика откликов"):
                await self.service.handle_command(self.command(text))
                self.assertIn("Связаны с вакансиями: 1", self.bot.sent[-1])
                self.assertIn("Ассистент — 1 (100%)", self.bot.sent[-1])
            self.assertEqual(self.service.client.requested, [123, 123])
            await self.service.handle_command(self.command("/responses stats"))
            await self.service.handle_command(self.command("/responses list"))
            self.assertEqual(self.service.client.requested, [123, 123])
            self.assertIn("@test_hr", self.bot.sent[-1])
            self.assertIn("Вакансия №", self.bot.sent[-1])

    async def test_response_command_owner_and_private_chat_checks_apply_before_history_read(self):
        self.service.application_statistics = AsyncMock()
        await self.service.handle_command(self.command("/responses", user_id=222))
        await self.service.handle_command(dict(self.command("/responses"), chat={"id":111, "type":"group"}))
        await self.service.handle_command(self.command("/responses", forward_origin={"type":"user"}))
        self.service.application_statistics.assert_not_awaited()

    async def test_response_concurrency_and_errors_have_actionable_messages(self):
        self.service.client.iter_dialogs = None  # Any accidental network read will fail this test.
        async with self.service.responses_lock:
            await self.service.handle_command(self.command("/responses"))
            self.assertIn("уже идёт", self.bot.sent[-1])
            await self.service.handle_command(self.command("/responses stats"))
            self.assertIn("Статистика откликов", self.bot.sent[-1])
        await self.service.handle_command(self.command("/responses -1"))
        self.assertIn("Пример", self.bot.sent[-1])
        await self.service.handle_command(self.command("/responses help"))
        self.assertIn("/responses all", self.bot.sent[-1])

    async def test_response_cooldown_survives_bot_restart_and_cache_stays_readable(self):
        self.db.set_setting("responses_resume", json.dumps({"retry_at": (datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat()}))
        self.service.client.iter_dialogs = None
        await self.service.handle_command(self.command("/responses"))
        self.assertIn("подождать ещё", self.bot.sent[-1])
        self.assertIn("Прогресс сохранён", self.bot.sent[-1])
        await self.service.handle_command(self.command("/responses stats"))
        self.assertIn("Статистика откликов", self.bot.sent[-1])

    async def test_background_scan_checks_cooldown_after_acquiring_history_lock(self):
        self.service.blocked_until = float("inf")
        self.service.sync_folder = AsyncMock()
        await self.service.scan()
        self.service.sync_folder.assert_not_awaited()

    async def test_response_scan_serializes_history_requests_with_channel_scan(self):
        from vacancy_bot.responses import ScanResult
        async def mocked_scan(*args, **kwargs):
            self.assertTrue(self.service.lock.locked())
            self.assertTrue(self.service.responses_lock.locked())
            return ScanResult(complete=True)
        with patch("vacancy_bot.service.scan_responses", side_effect=mocked_scan):
            await self.service.handle_command(self.command("/responses"))
        self.assertFalse(self.service.lock.locked())

    async def test_response_folder_selection_is_separate_from_channel_source_folder(self):
        self.db.set_setting("folder", "2")
        await self.service.handle_command(self.command("/responses folder Удаленки"))
        self.assertEqual(self.db.setting("responses_folder"), "51")
        self.assertEqual(self.db.setting("folder"), "2")
        self.assertIn("Удаленки", self.bot.sent[-1])
        await self.service.handle_command(self.command("/responses stats"))
        self.assertIn("Папка HR: «Удаленки»", self.bot.sent[-1])

    async def test_response_request_tells_user_scope_before_waiting_for_background_lock(self):
        await self.service.lock.acquire()
        task = asyncio.create_task(self.service.handle_command(self.command("/responses")))
        try:
            await asyncio.sleep(0)
            self.assertTrue(self.service.responses_requested)
            self.assertIn("только в папке", self.bot.sent[-1])
        finally:
            self.service.lock.release()
        await task
        self.assertFalse(self.service.responses_requested)

    async def test_responses_returns_saved_statistics_instead_of_only_wait_message_during_rate_limit(self):
        self.db.set_setting("responses_scope", json.dumps({"owner":111,"folder_id":51,"folder_title":"Удаленки","peer_ids":[123]}))
        self.db.set_setting("responses_scan", json.dumps({"scope_key":"folder:51","complete":True,
            "at":datetime.now(timezone.utc).isoformat(),"days":30,"chats":1,"messages":2,"errors":0}))
        self.service.blocked_until = float("inf")
        self.service.last_error = "Telegram просит подождать 17 секунд"
        self.service.client.iter_dialogs = None
        await self.service.handle_command(self.command("/responses"))
        self.assertIn("сохранённую статистику без ожидания", self.bot.sent[-2])
        self.assertIn("Папка HR: «Удаленки»", self.bot.sent[-1])
        self.assertIn("завершён", self.bot.sent[-1])

    async def test_professionals_find_rejected_jobs_without_changing_personal_analysis(self):
        technical = "Вакансия: Python-разработчик. Удалённо, 8 часов в день. Оклад 30000 ₽. Опыт работы 5 лет. Требования: Python, SQL."
        self.client.messages = [message(1, technical), message(2)]
        await self.read()
        row = self.db.db.execute("SELECT * FROM jobs WHERE message_id=1").fetchone()
        self.assertFalse(row["eligible"])
        personal_analysis = row["analysis"]
        await self.service.handle_command(self.command("Вакансии специалистов"))
        self.assertIn("Python-разработчик", self.bot.sent[-1])
        self.assertNotIn("ассистент", self.bot.sent[-1])
        self.assertIn(pro_group_label(1), self.bot.sent[-1])
        self.assertIn("Оригинал: " + row["link"], self.bot.sent[-1])
        self.assertEqual(self.client.forwarded, [])
        self.assertIn("Опыт работы 5 лет", self.bot.sent[-1])
        self.assertEqual(self.db.job(row["id"])["analysis"], personal_analysis)
        self.assertFalse(self.db.delivery_state(row["id"])["card_sent"])
        self.assertIsNone(self.db.delivery_state(row["id"])["notified_at"])

    async def test_professionals_scope_and_deduplication(self):
        technical = "Вакансия: Python-разработчик. Удалённо. Опыт работы 5 лет. Требования: Python, SQL. Контакт: @test_hr"
        self.client.messages = [message(1, technical), message(2, technical),
                                message(3, technical.replace("Python", "Java"), days_ago=4),
                                message(4, technical.replace("Удалённо", "Гибрид"))]
        await self.read()
        await self.service.handle_command(self.command("/pro"))
        output = self.bot.sent[-1]
        self.assertIn("Найдено: 1", output)
        self.assertEqual(output.count("· Python-разработчик"), 1)
        self.assertNotIn("Java-разработчик", output)
        self.db.sync_sources([])
        await self.service.handle_command(self.command("/pro"))
        self.assertIn("Найдено: 0", self.bot.sent[-1])

    async def test_professionals_groups_and_pagination(self):
        self.client.messages = [message(index, f"Вакансия: аналитик {index}. Удалённо. Требования: SQL, Power BI. Контакт: @hr_{index:04}")
                                for index in range(1, 13)]
        self.client.messages.append(message(13, "Вакансия: маркетолог. Удалённо. Требования: Google Ads."))
        await self.read()
        await self.service.handle_command(self.command("/pro 2"))
        self.assertIn("Найдено: 12", self.bot.sent[-1])
        self.assertEqual(self.bot.sent[-1].count("Оригинал: "), 10)
        self.assertIn("Ещё: /pro 2 page 2", self.bot.sent[-1])
        self.assertNotIn("· маркетолог", self.bot.sent[-1])
        await self.service.handle_command(self.command("/pro 2 page 2"))
        self.assertEqual(self.bot.sent[-1].count("Оригинал: "), 2)
        self.assertIn("Назад: /pro 2 page 1", self.bot.sent[-1])
        await self.service.handle_command(self.command("/pro 2 page 3"))
        self.assertIn("Страницы 3 нет", self.bot.sent[-1])
        await self.service.handle_command(self.command("/prohelp"))
        for group in PRO_GROUPS:
            self.assertIn(pro_group_label(group), self.bot.sent[-1])

    async def test_professionals_respect_source_protection_in_list(self):
        technical = "Вакансия: секретный Python-разработчик. Удалённо. Опыт 5 лет. Требования: Docker, SQL."
        self.client.messages = [message(1, technical)]
        await self.read()
        self.db.sync_sources([dict(SOURCE, protected=True)])
        row = self.db.recent(self.service.cutoff())[0]
        await self.service.handle_command(self.command("/pro"))
        for output in self.bot.sent:
            self.assertNotIn("секретный", output)
            self.assertNotIn("Docker", output)
            self.assertNotIn("Опыт 5", output)
            self.assertIn(row["link"], output)
        self.assertEqual(self.client.forwarded, [])

    async def test_proscan_reads_same_folder_and_returns_professional_results_without_personal_delivery(self):
        self.db.set_setting("folder", "2")
        self.db.set_setting("folder_title", "Вакансии")
        self.client.messages = [message(1, "Вакансия: аналитик. Удалённо, 8 часов в день. Требования: SQL, опыт 5 лет."), message(2)]
        self.service.sync_folder = AsyncMock()
        await self.service.handle_command(self.command("/proscan"))
        self.service.sync_folder.assert_awaited_once()
        self.assertIn("· аналитик", self.bot.sent[-1])
        self.assertNotIn("· ассистент", self.bot.sent[-1])
        self.assertEqual(self.client.requests[-1]["limit"], self.service.config.history_limit)
        self.assertEqual(self.client.forwarded, [])
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)
        self.db.set_setting("paused", "1")
        self.client.requests.clear()
        await self.service.handle_command(self.command("/proscan"))
        self.assertIn("на паузе", self.bot.sent[-1])
        self.assertEqual(self.client.requests, [])

    async def test_scan_score_forward_and_no_repeat(self):
        self.client.messages = [message(1), message(2, TEXT.replace("Без опыта", "Опыт 5 лет"))]
        await self.read()
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [1])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("100/100", self.bot.sent[0])
        self.assertIn("Кратко: Позиция: ассистент.", self.bot.sent[0])
        await self.read(history=True)
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [1])

    async def test_restart_preserves_notifications(self):
        self.client.messages = [message(1)]
        await self.read()
        await self.service.drain()
        self.db.close()
        self.db = Storage(self.path / "test.sqlite3")
        self.service.db = self.db
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [1])

    async def test_only_jobs_at_or_above_60_are_delivered(self):
        self.service.profile = replace(Profile(), keywords=["Excel", "почта", "чат", "таблицы", "поддержка"])
        self.service.profile.save(self.path / "profile.json")
        self.client.messages = [message(1, "Ищем удалённого ассистента. Excel, почта, чат."),
                                message(2, "Ищем удалённого ассистента. Excel, почта, чат, таблицы.")]
        await self.read()
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [2])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("60/100", self.bot.sent[0])
        self.assertEqual([row["score"] for row in self.db.ranked(self.service.cutoff())], [60])

    async def test_queued_jobs_are_rechecked_when_threshold_changes(self):
        self.client.messages = [message(1, "Ищем удалённого ассистента.")]
        await self.read()
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)
        replace(Profile(), min_score=70).save(self.path / "profile.json")
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [])
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.db.ranked(self.service.cutoff()), [])

    async def test_show_cannot_deliver_job_below_60(self):
        self.service.profile = replace(Profile(), keywords=["Excel"])
        self.service.profile.save(self.path / "profile.json")
        self.client.messages = [message(1, "Ищем удалённого ассистента.")]
        await self.read()
        row = self.db.recent(self.service.cutoff())[0]
        await self.service.handle_command(self.command(f"/show {row['id']}"))
        self.assertEqual(self.client.forwarded, [])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("не соответствует", self.bot.sent[0])
        self.assertIsNone(self.db.job(row["id"])["notified_at"])

    async def test_deliver_rejects_stale_eligible_row(self):
        self.client.messages = [message(1, "Ищем удалённого ассистента.")]
        await self.read()
        row = self.db.ranked(self.service.cutoff())[0]
        self.service.profile = replace(Profile(), keywords=["Excel"])
        self.assertFalse(await self.service.deliver(row))
        self.assertEqual(self.client.forwarded, [])
        self.assertEqual(self.bot.sent, [])

    async def test_card_composes_summary_for_old_analysis(self):
        self.client.messages = [message(1)]
        await self.read()
        row = dict(self.db.ranked(self.service.cutoff())[0])
        analysis = json.loads(row["analysis"])
        del analysis["summary"]
        row["analysis"] = json.dumps(analysis)
        self.assertIn("Кратко: Позиция: ассистент.", card(row))

    async def test_catchup_uses_saved_position(self):
        self.client.messages = [message(1)]
        await self.read()
        self.client.messages += [message(2, TEXT + " Excel."), message(3, TEXT + " Email.")]
        await self.read()
        self.assertEqual(self.client.requests[-1]["min_id"], 1)
        self.assertTrue(self.client.requests[-1]["reverse"])
        self.assertEqual(self.db.source(SOURCE["peer_id"])["last_id"], 3)

    async def test_history_does_not_jump_existing_cursor(self):
        self.client.messages = [message(1)]
        await self.read()
        self.client.messages += [message(i, TEXT + f" Объявление {i}") for i in range(2, 6)]
        await self.read(history=True)
        self.assertEqual(self.db.source(SOURCE["peer_id"])["last_id"], 1)
        await self.read()
        self.assertEqual(self.db.source(SOURCE["peer_id"])["last_id"], 5)

    async def test_old_messages_not_notified_but_cursor_advances(self):
        self.client.messages = [message(1, days_ago=8)]
        await self.read()
        self.assertEqual(self.db.stats(self.service.cutoff())["total"], 0)
        self.assertEqual(self.db.source(SOURCE["peer_id"])["last_id"], 1)

    async def test_source_protection_respected(self):
        self.client.messages = [message(1, TEXT + "\nЗадачи: ответы на секретные письма.")]
        self.client.protected = True
        await self.read()
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [])
        self.assertIn("запретил пересылку", self.bot.sent[0])
        self.assertIn("Кратко: Условия:", self.bot.sent[0])
        self.assertNotIn("секретные письма", self.bot.sent[0])

    async def test_roundup_only_matching_part_sent(self):
        self.client.messages = [message(1, "#вакансия\n" + TEXT + "\n\n#вакансия\n" + TEXT.replace("Без опыта", "Опыт 5 лет"))]
        await self.read()
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [])
        self.assertEqual(len(self.bot.sent), 2)
        self.assertNotIn("5 лет", self.bot.sent[1])

    async def test_foreign_and_forwarded_commands_ignored(self):
        await self.service.handle_command(self.command("/pause", user_id=222))
        await self.service.handle_command(self.command("/pause", forward_origin={"type": "channel"}))
        self.assertFalse(self.service.paused)
        self.assertEqual(self.bot.sent, [])

    async def test_pause_stops_notifications(self):
        self.client.messages = [message(1)]
        await self.read()
        await self.service.handle_command(self.command("/pause"))
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [])
        await self.service.handle_command(self.command("/resume"))
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [1])

    async def test_criteria_update_rechecks_existing_posts(self):
        self.client.messages = [message(1, TEXT.replace("4 часа", "8 часов"))]
        await self.read()
        self.assertEqual(self.db.ranked(self.service.cutoff()), [])
        await self.service.handle_command(self.command("/set hours 2 8"))
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)
        self.assertEqual(Profile.load(self.path / "profile.json").hours_max, 8)

    async def test_salary_command_rechecks_and_delivers_high_salary(self):
        self.service.profile = replace(Profile(), salary_min=200000)
        self.service.profile.save(self.path / "profile.json")
        self.client.messages = [message(1, TEXT.replace("50 000", "150 000"))]
        await self.read()
        self.assertEqual(self.db.ranked(self.service.cutoff()), [])
        await self.service.handle_command(self.command("/set salary 40"))
        self.assertEqual(Profile.load(self.path / "profile.json").salary_min, 40000)
        self.assertIn("без верхней границы", self.bot.sent[0])
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [1])
        self.assertIn("100/100", self.bot.sent[1])

    async def test_inactive_folder_sources_hidden(self):
        self.client.messages = [message(1)]
        await self.read()
        self.db.sync_sources([])
        self.assertEqual(self.db.ranked(self.service.cutoff()), [])

    async def test_reposts_deduplicated(self):
        self.client.messages = [message(1), message(2, "  " + TEXT.upper() + "\n")]
        await self.read()
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)

    async def test_edited_post_text_updated(self):
        self.client.messages = [message(1, TEXT.replace("4 часа", "8 часов"))]
        await self.read()
        self.client.messages = [message(1)]
        await self.read(history=True)
        row = self.db.ranked(self.service.cutoff())[0]
        self.assertEqual(row["text"], TEXT)
        self.service.reclassify()
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)

    async def test_edited_post_became_duplicate(self):
        self.client.messages = [message(1), message(2, TEXT + " Excel")]
        await self.read()
        self.client.messages = [message(1), message(2)]
        await self.read(history=True)
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)
        self.service.reclassify()
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)

    async def test_changed_profile_applies_after_restart(self):
        self.client.messages = [message(1, TEXT.replace("4 часа", "8 часов"))]
        await self.read()
        replace(Profile(), hours_max=8).save(self.path / "profile.json")
        Service(self.client, self.bot, self.db, Config(api_id=1, api_hash="x", owner=111))
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)

    async def test_removed_roundup_item_stays_removed(self):
        self.client.messages = [message(1, "#вакансия\n" + TEXT + "\n\n#вакансия\n" + TEXT + " Excel")]
        await self.read()
        self.client.messages = [message(1)]
        await self.read(history=True)
        self.service.reclassify()
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)

    async def test_show_explicitly_repeats_original(self):
        self.client.messages = [message(1)]
        await self.read()
        await self.service.drain()
        row = self.db.ranked(self.service.cutoff())[0]
        await self.service.handle_command(self.command(f"/show {row['id']}"))
        self.assertEqual(self.client.forwarded, [1, 1])

    async def test_date_checks(self):
        self.assertTrue(is_recent(message(1), 7))
        self.assertFalse(is_recent(message(1, days_ago=8), 7))
        self.assertFalse(is_recent(SimpleNamespace(date=None), 7))

    async def test_telegram_exact_72_hour_boundary_and_old_forward(self):
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        boundary = message(1)
        boundary.date = now - timedelta(hours=72)
        old = message(2)
        old.date = boundary.date - timedelta(seconds=1)
        forwarded = message(3)
        forwarded.date = now
        forwarded.fwd_from = SimpleNamespace(date=now - timedelta(days=4))
        self.client.messages = [boundary, old, forwarded]
        with patch("vacancy_bot.service.utc_now", return_value=now):
            await self.read()
            await self.service.drain()
        self.assertEqual(self.client.forwarded, [1])
        self.assertEqual(self.db.source(SOURCE["peer_id"])["last_id"], 3)


    async def test_cross_chat_repost_with_description_edits_is_sent_once(self):
        source2 = dict(SOURCE, peer_id=-1000000000020, raw_id=20, title="Другой чат", username="other_jobs")
        self.db.sync_sources([SOURCE, source2])
        text = TEXT + "\nЗадачи: вести таблицы; отвечать на письма клиентов.\nКонтакты: @test_recruiter"
        self.client.messages = [message(1, text)]
        await self.read()
        await self.service.drain()
        repost = text.replace("@test_recruiter", "https://t.me/Test_Recruiter").replace("вести таблицы", "вести таблицы учеников")
        self.client.messages = [message(2, repost)]
        await self.service.read_source(self.db.source(source2["peer_id"]), history=False)
        await asyncio.gather(self.service.drain(), self.service.drain())
        self.assertEqual(self.client.forwarded, [1])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.db.ranked(self.service.cutoff())), 1)
        self.service = Service(self.client, self.bot, self.db, self.service.config)
        await self.service.drain()
        self.assertEqual(len(self.bot.sent), 1)

    async def test_categories_browse_all_groups_and_only_eligible_jobs(self):
        roles = ["SMM Manager", "Online Chat Support Specialist", "Junior Project Manager", "Data Entry Specialist"]
        self.client.messages = [message(index, TEXT.replace("ассистент", role)) for index, role in enumerate(roles, 1)]
        self.client.messages.append(message(5, TEXT.replace("ассистент", "Data Entry Specialist").replace("4 часа", "8 часов")))
        await self.read()
        await self.service.handle_command(self.command("/jobs"))
        for group in range(1, 5):
            self.assertIn(category_label(group), self.bot.sent[-1])
        self.assertNotIn("№5", self.bot.sent[-1])
        for group, role in enumerate(roles, 1):
            await self.service.handle_command(self.command(f"/jobs {group}"))
            self.assertIn(category_label(group), self.bot.sent[-1])
            self.assertIn(role, self.bot.sent[-1])
            for other in roles:
                if other != role:
                    self.assertNotIn(other, self.bot.sent[-1])
        await self.service.drain()
        self.assertEqual(set(self.client.forwarded), {1, 2, 3, 4})
        for group in range(1, 5):
            self.assertTrue(any("Категория: " + category_label(group) in text for text in self.bot.sent))

    async def test_category_filter_is_applied_before_ranking_limit(self):
        self.client.messages = [message(index, TEXT.replace("ассистент", f"Copywriter {index}")) for index in range(1, 26)]
        self.client.messages.append(message(26, TEXT.replace("ассистент", "Data Entry Specialist")))
        await self.read()
        rows = self.db.ranked(self.service.cutoff(), limit=1, category=4)
        self.assertEqual(len(rows), 1)
        self.assertIn("Data Entry Specialist", rows[0]["text"])
        await self.service.handle_command(self.command("/jobs 4"))
        self.assertIn("Data Entry Specialist", self.bot.sent[-1])

    async def test_category_help_and_invalid_category_do_not_change_preferences(self):
        original = self.service.profile
        await self.service.handle_command(self.command("/categories"))
        for group in range(1, 5):
            self.assertIn(category_label(group), self.bot.sent[-1])
            self.assertIn(f"/jobs {group}", self.bot.sent[-1])
        await self.service.handle_command(self.command("/jobs 5"))
        self.assertIn("Пример:", self.bot.sent[-1])
        self.assertEqual(self.service.profile, original)

    async def test_old_analysis_is_upgraded_without_resending(self):
        self.client.messages = [message(1, TEXT.replace("ассистент", "Copywriter"))]
        await self.read()
        await self.service.drain()
        row = self.db.ranked(self.service.cutoff())[0]
        notified_at = row["notified_at"]
        analysis = json.loads(row["analysis"])
        for key in ("category", "description", "summary"):
            analysis.pop(key, None)
        self.db.db.execute("UPDATE jobs SET analysis=? WHERE id=?", (json.dumps(analysis), row["id"]))
        self.db.db.commit()
        self.assertIn(category_label(1), card(self.db.job(row["id"])))
        self.service = Service(self.client, self.bot, self.db, self.service.config)
        upgraded = self.db.ranked(self.service.cutoff(), category=1)[0]
        self.assertEqual(upgraded["notified_at"], notified_at)
        self.assertTrue(upgraded["card_sent"])
        await self.service.drain()
        self.assertEqual(self.client.forwarded, [1])
        self.assertEqual(len(self.bot.sent), 1)

    async def test_category_changes_after_edit_do_not_resend_duplicate(self):
        self.client.messages = [message(1, TEXT.replace("ассистент", "Copywriter"))]
        await self.read()
        await self.service.drain()
        self.client.messages = [message(1, TEXT.replace("ассистент", "Support Agent"))]
        await self.read(history=True)
        await self.service.drain()
        rows = self.db.ranked(self.service.cutoff(), category=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.db.ranked(self.service.cutoff(), category=1), [])
        self.assertEqual(self.client.forwarded, [1])
        self.assertEqual(len(self.bot.sent), 1)

    async def test_card_displays_metadata_and_useful_description(self):
        text = (TEXT.replace("ассистент", "Content Manager") + "\nКомпания: Acme\nЛокация: Москва"
                "\nЗанятость: частичная занятость\nОбязанности: писать статьи; вести контент-план"
                "\nТребования: WordPress; грамотный русский\nУсловия: оплачиваемое обучение; гибкий график")
        self.client.messages = [message(1, text)]
        await self.read()
        row = self.db.ranked(self.service.cutoff())[0]
        output = card(row)
        for value in ["Content Manager", "Компания: Acme", "Оплата: 50 000 ₽", "Локация: Москва",
                      "Формат: удалённо", "Занятость: частичная занятость", "писать статьи",
                      "WordPress", "оплачиваемое обучение", "гибкий график", category_label(1), row["link"]]:
            self.assertIn(value, output)
        self.assertLess(len(output.encode("utf-16-le")), 7600)

    async def test_card_omits_unknown_metadata(self):
        self.client.messages = [message(1, "Ищем удалённого ассистента.")]
        await self.read()
        output = card(self.db.ranked(self.service.cutoff())[0])
        for field in ("Компания:", "Оплата:", "Локация:", "Занятость:", "Требования:"):
            self.assertNotIn(field, output)
        self.assertIn("Нужно уточнить:", output)

    async def test_protected_card_does_not_leak_description_metadata(self):
        text = TEXT + "\nКомпания: Секретное имя\nЛокация: Секретный город\nТребования: секретный навык\nУсловия: секретная премия"
        self.client.messages = [message(1, text)]
        self.client.protected = True
        await self.read()
        await self.service.drain()
        output = self.bot.sent[-1]
        self.assertNotIn("секрет", output.casefold())
        self.assertIn(category_label(4), output)


    async def test_concurrent_drains_send_a_telegram_vacancy_once(self):
        self.client.messages = [message(1)]
        await self.read()
        async def yielding_send(owner, text, keyboard=False):
            await asyncio.sleep(0)
            self.bot.sent.append(text)
        with patch.object(self.bot, "send", new=yielding_send):
            await asyncio.gather(self.service.drain(), self.service.drain(), self.service.drain())
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.client.forwarded, [1])

    async def test_saved_and_queued_telegram_jobs_expire_after_72_hours(self):
        self.client.messages = [message(1)]
        await self.read()
        row = self.db.ranked(self.service.cutoff())[0]
        future = datetime.now(timezone.utc) + timedelta(hours=73)
        with patch("vacancy_bot.service.utc_now", return_value=future):
            await self.service.drain()
            self.assertEqual(self.client.forwarded, [])
            self.assertEqual(self.bot.sent, [])
            await self.service.handle_command(self.command("/tg"))
            self.assertNotIn(f"№{row['id']}", self.bot.sent[-1])
            await self.service.handle_command(self.command(f"/show {row['id']}"))
            self.assertIn("устарела", self.bot.sent[-1])

    async def test_telegram_exposes_all_four_categories(self):
        roles = ["SMM Manager", "Online Chat Support Specialist", "Junior Project Manager", "Data Entry Specialist"]
        self.client.messages = [message(index, TEXT.replace("ассистент", role)) for index, role in enumerate(roles, 1)]
        await self.read()
        for group, role in enumerate(roles, 1):
            await self.service.handle_command(self.command(f"/tg {group}"))
            self.assertIn(role, self.bot.sent[-1])
            self.assertIn(category_label(group), self.bot.sent[-1])
        await self.service.handle_command(self.command("/jobs"))
        self.assertTrue(self.bot.sent[-1].startswith("Вакансии ТГ"))

    async def test_startup_replaces_command_menu_and_runs_telegram_workers(self):
        self.bot.call = AsyncMock(side_effect=lambda method, **kwargs: {"username":"my_bot"} if method=="getMe" else True)
        self.service.background = AsyncMock()
        self.service.poll_commands = AsyncMock()
        await self.service.run()
        menu = next(call.kwargs["commands"] for call in self.bot.call.await_args_list if call.args[0]=="setMyCommands")
        self.assertEqual({item["command"] for item in menu}, {
            "start","help","tg","jobs","pro","proscan","prohelp","scan","folders","folder",
            "sources","categories","show","hr","responses","profile","set","pause","resume","stats"})
        self.service.background.assert_awaited_once()
        self.service.poll_commands.assert_awaited_once()
        self.assertIn("Парсер Telegram запущен", self.bot.sent[-1])


    async def test_plain_hr_username_and_link_show_all_roles_without_notifying(self):
        self.client.messages = [message(1, TEXT + "\nКонтакты: @test_hr"),
                                message(2, TEXT.replace("ассистент", "дизайнер").replace("4 часа", "8 часов") + "\nКонтакты: @test_hr"),
                                message(3, TEXT + "\nКонтакты: @test_hr_extra")]
        await self.read()
        pending = len(self.db.ranked(self.service.cutoff(), pending=True))
        for query in ("@Test_HR", "https://t.me/test_hr", "test_hr"):
            await self.service.handle_command(self.command(query))
            output = self.bot.sent[-1]
            self.assertIn("Найдено: 2", output)
            self.assertIn("ассистент", output)
            self.assertIn("дизайнер", output)
            self.assertIn("Не подходит", output)
            self.assertNotIn("№3 ·", output)
        self.assertEqual(self.client.forwarded, [])
        self.assertEqual(len(self.db.ranked(self.service.cutoff(), pending=True)), pending)

    async def test_phone_text_and_shared_contact_card_use_the_same_lookup(self):
        self.client.messages = [message(1, TEXT + "\nТелефон HR: 8 (999) 123-45-67")]
        await self.read()
        await self.service.handle_command(self.command("+7 999 123 45 67"))
        by_text = self.bot.sent[-1]
        await self.service.handle_command(self.command("", contact={"phone_number": "79991234567", "first_name": "HR"}))
        self.assertEqual(self.bot.sent[-1], by_text)
        self.assertIn("Найдено: 1", by_text)

    async def test_hr_search_retains_72_hours_and_explicit_history_is_labelled(self):
        self.client.messages = [message(1, TEXT + "\nКонтакты: @test_hr")]
        await self.read()
        later = datetime.now(timezone.utc) + timedelta(hours=73)
        with patch("vacancy_bot.service.utc_now", return_value=later):
            await self.service.handle_command(self.command("@test_hr"))
            self.assertIn("Совпадений", self.bot.sent[-1])
            self.assertIn("/hr @test_hr all", self.bot.sent[-1])
            await self.service.handle_command(self.command("/hr @test_hr all"))
            self.assertIn("Найдено: 1", self.bot.sent[-1])
            self.assertIn("Архив", self.bot.sent[-1])
            self.assertNotIn("Карточка: /show", self.bot.sent[-1])
        self.assertEqual(self.client.forwarded, [])

    async def test_hr_search_pages_cover_every_match_beyond_the_normal_jobs_limit(self):
        self.client.messages = [message(index, TEXT.replace("ассистент", f"Специалист {index}") + "\nКонтакты: @test_hr")
                                for index in range(1, 26)]
        await self.read()
        seen = []
        for page in range(1, 4):
            await self.service.handle_command(self.command(f"/hr @test_hr {page}"))
            output = self.bot.sent[-1]
            self.assertIn("Найдено: 25", output)
            self.assertIn(f"Страница {page}/3", output)
            seen.extend(re.findall(r"(?m)^№(\d+) ·", output))
            if page < 3:
                self.assertIn(f"Ещё: /hr @test_hr {page + 1}", output)
        self.assertEqual(len(set(seen)), 25)
        self.assertEqual(len(seen), 25)
        await self.service.handle_command(self.command("/hr @test_hr 4"))
        self.assertIn("Страницы 4 нет", self.bot.sent[-1])

    async def test_hr_search_help_unknown_contact_and_validation(self):
        await self.service.handle_command(self.command("Поиск по HR"))
        self.assertIn("Поиск вакансий по контакту HR", self.bot.sent[-1])
        await self.service.handle_command(self.command("@unknown_hr"))
        self.assertIn("Совпадений", self.bot.sent[-1])
        await self.service.handle_command(self.command("/hr @test_hr 0"))
        self.assertIn("Пример", self.bot.sent[-1])

    async def test_hr_search_is_local_and_works_while_paused_or_rate_limited(self):
        self.client.messages = [message(1, TEXT + "\nКонтакты: @test_hr")]
        await self.read()
        self.db.set_setting("paused", "1")
        self.service.blocked_until = float("inf")
        with patch.object(self.client, "get_entity", new=AsyncMock(side_effect=AssertionError("Search must stay local"))):
            await self.service.handle_command(self.command("@test_hr"))
        self.assertIn("Найдено: 1", self.bot.sent[-1])
        self.assertTrue(self.service.paused)

    async def test_foreign_or_forwarded_hr_search_is_ignored(self):
        for extra in ({"user_id": 222}, {"forward_origin": {"type": "user"}},
                      {"forward_from": {"id": 222}}, {"via_bot": {"id": 222}}):
            await self.service.handle_command(self.command("@test_hr", **extra))
        self.assertEqual(self.bot.sent, [])

    async def test_hr_search_respects_protected_source_description(self):
        private = TEXT.replace("ассистент", "Секретная должность") + "\nКомпания: Секретная компания\nКонтакты: @test_hr"
        entry = message(1, private)
        entry.noforwards = True
        self.client.messages = [entry]
        await self.read()
        await self.service.handle_command(self.command("@test_hr"))
        output = self.bot.sent[-1]
        self.assertIn("Найдено: 1", output)
        self.assertIn("запретил пересылку", output)
        self.assertNotIn("Секретная", output)
        self.assertEqual(self.client.forwarded, [])


    async def test_shared_international_contact_can_omit_the_plus(self):
        self.client.messages = [message(1, TEXT + "\nHR: +44 7700 900123")]
        await self.read()
        await self.service.handle_command(self.command("", contact={"phone_number": "447700900123"}))
        self.assertIn("Найдено: 1", self.bot.sent[-1])
        self.assertIn("+447700900123", self.bot.sent[-1])

    async def test_contact_search_keeps_duties_when_position_is_not_stated(self):
        self.client.messages = [message(1, "Обязанности:\nВести таблицы учеников\nКонтакты: @test_hr")]
        await self.read()
        await self.service.handle_command(self.command("@test_hr"))
        self.assertIn("Найдено: 1", self.bot.sent[-1])
        self.assertIn("Задачи: Вести таблицы учеников", self.bot.sent[-1])

