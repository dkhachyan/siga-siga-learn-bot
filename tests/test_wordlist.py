from __future__ import annotations

import pytest

from siga.core.wordlist import RejectReason, parse_wordlist


def test_parses_the_canonical_list() -> None:
    result = parse_wordlist(
        """
        το νερό — вода
        ο καφές — кофе
        το γάλα — молоко
        """
    )
    assert [w.lemma for w in result.words] == ["το νερό", "ο καφές", "το γάλα"]
    assert [w.translation_ru for w in result.words] == ["вода", "кофе", "молоко"]
    assert not result.rejected
    assert not result.duplicates


@pytest.mark.parametrize(
    "line",
    [
        "το νερό — вода",
        "το νερό – вода",
        "το νερό - вода",
        "το νερό-вода",
        "το νερό: вода",
        "το νερό = вода",
        "το νερό\tвода",
        "το νερό  вода",
        "1. το νερό — вода",
        "12) το νερό — вода",
        "- το νερό — вода",
        "• το νερό — вода",
        "«το νερό» — вода",
    ],
)
def test_separators_and_list_markers(line: str) -> None:
    (word,) = parse_wordlist(line).words
    assert word.lemma == "το νερό"
    assert word.translation_ru == "вода"


def test_translation_may_come_first() -> None:
    """Ориентируемся на письменность, а не на порядок: люди пишут и так, и так."""
    (word,) = parse_wordlist("вода — το νερό").words
    assert word.lemma == "το νερό"
    assert word.translation_ru == "вода"


def test_word_without_translation_is_kept() -> None:
    """FR-IMP-3: перевода нет — сгенерируем при обогащении, строку не теряем."""
    (word,) = parse_wordlist("ευχαριστώ").words
    assert word.lemma == "ευχαριστώ"
    assert word.translation_ru is None


def test_multiword_translation_survives_intact() -> None:
    (word,) = parse_wordlist("ο φούρνος — пекарня, булочная").words
    assert word.translation_ru == "пекарня, булочная"


def test_translation_with_dash_splits_only_once() -> None:
    (word,) = parse_wordlist("το ψωμί — хлеб — свежий").words
    assert word.lemma == "το ψωμί"
    assert word.translation_ru == "хлеб — свежий"


def test_duplicates_within_batch_are_separated_not_dropped_silently() -> None:
    """FR-IMP-5: дубль убираем, но человеку показываем, что он был."""
    result = parse_wordlist(
        """
        το νερό — вода
        ο καφές — кофе
        ΝΕΡΟ — водичка
        """
    )
    assert [w.lemma for w in result.words] == ["το νερό", "ο καφές"]
    assert [w.lemma for w in result.duplicates] == ["ΝΕΡΟ"]


def test_line_without_greek_is_rejected_with_reason() -> None:
    result = parse_wordlist(
        """
        το νερό — вода
        просто заметка на полях
        """
    )
    assert [w.lemma for w in result.words] == ["το νερό"]
    (bad,) = result.rejected
    assert bad.reason is RejectReason.NO_GREEK
    assert bad.line_no == 3
    assert bad.raw == "просто заметка на полях"


def test_overlong_greek_run_is_rejected() -> None:
    result = parse_wordlist("το νερό και ο καφές και το γάλα και το ψωμί και η ζάχαρη")
    (bad,) = result.rejected
    assert bad.reason is RejectReason.TOO_LONG


def test_blank_lines_are_skipped_without_noise() -> None:
    result = parse_wordlist("\n\n  \nτο νερό — вода\n\n")
    assert len(result.words) == 1
    assert not result.rejected


def test_line_numbers_point_at_the_source() -> None:
    result = parse_wordlist("το νερό — вода\n\nмусор\nο καφές — кофе")
    assert [w.line_no for w in result.words] == [1, 4]
    assert result.rejected[0].line_no == 3


def test_empty_input() -> None:
    result = parse_wordlist("")
    assert result == parse_wordlist("   \n  \n")
    assert not result.words


def test_real_pack_of_ten() -> None:
    """Та самая пачка, которую бот получит первой."""
    result = parse_wordlist(
        """
        το νερό — вода
        ο καφές — кофе
        το γάλα — молоко
        το ψωμί — хлеб
        η ζάχαρη — сахар
        ο φούρνος — пекарня
        πίνω — пить
        τρώω — есть
        αγοράζω — покупать
        ζεστός — горячий
        """
    )
    assert len(result.words) == 10
    assert not result.rejected
    assert not result.duplicates
    assert all(w.translation_ru for w in result.words)
