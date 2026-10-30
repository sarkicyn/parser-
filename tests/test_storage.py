import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vacancy_bot.matcher import classify
from vacancy_bot.storage import Storage


TEXT = "Вакансия: ассистент. Удалённо, 4 часа в день. Оклад 50 000 ₽. Без опыта."
SOURCE = dict(peer_id=-1000000000010, raw_id=10, kind="channel", access_hash=1,
              title="TG", username="test_jobs", protected=False)
POST = TEXT + "\nЗадачи: вести таблицы учеников; отвечать на письма клиентов.\nКонтакты: @test_hr"


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent / ".tmp"
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.path = Path(self.tmp.name) / "test.sqlite3"
        self.db = Storage(self.path)
        self.other = dict(SOURCE, peer_id=-1000000000020, raw_id=20, title="Other")
        self.db.sync_sources([SOURCE, self.other])
        self.cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def save(self, message_id, text=POST, source=SOURCE, days_ago=0):
        self.db.save_job(peer_id=source["peer_id"], message_id=message_id, part_index=0, part_count=1,
                         text=text, source_title=source["title"], link=f"https://t.me/test/{message_id}",
                         published_at=(datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
                         protected=False, match=classify(text))
        return self.db.db.execute("SELECT * FROM jobs WHERE peer_id=? AND message_id=?",
                                  (source["peer_id"], message_id)).fetchone()


class StorageDedupTests(StorageTestCase):

    def test_fresh_active_copy_is_visible_when_first_copy_is_old_and_inactive(self):
        first = self.save(1, days_ago=4)
        second = self.save(2, source=self.other)
        self.db.sync_sources([self.other])
        rows = self.db.ranked(self.cutoff, pending=True)
        self.assertEqual([row["id"] for row in rows], [second["id"]])
        self.assertEqual(first["dedup_key"], second["dedup_key"])

    def test_sent_copy_suppresses_fresh_copy_after_full_database_restart(self):
        first = self.save(1, days_ago=4)
        self.db.card_sent(first["id"])
        self.db.delivered(first["id"])
        second = self.save(2, text=POST.replace("вести таблицы", "вести таблицы и расписание"), source=self.other)
        self.db.close()
        self.db = Storage(self.path)
        self.assertEqual(self.db.ranked(self.cutoff, pending=True), [])
        self.assertTrue(self.db.delivery_state(second["id"])["notified_at"])
        self.assertEqual(self.db.stats(self.cutoff), dict(total=1, matched=1, sent=1))

    def test_deduplication_runs_before_result_limit(self):
        self.save(1)
        self.save(2, source=self.other)
        self.save(3, text=POST.replace("ассистент", "дизайнер"))
        self.assertEqual(len(self.db.ranked(self.cutoff, limit=2)), 2)

    def test_edit_merging_groups_preserves_both_delivery_markers(self):
        first = self.save(1)
        second = self.save(2, text=POST.replace("@test_hr", "@other_hr"))
        self.db.card_sent(second["id"])
        self.db.delivered(second["id"])
        edited = self.save(2)
        self.assertEqual(edited["dedup_key"], first["dedup_key"])
        self.assertTrue(self.db.delivery_state(first["id"])["notified_at"])
        self.assertEqual(self.db.ranked(self.cutoff, pending=True), [])

    def test_legacy_reposts_are_grouped_without_resending_or_deleting_rows(self):
        first = self.save(1)
        second = self.save(2, text=POST.replace("вести таблицы", "вести таблицы и расписание"), source=self.other)
        self.db.card_sent(second["id"])
        self.db.delivered(second["id"])
        # Emulate independently saved occurrences before the dedup migration.
        self.db.db.execute("UPDATE jobs SET dedup_key=NULL")
        self.db.db.execute("DELETE FROM job_deliveries")
        self.db.db.commit()
        self.db.close()
        self.db = Storage(self.path)
        self.assertEqual(self.db.job(first["id"])["dedup_key"], self.db.job(second["id"])["dedup_key"])
        self.assertTrue(self.db.job(second["id"])["notified_at"])
        self.assertEqual(self.db.ranked(self.cutoff, pending=True), [])
        self.assertEqual(len(self.db.recent(self.cutoff)), 2)

    def test_obsolete_columns_removal_preserves_jobs_deliveries_and_response_history(self):
        row = self.save(1)
        self.db.delivered(row["id"])
        self.db.set_setting("responses_folder", "51")
        self.db.replace_responses(123, None, [dict(peer_id=123,message_id=8,username="test_hr",
            sent_at=datetime.now(timezone.utc).isoformat(),job_key=row["dedup_key"],job_id=row["id"],
            title="Ассистент",sphere="Административная работа",status="linked",reason="контакт HR")])
        self.db.db.executescript("""ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT 'tg';
            ALTER TABLE jobs ADD COLUMN payload TEXT;
            CREATE INDEX jobs_origin ON jobs(origin,eligible,published_at);
            INSERT INTO sources(peer_id,raw_id,kind,title,active) VALUES(0,0,'retired','retired',1);""")
        self.db.close()
        self.db = Storage(self.path)
        columns = {item["name"] for item in self.db.db.execute("PRAGMA table_info(jobs)")}
        self.assertNotIn("origin", columns)
        self.assertNotIn("payload", columns)
        self.assertEqual(self.db.job(row["id"])["dedup_key"], row["dedup_key"])
        self.assertTrue(self.db.delivery_state(row["id"])["notified_at"])
        self.assertEqual(len(self.db.responses(peer_ids=[123])), 1)
        self.assertEqual(self.db.setting("responses_folder"), "51")
        self.assertIsNone(self.db.source(0))
        self.assertEqual(self.db.ranked(self.cutoff, pending=True), [])


class StorageMigrationTests(unittest.TestCase):
    def test_legacy_database_migration_preserves_ids_and_delivery_markers(self):
        root = Path(__file__).resolve().parent.parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path = Path(directory) / "legacy.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
                peer_id INTEGER NOT NULL,message_id INTEGER NOT NULL,part_index INTEGER NOT NULL,part_count INTEGER NOT NULL,
                text TEXT NOT NULL,source_title TEXT NOT NULL,link TEXT,published_at TEXT NOT NULL,protected INTEGER NOT NULL,
                analysis TEXT NOT NULL,score INTEGER NOT NULL,eligible INTEGER NOT NULL,
                card_sent INTEGER NOT NULL DEFAULT 0,notified_at TEXT,
                UNIQUE(peer_id,message_id,part_index))""")
            published = datetime.now(timezone.utc).isoformat()
            legacy.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (42, "legacy-hash", -1000000000010, 1, 0, 1, TEXT, "TG", "https://t.me/test/1", published, 0,
                 json.dumps(classify(TEXT).to_dict()), 100, 1, 1, published))
            legacy.commit()
            legacy.close()
            storage = Storage(path)
            try:
                row = storage.job(42)
                self.assertEqual((row["id"], row["card_sent"], row["notified_at"]), (42, 1, published))
                columns = {item["name"] for item in storage.db.execute("PRAGMA table_info(jobs)")}
                self.assertNotIn("origin", columns)
                self.assertNotIn("payload", columns)
            finally:
                storage.close()
            # The migration is idempotent on the next startup.
            storage = Storage(path)
            self.assertEqual(storage.job(42)["notified_at"], published)
            storage.close()


class ContactStorageTests(StorageTestCase):
    def test_lookup_includes_distinct_roles_and_rejected_vacancies(self):
        first = self.save(1)
        second = self.save(2, POST.replace("ассистент", "дизайнер").replace("4 часа", "8 часов"))
        self.save(3, POST.replace("@test_hr", "@test_hr_extra"))
        self.assertFalse(second["eligible"])
        rows = self.db.jobs_by_contact("tg:test_hr", self.cutoff)
        self.assertEqual({row["id"] for row in rows}, {first["id"], second["id"]})

    def test_lookup_deduplicates_across_chats_without_delivery_changes(self):
        first = self.save(1)
        second = self.save(2, POST.replace("вести таблицы", "вести таблицы и расписание"), source=self.other)
        self.assertEqual(len(self.db.jobs_by_contact("tg:test_hr", self.cutoff)), 1)
        self.assertIsNone(self.db.delivery_state(first["id"])["notified_at"])
        self.assertIsNone(self.db.delivery_state(second["id"])["notified_at"])

    def test_history_is_explicit_and_recent_search_uses_active_sources(self):
        old = self.save(1, days_ago=4)
        active = self.save(2, POST.replace("ассистент", "дизайнер"))
        inactive = self.save(3, POST.replace("ассистент", "редактор"), source=self.other)
        self.db.sync_sources([SOURCE])
        self.assertEqual([row["id"] for row in self.db.jobs_by_contact("tg:test_hr", self.cutoff)], [active["id"]])
        all_rows = self.db.jobs_by_contact("tg:test_hr")
        self.assertEqual({row["id"] for row in all_rows}, {old["id"], active["id"], inactive["id"]})
        self.assertEqual(next(row for row in all_rows if row["id"] == inactive["id"])["source_active"], 0)

    def test_contact_edits_and_removed_roundup_parts_update_the_index(self):
        row = self.save(1)
        self.save(1, POST.replace("@test_hr", "@new_hr"))
        self.assertEqual(self.db.jobs_by_contact("tg:test_hr"), [])
        self.assertEqual([item["id"] for item in self.db.jobs_by_contact("tg:new_hr")], [row["id"]])
        self.db.trim_message_parts(SOURCE["peer_id"], 1, 0)
        self.assertEqual(self.db.jobs_by_contact("tg:new_hr"), [])
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM job_contacts").fetchone()[0], 0)

    def test_existing_database_is_indexed_on_startup_with_markers_preserved(self):
        row = self.save(1)
        self.db.card_sent(row["id"])
        self.db.delivered(row["id"])
        self.db.db.execute("DROP TABLE job_contacts")
        self.db.db.commit()
        self.db.close()
        self.db = Storage(self.path)
        rows = self.db.jobs_by_contact("tg:test_hr")
        self.assertEqual([item["id"] for item in rows], [row["id"]])
        self.assertTrue(self.db.delivery_state(row["id"])["notified_at"])

    def test_requested_contact_belongs_to_the_displayed_occurrence(self):
        first = self.save(1)
        self.save(2, POST.replace("\nКонтакты: @test_hr", ""), source=self.other)
        rows = self.db.jobs_by_contact("tg:test_hr")
        self.assertEqual([row["id"] for row in rows], [first["id"]])

    def test_ranked_category_and_telegram_age_filters_compose(self):
        self.save(1, days_ago=4)
        self.save(2, TEXT.replace("ассистент", "Data Entry Specialist") + "\nКонтакты: @test_hr")
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows = self.db.ranked(self.cutoff, category=4)
        self.assertEqual(len(rows), 1)
        self.assertIn("Data Entry Specialist", rows[0]["text"])
