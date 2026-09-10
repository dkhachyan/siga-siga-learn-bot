"""Разговор с Ником в чате: `/next`, ответ текстом, `🔍 Разбор`, `/end`.

Здесь только Telegram: прочитать сообщение, показать «печатает», разложить
ответ по сообщениям. Сам сценарий — в `siga.dialog`, и это не педантизм: те же
эпизоды открывает воркер расписания, у которого чата нет вовсе.

`/next` — способ попросить разговор вне очереди. Обычно эпизод приходит сам,
по расписанию; команда остаётся для тех, у кого выдалось десять свободных
минут не в своё время.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from siga import dialog
from siga.bot import keyboards, render, texts
from siga.core import srs
from siga.core.enums import Level
from siga.db import episodes as episodes_db
from siga.db import packs, users
from siga.db.models import Episode, Turn, User
from siga.llm import hint as hint_llm
from siga.llm import translate as translate_llm
from siga.llm.base import LlmClient, LlmError

log = logging.getLogger(__name__)

router = Router(name="episodes")

#: Короче — не ответ, а промах по клавиатуре (FR-CHK-4). Модель на такое не зовём.
MIN_ANSWER_CHARS = 2


async def _current_user(session: AsyncSession, tg_user_id: int) -> User:
    user, _ = await users.get_or_create(session, tg_user_id=tg_user_id)
    return user


async def _sweep(session: AsyncSession, *, user_id: int, now: dt.datetime) -> bool:
    """Закрыть просроченный эпизод этого человека. True — такой был.

    Тем же занимается отправщик (FR-SCH-6), и всё-таки проверяем сами: бот
    умеет работать без воркеров — локально их обычно и не поднимают, — а
    подвисший эпизод намертво занимает место, и `/next` будет отвечать
    «разговор уже идёт» до скончания века. Работа та же самая, просто
    выполняется в момент, когда человек всё равно ждёт ответа.
    """
    stale = await episodes_db.abandon_stale(session, now=now, user_id=user_id)
    return bool(stale)


def _is_meaningless(text: str) -> bool:
    """Пустой или бессмысленный ответ, FR-CHK-4.

    Проверка нарочно грубая: буква есть — считаем ответом и несём в модель.
    Судить о содержательности по длине строки значит однажды отвергнуть
    честное «ναι».
    """
    stripped = text.strip()
    return len(stripped) < MIN_ANSWER_CHARS or not any(char.isalpha() for char in stripped)


async def _outcomes_block(
    session: AsyncSession, *, episode: Episode, outcomes: dict[int, srs.Outcome]
) -> str:
    """Блок «Как прошло» по итогам закрытия (FR-EP-8)."""
    known = await packs.lemmas(session, word_ids=episode.target_word_ids)
    return render.closing_block(
        [
            render.WordOutcome(
                lemma=known.get(word_id, f"#{word_id}"),
                verdict=outcome.verdict.value if outcome.verdict else None,
                used=outcome.used,
            )
            for word_id, outcome in outcomes.items()
        ]
    )


# --- начало разговора ---------------------------------------------------------


@router.message(Command("next"))
async def handle_next(message: Message, session: AsyncSession, llm: LlmClient, bot: Bot) -> None:
    """Открыть эпизод здесь и сейчас."""
    if message.from_user is None:
        return

    user = await _current_user(session, message.from_user.id)
    now = dt.datetime.now(dt.UTC)
    await _sweep(session, user_id=user.id, now=now)

    if await episodes_db.get_open(session, user_id=user.id) is not None:
        await message.answer(texts.EPISODE_ALREADY_OPEN)
        return

    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            started = await dialog.start(session, llm, user=user, now=now)
    except dialog.NoWordsToPractise as empty:
        await message.answer(
            texts.NO_WORDS_ALL_LEARNED if empty.has_pack else texts.NO_WORDS_NO_PACK
        )
        return
    except LlmError as error:
        log.warning("R3: эпизод для %s не начался (%s)", user.id, error)
        await message.answer(texts.EPISODE_FAILED)
        return

    await message.answer(
        render.plain(started.opening),
        reply_markup=keyboards.reply_actions(
            episode_id=started.episode.id, turn_idx=keyboards.OPENING_TURN_IDX
        ),
    )


# --- ход ----------------------------------------------------------------------


async def _run_turn(
    message: Message,
    session: AsyncSession,
    llm: LlmClient,
    bot: Bot,
    *,
    episode: Episode,
    user: User,
    text: str,
    now: dt.datetime,
) -> None:
    """Сходить в модель за репликой и показать, что вышло."""
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            replied = await dialog.answer(
                session, llm, episode=episode, user=user, text=text, now=now
            )
    except LlmError as error:
        log.warning("R4: ход эпизода %s не удался (%s)", episode.id, error)
        await message.answer(texts.TURN_FAILED)
        return

    if not replied.closed:
        await message.answer(
            render.plain(replied.reply_text),
            reply_markup=keyboards.reply_actions(episode_id=episode.id, turn_idx=replied.turn_idx),
        )
        return

    # Прощание и итог — разными сообщениями: первое говорит Ник, второе
    # показывает бот, и смешивать их в одном пузыре значит смазать оба (§6).
    await message.answer(render.plain(replied.reply_text))
    await message.answer(
        await _outcomes_block(session, episode=episode, outcomes=replied.outcomes),
        reply_markup=keyboards.after_episode(episode.id),
    )
    log.info("эпизод %s закрыт: слов %s", episode.id, len(replied.outcomes))


@router.message(F.text, ~F.text.startswith("/"))
async def handle_answer(message: Message, session: AsyncSession, llm: LlmClient, bot: Bot) -> None:
    """Свободный текст — это ответ в открытом эпизоде (§6).

    Если эпизода нет, ход отдаётся дальше по цепочке через `SkipHandler`:
    проверить это фильтром нельзя — сессия базы приезжает мидлварью, а она
    отрабатывает уже после фильтров.
    """
    if message.from_user is None or message.text is None:
        raise SkipHandler

    user = await _current_user(session, message.from_user.id)
    now = dt.datetime.now(dt.UTC)
    expired = await _sweep(session, user_id=user.id, now=now)

    episode = await episodes_db.get_open(session, user_id=user.id)
    if episode is None:
        if expired:
            await message.answer(texts.EPISODE_EXPIRED)
            return
        # Свободный вопрос по греческому (`R5`, FR-CHK-6) — этап 4.
        raise SkipHandler

    if _is_meaningless(message.text):
        # Кнопки адресуем текущим ходом, а не оставляем без адреса: «не
        # разобрал ответа» — это ровно тот момент, когда человек не знает, что
        # сказать, и подсказка ему нужнее всего.
        current = await episodes_db.last_turn(session, episode_id=episode.id)
        await message.answer(
            texts.ANSWER_UNCLEAR,
            reply_markup=keyboards.reply_actions(
                episode_id=episode.id,
                turn_idx=current.idx if current is not None else None,
            ),
        )
        return

    await _run_turn(
        message, session, llm, bot, episode=episode, user=user, text=message.text, now=now
    )


@router.callback_query(keyboards.EpisodeAction.filter(F.action == "dunno"))
async def handle_dunno(
    callback: CallbackQuery, session: AsyncSession, llm: LlmClient, bot: Bot
) -> None:
    """«Не знаю» — обычный ход, просто реплику за человека пишем мы.

    Не пропуск и не подсказка: Ник должен ответить на признание в незнании
    так же, как ответил бы живой собеседник, — сам употребив слово. Бокс от
    этого не пострадает, слово просто останется неоценённым (FR-EP-7).
    """
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    user = await _current_user(session, callback.from_user.id)
    now = dt.datetime.now(dt.UTC)
    episode = await episodes_db.get_open(session, user_id=user.id)
    if episode is None:
        await callback.answer(texts.NO_EPISODE, show_alert=True)
        return

    await callback.answer()
    await _run_turn(
        callback.message,
        session,
        llm,
        bot,
        episode=episode,
        user=user,
        text=texts.DUNNO_ANSWER,
        now=now,
    )


# --- конец разговора и разбор -------------------------------------------------


@router.message(Command("end"))
async def handle_end(message: Message, session: AsyncSession) -> None:
    """Закрыть эпизод по просьбе человека (FR-EP-6). Модель для этого не нужна."""
    if message.from_user is None:
        return

    user = await _current_user(session, message.from_user.id)
    now = dt.datetime.now(dt.UTC)
    episode = await episodes_db.get_open(session, user_id=user.id)
    if episode is None:
        await message.answer(texts.NO_EPISODE)
        return

    outcomes = await dialog.close_by_request(session, episode=episode, user_id=user.id, now=now)
    await message.answer(texts.EPISODE_ENDED)
    await message.answer(
        await _outcomes_block(session, episode=episode, outcomes=outcomes),
        reply_markup=keyboards.after_episode(episode.id),
    )


async def _episode_for_analysis(
    session: AsyncSession, *, user_id: int, episode_id: int
) -> Episode | None:
    """Какой разговор разбирать: названный кнопкой, идущий или последний."""
    if episode_id:
        episode = await session.get(Episode, episode_id)
        return episode if episode is not None and episode.user_id == user_id else None
    current = await episodes_db.get_open(session, user_id=user_id)
    return current or await episodes_db.get_last_closed(session, user_id=user_id)


@router.callback_query(keyboards.EpisodeAction.filter(F.action == "analysis"))
async def handle_analysis(
    callback: CallbackQuery,
    callback_data: keyboards.EpisodeAction,
    session: AsyncSession,
) -> None:
    """Разбор по всему эпизоду — и во время разговора, и после (FR-CHK-2)."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    user = await _current_user(session, callback.from_user.id)
    episode = await _episode_for_analysis(
        session, user_id=user.id, episode_id=callback_data.episode_id
    )
    if episode is None:
        await callback.answer(texts.NO_EPISODE, show_alert=True)
        return

    turns = await episodes_db.list_turns(session, episode_id=episode.id)
    known = await packs.lemmas(session, word_ids=episode.target_word_ids)
    screen = render.analysis_screen(
        [
            render.AnalysisTurn(
                user_text=turn.user_text,
                analysis_ru=turn.analysis_ru,
                corrections=turn.corrections,
            )
            for turn in turns
        ],
        lemmas=known,
    )
    await callback.answer()
    await callback.message.answer(screen)


# --- перевод реплики ----------------------------------------------------------

#: Сколько ждём перевод. Своё ограничение, потому что `answerCallbackQuery`
#: Telegram принимает секунд 15–30 после нажатия, а `llm_timeout_s` с ретраями
#: даёт минуты: без потолка ответ приезжал бы в просроченное нажатие, и кнопка
#: крутилась бы до конца, ничего не показав.
TRANSLATE_TIMEOUT_S = 8.0

#: Предел текста всплывающего окна в Telegram.
ALERT_LIMIT = 200


def _fit_alert(text: str) -> str:
    """Подрезать перевод под окно.

    Нужно только когда сообщение в чат недоступно, — обычно длинный перевод
    уходит именно сообщением. Обрезанный текст всё равно лучше пустого окна.
    """
    if len(text) <= ALERT_LIMIT:
        return text
    return text[: ALERT_LIMIT - 1].rstrip() + "…"


async def _translation(
    session: AsyncSession, llm: LlmClient, *, episode: Episode, turn: Turn
) -> str | None:
    """Перевод реплики — из кэша или у модели. `None` — не получилось.

    Контекст берём минимальный: сцена и предыдущая реплика человека. Ответ Ника
    часто и есть переделанная фраза ученика («рекаст»), а в отрыве от неё
    перевод получается про другое.
    """
    if turn.bot_text_ru:
        return turn.bot_text_ru

    # Реплика хода `N` прозвучала после ответа человека в ходе `N-1`: свой
    # `user_text` тот же ход получает уже позже, отвечая на эту реплику.
    previous = (
        await episodes_db.get_turn(session, episode_id=episode.id, idx=turn.idx - 1)
        if turn.idx
        else None
    )

    try:
        async with asyncio.timeout(TRANSLATE_TIMEOUT_S):
            result = await translate_llm.translate(
                llm,
                text=turn.bot_text,
                scene=episode.scene or "",
                user_text=previous.user_text if previous is not None else None,
            )
    except (LlmError, TimeoutError) as error:
        log.warning("R6: перевод хода %s не удался (%s)", turn.id, error)
        return None

    if not result.ru:
        # Пустое в `bot_text_ru` не пишем: `NULL` там значит «ещё не
        # переводили», и кнопка должна остаться работающей.
        return None
    return await episodes_db.save_translation(session, turn=turn, ru=result.ru)


@router.callback_query(keyboards.TranslateAction.filter())
async def handle_translate(
    callback: CallbackQuery,
    callback_data: keyboards.TranslateAction,
    session: AsyncSession,
    llm: LlmClient,
) -> None:
    """`🇷🇺 Перевод` — что Ник сказал этой репликой, по-русски (FR-CHK-7).

    Всплывающим окном, а не сообщением: перевод читают один раз и на месте,
    а переписка должна остаться греческой.

    `callback.message` здесь нарочно не проверяется: через 48 часов Telegram
    подменяет его недоступным — то есть ровно тогда, когда человек листает
    переписку назад и хочет перевод. Окну сообщение не нужно.
    """
    if callback.from_user is None:
        await callback.answer()
        return

    user = await _current_user(session, callback.from_user.id)
    episode = await session.get(Episode, callback_data.episode_id)
    if episode is None or episode.user_id != user.id:
        # Данные кнопки подделываются, а эпизод чужой отдавать нельзя.
        await callback.answer(texts.NO_EPISODE, show_alert=True)
        return

    turn = await episodes_db.get_turn(session, episode_id=episode.id, idx=callback_data.turn_idx)
    if turn is None:
        # То самое окно NFR-1: сообщение отправлено, а процесс умер до записи
        # хода. В переписке кнопка есть, переводить нечего.
        await callback.answer(texts.TRANSLATE_NO_TURN, show_alert=True)
        return

    ru = await _translation(session, llm, episode=episode, turn=turn)
    if ru is None:
        await callback.answer(texts.TRANSLATE_FAILED, show_alert=True)
        return

    if len(ru) > ALERT_LIMIT and isinstance(callback.message, Message):
        await callback.answer()
        # Тут, в отличие от окна, экранирование обязательно: сообщения уходят
        # при `ParseMode.HTML`, и одна `<` в переводе даёт 400.
        await callback.message.answer(render.plain(ru))
        return

    try:
        # Текст окна Telegram как HTML не разбирает — экранировать нельзя,
        # иначе в окне будут литеральные `&quot;`.
        await callback.answer(_fit_alert(ru), show_alert=True)
    except TelegramBadRequest:
        # Нажатие просрочено, окна уже не будет. Перевод всё равно нужен.
        if isinstance(callback.message, Message):
            await callback.message.answer(render.plain(ru))


# --- подсказка «что ответить» -------------------------------------------------

#: Сколько ждём подсказку. Потолок свой, а не `llm_timeout_s` с ретраями:
#: человек застрял на вопросе и ждёт ответа сейчас, а не через две минуты —
#: столько он молча смотрит в экран и решает, что бот сломался.
HINT_TIMEOUT_S = 12.0


async def _hint(
    session: AsyncSession, llm: LlmClient, *, episode: Episode, turn: Turn, user: User
) -> str | None:
    """Подсказка к реплике — из кэша или у модели. `None` — не получилось.

    Готовым текстом сообщения: разбирать кэш обратно на варианты незачем, а
    собрать его надо один раз.
    """
    if turn.hint_ru:
        return turn.hint_ru

    words = await dialog.words_to_practise(session, episode)
    if not words:
        # Слова удалили вместе с пачкой посреди разговора: подсказывать нечем,
        # а звать модель с пустым списком — платить за пустоту.
        return None

    # Реплика хода `N` прозвучала после ответа человека в ходе `N-1` — тот же
    # довод, что и у перевода: без него ломаются рекасты и эллипсисы.
    previous = (
        await episodes_db.get_turn(session, episode_id=episode.id, idx=turn.idx - 1)
        if turn.idx
        else None
    )

    try:
        async with asyncio.timeout(HINT_TIMEOUT_S):
            result = await hint_llm.suggest(
                llm,
                question=turn.bot_text,
                words=words,
                level=Level(user.level),
                scene=episode.scene or "",
                user_text=previous.user_text if previous is not None else None,
            )
    except (LlmError, TimeoutError) as error:
        log.warning("R7: подсказка к ходу %s не удалась (%s)", turn.id, error)
        return None

    if not result.options:
        # Пустое в `hint_ru` не пишем: `NULL` там значит «не спрашивали», и
        # кнопка должна остаться работающей.
        return None

    lemmas = await packs.lemmas(session, word_ids=episode.target_word_ids)
    text = render.hint_block(
        [
            render.Hint(
                ru=option.ru,
                words=[lemmas[word_id] for word_id in option.word_ids if word_id in lemmas],
            )
            for option in result.options
        ]
    )
    return await episodes_db.save_hint(session, turn=turn, text=text)


@router.callback_query(keyboards.HintAction.filter())
async def handle_hint(
    callback: CallbackQuery,
    callback_data: keyboards.HintAction,
    session: AsyncSession,
    llm: LlmClient,
    bot: Bot,
) -> None:
    """`💡 Что ответить` — 2–3 варианта по-русски (FR-CHK-8).

    Сообщением, а не окном: подсказку читают, пока набирают ответ, — а окно
    исчезает от первого касания клавиатуры. Нажатие поэтому закрываем сразу,
    не дожидаясь модели: спиннер всё равно ничего не покажет.
    """
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    user = await _current_user(session, callback.from_user.id)
    episode = await session.get(Episode, callback_data.episode_id)
    if episode is None or episode.user_id != user.id:
        # Данные кнопки подделываются, а чужой разговор отдавать нельзя.
        await callback.answer(texts.NO_EPISODE, show_alert=True)
        return

    turn = await episodes_db.get_turn(session, episode_id=episode.id, idx=callback_data.turn_idx)
    if turn is None:
        # То самое окно NFR-1: сообщение отправлено, а процесс умер до записи
        # хода. Кнопка в переписке есть, подсказывать нечему.
        await callback.answer(texts.HINT_NO_TURN, show_alert=True)
        return

    if turn.user_text is not None and not turn.hint_ru:
        # Кнопка из истории: на этот вопрос уже ответили, и разговор ушёл
        # вперёд. Кэш бы показали, а вот платить за подсказку в прошлое незачем.
        await callback.answer()
        await callback.message.answer(texts.HINT_ANSWERED)
        return

    await callback.answer()
    async with ChatActionSender.typing(bot=bot, chat_id=callback.message.chat.id):
        text = await _hint(session, llm, episode=episode, turn=turn, user=user)

    await callback.message.answer(text if text is not None else texts.HINT_FAILED)


__all__ = ["router"]
