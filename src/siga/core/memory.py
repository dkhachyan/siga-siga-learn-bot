"""Профиль диалога: что Ник помнит о человеке и как это влезает в потолок.

Чистая логика §5.5. Профиль ходит в промпт каждого `R4`, то есть его размер —
это цена каждой реплики, а не разовая трата. Отсюда жёсткий потолок и правила
вытеснения; отсюда же и то, что профиль — обычный словарь, а не набор колонок:
он всегда читается и пишется целиком.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Self

#: Потолок профиля, §5.5.
MAX_TOKENS = 800

#: Сколько символов в токене. Оценка грубая и намеренно пессимистичная:
#: кириллица и греческий дробятся мельче латиницы, и ошибиться лучше в сторону
#: «профиль больше, чем кажется», чем однажды раздуть промпт вдвое.
CHARS_PER_TOKEN = 3

#: Верхние границы списков — до всякого счёта токенов. Потолок ловит совсем
#: разросшийся профиль, а это просто форма: двадцать «недавних тем» перестают
#: быть недавними.
MAX_FACTS = 20
MAX_ERRORS = 10
MAX_TOPICS = 8


@dataclass(slots=True)
class Profile:
    """То же, что лежит в `dialog_memory.profile`, но с гарантиями по форме."""

    facts: list[str] = field(default_factory=list)
    """Только то, что человек сообщил сам (FR-MEM-2). Ничего не додумываем."""

    recurring_errors: list[str] = field(default_factory=list)
    recent_topics: list[str] = field(default_factory=list)
    tone_notes: str = ""
    last_seen_at: dt.datetime | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Self:
        """Разобрать то, что лежит в базе. Мусор молча игнорируется.

        Профиль пишет модель, и однажды она напишет туда не то. Падать на
        разборе нельзя: это стоило бы человеку разговора из-за строки, которую
        он даже не видел.
        """
        raw = raw or {}
        tone = raw.get("tone_notes")
        return cls(
            facts=_strings(raw.get("facts")),
            recurring_errors=_strings(raw.get("recurring_errors")),
            recent_topics=_strings(raw.get("recent_topics")),
            tone_notes=tone.strip() if isinstance(tone, str) else "",
            last_seen_at=_moment(raw.get("last_seen_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Обратно в JSON. Пустые поля не пишем — они и в промпте лишние."""
        data: dict[str, Any] = {}
        if self.facts:
            data["facts"] = self.facts
        if self.recurring_errors:
            data["recurring_errors"] = self.recurring_errors
        if self.recent_topics:
            data["recent_topics"] = self.recent_topics
        if self.tone_notes:
            data["tone_notes"] = self.tone_notes
        if self.last_seen_at is not None:
            data["last_seen_at"] = self.last_seen_at.isoformat()
        return data

    def is_empty(self) -> bool:
        return not self.to_dict()


def _strings(value: Any) -> list[str]:
    """Список непустых строк из чего угодно."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _moment(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def estimate_tokens(profile: Profile) -> int:
    """Во сколько токенов профиль обойдётся в промпте."""
    text = json.dumps(profile.to_dict(), ensure_ascii=False)
    return len(text) // CHARS_PER_TOKEN


def trim(profile: Profile, *, max_tokens: int = MAX_TOKENS) -> Profile:
    """Ужать профиль до потолка, FR-MEM-3.

    Порядок вытеснения — от наименее ценного: сначала старые темы разговоров,
    потом повторяющиеся ошибки, и только в последнюю очередь факты. Факт вроде
    «есть дочь шести лет» — это то, ради чего память вообще заводилась, а тема
    «кофе» перестаёт быть полезной через неделю.

    Списки урезаются с начала: новое дописывается в конец, значит начало — самое
    старое.
    """
    trimmed = Profile(
        facts=profile.facts[-MAX_FACTS:],
        recurring_errors=profile.recurring_errors[-MAX_ERRORS:],
        recent_topics=profile.recent_topics[-MAX_TOPICS:],
        tone_notes=profile.tone_notes,
        last_seen_at=profile.last_seen_at,
    )

    for attribute in ("recent_topics", "recurring_errors", "facts"):
        items: list[str] = getattr(trimmed, attribute)
        while items and estimate_tokens(trimmed) > max_tokens:
            items.pop(0)

    return trimmed


def merge(
    profile: Profile,
    *,
    facts: Iterable[str] = (),
    recurring_errors: Iterable[str] = (),
    recent_topics: Iterable[str] = (),
    tone_notes: str | None = None,
    max_tokens: int = MAX_TOKENS,
) -> Profile:
    """Дописать в профиль то, что модель заметила за эпизод (FR-MEM-1).

    Только дополнение: удалять записи модель не может — это право человека
    (FR-MEM-4). Повтор уже известного игнорируется, иначе профиль за неделю
    зарастёт десятком «работает в IT» и вытеснит собой всё остальное.

    `tone_notes` — единственное поле, которое перезаписывается: это одна
    заметка о манере, а не список.
    """
    merged = Profile(
        facts=_append(profile.facts, facts),
        recurring_errors=_append(profile.recurring_errors, recurring_errors),
        recent_topics=_append(profile.recent_topics, recent_topics),
        tone_notes=tone_notes.strip() if tone_notes and tone_notes.strip() else profile.tone_notes,
        last_seen_at=profile.last_seen_at,
    )
    return trim(merged, max_tokens=max_tokens)


def forget_fact(profile: Profile, index: int) -> Profile:
    """Убрать один факт по номеру (FR-MEM-4). Чужой номер профиль не меняет.

    Только факты: это то, что бывает про человека неверным. «Повторяющиеся
    ошибки» — наблюдение бота, оно само устареет; «недавние темы» вытесняются
    сами по себе. Держать кнопку удаления над каждым из трёх списков значило
    бы превратить экран доверия в панель управления.
    """
    if not 0 <= index < len(profile.facts):
        return profile
    remaining = list(profile.facts)
    remaining.pop(index)
    return Profile(
        facts=remaining,
        recurring_errors=list(profile.recurring_errors),
        recent_topics=list(profile.recent_topics),
        tone_notes=profile.tone_notes,
        last_seen_at=profile.last_seen_at,
    )


def _append(existing: Sequence[str], added: Iterable[str]) -> list[str]:
    """Дописать новое в конец, пропуская то, что уже есть.

    Сравниваем без регистра и пробелов по краям: «Работает в IT» и
    «работает в IT » — одна и та же запись, и хранить обе незачем.
    """
    result = list(existing)
    seen = {item.casefold() for item in result}
    for item in added:
        if not isinstance(item, str):
            continue
        value = item.strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        result.append(value)
        seen.add(key)
    return result
