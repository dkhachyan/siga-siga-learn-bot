"""Просмотр активной пачки: список и карточки слов.

Последний шаг §5.2 после обогащения — человек должен увидеть, что получилось:
слово с ударением, артикль, род, примеры. Это же и способ вернуться к пачке
потом, через `/pack`.

Правок тут нет намеренно. Пачка уже в работе, по ней считается прогресс, и
менять в ней слова на ходу — отдельный разговор (см. §5.8); экран правки
живёт на черновике, в `handlers.imports`.
"""

from __future__ import annotations

import datetime as dt

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from siga.bot import keyboards, render, texts
from siga.db import packs, users
from siga.db.models import Pack, Word

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


def _days_left(pack: Pack, now: dt.datetime) -> int | None:
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
        pack.title, lines, days_left=_days_left(pack, dt.datetime.now(dt.UTC))
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


__all__ = ["router", "show_pack"]
