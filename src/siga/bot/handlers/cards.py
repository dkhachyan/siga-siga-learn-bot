"""Просмотр активной пачки: список, карточки слов и уборка слова из пачки.

Последний шаг §5.2 после обогащения — человек должен увидеть, что получилось:
слово с ударением, артикль, род, примеры. Это же и способ вернуться к пачке
потом, через `/pack`.

Переводы и грамматику тут не правим: пачка уже в работе, и экран правки живёт
на черновике, в `handlers.imports`. Убрать слово целиком — можно (FR-PACK-1):
список на ходу перегружают, а слово, которое уже знали до пачки, в разговорах
не нужно. Правка перевода его бы тоже устроила, но одна кнопка за раз.
"""

from __future__ import annotations

import datetime as dt
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from siga.bot import keyboards, render, texts
from siga.db import packs, users
from siga.db.models import Pack, User, Word

log = logging.getLogger(__name__)

router = Router(name="cards")


def _card(word: Word) -> render.Card:
    return render.Card(
        lemma=word.lemma_accented,
        translation=word.translation_ru,
        translation_model=word.translation_model,
        pos=word.pos,
        article=word.article,
        gender=word.gender,
        verb_form=word.verb_form,
        examples=word.examples,
        enriched=word.enriched_at is not None,
    )


def days_left(pack: Pack, now: dt.datetime) -> int | None:
    """Сколько дней осталось до конца периода. None — срок не выставлен."""
    if pack.ends_at is None:
        return None
    remaining = pack.ends_at - now
    if remaining <= dt.timedelta(0):
        return 0
    # Вверх, а не вниз: пока срок не истёк, «остался 1 день» правдивее нуля.
    return -(-remaining // dt.timedelta(days=1))


async def show_pack(message: Message, session: AsyncSession, pack: Pack) -> None:
    """Отправить список активной пачки с кнопкой перехода к карточкам."""
    words = await packs.list_words(session, pack_id=pack.id)
    if not words:
        await message.answer(texts.PACK_EMPTY)
        return

    lines = [
        render.WordLine(lemma=word.lemma_accented, translation=word.translation_ru)
        for word in words
    ]
    chunks = render.active_pack_screen(
        pack.title, lines, days_left=days_left(pack, dt.datetime.now(dt.UTC))
    )
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=keyboards.active_pack_screen())


async def _cards_or_complain(
    callback: CallbackQuery, session: AsyncSession, index: int
) -> tuple[Message, Pack, list[Word], int] | None:
    """Сообщение, пачка, её слова и выправленный номер — или отказ."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return None

    user, _ = await users.get_or_create(session, tg_user_id=callback.from_user.id)
    pack = await packs.get_active(session, user_id=user.id)
    if pack is None:
        await callback.answer(texts.NO_PACK, show_alert=True)
        return None

    words = await packs.list_words(session, pack_id=pack.id)
    if not words:
        await callback.answer(texts.PACK_EMPTY, show_alert=True)
        return None

    # По модулю, а не проверкой границ: номер приехал из кнопки, нарисованной
    # для прежнего состава пачки, и вполне может смотреть за конец.
    return callback.message, pack, words, index % len(words)


def _render_card(pack: Pack, words: list[Word], index: int) -> tuple[str, InlineKeyboardMarkup]:
    text = render.word_card(
        _card(words[index]), position=index + 1, total=len(words), title=pack.title
    )
    return text, keyboards.card_nav(index, len(words))


@router.callback_query(keyboards.CardAction.filter(F.action == "open"))
async def handle_open(
    callback: CallbackQuery, callback_data: keyboards.CardAction, session: AsyncSession
) -> None:
    """Первая карточка — новым сообщением, список остаётся выше нетронутым."""
    found = await _cards_or_complain(callback, session, callback_data.index)
    if found is None:
        return
    message, pack, words, index = found
    text, markup = _render_card(pack, words, index)
    await callback.answer()
    await message.answer(text, reply_markup=markup)


@router.callback_query(keyboards.CardAction.filter(F.action == "show"))
async def handle_flip(
    callback: CallbackQuery, callback_data: keyboards.CardAction, session: AsyncSession
) -> None:
    """Листание на месте: карточка сменяется в том же сообщении."""
    found = await _cards_or_complain(callback, session, callback_data.index)
    if found is None:
        return
    message, pack, words, index = found
    text, markup = _render_card(pack, words, index)
    await callback.answer()
    await message.edit_text(text, reply_markup=markup)


@router.callback_query(keyboards.CardAction.filter(F.action == "list"))
async def handle_list(callback: CallbackQuery, session: AsyncSession) -> None:
    found = await _cards_or_complain(callback, session, 0)
    if found is None:
        return
    message, pack, _, _ = found
    await callback.answer()
    await show_pack(message, session, pack)


# --- убрать слово (FR-PACK-1) -------------------------------------------------


async def _active_or_complain(
    callback: CallbackQuery, session: AsyncSession
) -> tuple[Message, Pack, User] | None:
    """Активная пачка нажавшего и его сообщение — или отказ.

    Пачка берётся по нажавшему, а не по сообщению: чужая кнопка показывает
    чужому его собственную (отсутствующую) пачку, а не содержимое чужой.
    """
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return None
    user, _ = await users.get_or_create(session, tg_user_id=callback.from_user.id)
    pack = await packs.get_active(session, user_id=user.id)
    if pack is None:
        await callback.answer(texts.NO_PACK, show_alert=True)
        return None
    return callback.message, pack, user


def _word_of(words: list[Word], word_id: int) -> Word | None:
    """Слово пачки по номеру из кнопки. Подделанный номер молча не совпадёт."""
    return next((word for word in words if word.id == word_id), None)


@router.callback_query(keyboards.PackAction.filter(F.action == "drop"))
async def handle_drop_pick(callback: CallbackQuery, session: AsyncSession) -> None:
    """Открыть выбор: номера слов активной пачки."""
    found = await _active_or_complain(callback, session)
    if found is None:
        return
    message, pack, _ = found
    words = await packs.list_words(session, pack_id=pack.id)
    if not words:
        await callback.answer(texts.PACK_EMPTY, show_alert=True)
        return
    await callback.answer()
    await message.answer(
        texts.DELETE_PICK,
        reply_markup=keyboards.word_numbers([word.id for word in words], "drop", back_to_list=True),
    )


@router.callback_query(keyboards.WordAction.filter(F.action == "drop"))
async def handle_drop_pick_word(
    callback: CallbackQuery, callback_data: keyboards.WordAction, session: AsyncSession
) -> None:
    """Выбран номер — переспросить, прежде чем сжигать прогресс."""
    found = await _active_or_complain(callback, session)
    if found is None:
        return
    message, pack, _ = found
    words = await packs.list_words(session, pack_id=pack.id)
    word = _word_of(words, callback_data.word_id)
    if word is None:
        await callback.answer(texts.DROP_MISSING, show_alert=True)
        return
    await callback.answer()
    await message.edit_text(
        texts.DROP_CONFIRM.format(
            lemma=word.lemma_accented,
            translation=word.translation_ru or word.translation_model or "",
        ),
        reply_markup=keyboards.confirm_drop(word.id),
    )


@router.callback_query(keyboards.WordAction.filter(F.action == "drop_yes"))
async def handle_drop_yes(
    callback: CallbackQuery,
    callback_data: keyboards.WordAction,
    session: AsyncSession,
) -> None:
    """Убрать слово и показать пачку, какой она стала."""
    found = await _active_or_complain(callback, session)
    if found is None:
        return
    message, pack, _ = found
    words = await packs.list_words(session, pack_id=pack.id)
    word = _word_of(words, callback_data.word_id)
    if word is None:
        await callback.answer(texts.DROP_MISSING, show_alert=True)
        return

    lemma = word.lemma_accented
    await packs.remove_word(session, pack_id=pack.id, word_id=word.id)
    log.info("слово %s (%s) убрано из активной пачки %s", word.id, lemma, pack.id)
    await callback.answer("Убрал")
    # Список после удаления нужен заново: нумерация и «осталось дней» другие.
    await show_pack(message, session, pack)


@router.callback_query(keyboards.WordAction.filter(F.action == "drop_no"))
async def handle_drop_no(
    callback: CallbackQuery,
    callback_data: keyboards.WordAction,
    session: AsyncSession,
) -> None:
    """Отказ: вернуть тот же экран выбора, откуда переспрос и приехал."""
    found = await _active_or_complain(callback, session)
    if found is None:
        return
    message, pack, _ = found
    words = await packs.list_words(session, pack_id=pack.id)
    await callback.answer()
    await message.edit_text(
        texts.DELETE_PICK,
        reply_markup=keyboards.word_numbers([word.id for word in words], "drop", back_to_list=True),
    )


__all__ = ["days_left", "router", "show_pack"]
