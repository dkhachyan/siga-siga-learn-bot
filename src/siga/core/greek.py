"""Работа с греческим письмом.

Нужна одна вещь: приводить слово к ключу, по которому считаются дубликаты.
«το Νερό», «νερό» и «ΝΕΡΟ» — одно и то же слово, и дважды в пачку попасть
не должны.
"""

from __future__ import annotations

import unicodedata

GREEK_RANGES = (
    (0x0370, 0x03FF),  # греческий и коптский
    (0x1F00, 0x1FFF),  # политоническая диакритика
)

#: Артикли именительного падежа. Пользователи пишут слова и с ними, и без.
ARTICLES = frozenset({"ο", "η", "το", "οι", "τα"})


def has_greek(text: str) -> bool:
    """Есть ли в строке хоть одна греческая буква."""
    return any(any(low <= ord(char) <= high for low, high in GREEK_RANGES) for char in text)


def strip_accents(text: str) -> str:
    """Убрать диакритику: καφές → καφες.

    Только для сравнения. Хранить и показывать нужно всегда с ударением —
    без него слово написано неправильно, а у части слов ударение ещё и
    различает значения (νόμος «закон» / νομός «ном»).
    """
    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", without_marks)


def split_article(text: str) -> tuple[str | None, str]:
    """Отделить артикль от слова: «το νερό» → («το», «νερό»).

    Если артикля нет, первый элемент None. Односложные слова, совпадающие с
    артиклем (само «το» как местоимение), не разбираем — иначе слово исчезнет.
    """
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in ARTICLES:
        return parts[0].lower(), parts[1].strip()
    return None, text.strip()


def normalize_lemma(text: str) -> str:
    """Ключ для сравнения слов между собой.

    Без артикля, без ударений, в нижнем регистре, с конечной сигмой,
    приведённой к обычной: «Ο Καφές» и «καφες» дают один ключ.
    """
    _, word = split_article(text.strip())
    key = strip_accents(word).lower().replace("ς", "σ")
    return " ".join(key.split())
