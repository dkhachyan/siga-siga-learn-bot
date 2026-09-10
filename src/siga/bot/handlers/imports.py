"""Импорт слов: список текстом → экран подтверждения → пачка.

Реализует §5.2 спецификации в части текстового ввода. Фото — этап 2.

Состояние диалога живёт в FSM, но сам список — в базе (см. `db.packs`).
Поэтому потеря состояния при рестарте не теряет слова: `/pack` вернёт
человека к тому же черновику.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from siga.bot import keyboards, render, texts
from siga.bot.handlers import cards
from siga.core.enums import Level, WordSource
from siga.core.wordlist import (
    MAX_PACK_WORDS,
    MIN_PACK_WORDS,
    ParsedWord,
    RejectedLine,
    parse_wordlist,
)
from siga.db import packs, users
from siga.db.models import DEFAULT_PERIOD_DAYS, Pack, User
from siga.llm.base import LlmClient, LlmError
from siga.llm.enrich import enrich

log = logging.getLogger(__name__)

router = Router(name="imports")


class ImportFlow(StatesGroup):
    """Шаги импорта. Хранится только то, чего нет в базе."""

    waiting_list = State()
    """Ждём сообщение со списком слов."""

    confirming = State()
    """Показан экран подтверждения, ждём нажатия кнопки."""

    waiting_new_word = State()
    """Нажали «Добавить», ждём одну строку."""

    waiting_translation = State()
    """Выбрали слово для правки, ждём новый перевод. В данных — `word_id`."""


async def _current_user(session: AsyncSession, message_from_id: int) -> User:
    user, _ = await users.get_or_create(session, tg_user_id=message_from_id)
    return user


async def _current_user_id(session: AsyncSession, message_from_id: int) -> int:
    return (await _current_user(session, message_from_id)).id


async def _show_pack(message: Message, session: AsyncSession, pack: Pack) -> None:
    """Отправить экран подтверждения: список плюс кнопки на последнем сообщении."""
    words = await packs.list_words(session, pack_id=pack.id)
    if not words:
        await message.answer(texts.PACK_EMPTY)
        return

    known = await packs.find_known_lemmas(
        session,
        user_id=pack.user_id,
        lemmas=[word.lemma for word in words],
        exclude_pack_id=pack.id,
    )
    lines = [
        render.WordLine(
            lemma=word.lemma_accented,
            translation=word.translation_ru,
            known=word.lemma in known,
        )
        for word in words
    ]

    chunks = render.pack_screen(pack.title, lines)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=keyboards.pack_screen())


# --- вход --------------------------------------------------------------------


@router.message(Command("add"))
async def handle_add(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """Начать импорт — или сперва спросить, что делать с текущей пачкой.

    Активная пачка у человека одна (§5.8): период, отчёт в конце и перенос
    невыученных слов написаны в единственном числе, и две пачки сразу ломают
    всё три. Молча архивировать прежнюю нельзя — человек мог набрать `/add`
    из любопытства, а не чтобы попрощаться с недоученным списком.
    """
    if message.from_user is None:
        return

    user_id = await _current_user_id(session, message.from_user.id)
    active = await packs.get_active(session, user_id=user_id)
    if active is not None:
        await _ask_about_replacing(message, session, active)
        return

    await state.set_state(ImportFlow.waiting_list)
    await message.answer(texts.ADD_PROMPT)


async def _ask_about_replacing(message: Message, session: AsyncSession, active: Pack) -> None:
    words = await packs.list_words(session, pack_id=active.id)
    await message.answer(
        texts.ADD_WHILE_ACTIVE.format(
            title=active.title,
            count=len(words),
            word_form=render.words_form(len(words)),
            deadline=render.deadline_line(cards.days_left(active, dt.datetime.now(dt.UTC))),
        ),
        reply_markup=keyboards.replace_pack(),
    )


@router.callback_query(keyboards.PackAction.filter(F.action == "replace"))
async def handle_replace(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    """Убрать активную пачку в архив и сразу начать новый импорт."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    user_id = await _current_user_id(session, callback.from_user.id)
    active = await packs.get_active(session, user_id=user_id)
    await callback.answer()
    if active is None:
        # Пачку успели убрать другим окном — цель нажатия уже достигнута.
        await state.set_state(ImportFlow.waiting_list)
        await callback.message.answer(texts.ADD_PROMPT)
        return

    await packs.archive(session, pack=active)
    log.info("пачка %s убрана в архив перед новым импортом", active.id)
    await state.set_state(ImportFlow.waiting_list)
    await callback.message.answer(
        f"{texts.ADD_ARCHIVED.format(title=active.title)}\n\n{texts.ADD_PROMPT}"
    )


@router.callback_query(keyboards.PackAction.filter(F.action == "keep"))
async def handle_keep(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    """Отказ от новой пачки: возвращаем человека к текущей."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    user_id = await _current_user_id(session, callback.from_user.id)
    active = await packs.get_active(session, user_id=user_id)
    await state.clear()
    await callback.answer()
    await callback.message.answer(texts.ADD_KEPT)
    if active is not None:
        await cards.show_pack(callback.message, session, active)


@router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(texts.CANCELLED)
        return
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.message(Command("pack"))
async def handle_pack(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """Показать текущую пачку: сначала черновик, потом активную.

    Черновик вперёд не случайно: незаконченный импорт — это долг перед
    человеком, и `/pack` должен возвращать к нему, а не показывать прошлую
    пачку, будто список слов куда-то делся.
    """
    if message.from_user is None:
        return
    user_id = await _current_user_id(session, message.from_user.id)

    draft = await packs.get_draft(session, user_id=user_id)
    if draft is not None:
        await state.set_state(ImportFlow.confirming)
        await _show_pack(message, session, draft)
        return

    active = await packs.get_active(session, user_id=user_id)
    if active is None:
        await message.answer(texts.NO_PACK)
        return
    await cards.show_pack(message, session, active)


@router.message(ImportFlow.waiting_list, F.photo)
async def handle_photo(message: Message) -> None:
    """Фото — этап 2. Отвечаем честно, а не молчим в состоянии ожидания."""
    await message.answer(texts.ADD_PHOTO_NOT_YET)


@router.message(ImportFlow.waiting_list, F.text)
async def handle_list(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return

    result = parse_wordlist(message.text)
    if not result.words:
        await message.answer(texts.ADD_NOTHING_PARSED)
        return

    if len(result.words) > MAX_PACK_WORDS:
        await message.answer(texts.ADD_TOO_MANY.format(count=len(result.words)))
        return

    user_id = await _current_user_id(session, message.from_user.id)
    pack = await packs.create_draft(
        session,
        user_id=user_id,
        title=render.pack_title(dt.datetime.now(dt.UTC).date()),
        source_type=WordSource.TEXT,
        raw_text=message.text,
        words=result.words,
    )
    log.info(
        "импорт: пользователь=%s слов=%s дублей=%s отброшено=%s",
        user_id,
        len(result.words),
        len(result.duplicates),
        len(result.rejected),
    )

    if result.duplicates or result.rejected:
        await message.answer(_parse_report(result.duplicates, result.rejected))

    if len(result.words) < MIN_PACK_WORDS:
        await message.answer(texts.ADD_TOO_FEW.format(count=len(result.words)))

    await state.set_state(ImportFlow.confirming)
    await _show_pack(message, session, pack)


def _parse_report(duplicates: Sequence[ParsedWord], rejected: Sequence[RejectedLine]) -> str:
    """Что не попало в пачку и почему — молча ничего не теряем."""
    parts = []
    if duplicates:
        listed = ", ".join(word.lemma for word in duplicates)
        parts.append(f"Повторы в списке, взял по одному разу: {listed}.")
    if rejected:
        listed = "\n".join(f"• строка {line.line_no}: {line.raw}" for line in rejected)
        parts.append(f"Не разобрал:\n{listed}")
    return "\n\n".join(parts)


# --- кнопки экрана подтверждения ---------------------------------------------


async def _draft_or_complain(
    callback: CallbackQuery, session: AsyncSession
) -> tuple[Pack, Message, User] | None:
    """Черновик, сообщение для ответа и хозяин черновика — или отказ."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return None
    user = await _current_user(session, callback.from_user.id)
    pack = await packs.get_draft(session, user_id=user.id)
    if pack is None:
        await callback.answer(texts.NO_DRAFT, show_alert=True)
        return None
    return pack, callback.message, user


@router.callback_query(keyboards.PackAction.filter(F.action == "back"))
async def handle_back(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    found = await _draft_or_complain(callback, session)
    if found is None:
        return
    pack, message, _ = found
    await state.set_state(ImportFlow.confirming)
    await callback.answer()
    await _show_pack(message, session, pack)


@router.callback_query(keyboards.PackAction.filter(F.action == "delete"))
async def handle_delete_pick(callback: CallbackQuery, session: AsyncSession) -> None:
    found = await _draft_or_complain(callback, session)
    if found is None:
        return
    pack, message, _ = found
    words = await packs.list_words(session, pack_id=pack.id)
    await callback.answer()
    await message.answer(
        texts.DELETE_PICK,
        reply_markup=keyboards.word_numbers([word.id for word in words], "delete"),
    )


@router.callback_query(keyboards.PackAction.filter(F.action == "edit"))
async def handle_edit_pick(callback: CallbackQuery, session: AsyncSession) -> None:
    found = await _draft_or_complain(callback, session)
    if found is None:
        return
    pack, message, _ = found
    words = await packs.list_words(session, pack_id=pack.id)
    await callback.answer()
    await message.answer(
        texts.EDIT_PICK,
        reply_markup=keyboards.word_numbers([word.id for word in words], "edit"),
    )


@router.callback_query(keyboards.WordAction.filter(F.action == "delete"))
async def handle_delete_word(
    callback: CallbackQuery,
    callback_data: keyboards.WordAction,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    found = await _draft_or_complain(callback, session)
    if found is None:
        return
    pack, message, _ = found
    await packs.remove_word(session, pack_id=pack.id, word_id=callback_data.word_id)
    await state.set_state(ImportFlow.confirming)
    await callback.answer("Убрал")
    await _show_pack(message, session, pack)


@router.callback_query(keyboards.WordAction.filter(F.action == "edit"))
async def handle_edit_word(
    callback: CallbackQuery,
    callback_data: keyboards.WordAction,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    found = await _draft_or_complain(callback, session)
    if found is None:
        return
    pack, message, _ = found
    words = {word.id: word for word in await packs.list_words(session, pack_id=pack.id)}
    word = words.get(callback_data.word_id)
    if word is None:
        await callback.answer(texts.NO_DRAFT, show_alert=True)
        return

    await state.set_state(ImportFlow.waiting_translation)
    await state.update_data(word_id=word.id)
    await callback.answer()
    await message.answer(texts.EDIT_TRANSLATION_PROMPT.format(lemma=word.lemma_accented))


@router.message(ImportFlow.waiting_translation, F.text)
async def handle_new_translation(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user is None or message.text is None:
        return
    data = await state.get_data()
    word_id = data.get("word_id")
    user_id = await _current_user_id(session, message.from_user.id)
    pack = await packs.get_draft(session, user_id=user_id)
    if pack is None or word_id is None:
        await state.clear()
        await message.answer(texts.NO_DRAFT)
        return

    await packs.set_translation(
        session, pack_id=pack.id, word_id=int(word_id), translation_ru=message.text.strip()
    )
    await state.set_state(ImportFlow.confirming)
    await _show_pack(message, session, pack)


@router.callback_query(keyboards.PackAction.filter(F.action == "add"))
async def handle_add_pick(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    found = await _draft_or_complain(callback, session)
    if found is None:
        return
    _, message, _ = found
    await state.set_state(ImportFlow.waiting_new_word)
    await callback.answer()
    await message.answer(texts.ADD_ONE_PROMPT)


@router.message(ImportFlow.waiting_new_word, F.text)
async def handle_new_word(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    user_id = await _current_user_id(session, message.from_user.id)
    pack = await packs.get_draft(session, user_id=user_id)
    if pack is None:
        await state.clear()
        await message.answer(texts.NO_DRAFT)
        return

    result = parse_wordlist(message.text)
    if not result.words:
        await message.answer(texts.ADD_ONE_NO_GREEK)
        return

    parsed = result.words[0]
    word = await packs.add_word(
        session,
        pack=pack,
        lemma=parsed.lemma,
        translation_ru=parsed.translation_ru,
    )
    if word is None:
        await message.answer(texts.ADD_ONE_DUPLICATE.format(lemma=parsed.lemma))
        return

    await state.set_state(ImportFlow.confirming)
    await _show_pack(message, session, pack)


async def _enrich_pack(
    message: Message, session: AsyncSession, llm: LlmClient, pack: Pack, level: Level
) -> None:
    """Добрать грамматику для пачки и рассказать человеку, что вышло.

    Пачка важнее грамматики: любая неудача модели тут заканчивается словами
    «сохранил без артиклей», а не потерей списка, который человек набирал
    руками. Слова без обогащения остаются с пустым `enriched_at`.
    """
    words = await packs.list_words(session, pack_id=pack.id)
    await message.answer(texts.ENRICHING)

    try:
        result = await enrich(llm, lemmas=[word.lemma_accented for word in words], level=level)
    except LlmError as error:
        log.warning("R2: обогащение пачки %s не удалось (%s)", pack.id, error)
        await message.answer(texts.ENRICH_FAILED.format(reason=error))
        return

    updated = await packs.apply_enrichment(
        session, pack_id=pack.id, enriched=result.words, now=dt.datetime.now(dt.UTC)
    )
    log.info(
        "R2: пачка=%s обогащено=%s без грамматики=%s токенов=%s",
        pack.id,
        updated,
        len(result.missing),
        result.usage.total_tokens,
    )
    if result.missing:
        await message.answer(
            texts.ENRICH_PARTIAL.format(count=len(result.missing), listed=", ".join(result.missing))
        )


@router.callback_query(keyboards.PackAction.filter(F.action == "confirm"))
async def handle_confirm(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, llm: LlmClient
) -> None:
    found = await _draft_or_complain(callback, session)
    if found is None:
        return
    pack, message, user = found

    words = await packs.list_words(session, pack_id=pack.id)
    if not words:
        await callback.answer()
        await message.answer(texts.PACK_EMPTY)
        return

    # Кнопку отпускаем сразу: обогащение идёт до минуты, а Telegram ждёт
    # ответа на callback секунды и потом рисует человеку ошибку.
    await callback.answer()
    await _enrich_pack(message, session, llm, pack, Level(user.level))

    await packs.activate(
        session,
        pack=pack,
        period_days=DEFAULT_PERIOD_DAYS,
        now=dt.datetime.now(dt.UTC),
    )
    await state.clear()
    log.info("пачка подтверждена id=%s слов=%s", pack.id, len(words))

    await message.answer(
        texts.CONFIRMED.format(
            title=pack.title,
            count=len(words),
            word_form=render.words_form(len(words)),
        ),
        reply_markup=keyboards.active_pack_screen(),
    )


__all__ = ["ImportFlow", "router"]
