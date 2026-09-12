"""Правила эпизода: сколько слов, сколько ходов, когда закрывать.

Чистая логика §5.3 — то, что не зависит ни от модели, ни от базы. Решение
«продолжать или закрывать» модель тоже возвращает (§8.3), но последнее слово
здесь: лимит ходов и «все слова оценены» — правила продукта, и оставлять их
на усмотрение промпта значит спорить с моделью в проде.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence

from siga.core.enums import EpisodeIntent, Verdict

#: Целевых слов в эпизоде, FR-EP-1.
MIN_TARGET_WORDS = 1
MAX_TARGET_WORDS = 3

#: Ходов в эпизоде, FR-EP-1 и FR-EP-6.
MAX_TURNS = 4

#: Через сколько эпизод без ответа считается брошенным, FR-SCH-6.
EPISODE_TTL = dt.timedelta(hours=12)


def intent_for(boxes: Iterable[int]) -> EpisodeIntent:
    """Тип эпизода по боксам целевых слов (§5.3).

    Ведёт самое слабое слово: если в наборе есть незнакомое, эпизод строится
    вокруг знакомства с ним, иначе разговор перескочит через то, ради чего
    затевался.

    Пока возвращаются только D1 и D2 — этап 2. D3 (пользователь спрашивает,
    FR-EP-2), D4 и D5 приезжают на этапе 5; до тех пор слова верхних боксов
    попадают в D2, а не в тип, которого ещё нет.
    """
    weakest = min(boxes, default=0)
    return EpisodeIntent.D1 if weakest <= 1 else EpisodeIntent.D2


def target_words_limit(available: int) -> int:
    """Сколько слов взять в эпизод: не больше трёх и не больше, чем есть."""
    return min(available, MAX_TARGET_WORDS)


def all_targets_assessed(
    target_word_ids: Sequence[int], assessed: Mapping[int, Sequence[Verdict]]
) -> bool:
    """Все ли целевые слова успели получить оценку."""
    return all(assessed.get(word_id) for word_id in target_word_ids)


def should_close(
    *,
    turns_done: int,
    target_word_ids: Sequence[int],
    assessed: Mapping[int, Sequence[Verdict]],
    model_wants_close: bool,
    user_asked: bool = False,
) -> bool:
    """Пора ли закрывать эпизод, FR-EP-6.

    Просьба человека — вне очереди: «хватит» должно работать сразу, даже если
    модель считает, что разговор в разгаре. Лимит ходов тоже наш, а не её:
    модель, увлёкшись, готова беседовать бесконечно, а эпизод — это две-четыре
    реплики, иначе он перестаёт быть коротким.

    Разговор по теме (FR-EP-9) живёт без целевых слов, а «все слова оценены»
    на пустом списке — это пустая конъюнкция, то есть истина. Без этой
    оговорки тематический эпизод закрывался бы после первого же хода.
    """
    if user_asked:
        return True
    if turns_done >= MAX_TURNS:
        return True
    if target_word_ids and all_targets_assessed(target_word_ids, assessed):
        return True
    return model_wants_close


def unused_words(
    target_word_ids: Sequence[int], assessed: Mapping[int, Sequence[Verdict]]
) -> list[int]:
    """Целевые слова, которые так и не прозвучали (FR-EP-7)."""
    return [word_id for word_id in target_word_ids if not assessed.get(word_id)]
