"""Telegram formatting without interpreting vacancy text as markup."""
from __future__ import annotations

import re


GROUP_HEADING = re.compile(r"(?m)^(?:Категория: )?(Группа \d+ — [^\r\n·]+?)(?=[ \t]*(?:·|\r?$))")


def group_entities(text: str) -> list[dict]:
    return [
        {"type": "bold", "offset": len(text[:match.start(1)].encode("utf-16-le")) // 2,
         "length": len(match[1].encode("utf-16-le")) // 2}
        for match in GROUP_HEADING.finditer(text)
    ]
