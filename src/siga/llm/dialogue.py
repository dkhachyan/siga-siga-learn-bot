"""Общее для разговорных маршрутов: как описать слова, профиль и историю.

`R3` придумывает рамку эпизода, `R4` ведёт разговор — но смотрят они на одно
и то же: персона, уровень, профиль диалога, целевые слова. Держать это в
одном месте нужно не ради экономии строк, а ради кэша (§8.4): статический
блок обоих маршрутов должен собираться байт в байт одинаково, иначе половина
экономии теряется на разнице в пробеле.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from dataclasses import dataclass

from siga.core.enums import Level
from siga.core.memory import Profile
from siga.persona import Persona


@dataclass(frozen=True, slots=True)
class WordBrief:
    """Слово так, как его видит модель: без базы и без лишних полей."""

    word_id: int
    lemma: str
    """С ударением и артиклем — то есть так, как слово пишется."""
    translation_ru: str | None = None
    box: int = 0
    """Ступень лестницы: 0 — человек его ещё не видел в разговоре."""

    def render(self) -> str:
        translation = f" — {self.translation_ru}" if self.translation_ru else ""
        familiarity = "новое" if self.box == 0 else f"бокс {self.box}"
        return f"[{self.word_id}] {self.lemma}{translation} ({familiarity})"


def words_block(words: Sequence[WordBrief]) -> str:
    """Целевые слова списком. Номера в квадратных скобках — ключ ответа.

    Модель обязана вернуть оценку по `word_id`, а не по написанию: человек
    напишет слово в другой форме или с ошибкой, и сопоставлять по строке
    значит терять ровно те случаи, ради которых всё затевалось.
    """
    return "\n".join(word.render() for word in words)


def profile_block(profile: Profile) -> str:
    """Профиль диалога для промпта. Пустой профиль — честная строка про это.

    JSON, а не проза: он и хранится JSON'ом, и модель просят его дополнять
    в том же виде. Лишний перевод формата туда-обратно — лишний повод
    разойтись.
    """
    if profile.is_empty():
        return "Профиль пуст: вы ещё не разговаривали. Ничего о человеке не выдумывай."
    return json.dumps(profile.to_dict(), ensure_ascii=False, indent=None)


def gap_note(last_seen_at: dt.datetime | None, now: dt.datetime) -> str:
    """Сколько прошло с прошлой реплики, FR-MEM-5.

    Ответ через пять часов — часть разговора: живой собеседник это заметит,
    а не продолжит сцену как ни в чём не бывало. Считаем грубо, до часов:
    точность здесь не нужна, а «прошло 4 ч 37 мин» звучит как отчёт.
    """
    if last_seen_at is None:
        return ""

    gap = now - last_seen_at
    hours = gap.total_seconds() / 3600
    if hours < 1:
        return ""
    if hours < 24:
        return f"С прошлой реплики прошло примерно {round(hours)} ч."
    return f"С прошлого разговора прошло {gap.days} дн."


def system_prompt(persona: Persona, task: str, schema: str) -> str:
    """Статический блок маршрута: персона, задача, формат ответа (§8.4).

    Порядок важен: сначала то, что не меняется никогда (персона), потом то,
    что меняется с релизом (задача и схема). Ни уровня, ни имени, ни дат —
    любая подстановка отсюда обнуляет кэш на всех последующих вызовах.
    """
    return f"{persona.prompt_block()}\n\n{task.strip()}\n\n{schema.strip()}"


def level_note(level: Level) -> str:
    """Одна строка про уровень — она идёт в переменную часть, не в системную."""
    return f"Уровень ученика: {level.value}. Говори так, чтобы он понял без словаря."
