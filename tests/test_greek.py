from __future__ import annotations

import pytest

from siga.core.greek import has_greek, normalize_lemma, split_article, strip_accents


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("νερό", True),
        ("вода", False),
        ("water", False),
        ("το νερό — вода", True),
        ("", False),
        ("123", False),
        ("ᾶ", True),  # политоническая диакритика
    ],
)
def test_has_greek(text: str, expected: bool) -> None:
    assert has_greek(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("καφές", "καφες"),
        ("νερό", "νερο"),
        ("άνθρωπος", "ανθρωπος"),
        ("ευχαριστώ", "ευχαριστω"),
        ("Ελλάδα", "Ελλαδα"),
        ("вода", "вода"),  # чужое письмо не трогаем
    ],
)
def test_strip_accents(text: str, expected: str) -> None:
    assert strip_accents(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("το νερό", ("το", "νερό")),
        ("ο καφές", ("ο", "καφές")),
        ("Η ζάχαρη", ("η", "ζάχαρη")),
        ("τα παιδιά", ("τα", "παιδιά")),
        ("νερό", (None, "νερό")),
        ("πίνω", (None, "πίνω")),
        ("το", (None, "το")),  # само по себе — слово, а не артикль при слове
    ],
)
def test_split_article(text: str, expected: tuple[str | None, str]) -> None:
    assert split_article(text) == expected


def test_normalize_lemma_collapses_writing_variants() -> None:
    variants = ["το νερό", "Το Νερό", "νερό", "ΝΕΡΟ", "  νερο  "]
    assert len({normalize_lemma(v) for v in variants}) == 1


def test_normalize_lemma_folds_final_sigma() -> None:
    # καφές и καφες- в составе формы должны считаться одним словом
    assert normalize_lemma("ο καφές") == normalize_lemma("καφες")


def test_normalize_lemma_keeps_different_words_apart() -> None:
    assert normalize_lemma("νερό") != normalize_lemma("νερά")
