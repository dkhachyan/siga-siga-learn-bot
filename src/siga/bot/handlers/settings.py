"""Расписание глазами человека: `/settings`, `/pause`, `/resume` (US-4).

Экран один, потому что настройки связаны между собой: частота, окно и часовой
пояс вместе определяют, когда придёт разговор, и менять их вслепую по одной —
значит каждый раз гадать, что получится.

Любая правка пересобирает остаток сегодняшнего дня. Оставить старые слоты было
бы дешевле — план на день собран ночью, и переделка стоит одного вызова `R3`, —
но человек, который только что подвинул окно, ждёт нового расписания сегодня, а
не завтра.
"""

from __future__ import annotations

import datetime as dt
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from siga import dialog
from siga.bot import keyboards, render, texts
from siga.core import schedule as clock
from siga.db import users
from siga.db.models import User
from siga.llm.base import LlmClient, LlmError

log = logging.getLogger(__name__)

router = Router(name="settings")


class SettingsFlow(StatesGroup):
    """Шаги, на которых человек печатает значение руками.

    Только они: кнопкам состояние не нужно — всё, что кнопка меняет, лежит
    у неё же в `callback_data`.
    """

    frequency = State()
    window = State()
    gap = State()
    timezone = State()


async def _current_user(session: AsyncSession, tg_user_id: int) -> User:
    user, _ = await users.get_or_create(session, tg_user_id=tg_user_id)
    return user


def _screen(user: User) -> str:
    wanted = clock.clamp_episodes_per_day(user.episodes_per_day)
    gap = clock.gap_of(user.min_gap_minutes)
    return render.settings_screen(
        episodes_per_day=wanted,
        min_gap_minutes=int(gap.total_seconds() // 60),
        window_start=user.window_start,
        window_end=user.window_end,
        tz=user.tz,
        paused=user.paused_at is not None,
        # Считаем той же функцией, что и планировщик: разойдись они, экран
        # обещал бы одно, а разговоров приходило бы другое.
        fits_per_day=clock.fits_in_window(
            clock.window_minutes(user.window_start, user.window_end), wanted, gap=gap
        ),
    )


def _menu(user: User) -> InlineKeyboardMarkup:
    return keyboards.settings_menu(paused=user.paused_at is not None)


async def _show(message: Message, user: User) -> None:
    await message.answer(_screen(user), reply_markup=_menu(user))


async def _replan(session: AsyncSession, llm: LlmClient, *, user: User, now: dt.datetime) -> None:
    """Переставить сегодняшние разговоры под новые настройки.

    Ошибка модели тут не беда: настройки уже сохранены, а день пересоберётся
    ночью сам. Поэтому ругаться человеку не на что — молча пишем в журнал.
    """
    await dialog.clear_day(session, user_id=user.id, now=now)
    if user.paused_at is not None:
        return

    tz = clock.zone(user.tz)
    try:
        await dialog.plan_day(session, llm, user=user, day=clock.local_day(now, tz=tz), now=now)
    except LlmError as error:
        log.warning("не удалось пересобрать день для %s: %s", user.id, error)


# --- экран --------------------------------------------------------------------


@router.message(Command("settings"))
async def handle_settings(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    user = await _current_user(session, message.from_user.id)
    await _show(message, user)


@router.callback_query(keyboards.SettingsAction.filter(F.action == "menu"))
async def handle_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user = await _current_user(session, callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(_screen(user), reply_markup=_menu(user))


@router.callback_query(keyboards.SettingsAction.filter(F.action == "freq"))
async def handle_frequency(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user = await _current_user(session, callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(
        texts.SETTINGS_FREQUENCY.format(
            gap=render.gap_form(clock.clamp_min_gap(user.min_gap_minutes))
        ),
        reply_markup=keyboards.settings_frequency(
            clock.clamp_episodes_per_day(user.episodes_per_day),
            low=clock.MIN_EPISODES_PER_DAY,
            high=clock.MAX_EPISODES_PER_DAY,
        ),
    )


@router.callback_query(keyboards.SettingsAction.filter(F.action == "window"))
async def handle_window(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user = await _current_user(session, callback.from_user.id)
    current = f"{user.window_start:%H:%M}-{user.window_end:%H:%M}"
    await callback.answer()
    await callback.message.edit_text(
        texts.SETTINGS_WINDOW, reply_markup=keyboards.settings_window(current)
    )


@router.callback_query(keyboards.SettingsAction.filter(F.action == "gap"))
async def handle_gap(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user = await _current_user(session, callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(
        texts.SETTINGS_GAP,
        reply_markup=keyboards.settings_gap(clock.clamp_min_gap(user.min_gap_minutes)),
    )


@router.callback_query(keyboards.SettingsAction.filter(F.action == "tz"))
async def handle_timezone(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user = await _current_user(session, callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(
        texts.SETTINGS_TIMEZONE, reply_markup=keyboards.settings_timezone(user.tz)
    )


# --- правка -------------------------------------------------------------------


#: Кнопки, которые что-то меняют. Открывающие экран (`freq`, `window`, `gap`,
#: `tz`) сюда не входят: их дело — нарисовать список, а не записать выбор.
SET_ACTIONS = frozenset({"set_freq", "set_window", "set_gap", "set_tz"})


@router.callback_query(keyboards.SettingsAction.filter(F.action.in_(SET_ACTIONS)))
async def handle_set(
    callback: CallbackQuery,
    callback_data: keyboards.SettingsAction,
    session: AsyncSession,
    state: FSMContext,
    llm: LlmClient,
) -> None:
    """Применить выбранное кнопкой значение.

    Каждое значение проверяется, а не приводится к типу на веру: данные кнопки
    видны в клиенте и подставляются руками, а глобального error-хендлера у
    диспетчера нет — на `int("½")` обработчик просто молча умрёт.
    """
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    action, value = callback_data.action, callback_data.value
    user = await _current_user(session, callback.from_user.id)

    if value == "custom":
        await callback.answer()
        if action == "set_freq":
            await state.set_state(SettingsFlow.frequency)
            prompt = texts.SETTINGS_FREQUENCY_CUSTOM.format(
                low=clock.MIN_EPISODES_PER_DAY, high=clock.MAX_EPISODES_PER_DAY
            )
        elif action == "set_window":
            await state.set_state(SettingsFlow.window)
            prompt = texts.SETTINGS_WINDOW_CUSTOM
        elif action == "set_gap":
            await state.set_state(SettingsFlow.gap)
            prompt = texts.SETTINGS_GAP_CUSTOM.format(
                low=clock.MIN_GAP_MINUTES, high=clock.MAX_GAP_MINUTES // 60
            )
        else:
            await state.set_state(SettingsFlow.timezone)
            prompt = texts.SETTINGS_TIMEZONE_CUSTOM
        await callback.message.answer(prompt)
        return

    if action == "set_freq":
        if not value.isdecimal():
            await callback.answer()
            return
        user.episodes_per_day = clock.clamp_episodes_per_day(int(value))
    elif action == "set_window":
        span = keyboards.window_preset(value)
        # Меркой общая константа, а не промежуток этого человека: кнопку он
        # нажал, и отвергнуть её молча нельзя — кнопки не умеют объясняться.
        # Что при большом промежутке в такое окно влезет мало разговоров,
        # скажет сам экран строкой «столько не помещается».
        parsed = clock.parse_window(span) if span is not None else None
        if parsed is None:
            await callback.answer()
            return
        user.window_start, user.window_end = parsed
    elif action == "set_gap":
        minutes = keyboards.gap_preset(value)
        if minutes is None:
            await callback.answer()
            return
        user.min_gap_minutes = clock.clamp_min_gap(minutes)
    else:
        if clock.zone(value).key != value:
            # Тот же довод, что и у набранного руками пояса ниже: `clock.zone`
            # подменяет незнакомое имя на UTC, и записать его — значит показать
            # человеку пояс, по которому разговоры не приходят.
            await callback.answer()
            return
        user.tz = value

    await session.commit()
    await callback.answer(texts.SETTINGS_SAVED)
    await callback.message.edit_text(_screen(user), reply_markup=_menu(user))
    await _replan(session, llm, user=user, now=dt.datetime.now(dt.UTC))


@router.message(SettingsFlow.frequency)
async def handle_frequency_typed(
    message: Message, session: AsyncSession, state: FSMContext, llm: LlmClient
) -> None:
    if message.from_user is None or message.text is None:
        return

    count = clock.parse_episodes_per_day(message.text)
    if count is None:
        await message.answer(
            texts.SETTINGS_FREQUENCY_BAD.format(
                low=clock.MIN_EPISODES_PER_DAY, high=clock.MAX_EPISODES_PER_DAY
            )
        )
        return

    user = await _current_user(session, message.from_user.id)
    user.episodes_per_day = count
    await session.commit()
    await state.clear()
    await _show(message, user)
    await _replan(session, llm, user=user, now=dt.datetime.now(dt.UTC))


@router.message(SettingsFlow.gap)
async def handle_gap_typed(
    message: Message, session: AsyncSession, state: FSMContext, llm: LlmClient
) -> None:
    if message.from_user is None or message.text is None:
        return

    minutes = clock.parse_gap(message.text)
    if minutes is None:
        await message.answer(
            texts.SETTINGS_GAP_BAD.format(
                low=clock.MIN_GAP_MINUTES, high=clock.MAX_GAP_MINUTES // 60
            )
        )
        return

    user = await _current_user(session, message.from_user.id)
    user.min_gap_minutes = minutes
    await session.commit()
    await state.clear()
    await _show(message, user)
    await _replan(session, llm, user=user, now=dt.datetime.now(dt.UTC))


@router.message(SettingsFlow.window)
async def handle_window_typed(
    message: Message, session: AsyncSession, state: FSMContext, llm: LlmClient
) -> None:
    """Окно меряем промежутком этого человека, а не общей константой.

    Поэтому пользователь читается до разбора: выбрав два часа между
    разговорами, он тем самым объявил часовое окно бессмысленным, и сказать
    ему об этом надо его же меркой.
    """
    if message.from_user is None or message.text is None:
        return

    user = await _current_user(session, message.from_user.id)
    gap = clock.gap_of(user.min_gap_minutes)
    parsed = clock.parse_window(message.text, min_span=gap)
    if parsed is None:
        await message.answer(
            texts.SETTINGS_WINDOW_BAD.format(gap=render.gap_form(int(gap.total_seconds() // 60)))
        )
        return

    user.window_start, user.window_end = parsed
    await session.commit()
    await state.clear()
    await _show(message, user)
    await _replan(session, llm, user=user, now=dt.datetime.now(dt.UTC))


@router.message(SettingsFlow.timezone)
async def handle_timezone_typed(
    message: Message, session: AsyncSession, state: FSMContext, llm: LlmClient
) -> None:
    """Пояс проверяем до записи: `clock.zone` подменяет незнакомый на UTC.

    Подмена спасает воркер от падения на строке, которая уже лежит в базе, но
    записывать заведомо неизвестное имя нельзя — человек увидит на экране свой
    Europe/Lisboa и не поймёт, почему разговоры приходят не тогда.
    """
    if message.from_user is None or message.text is None:
        return

    name = message.text.strip()
    if clock.zone(name).key != name:
        await message.answer(texts.SETTINGS_TIMEZONE_BAD)
        return

    user = await _current_user(session, message.from_user.id)
    user.tz = name
    await session.commit()
    await state.clear()
    await _show(message, user)
    await _replan(session, llm, user=user, now=dt.datetime.now(dt.UTC))


# --- пауза --------------------------------------------------------------------


async def _pause(session: AsyncSession, *, user: User, now: dt.datetime) -> str:
    if user.paused_at is not None:
        return texts.PAUSED_ALREADY
    await users.pause(session, user=user, now=now)
    await dialog.clear_day(session, user_id=user.id, now=now)
    return texts.PAUSED


async def _resume(session: AsyncSession, llm: LlmClient, *, user: User, now: dt.datetime) -> str:
    if user.paused_at is None:
        return texts.RESUMED_ALREADY
    days = await users.resume(session, user=user, now=now)
    await _replan(session, llm, user=user, now=now)
    if not days:
        return texts.RESUMED
    return texts.RESUMED_WITH_DAYS.format(days=days, days_form=render.days_left_form(days))


@router.message(Command("pause"))
async def handle_pause(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    user = await _current_user(session, message.from_user.id)
    await message.answer(await _pause(session, user=user, now=dt.datetime.now(dt.UTC)))


@router.message(Command("resume"))
async def handle_resume(message: Message, session: AsyncSession, llm: LlmClient) -> None:
    if message.from_user is None:
        return
    user = await _current_user(session, message.from_user.id)
    await message.answer(await _resume(session, llm, user=user, now=dt.datetime.now(dt.UTC)))


@router.callback_query(keyboards.SettingsAction.filter(F.action.in_({"pause", "resume"})))
async def handle_pause_button(
    callback: CallbackQuery,
    callback_data: keyboards.SettingsAction,
    session: AsyncSession,
    llm: LlmClient,
) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    user = await _current_user(session, callback.from_user.id)
    now = dt.datetime.now(dt.UTC)
    if callback_data.action == "pause":
        answer = await _pause(session, user=user, now=now)
    else:
        answer = await _resume(session, llm, user=user, now=now)

    await callback.answer()
    await callback.message.edit_text(_screen(user), reply_markup=_menu(user))
    await callback.message.answer(answer)


__all__ = ["router"]
