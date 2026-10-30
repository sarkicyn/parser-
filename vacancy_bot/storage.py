from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from .matcher import Match
from .categories import CATEGORY_NAMES
from .dedup import Identity, contacts, same_vacancy


class Storage:
    """All access stays on the application event loop; transactions are short."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(
                peer_id INTEGER PRIMARY KEY, raw_id INTEGER NOT NULL, kind TEXT NOT NULL,
                access_hash INTEGER, title TEXT NOT NULL, username TEXT,
                protected INTEGER NOT NULL DEFAULT 0,
                last_id INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                part_index INTEGER NOT NULL, part_count INTEGER NOT NULL,
                text TEXT NOT NULL, source_title TEXT NOT NULL, link TEXT,
                published_at TEXT NOT NULL, protected INTEGER NOT NULL,
                analysis TEXT NOT NULL, score INTEGER NOT NULL, eligible INTEGER NOT NULL,
                card_sent INTEGER NOT NULL DEFAULT 0, notified_at TEXT,
                UNIQUE(peer_id, message_id, part_index)
            );
            CREATE INDEX IF NOT EXISTS jobs_ranking ON jobs(eligible, score DESC, published_at DESC);
            CREATE TABLE IF NOT EXISTS job_deliveries(
                dedup_key TEXT PRIMARY KEY, card_sent INTEGER NOT NULL DEFAULT 0, notified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS job_contacts(
                contact TEXT NOT NULL, job_id INTEGER NOT NULL,
                PRIMARY KEY(contact,job_id)
            );
            CREATE INDEX IF NOT EXISTS job_contacts_job ON job_contacts(job_id);
            CREATE TABLE IF NOT EXISTS application_responses(
                peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                username TEXT NOT NULL, sent_at TEXT NOT NULL,
                job_key TEXT NOT NULL, job_id INTEGER,
                title TEXT NOT NULL, sphere TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(peer_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS application_responses_date ON application_responses(sent_at);
        """)
        # Preserve job IDs and delivery markers across schema revisions.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "dedup_key" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN dedup_key TEXT")
        self.db.execute("DROP INDEX IF EXISTS jobs_origin")
        for unused in ("origin", "payload"):
            if unused in columns:
                self.db.execute(f"ALTER TABLE jobs DROP COLUMN {unused}")
        self.db.execute("DELETE FROM sources WHERE peer_id=0")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs(dedup_key)")
        self.db.commit()
        self._identities: dict[int, Identity] = {}
        self._index: dict[str, set[int]] = defaultdict(set)
        self._groups: dict[int, str] = {}
        self._migrate_duplicates()

    def _remember(self, job_id: int, identity: Identity):
        old = self._identities.get(job_id)
        if old is not None:
            for key in old.index_keys():
                self._index[key].discard(job_id)
        self._identities[job_id] = identity
        for key in identity.index_keys():
            self._index[key].add(job_id)

    def _duplicate_groups(self, identity: Identity, excluding: int | None = None) -> set[str]:
        candidates = set().union(*(self._index.get(key, set()) for key in identity.index_keys()))
        return {self._groups[job_id] for job_id in candidates
                if job_id != excluding and same_vacancy(identity, self._identities[job_id])}

    def _merge_groups(self, target: str, old: str):
        if target == old:
            return
        self.db.execute("""INSERT INTO job_deliveries(dedup_key,card_sent,notified_at)
            SELECT ?,card_sent,notified_at FROM job_deliveries WHERE dedup_key=?
            ON CONFLICT(dedup_key) DO UPDATE SET
                card_sent=max(card_sent,excluded.card_sent),
                notified_at=coalesce(notified_at,excluded.notified_at)""", (target, old))
        self.db.execute("UPDATE jobs SET dedup_key=? WHERE dedup_key=?", (target, old))
        self.db.execute("DELETE FROM job_deliveries WHERE dedup_key=?", (old,))
        for job_id, group in self._groups.items():
            if group == old:
                self._groups[job_id] = target

    def _migrate_duplicates(self):
        # One additive migration also groups existing reposts. IDs and per-message
        # markers survive; the separate ledger retains delivery state after edits.
        rows = self.db.execute("SELECT * FROM jobs WHERE peer_id<0 ORDER BY id").fetchall()
        with self.db:
            self.db.execute("DELETE FROM job_contacts WHERE job_id NOT IN(SELECT id FROM jobs)")
            for row in rows:
                self._write_contacts(row["id"], row["text"])
                key = row["dedup_key"] or row["fingerprint"]
                self._groups[row["id"]] = key
                self.db.execute("UPDATE jobs SET dedup_key=? WHERE id=?", (key, row["id"]))
                self.db.execute("""INSERT INTO job_deliveries VALUES(?,?,?)
                    ON CONFLICT(dedup_key) DO UPDATE SET
                    card_sent=max(card_sent,excluded.card_sent),
                    notified_at=coalesce(notified_at,excluded.notified_at)""",
                    (key, row["card_sent"], row["notified_at"]))
            for row in rows:
                identity = Identity.from_text(row["text"])
                if row["dedup_key"] is None:
                    groups = self._duplicate_groups(identity)
                    if groups:
                        target = min(groups)
                        for old in groups | {self._groups[row["id"]]}:
                            self._merge_groups(target, old)
                self._remember(row["id"], identity)

    def _write_contacts(self, job_id: int, text: str):
        self.db.execute("DELETE FROM job_contacts WHERE job_id=?", (job_id,))
        self.db.executemany("INSERT INTO job_contacts(contact,job_id) VALUES(?,?)",
                            ((contact, job_id) for contact in contacts(text)))

    def close(self):
        self.db.close()

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def sync_sources(self, records: list[dict]):
        with self.db:
            self.db.execute("UPDATE sources SET active=0")
            for record in records:
                self.db.execute("""
                    INSERT INTO sources(peer_id,raw_id,kind,access_hash,title,username,protected)
                    VALUES(:peer_id,:raw_id,:kind,:access_hash,:title,:username,:protected)
                    ON CONFLICT(peer_id) DO UPDATE SET active=1,
                    access_hash=excluded.access_hash,title=excluded.title,username=excluded.username,
                    protected=excluded.protected
                """, record)

    def sources(self):
        return self.db.execute("SELECT * FROM sources WHERE active=1 AND peer_id<0 ORDER BY title").fetchall()

    def source(self, peer_id: int):
        return self.db.execute("SELECT * FROM sources WHERE peer_id=?", (peer_id,)).fetchone()

    def advance(self, peer_id: int, message_id: int):
        with self.db:
            self.db.execute("UPDATE sources SET last_id=max(last_id,?),error=NULL WHERE peer_id=?", (message_id, peer_id))

    def source_error(self, peer_id: int, error: str):
        with self.db:
            self.db.execute("UPDATE sources SET error=? WHERE peer_id=?", (error, peer_id))

    def save_job(self, *, peer_id: int, message_id: int, part_index: int, part_count: int,
                 text: str, source_title: str, link: str | None, published_at: str,
                 protected: bool, match: Match):
        if peer_id >= 0:
            raise ValueError("Источник вакансий должен быть Telegram-каналом или группой")
        # Keep each source occurrence so a stale/inactive original cannot hide a
        # fresh repost. Group delivery, rather than deleting source records.
        fingerprint = hashlib.sha256(f"tg:{peer_id}:{message_id}:{part_index}".encode()).hexdigest()
        identity = Identity.from_text(text)
        existing = self.db.execute("SELECT id,dedup_key FROM jobs WHERE peer_id=? AND message_id=? AND part_index=?",
                                   (peer_id, message_id, part_index)).fetchone()
        groups = self._duplicate_groups(identity, existing["id"] if existing else None)
        own_key = existing["dedup_key"] if existing else fingerprint
        key = min(groups) if groups else own_key
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO job_deliveries(dedup_key) VALUES(?)", (key,))
            for old in groups | {own_key}:
                self._merge_groups(key, old)
            cursor = self.db.execute("""
                INSERT INTO jobs(fingerprint,peer_id,message_id,part_index,part_count,text,
                    source_title,link,published_at,protected,analysis,score,eligible,dedup_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(peer_id,message_id,part_index) DO UPDATE SET
                    fingerprint=excluded.fingerprint,text=excluded.text,
                    part_count=excluded.part_count,source_title=excluded.source_title,
                    link=excluded.link,protected=excluded.protected,
                    analysis=excluded.analysis,score=excluded.score,eligible=excluded.eligible,
                    published_at=excluded.published_at,
                    dedup_key=excluded.dedup_key
            """, (fingerprint, peer_id, message_id, part_index, part_count, text,
                  source_title, link, published_at, protected,
                  json.dumps(match.to_dict(), ensure_ascii=False), match.score, int(match.eligible), key))
            job_id = existing["id"] if existing else cursor.lastrowid
            self._write_contacts(job_id, text)
        self._groups[job_id] = key
        self._remember(job_id, identity)

    def trim_message_parts(self, peer_id: int, message_id: int, part_count: int):
        removed = self.db.execute("SELECT id FROM jobs WHERE peer_id=? AND message_id=? AND part_index>=?",
                                  (peer_id, message_id, part_count)).fetchall()
        with self.db:
            self.db.executemany("DELETE FROM job_contacts WHERE job_id=?", ((row["id"],) for row in removed))
            self.db.execute("DELETE FROM jobs WHERE peer_id=? AND message_id=? AND part_index>=?",
                            (peer_id, message_id, part_count))
        for row in removed:
            job_id = row["id"]
            identity = self._identities.pop(job_id, None)
            if identity:
                for key in identity.index_keys():
                    self._index[key].discard(job_id)
            self._groups.pop(job_id, None)

    def reclassify(self, job_id: int, match: Match):
        with self.db:
            self.db.execute("UPDATE jobs SET analysis=?,score=?,eligible=? WHERE id=?",
                            (json.dumps(match.to_dict(), ensure_ascii=False), match.score, int(match.eligible), job_id))

    def recent(self, cutoff: str):
        return self.db.execute("SELECT * FROM jobs WHERE peer_id<0 AND published_at>=?", (cutoff,)).fetchall()

    def tg_candidates(self, cutoff: str):
        # Independent selections must also see jobs rejected by the personal
        # profile. Deduplicate after their own eligibility checks.
        return self.db.execute("""SELECT jobs.*,sources.protected AS source_protected
            FROM jobs JOIN sources USING(peer_id)
            WHERE jobs.peer_id<0 AND sources.active=1 AND jobs.published_at>=?
            ORDER BY jobs.published_at DESC,jobs.id DESC""", (cutoff,)).fetchall()

    def ranked(self, cutoff: str, limit: int = 20, pending: bool = False, category: int | None = None):
        if category is not None and category not in CATEGORY_NAMES:
            raise ValueError("Категория: число от 1 до 4")
        parameters = [cutoff]
        category_filter = ""
        if category is not None:
            category_filter += " AND json_extract(jobs.analysis, '$.category') = ?"
            parameters.append(category)
        parameters.append(limit)
        return self.db.execute("""WITH candidates AS (
            SELECT jobs.*,row_number() OVER(PARTITION BY jobs.dedup_key
                ORDER BY jobs.notified_at IS NOT NULL DESC,jobs.card_sent DESC,
                         jobs.score DESC,jobs.published_at DESC,jobs.id) AS occurrence
            FROM jobs JOIN sources USING(peer_id)
            WHERE jobs.peer_id<0 AND jobs.eligible=1 AND jobs.published_at>=? AND sources.active=1
        """ + (" AND NOT EXISTS(SELECT 1 FROM job_deliveries d WHERE d.dedup_key=jobs.dedup_key AND d.notified_at IS NOT NULL)"
               if pending else "") + category_filter + """
            ) SELECT * FROM candidates WHERE occurrence=1
            ORDER BY score DESC,published_at DESC LIMIT ?
        """, parameters).fetchall()

    def jobs_by_contact(self, contact: str, cutoff: str | None = None):
        parameters = [contact]
        filters = ""
        if cutoff is not None:
            filters += " AND jobs.published_at>=? AND sources.active=1"
            parameters.append(cutoff)
        # Filter before choosing a representative so the displayed occurrence
        # actually contains the requested contact and is inside the time window.
        return self.db.execute("""WITH matches AS (
            SELECT jobs.*,coalesce(sources.active,0) AS source_active,
                   row_number() OVER(PARTITION BY jobs.dedup_key
                       ORDER BY jobs.published_at DESC,jobs.id) AS occurrence
            FROM job_contacts JOIN jobs ON jobs.id=job_contacts.job_id
            LEFT JOIN sources ON sources.peer_id=jobs.peer_id
            WHERE jobs.peer_id<0 AND job_contacts.contact=?
        """ + filters + """
            ) SELECT * FROM matches WHERE occurrence=1
            ORDER BY published_at DESC,id
        """, parameters).fetchall()

    def job(self, job_id: int):
        return self.db.execute("SELECT * FROM jobs WHERE id=? AND peer_id<0", (job_id,)).fetchone()

    def response_jobs(self):
        # Historical and rejected jobs are evidence too, regardless of source activity.
        return self.db.execute("""SELECT jobs.*,coalesce(sources.protected,0) AS source_protected
            FROM jobs LEFT JOIN sources USING(peer_id) WHERE jobs.peer_id<0 ORDER BY jobs.id""").fetchall()

    def replace_responses(self, peer_id: int, cutoff: str | None, responses: list[dict], *,
                          before_id: int | None = None, min_id: int | None = None):
        filters, parameters = ["peer_id=?"], [peer_id]
        for condition, value in (("sent_at>=?", cutoff), ("message_id<?", before_id), ("message_id>=?", min_id)):
            if value is not None:
                filters.append(condition)
                parameters.append(value)
        with self.db:
            # Partial scans replace only the segment actually read. Responses in
            # older, unread history survive a rate limit or interrupted refresh.
            self.db.execute("DELETE FROM application_responses WHERE " + " AND ".join(filters), parameters)
            self.db.executemany("""INSERT INTO application_responses(
                peer_id,message_id,username,sent_at,job_key,job_id,title,sphere,status,reason)
                VALUES(:peer_id,:message_id,:username,:sent_at,:job_key,:job_id,:title,:sphere,:status,:reason)
                ON CONFLICT(peer_id,message_id) DO UPDATE SET
                username=excluded.username,sent_at=excluded.sent_at,job_key=excluded.job_key,
                job_id=excluded.job_id,title=excluded.title,sphere=excluded.sphere,
                status=excluded.status,reason=excluded.reason""", responses)

    def responses(self, cutoff: str | None = None, *, peer_ids=None):
        rows = self.db.execute("""SELECT r.*,coalesce(j.dedup_key,r.job_key) AS current_job_key,
            j.link AS job_link,j.analysis AS job_analysis,j.protected AS job_protected,
            coalesce(s.protected,0) AS source_protected
            FROM application_responses r LEFT JOIN jobs j ON j.id=r.job_id
            LEFT JOIN sources s ON s.peer_id=j.peer_id
            ORDER BY r.sent_at,r.message_id""").fetchall()
        unique = {}
        allowed = set(peer_ids) if peer_ids is not None else None
        for source in rows:
            row = dict(source)
            if allowed is not None and row["peer_id"] not in allowed:
                continue
            # First observed application to this vacancy, even when follow-ups
            # fall into a newer report period. Repost group merges stay deduplicated.
            unique.setdefault((row["peer_id"], row["current_job_key"]), row)
        return sorted((row for row in unique.values() if cutoff is None or row["sent_at"] >= cutoff),
                      key=lambda row: row["sent_at"], reverse=True)

    def card_sent(self, job_id: int):
        with self.db:
            self.db.execute("UPDATE jobs SET card_sent=1 WHERE id=?", (job_id,))
            self.db.execute("UPDATE job_deliveries SET card_sent=1 WHERE dedup_key=(SELECT dedup_key FROM jobs WHERE id=?)", (job_id,))

    def delivery_state(self, job_id: int):
        return self.db.execute("SELECT d.* FROM job_deliveries d JOIN jobs USING(dedup_key) WHERE jobs.id=?", (job_id,)).fetchone()

    def delivered(self, job_id: int):
        with self.db:
            self.db.execute("UPDATE jobs SET notified_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            self.db.execute("UPDATE job_deliveries SET notified_at=CURRENT_TIMESTAMP WHERE dedup_key=(SELECT dedup_key FROM jobs WHERE id=?)", (job_id,))

    def stats(self, cutoff: str):
        return dict(self.db.execute("""
            SELECT count(DISTINCT dedup_key) AS total,
                count(DISTINCT CASE WHEN eligible=1 THEN dedup_key END) AS matched,
                count(DISTINCT CASE WHEN d.notified_at IS NOT NULL THEN dedup_key END) AS sent
            FROM jobs LEFT JOIN job_deliveries d USING(dedup_key) WHERE peer_id<0 AND published_at>=?
        """, (cutoff,)).fetchone())
