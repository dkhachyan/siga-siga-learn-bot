"""`/memory`: что Ник о тебе помнит, и как это поправить (FR-MEM-4).

Экран нужен не для отладки. Профиль пишет модель со слов человека, ошибётся
она обязательно, и жить с неверным фактом о себе, который к тому же не видно,
человек не должен. Отсюда и то, что показывается ровно то, что уходит в
промпт, без пересказа своими словами.

Правка здесь одна — забыть: факт по номеру или всё сразу. Дописывать в память
руками нельзя намеренно, иначе профиль перестаёт быть отражением разговоров.
"""

from __future__ import annotations

import datetime as dt

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from siga.bot import keyboards, render, texts
from siga.core import memory as core_memory
from siga.db import memory as memory_db
from siga.db import users

router = Router(name="memory")


Screen = tuple[str, InlineKeyboardMarkup | None]


async def _screen(session: AsyncSession, *, user_id: int) -> Screen:
    """Текст и кнопки экрана памяти по текущему состоянию профиля."""
    profile = await memory_db.load(session, user_id=user_id)
    if profile.is_empty():
        return texts.MEMORY_EMPTY, None

    parts = [render.memory_screen(profile), texts.MEMORY_HINT]
    if profile.facts:
        parts.append(texts.MEMORY_HINT_FACTS)
    return "\n\n".join(parts), keyboards.memory_screen(len(profile.facts))


@router.message(Command("memory"))
async def handle_memory(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    user, _ = await users.get_or_create(session, tg_user_id=message.from_user.id)
    text, markup = await _screen(session, user_id=user.id)
    await message.answer(text, reply_markup=markup)


async def _repaint(callback: CallbackQuery, session: AsyncSession, *, user_id: int) -> None:
    """Перерисовать экран на месте.

    Правкой сообщения, а не новым: память смотрят, чтобы что-то в ней убрать,
    и три копии одного списка подряд превратили бы это в свалку.
    """
    if not isinstance(callback.message, Message):
        return
    text, markup = await _screen(session, user_id=user_id)
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(keyboards.MemoryAction.filter(F.action == "forget"))
async def handle_forget(
    callback: CallbackQuery,
    callback_data: keyboards.MemoryAction,
    session: AsyncSession,
) -> None:
    """Забыть один факт по номеру."""
    if callback.from_user is None:
        await callback.answer()
        return

    user, _ = await users.get_or_create(session, tg_user_id=callback.from_user.id)
    profile = await memory_db.load(session, user_id=user.id)
    shortened = core_memory.forget_fact(profile, callback_data.index)

    # Номер мог устареть: пока экран висел в переписке, разговор мог дописать
    # в память своё. Молча удалить «тот, что теперь под этим номером» нельзя —
    # это ровно та ошибка, от которой экран и заводился.
    if len(shortened.facts) == len(profile.facts):
        await callback.answer(texts.MEMORY_FACT_GONE, show_alert=True)
        await _repaint(callback, session, user_id=user.id)
        return

    await memory_db.save(session, user_id=user.id, profile=shortened, now=dt.datetime.now(dt.UTC))
    await callback.answer(texts.MEMORY_FACT_FORGOTTEN)
    await _repaint(callback, session, user_id=user.id)


@router.callback_query(keyboards.MemoryAction.filter(F.action == "clear"))
async def handle_clear(callback: CallbackQuery) -> None:
    """Спросить перед очисткой. Забвение необратимо."""
    if not isinstance(callback.message, Message):
        await callback.answer()
        return

    await callback.answer()
    await callback.message.edit_text(
        texts.MEMORY_CONFIRM_CLEAR, reply_markup=keyboards.confirm_forget_all()
    )


@router.callback_query(keyboards.MemoryAction.filter(F.action == "confirm"))
async def handle_confirm_clear(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    user, _ = await users.get_or_create(session, tg_user_id=callback.from_user.id)
    await memory_db.clear(session, user_id=user.id, now=dt.datetime.now(dt.UTC))
    await callback.answer()
    await callback.message.edit_text(texts.MEMORY_CLEARED)


@router.callback_query(keyboards.MemoryAction.filter(F.action == "cancel"))
async def handle_cancel_clear(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.from_user is None:
        await callback.answer()
        return

    user, _ = await users.get_or_create(session, tg_user_id=callback.from_user.id)
    await callback.answer(texts.MEMORY_KEPT)
    await _repaint(callback, session, user_id=user.id)


__all__ = ["router"]
