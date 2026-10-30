"""Local, responsibility-aware role classification, independent of eligibility.

Rules combine bilingual role synonyms with action/object concepts. Adding a role
or a responsibility family only requires extending a CategoryRule below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .description import duty_text, extract_description


CATEGORY_NAMES = {
    1: "Content & Creative",
    2: "Chat Support & Moderation",
    3: "Management & Sales",
    4: "Other / Uncategorized",
}


def category_label(category: int) -> str:
    return f"Группа {category} — {CATEGORY_NAMES[category]}"


@dataclass(frozen=True)
class CategoryRule:
    group: int
    titles: str
    duties: tuple[str, ...]


GAP = r"[^\n.!?;]{0,65}"
RULES = (
    CategoryRule(1,
        r"копирайтер\w*|редактор\w*|корректор\w*|сценарист\w*|райтер\w*|"
        r"smm(?:[- ](?:специалист\w*|менеджер\w*|specialist|manager))?|"
        r"контент[- ]?(?:менеджер\w*|мейкер\w*|креатор\w*)|менеджер\w*\s+(?:по\s+)?контент\w*|"
        r"(?:text |video |content )?editor|copy\s?writer|proofreader|script\s?writer|"
        r"screenwriter|social[- ]media\s+(?:manager|specialist)|"
        r"content\s+(?:manager|creator|writer|producer)|technical writer|"
        r"специалист\w*\s+(?:по\s+)?соцсет\w*|менеджер\w*\s+(?:соцсет\w*|социальн\w*\s+сет\w*)",
        (
            rf"(?:пис\w*|написани\w*|созда\w*|подготов\w*|придумыва\w*|write|writing|creat\w*|produc\w*|craft\w*|compos\w*|author\w*){GAP}(?:текст\w*|стат\w*|пост\w*|сценари\w*|copy\b|articles?|scripts?|blog posts?|stories)",
            rf"(?:редактир\w*|редактур\w*|корректур\w*|вычит\w*|edit\w*|proofread\w*){GAP}(?:текст\w*|стат\w*|публикаци\w*|материал\w*|copy\b|articles?|content|texts?|manuscripts?)",
            rf"(?:вед\w*|вести|разви\w*|публиков\w*|manage|maintain|publish\w*){GAP}(?:соцсет\w*|социальн\w* сет\w*|пост\w*|social[- ]media|social accounts?|posts?|instagram|telegram[- ]канал\w*)",
            r"контент[- ]?план\w*|редакцион\w*\s+(?:план\w*|календар\w*)|content (?:calendar|plan|strategy)|editorial calendar",
            rf"(?:созда\w*|снима\w*|монтир\w*|creat\w*|edit\w*|produc\w*){GAP}(?:контент\w*|ролик\w*|видео|content|videos?|reels)",
            r"сторителлинг|storytelling|провер\w*[^\n.!?;]{0,40}(?:орфограф\w*|пунктуаци\w*)|correct\w*[^\n.!?;]{0,40}(?:grammar|spelling)",
        )),
    CategoryRule(2,
        r"модератор\w*|чат[- ]?(?:оператор\w*|поддержк\w*)|оператор\w*\s+(?:(?:онлайн|online)[- ]?)?чат\w*|"
        r"(?:специалист\w*|оператор\w*|агент\w*)\s+(?:(?:клиентск\w*|техническ\w*)\s+)?(?:поддержк\w*|техподдержк\w*)|"
        r"(?:chat|community|content)\s+moderator|(?:online\s+)?chat\s+(?:support(?:\s+specialist)?|operator|agent)|"
        r"(?:customer|technical|customer service)\s+support(?:\s+(?:specialist|agent|operator))?|"
        r"support\s+(?:agent|specialist|operator)|customer service\s+(?:agent|representative|specialist)",
        (
            rf"(?:отвеч\w*|ответ\w*|обрабатыв\w*|обработк\w*|answer\w*|respond\w*|handle|handling){GAP}(?:чат\w*|письм\w*|сообщени\w*|обращени\w*|тикет\w*|chats?|messages?|emails?|tickets?|inquiries)",
            rf"(?:помога\w*|помощ\w*|поддерж\w*|реша\w*|решени\w*|assist\w*|help\w*|resolv\w*){GAP}(?:клиент\w*|пользовател\w*|customers?|users?|issues|tickets?)",
            rf"(?:модер\w*|moder\w*){GAP}(?:чат\w*|сообществ\w*|комментар\w*|контент\w*|публикаци\w*|chats?|community|communities|comments?|content|posts?)",
            rf"(?:удаля\w*|удалени\w*|блокир\w*|блокиров\w*|remove|removing|ban|banning|enforc\w*){GAP}(?:спам\w*|нарушител\w*|правил\w*|spam|abuse|rules|violations)",
            r"письменн\w*\s+(?:поддержк\w*|консультаци\w*)|text[- ]based (?:support|customer service)|обработк\w*\s+(?:обращени\w*|тикет\w*)",
        )),
    CategoryRule(3,
        r"(?:продуктов\w*|проектн\w*)\s+менеджер\w*|(?:продакт|проджект)[- ]?менеджер\w*|"
        r"менеджер\w*\s+(?:по\s+)?(?:продаж\w*|проект\w*|продукт\w*|работе\s+с\s+клиент\w*)|"
        r"аккаунт[- ]?менеджер\w*|руководител\w*\s+проект\w*|координатор\w*\s+проект\w*|"
        r"менеджер\w*\s+(?:по\s+)?развити\w*\s+бизнес\w*|менеджер\w*\s+по\s+сопровождени\w*\s+клиент\w*|"
        r"(?:product|project|sales|account|business development|customer success|client success)\s+manager|"
        r"project coordinator|client manager|sales\s+(?:representative|specialist|executive)|"
        r"business development\s+(?:representative|specialist)|client relationship\s+manager",
        (
            rf"(?:управл\w*|координ\w*|вед\w*|вести|manage|managing|coordinat\w*|lead\w*|oversee\w*){GAP}(?:проект\w*|продукт\w*|команд\w*|projects?|products?|teams?)",
            r"(?:roadmap|роадмап\w*|дорожн\w* карт\w*|product backlog|бэклог\w*)|(?:приоритиз\w*|prioriti\w*)[^\n.!?;]{0,40}(?:задач\w*|фич\w*|features|backlog)",
            rf"(?:контрол\w*|планир\w*|отслеж\w*|track\w*|plan\w*|manag\w*){GAP}(?:срок\w*|бюджет\w*|дедлайн\w*|deadlines?|budgets?|milestones?)",
            rf"(?:прода\w*|продаж\w*|sell|selling|close|closing){GAP}(?:услуг\w*|товар\w*|продукт\w*|сдел\w*|products?|services?|deals?|sales)",
            rf"(?:привле\w*|поиск|искать|генераци\w*|find|finding|acquir\w*|generat\w*|prospect\w*){GAP}(?:клиент\w*|лид\w*|customers?|clients?|leads)",
            r"(?:переговор\w*|negotia\w*)[^\n.!?;]{0,50}(?:клиент\w*|партнер\w*|договор\w*|client\w*|partner\w*|contract\w*)|воронк\w*\s+продаж\w*|sales pipeline",
            r"(?:продлени\w*|продлев\w*|renew\w*|upsell\w*|допродаж\w*|удержани\w*|retain\w*|retention)[^\n.!?;]{0,50}(?:клиент\w*|контракт\w*|подпис\w*|client\w*|customer\w*|contract\w*|subscription\w*)",
        )),
)


def categorize(text: str, title: str = "") -> int:
    """Return one group. The caller must apply eligibility filters first.

    Each distinct responsibility family votes once, so repetition cannot inflate
    a category. Two clear duty families outweigh a misleading role title.
    Title evidence breaks ties; final ties use stable rule order.
    """
    title = (title or extract_description(text).title).casefold().replace("ё", "е")
    duties = duty_text(text).casefold().replace("ё", "е")
    # Negated tasks must not act as evidence (e.g. "без продаж", "no selling").
    duties = re.sub(r"(?:без|не нужно|не требуется|не будете|no|without|not responsible for)\s+[^\n.;!?]+", "", duties)
    candidates = []
    for rule in RULES:
        title_match = bool(re.search(r"(?<!\w)(?:" + rule.titles + r")(?!\w)", title))
        duty_matches = sum(bool(re.search(pattern, duties)) for pattern in rule.duties)
        score = 5 * title_match + 4 * duty_matches
        if rule.group == 2 and re.search(r"только[^\n.;]{0,25}(?:звон\w*|телефон\w*)|(?:phone|calls?)[- ]only|only[^\n.;]{0,20}(?:phone|calls?)", duties):
            score = 0
        candidates.append((score, title_match, -rule.group, rule.group))
    best = max(candidates)
    return best[3] if best[0] else 4
