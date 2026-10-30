"""Local application detection. Telegram history is read only on request."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError, RPCError

from .categories import categorize
from .dedup import contacts, normalize, words
from .description import extract_description, shorten


RESPONSE_HELP = """Статистика откликов · /responses

/responses — прочитать личные чаты HR папки «Удаленки» и посчитать отклики за 30 дней
/responses folder название (или ID) — выбрать папку HR, отдельно от каналов вакансий
/responses 90 — за 90 дней
/responses all — за всю доступную историю (может занять много времени)
/responses stats — сохранённый отчёт без чтения Telegram
/responses list — список найденных откликов; следующая страница: /responses list 2
/responses stats all · /responses list 2 all — вся сохранённая история

Сопоставляю юзернеймы HR с контактами в сохранённых вакансиях Telegram, включая архив и объявления вне твоих фильтров. Ищу исходящие обращения о вакансии и отправку резюме. Обычное приветствие или входящее предложение не подтверждает отклик.
Ссылка и название помогают выбрать вакансию, когда у HR их несколько. Неоднозначные обращения показываю отдельно. Без объявления должность определяется только по тексту обращения и помечается как предположение.

Одна вакансия у одного HR считается один раз. Сферы — профессиональные направления, например маркетинг или IT. Анализ запускается по команде; переписка целиком и файлы резюме в базу не сохраняются.
При ограничении Telegram найденное сохраняется. Короткое ожидание выполняется автоматически; если анализ остановился, повтор команды с тем же периодом продолжит его с сохранённого места. Известные HR проверяются первыми.
"""
PAGE_SIZE = 10
# Canonical occupations keep inflections and synonyms in the same statistic.
ROLE_RULES = (
    ("Продуктовый аналитик", "Аналитика", r"продуктов\w*\s+аналитик\w*|product\s+analyst"),
    ("Аналитик", "Аналитика", r"аналитик\w*|(?:data|business|system|financial|bi)\s+analyst|data\s+scientist"),
    ("Менеджер продукта", "Продукты и проекты", r"продакт\w*|продуктов\w*\s+менеджер\w*|менеджер\w*\s+продукт\w*|product\s+(?:manager|owner)"),
    ("Менеджер проектов", "Продукты и проекты", r"проджект\w*|проектн\w*\s+менеджер\w*|(?:менеджер\w*|руководител\w*|координатор\w*)\s+проект\w*|project\s+(?:manager|coordinator)|delivery\s+manager"),
    ("SMM-специалист", "Маркетинг", r"smm(?:[- ]\w+)?|social\s+media\s+(?:manager|specialist)"),
    ("Копирайтер", "Контент и творчество", r"копирайтер\w*|copy\s?writer\w*|райтер\w*|content\s+writer"),
    ("Редактор", "Контент и творчество", r"редактор\w*|корректор\w*|editor\w*|proofreader\w*"),
    ("Контент-менеджер", "Контент и творчество", r"контент[- ]?(?:менеджер\w*|мейкер\w*|креатор\w*)|content\s+(?:manager|creator)"),
    ("Сценарист", "Контент и творчество", r"сценарист\w*|script\s?writer\w*"),
    ("Оператор чата", "Поддержка и модерация", r"(?:оператор\w*\s+(?:(?:онлайн|online)[- ]?)?чат\w*|чат[- ]оператор\w*|chat\s+(?:operator|agent|support))"),
    ("Модератор", "Поддержка и модерация", r"модератор\w*|moderator\w*"),
    ("Специалист поддержки", "Поддержка и модерация", r"(?:специалист\w*|оператор\w*)\s+(?:техническ\w*\s+|клиентск\w*\s+)?(?:поддержк\w*|техподдержк\w*)|(?:customer|technical)\s+support|support\s+(?:agent|specialist)"),
    ("Менеджер по продажам", "Продажи", r"менеджер\w*\s+(?:по\s+)?продаж\w*|sales\s+(?:manager|specialist|representative)"),
    ("Аккаунт-менеджер", "Продажи", r"аккаунт[- ]?менеджер\w*|менеджер\w*\s+по\s+работе\s+с\s+клиент\w*|(?:account|customer\s+success)\s+manager"),
    ("Маркетолог", "Маркетинг", r"маркетолог\w*|таргетолог\w*|директолог\w*|(?:seo|ppc|performance|digital|marketing|brand)[- ](?:специалист\w*|менеджер\w*|manager|specialist|marketer)"),
    ("Тестировщик", "Разработка и IT", r"тестировщик\w*|qa(?:[- ]engineer)?"),
    ("Разработчик", "Разработка и IT", r"разработчик\w*|программист\w*|developer\w*|software\s+engineer"),
    ("Системный администратор / DevOps", "Разработка и IT", r"devops\w*|sre|системн\w*\s+администратор\w*"),
    ("Дизайнер", "Дизайн", r"дизайнер\w*|designer\w*"),
    ("Рекрутер / HR", "Подбор персонала", r"рекрутер\w*|recruiter\w*|hr[- ](?:менеджер\w*|manager)|менеджер\w*\s+по\s+персонал\w*"),
    ("Бухгалтер", "Финансы и учёт", r"бухгалтер\w*|accountant\w*"),
    ("Ассистент", "Административная работа", r"ассистент\w*|помощник\w*|assistant\w*"),
)
INTENT = re.compile(
    r"хочу\s+отклик\w*|откликаюсь|отклик(?:аюсь|нуться|\s+на)|"
    r"(?:заинтересова\w*|интересует|интересна|интересен|по\s+поводу|насчет|пишу\s+по|вопрос\s+по|по)\s+(?:ваш\w*\s+)?(?:ваканси\w*|позици\w*|должност\w*)|"
    r"(?:ваканси\w*|позици\w*)[^\n.!?]{0,60}(?:актуальн\w*|открыт\w*|интерес\w*)|"
    r"рассмотр\w*\s+(?:мою\s+)?кандидатур\w*|"
    r"(?:направляю|отправляю|прикрепляю|прилагаю|высылаю)\s+(?:вам\s+|свое\s+|мое\s+)?(?:резюме|cv)|"
    r"(?:apply|applying|application)\s+(?:for|to)|interested\s+in\s+(?:the\s+)?(?:role|position|vacancy)", re.I,
)
NEGATIVE = re.compile(
    r"не\s+(?:хочу\s+отклик\w*|откликаюсь|интересует|интересна|рассматриваю|ищу\s+работу)|"
    r"(?:отзываю|отменяю)\s+(?:свой\s+)?отклик|not\s+interested|withdraw\s+my\s+application", re.I,
)
RESUME = re.compile(r"резюме|resume|curriculum|(?<![a-zа-я0-9])cv(?![a-zа-я0-9])", re.I)
JOB_LINK = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:c/)?[\w]+/\d+", re.I)


def utc_date(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def link_key(value: str) -> str:
    value = re.sub(r"^https?://", "", value.casefold()).replace("telegram.me/", "t.me/")
    return value.rstrip("/")


def links(text: str) -> set[str]:
    return {link_key(match[0]) for match in JOB_LINK.finditer(text)}


def roles(text: str) -> list[tuple[str, str]]:
    body = normalize(text)
    found = []
    for title, sphere, pattern in ROLE_RULES:
        pattern = r"(?<!\w)(?:" + pattern + r")(?!\w)"
        if re.search(pattern, body):
            found.append((title, sphere))
            # A specific role (product analyst) should not also vote for its
            # generic synonym (analyst), while distinct roles remain distinct.
            body = re.sub(pattern, lambda match: " " * len(match[0]), body)
    return found


def occupation(title: str, text: str = "") -> tuple[str, str]:
    found = roles(title)
    if found:
        return found[0]
    sphere = {1: "Контент и творчество", 2: "Поддержка и модерация", 3: "Управление и продажи"}.get(categorize(text, title), "Не определена")
    return title or "Должность не определена", sphere


def topic(text: str) -> str:
    # Do not use previous jobs listed in a cover letter as the target position.
    sentences = re.split(r"[\n.!?]+", text)
    for sentence in sentences:
        if re.search(r"ваканси|позици|должност|apply|applying|interested in", sentence, re.I):
            return re.split(r"\b(?:мой\s+опыт|ранее|раньше|работал\w*)\b", sentence, maxsplit=1, flags=re.I)[0]
    return text[:500]


@dataclass
class Response:
    peer_id: int
    username: str
    message_id: int
    sent_at: str
    job_key: str
    job_id: int | None
    title: str
    sphere: str
    status: str
    reason: str


class Catalog:
    def __init__(self, rows):
        self.by_contact = defaultdict(list)
        self.by_link = defaultdict(list)
        for source in rows:
            row = dict(source)
            analysis = json.loads(row["analysis"])
            if not analysis.get("facts", {}).get("is_vacancy"):
                continue
            row["title"] = (analysis.get("description") or {}).get("title") or extract_description(row["text"]).title
            row["role"], row["sphere"] = occupation(row["title"], row["text"])
            row["date"] = utc_date(row["published_at"])
            for contact in contacts(row["text"]):
                self.by_contact[contact].append(row)
            if row["link"]:
                for key in links(row["link"]):
                    self.by_link[key].append(row)

    @staticmethod
    def available(rows, when: datetime):
        result = {}
        for row in rows:
            # Future reposts must not hide an older occurrence available at contact time.
            if row["date"] is not None and row["date"] <= when + timedelta(minutes=5):
                key = row["dedup_key"] or row["fingerprint"]
                if key not in result or row["date"] > result[key]["date"]:
                    result[key] = row
        return list(result.values())

    def detect(self, peer_id: int, usernames: list[str], message, context: str = "") -> Response | None:
        if (not getattr(message, "out", False) or getattr(message, "fwd_from", None)
                or getattr(message, "action", None)):
            return None
        when = utc_date(getattr(message, "date", None))
        if when is None:
            return None
        text = getattr(message, "message", "") or ""
        if NEGATIVE.search(normalize(text)):
            return None
        candidates = self.available([row for name in usernames for row in self.by_contact.get("tg:" + name, ())], when)
        direct = self.available([row for key in links(text) for row in self.by_link.get(key, ())], when)
        filename = getattr(getattr(message, "file", None), "name", "") or ""
        resume = bool(RESUME.search(filename) or re.search(r"^\s*(?:(?:вот|мое|мое\s+актуальное)\s+)?(?:резюме|cv)\b", normalize(text)))
        intent = bool(INTENT.search(normalize(text)))
        if not intent and not (candidates and resume) and not direct:
            return None
        focus = topic(text)
        # A nearby recruiter's message can identify what a bare CV replies to.
        if not roles(focus) and not links(focus) and context:
            focus = topic(context)
            contextual = self.available([row for key in links(context) for row in self.by_link.get(key, ())], when)
            direct = direct or contextual
        found_roles = {role for role, _ in roles(focus)}
        selected, reason = None, ""
        if len(direct) == 1:
            selected, reason = direct[0], "ссылка на вакансию в обращении или ответе"
        else:
            pool = direct or candidates
            exact = [row for row in pool if len(words(row["title"])) >= 2
                     and words(row["title"]) <= words(focus)]
            by_role = [row for row in pool if row["role"] in found_roles]
            if len(exact) == 1:
                selected, reason = exact[0], "юзернейм HR и название вакансии"
            elif len(by_role) == 1:
                selected, reason = by_role[0], "юзернейм HR и должность в переписке"
            elif not direct and len(candidates) == 1 and not found_roles and not links(text):
                selected, reason = candidates[0], "юзернейм HR: одна сохранённая вакансия на дату обращения"
        base = dict(peer_id=peer_id, username=usernames[0] if usernames else "", message_id=message.id, sent_at=when.isoformat())
        if selected:
            protected = selected["protected"] or selected.get("source_protected")
            return Response(**base, job_key=selected["dedup_key"] or selected["fingerprint"], job_id=selected["id"],
                            title="Вакансия из " + selected["source_title"] if protected else selected["role"],
                            sphere=selected["sphere"], status="linked", reason=reason)
        plausible = direct or [row for row in candidates if not found_roles or row["role"] in found_roles]
        if len(plausible) > 1:
            return Response(**base, job_key="ambiguous", job_id=None, title="Вакансия не уточнена", sphere="Не определена",
                            status="ambiguous", reason=f"у HR несколько подходящих вакансий ({len(plausible)}); нужна ссылка или точное название")
        inferred = roles(focus)
        title, sphere = inferred[0] if len(inferred) == 1 else ("Должность не определена", "Не определена")
        return Response(**base, job_key="inferred:" + title, job_id=None, title=title, sphere=sphere,
                        status="inferred" if len(inferred) == 1 else "unmatched",
                        reason="должность из текста, объявление не найдено" if len(inferred) == 1 else "обращение о работе без определённой вакансии")


@dataclass
class ScanResult:
    chats: int = 0
    messages: int = 0
    errors: int = 0
    complete: bool = False
    flood_seconds: int = 0
    partial_chats: int = 0
    resumed: bool = False
    selected_chats: int = 0
    outgoing: int = 0


def entity_usernames(entity) -> list[str]:
    names = [normalize(entity.username)] if getattr(entity, "username", None) else []
    for item in getattr(entity, "usernames", ()) or ():
        if getattr(item, "active", False) and item.username and normalize(item.username) not in names:
            names.append(normalize(item.username))
    return names


def detect_messages(catalog: Catalog, peer_id: int, usernames: list[str], messages) -> list[dict]:
    found, recent_incoming = [], None
    recent_application = None
    by_id = {item.id: item for item in messages}
    for message in sorted(messages, key=lambda item: (utc_date(item.date), item.id)):
        if not getattr(message, "out", False):
            if not getattr(message, "fwd_from", None) and not getattr(message, "action", None):
                recent_incoming = message
            continue
        reply = by_id.get(getattr(message, "reply_to_msg_id", None))
        context = reply or recent_incoming
        if (context is not None and not getattr(context, "out", False)
                and timedelta(0) <= utc_date(message.date) - utc_date(context.date) <= timedelta(days=1)):
            context_text = getattr(context, "message", "") or ""
        else:
            context_text = ""
        response = catalog.detect(peer_id, usernames, message, context_text)
        filename = getattr(getattr(message, "file", None), "name", "") or ""
        text = getattr(message, "message", "") or ""
        bare_resume = bool(RESUME.search(filename) or re.fullmatch(r"\s*(?:(?:вот|мое)\s+)?(?:резюме|cv)[\s.!]*", normalize(text)))
        if (bare_resume and recent_application and not roles(topic(text)) and not links(text)
                and not getattr(message, "fwd_from", None) and not getattr(message, "action", None)
                and not NEGATIVE.search(normalize(text))
                and timedelta(0) <= utc_date(message.date) - utc_date(recent_application.sent_at) <= timedelta(days=1)):
            response = replace(recent_application, message_id=message.id, sent_at=utc_date(message.date).isoformat(),
                               reason="резюме в продолжение того же отклика в переписке")
        if response:
            found.append(asdict(response))
            if response.status in ("linked", "inferred"):
                recent_application = response
    return found


async def scan_responses(client, db, owner: int, days: int | None, *, now: datetime, progress,
                         dialogs=None, scope_key: str = "", scope_title: str = "") -> ScanResult:
    catalog = Catalog(db.response_jobs())
    checkpoint = json.loads(db.setting("responses_resume", "{}"))
    resumed = (checkpoint.get("owner") == owner and checkpoint.get("days") == days
               and checkpoint.get("scope_key", "") == scope_key)
    cutoff = utc_date(checkpoint.get("cutoff")) if resumed else (now - timedelta(days=days) if days else None)
    done = set(checkpoint.get("done", ())) if resumed else set()
    cursors = dict(checkpoint.get("cursors", {})) if resumed else {}
    allowed_ids = {item.entity.id for item in dialogs} if dialogs is not None else None
    if allowed_ids is not None:
        done.intersection_update(allowed_ids)
        cursors = {key:value for key,value in cursors.items() if int(key) in allowed_ids}
    result = ScanResult(chats=len(done), messages=checkpoint.get("messages", 0) if resumed else 0, resumed=resumed,
                        selected_chats=len(allowed_ids) if allowed_ids is not None else 0,
                        outgoing=checkpoint.get("outgoing", 0) if resumed else 0)
    started = last_progress = time.monotonic()
    automatic_waits = 0

    def save_checkpoint():
        result.partial_chats = len(cursors)
        db.set_setting("responses_scan", json.dumps({**asdict(result), "at": now.isoformat(), "days": days,
                                                   "scope_key":scope_key, "folder_title":scope_title}))
        db.set_setting("responses_resume", json.dumps({
            "owner": owner, "days": days, "cutoff": cutoff.isoformat() if cutoff else None,
            "done": sorted(done), "cursors": cursors, "messages": result.messages,
            "scope_key":scope_key, "outgoing":result.outgoing,
            "retry_at": (now + timedelta(seconds=time.monotonic() - started + result.flood_seconds)).isoformat()
                        if result.flood_seconds else None,
        }))

    save_checkpoint()

    async def tick():
        nonlocal last_progress
        if time.monotonic() - last_progress >= 20:
            await progress(f"Анализ откликов{(' · ' + scope_title) if scope_title else ''}: прочитано чатов {result.chats}"
                           + (f"/{result.selected_chats}" if result.selected_chats else "") +
                           f", сообщений {result.messages}, исходящих {result.outgoing}…")
            last_progress = time.monotonic()

    async def read_dialog(dialog):
        nonlocal automatic_waits
        entity = dialog.entity
        if entity.id in done:
            return
        names = entity_usernames(entity)
        initial_offset = int(cursors.get(str(entity.id), 0))
        offset = initial_offset
        messages, finished, failure = [], False, None
        detected, analyzed = {}, 0

        def save_segment(complete):
            nonlocal analyzed
            if messages or complete:
                # Analyze only the new page plus context at its boundary while
                # downloading. A final pass resolves longer reply chains without
                # reclassifying the entire history for every page checkpoint.
                segment = messages if complete else messages[max(0, analyzed - 20):]
                for item in segment:
                    detected.pop(item.id, None)
                for response in detect_messages(catalog, entity.id, names, segment):
                    detected[response["message_id"]] = response
                analyzed = len(messages)
                db.replace_responses(entity.id, cutoff.isoformat() if cutoff else None,
                    list(detected.values()),
                    before_id=initial_offset or None,
                    min_id=min(item.id for item in messages) if messages and not complete else None)
            if complete:
                done.add(entity.id)
                cursors.pop(str(entity.id), None)
            else:
                ids = sorted((item.id for item in messages), reverse=True)
                # A small overlap can resolve a CV at the page boundary using
                # the older recruiter's message when the scan is resumed.
                cursors[str(entity.id)] = ids[-20] + 1 if len(ids) > 20 else (min(ids) if ids else initial_offset)
            result.chats = len(done)
            save_checkpoint()

        try:
            while not finished:
                try:
                    # Sequential page requests and pauses reduce contention with
                    # Telegram's history limits. Never retry before FloodWait expires.
                    options = {"limit": None, "wait_time": 3}
                    if offset:
                        options["offset_id"] = offset
                    iterator = client.iter_messages(getattr(dialog, "input_entity", entity), **options).__aiter__()
                    while True:
                        try:
                            message = await asyncio.wait_for(anext(iterator), timeout=30)
                        except StopAsyncIteration:
                            break
                        when = utc_date(getattr(message, "date", None))
                        if when is None:
                            continue
                        if cutoff and when < cutoff:
                            break
                        if when > now + timedelta(minutes=5):
                            continue
                        messages.append(message)
                        offset = message.id
                        result.messages += 1
                        if getattr(message, "out", False):
                            result.outgoing += 1
                        # Durable page checkpoints also survive a bot restart.
                        if len(messages) % 100 == 0:
                            save_segment(False)
                        await tick()
                    finished = True
                except FloodWaitError as exc:
                    failure = exc
                    result.flood_seconds = exc.seconds
                    save_segment(False)
                    if 0 < exc.seconds <= 30 and automatic_waits < 2:
                        automatic_waits += 1
                        await progress(f"Telegram просит подождать {exc.seconds} сек. Найденные отклики сохранены; продолжу автоматически после ожидания.")
                        await asyncio.sleep(exc.seconds)
                        result.flood_seconds = 0
                        failure = None
                        continue
                    raise
                except (RPCError, ValueError, OSError) as exc:
                    failure = exc
                    result.errors += 1
                    break
        finally:
            # Even an interrupted first chat must contribute the responses that
            # have already been read, instead of leaving a misleading zero report.
            save_segment(finished)
        if failure is None and finished:
            await asyncio.sleep(1)

    async def selected_dialogs():
        if dialogs is not None:
            for item in dialogs:
                yield item
        else:
            async for item in client.iter_dialogs(limit=None):
                yield item

    try:
        deferred = []
        async for dialog in selected_dialogs():
            entity = dialog.entity
            if (not dialog.is_user or entity.id == owner or getattr(entity, "bot", False)
                    or getattr(entity, "deleted", False)):
                continue
            latest = utc_date(getattr(dialog, "date", None))
            if cutoff and latest and latest < cutoff:
                if allowed_ids is not None:
                    done.add(entity.id)
                    result.chats = len(done)
                continue
            if any(catalog.by_contact.get("tg:" + name) for name in entity_usernames(entity)):
                await read_dialog(dialog)
            else:
                deferred.append(dialog)
        # A busy personal chat should not delay all known recruiters. Other
        # private dialogs are still checked for applications to previously unseen HR.
        for dialog in deferred:
            await read_dialog(dialog)
        result.complete = not result.errors and not cursors
    except FloodWaitError as exc:
        result.flood_seconds = exc.seconds
    except (RPCError, ValueError, OSError):
        result.errors += 1
    finally:
        save_checkpoint()
        if result.complete:
            db.set_setting("responses_resume", "{}")
    return result


@dataclass(frozen=True)
class ResponseRequest:
    action: str = "scan"
    days: int | None = 30
    page: int = 1
    selection: str = ""


def parse_response_request(arguments: str) -> ResponseRequest:
    parts = arguments.casefold().split()
    if parts and parts[0] == "folder":
        return ResponseRequest(action="folder", selection=" ".join(arguments.split()[1:]))
    action = parts.pop(0) if parts and parts[0] in ("stats", "list", "help") else "scan"
    if action == "help" and not parts:
        return ResponseRequest(action="help")
    try:
        page = int(parts.pop(0)) if action == "list" and parts and parts[0] != "all" else 1
        period = parts.pop(0) if parts else "30"
        days = None if period == "all" else int(period)
        if parts or page < 1 or (days is not None and not 1 <= days <= 3650) or action == "help":
            raise ValueError
        return ResponseRequest(action, days, page)
    except ValueError:
        raise ValueError("Пример: /responses, /responses 90, /responses all, /responses stats all, /responses list 2 all.\nПериод: 1–3650 дней или all. Справка: /responses help") from None


def response_report(rows, days: int | None, *, incomplete: bool = False) -> str:
    resolved = [row for row in rows if row["status"] in ("linked", "inferred")]
    linked = sum(row["status"] == "linked" for row in rows)
    inferred = len(resolved) - linked
    lines = [("Промежуточная статистика откликов · " if incomplete else "Статистика откликов · ") +
             (f"последние {days} дней" if days else "вся сохранённая история"),
             f"Найдено обращений: {len(rows)} · HR: {len({row['peer_id'] for row in rows})}",
             f"Связаны с вакансиями: {linked} · Должность предположена: {inferred}",
             f"Без определённой вакансии: {len(rows) - len(resolved)}"]
    for label, field in (("Чаще всего откликаешься на должности", "title"), ("По профессиональным сферам", "sphere")):
        counts = Counter(row[field] for row in resolved)
        lines.append("\n" + label + ":")
        lines.extend(f"{index}. {name} — {count} ({count / len(resolved):.0%})" for index, (name, count) in enumerate(counts.most_common(10), 1))
        if not counts:
            lines.append("Пока нет определённых откликов.")
    lines.append("\nПроценты — среди обращений с определённой должностью, включая предположения. Повторы у одного HR на одну вакансию объединены.")
    lines.append("Дата отклика — первое найденное обращение. Анализ /responses all помогает отличить давний отклик от недавней переписки по нему.")
    if not rows:
        if incomplete:
            lines.append("Анализ ещё не завершён. По этим данным нельзя судить о количестве твоих откликов; повтори команду после ожидания, чтобы продолжить.")
        else:
            lines.append("Запусти /responses для анализа Telegram. Для сопоставления нужны собранные объявления: /scan.")
    lines.append("Список и основания: /responses list" + (" 1 all" if days is None else f" 1 {days}") + " · Справка: /responses help")
    return "\n".join(lines)


def response_list(rows, request: ResponseRequest) -> str:
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    if request.page > pages:
        raise ValueError(f"Страницы {request.page} нет. Всего страниц: {pages}.")
    period = "all" if request.days is None else str(request.days)
    lines = [f"Найденные отклики · Страница {request.page}/{pages} · Всего: {len(rows)}"]
    for row in rows[(request.page - 1) * PAGE_SIZE:request.page * PAGE_SIZE]:
        contact = "@" + row["username"] if row["username"] else f"Telegram ID {row['peer_id']}"
        date = utc_date(row["sent_at"]).astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК")
        label = {"linked": "Связано с вакансией", "inferred": "Предположение", "ambiguous": "Неоднозначно", "unmatched": "Без вакансии"}[row["status"]]
        lines.append(f"\n{date} · {contact}\n{shorten(row['title'], 100)} · {row['sphere']}\n{label}: {row['reason']}"
                     + (f"\nВакансия №{row['job_id']}" if row["job_id"] else ""))
        if row.get("job_analysis") and not (row.get("job_protected") or row.get("source_protected")):
            title = (json.loads(row["job_analysis"]).get("description") or {}).get("title")
            if title:
                lines.append("Объявление: " + shorten(title, 120))
        if row.get("job_link"):
            lines.append("Оригинал: " + row["job_link"])
    if request.page < pages:
        lines.append(f"\nЕщё: /responses list {request.page + 1} {period}")
    if request.page > 1:
        lines.append(f"Назад: /responses list {request.page - 1} {period}")
    if not rows:
        lines.append("Обращений пока нет. Запусти /responses.")
    return "\n".join(lines)
