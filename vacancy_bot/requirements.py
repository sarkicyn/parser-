"""Evidence of candidate requirements, independent of a vacancy's job category."""
import re

from .description import SECTION_HEADING, SECTION_NAMES, sections, shorten


SKILLS = re.compile(
    r"(?<!\w)(?:python|java(?:script)?|typescript|sql|c\+\+|c#|golang|php|ruby|rust|kotlin|swift|"
    r"react|vue|angular|node\.?js|django|fastapi|docker|kubernetes|linux|git|aws|azure|api|"
    r"postgres(?:ql)?|mysql|mongodb|redis|terraform|ansible|graphql|pytorch|tensorflow|etl|"
    r"excel|power\s?bi|tableau|qlik|1с|1c|битрикс\w*|bitrix\w*|amocrm|salesforce|hubspot|"
    r"figma|photoshop|illustrator|indesign|after effects|premiere pro|autocad|revit|"
    r"jira|confluence|scrum|kanban|agile|bpmn|uml|seo|ppc|google ads|яндекс[. ]директ|"
    r"wordpress|tilda|webflow|notion|google analytics|google tag manager|"
    r"google analytics|яндекс[. ]метрик\w*|таргетированн\w* реклам\w*|контекстн\w* реклам\w*|"
    r"бухгалтерск\w* учет\w*|налогов\w* учет\w*|кадров\w* делопроизводств\w*|"
    r"финансов\w* моделирован\w*|управленческ\w* учет\w*|договорн\w* прав\w*|"
    r"копирайтинг\w*|редакторск\w* работ\w*|верстк\w*|вёрстк\w*|"
    r"b2b[- ]продаж\w*|ведение переговоров|управлени\w* проект\w*|"
    r"a/b[- ]тест\w*|статистическ\w* анализ\w*|медиапланирован\w*|"
    r"machine learning|data science|тестировани\w* по|автоматизаци\w* тест\w*|"
    r"аналитик\w* данных|веб[- ]аналитик\w*|контент[- ]стратеги\w*|"
    r"техническ\w* перевод\w*|английск\w* (?:язык\w* )?(?:b2|c1|c2)|"
    r"english (?:b2|c1|c2))(?!\w)", re.I)
OPTIONAL = re.compile(r"желател\w*|будет (?:плюсом|преимуществом)|не (?:обязател\w*|требу\w*)|"
                      r"не требуется|без опыта|обуч\w* с нуля|no experience|not required|nice to have|optional", re.I)
EXPERIENCE = re.compile(r"(?<!\w)(?:опыт(?:а|ом)?|experience)(?!\w)", re.I)
REQUIREMENT = re.compile(r"требован\w*|требует\w*|необходим\w*|обязател\w*|нуж(?:ен|на|ны|но)|"
                         r"знани\w*|зна(?:ть|ете|ешь)|умени\w*|уме(?:ть|ете|ешь)|навык\w*|владе\w*|"
                         r"ищем специалист\w* с|опыт|experience|proficien\w*|skills?|must|required|knowledge", re.I)
COMPANY = re.compile(r"на рынк\w*|компани\w*[^.\n]{0,30}(?:лет|год\w*|experience)|"
                     r"(?:получ\w*|приобрести|набраться) опыт\w*", re.I)


def professional_evidence(text: str) -> list[str]:
    """Find explicit, non-optional experience or specific skills required of a candidate."""
    body = re.sub(r"[*_`]", "", text)
    headings = list(SECTION_HEADING.finditer(body))
    requirements = sections(body)["requirements"]
    # Inline claims are considered outside duties, benefits and company descriptions.
    pieces = []
    start = 0
    for i, heading in enumerate(headings):
        pieces.append(body[start:heading.start()])
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        start = end
    if not headings:
        pieces.append(body)
    else:
        pieces.append(body[start:])
    evidence = []
    for text_part, in_section in [(requirements, True), ("\n".join(pieces), False)]:
        for raw in re.split(r"\n+|;|(?<=[.!?])\s+|,\s*(?=(?:но\s+)?(?:требует\w*|необходим\w*|обязател\w*|знани\w*|владе\w*|must|required))", text_part, flags=re.I):
            line = raw.strip(" •●▪◦-–—\t")
            if not line or OPTIONAL.search(line) or COMPANY.search(line):
                continue
            experience = EXPERIENCE.search(line) and not re.search(r"опыт\s*[:—-]?\s*0\s*(?:лет|год\w*)", line, re.I)
            skill = SKILLS.search(line)
            if (in_section or REQUIREMENT.search(line)) and (experience or skill):
                evidence.append(shorten(line, 180))
    return list(dict.fromkeys(evidence))[:4]
