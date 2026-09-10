"""Сборка текста сообщений из данных.

Чистые функции: на вход строки и числа, на выходе строки. Ни бота, ни базы —
чтобы вёрстку экрана подтверждения можно было проверить тестом, а не глазами
в Telegram.
"""

from __future__ import annotations

import datetime as dt
import html
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from siga.core.enums import ErrorType, Gender, PartOfSpeech, Verdict
from siga.core.memory import Profile

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


#: Итог слова за эпизод: значок и подпись. Ключи — `Verdict`, но тип строковый:
#: оценки приезжают из JSON модели через базу, где лежат строками.
VERDICT_RU: dict[str, tuple[str, str]] = {
    Verdict.CORRECT: ("✅", "верно"),
    Verdict.ALMOST: ("≈", "почти — подвела форма"),
    Verdict.INCORRECT: ("✗", "мимо"),
}

#: Чем ошибся человек — по-русски, для `🔍 Разбор`.
#: `other` пропущен нарочно: «прочее» в скобках не объясняет ничего.
ERROR_TYPE_RU: dict[str, str] = {
    ErrorType.CASE: "падеж",
    ErrorType.GENDER: "род",
    ErrorType.ARTICLE: "артикль",
    ErrorType.VERB_FORM: "форма глагола",
    ErrorType.ACCENT: "ударение",
    ErrorType.SPELLING: "орфография",
    ErrorType.WRONG_WORD: "не то слово",
    ErrorType.LATIN: "латиница",
}

NOTHING_TO_ANALYSE = "Разбирать нечего — ошибок не было."


def plain(text: str) -> str:
    """Чужой текст внутрь HTML-сообщения.

    Реплики Ника и ответы человека разметки не несут, но угловая скобка в них
    случиться может — и тогда Telegram отвергнет сообщение целиком. Экранируем
    на входе, а не надеемся, что не встретится.
    """
    return html.escape(text, quote=False)


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
        footer = ["", deadline_line(days_left)]

    return _chunk([header, "", *lines, *footer])


def deadline_line(days_left: int | None) -> str:
    """Строка про срок пачки. Пустая, если срока нет: молчание честнее выдумки."""
    if days_left is None:
        return ""
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


@dataclass(frozen=True, slots=True)
class WordOutcome:
    """Итог одного целевого слова за эпизод — `srs.Outcome` плюс само слово."""

    lemma: str
    verdict: str | None = None
    used: bool = False


def closing_block(outcomes: Sequence[WordOutcome]) -> str:
    """Блок «Как прошло» под прощальной репликой (FR-EP-8).

    Отдельным сообщением от реплики Ника, а не в ней: итог — это интерфейс
    бота, а не слова персонажа (§6). Не прозвучавшее слово получает не крестик,
    а точку: молчание не ошибка (FR-EP-7), и человеку это надо сказать прямо.
    """
    lines = ["<b>Как прошло</b>", ""]
    for item in outcomes:
        if not item.used or item.verdict is None:
            lines.append(f"· <b>{item.lemma}</b> — не прозвучало, вернётся")
            continue
        mark, label = VERDICT_RU.get(item.verdict, ("·", "без оценки"))
        lines.append(f"{mark} <b>{item.lemma}</b> — {label}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class AnalysisTurn:
    """Один ход глазами разбора: что человек сказал и что с этим не так."""

    user_text: str | None = None
    analysis_ru: str | None = None
    corrections: Sequence[Mapping[str, Any]] = ()


def correction_line(item: Mapping[str, Any], lemmas: Mapping[int, str]) -> str | None:
    """«νερώ → νερό (падеж)». None — записи нечего показать.

    Исправления пишет модель, и полей в них может не быть: без правильной
    формы строка бессмысленна, а без ошибочной подставляем словарное слово —
    человек всё равно узнает, о чём речь.
    """
    correct_form = item.get("correct_form")
    if not correct_form:
        return None

    word_id = item.get("word_id")
    was = item.get("user_form") or (lemmas.get(word_id) if isinstance(word_id, int) else None)
    head = f"{plain(str(was))} → {plain(str(correct_form))}" if was else plain(str(correct_form))

    label = ERROR_TYPE_RU.get(str(item.get("error_type") or ""))
    return f"• {head} ({label})" if label else f"• {head}"


def analysis_screen(turns: Sequence[AnalysisTurn], *, lemmas: Mapping[int, str]) -> str:
    """Разбор по всему эпизоду (FR-CHK-2).

    По ходам, а не одним списком ошибок: человек вспоминает свою фразу и
    видит, что в ней было не так. Ход без замечаний пропускаем — строка
    «тут всё хорошо» в разборе только удлиняет экран.
    """
    blocks: list[str] = []
    for turn in turns:
        details = [line for item in turn.corrections if (line := correction_line(item, lemmas))]
        if turn.analysis_ru:
            details.append(plain(turn.analysis_ru))
        if not details:
            continue
        head = [f"<i>{plain(turn.user_text)}</i>"] if turn.user_text else []
        blocks.append("\n".join([*head, *details]))

    if not blocks:
        return NOTHING_TO_ANALYSE
    return "\n\n".join(["<b>Разбор</b>", *blocks])


def memory_screen(profile: Profile) -> str:
    """Профиль так, как его читает человек (FR-MEM-4).

    Показывается всё, что уходит в промпт, — иначе экран перестаёт быть
    ответом на вопрос «что он про меня знает». Факты пронумерованы: номер —
    это адрес кнопки, которой факт удаляют.
    """
    blocks: list[str] = ["<b>Что я о тебе помню</b>"]

    if profile.facts:
        lines = [f"{number}. {plain(fact)}" for number, fact in enumerate(profile.facts, start=1)]
        blocks.append("\n".join(["<b>Про тебя</b>", *lines]))

    if profile.recurring_errors:
        lines = [f"· {plain(item)}" for item in profile.recurring_errors]
        blocks.append("\n".join(["<b>Что даётся тяжело</b>", *lines]))

    if profile.recent_topics:
        topics = ", ".join(plain(topic) for topic in profile.recent_topics)
        blocks.append(f"<b>О чём говорили</b>\n{topics}")

    if profile.tone_notes:
        blocks.append(f"<b>Как с тобой говорить</b>\n{plain(profile.tone_notes)}")

    return "\n\n".join(blocks)


def episodes_form(count: int) -> str:
    return plural_ru(count, "разговор", "разговора", "разговоров")


def gap_form(minutes: int) -> str:
    """Промежуток по-человечески: «45 минут», «1 час», «1 час 30 минут»."""
    hours, rest = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')}")
    if rest or not hours:
        parts.append(f"{rest} {plural_ru(rest, 'минута', 'минуты', 'минут')}")
    return " ".join(parts)


def settings_screen(
    *,
    episodes_per_day: int,
    min_gap_minutes: int,
    window_start: dt.time,
    window_end: dt.time,
    tz: str,
    paused: bool,
    fits_per_day: int,
) -> str:
    """Экран расписания: что настроено сейчас (FR-SCH-1, FR-SCH-7).

    Одним экраном, а не четырьмя отдельными командами: настройки связаны —
    шесть разговоров в двухчасовом окне не поместятся, — и человек должен
    видеть их рядом, чтобы понимать, почему разговоров приходит меньше, чем он
    просил.

    `fits_per_day` считает хендлер, а не этот модуль: раскладка дня — дело
    планировщика, и повторять её арифметику в шаблоне значит однажды разойтись
    с ней в ответе.
    """
    span = f"{window_start:%H:%M}–{window_end:%H:%M}"
    lines = [
        "<b>Расписание</b>",
        "",
        f"Разговоров в день: <b>{episodes_per_day}</b>",
        f"Когда писать: <b>{span}</b>",
        f"Между разговорами: <b>не меньше {gap_form(min_gap_minutes)}</b>",
        f"Часовой пояс: <b>{plain(tz)}</b>",
    ]
    if fits_per_day < episodes_per_day:
        lines += [
            "",
            f"Столько в окно не помещается — приду "
            f"<b>{fits_per_day} {episodes_form(fits_per_day)}</b>. "
            f"Раздвинь окно или сократи промежуток.",
        ]
    if paused:
        lines += ["", "Сейчас на паузе — я не пишу первым. Период пачки при этом стоит."]
    return "\n".join(lines)
