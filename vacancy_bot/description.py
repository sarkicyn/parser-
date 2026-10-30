"""Extractive vacancy descriptions: keep source wording and omit absent fields."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


SECTION_NAMES = {
    "responsibilities": r"(?:основные |ваши |твои )?обязанности(?: и задачи)?|(?:основные |ваши |твои )?задачи|чем (?:предстоит |будете |будешь )?заниматься|(?:вам|тебе) предстоит|что (?:(?:нужно(?: будет)?|предстоит|будете|будешь) )?делать|(?:your )?responsibilities|duties|tasks|what you(?: will|'ll) (?:do|be doing)",
    "requirements": r"требования(?: к кандидату)?|ключевые навыки|от вас|от тебя|мы (?:ожидаем|жд[её]м)(?: от вас| от тебя)?|что (?:мы )?(?:ожидаем|жд[её]м)|что важно(?: для нас)?|кого (?:ищем|мы ищем)|наш кандидат|requirements|qualifications|your profile|skills|what you(?: bring| need)|you should",
    "conditions": r"условия|мы предлагаем(?: вам)?|предлагаем|от нас|что (?:мы )?предлагаем|benefits|we offer|what we offer|what you get|perks",
    "other": r"контакт(?:ы)?|для отклика|отклик(?:нуться)?|как откликнуться|о компании|about (?:us|the company)|apply|contact|компания|company|работодатель|employer|локация|location|город|формат(?: работы)?|work (?:format|mode)|график|занятость|employment(?: type)?|оклад|зарплата|оплата|salary|compensation",
}
SECTION_HEADING = re.compile(
    r"(?:^|(?<=[.!?;])\s+)[^\w\n]*"
    r"(?P<name>" + "|".join(SECTION_NAMES.values()) + r")"
    r"(?=\s*[:—–-]|\s*$)\s*[:—–-]?\s*", re.I | re.M
)


def clean(text: str) -> str:
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", "", text, flags=re.I)
    text = re.sub(r"https?://\S+|t\.me/\S+|(?<!\w)[@#]\w+", "", text)
    text = re.sub(r"[*_`\u200b\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip(" \t\r\n•●▪◦-–—:;.,!✅📌🔹🔸")


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:.") + "…"


def sections(text: str) -> dict[str, str]:
    """Separate duties from skills, benefits, and company boilerplate.

    Keep full sections for categorization; presentation truncates separately.
    """
    # Markdown emphasis must not prevent recognition of a heading.
    text = re.sub(r"[*_`]", "", text)
    result = {key: "" for key in SECTION_NAMES}
    headings = list(SECTION_HEADING.finditer(text))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        name = heading["name"]
        key = next(key for key, pattern in SECTION_NAMES.items() if re.fullmatch(pattern, name, re.I))
        result[key] += "\n" + text[heading.end():end]
    return result


def items(section: str, count: int = 3, limit: int = 140) -> list[str]:
    result = []
    for part in re.split(r"\n+|;|(?<=[.!?])\s+(?=[А-ЯЁA-Z])", section):
        if re.search(r"(?i)\b(?:пишите|пиши|отклик\w*|заполняйте|отправьте резюме|subscribe|apply|contact)\b", part):
            continue
        part = clean(re.sub(r"^\s*\d+[.)]\s*", "", part))
        if part and part.casefold() not in {item.casefold() for item in result}:
            result.append(shorten(part, limit))
        if len(result) == count:
            break
    return result


ACTION = re.compile(
    r"(?i)\b(?:писать|написани\w*|создава\w*|создани\w*|редактир\w*|проверя\w*|"
    r"проверка|вести|ведени\w*|отвеча\w*|ответы|помога\w*|обрабатыва\w*|обработка|"
    r"модерир\w*|модераци\w*|координир\w*|координаци\w*|управля\w*|управлени\w*|"
    r"продава\w*|продажи|привлека\w*|искать|готовить|подготовка|составля\w*|"
    r"write|writing|craft\w*|compos\w*|author\w*|create|creating|edit|editing|proofread\w*|publish\w*|respond\w*|"
    r"answer\w*|assist\w*|resolve\w*|moderate|moderating|manage|managing|coordinate|"
    r"coordinating|sell|selling|negotiate|develop\w*|maintain\w*|handle|handling|"
    r"you will|you'll|предстоит)\b"
)


def duty_text(text: str) -> str:
    """Prefer a duties section, otherwise action sentences outside other sections."""
    extracted = sections(text)
    if extracted["responsibilities"].strip():
        return extracted["responsibilities"].strip()
    body = re.sub(r"[*_`]", "", text)
    headings = list(SECTION_HEADING.finditer(body))
    excluded = []
    for index, heading in enumerate(headings):
        # Metadata is a single line. Requirements, benefits and company background
        # are full sections and must never be mistaken for responsibilities.
        if re.fullmatch(SECTION_NAMES["requirements"] + "|" + SECTION_NAMES["conditions"] + r"|о компании|about (?:us|the company)", heading["name"], re.I):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        else:
            end = body.find("\n", heading.end())
            end = end if end != -1 else len(body)
        excluded.append((heading.start(), end))
    for start, end in reversed(excluded):
        body = body[:start] + "\n" + body[end:]
    result = []
    for sentence in re.split(r"\n+|(?<=[.!?;])\s+", body):
        if re.search(r"(?i)\b(?:отклик\w*|контакт\w*|пишите|отправ\w* резюме|подписывай\w*|subscribe|apply|contact)\b", sentence):
            continue
        if re.search(r"(?i)\b(?:опыт|умение|навык\w*|требован\w*|experience|ability|skills?|company|компания)\b|^\W*(?:мы|we)\s+(?!are looking|ищем)", sentence):
            continue
        if ACTION.search(sentence):
            result.append(sentence)
    return "\n".join(result)


@dataclass
class VacancyDescription:
    title: str = ""
    company: str = ""
    salary: str = ""
    location: str = ""
    work_format: str = ""
    employment_type: str = ""
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)


def _label(text: str, names: str, limit: int = 140) -> str:
    match = re.search(r"(?im)(?:^|(?<=[.!?;])\s+)[^\w\n]*(?:" + names + r")\s*[:—–-]\s*([^\n;]+)", text)
    if not match:
        return ""
    # Do not absorb subsequent inline fields or unrelated sentences.
    value = re.split(r"(?<=[.!?])\s+|\s*[|]\s*", match[1])[0]
    return shorten(clean(value), limit)


HIRING = re.compile(
    r"(?i)\b(?:ищем|нанимаем|we are looking for|hiring|требуется|нужен|нужна|приглашаем)\s+"
    r"(?:(?:в (?:нашу )?команду|на (?:удал[её]нную )?работу)\s+)?([^\n.!?]+)"
)
JOB_LABEL = re.compile(
    r"(?i)^[^\w\n]*(?:(?:открыта|новая)\s+)?"
    r"(?:вакансия|vacancy|должность|позиция|position|job title|role)[ \t]*[:—–-]?[ \t]+([^\n.!?]+)"
)
GENERIC_ROLE = re.compile(r"(?i)^(?:опыт|знание|умение|навык|от вас|от тебя|людей|человека|на самом деле|того|тех)\b")


def title_text(text: str) -> str:
    """Preserve role hashtags, while removing distribution tags such as #Удаленка."""
    def hashtag(match):
        tag = match[1].casefold().replace("ё", "е")
        if re.fullmatch(r"ваканси\w*|удален\w*|remote|работа|jobs?|безопыта|parttime|fulltime", tag):
            return ""
        return match[1].replace("_", " ")
    return re.sub(r"(?<!\w)#(\w+)", hashtag, text)


def extract_title(text: str) -> str:
    lines = re.sub(r"[*_`]", "", title_text(text)).splitlines()
    for line in lines:
        labelled = JOB_LABEL.search(line)
        if labelled and not GENERIC_ROLE.match(clean(labelled[1])):
            return clean(labelled[1])
    # A headline at the top is more informative than a later "Ищем людей".
    for raw in lines[:8]:
        line = re.sub(r"^\W+", "", clean(raw)).strip(" /")
        if not line or line.casefold() in ("вакансия", "vacancy"):
            continue
        if SECTION_HEADING.match(line):
            break
        hiring = HIRING.search(line)
        if hiring:
            candidate = clean(hiring[1])
            if not GENERIC_ROLE.match(candidate):
                return candidate
            continue
        if (len(line) <= 100 and not re.search(r"[.!?]", line)
                and not re.match(r"(?i)(?:привет|добрый день|мы\b|о нас|подписывай|пишите|свежие вакансии|вакансия дня|"
                                 r"в (?:команду|компанию|онлайн)|ид[её]т|опыт|без опыта|полностью|удал[её]н|доход|"
                                 r"спокойная работа|кого (?:мы )?ищем)", line)):
            return line
    for line in lines:
        if re.search(r"(?i)кого (?:мы )?ищем|требования|контакт", line):
            continue
        hiring = HIRING.search(line)
        if hiring and not GENERIC_ROLE.match(clean(hiring[1])):
            return clean(hiring[1])
    return ""


def extract_description(text: str) -> VacancyDescription:
    title = extract_title(text)
    text = re.sub(r"[*_`]", "", text)
    result = VacancyDescription()
    result.title = re.sub(r"\([^)]*(?:график|опыт|удал[её]н|занятость|₽)[^)]*\)", "", title, flags=re.I).strip(" /()")
    # A title may introduce a company on the same line; don't call it part of the role.
    title_parts = re.split(r"\s+(?:в компанию|в компании|at)\s+", result.title, maxsplit=1, flags=re.I)
    result.title = re.split(
        r"(?i)\s*[,;|—–]\s*|\s+-\s+|\s+(?=(?:полностью )?(?:удал[её]нно|remote|part[- ]time|full[- ]time)\b)|\s+(?=\d[\d ]*\s*(?:₽|руб|usd|eur|\$))",
        title_parts[0], maxsplit=1,
    )[0].strip()
    result.title = shorten(result.title, 100)
    result.company = _label(text, r"компания|название компании|работодатель|company(?: name)?|employer")
    if not result.company and len(title_parts) > 1:
        result.company = shorten(title_parts[1], 100)
    if not result.company:
        match = re.search(r"(?i)\b(?:компания|компании|компанию)\s+[«\"]([^»\"\n]+)[»\"]", text)
        if match:
            result.company = shorten(clean(match[1]), 100)
    result.location = _label(text, r"локация|город|место работы|местоположение|регион|location|city|based in")
    result.salary = _label(text, r"оклад|зарплата|з/?п|оплата|salary|compensation")
    if not result.salary:
        match = re.search(r"(?im)(?:^|(?<=[.!?;])\s+)[^\w\n]*(?:оклад|зарплата|з/?п|оплата|salary|compensation)\s*[:—–-]?\s*((?:(?:от|до|from|up to)\s+)?[\d$€₽][^\n;]*)", text)
        if match:
            result.salary = shorten(clean(re.split(r"(?<=[.!?])\s+", match[1])[0]), 140)
    if result.salary and not re.search(r"\d", result.salary):
        # "По договорённости" is still explicit information about compensation.
        result.salary = result.salary if re.search(r"договор|negotiable", result.salary, re.I) else ""
    result.employment_type = _label(text, r"занятость|тип занятости|employment(?: type)?|job type")
    if not result.employment_type:
        match = re.search(r"(?i)\b(?:частичная занятость|полная занятость|полный рабочий день|подработка|стажировка|проектная работа|фриланс|part[- ]time|full[- ]time|freelance|internship|temporary|contract)\b", text)
        if match:
            result.employment_type = clean(match[0])
    # Remove negated office visits before looking for explicit hybrid/office work.
    format_text = re.sub(r"(?i)(?:не (?:нужно|нужны|требуется|требуются)|без)\s+(?:посещени\w*|поезд\w*|визит\w*)\s+(?:в\s+)?офис\w*", "", text)
    format_text = re.sub(r"(?i)\b(?:не|not|no)\s+(?:гибрид\w*|hybrid)\b", "", format_text)
    if re.search(r"(?i)\b(?:гибрид\w*|hybrid)\b|(?:посещени\w*|присутстви\w*|выход\w*)\s+(?:в\s+)?офис\w*|\d+\s+дн\w*\s+в\s+офис\w*", format_text):
        result.work_format = "гибрид"
    elif re.search(r"(?i)\b(?:on[- ]?site|office[- ]based)\b|(?:работа\s+в\s+|только\s+(?:в\s+)?|формат\s*[:—-]?\s*)офис\w*", format_text):
        result.work_format = "офис"
    elif not re.search(r"(?i)(?:не|no|not)\s+(?:удал[её]н\w*|remote)|удал[её]н\w*\s+(?:нет|не предусмотрен\w*)", format_text) and re.search(r"(?i)удал[её]н\w*|дистанцион\w*|\bremote\b|из дома|work from home", format_text):
        result.work_format = "удалённо"
    extracted = sections(text)
    duties = re.sub(r",\s+(?=(?:проверять|сверять|обновлять|работать|вести|отвечать|помогать|обрабатывать|готовить|писать)\b)",
                    "\n", duty_text(text), flags=re.I)
    duties = "\n".join(part for part in re.split(r"\n|(?<=[.!?])\s+", duties)
                        if not re.match(r"(?i)\s*без\b", part))
    result.responsibilities = items(duties)
    result.requirements = items(extracted["requirements"], count=2)
    result.conditions = items(extracted["conditions"], count=2)
    return result
