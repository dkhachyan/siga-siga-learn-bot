"""Правила эпизода: какой он и когда заканчивается.

Проверяется §5.3 и FR-EP-6 — то, что должно оставаться за нами, а не за
моделью. Вызовов LLM здесь нет: решение «закрывать» приходит от неё флажком,
и тесты как раз про то, что этот флажок не последнее слово.
"""

from __future__ import annotations

from siga.core import episodes
from siga.core.enums import EpisodeIntent, Verdict

WORDS = [1, 2, 3]
ALL_ASSESSED = {1: [Verdict.CORRECT], 2: [Verdict.ALMOST], 3: [Verdict.INCORRECT]}
NOTHING_ASSESSED: dict[int, list[Verdict]] = {}


def test_a_new_word_makes_it_an_introduction() -> None:
    assert episodes.intent_for([0, 3, 4]) is EpisodeIntent.D1


def test_familiar_words_ask_to_be_used() -> None:
    assert episodes.intent_for([2, 3]) is EpisodeIntent.D2


def test_the_weakest_word_leads() -> None:
    """Иначе разговор перескочит через то слово, ради которого затевался."""
    assert episodes.intent_for([5, 5, 1]) is EpisodeIntent.D1


def test_an_empty_set_is_treated_as_new() -> None:
    assert episodes.intent_for([]) is EpisodeIntent.D1


def test_no_more_than_three_words_per_episode() -> None:
    assert episodes.target_words_limit(10) == episodes.MAX_TARGET_WORDS
    assert episodes.target_words_limit(2) == 2


def test_all_words_assessed_closes_the_episode() -> None:
    assert episodes.should_close(
        turns_done=1,
        target_word_ids=WORDS,
        assessed=ALL_ASSESSED,
        model_wants_close=False,
    )


def test_the_turn_limit_closes_the_episode_even_mid_conversation() -> None:
    """Модель, увлёкшись, готова беседовать бесконечно — лимит наш, не её."""
    assert episodes.should_close(
        turns_done=episodes.MAX_TURNS,
        target_word_ids=WORDS,
        assessed=NOTHING_ASSESSED,
        model_wants_close=False,
    )


def test_the_user_asking_closes_it_immediately() -> None:
    assert episodes.should_close(
        turns_done=0,
        target_word_ids=WORDS,
        assessed=NOTHING_ASSESSED,
        model_wants_close=False,
        user_asked=True,
    )


def test_the_model_may_close_it_early() -> None:
    assert episodes.should_close(
        turns_done=1,
        target_word_ids=WORDS,
        assessed=NOTHING_ASSESSED,
        model_wants_close=True,
    )


def test_an_unfinished_episode_stays_open() -> None:
    assert not episodes.should_close(
        turns_done=1,
        target_word_ids=WORDS,
        assessed={1: [Verdict.CORRECT]},
        model_wants_close=False,
    )


def test_words_that_never_came_up_are_reported() -> None:
    assert episodes.unused_words(WORDS, {1: [Verdict.CORRECT]}) == [2, 3]


# --- разговор по теме (FR-EP-9) -----------------------------------------------


def test_a_topic_episode_survives_an_answer() -> None:
    """Пустая конъюнкция «все слова оценены» не должна закрывать разговор.

    У тематического эпизода целевых слов нет, и без этой оговорки он
    закрывался бы после первого же хода: `all([])` — это истина.
    """
    assert not episodes.should_close(
        turns_done=1,
        target_word_ids=[],
        assessed=NOTHING_ASSESSED,
        model_wants_close=False,
    )


def test_a_topic_episode_closes_for_the_same_reasons_as_usual() -> None:
    assert episodes.should_close(
        turns_done=episodes.MAX_TURNS,
        target_word_ids=[],
        assessed=NOTHING_ASSESSED,
        model_wants_close=False,
    ), "лимит ходов работает и без слов"
    assert episodes.should_close(
        turns_done=1,
        target_word_ids=[],
        assessed=NOTHING_ASSESSED,
        model_wants_close=True,
    ), "решение модели тоже"
    assert episodes.should_close(
        turns_done=1,
        target_word_ids=[],
        assessed=NOTHING_ASSESSED,
        model_wants_close=False,
        user_asked=True,
    ), "и /end"
