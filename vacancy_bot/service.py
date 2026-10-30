from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from dataclasses import asdict

from telethon.errors import FloodWaitError, RPCError

from .bot_api import BotAPI, BotError
from .config import Config, ROOT
from .categories import CATEGORY_NAMES, categorize, category_label
from .description import extract_description
from .folders import (folder_title, get_folders, message_link, resolve_folder,
                      response_folder_dialogs, source_input)
from .hr import HR_HELP, PAGE_SIZE, parse_contact, parse_request, result_entry
from .matcher import Facts, classify, split_post
from .profile import Profile, update_profile
from .professionals import (PRO_GROUPS, PRO_PAGE_SIZE, classify_professional,
                            parse_pro_request, pro_group_label)
from .responses import (RESPONSE_HELP, parse_response_request, response_list,
                        response_report, scan_responses)
from .storage import Storage
from .summary import summarize


LOG = logging.getLogger("vacancy_bot")
HELP = """Вакансии из Telegram

/tg — вакансии Telegram; /tg 1 (2, 3, 4) — категории
/pro — удалённые вакансии специалистов с опытом или профильными навыками
/proscan — обновить подборку специалистов из той же Telegram-папки
/pro 1 (2, 3, 4, 5) · /pro page 2 — группы и страницы специалистов
/prohelp — условия и группы новой подборки

/folders — список папок аккаунта
/folder Вакансии — выбрать папку по названию или ID
/sources — каналы и группы выбранной папки
/scan — проверить сообщения за последние 72 часа, включая ранее отклонённые
/jobs — лучшие 20 вакансий Telegram, сгруппированные по категориям
/jobs 1 (2, 3, 4) — лучшие 20 вакансий выбранной категории
/categories — список четырёх категорий
/show 12 — карточка и оригинал вакансии №12
/hr @никнейм — все вакансии по контакту HR; можно просто отправить @никнейм или телефон
/hr @никнейм all — поиск по сохранённой истории
/responses — отклики из личных чатов HR папки «Удаленки» за 30 дней
/responses folder название — выбрать папку HR (отдельно от каналов вакансий)
/responses 90 (all) — другой период · /responses help — справка
/responses list — найденные обращения · /responses stats — сохранённый отчёт
/profile — критерии поиска
/pause и /resume — остановить/возобновить сбор и уведомления
/stats — статистика и состояние

Изменение критериев:
/set hours 2 6
/set salary 40000 (или 40; верхней границы нет)
/set experience 3
/set score 60 (минимум 60, можно повысить)
/set remote on
/set fixed on
/set unknown on (off — только полностью заполненные объявления)
/set keywords Excel, поддержка
/set exclude холодные звонки, продажи
/set keywords clear и /set exclude clear — очистить

Оценка — баллы совпадения по указанным данным, а не вероятность трудоустройства. Неуказанные условия помечаются «нужно уточнить». После изменения критериев сохранённые объявления пересчитываются автоматически; /scan дополнительно проверит историю каналов.
"""

PRO_HELP = """Вакансии специалистов · /pro

Только явно указанная удалённая работа. Нужны требования к опыту или конкретным профильным навыкам. Без ограничений по часам, зарплате, фиксированному окладу и стажу. Объявления с обучением с нуля исключаются.

Источники — та же выбранная Telegram-папка, последние 72 часа. /proscan заново читает историю каналов; фоновый сбор тоже обновляет подборку. /pro показывает уже собранные вакансии. Поиск учитывает объявления, отклонённые личными фильтрами.

""" + "\n".join(f"{pro_group_label(group)} · /pro {group}" for group in PRO_GROUPS) + """

Все группы: /pro · Ещё: /pro page 2
Страницы группы: /pro 3 page 2
Оригинал вакансии — по ссылке в подборке.
Условия этой подборки независимы от /profile и /set. Вакансии специалистов показываются по запросу.
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def publication_date(message) -> datetime | None:
    date = getattr(message, "date", None)
    if not isinstance(date, datetime):
        return None
    date = date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc)
    original = getattr(getattr(message, "fwd_from", None), "date", None)
    if isinstance(original, datetime):
        original = original.replace(tzinfo=original.tzinfo or timezone.utc).astimezone(timezone.utc)
        date = min(date, original)
    return date


def is_recent(message, days: int) -> bool:
    return is_recent_date(publication_date(message), days)


def is_recent_date(date: datetime | None, days: int) -> bool:
    if date is None:
        return False
    date = date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc)
    now = utc_now()
    return now - timedelta(days=days) <= date <= now + timedelta(minutes=5)


def job_title(row) -> str:
    if row["protected"]:
        return f"Вакансия из {row['source_title']}"
    analysis = json.loads(row["analysis"])
    return (analysis.get("description") or {}).get("title") or extract_description(row["text"]).title or "Вакансия"


def job_category(row) -> int | None:
    analysis = json.loads(row["analysis"])
    if not analysis["eligible"]:
        return None
    category = analysis.get("category")
    return category if category in CATEGORY_NAMES else categorize(row["text"])


def card(row) -> str:
    analysis = json.loads(row["analysis"])
    label = "✅" if analysis["verdict"] == "Подходит" else "🟡"
    lines = [f"{label} {analysis['verdict']} · {analysis['score']}/100 · №{row['id']}",
             job_title(row), "Раздел: Вакансии ТГ"]
    category = job_category(row)
    if category is not None:
        lines.append("Категория: " + category_label(category))
    if not row["protected"]:
        description = analysis.get("description") or asdict(extract_description(row["text"]))
        fields = (("Компания", "company"), ("Оплата", "salary"), ("Локация", "location"),
                  ("Формат", "work_format"), ("Занятость", "employment_type"))
        for label, key in fields:
            value = description.get(key)
            if key == "salary" and not value:
                value = analysis["facts"].get("evidence", {}).get("salary")
            if value:
                lines.append(f"{label}: {value}")
    lines.extend([f"Источник: {row['source_title']}",
                  f"Опубликовано: {row['published_at'][:16].replace('T', ' ')} UTC", ""])
    summary = (analysis.get("summary") if analysis.get("description") and not row["protected"] else "") or summarize(
        "" if row["protected"] else row["text"], Facts(**analysis["facts"])
    )
    if summary:
        lines.extend(["Кратко: " + summary, ""])
    lines.extend("✓ " + reason for reason in analysis["reasons"])
    if analysis["unknowns"]:
        lines.append("\nНужно уточнить:")
        lines.extend("? " + unknown for unknown in analysis["unknowns"])
    if row["part_count"] > 1:
        lines.append(f"\nПункт {row['part_index'] + 1} из {row['part_count']} в общей публикации")
    if row["link"]:
        lines.append("\nОригинал: " + row["link"])
    if row["protected"]:
        lines.append("Источник запретил пересылку. Текст доступен по ссылке в Telegram.")
    return "\n".join(lines)


class Service:
    def __init__(self, client, bot: BotAPI, db: Storage, config: Config):
        self.client, self.bot, self.db, self.config = client, bot, db, config
        self.profile = Profile.load(ROOT / "profile.json")
        self.lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.responses_lock = asyncio.Lock()
        self.responses_requested = False
        self.last_sync = 0.0
        self.bot_entity = None
        self.last_error = ""
        self.last_checked = "ещё не проверяли"
        self.blocked_until = 0.0
        self.commands: set[asyncio.Task] = set()
        self.reclassify()

    def cutoff(self) -> str:
        return (utc_now() - timedelta(days=self.config.tg_max_age_days)).isoformat()

    @property
    def paused(self) -> bool:
        return self.db.setting("paused") == "1"

    async def sync_folder(self, selection: str | None = None):
        selection = selection or self.db.setting("folder", self.config.folder)
        if not selection:
            self.db.sync_sources([])
            self.last_sync = time.monotonic()
            return
        folder, records = await resolve_folder(self.client, selection)
        self.db.sync_sources(records)
        self.db.set_setting("folder", str(folder.id))
        self.db.set_setting("folder_title", folder_title(folder))
        self.last_sync = time.monotonic()

    def reload_profile(self):
        updated = Profile.load(ROOT / "profile.json")
        if updated != self.profile:
            self.profile = updated
            self.reclassify()

    def reclassify(self):
        for row in self.db.recent(self.cutoff()):
            self.db.reclassify(row["id"], self.analyze(row))

    def analyze(self, row):
        return classify(row["text"], self.profile)

    async def read_source(self, source, history: bool):
        peer = source_input(source)
        first_run = source["last_id"] == 0
        if history or first_run:
            messages = [m async for m in self.client.iter_messages(peer, limit=self.config.history_limit)]
            messages.reverse()
        else:
            messages = [m async for m in self.client.iter_messages(peer, min_id=source["last_id"], reverse=True, limit=200)]
        for message in messages:
            if self.paused:
                break
            text = getattr(message, "message", None)
            if text and is_recent(message, self.config.tg_max_age_days):
                parts = split_post(text)
                self.db.trim_message_parts(source["peer_id"], message.id, len(parts))
                for index, part in enumerate(parts):
                    self.db.save_job(
                        peer_id=source["peer_id"], message_id=message.id, part_index=index,
                        part_count=len(parts), text=part, source_title=source["title"],
                        link=message_link(source, message.id),
                        published_at=publication_date(message).isoformat(),
                        protected=bool(source["protected"] or getattr(message, "noforwards", False)),
                        match=classify(part, self.profile),
                    )
            # A manual lookback must not jump over an existing catch-up cursor.
            if first_run or not history:
                self.db.advance(source["peer_id"], message.id)
        self.db.source_error(source["peer_id"], "")

    async def deliver(self, row, repeat: bool = False):
        # Serialize deliveries so concurrent scans and /show share fresh markers.
        async with self.delivery_lock:
            current = self.db.job(row["id"])
            if current is None:
                return False
            state = self.db.delivery_state(current["id"])
            if not repeat and state["notified_at"]:
                return False
            return await self._deliver(current, repeat)

    async def _deliver(self, row, repeat: bool = False):
        # Recheck queued rows and explicit /show requests against current criteria.
        try:
            published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        days = self.config.tg_max_age_days
        if not is_recent_date(published, days):
            return False
        match = self.analyze(row)
        self.db.reclassify(row["id"], match)
        if not match.eligible:
            return False
        state = self.db.delivery_state(row["id"])
        source = self.db.source(row["peer_id"])
        # Verify current protection as folder metadata can be a few minutes old.
        entity = await self.client.get_entity(source_input(source))
        current = dict(row)
        current["analysis"] = json.dumps(match.to_dict(), ensure_ascii=False)
        current["protected"] = bool(row["protected"] or getattr(entity, "noforwards", False))
        if repeat or not state["card_sent"]:
            await self.bot.send(self.config.owner, card(current))
            self.db.card_sent(row["id"])
        if not current["protected"]:
            if row["part_count"] > 1:
                # A whole roundup may contain rejected jobs. Send only its matching item.
                await self.bot.send(self.config.owner, row["text"])
            else:
                try:
                    # The account is the owner. The original appears in their chat with the bot.
                    await self.client.forward_messages(self.bot_entity, [row["message_id"]], from_peer=source_input(source))
                except FloodWaitError:
                    raise
                except RPCError as exc:
                    await self.bot.send(self.config.owner, "Telegram не разрешил переслать оригинал. "
                                        "Открой ссылку в карточке. Причина: " + type(exc).__name__)
        self.db.delivered(row["id"])
        return True

    async def drain(self):
        self.reload_profile()
        if time.monotonic() < self.blocked_until:
            return
        for row in self.db.ranked(self.cutoff(), self.config.notifications_limit, pending=True):
            if self.paused:
                break
            try:
                await self.deliver(row)
            except FloodWaitError:
                raise
            except RPCError as exc:
                self.db.source_error(row["peer_id"], type(exc).__name__)
                LOG.warning("Источник вакансии %s: %s", row["id"], type(exc).__name__)

    async def scan(self, history: bool = False, *, notify: bool = True):
        async with self.lock:
            if self.paused or time.monotonic() < self.blocked_until or (self.responses_requested and not history):
                return
            self.reload_profile()
            if history or time.monotonic() - self.last_sync >= 300 or self.last_sync == 0:
                try:
                    await self.sync_folder()
                except ValueError:
                    # A removed folder must never leave its old sources monitored.
                    self.db.sync_sources([])
                    raise
            for source in self.db.sources():
                if self.paused or (self.responses_requested and not history):
                    break
                try:
                    await self.read_source(source, history)
                except FloodWaitError:
                    raise
                except (RPCError, ValueError) as exc:
                    self.db.source_error(source["peer_id"], type(exc).__name__)
                    LOG.warning("Источник %s: %s", source["peer_id"], type(exc).__name__)
            self.last_checked = utc_now().strftime("%Y-%m-%d %H:%M UTC")
            if notify and not self.responses_requested:
                await self.drain()

    async def background(self):
        while True:
            try:
                if not self.paused and time.monotonic() >= self.blocked_until:
                    await self.scan()
                    self.last_error = ""
            except FloodWaitError as exc:
                self.blocked_until = time.monotonic() + exc.seconds
                self.last_error = f"Telegram просит подождать {exc.seconds} секунд"
                LOG.warning(self.last_error)
            except (BotError, RPCError, OSError, ValueError) as exc:
                self.last_error = str(exc) if isinstance(exc, (BotError, ValueError)) else type(exc).__name__
                LOG.warning("Проверка: %s", self.last_error)
            await asyncio.sleep(self.config.poll_seconds)

    def professional_matches(self, group: int | None = None):
        matches, seen = [], set()
        for row in self.db.tg_candidates(self.cutoff()):
            try:
                published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if not is_recent_date(published, self.config.tg_max_age_days):
                continue
            match = classify_professional(row["text"])
            if not match.eligible or group is not None and match.group != group:
                continue
            if row["dedup_key"] in seen:
                continue
            seen.add(row["dedup_key"])
            matches.append((row, match))
        # Within each group keep the newest vacancies first.
        return sorted(matches, key=lambda item: item[1].group)

    async def list_professionals(self, arguments: str = ""):
        group, page = parse_pro_request(arguments)
        matches = self.professional_matches(group)
        pages = max(1, (len(matches) + PRO_PAGE_SIZE - 1) // PRO_PAGE_SIZE)
        if page > pages:
            raise ValueError(f"Страницы {page} нет. Всего страниц: {pages}. Начало: /pro")
        lines = ["Вакансии специалистов · только удалённо",
                 f"Папка: {self.db.setting('folder_title', 'не выбрана')} · последние {self.config.tg_max_age_days * 24} часа",
                 f"Найдено: {len(matches)} · Страница {page}/{pages}",
                 "Требуется опыт или профильные навыки; без ограничений по часам, зарплате и стажу."]
        visible = matches[(page - 1) * PRO_PAGE_SIZE:page * PRO_PAGE_SIZE]
        for current_group in ([group] if group is not None else PRO_GROUPS):
            entries = []
            for row, match in visible:
                if match.group != current_group:
                    continue
                protected = bool(row["protected"] or row["source_protected"])
                title = f"Вакансия из {row['source_title']}" if protected else match.description.title or "Вакансия"
                entry = [f"№{row['id']} · {title}"]
                if not protected:
                    if match.description.salary:
                        entry.append("Оплата: " + match.description.salary)
                    entry.append("Требования: " + "; ".join(match.evidence[:2]))
                else:
                    entry.append("Источник запретил пересылку; описание доступно по ссылке.")
                entry.append(f"{row['published_at'][:16].replace('T', ' ')} UTC · {row['source_title']}")
                if row["link"]:
                    entry.append("Оригинал: " + row["link"])
                entries.append("\n".join(entry))
            if entries:
                lines.append(pro_group_label(current_group) + "\n\n" + "\n\n".join(entries))
        if not matches:
            lines.append("Подходящих вакансий специалистов пока нет. Выбери папку: /folders → /folder название. Обновить: /proscan")
        base = "/pro" + (f" {group}" if group is not None else "")
        if page < pages:
            lines.append(f"Ещё: {base} page {page + 1}")
        if page > 1:
            lines.append(f"Назад: {base} page {page - 1}")
        lines.append("Группы: " + " · ".join(f"/pro {group}" for group in PRO_GROUPS)
                     + "\nОбновить: /proscan · Условия: /prohelp")
        await self.bot.send(self.config.owner, "\n\n".join(lines), keyboard=True)

    async def search_hr(self, arguments: str):
        if not arguments:
            await self.bot.send(self.config.owner, HR_HELP, keyboard=True)
            return
        query, archive, page = parse_request(arguments)
        self.reload_profile()
        rows = self.db.jobs_by_contact(query.key, None if archive else self.cutoff())
        matches = []
        for row in rows:
            match = self.analyze(row)
            if not match.facts.is_vacancy:
                continue
            try:
                published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                published = None
            days = self.config.tg_max_age_days
            recent = is_recent_date(published, days)
            if not archive and not recent:
                continue
            matches.append((row, match, recent))
        header = "Вакансии по контакту HR: " + query.display
        if not matches:
            scope = "в сохранённой истории" if archive else "среди свежих объявлений активных источников"
            hint = ("Обновить объявления: /scan" if archive
                    else f"Сохранённая история: /hr {query.display} all\nОбновить объявления: /scan")
            await self.bot.send(self.config.owner, header + f"\nСовпадений {scope} пока нет.\n\n" + hint)
            return
        pages = (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE
        if page > pages:
            raise ValueError(f"Страницы {page} нет. Всего страниц: {pages}.\n"
                             f"Начало: /hr {query.display}" + (" all" if archive else ""))
        scope = ("Сохранённая история; старые вакансии отмечены «Архив»." if archive else
                 f"ТГ: последние {self.config.tg_max_age_days * 24} часа.")
        entries = [result_entry(row, match, recent=recent)
                   for row, match, recent in matches[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
        lines = [header, f"Найдено: {len(matches)} · Страница {page}/{pages}", scope,
                 "Показаны все совпадения; соответствие твоим критериям отмечено у каждой вакансии.",
                 "\n\n".join(entries)]
        base = f"/hr {query.display}" + (" all" if archive else "")
        if page < pages:
            lines.append(f"Ещё: {base} {page + 1}")
        if page > 1:
            lines.append(f"Назад: {base} {page - 1}")
        if not archive:
            lines.append(f"Сохранённая история: /hr {query.display} all")
        await self.bot.send(self.config.owner, "\n\n".join(lines))

    async def prepare_response_scope(self, selection: str):
        try:
            folder, dialogs = await asyncio.wait_for(response_folder_dialogs(self.client, selection), timeout=30)
        except asyncio.TimeoutError:
            raise ValueError("Telegram не ответил при чтении папки за 30 сек. Повтори /responses позже.") from None
        dialogs = [item for item in dialogs if item.entity.id != self.config.owner
                   and not getattr(item.entity, "bot", False) and not getattr(item.entity, "deleted", False)]
        self.db.set_setting("responses_scope", json.dumps({"owner":self.config.owner, "folder_id":folder.id,
            "folder_title":folder_title(folder), "peer_ids":[item.entity.id for item in dialogs]}))
        return folder, dialogs

    async def cached_responses_while_waiting(self, days: int | None, reason: str) -> bool:
        scope = json.loads(self.db.setting("responses_scope", "{}"))
        saved = json.loads(self.db.setting("responses_scan", "{}"))
        if (scope.get("owner") != self.config.owner or not saved
                or saved.get("scope_key") != f"folder:{scope.get('folder_id')}"):
            return False
        await self.bot.send(self.config.owner, reason + ". Показываю сохранённую статистику без ожидания Telegram.")
        await self.application_statistics("stats " + ("all" if days is None else str(days)))
        return True

    async def application_statistics(self, arguments: str):
        request = parse_response_request(arguments)
        if request.action == "help":
            await self.bot.send(self.config.owner, RESPONSE_HELP, keyboard=True)
            return
        selection = self.db.setting("responses_folder", self.config.responses_folder)
        if request.action == "folder":
            if not request.selection:
                await self.bot.send(self.config.owner, f"Папка личных чатов HR: {selection}.\nИзменить: /responses folder название или ID. Список: /folders")
                return
            if self.responses_lock.locked():
                raise ValueError("Дождись завершения анализа, затем измени папку откликов.")
            async with self.responses_lock, self.lock:
                folder, dialogs = await self.prepare_response_scope(request.selection)
                self.db.set_setting("responses_folder", str(folder.id))
                self.db.set_setting("responses_resume", "{}")
                self.db.set_setting("responses_scan", "{}")
            await self.bot.send(self.config.owner, f"Папка откликов: «{folder_title(folder)}». Личных чатов HR: {len(dialogs)}.\nЗапустить анализ: /responses")
            return
        if request.action == "scan":
            if self.responses_lock.locked():
                if await self.cached_responses_while_waiting(request.days, "Анализ уже идёт"):
                    return
                raise ValueError("Анализ откликов уже идёт. Сохранённый отчёт: /responses stats")
            if time.monotonic() < self.blocked_until:
                if await self.cached_responses_while_waiting(request.days, self.last_error or "Telegram временно ограничил обновление"):
                    return
                raise ValueError(self.last_error)
            checkpoint = json.loads(self.db.setting("responses_resume", "{}"))
            retry_at = checkpoint.get("retry_at")
            if retry_at:
                remaining = (datetime.fromisoformat(retry_at) - utc_now()).total_seconds()
                if remaining > 0:
                    if await self.cached_responses_while_waiting(request.days, f"Telegram просит подождать ещё {int(remaining) + 1} сек. перед обновлением"):
                        return
                    raise ValueError(f"Telegram просит подождать ещё {int(remaining) + 1} сек. Прогресс сохранён; затем повтори /responses" +
                                     (" all" if request.days is None else f" {request.days}"))
            async with self.responses_lock:
                self.responses_requested = True
                try:
                    await self.bot.send(self.config.owner, f"Проверяю личные чаты HR только в папке «{selection}»…")
                    async with self.lock:
                        # The background collector may have encountered a limit
                        # while this command was waiting for the account lock.
                        if time.monotonic() < self.blocked_until:
                            if await self.cached_responses_while_waiting(request.days, self.last_error or "Telegram временно ограничил обновление"):
                                return
                            raise ValueError(self.last_error + ". Сохранённый отчёт: /responses stats")
                        folder, dialogs = await self.prepare_response_scope(selection)
                        scope_key = f"folder:{folder.id}"
                        continuing = (checkpoint.get("owner") == self.config.owner and checkpoint.get("days") == request.days
                                      and checkpoint.get("scope_key") == scope_key)
                        period = f"за последние {request.days} дней" if request.days else "за всю доступную историю"
                        await self.bot.send(self.config.owner, ("Продолжаю анализ" if continuing else "Анализирую") +
                            f" {period}: папка «{folder_title(folder)}», личных чатов HR: {len(dialogs)}.")
                        result = await scan_responses(self.client, self.db, self.config.owner, request.days,
                            now=utc_now(), progress=lambda text: self.bot.send(self.config.owner, text),
                            dialogs=dialogs, scope_key=scope_key, scope_title=folder_title(folder))
                        if result.flood_seconds:
                            self.blocked_until = max(self.blocked_until, time.monotonic() + result.flood_seconds)
                            self.last_error = f"Telegram просит подождать {result.flood_seconds} секунд"
                finally:
                    self.responses_requested = False
        cutoff = (utc_now() - timedelta(days=request.days)).isoformat() if request.days else None
        scope = json.loads(self.db.setting("responses_scope", "{}"))
        rows = self.db.responses(cutoff, peer_ids=scope.get("peer_ids", []))
        saved = json.loads(self.db.setting("responses_scan", "{}"))
        if saved.get("scope_key") != f"folder:{scope.get('folder_id')}":
            saved = {}
        text = response_list(rows, request) if request.action == "list" else response_report(
            rows, request.days, incomplete=bool(saved and not saved["complete"]))
        text = f"Папка HR: «{scope.get('folder_title', selection)}» · личных чатов: {len(scope.get('peer_ids', []))}\n\n" + text
        if saved:
            state = "завершён" if saved["complete"] else "НЕ ЗАВЕРШЁН, статистика неполная"
            scope = f"{saved['days']} дней" if saved.get("days") else "вся доступная история"
            text += (f"\n\nПоследний анализ: {saved['at'][:16].replace('T', ' ')} UTC · {scope} · {state}."
                     f"\nПроверено чатов: {saved['chats']} · недочитано: {saved.get('partial_chats', 0)}"
                     f"\nСообщений: {saved['messages']} · исходящих: {saved.get('outgoing', 0)} · ошибок: {saved['errors']}")
            if saved.get("flood_seconds"):
                resume_command = "/responses" + (" all" if saved.get("days") is None else f" {saved['days']}")
                text += f"\nTelegram ограничил запросы; после ожидания {saved['flood_seconds']} сек. отправь {resume_command}. Анализ продолжится с сохранённого места."
        else:
            text += "\nАнализ Telegram ещё не запускался: /responses"
        await self.bot.send(self.config.owner, text, keyboard=True)

    async def handle_command(self, message: dict):
        if (message.get("from", {}).get("id") != self.config.owner
                or message.get("chat", {}).get("id") != self.config.owner
                or message.get("chat", {}).get("type") != "private"
                or message.get("forward_origin") or message.get("forward_from")
                or message.get("forward_from_chat") or message.get("sender_chat") or message.get("via_bot")):
            return
        text = message.get("text", "").strip()
        text = {"Вакансии ТГ": "/tg", "Поиск по HR": "/hr",
                "Вакансии специалистов": "/pro", "Статистика откликов": "/responses"}.get(text, text)
        if not text.startswith("/"):
            shared = message.get("contact")
            value = shared.get("phone_number", "") if shared else text
            if shared and value and not value.startswith("+"):
                value = "+" + value
            query = parse_contact(value)
            if query is None:
                if shared:
                    await self.bot.send(self.config.owner, "В контакте нет распознаваемого номера. Пришли @никнейм HR или телефон с кодом страны.")
                return
            text = "/hr " + query.display
        pieces = text.split(maxsplit=1)
        command, arguments = pieces[0].split("@")[0].lower(), pieces[1] if len(pieces) > 1 else ""
        owner = self.config.owner
        try:
            if command in ("/start", "/help"):
                await self.bot.send(owner, HELP, keyboard=True)
            elif command == "/prohelp":
                await self.bot.send(owner, PRO_HELP, keyboard=True)
            elif command == "/pro":
                await self.list_professionals(arguments)
            elif command == "/proscan":
                parse_pro_request(arguments)
                if self.paused:
                    raise ValueError("Мониторинг на паузе. Сначала /resume")
                if time.monotonic() < self.blocked_until:
                    raise ValueError(self.last_error)
                if self.lock.locked():
                    raise ValueError("Проверка Telegram уже идёт. Подборка: /pro · Состояние: /stats")
                if not self.db.setting("folder", self.config.folder):
                    raise ValueError("Сначала выбери Telegram-папку: /folders → /folder название")
                await self.bot.send(owner, f"Ищу удалённые вакансии специалистов: до {self.config.history_limit} последних сообщений каждого источника, только последние 72 часа…")
                await self.scan(history=True, notify=False)
                await self.list_professionals(arguments)
            elif command == "/hr":
                await self.search_hr(arguments)
            elif command == "/responses":
                await self.application_statistics(arguments)
            elif command == "/profile":
                self.reload_profile()
                await self.bot.send(owner, "Фильтры вакансий Telegram:\n\n" + self.profile.describe())
            elif command == "/set":
                async with self.lock:
                    self.reload_profile()
                    profile = update_profile(self.profile, arguments)
                    profile.save(ROOT / "profile.json")
                    self.profile = profile
                    self.reclassify()
                await self.bot.send(owner, "Критерии сохранены, объявления пересчитаны.\n\n" + self.profile.describe())
            elif command == "/folders":
                if time.monotonic() < self.blocked_until:
                    raise ValueError(self.last_error)
                folders = await get_folders(self.client)
                text = "\n".join(f"{f.id} — {folder_title(f)}" for f in folders)
                await self.bot.send(owner, "Папки:\n" + (text or "Создай папку с каналами в Telegram") + "\n\nВыбрать: /folder название или ID")
            elif command == "/folder":
                if not arguments:
                    raise ValueError("Пример: /folder Вакансии или /folder 2")
                if time.monotonic() < self.blocked_until:
                    raise ValueError(self.last_error)
                async with self.lock:
                    await self.sync_folder(arguments)
                await self.bot.send(owner, f"Папка «{self.db.setting('folder_title')}» выбрана. Источников: {len(self.db.sources())}.\nПроверить историю: /scan")
            elif command == "/sources":
                rows = self.db.sources()
                lines = [f"{row['title']} ({row['peer_id']})" + (f" · ошибка: {row['error']}" if row["error"] else "") for row in rows]
                await self.bot.send(owner, "Источники выбранной папки:\n" + ("\n".join(lines) or "Папка не выбрана или пуста. /folders → /folder название"))
            elif command == "/scan":
                if self.paused:
                    raise ValueError("Мониторинг на паузе. Сначала /resume")
                if time.monotonic() < self.blocked_until:
                    raise ValueError(self.last_error)
                if self.lock.locked():
                    raise ValueError("Проверка уже идёт. Результаты появятся в чате; состояние — /stats")
                await self.bot.send(owner, f"Проверяю до {self.config.history_limit} последних сообщений в каждом источнике; вакансии только за последние 72 часа…")
                await self.scan(history=True)
                await self.bot.send(owner, "Проверка завершена. Подборка по оценке: /jobs")
            elif command == "/categories":
                await self.bot.send(owner, "Категории вакансий:\n\n" + "\n".join(
                    f"{category_label(group)} · /jobs {group} · /tg {group}" for group in CATEGORY_NAMES
                ) + "\n\nВсе категории: /jobs. Категория не меняет критерии и уведомления.")
            elif command in ("/jobs", "/tg"):
                if arguments and arguments not in {str(group) for group in CATEGORY_NAMES}:
                    raise ValueError("Пример: /tg, /jobs, /tg 1 или /jobs 4. Категории: /categories")
                self.reload_profile()
                category = int(arguments) if arguments else None
                rows = self.db.ranked(self.cutoff(), category=category)
                groups = [category] if category is not None else CATEGORY_NAMES
                lines = []
                for group in groups:
                    grouped = [row for row in rows if job_category(row) == group]
                    if grouped:
                        lines.append(category_label(group) + "\n" + "\n\n".join(
                            f"{row['score']}/100 · №{row['id']} · {job_title(row)}\n"
                            + (row["link"] or f"/show {row['id']}") for row in grouped
                        ))
                await self.bot.send(owner, "Вакансии ТГ · лучшие вакансии по текущим критериям:\n\n" +
                    ("\n\n".join(lines) or "Пока нет подходящих вакансий. Выбери папку и выполни /scan.") +
                    "\n\nОткрыть карточку: /show номер. Категории: /categories · /tg 1 · /tg 2 · /tg 3 · /tg 4", keyboard=True)
            elif command == "/show":
                if not arguments.isdigit():
                    raise ValueError("Пример: /show 12")
                async with self.lock:
                    self.reload_profile()
                    row = self.db.job(int(arguments))
                    if row is None:
                        raise ValueError("Вакансия не найдена. Список: /jobs")
                    if time.monotonic() < self.blocked_until:
                        raise ValueError(self.last_error)
                    if not await self.deliver(row, repeat=True):
                        raise ValueError("Вакансия устарела или не соответствует текущим критериям. "
                                         f"Минимальная оценка: {self.profile.min_score:g}/100. Список: /jobs")
            elif command in ("/pause", "/resume"):
                self.db.set_setting("paused", "1" if command == "/pause" else "0")
                await self.bot.send(owner, "Мониторинг приостановлен." if self.paused else "Мониторинг возобновлён.")
            elif command == "/stats":
                tg_stats = self.db.stats(self.cutoff())
                await self.bot.send(owner,
                    f"Папка: {self.db.setting('folder_title', 'не выбрана')}\n"
                    f"Состояние: {'пауза' if self.paused else 'работает'}\n"
                    f"Источников: {len(self.db.sources())}\n"
                    f"ТГ за {self.config.tg_max_age_days * 24} часа: {tg_stats['total']} уникальных объявлений, "
                    f"подходящих {tg_stats['matched']}, отправлено {tg_stats['sent']}\n"
                    f"Последняя проверка: {self.last_checked}\n"
                    f"Последняя ошибка ТГ: {self.last_error or 'нет'}\n"
                    "Статистика твоих откликов: /responses · Сохранённый отчёт: /responses stats")
            else:
                await self.bot.send(owner, "Неизвестная команда. /help")
        except FloodWaitError as exc:
            self.blocked_until = time.monotonic() + exc.seconds
            self.last_error = f"Telegram просит подождать {exc.seconds} секунд"
            await self.bot.send(owner, self.last_error)
        except (ValueError, RPCError) as exc:
            await self.bot.send(owner, str(exc) if isinstance(exc, ValueError) else "Ошибка Telegram: " + type(exc).__name__)
        except BotError as exc:
            self.last_error = str(exc)
            LOG.warning("Команда: %s", self.last_error)

    async def poll_commands(self):
        offset = int(self.db.setting("update_offset", "0"))
        while True:
            try:
                updates = await self.bot.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
                for update in updates:
                    # Separate tasks let /pause and /stats work during scans.
                    task = asyncio.create_task(self.handle_command(update.get("message", {})))
                    self.commands.add(task)
                    task.add_done_callback(self.command_finished)
                    offset = update["update_id"] + 1
                    self.db.set_setting("update_offset", str(offset))
            except BotError as exc:
                self.last_error = str(exc)
                LOG.warning("Получение команд: %s", self.last_error)
                await asyncio.sleep(5)

    def command_finished(self, task: asyncio.Task):
        self.commands.discard(task)
        if not task.cancelled() and task.exception():
            LOG.error("Команда завершилась с ошибкой: %s", type(task.exception()).__name__)

    async def run(self):
        info = await self.bot.call("getMe")
        await self.bot.call("setMyCommands", commands=[
            {"command":name,"description":description} for name, description in (
                ("start", "Открыть меню"), ("help", "Все команды и справка"),
                ("tg", "Вакансии Telegram"), ("jobs", "Лучшие вакансии"),
                ("pro", "Вакансии специалистов"), ("proscan", "Обновить вакансии специалистов"),
                ("prohelp", "Условия подборки специалистов"), ("scan", "Проверить каналы сейчас"),
                ("folders", "Папки Telegram"), ("folder", "Выбрать папку каналов"),
                ("sources", "Источники вакансий"), ("categories", "Категории вакансий"),
                ("show", "Показать вакансию по номеру"), ("hr", "Найти вакансии по контакту HR"),
                ("responses", "Статистика откликов из папки HR"), ("profile", "Критерии поиска"),
                ("set", "Изменить критерии"), ("pause", "Приостановить сбор"),
                ("resume", "Возобновить сбор"), ("stats", "Статистика и состояние бота"),
            )])
        self.bot_entity = await self.client.get_entity("@" + info["username"])
        await self.bot.send(self.config.owner, "Парсер Telegram запущен. Вакансии: /tg · /jobs.\n"
                            "Вакансии специалистов: /pro · Обновить: /proscan · Условия: /prohelp\n"
                            "Папка каналов: /folders → /folder название.\n"
                            "Поиск HR: отправь @никнейм или телефон.\nСтатистика откликов: /responses\nОбщие фильтры: /profile", keyboard=True)
        background = asyncio.create_task(self.background())
        polling = asyncio.create_task(self.poll_commands())
        try:
            await asyncio.gather(background, polling)
        finally:
            tasks = [background, polling, *self.commands]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
