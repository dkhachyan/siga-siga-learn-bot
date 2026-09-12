"""`/settings`: экран расписания целиком.

Тестов у экрана не было вовсе, и это стоило дорого: `callback_data` вида
`cfg:set:win:08:00-13:00` aiogram отказывался паковать — двоеточие у него
разделяет поля и внутри значения запрещено, — так что все три подэкрана падали
с `ValueError` при первом же нажатии. Триста пятьдесят зелёных тестов об этом
молчали, потому что ни один не рисовал эти клавиатуры.

Отсюда порядок: сначала проверка, что кнопки вообще собираются и читаются
обратно, и только потом — что выбор доезжает до базы.
"""

from __future__ import annotations

import datetime as dt

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from siga.bot import keyboards
from siga.core import schedule as clock
from siga.db.models import User
from tests.fake_telegram import FakeTelegram


def screens() -> list[InlineKeyboardMarkup]:
    """Все клавиатуры настроек. Пауза в двух видах — кнопка там разная."""
    return [
        keyboards.settings_menu(paused=False),
        keyboards.settings_menu(paused=True),
        keyboards.settings_frequency(
            clock.MIN_EPISODES_PER_DAY,
            low=clock.MIN_EPISODES_PER_DAY,
            high=clock.MAX_EPISODES_PER_DAY,
        ),
        keyboards.settings_window("10:00-20:00"),
        keyboards.settings_gap(clock.MIN_GAP_MINUTES),
        keyboards.settings_timezone("Asia/Nicosia"),
        keyboards.settings_level("A1"),
    ]


async def user_now(engine: AsyncEngine) -> User:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = await session.scalar(select(User))
    assert user is not None
    return user


# --- сборка кнопок ------------------------------------------------------------


def test_every_settings_button_survives_a_round_trip() -> None:
    """Каждая кнопка пакуется и распаковывается обратно.

    Сам вызов `screens()` — уже половина проверки: `pack()` бросает `ValueError`
    прямо при сборке клавиатуры, и экран не доходит до человека целиком.
    """
    for markup in screens():
        for row in markup.inline_keyboard:
            for button in row:
                assert button.callback_data is not None, f"кнопка «{button.text}» без данных"
                keyboards.SettingsAction.unpack(button.callback_data)


def test_no_setting_carries_a_colon_inside_its_value() -> None:
    """Двоеточие законно между полями и запрещено внутри значения.

    Проверка на будущее: подписать кнопку её же значением («08:00-13:00»,
    «12:00») — соблазн постоянный, а ломается это не здесь, а в переписке.
    """
    for markup in screens():
        for row in markup.inline_keyboard:
            for button in row:
                action = keyboards.SettingsAction.unpack(button.callback_data or "")
                assert ":" not in action.value, f"кнопка «{button.text}» не запакуется"


def test_window_presets_are_all_reachable() -> None:
    """Номер каждой кнопки указывает на разбираемое окно."""
    for index, (_, span) in enumerate(keyboards.WINDOW_PRESETS):
        assert keyboards.window_preset(str(index)) == span
        assert clock.parse_window(span) is not None, f"пресет «{span}» не разбирается"

    assert keyboards.window_preset(str(len(keyboards.WINDOW_PRESETS))) is None
    assert keyboards.window_preset("custom") is None


def test_gap_presets_are_all_reachable_and_allowed() -> None:
    """Пресет вне границ ядра дал бы отказ базы вместо настройки."""
    for index, (_, minutes) in enumerate(keyboards.GAP_PRESETS):
        assert keyboards.gap_preset(str(index)) == int(minutes)
        assert clock.MIN_GAP_MINUTES <= int(minutes) <= clock.MAX_GAP_MINUTES

    assert keyboards.gap_preset(str(len(keyboards.GAP_PRESETS))) is None
    assert keyboards.gap_preset("custom") is None


# --- выбор доезжает до базы ---------------------------------------------------


async def test_a_window_preset_reaches_the_database(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    await telegram.send("/settings")
    await telegram.press("🕘")
    await telegram.press("Вечер")

    user = await user_now(engine)
    assert (user.window_start, user.window_end) == (dt.time(15, 0), dt.time(22, 0))


async def test_a_frequency_button_reaches_the_database(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    await telegram.send("/settings")
    await telegram.press("🔢")
    await telegram.press("6")

    assert (await user_now(engine)).episodes_per_day == 6


async def test_a_timezone_button_reaches_the_database(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    await telegram.send("/settings")
    await telegram.press("🌍")
    await telegram.press("Греция")

    assert (await user_now(engine)).tz == "Europe/Athens"


async def test_a_custom_window_is_typed_in(telegram: FakeTelegram, engine: AsyncEngine) -> None:
    """«Своё окно» уводит в FSM, а не пытается разобрать слово «custom»."""
    await telegram.send("/settings")
    await telegram.press("🕘")
    await telegram.press("✏️")
    await telegram.send("07:30-11:45")

    user = await user_now(engine)
    assert (user.window_start, user.window_end) == (dt.time(7, 30), dt.time(11, 45))


async def test_a_frequency_beyond_the_buttons_is_typed_in(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    """Кнопок шесть, а разрешено двенадцать — остальное набирается руками."""
    await telegram.send("/settings")
    await telegram.press("🔢")
    await telegram.press("✏️")
    await telegram.send("10")

    assert (await user_now(engine)).episodes_per_day == 10


async def test_an_impossible_frequency_keeps_the_old_one(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    """«Столько не могу» — а не тихая подгонка к границе."""
    await telegram.send("/settings")
    await telegram.press("🔢")
    await telegram.press("✏️")
    answer = await telegram.send("0")

    assert (await user_now(engine)).episodes_per_day == 4
    assert "не могу" in answer[-1]


async def test_a_gap_button_reaches_the_database(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    await telegram.send("/settings")
    await telegram.press("⏱")
    await telegram.press("2 часа")

    assert (await user_now(engine)).min_gap_minutes == 120


async def test_a_level_button_reaches_the_database(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    """Уровень едет в промпты уже сейчас — править его надо не руками в базе."""
    await telegram.send("/settings")
    await telegram.press("🎓")
    await telegram.press("B1 — говорю по-своему")

    assert (await user_now(engine)).level == "B1"


async def test_the_current_level_is_marked_on_the_screen(telegram: FakeTelegram) -> None:
    """Помечен один уровень — иначе непонятно, что менять."""
    await telegram.send("/settings")
    await telegram.press("🎓")

    assert "Насколько сложным" in telegram.edits[-1]
    screen = telegram.last_keyboard
    assert screen is not None
    marked = [
        button.text
        for row in screen.inline_keyboard
        for button in row
        if button.text.startswith("· ")
    ]
    assert marked == ["· A1 — начинаю с нуля"]


async def test_the_screen_shows_the_level(telegram: FakeTelegram) -> None:
    answers = await telegram.send("/settings")

    assert "Уровень греческого" in answers[-1]


async def test_a_custom_gap_is_typed_in(telegram: FakeTelegram, engine: AsyncEngine) -> None:
    await telegram.send("/settings")
    await telegram.press("⏱")
    await telegram.press("✏️")
    await telegram.send("90 минут")

    assert (await user_now(engine)).min_gap_minutes == 90


async def test_the_screen_admits_when_the_two_settings_do_not_fit(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    """Шесть разговоров с промежутком в три часа в десятичасовое окно не лезут.

    Молча приходить реже — значит оставить человека гадать, почему у него
    выбрано шесть, а разговоров три.
    """
    await telegram.send("/settings")
    await telegram.press("🔢")
    await telegram.press("6")
    await telegram.press("⏱")
    await telegram.press("3 часа")

    assert (await user_now(engine)).episodes_per_day == 6
    assert "не помещается" in telegram.edits[-1]
    assert "3 разговора" in telegram.edits[-1]


async def test_a_narrow_window_is_refused_by_the_chosen_gap(
    telegram: FakeTelegram, engine: AsyncEngine
) -> None:
    """Окно меряется промежутком этого человека, а не общей константой."""
    await telegram.send("/settings")
    await telegram.press("⏱")
    await telegram.press("3 часа")
    await telegram.press("🕘")
    await telegram.press("✏️")
    answer = await telegram.send("09:00-11:00")

    user = await user_now(engine)
    assert (user.window_start, user.window_end) == (dt.time(10, 0), dt.time(20, 0))
    assert "3 часа" in answer[-1]


async def test_forged_values_change_nothing(telegram: FakeTelegram, engine: AsyncEngine) -> None:
    """Мусор в данных кнопки не меняет настройки и не роняет обработчик.

    Данные видны в клиенте и подставляются руками, а глобального
    error-хендлера у диспетчера нет: упавший обработчик — это молчащая кнопка.
    """
    await telegram.send("/settings")
    for data in (
        "cfg:set_freq:½",
        "cfg:set_window:99",
        "cfg:set_gap:99",
        "cfg:set_tz:Europe/Lisboa",
        "cfg:set_level:C1",
    ):
        await telegram.click(data)

    user = await user_now(engine)
    assert (user.episodes_per_day, user.tz, user.min_gap_minutes) == (4, "Asia/Nicosia", 45)
    assert (user.window_start, user.window_end) == (dt.time(10, 0), dt.time(20, 0))
    assert user.level == "A1"
