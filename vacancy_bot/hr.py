"""Contact queries and compact vacancy entries for the existing bot chat."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .dedup import EMAIL, normalize, phone_number
from .description import shorten
from .summary import summarize


PAGE_SIZE = 10
HR_HELP = """Поиск вакансий по контакту HR

Отправь одним сообщением @никнейм, t.me/никнейм или телефон HR. Можно отправить карточку контакта с номером.

Примеры:
@recruiter_name
+7 999 123-45-67
/hr @recruiter_name

Покажу все найденные вакансии из активных источников, включая не прошедшие твои критерии, без повторов. В ТГ — только последние 72 часа.
Сохранённая история: /hr @recruiter_name all
Следующая страница: /hr @recruiter_name 2

Поиск работает по уже собранным объявлениям. Обновить базу: /scan."""


@dataclass(frozen=True)
class ContactQuery:
    key: str

    @property
    def display(self) -> str:
        kind, value = self.key.split(":", 1)
        return {"tg": "@", "phone": "+", "email": ""}[kind] + value


def parse_contact(text: str) -> ContactQuery | None:
    text = normalize(text).strip()
    if not text or len(text) > 256:
        return None
    if re.fullmatch(r"\+?[\d ()\t.\-–—]+", text):
        number = phone_number(text)
        return ContactQuery("phone:" + number) if number else None
    if EMAIL.fullmatch(text):
        return ContactQuery("email:" + text)
    match = re.fullmatch(
        r"(?:(?:https?://)?(?:t\.me|telegram\.me)/|@)?([a-z][a-z0-9_]{3,31})/?", text,
    )
    return ContactQuery("tg:" + match[1]) if match else None


def parse_request(arguments: str) -> tuple[ContactQuery, bool, int]:
    """Accept spaced phone numbers and optional trailing 'all' / page number."""
    parts = arguments.split()
    archive, page = False, 1
    # Only strip a page after a complete contact or the archive suffix; this
    # keeps the last group of a spaced telephone number inside the contact.
    if parts and re.fullmatch(r"[0-9]{1,9}", parts[-1]):
        prefix = " ".join(parts[:-1])
        if parse_contact(prefix) is not None or prefix.casefold().endswith(" all"):
            page = int(parts.pop())
    if parts and parts[-1].casefold() == "all":
        archive = True
        parts.pop()
    query = parse_contact(" ".join(parts))
    if query is None or page < 1:
        raise ValueError("Не удалось распознать контакт. Пример: /hr @recruiter_name или /hr +79991234567.\n"
                         "История: /hr @recruiter_name all · Страница: /hr @recruiter_name 2")
    return query, archive, page


def result_entry(row, match, *, recent: bool) -> str:
    protected = bool(row["protected"])
    title = f"Вакансия из {row['source_title']}" if protected else match.description.title or "Вакансия"
    status = ("✅ " if match.verdict == "Подходит" else "🟡 ") if match.eligible else "⛔ "
    status += match.verdict
    if not recent:
        status += " · Архив"
    if not row["source_active"]:
        status += " · Источник вне текущей подборки"
    lines = [f"№{row['id']} · {title}", f"{status} · {match.score}/100 · Вакансии ТГ"]
    if not protected:
        description = match.description
        details = [value for value in (description.company, description.salary) if value]
        if details:
            lines.append(" · ".join(shorten(value, 100) for value in details))
        # The entry already starts with the position; omit its repetition.
        brief_lines = match.summary.splitlines()
        if brief_lines and brief_lines[0].startswith("Позиция: "):
            brief_lines = brief_lines[1:]
        brief = "\n".join(brief_lines)
    else:
        brief = summarize("", match.facts)
    if brief:
        lines.append(shorten(brief, 320))
    lines.append(f"{row['published_at'][:16].replace('T', ' ')} UTC · {row['source_title']}")
    if row["link"]:
        lines.append("Оригинал: " + row["link"])
    if protected:
        lines.append("Источник запретил пересылку; описание доступно по ссылке.")
    if recent and match.eligible and row["source_active"]:
        lines.append(f"Карточка: /show {row['id']}")
    return "\n".join(lines)
