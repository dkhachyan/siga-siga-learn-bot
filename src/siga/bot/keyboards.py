"""Инлайн-клавиатуры и данные их кнопок.

`CallbackData` вместо строк руками: aiogram сам собирает и разбирает payload,
и опечатка в имени поля становится ошибкой типов, а не молчащей кнопкой.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

#: Сколько кнопок-номеров в ряду на экранах правки и удаления.
NUMBERS_PER_ROW = 5


class PackAction(CallbackData, prefix="pack"):
    """Кнопки работы с пачкой — экрана подтверждения и замены активной."""

    action: Literal["confirm", "edit", "delete", "add", "back", "replace", "keep"]
    """`replace` — убрать активную пачку в архив и начать новую, `keep` — отказаться."""


class WordAction(CallbackData, prefix="word"):
    """Выбор конкретного слова номером."""

    action: Literal["edit", "delete"]
    word_id: int


class CardAction(CallbackData, prefix="card"):
    """Листание карточек. `index` — номер слова с нуля в порядке пачки.

    Номер, а не `word_id`: кнопка «дальше» должна знать, куда идти, а не что
    показать, и при удалении слова из пачки список просто сдвигается — вместо
    кнопки, ведущей в никуда.
    """

    action: Literal["open", "show", "list"]
    """`open` — первая карточка новым сообщением, `show` — листание на месте.

    Разделены нарочно: список пачки может не влезть в одно сообщение, и тогда
    «вернуться к списку» — это несколько новых сообщений, а не правка одного.
    Листать же удобно на месте, не заваливая переписку.
    """

    index: int = 0


class EpisodeAction(CallbackData, prefix="ep"):
    """Кнопки под репликой Ника."""

    action: Literal["analysis", "dunno"]
    """`dunno` — «Не знаю»: ход всё равно засчитывается, слово подскажет Ник.

    Кнопки с ним больше не рисуются — её место заняла `💡 Что ответить`
    (FR-CHK-8), — но значение остаётся, и хендлер вместе с ним: кнопки живут в
    переписке вечно, и убрать значение из `Literal` значит молча обездвижить
    их во всей истории разговоров."""

    episode_id: int = 0
    """0 — «тот разговор, что идёт сейчас».

    Номер проставляется только под закрытым эпизодом: кнопка `🔍 Разбор` живёт
    в переписке вечно, и нажатая через неделю она должна показать тот разговор,
    под которым стоит, а не текущий.
    """


class TranslateAction(CallbackData, prefix="tr"):
    """Кнопка `🇷🇺 Перевод` — адрес одной реплики Ника.

    Отдельный префикс, а не четвёртое поле в `EpisodeAction`: у кнопок в
    переписке payload зашит навсегда, и добавь мы поле — все `ep:analysis:0`
    из прошлых разговоров перестали бы распаковываться. Обработчика на все
    нажатия подряд нет, так что кнопка просто крутилась бы без ответа.
    """

    episode_id: int
    turn_idx: int
    """Номер хода внутри эпизода, а не `id` строки: клавиатура прицепляется к
    сообщению до отправки, а ход пишется после успешной отправки (NFR-1) —
    `id` в этот момент ещё неизвестен, а номер известен."""


class HintAction(CallbackData, prefix="hi"):
    """Кнопка `💡 Что ответить` — адрес вопроса, на который нужна подсказка.

    Свой префикс по тому же доводу, что у `TranslateAction`: поле в
    существующий класс добавлять нельзя, старые payload'ы перестанут
    распаковываться.
    """

    episode_id: int
    turn_idx: int


#: Номер хода с вводной репликой. `episodes_db.open_` всегда пишет нулевой ход,
#: так что для кнопки под приглашением к разговору хватает константы.
OPENING_TURN_IDX = 0


class MemoryAction(CallbackData, prefix="mem"):
    """Кнопки экрана «что Ник обо мне помнит» (FR-MEM-4)."""

    action: Literal["forget", "clear", "confirm", "cancel"]
    """`clear` — спросить, `confirm` — стереть. Забвение необратимо, и одного
    случайного нажатия для него мало."""

    index: int = 0
    """Номер факта с нуля — для `forget`."""


def memory_screen(facts: int) -> InlineKeyboardMarkup:
    """Номера фактов и кнопка «стереть всё» под профилем.

    Номерами, как в пачке: сам факт на кнопке не помещается, а номер совпадает
    с тем, что человек только что прочитал в списке.
    """
    builder = InlineKeyboardBuilder()
    for index in range(facts):
        builder.button(
            text=str(index + 1), callback_data=MemoryAction(action="forget", index=index)
        )
    if facts:
        builder.adjust(*([NUMBERS_PER_ROW] * (facts // NUMBERS_PER_ROW + 1)))
    forget_all = MemoryAction(action="clear").pack()
    builder.row(InlineKeyboardButton(text="🧹 Забыть всё", callback_data=forget_all))
    return builder.as_markup()


def confirm_forget_all() -> InlineKeyboardMarkup:
    """Переспрос перед очисткой профиля."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Да, забыть всё", callback_data=MemoryAction(action="confirm"))
    builder.button(text="↩︎ Оставить", callback_data=MemoryAction(action="cancel"))
    builder.adjust(1)
    return builder.as_markup()


def reply_actions(
    *, episode_id: int | None = None, turn_idx: int | None = None
) -> InlineKeyboardMarkup:
    """Кнопки под репликой — минимум (§6).

    Кнопки «закончить» нет намеренно: `/end` командой не мозолит глаза в каждом
    сообщении, а клавиатура на весь экран убивает ощущение переписки. По тому
    же доводу их ровно три: `💡 Что ответить` не добавилась четвёртой, а заняла
    место `🤷 Не знаю` (FR-CHK-8).

    `💡` и `🇷🇺` появляются только когда сказано, к какой реплике они относятся.
    Без пары адрес неизвестен — так вызывают из-под сообщений самого бота,
    которые и переводить незачем, и подсказывать к ним нечего.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Разбор", callback_data=EpisodeAction(action="analysis"))
    if episode_id is not None and turn_idx is not None:
        builder.button(
            text="💡 Что ответить",
            callback_data=HintAction(episode_id=episode_id, turn_idx=turn_idx),
        )
        builder.button(
            text="🇷🇺 Перевод",
            callback_data=TranslateAction(episode_id=episode_id, turn_idx=turn_idx),
        )
        # Отдельным рядом: три подписи в одну строку Telegram сжимает до
        # нечитаемых огрызков на узком экране.
        builder.adjust(2, 1)
    else:
        builder.adjust(1)
    return builder.as_markup()


def after_episode(episode_id: int) -> InlineKeyboardMarkup:
    """Под блоком «Как прошло» — только разбор (FR-EP-8)."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔍 Разбор",
        callback_data=EpisodeAction(action="analysis", episode_id=episode_id),
    )
    return builder.as_markup()


def active_pack_screen() -> InlineKeyboardMarkup:
    """Кнопки под списком активной пачки. Правки тут нет: пачка уже в работе."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🃏 Карточки", callback_data=CardAction(action="open", index=0))
    return builder.as_markup()


def replace_pack() -> InlineKeyboardMarkup:
    """Выбор при `/add`, когда пачка уже в работе.

    Кнопками, а не командой: «убрать в архив» — необратимое для человека
    действие, и оно должно стоить одного осознанного нажатия, а не случайно
    повторённого `/add`.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 В архив, начать новую", callback_data=PackAction(action="replace"))
    builder.button(text="↩︎ Оставить текущую", callback_data=PackAction(action="keep"))
    builder.adjust(1)
    return builder.as_markup()


def card_nav(index: int, total: int) -> InlineKeyboardMarkup:
    """Стрелки листания и возврат к списку.

    Листание кольцевое: с последней карточки «дальше» ведёт на первую. Так
    не нужна отдельная неактивная кнопка на краях — а тупиковая стрелка,
    которая просто ничего не делает, читается как поломка.
    """
    builder = InlineKeyboardBuilder()
    if total > 1:
        builder.button(text="←", callback_data=CardAction(action="show", index=(index - 1) % total))
        builder.button(text="→", callback_data=CardAction(action="show", index=(index + 1) % total))
        builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="☰ Списком", callback_data=CardAction(action="list").pack())
    )
    return builder.as_markup()


def pack_screen() -> InlineKeyboardMarkup:
    """Обязательный набор кнопок экрана подтверждения, FR-IMP-8."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Всё верно", callback_data=PackAction(action="confirm"))
    builder.button(text="✏️ Править", callback_data=PackAction(action="edit"))
    builder.button(text="🗑 Удалить", callback_data=PackAction(action="delete"))
    builder.button(text="➕ Добавить", callback_data=PackAction(action="add"))
    builder.adjust(1, 3)
    return builder.as_markup()


def word_numbers(
    word_ids: Sequence[int], action: Literal["edit", "delete"]
) -> InlineKeyboardMarkup:
    """Кнопки-номера по числу слов в пачке плюс возврат.

    Номер, а не само слово: греческое слово на кнопке не помещается, а номер
    совпадает с тем, что человек видит в списке.
    """
    builder = InlineKeyboardBuilder()
    for index, word_id in enumerate(word_ids, start=1):
        builder.button(text=str(index), callback_data=WordAction(action=action, word_id=word_id))
    builder.adjust(*([NUMBERS_PER_ROW] * (len(word_ids) // NUMBERS_PER_ROW + 1)))
    builder.row(
        InlineKeyboardButton(text="← Назад", callback_data=PackAction(action="back").pack())
    )
    return builder.as_markup()


class SettingsAction(CallbackData, prefix="cfg"):
    """Кнопки экрана настроек расписания (FR-SCH-1, FR-SCH-7).

    `value` — строка, потому что за одним и тем же экраном стоят и число
    эпизодов, и номер окна, и имя часового пояса. Разбирает её тот
    хендлер, который эту кнопку и нарисовал.

    Что именно правим, сказано отдельным действием (`set_freq`, `set_window`,
    `set_tz`), а не приклеено к `value` через двоеточие: двоеточием aiogram
    разделяет поля и внутри значения запрещает — `pack()` бросает `ValueError`,
    и экран не рисуется вовсе. По той же причине окно едет номером кнопки, а не
    часами: в «08:00-13:00» двоеточий больше, чем цифр между ними.
    """

    action: Literal[
        "menu",
        "freq",
        "window",
        "gap",
        "tz",
        "set_freq",
        "set_window",
        "set_gap",
        "set_tz",
        "pause",
        "resume",
    ]
    value: str = ""


#: Докуда рисуем частоту кнопками. Дальше — «своё число»: двенадцать кнопок в
#: ряд Telegram сжимает до нечитаемых огрызков, а просят столько редко.
FREQUENCY_QUICK_MAX = 6

#: Готовые промежутки между разговорами. Значение — минуты: двоеточий не
#: содержит, а значит пакуется (см. `SettingsAction`).
GAP_PRESETS: tuple[tuple[str, str], ...] = (
    ("30 минут", "30"),
    ("45 минут", "45"),
    ("1 час", "60"),
    ("2 часа", "120"),
    ("3 часа", "180"),
)

#: Готовые окна. Человеку проще ткнуть в «вечер», чем печатать часы; на случай,
#: когда ни одно не подходит, рядом стоит «своё».
WINDOW_PRESETS: tuple[tuple[str, str], ...] = (
    ("Утро", "08:00-13:00"),
    ("День", "10:00-20:00"),
    ("Вечер", "15:00-22:00"),
    ("Весь день", "08:00-22:00"),
)

#: Часовые пояса, в которых живут те, кто учит греческий. Список короткий
#: нарочно: полный перечень IANA — это не экран, а справочник.
TZ_PRESETS: tuple[tuple[str, str], ...] = (
    ("Кипр", "Asia/Nicosia"),
    ("Греция", "Europe/Athens"),
    ("Москва", "Europe/Moscow"),
    ("Тбилиси", "Asia/Tbilisi"),
    ("Ереван", "Asia/Yerevan"),
    ("Белград", "Europe/Belgrade"),
)


def settings_menu(*, paused: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔢 Сколько раз в день", callback_data=SettingsAction(action="freq"))
    builder.button(text="🕘 Когда писать", callback_data=SettingsAction(action="window"))
    builder.button(
        text="⏱ Промежуток между разговорами", callback_data=SettingsAction(action="gap")
    )
    builder.button(text="🌍 Часовой пояс", callback_data=SettingsAction(action="tz"))
    if paused:
        builder.button(text="▶️ Продолжить", callback_data=SettingsAction(action="resume"))
    else:
        builder.button(text="⏸ Пауза", callback_data=SettingsAction(action="pause"))
    builder.adjust(1)
    return builder.as_markup()


def settings_frequency(current: int, *, low: int, high: int) -> InlineKeyboardMarkup:
    """Числа кнопками. Текущее помечено — иначе непонятно, что менять.

    Кнопок ровно `FREQUENCY_QUICK_MAX`, хотя разрешено больше: остальное
    набирается руками. Границы приходят снаружи, из ядра, — здесь только то,
    сколько из них помещается на экран.
    """
    builder = InlineKeyboardBuilder()
    quick = min(high, FREQUENCY_QUICK_MAX)
    for count in range(low, quick + 1):
        mark = "· " if count == current else ""
        builder.button(
            text=f"{mark}{count}",
            callback_data=SettingsAction(action="set_freq", value=str(count)),
        )
    builder.adjust(quick - low + 1)
    builder.row(
        InlineKeyboardButton(
            text=f"✏️ Своё число (до {high})",
            callback_data=SettingsAction(action="set_freq", value="custom").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(text="← Назад", callback_data=SettingsAction(action="menu").pack())
    )
    return builder.as_markup()


def gap_preset(value: str) -> int | None:
    """Минуты по номеру кнопки. `None` — номера такого нет.

    Как и у окна: данные кнопки видны в клиенте и подставляются руками.
    """
    if not value.isdecimal():
        return None
    index = int(value)
    if index >= len(GAP_PRESETS):
        return None
    return int(GAP_PRESETS[index][1])


def settings_gap(current: int) -> InlineKeyboardMarkup:
    """Промежуток между разговорами (FR-SCH-8)."""
    builder = InlineKeyboardBuilder()
    for index, (title, minutes) in enumerate(GAP_PRESETS):
        mark = "· " if int(minutes) == current else ""
        builder.button(
            text=f"{mark}{title}",
            callback_data=SettingsAction(action="set_gap", value=str(index)),
        )
    builder.button(text="✏️ Своё", callback_data=SettingsAction(action="set_gap", value="custom"))
    builder.adjust(3)
    builder.row(
        InlineKeyboardButton(text="← Назад", callback_data=SettingsAction(action="menu").pack())
    )
    return builder.as_markup()


def window_preset(value: str) -> str | None:
    """Часы окна по номеру кнопки. `None` — номера такого нет.

    Обратная сторона `settings_window`: данные кнопки видны в клиенте и
    подставляются руками, так что номер приезжает какой угодно.
    """
    if not value.isdecimal():
        return None
    index = int(value)
    if index >= len(WINDOW_PRESETS):
        return None
    return WINDOW_PRESETS[index][1]


def settings_window(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, (title, span) in enumerate(WINDOW_PRESETS):
        mark = "· " if span == current else ""
        builder.button(
            text=f"{mark}{title} {span}",
            callback_data=SettingsAction(action="set_window", value=str(index)),
        )
    builder.button(
        text="✏️ Своё окно", callback_data=SettingsAction(action="set_window", value="custom")
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="← Назад", callback_data=SettingsAction(action="menu").pack())
    )
    return builder.as_markup()


def settings_timezone(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for title, name in TZ_PRESETS:
        mark = "· " if name == current else ""
        builder.button(
            text=f"{mark}{title}", callback_data=SettingsAction(action="set_tz", value=name)
        )
    builder.button(text="✏️ Другой", callback_data=SettingsAction(action="set_tz", value="custom"))
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="← Назад", callback_data=SettingsAction(action="menu").pack())
    )
    return builder.as_markup()
