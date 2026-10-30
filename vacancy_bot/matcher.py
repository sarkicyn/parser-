"""Conservative, local extraction. Missing facts never become confirmed facts."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .profile import MIN_SCORE, Profile
from .categories import categorize
from .description import VacancyDescription, extract_description
from .summary import summarize


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е").replace("\xa0", " ").replace("\u202f", " ")


def contains(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(normalize(phrase)) + r"(?!\w)", text))


def found(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.I))


@dataclass
class Facts:
    is_vacancy: bool = False
    remote: bool | None = None
    hours_min: float | None = None
    hours_max: float | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_monthly: bool = False
    currency: str | None = None
    fixed: bool | None = None
    experience_max: float | None = None
    experience_exclusive: bool = False
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass
class Match:
    score: int
    eligible: bool
    verdict: str
    reasons: list[str]
    unknowns: list[str]
    facts: Facts
    summary: str = ""
    category: int | None = None
    description: VacancyDescription | None = None

    def to_dict(self) -> dict:
        return asdict(self)


NUM = r"\d{1,2}(?:[.,]\d+)?"
RANGE = rf"(?P<a>{NUM})\s*(?:(?:[-–—]|до|to)\s*(?P<b>{NUM}))?"
HOURS = re.compile(
    rf"(?<!\d)(?:от\s+|from\s+)?{RANGE}\s*(?:ч(?:ас(?:а|ов)?)?\.?|hours?|hrs?)"
    r"(?:\s*(?:в|за|per|a|/)\s*(?:день|сутки|day|дн\.?))?", re.I
)
EXPERIENCE = re.compile(
    rf"(?:опыт\w*|experience)\s*[:—-]?\s*(?:работы\s*)?"
    rf"(?:[^\n.;\d]{{0,45}}?)(?P<qual>более|больше|свыше|от|не менее|минимум|до|"
    rf"не более|over|more than|at least|up to)?\s*{RANGE}\s*(?P<plus>\+)?\s*"
    r"(?P<unit>лет|года?|years?|месяц\w*|months?)", re.I
)
MONEY_NUM = r"\d{1,3}(?:[ .]\d{3})+(?:,\d+)?|\d{2,7}(?:[.,]\d+)?"
MONEY = re.compile(
    rf"(?<![\w\d])(?P<a>{MONEY_NUM})\s*(?P<ka>тыс\.?|[kк])?\s*"
    rf"(?:(?:[-–—]|до|to)\s*(?P<b>{MONEY_NUM})\s*(?P<kb>тыс\.?|[kк])?\s*)?"
    r"(?P<currency>₽|руб(?:\.|лей|ля|ль)?|р\.|rub\b|\$|usd\b|€|eur\b|тенге|₸|byn\b)", re.I
)
SALARY_LINE = r"оклад|зарплат\w*|з/?п|оплат\w*|salary|фикс\w*"


def _money_number(value: str, multiplier: bool) -> float:
    value = value.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", value):
        value = value.replace(".", "")
    return float(value.replace(",", ".")) * (1000 if multiplier else 1)


def extract(text: str) -> Facts:
    t = normalize(text)
    numbers = {
        "один": "1", "одного": "1", "два": "2", "двух": "2", "три": "3", "трех": "3",
        "четыре": "4", "четырех": "4", "пять": "5", "пяти": "5", "шесть": "6",
        "шести": "6", "семь": "7", "семи": "7", "восемь": "8", "восьми": "8",
        "девять": "9", "девяти": "9", "десять": "10", "десяти": "10",
    }
    t = re.sub(r"\b(?:" + "|".join(numbers) + r")\b", lambda m: numbers[m[0]], t)
    f = Facts()
    seller = found(r"ищу (?:работу|подработку|вакансию)|мое резюме|ищу заказ\w*|"
                   r"предлагаю (?:свои )?услуги|looking for (?:a job|work)", t)
    advertising = found(r"(?:запишись|записывайтесь|покупайте|купи) на (?:курс|обучение)|"
                        r"курс\w* с (?:гарантией )?трудоустройств", t)
    f.is_vacancy = not (seller or advertising) and found(
        r"ваканси\w*|требуется|требуются|ищем|нанимаем|hiring|we are looking|"
        r"(?:нужен|нужна|нужны)\s+\w+|должность|обязанности|работа\s*[:—-]|"
        r"(?:удален\w*|remote)[^\n]{0,30}(?:работа|ассистент|оператор)", t
    )
    # Remove explicitly negated optional office visits before detecting hybrid work.
    remote_text = re.sub(r"(?:не (?:нужно|нужны|требуется|требуются)|без)\s+"
                         r"(?:посещени\w*|поезд\w*|визит\w*)\s+(?:в\s+)?офис\w*", "", t)
    negative_remote = found(r"не\s+(?:удален\w*|remote)|удален\w*\s+(?:нет|не (?:предусмотрен\w*|рассматривается))|"
                            r"только\s+(?:из\s+|в\s+)?офис\w*|работа\s+в\s+офис\w*|"
                            r"(?:формат|режим)\s*[:—-]?\s*офис\w*|гибрид\w*|hybrid|on[- ]?site|"
                            r"(?:посещени\w*|присутстви\w*|выход\w*)\s+(?:в\s+)?офис\w*", remote_text)
    positive_remote = found(r"удален\w*|дистанцион\w*|remote|из дома|work from home", remote_text)
    if negative_remote:
        f.remote = False
    elif positive_remote:
        f.remote = True

    hours = []
    for match in HOURS.finditer(t):
        before = t[max(0, match.start()-32):match.start()]
        after = t[match.end():match.end()+32]
        if found(r"(?:ответ\w*|обучени\w*|созвон\w*|тест\w*|курс\w*|перерыв\w*)[^\n]{0,20}$", before):
            continue
        low, high = float(match["a"].replace(",", ".")), float((match["b"] or match["a"]).replace(",", "."))
        if high < low or high > 168:
            continue
        if found(r"^\s*(?:в|per|/)\s*(?:недел\w*|week)", after):
            # Workdays are unknown; do not fabricate a daily schedule.
            continue
        if found(r"(?:более|больше|свыше)\s*$", before):
            high += 0.01
        hours.append((low, high, match.group(0)))
    if hours:
        f.hours_min = min(h[0] for h in hours)
        f.hours_max = max(h[1] for h in hours)
        f.evidence["hours"] = "; ".join(h[2] for h in hours)
    else:
        clock = re.search(r"(?:с\s+|график\s*[:—-]?\s*)(\d{1,2})(?::00)?\s*(?:до|[-–—])\s*(\d{1,2})(?::00)?", t)
        if clock and found(r"(?:график|рабоч\w* время|смена)", t):
            duration = (int(clock[2]) - int(clock[1])) % 24
            if 0 < duration < 24:
                f.hours_min = f.hours_max = duration
                f.evidence["hours"] = clock.group(0)
        elif found(r"полный рабочий день|полная занятость|full[- ]time", t):
            f.hours_min = f.hours_max = 8
            f.evidence["hours"] = "полная занятость"

    for match in EXPERIENCE.finditer(t):
        if found(r"компани\w*|на рынке|существу\w*", match.group(0)):
            continue
        low = float(match["a"].replace(",", "."))
        high = float((match["b"] or match["a"]).replace(",", "."))
        if match["unit"].startswith(("месяц", "month")):
            high /= 12
        line_start = t.rfind("\n", 0, match.start()) + 1
        line_end = t.find("\n", match.end())
        line = t[line_start:line_end if line_end != -1 else len(t)]
        if found(r"опыт\w*[^\n]{0,45}не (?:обязател\w*|требуется)|будет (?:плюсом|преимуществом)|желател\w*", line):
            continue
        if f.experience_max is None or high > f.experience_max:
            f.experience_max = high
            f.experience_exclusive = match["qual"] in ("более", "больше", "свыше", "over", "more than")
            f.evidence["experience"] = match.group(0)
    # Also common: "3+ years of experience", "от 4 лет опыта".
    for match in re.finditer(rf"(?P<qual>от|более|свыше)?\s*(?P<n>{NUM})\s*(?P<plus>\+)?\s*"
                             r"(?:лет|года?|years?)(?:\s+of)?\s+(?:опыта|experience)", t):
        high = float(match["n"].replace(",", "."))
        if f.experience_max is None or high > f.experience_max:
            f.experience_max = high
            f.experience_exclusive = match["qual"] in ("более", "свыше")
            f.evidence["experience"] = match.group(0)
    if f.experience_max is None and found(r"без опыта|опыт\w*\s+(?:работы\s+)?не (?:требуется|обязател\w*)|no experience", t):
        f.experience_max = 0
        f.evidence["experience"] = "без опыта / опыт не обязателен"

    salaries = []
    for match in MONEY.finditer(t):
        line_start = t.rfind("\n", 0, match.start()) + 1
        line_end = t.find("\n", match.end())
        line = t[line_start:line_end if line_end != -1 else len(t)]
        before = t[max(line_start, match.start()-40):match.start()]
        after = t[match.end():min(len(t), match.end()+35)]
        if found(r"стоимость курса|взнос|стоимость обучения|штраф", before):
            continue
        if not found(SALARY_LINE, line) and not found(r"месяц|/мес|per month", after):
            continue
        # A shared suffix applies to both endpoints: 40–60 тыс. ₽.
        low = _money_number(match["a"], bool(match["ka"] or match["kb"]))
        high = _money_number(match["b"] or match["a"], bool(match["kb"] or match["ka"]))
        if high < low:
            continue
        currency = "RUB" if found(r"₽|руб|р\.|rub", match["currency"]) else match["currency"].upper()
        non_monthly = found(r"^\s*(?:/|в|за|per)?\s*(?:час|hour|день|смен\w*|day|проект|задан\w*|недел\w*|week)", after)
        monthly = not non_monthly and (found(r"оклад|зарплат\w*|з/?п|salary|фикс\w*|месяц|/мес", line))
        fixed = found(r"оклад|фиксирован\w*|фикс\b|fixed|base salary", line)
        rank = (2 if fixed else 1) + (2 if monthly else 0)
        salaries.append((rank, low, high, currency, monthly, fixed, match.group(0), before))
    if salaries:
        _, low, high, currency, monthly, fixed, evidence, before = max(salaries, key=lambda s: s[0])
        f.salary_min, f.salary_max, f.currency, f.salary_monthly = low, high, currency, monthly
        f.evidence["salary"] = evidence
        # "До 60 тыс." does not guarantee a 60k base.
        if found(r"до\s*$", before) and low == high:
            f.salary_min = None
        f.fixed = True if fixed else None
    # A fixed base may omit a repeated currency: "Оклад 30 000 + бонус до 100 000 ₽".
    # Prefer the explicitly labelled base over the advertised total earnings.
    base_pattern = re.compile(
        rf"(?:оклад|фиксирован\w*\s+(?:зарплат\w*|оплат\w*)|фикс)\s*[:—=]?\s*"
        rf"(?P<qual>от|до)?\s*(?P<a>{MONEY_NUM})\s*(?P<ka>тыс\.?|[kк])?\s*"
        rf"(?:(?:[-–—]|до)\s*(?P<b>{MONEY_NUM})\s*(?P<kb>тыс\.?|[kк])?\s*)?"
        r"(?P<currency>₽|руб(?:\.|лей|ля|ль)?|р\.|rub\b|\$|usd\b|€|eur\b)?", re.I
    )
    for base in base_pattern.finditer(t):
        currency = base["currency"]
        if not currency:
            currencies = {s[3] for s in salaries}
            if len(currencies) == 1:
                currency = next(iter(currencies))
            else:
                continue
        after = t[base.end():base.end()+30]
        if found(r"^\s*(?:/|в|за|per)?\s*(?:час|hour|день|смен\w*|day|проект|недел\w*)", after):
            continue
        low = _money_number(base["a"], bool(base["ka"] or base["kb"]))
        high = _money_number(base["b"] or base["a"], bool(base["kb"] or base["ka"]))
        if high < low:
            continue
        f.salary_min = None if base["qual"] == "до" and low == high else low
        f.salary_max = high
        f.currency = "RUB" if found(r"₽|руб|р\.|rub", currency) else currency.upper()
        f.salary_monthly, f.fixed = True, True
        f.evidence["salary"] = base.group(0).strip()
        break
    if found(r"без оклада|оклад\w*\s+(?:нет|не предусмотрен)|только\s*(?:процент\w*|%|комисси\w*)|"
             r"commission[- ]only|сдельн\w*\s+оплат\w*", t):
        f.fixed = False
    return f


def classify(text: str, profile: Profile | None = None, *, facts: Facts | None = None,
             description: VacancyDescription | None = None) -> Match:
    p = profile or Profile()
    f, t = facts if facts is not None else extract(text), normalize(text)
    reasons, unknowns, reject = [], [], []
    score = 5
    if not f.is_vacancy:
        reject.append("Не похоже на вакансию работодателя")
    excluded = [word for word in p.exclude_keywords if contains(t, word)]
    if excluded:
        reject.append("Исключённые слова: " + ", ".join(excluded))
    if f.remote is True:
        score += 30
        reasons.append("Указана удалённая работа")
    elif f.remote is False:
        if p.remote_only:
            reject.append("Офисный или гибридный формат")
    else:
        score += 10
        if p.remote_only:
            unknowns.append("Формат работы не указан")
    if not p.remote_only and f.remote is not True:
        score += 20
    if f.hours_max is None:
        score += 10
        unknowns.append("Не указаны часы в день")
    elif f.hours_max > p.hours_max:
        reject.append(f"Занятость до {f.hours_max:g} ч/день, больше {p.hours_max:g}")
    elif f.hours_max < p.hours_min:
        score += 15
        reasons.append(f"Занятость {f.hours_max:g} ч/день, меньше желаемой")
    else:
        score += 25
        reasons.append(f"Занятость {f.hours_min:g}–{f.hours_max:g} ч/день")
    salary_known = f.currency == "RUB" and f.salary_monthly and f.salary_min is not None
    if salary_known:
        if f.salary_min < p.salary_min:
            reject.append(f"Гарантированная сумма {f.salary_min:,.0f} ₽ ниже желаемой")
        else:
            score += 25
            reasons.append(f"Зарплата от {f.salary_min:,.0f} ₽/месяц")
    else:
        score += 10
        unknowns.append("Нет гарантированной месячной суммы в рублях")
    if p.fixed_salary_only:
        if f.fixed is False:
            reject.append("Нет фиксированного оклада")
        elif f.fixed is None:
            unknowns.append("Фиксированный оклад не подтверждён")
    if f.experience_max is None:
        score += 6
        unknowns.append("Требуемый опыт не указан")
    elif f.experience_max > p.max_experience_years or (
        f.experience_max == p.max_experience_years and f.experience_exclusive
    ):
        reject.append(f"Требуется опыт: {f.evidence['experience']}")
    else:
        score += 15
        reasons.append("Без опыта" if f.experience_max == 0 else f"Опыт до {f.experience_max:g} лет")
    if p.keywords:
        matching = [word for word in p.keywords if contains(t, word)]
        score = max(0, score - 5) + round(5 * len(matching) / len(p.keywords))
        if matching:
            reasons.append("Совпадение по словам: " + ", ".join(matching))
        else:
            unknowns.append("Нет совпадений с желаемыми словами")
    score = min(100, score)
    description = (description or extract_description(text)) if f.is_vacancy else None
    summary = summarize(text, f, description)
    if reject:
        return Match(0, False, "Не подходит", reject + reasons, unknowns, f, summary, description=description)
    minimum = max(MIN_SCORE, p.min_score)
    eligible = score >= minimum and (p.include_unknown or not unknowns)
    verdict = "Нужно уточнить" if unknowns else "Подходит"
    if not eligible:
        verdict = "Ниже порога" if score < minimum else "Недостаточно данных"
    category = categorize(text, description.title) if eligible else None
    return Match(score, eligible, verdict, reasons, unknowns, f, summary, category, description)


def split_post(text: str) -> list[str]:
    """Split only explicit repeated job headings; never borrow facts across jobs."""
    starts = [m.start() for m in re.finditer(
        r"(?im)^[ \t]*(?:(?:\d{1,2}[.)]|[🔹🔸📌])[ \t]*)?(?:#вакансия\b|вакансия[ \t]*[:№]|vacancy[ \t]*:)", text
    )]
    if len(starts) < 2:
        return [text.strip()]
    parts, prefix = [], ""
    for start, end in zip(starts, starts[1:] + [len(text)]):
        part = text[start:end].strip()
        if re.fullmatch(r"(?:#\w+\s*)+", part):
            prefix += part + "\n"
            continue
        parts.append(prefix + part)
        prefix = ""
    if prefix and parts:
        parts[-1] += "\n" + prefix.strip()
    return parts if len(parts) >= 2 else [text.strip()]
