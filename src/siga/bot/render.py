"""Сборка текста сообщений из данных.

Чистые функции: на вход строки и числа, на выходе строки. Ни бота, ни базы —
чтобы вёрстку экрана подтверждения можно было проверить тестом, а не глазами
в Telegram.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from siga.core.enums import Gender, PartOfSpeech

#: Telegram режет сообщение на 4096 символах. Оставляем запас на заголовок и
#: на то, что HTML-теги считаются вместе с текстом.
MESSAGE_LIMIT = 3800

#: Части речи по-русски. Полными словами, а не «сущ.»: сокращения из бумажных
#: словарей экономили место на странице, в телефоне экономить нечего.
#: Ключи — члены `StrEnum`, но тип объявлен строковым: в базе лежат строки, и
#: искать по ним хочется без обратного превращения в перечисление.
POS_RU: dict[str, str] = {
    PartOfSpeech.NOUN: "существительное",
    PartOfSpeech.VERB: "глагол",
    PartOfSpeech.ADJECTIVE: "прилагательное",
    PartOfSpeech.ADVERB: "наречие",
    PartOfSpeech.PHRASE: "выражение",
}

GENDER_RU: dict[str, str] = {
    Gender.MASCULINE: "мужской род",
    Gender.FEMININE: "женский род",
    Gender.NEUTER: "средний род",
}

#: Родительный падеж: «от 8 сентября». Стандартный `%B` даёт именительный.
MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def pack_title(when: dt.date) -> str:
    """Название пачки по дате загрузки: «Пачка от 8 сентября»."""
    return f"Пачка от {when.day} {MONTHS_RU[when.month - 1]}"


@dataclass(frozen=True, slots=True)
class WordLine:
    """То, что нужно строке списка, — без модели SQLAlchemy."""

    lemma: str
    translation: str | None
    known: bool = False
    """Слово уже встречалось в прошлых пачках (FR-IMP-5)."""


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Русская форма числительного: 1 слово, 2 слова, 5 слов."""
    tail_100 = count % 100
    if 11 <= tail_100 <= 14:
        return many
    match count % 10:
        case 1:
            return one
        case 2 | 3 | 4:
            return few
        case _:
            return many


def words_form(count: int) -> str:
    return plural_ru(count, "слово", "слова", "слов")


def word_line(index: int, word: WordLine) -> str:
    """Одна строка нумерованного списка."""
    translation = word.translation or "<i>переведу сам</i>"
    mark = " ↩︎" if word.known else ""
    return f"{index}. <b>{word.lemma}</b> — {translation}{mark}"


def _chunk(lines: Iterable[str], *, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Склеить строки в сообщения, не переступая лимит Telegram."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + 1
        if current and length + addition > limit:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def pack_screen(title: str, words: Sequence[WordLine]) -> list[str]:
    """Экран подтверждения (FR-IMP-8): заголовок и нумерованный список.

    Возвращает список сообщений: сотня слов в одно сообщение Telegram не влезает.
    Кнопки вешаются на последнее.
    """
    header = f"<b>{title}</b> — {len(words)} {words_form(len(words))}"
    lines = [word_line(i, word) for i, word in enumerate(words, start=1)]

    known_count = sum(1 for word in words if word.known)
    footer = []
    if known_count:
        footer = [
            "",
            f"↩︎ — {known_count} {words_form(known_count)} из прошлых пачек. "
            "Оставишь — будем повторять, не нужны — удали.",
        ]

    chunks = _chunk([header, "", *lines, *footer])
    return chunks


def days_left_form(days: int) -> str:
    return plural_ru(days, "день", "дня", "дней")


def active_pack_screen(
    title: str, words: Sequence[WordLine], *, days_left: int | None
) -> list[str]:
    """Экран активной пачки: тот же список, но без правки и со сроком.

    Прогресс по словам тут пока не показываем — до эпизодов (этап 2) он у всех
    нулевой, а строка «0 из 0 употреблений» под каждым словом только мешает.
    """
    header = f"<b>{title}</b> — {len(words)} {words_form(len(words))}, в работе"
    lines = [word_line(i, word) for i, word in enumerate(words, start=1)]

    footer = []
    if days_left is not None:
        footer = ["", _deadline(days_left)]

    return _chunk([header, "", *lines, *footer])


def _deadline(days_left: int) -> str:
    if days_left > 0:
        return f"До конца периода — {days_left} {days_left_form(days_left)}."
    return "Период закончился."


@dataclass(frozen=True, slots=True)
class Card:
    """Одно слово со всей грамматикой — то, что показывает карточка.

    Поля повторяют `db.models.Word`, но модель сюда не пускаем: вёрстку
    карточки надо проверять тестом без базы, а «непонятно, что выведется,
    если род есть, а части речи нет» — это ровно тот случай, где тест дешевле
    ручной проверки в Telegram.
    """

    lemma: str
    """С ударением — то, как слово выглядит для человека."""
    translation: str | None
    translation_model: str | None = None
    pos: str | None = None
    article: str | None = None
    gender: str | None = None
    verb_form: str | None = None
    examples: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    enriched: bool = False
    """`False` — обогащение не дошло: показываем слово, но честно говорим об этом."""


def _headword(card: Card) -> str:
    """Слово в шапке карточки — у существительных с артиклем.

    Артикль обычно уже внутри `lemma` (модель просят его дописать), поэтому
    приклеиваем только если его там нет: иначе выйдет «το το νερό».
    """
    if card.article and not card.lemma.lower().startswith(f"{card.article.lower()} "):
        return f"{card.article} {card.lemma}"
    return card.lemma


def _grammar_line(card: Card) -> str | None:
    """«существительное, средний род» — или None, если сказать нечего."""
    parts = []
    # `.get`, а не `[...]`: у `other` русского названия нет намеренно — про
    # предлог сказать «предлог» человеку нечего, а падать тут не за что.
    if card.pos and (label := POS_RU.get(card.pos)):
        parts.append(label)
    if card.gender and (label := GENDER_RU.get(card.gender)):
        parts.append(label)
    # Форму 1 л. ед. ч. показываем только если она отличается от леммы: у
    # большинства глаголов это одно и то же слово, и повторять его — шум.
    if card.pos == PartOfSpeech.VERB and card.verb_form and card.verb_form != card.lemma:
        parts.append(f"1 л. ед. ч. — {card.verb_form}")
    return ", ".join(parts) or None


def word_card(card: Card, *, position: int, total: int, title: str) -> str:
    """Карточка слова: шапка, перевод, грамматика, примеры.

    `position` — номер от единицы, как в списке пачки: человек листает и
    должен понимать, где находится, без отдельной кнопки-счётчика.
    """
    lines = [f"{title} · {position} из {total}", "", f"<b>{_headword(card)}</b>"]

    if card.translation:
        lines.append(card.translation)
    if card.translation_model and card.translation_model != card.translation:
        # Свой перевод человека не трогаем (FR-IMP-4), но словарный показываем
        # рядом: расхождение — это чаще всего интересный оттенок значения.
        lines.append(f"<i>в словаре: {card.translation_model}</i>")

    if grammar := _grammar_line(card):
        lines += ["", grammar]

    for example in card.examples:
        greek, russian = example.get("el"), example.get("ru")
        if not greek:
            continue
        lines += ["", f"<i>{greek}</i>"]
        if russian:
            lines.append(russian)

    if not card.enriched:
        lines += ["", "<i>Грамматику для этого слова я ещё не собрал.</i>"]

    return "\n".join(lines)
