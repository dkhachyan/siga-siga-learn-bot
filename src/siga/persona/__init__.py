"""Персона собеседника: кто такой Ник и как он говорит (§3).

Текст персоны лежит в TOML рядом, а не в коде: его придётся переписывать по
живым разговорам, и это работа над текстом. Здесь только чтение файла и та
форма, в которой персона уходит в промпт.

Читается один раз за процесс — файл не меняется на ходу, а разбор TOML на
каждом вызове `R4` был бы платой ни за что.
"""

from __future__ import annotations

import random
import tomllib
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any

DEFAULT_PERSONA = "nikos"


@dataclass(frozen=True, slots=True)
class Persona:
    """Всё, что модель должна знать о собеседнике, которого играет."""

    name: str
    """Как Ника зовут по-гречески — так он и представляется."""
    display_name: str
    """Как мы называем его в русских служебных текстах."""

    bio: str
    rules: tuple[str, ...] = ()
    constants: dict[str, Any] = field(default_factory=dict)
    """Неизменные факты (FR-PER-3): возраст, город, работа, кот."""
    nudges: tuple[str, ...] = ()
    """Чем Ник напоминает о себе, если человек замолчал (FR-SCH-6)."""
    max_words: int = 25

    def nudge(self, rng: random.Random | None = None) -> str:
        """Одна фраза-напоминание. Случайная — чтобы не приедалась.

        В промпт не уходит: пинок пишется без модели, поэтому и в статическом
        блоке ему делать нечего.
        """
        if not self.nudges:
            return ""
        return (rng or random).choice(self.nudges)

    def prompt_block(self) -> str:
        """Статическая часть системного промпта (§8.4).

        Собирается из файла целиком и без единой подстановки: любой байт,
        зависящий от пользователя, ломает префиксный кэш провайдера — и с ним
        экономию в тридцать раз на каждом вызове.
        """
        rules = "\n".join(f"- {rule}" for rule in self.rules)
        return f"{self.bio.strip()}\n\nКак ты говоришь:\n{rules}"


@cache
def load(name: str = DEFAULT_PERSONA) -> Persona:
    """Прочитать персону по имени файла в `siga/persona`."""
    source = resources.files(__package__).joinpath(f"{name}.toml").read_bytes()
    raw = tomllib.loads(source.decode())

    style = raw.get("style", {})
    return Persona(
        name=raw["name"],
        display_name=raw["display_name"],
        bio=raw["bio"],
        rules=tuple(style.get("rules", ())),
        constants=dict(raw.get("constants", {})),
        nudges=tuple(raw.get("nudges", ())),
        max_words=int(style.get("max_words", 25)),
    )


__all__ = ["DEFAULT_PERSONA", "Persona", "load"]
