"""Independent, evidence-based selection of experienced remote specialists."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .description import (SECTION_HEADING, SECTION_NAMES, VacancyDescription,
                          duty_text, extract_description, sections, shorten)
from .matcher import Facts, extract, normalize


PRO_GROUPS = {
    1: "Разработка и IT",
    2: "Аналитика",
    3: "Маркетинг",
    4: "Продажи",
    5: "Продукты и проекты",
}
PRO_PAGE_SIZE = 10


def pro_group_label(group: int) -> str:
    return f"Группа {group} — {PRO_GROUPS[group]}"


@dataclass(frozen=True)
class SpecialistRule:
    roles: str
    skills: str
    duties: str


RULES = {
    1: SpecialistRule(
        r"программист\w*|разработчик\w*|(?:backend|frontend|full[- ]?stack|back[- ]end|front[- ]end|"
        r"mobile|ios|android|software|web|python|java|golang|php|c\+\+|\.net)\s+(?:developer|engineer)|developer|"
        r"(?:devops|sre|qa)(?:[- ](?:engineer|инженер\w*))?|тестировщик\w*|"
        r"системн\w*\s+администратор\w*|(?:инженер\w*|специалист\w*)\s+(?:по\s+)?(?:тестировани\w*|информационн\w*\s+безопасност\w*)|"
        r"data engineer|ml engineer|machine learning engineer|software architect|архитектор\w*\s+(?:по|программ\w*)",
        r"python|java(?:script)?|typescript|golang|go|c\+\+|c#|\.net|php|ruby|rust|kotlin|swift|"
        r"sql|postgres\w*|mysql|react|vue|angular|django|fastapi|spring|laravel|node\.?js|"
        r"docker|kubernetes|linux|git|ci/cd|aws|azure|terraform|ansible|selenium|pytest|"
        r"тест[- ]?кейс\w*|автоматизаци\w*\s+тест\w*|api|tcp/ip|pytorch|tensorflow",
        r"(?:разработ\w*|проектир\w*|создава\w*)[^\n.;]{0,60}(?:приложени\w*|сервис\w*|api|программ\w*)|"
        r"(?:писать|поддерживать|ревью)\s+(?:программн\w*\s+)?код\w*|(?:develop|build)\w*[^\n.;]{0,50}(?:software|applications?|apis?)",
    ),
    2: SpecialistRule(
        r"аналитик\w*|(?:data|business|system|systems|product|financial|marketing|bi)\s+analyst|"
        r"data scientist|дата[- ]?сайентист\w*|бизнес[- ]аналитик\w*|системн\w*\s+аналитик\w*",
        r"sql|python|\br\b|power\s?bi|tableau|qlik|excel|bpmn|uml|etl|dwh|"
        r"статистик\w*|матстат\w*|a/b|а/б|ab[- ]тест\w*|когорт\w*|юнит[- ]экономик\w*|"
        r"unit economics|продуктов\w*\s+метрик\w*|сводн\w*\s+таблиц\w*|дашборд\w*|dashboard\w*",
        r"(?:анализ\w*|обрабатыва\w*)[^\n.;]{0,45}(?:данн\w*|метрик\w*|data)|"
        r"(?:собирать|описывать|формализ\w*)[^\n.;]{0,45}(?:бизнес[- ]требован\w*|требован\w*\s+к\s+систем\w*)",
    ),
    3: SpecialistRule(
        r"маркетолог\w*|таргетолог\w*|директолог\w*|(?:seo|ppc|performance|digital|growth|"
        r"email|e-mail|crm|smm)[- ](?:специалист\w*|менеджер\w*|маркетолог\w*|manager|specialist|marketer)|"
        r"(?:marketing|brand|traffic)\s+(?:manager|specialist|lead)|маркетинг[- ]менеджер\w*|"
        r"менеджер\w*\s+(?:по\s+)?маркетинг\w*|интернет[- ]маркетолог\w*|cmo|head of marketing",
        r"яндекс[. ]?директ|yandex direct|яндекс[. ]?метрик\w*|google ads|google analytics|ga4|"
        r"seo|ppc|таргет\w*|контекстн\w*\s+реклам\w*|рекламн\w*\s+кабинет\w*|"
        r"медиаплан\w*|romi|roas|cpa|cpl|ctr|cac|ltv|email[- ]маркетинг\w*|сквозн\w*\s+аналитик\w*",
        r"(?:настраива\w*|запуска\w*|оптимизир\w*)[^\n.;]{0,45}(?:реклам\w*|кампани\w*|таргет\w*)|"
        r"(?:разрабатыва\w*|управля\w*)[^\n.;]{0,45}(?:маркетингов\w*|рекламн\w*)",
    ),
    4: SpecialistRule(
        r"менеджер\w*\s+(?:по\s+)?(?:продаж\w*|развити\w*\s+бизнес\w*|работе\s+с\s+клиент\w*)|"
        r"аккаунт[- ]менеджер\w*|руководител\w*\s+(?:отдел\w*\s+)?продаж\w*|"
        r"(?:sales|account|business development|customer success)\s+(?:manager|executive|specialist|lead|representative)|"
        r"bizdev|bdm|head of sales|коммерческ\w*\s+директор\w*",
        r"b2b|b2c|enterprise|saas|crm|bitrix\w*|битрикс\w*|amocrm|salesforce|hubspot|"
        r"spin|meddic|sandler|сложн\w*\s+продаж\w*|переговор\w*|закрыти\w*\s+сдел\w*|"
        r"воронк\w*\s+продаж\w*|холодн\w*\s+продаж\w*|тендер\w*",
        r"(?:продава\w*|продаж\w*|закрыва\w*)[^\n.;]{0,45}(?:b2b|сдел\w*|saas|корпоративн\w*)|"
        r"(?:привлека\w*|искать)\s+(?:корпоративн\w*|b2b)[- ]?(?:клиент\w*)",
    ),
    5: SpecialistRule(
        r"(?:продакт|проджект)[- ]?(?:менеджер\w*)?|(?:продуктов\w*|проектн\w*)\s+менеджер\w*|"
        r"менеджер\w*\s+(?:по\s+)?(?:продукт\w*|проект\w*)|руководител\w*\s+(?:продукт\w*|проект\w*)|"
        r"(?:product|project|delivery)\s+(?:manager|owner|lead)|head of product|cpo",
        r"agile|scrum|kanban|jira|confluence|roadmap|backlog|бэклог\w*|беклог\w*|роадмап\w*|"
        r"custdev|customer development|unit economics|юнит[- ]экономик\w*|p&l|sdlc|"
        r"приоритизаци\w*|продуктов\w*\s+метрик\w*|управлени\w*\s+(?:проект\w*|бюджет\w*)",
        r"(?:управля\w*|управлени\w*|развива\w*)[^\n.;]{0,40}(?:продукт\w*|проект\w*)|"
        r"(?:вести|приоритизир\w*)[^\n.;]{0,35}(?:бэклог\w*|беклог\w*|roadmap|backlog)",
    ),
}
OPTIONAL = re.compile(
    r"желател\w*|будет\s+(?:плюсом|преимуществом)|не\s+(?:обязател\w*|требуется|требуем)|"
    r"nice to have|optional|not required|no experience", re.I,
)
REQUIREMENT = re.compile(
    r"опыт\w*|знани\w*|зна(?:ть|ешь|ете)|уме(?:ть|ешь|ете)|умени\w*|навык\w*|владе\w*|"
    r"понимани\w*|требован\w*|необходим\w*|обязател\w*|уверенн\w*|продвинут\w*|"
    r"experience|proficien\w*|skills?|must|required|knowledge|understand\w*", re.I,
)
EXPERIENCE_CLAIM = re.compile(r"(?<!\w)(?:опыт(?:а|ом)?|experience)(?!\w)", re.I)


def has(pattern: str, text: str) -> bool:
    return bool(re.search(r"(?<!\w)(?:" + pattern + r")(?!\w)", text, re.I))


@dataclass
class ProfessionalMatch:
    eligible: bool
    group: int | None
    facts: Facts
    description: VacancyDescription
    evidence: list[str] = field(default_factory=list)


def classify_professional(text: str) -> ProfessionalMatch:
    facts, description = extract(text), extract_description(text)
    rejected = ProfessionalMatch(False, None, facts, description)
    if not facts.is_vacancy or facts.remote is not True:
        return rejected
    normalized = normalize(re.sub(r"[*_`]", "", text))
    if has(r"(?:обучим|обучаем|обучение)\s+с\s+нуля|без\s+опыта\s+и\s+(?:навыков|знаний)|"
           r"no skills? (?:or experience )?required", normalized):
        return rejected
    title, duties = normalize(description.title), normalize(duty_text(text))
    # A role in requirements or the company description is not the advertised job.
    by_title = [group for group, rule in RULES.items() if has(rule.roles, title)]
    by_duties = [group for group, rule in RULES.items() if has(rule.duties, duties)]
    groups = by_title or by_duties
    if not groups:
        return rejected
    group = groups[0]
    rule = RULES[group]
    plain_text = re.sub(r"[*_`]", "", text)
    extracted = sections(plain_text)
    requirements = extracted["requirements"]
    evidence = []
    for raw in re.split(r"\n+|;|(?<=[.!?])\s+", requirements):
        line = raw.strip(" •●▪◦-–—\t")
        if line and not OPTIONAL.search(line) and (
            has(rule.skills, line) or EXPERIENCE_CLAIM.search(line)
            and not has(r"без\s+опыта|no experience|опыт\s*[:—-]?\s*0\s*(?:лет|год\w*)", line)
        ):
            evidence.append(shorten(line, 180))
    # Inline requirements are common in short channel announcements. Ignore
    # benefits and company boilerplate when looking for candidate experience.
    headings = list(SECTION_HEADING.finditer(plain_text))
    inline = plain_text
    for index in range(len(headings) - 1, -1, -1):
        heading = headings[index]
        if re.fullmatch(SECTION_NAMES["conditions"] + "|" + SECTION_NAMES["responsibilities"]
                        + r"|о компании|about (?:us|the company)|компания|company|работодатель|employer",
                        heading["name"], re.I):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(plain_text)
            inline = inline[:heading.start()] + "\n" + inline[end:]
    for raw in re.split(r"\n+|;|(?<=[.!?])\s+", inline):
        line = raw.strip(" •●▪◦-–—\t")
        if (not line or OPTIONAL.search(line) or not REQUIREMENT.search(line)
                or has(r"о\s+компании|компани\w*|на\s+рынке|мы\s+предлагаем|обучени\w*|поможем|научим|"
                       r"получи\w*\s+опыт\w*|набраться\s+опыта|приобрет\w*\s+опыт\w*|возможност\w*", line)):
            continue
        skill = has(rule.skills, line)
        experience = EXPERIENCE_CLAIM.search(line)
        if skill or experience and not has(r"без\s+опыта|опыт\w*\s+не\s+требуется|no experience|"
                                          r"опыт\s*[:—-]?\s*0\s*(?:лет|год\w*)", line):
            evidence.append(shorten(line, 180))
    evidence = list(dict.fromkeys(evidence))
    if not evidence:
        return rejected
    return ProfessionalMatch(True, group, facts, description, evidence[:4])


def parse_pro_request(arguments: str) -> tuple[int | None, int]:
    parts = arguments.lower().split()
    group, page = None, 1
    if parts and parts[0] in {str(group) for group in PRO_GROUPS}:
        group = int(parts.pop(0))
    if len(parts) == 2 and parts[0] == "page" and parts[1].isdigit() and 0 < int(parts[1]) <= 1000000:
        page = int(parts[1])
        parts = []
    if parts:
        raise ValueError("Пример: /pro, /pro 1, /pro page 2 или /pro 3 page 2. Группы: /prohelp")
    return group, page
