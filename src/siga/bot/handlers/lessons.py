"""Пачка из уроков учебника: `/lesson 3`, `/lesson 1-4` (FR-IMP-12).

Слова берутся из встроенного словаря (`siga.glossary`) и кладутся в
черновик пачки — дальше всё как при импорте текстом: экран подтверждения
с правкой, `✅` → обогащение → активная пачка → разговоры по расписанию.
Отдельного «режима тренировки по урокам» нет: единица работы бота —
пачка, и всё остальное (SRS, план дня, `/next`, `/pack`) уже умеет с ней жить.

Роутер подключается раньше `imports`: там в состоянии «жду список» любой
текст считается списком, и `/lesson 3` превратился бы в «не нашёл слов».
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from siga import glossary
from siga.bot import keyboards, render, texts
from siga.bot.handlers.imports import ImportFlow, show_draft
from siga.core.enums import WordSource
from siga.core.wordlist import MAX_LESSON_PACK_WORDS, MIN_PACK_WORDS
from siga.db import packs, users

log = logging.getLogger(__name__)

router = Router(name="lessons")


def _lessons_label(first: int, last: int) -> str:
    return f"урок {first}" if first == last else f"уроки {first}–{last}"


def _widest_fitting(book: glossary.Glossary, first: int) -> int:
    """Последний урок, до которого диапазон от `first` ещё влезает в лимит."""
    last = first
    for lesson in book.lessons:
        if lesson <= first:
            continue
        if len(book.words(first, lesson)) > MAX_LESSON_PACK_WORDS:
            break
        last = lesson
    return last


@router.message(Command("lesson"))
async def handle_lesson(
    message: Message, command: CommandObject, session: AsyncSession, state: FSMContext
) -> None:
    """Без номера — меню уроков, с номером или диапазоном — черновик пачки."""
    if message.from_user is None:
        return
    book = glossary.load()
    lessons = book.lessons
    args = (command.args or "").strip()

    if not args:
        await message.answer(
            texts.LESSON_MENU.format(book=glossary.BOOK_TITLE, first=lessons[0], last=lessons[-1]),
            reply_markup=keyboards.lesson_menu(lessons),
        )
        return

    picked = glossary.parse_range(args, lessons)
    if picked is None:
        await message.answer(texts.LESSON_BAD_RANGE.format(first=lessons[0], last=lessons[-1]))
        return

    await _make_draft(message, session, state, tg_user_id=message.from_user.id, lessons=picked)


@router.callback_query(keyboards.LessonAction.filter())
async def handle_lesson_button(
    callback: CallbackQuery,
    callback_data: keyboards.LessonAction,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Кнопка урока из меню. Payload проверяем так же, как текст команды:
    кнопка живёт в переписке вечно, а словарь может поменяться."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    lessons = glossary.load().lessons
    picked = glossary.parse_range(f"{callback_data.first}-{callback_data.last}", lessons)
    await callback.answer()
    if picked is None:
        await callback.message.answer(
            texts.LESSON_BAD_RANGE.format(first=lessons[0], last=lessons[-1])
        )
        return
    await _make_draft(
        callback.message, session, state, tg_user_id=callback.from_user.id, lessons=picked
    )


async def _make_draft(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    *,
    tg_user_id: int,
    lessons: tuple[int, int],
) -> None:
    """Собрать черновик пачки из уроков и показать экран подтверждения.

    Активную пачку здесь не трогаем и не спрашиваем о ней заранее, как `/add`:
    черновик ей не мешает, а архивирует её только подтверждение
    (`packs.activate`). Об этом честно говорим над списком.
    """
    first, last = lessons
    book = glossary.load()
    words = book.words(first, last)
    label = _lessons_label(first, last)

    if len(words) > MAX_LESSON_PACK_WORDS:
        await message.answer(
            texts.LESSON_TOO_MANY.format(
                lessons=label.capitalize(),
                count=len(words),
                word_form=render.words_form(len(words)),
                limit=MAX_LESSON_PACK_WORDS,
                first=first,
                suggest_last=_widest_fitting(book, first),
            )
        )
        return

    user, _ = await users.get_or_create(session, tg_user_id=tg_user_id)
    pack = await packs.create_draft(
        session,
        user_id=user.id,
        title=render.lesson_pack_title(first, last),
        source_type=WordSource.GLOSSARY,
        raw_text=f"{glossary.BOOK_TITLE}: {label}",
        words=words,
    )
    log.info("уроки: пользователь=%s уроки=%s-%s слов=%s", user.id, first, last, len(words))

    await message.answer(
        texts.LESSON_DRAFT.format(
            lessons=label, count=len(words), word_form=render.words_form(len(words))
        )
    )
    if len(words) < MIN_PACK_WORDS:
        await message.answer(texts.ADD_TOO_FEW.format(count=len(words)))

    active = await packs.get_active(session, user_id=user.id)
    if active is not None:
        await message.answer(texts.LESSON_REPLACES_ACTIVE.format(title=active.title))

    await state.set_state(ImportFlow.confirming)
    await show_draft(message, session, pack)


__all__ = ["router"]
