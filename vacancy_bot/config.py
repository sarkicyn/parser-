from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Заполните {name} в .env")
    return value


def positive_integer(name: str, default: int, maximum: int) -> int:
    try:
        result = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name}: нужно целое число") from None
    if not 1 <= result <= maximum:
        raise ValueError(f"{name}: нужно число от 1 до {maximum}")
    return result


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    token: str = ""
    owner: int = 0
    folder: str = ""
    poll_seconds: int = 30
    history_limit: int = 100
    max_age_days: int = 7
    notifications_limit: int = 10
    responses_folder: str = "Удаленки"

    @property
    def tg_max_age_days(self) -> int:
        # Telegram publications are capped at 72 hours.
        return min(self.max_age_days, 3)

    @classmethod
    def load(cls, account_only: bool = False):
        load_dotenv(ROOT / ".env")
        try:
            api_id = int(required("TELEGRAM_API_ID"))
            owner = 0 if account_only else int(required("OWNER_TELEGRAM_ID"))
        except ValueError as exc:
            if "Заполните" in str(exc):
                raise
            raise ValueError("TELEGRAM_API_ID и OWNER_TELEGRAM_ID должны быть целыми числами") from None
        if api_id <= 0 or (not account_only and owner <= 0):
            raise ValueError("Telegram ID должны быть положительными числами")
        return cls(
            api_id=api_id, api_hash=required("TELEGRAM_API_HASH"),
            token="" if account_only else required("BOT_TOKEN"), owner=owner,
            folder=os.environ.get("TELEGRAM_FOLDER", "").strip(),
            responses_folder=os.environ.get("RESPONSES_FOLDER", "Удаленки").strip() or "Удаленки",
            poll_seconds=positive_integer("POLL_SECONDS", 30, 3600),
            history_limit=positive_integer("HISTORY_LIMIT", 100, 1000),
            max_age_days=positive_integer("MAX_POST_AGE_DAYS", 7, 365),
            notifications_limit=positive_integer("MAX_NOTIFICATIONS_PER_CYCLE", 10, 100),
        )
