import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError, RPCError

from vacancy_bot.matcher import classify
from vacancy_bot.responses import (Catalog, ResponseRequest, parse_response_request,
                                   response_list, response_report, roles, scan_responses)
from vacancy_bot.storage import Storage


NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def message(number=1, text="Здравствуйте! Хочу откликнуться на вакансию копирайтера.", *, out=True, days=0, **kwargs):
    return SimpleNamespace(id=number, message=text, out=out, date=NOW - timedelta(days=days), **kwargs)


class ResponseCase(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent / ".tmp"
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.path = Path(self.tmp.name) / "test.sqlite3"
        self.db = Storage(self.path)
        self.counter = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def job(self, title="копирайтер", *, hr="test_hr", days=10, protected=False, text=None, part=0, message_id=None):
        self.counter += 1
        number = message_id or self.counter
        text = text or f"Вакансия: {title}. Офис, 8 часов в день. Опыт 5 лет. Контакт: @{hr}"
        self.db.save_job(peer_id=-100123, message_id=number, part_index=part, part_count=part + 1,
                         text=text, source_title="jobs", link=f"https://t.me/jobs/{number}",
                         published_at=(NOW - timedelta(days=days)).isoformat(), protected=protected,
                         match=classify(text))
        return self.db.db.execute("SELECT * FROM jobs WHERE peer_id=-100123 AND message_id=? AND part_index=?", (number, part)).fetchone()

    def detect(self, text=None, **kwargs):
        item = message(**kwargs) if text is None else message(text=text, **kwargs)
        return Catalog(self.db.response_jobs()).detect(123, ["test_hr"], item)


class DetectionTests(ResponseCase):
    def test_links_rejected_archived_job_by_username_without_profile_filters(self):
        row = self.job(days=200)
        self.assertFalse(row["eligible"])
        result = self.detect()
        self.assertEqual((result.status, result.job_id, result.title, result.sphere),
                         ("linked", row["id"], "Копирайтер", "Контент и творчество"))

    def test_greetings_incoming_forwards_and_refusals_are_not_applications(self):
        self.job()
        for text, kwargs in (("Здравствуйте!", {}), ("Давайте обсудим резюме друга", {}),
                             ("Хочу откликнуться на вакансию", {"out": False}),
                             ("Хочу откликнуться на вакансию", {"fwd_from": object()}),
                             ("Не хочу откликаться на вакансию копирайтера", {}),
                             ("Меня не интересует ваша вакансия", {}),
                             ("Хочу откликнуться на вакансию", {"action": object()})):
            with self.subTest(text=text, kwargs=kwargs):
                self.assertIsNone(self.detect(text, **kwargs))

    def test_role_selects_one_job_of_hr_and_reposts_are_not_ambiguous(self):
        first = self.job()
        self.job()
        self.job("дизайнер")
        result = self.detect()
        self.assertEqual(result.job_key, first["dedup_key"])
        self.assertEqual(result.status, "linked")

    def test_ambiguous_hr_is_excluded_from_occupation_statistics(self):
        self.job()
        self.job("дизайнер")
        result = self.detect("Здравствуйте, по поводу вакансии")
        self.assertEqual(result.status, "ambiguous")
        report = response_report([asdict(result)], 30)
        self.assertIn("Без определённой вакансии: 1", report)
        self.assertIn("Пока нет определённых откликов", report)

    def test_same_role_different_employers_requires_specific_title_or_link(self):
        self.job("Python-разработчик")
        other = self.job("Java-разработчик")
        self.assertEqual(self.detect("Хочу откликнуться на вакансию разработчика").status, "ambiguous")
        specific = self.detect("Хочу откликнуться на вакансию Java-разработчик")
        self.assertEqual(specific.job_id, other["id"])
        direct = self.detect("Здравствуйте, " + other["link"])
        self.assertEqual(direct.job_id, other["id"])

    def test_link_overrides_username_and_disambiguates_split_post(self):
        self.job("дизайнер", message_id=7)
        chosen = self.job("копирайтер", message_id=7, part=1)
        result = Catalog(self.db.response_jobs()).detect(321, ["renamed_hr"], message(text="Хочу откликнуться на вакансию копирайтера " + chosen["link"]))
        self.assertEqual(result.job_id, chosen["id"])

    def test_job_published_after_contact_is_not_linked(self):
        self.job(days=-1)
        self.assertEqual(self.detect().status, "inferred")

    def test_future_repost_does_not_hide_original_available_at_contact(self):
        first = self.job(days=10)
        self.job(days=-1)
        self.assertEqual(self.detect().job_id, first["id"])

    def test_unseen_hr_role_is_inferred_and_not_linked_to_an_unrelated_job(self):
        self.job("дизайнер")
        result = self.detect()
        self.assertEqual((result.status, result.title), ("inferred", "Копирайтер"))
        self.assertIsNone(result.job_id)
        without_database = Catalog([]).detect(123, [], message())
        self.assertEqual(without_database.status, "inferred")

    def test_named_resume_uses_incoming_context_to_select_vacancy(self):
        self.job()
        row = self.job("дизайнер")
        result = Catalog(self.db.response_jobs()).detect(123, ["test_hr"],
            message(text="", file=SimpleNamespace(name="my_CV.pdf")), "Вакансия: дизайнер")
        self.assertEqual(result.job_id, row["id"])

    def test_unknown_occupation_and_protected_post_do_not_expose_text(self):
        row = self.job("секретный копирайтер", protected=True)
        result = self.detect()
        self.assertEqual(result.job_id, row["id"])
        self.assertNotIn("секретный", response_list([asdict(result)], ResponseRequest(action="list")))
        unknown = Catalog([]).detect(123, [], message(text="Здравствуйте, по поводу вакансии"))
        self.assertEqual(unknown.status, "unmatched")

    def test_role_synonyms_and_specific_analyst_do_not_vote_twice(self):
        for text in ("копирайтера", "copywriter", "Копирайтер"):
            self.assertEqual(roles(text), [("Копирайтер", "Контент и творчество")])
        self.assertEqual(roles("product analyst"), [("Продуктовый аналитик", "Аналитика")])

    def test_cover_letter_previous_role_does_not_override_application(self):
        wanted = self.job("дизайнер")
        self.job()
        result = self.detect("Хочу откликнуться на вакансию дизайнера. Раньше работал копирайтером.")
        self.assertEqual(result.job_id, wanted["id"])

    def test_resume_is_not_assumed_to_be_an_application_to_an_unknown_user(self):
        self.assertIsNone(Catalog([]).detect(123, ["friend"], message(text="Вот резюме", file=SimpleNamespace(name="CV.pdf"))))


class ResponseStorageTests(ResponseCase):
    def test_deduplicates_followups_persists_after_restart_and_filters_first_date(self):
        self.job(days=100)
        catalog = Catalog(self.db.response_jobs())
        responses = [asdict(catalog.detect(123, ["test_hr"], message(number=n, days=days))) for n, days in ((1, 60), (2, 0))]
        self.db.replace_responses(123, None, responses)
        self.db.replace_responses(123, None, responses)
        self.assertEqual(len(self.db.responses()), 1)
        self.assertEqual(self.db.responses((NOW - timedelta(days=30)).isoformat()), [])
        self.db.close()
        self.db = Storage(self.path)
        self.assertEqual(len(self.db.responses()), 1)

    def test_window_replacement_keeps_older_history_and_other_peers(self):
        self.job(days=100)
        catalog = Catalog(self.db.response_jobs())
        for peer in (123, 456):
            self.db.replace_responses(peer, None, [asdict(catalog.detect(peer, ["test_hr"], message(days=60)))])
        self.db.replace_responses(123, (NOW - timedelta(days=30)).isoformat(), [])
        self.assertEqual(len(self.db.responses()), 2)
        self.db.replace_responses(123, None, [])
        self.assertEqual(len(self.db.responses()), 1)

    def test_list_links_to_specific_vacancy_and_source_protection_remains_effective(self):
        row = self.job("секретный копирайтер")
        self.db.replace_responses(123, None, [asdict(self.detect())])
        output = response_list(self.db.responses(), ResponseRequest("list"))
        self.assertIn(row["link"], output)
        self.assertIn("секретный копирайтер", output)
        self.db.db.execute("INSERT INTO sources(peer_id,raw_id,kind,title,protected) VALUES(-100123,123,'channel','jobs',1)")
        output = response_list(self.db.responses(), ResponseRequest("list"))
        self.assertNotIn("секретный", output)
        self.assertIn(row["link"], output)

    def test_partial_segment_does_not_delete_unread_responses(self):
        self.job()
        old = asdict(self.detect(number=10))
        recent = asdict(self.detect(number=200))
        self.db.replace_responses(123, None, [old, recent])
        self.db.replace_responses(123, None, [], min_id=100)
        records = self.db.db.execute("SELECT message_id FROM application_responses").fetchall()
        self.assertEqual([row[0] for row in records], [10])
        self.db.replace_responses(123, None, [recent])
        self.db.replace_responses(123, None, [old], before_id=100)
        records = self.db.db.execute("SELECT message_id FROM application_responses ORDER BY message_id").fetchall()
        self.assertEqual([row[0] for row in records], [10, 200])

    def test_cached_statistics_exclude_contacts_outside_selected_folder(self):
        self.job()
        for peer in (123, 456):
            payload = asdict(self.detect())
            payload["peer_id"] = peer
            self.db.replace_responses(peer, None, [payload])
        self.assertEqual(len(self.db.responses(peer_ids=[123])), 1)
        self.assertEqual(self.db.responses(peer_ids=[]), [])


class FakeClient:
    def __init__(self, dialogs, messages):
        self.dialogs, self.messages = dialogs, messages
        self.requested = []
        self.options = []

    async def iter_dialogs(self, **kwargs):
        for dialog in self.dialogs:
            if isinstance(dialog, Exception):
                raise dialog
            yield dialog

    async def iter_messages(self, entity, **kwargs):
        self.requested.append(entity.id)
        self.options.append(kwargs)
        for item in sorted(self.messages.get(entity.id, []), key=lambda msg: msg.id if not isinstance(msg, Exception) else 0, reverse=True):
            if isinstance(item, Exception):
                raise item
            if kwargs.get("offset_id") and item.id >= kwargs["offset_id"]:
                continue
            yield item


class InterruptedClient(FakeClient):
    """Fail exactly after a page, then honor the offset on subsequent attempts."""
    def __init__(self, dialogs, messages, *, fail_after=100, error=None):
        super().__init__(dialogs, messages)
        self.fail_after = fail_after
        self.error = error or FloodWaitError(None, 15)
        self.failed = False

    async def iter_messages(self, entity, **kwargs):
        count = 0
        async for item in super().iter_messages(entity, **kwargs):
            yield item
            count += 1
            if count == self.fail_after and not self.failed:
                self.failed = True
                raise self.error


def dialog(peer=123, username="test_hr", **kwargs):
    return SimpleNamespace(entity=SimpleNamespace(id=peer, username=username, **kwargs), is_user=peer > 0, date=NOW)


class ScanTests(ResponseCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sleep_patch = patch("vacancy_bot.responses.asyncio.sleep", new_callable=AsyncMock)
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    async def test_scans_private_archived_dialogs_skips_self_bots_and_channels(self):
        self.job()
        client = FakeClient([dialog(), dialog(456), dialog(111), dialog(789, bot=True), dialog(-100)],
                            {123: [message()], 456: [message()]})
        result = await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(client.requested, [123, 456])
        self.assertTrue(result.complete)
        self.assertEqual(len(self.db.responses()), 2)

    async def test_scan_uses_outgoing_resume_and_recent_incoming_role(self):
        self.job()
        selected = self.job("дизайнер")
        client = FakeClient([dialog()], {123: [message(1, "Вакансия: дизайнер", out=False),
            message(2, "", file=SimpleNamespace(name="CV.pdf"), reply_to_msg_id=1)]})
        await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(self.db.responses()[0]["job_id"], selected["id"])

    async def test_repeated_scan_is_idempotent_and_deleted_messages_disappear(self):
        self.job()
        client = FakeClient([dialog()], {123: [message(1), message(2)]})
        for _ in range(2):
            await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(len(self.db.responses()), 1)
        client.messages[123] = []
        await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(self.db.responses(), [])

    async def test_period_does_not_limit_archived_job_lookup_and_all_includes_older_messages(self):
        self.job(days=100)
        client = FakeClient([dialog()], {123: [message(1, days=60), message(2)]})
        await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(self.db.responses()[0]["message_id"], 2)
        await scan_responses(client, self.db, 111, None, now=NOW, progress=AsyncMock())
        self.assertEqual(self.db.responses()[0]["message_id"], 1)

    async def test_flood_wait_marks_report_partial_and_preserves_finished_and_failed_chat(self):
        self.job()
        original = asdict(self.detect())
        self.db.replace_responses(123, None, [original])
        client = FakeClient([dialog(456), dialog()], {456: [message()], 123: [FloodWaitError(None, 42)]})
        result = await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertFalse(result.complete)
        self.assertEqual(result.flood_seconds, 42)
        self.assertEqual(len(self.db.responses()), 2)
        self.assertFalse(json.loads(self.db.setting("responses_scan"))["complete"])

    async def test_inaccessible_chat_does_not_hide_successful_results(self):
        self.job()
        client = FakeClient([dialog(), dialog(456)], {123: [RPCError(None, "unavailable", 400)], 456: [message()]})
        result = await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual((result.chats, result.errors, result.complete), (1, 1, False))
        self.assertEqual(len(self.db.responses()), 1)

    async def test_lost_connection_marks_partial_report_and_keeps_previous_data(self):
        self.job()
        self.db.replace_responses(123, None, [asdict(self.detect())])
        client = FakeClient([dialog()], {123: [ConnectionError("disconnected")]})
        result = await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertFalse(result.complete)
        self.assertEqual(len(self.db.responses()), 1)

    async def test_short_flood_wait_is_obeyed_and_scan_continues_after_first_100_messages(self):
        self.job()
        client = InterruptedClient([dialog()], {123: [message(n) for n in range(1, 202)]})
        progress = AsyncMock()
        result = await scan_responses(client, self.db, 111, 30, now=NOW, progress=progress)
        self.assertTrue(result.complete)
        self.assertEqual(result.messages, 201)
        self.assertEqual(client.options[1]["offset_id"], 102)
        self.assertEqual(client.options[0]["wait_time"], 3)
        self.sleep.assert_any_await(15)
        self.assertTrue(any("15 сек" in call.args[0] for call in progress.await_args_list))
        self.assertEqual(self.db.responses()[0]["message_id"], 1)
        self.assertEqual(json.loads(self.db.setting("responses_resume")), {})

    async def test_partial_first_chat_is_saved_and_restart_resumes_instead_of_rereading_top_page(self):
        self.job()
        client = InterruptedClient([dialog()], {123: [message(n) for n in range(1, 202)]}, error=FloodWaitError(None, 45))
        first = await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual((first.chats, first.messages, first.partial_chats), (0, 100, 1))
        self.assertEqual(self.db.responses()[0]["message_id"], 102)
        cursor = json.loads(self.db.setting("responses_resume"))["cursors"]["123"]
        self.assertLess(cursor, 201)
        self.db.close()
        self.db = Storage(self.path)
        second = await scan_responses(client, self.db, 111, 30, now=NOW + timedelta(seconds=46), progress=AsyncMock())
        self.assertTrue(second.complete)
        self.assertTrue(second.resumed)
        self.assertEqual(client.options[1]["offset_id"], cursor)
        self.assertEqual(self.db.responses()[0]["message_id"], 1)
        self.assertEqual(len(self.db.responses()), 1)

    async def test_resume_skips_finished_chats_and_prioritizes_known_hr(self):
        self.job()
        client = InterruptedClient([dialog(456, "friend"), dialog()],
            {123: [message()], 456: [message(n) for n in range(1, 102)]}, error=FloodWaitError(None, 45))
        first = await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(client.requested, [123, 456])
        self.assertEqual(first.chats, 1)
        await scan_responses(client, self.db, 111, 30, now=NOW + timedelta(seconds=46), progress=AsyncMock())
        self.assertEqual(client.requested, [123, 456, 456])

    async def test_cancellation_after_a_page_keeps_detected_responses_and_cursor(self):
        self.job()
        client = InterruptedClient([dialog()], {123: [message(n) for n in range(1, 202)]}, error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(len(self.db.responses()), 1)
        self.assertFalse(json.loads(self.db.setting("responses_scan"))["complete"])
        self.assertIn("123", json.loads(self.db.setting("responses_resume"))["cursors"])

    async def test_resume_overlap_resolves_cv_using_older_incoming_message_at_page_boundary(self):
        self.job()
        selected = self.job("дизайнер")
        history = [message(1, "Вакансия: дизайнер", out=False),
                   message(2, "", file=SimpleNamespace(name="CV.pdf"))]
        history += [message(n, "Спасибо") for n in range(3, 27)]
        client = InterruptedClient([dialog()], {123: history}, fail_after=25, error=FloodWaitError(None, 45))
        await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(self.db.responses()[0]["status"], "ambiguous")
        await scan_responses(client, self.db, 111, 30, now=NOW + timedelta(seconds=46), progress=AsyncMock())
        self.assertEqual(self.db.responses()[0]["job_id"], selected["id"])
        self.assertEqual(self.db.responses()[0]["status"], "linked")

    async def test_explicit_folder_scope_never_enumerates_or_reads_outside_dialogs(self):
        self.job()
        client = FakeClient([dialog(999)], {123: [message()], 999: [message()]})
        client.iter_dialogs = None
        result = await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock(),
                                     dialogs=[dialog()], scope_key="folder:51", scope_title="Удаленки")
        self.assertTrue(result.complete)
        self.assertEqual(client.requested, [123])
        self.assertEqual(result.selected_chats, 1)
        self.assertEqual(result.outgoing, 1)

    async def test_checkpoint_from_other_folder_does_not_skip_selected_hr(self):
        self.job()
        self.db.set_setting("responses_resume", json.dumps({"owner":111,"days":30,"scope_key":"folder:2","done":[123]}))
        client = FakeClient([], {123:[message()]})
        result = await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock(),
                                     dialogs=[dialog()], scope_key="folder:51")
        self.assertFalse(result.resumed)
        self.assertEqual(client.requested, [123])

    async def test_resume_after_cover_letter_counts_as_same_application_for_ambiguous_hr(self):
        self.job()
        self.job("дизайнер")
        client = FakeClient([dialog()], {123:[message(1), message(2,"",file=SimpleNamespace(name="CV.pdf"))]})
        await scan_responses(client, self.db, 111, 30, now=NOW, progress=AsyncMock())
        self.assertEqual(len(self.db.responses()), 1)
        self.assertEqual(self.db.responses()[0]["title"], "Копирайтер")


class RequestTests(unittest.TestCase):
    def test_periods_and_list_pages(self):
        for text, expected in (("", ResponseRequest()), ("all", ResponseRequest(days=None)),
            ("90", ResponseRequest(days=90)), ("stats 7", ResponseRequest("stats", 7)),
            ("list 2 all", ResponseRequest("list", None, 2)), ("help", ResponseRequest("help"))):
            self.assertEqual(parse_response_request(text), expected)
        for text in ("0", "-1", "3651", "90 junk", "list 0", "stats no", "help 90"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "Пример"):
                parse_response_request(text)

    def test_report_percentage_is_based_on_resolved_roles_and_list_is_paginated(self):
        rows = [dict(peer_id=n, username="hr_test", message_id=n, sent_at=NOW.isoformat(), job_id=1,
                     title="Копирайтер", sphere="Контент и творчество", status="linked", reason="контакт") for n in range(11)]
        report = response_report(rows, 30)
        self.assertIn("Копирайтер — 11 (100%)", report)
        listing = response_list(rows, ResponseRequest("list"))
        self.assertEqual(listing.count("Вакансия №"), 10)
        self.assertIn("Ещё: /responses list 2 30", listing)
        self.assertIn("Назад:", response_list(rows, ResponseRequest("list", 30, 2)))
        with self.assertRaises(ValueError):
            response_list(rows, ResponseRequest("list", 30, 3))

    def test_empty_incomplete_report_does_not_claim_that_no_applications_exist(self):
        output = response_report([], 30, incomplete=True)
        self.assertIn("Промежуточная статистика", output)
        self.assertIn("нельзя судить о количестве", output)
        self.assertNotIn("Для сопоставления нужны собранные объявления", output)
