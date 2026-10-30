"""Conservative local comparison of reposts; no network or model calls."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .description import extract_description, title_text


EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)
TELEGRAM = re.compile(r"(?<![\w@])@([a-z][a-z0-9_]{3,31})\b|(?:https?://)?(?:t\.me|telegram\.me)/([a-z][a-z0-9_]{3,31})/?(?![\w/])", re.I)
PHONE = re.compile(r"(?<!\w)\+?\d[\d ().\-–—]{8,}\d(?!\w)")
URL = re.compile(r"(?:https?://|(?:t|telegram)\.me/)\S+", re.I)
PROMO = re.compile(r"(?i)подписывай\w*|подписаться|больше вакансий|наш (?:канал|чат)|разместить вакансию|реклама|subscribe|more vacancies")
STOPWORDS = frozenset("и в на с по для от до к из за о об а но или мы вы это как что который вам вас наш ваша ваши будет нужно вакансия vacancy обязанности задачи требования условия responsibilities requirements benefits ищем требуется нужен нужна the a an and or in on at to of for with we you your our is are".split())


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def words(text: str) -> frozenset[str]:
    return frozenset(word for word in re.findall(r"[a-zа-я0-9]+", normalize(text)) if word not in STOPWORDS)


def phone_number(value: str) -> str:
    number = re.sub(r"\D", "", value)
    if len(number) == 10 and number.startswith("9"):
        number = "7" + number
    if len(number) == 11 and number.startswith("8"):
        number = "7" + number[1:]
    if number.startswith("7"):
        return number if len(number) == 11 else ""
    if (len(number) == 11 and number.startswith("7")) or (value.startswith("+") and 10 <= len(number) <= 15):
        return number
    return ""


def contacts(text: str) -> frozenset[str]:
    # Channel advertisements must not act as a shared HR contact.
    text = "\n".join(line for line in normalize(text).splitlines() if not PROMO.search(line))
    result = {"email:" + match[0] for match in EMAIL.finditer(text)}
    without_emails = EMAIL.sub("", text)
    result.update("tg:" + (match[1] or match[2]) for match in TELEGRAM.finditer(without_emails))
    for match in PHONE.finditer(text):
        number = phone_number(match[0])
        if number:
            result.add("phone:" + number)
    return frozenset(result)


def content(text: str) -> str:
    text = "\n".join(line for line in normalize(text).splitlines() if not PROMO.search(line))
    text = title_text(text)
    text = EMAIL.sub("", text)
    text = TELEGRAM.sub("", text)
    text = PHONE.sub(lambda match: "" if phone_number(match[0]) else match[0], text)
    text = URL.sub("", text)
    text = re.sub(r"(?<!\w)#\w+", "", text)
    text = re.sub(r"(?<=\d)\s+(?=\d{3}\b)", "", text)
    # Contact labels differ between publishers and do not describe the job.
    text = re.sub(r"\b(?:контакты?|откликнуться|отклик|писать|пишите|hr|contact|apply)\s*[:—–-]?\s*(?=$|[\n.!])", "", text)
    return " ".join(re.findall(r"[a-zа-я0-9]+", text))


def overlap(left: frozenset[str], right: frozenset[str]) -> float:
    return 2 * len(left & right) / (len(left) + len(right)) if left and right else 0.0


@dataclass(frozen=True)
class Identity:
    body: str
    body_hash: str
    tokens: frozenset[str]
    role: frozenset[str]
    company: frozenset[str]
    location: frozenset[str]
    duties: frozenset[str]
    contacts: frozenset[str]

    @classmethod
    def from_text(cls, text: str) -> Identity:
        description = extract_description(text)
        body = content(text)
        return cls(body, hashlib.sha256(body.encode()).hexdigest(), words(body), words(description.title),
                   words(description.company), words(description.location),
                   words(" ".join(description.responsibilities)), contacts(text))

    def index_keys(self) -> set[str]:
        keys = {"body:" + self.body_hash}
        keys.update("contact:" + contact for contact in self.contacts)
        if self.role:
            keys.add("role:" + " ".join(sorted(self.role)))
        return keys


def same_vacancy(left: Identity, right: Identity) -> bool:
    if not left.body or not right.body:
        return False
    if left.contacts and right.contacts and not left.contacts & right.contacts:
        return False
    if left.company and right.company and left.company != right.company:
        return False
    if left.location and right.location and left.location != right.location:
        return False
    if left.role and right.role and overlap(left.role, right.role) < 0.8:
        return False
    if left.body == right.body:
        return True
    # A shared recruiter or a shared template alone is insufficient.
    if not left.role or not right.role or overlap(left.role, right.role) < 0.8:
        return False
    qualifiers = {"senior", "junior", "middle", "lead", "стажер", "ведущий"}
    if left.role & qualifiers != right.role & qualifiers:
        return False
    if {word for word in left.role if word.isdigit()} != {word for word in right.role if word.isdigit()}:
        return False
    if left.duties and right.duties and overlap(left.duties, right.duties) < 0.7:
        return False
    similarity = overlap(left.tokens, right.tokens)
    if left.contacts & right.contacts:
        return similarity >= 0.8 and min(len(left.tokens), len(right.tokens)) >= 10
    # Without a matching contact require an almost verbatim, substantial text.
    return (similarity >= 0.95 and min(len(left.tokens), len(right.tokens)) >= 20
            and SequenceMatcher(None, left.body, right.body, autojunk=False).ratio() >= 0.9)
