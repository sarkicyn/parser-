"""Compose a short description from the post and confirmed extracted facts."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .description import VacancyDescription, clean as _clean, extract_description, shorten as _shorten

if TYPE_CHECKING:
    from .matcher import Facts


MAX_SUMMARY_LENGTH = 600


def _role(description: VacancyDescription) -> str:
    if not description.title:
        return ""
    return "Позиция: " + _shorten(description.title, 90) + "."


def _tasks(description: VacancyDescription) -> str:
    tasks = []
    for item in description.responsibilities[:2]:
        task = _clean(item)
        if not task:
            continue
        tasks.append(_shorten(task, 110))
    return "Задачи: " + "; ".join(tasks) + "." if tasks else ""


def _amount(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def summarize(text: str, facts: Facts, description: VacancyDescription | None = None) -> str:
    """Use only stated duties and facts; missing conditions remain unknown.

    Passing empty text produces a facts-only description for protected sources.
    """
    if not facts.is_vacancy:
        return ""
    description = description or extract_description(text)
    sentences = [part for part in (_role(description), _tasks(description)) if part]
    if description.requirements:
        sentences.append("Требования: " + "; ".join(_shorten(item, 110) for item in description.requirements) + ".")
    conditions = []
    if facts.remote is True:
        conditions.append("удалённо")
    elif facts.remote is False:
        conditions.append(description.work_format or "работа не полностью удалённая")
    if facts.hours_max is not None:
        hours = f"{facts.hours_max:g}"
        if facts.hours_min != facts.hours_max and facts.hours_min is not None:
            hours = f"{facts.hours_min:g}–{hours}"
        conditions.append(hours + " ч/день")
    if facts.salary_monthly and facts.salary_max is not None and facts.currency:
        currency = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(facts.currency, facts.currency)
        if facts.salary_min is None:
            salary = "до " + _amount(facts.salary_max)
        elif facts.salary_min == facts.salary_max:
            salary = _amount(facts.salary_min)
        else:
            salary = _amount(facts.salary_min) + "–" + _amount(facts.salary_max)
        conditions.append(("оклад " if facts.fixed is True else "оплата ") + salary + " " + currency + "/мес.")
    if facts.experience_max == 0:
        conditions.append("без опыта")
    elif facts.experience_max is not None:
        # Preserve "от", "до" and other qualifiers rather than inventing a range.
        conditions.append(_clean(facts.evidence.get("experience", "")))
    conditions.extend(_shorten(item, 110) for item in description.conditions
                      if item.casefold() not in {value.casefold() for value in conditions})
    if conditions:
        sentences.append("Условия: " + "; ".join(conditions).rstrip(".") + ".")
    return _shorten("\n".join(sentences), MAX_SUMMARY_LENGTH)
