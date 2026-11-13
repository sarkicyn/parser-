from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


MIN_SCORE = 60


@dataclass(frozen=True)
class Profile:
    remote_only: bool = True
    salary_min: int = 40000
    fixed_salary_only: bool = True
    min_score: int = MIN_SCORE
    include_unknown: bool = True
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)

    def validate(self) -> Profile:
        for name in ("remote_only", "fixed_salary_only", "include_unknown"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name}: нужно true или false")
        for name in ("salary_min", "min_score"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name}: нужно конечное число")
        if self.salary_min < 0:
            raise ValueError("Минимальный оклад: не меньше 0")
        if not MIN_SCORE <= self.min_score <= 100:
            raise ValueError(f"Оценка: от {MIN_SCORE} до 100")
        for name in ("keywords", "exclude_keywords"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
                raise ValueError(f"{name}: нужен список непустых строк")
        return self

    @classmethod
    def load(cls, path: Path) -> Profile:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        legacy = {"salary_max", "hours_min", "hours_max", "max_experience_years"}
        if not isinstance(data, dict) or set(data) - ({f.name for f in fields(cls)} | legacy):
            raise ValueError("В profile.json есть неизвестные поля или неверный формат")
        # Older profiles stored an upper salary preference; it no longer applies.
        for name in legacy:
            data.pop(name, None)
        # Upgrade the old threshold while preserving stricter saved preferences.
        score = data.get("min_score", MIN_SCORE)
        if type(score) in (int, float) and 0 <= score < MIN_SCORE:
            data["min_score"] = MIN_SCORE
        return cls(**data).validate()

    def save(self, path: Path) -> None:
        self.validate()
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def describe(self) -> str:
        salary = f"{self.salary_min:,.0f}".replace(",", " ")
        return (
            f"Формат: {'только удалённо' if self.remote_only else 'любой'}\n"
            "Занятость: любая, без влияния на оценку\n"
            f"Оклад: от {salary} ₽/месяц, без верхней границы\n"
            f"Фиксированная оплата: {'обязательна' if self.fixed_salary_only else 'не обязательна'}\n"
            "Профессиональные требования: обязательный опыт или конкретные навыки; без ограничения стажа\n"
            f"Минимальная оценка: {self.min_score:g}/100\n"
            f"Неполные объявления: {'показывать с пометкой' if self.include_unknown else 'скрывать'}\n"
            f"Желаемые слова: {', '.join(self.keywords) or 'не заданы'}\n"
            f"Исключения: {', '.join(self.exclude_keywords) or 'не заданы'}"
        )


def update_profile(profile: Profile, arguments: str) -> Profile:
    parts = arguments.split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Пример: /set salary 40000. Все варианты: /help")
    key, value = parts
    data = asdict(profile)
    switches = {"remote": "remote_only", "fixed": "fixed_salary_only", "unknown": "include_unknown"}
    if key in switches:
        if value not in ("on", "off"):
            raise ValueError("Укажите on или off")
        data[switches[key]] = value == "on"
    elif key in ("hours", "experience"):
        raise ValueError("Ограничения по часам и годам опыта больше не используются")
    elif key == "salary":
        numbers = value.replace(",", ".").split()
        if len(numbers) != 1:
            raise ValueError("Пример: /set salary 40000 (или 40). Верхней границы нет")
        minimum = float(numbers[0])
        data["salary_min"] = minimum * 1000 if 0 < minimum < 1000 else minimum
    elif key == "score":
        data["min_score"] = float(value)
    elif key in ("keywords", "exclude"):
        data[{"keywords": "keywords", "exclude": "exclude_keywords"}[key]] = (
            [] if value == "clear" else [s.strip().lower() for s in value.split(",") if s.strip()]
        )
    else:
        raise ValueError("Неизвестная настройка. Список: /help")
    return Profile(**data).validate()
