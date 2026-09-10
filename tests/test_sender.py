"""Отправщик: что именно уезжает в Telegram по расписанию.

Что событие превращается в открытый эпизод и вооружённый пинок, проверено в
`test_schedule_db` — там всё, что происходит в базе. Здесь только сама
отправка: реплика по расписанию должна выглядеть в переписке так же, как
заведённая руками через `/next`, — с кнопками и с экранированным текстом.
Через `dialog.prepare` этого не увидеть: `Delivery` не знает ни про
клавиатуру, ни про HTML.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from typing import Any

import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from siga import dialog
from siga.core.enums import EventType, WordSource
from siga.core.wordlist import parse_wordlist
from siga.db import packs, users
from siga.db import schedule as schedule_db
from siga.db.models import ScheduledEvent, User
from siga.workers import sender
from tests.fake_llm import ScriptedClient
from tests.fake_telegram import FAKE_TOKEN, RecordingSession

#: Полночь UTC — 03:00 на Кипре, час ночного пересчёта (FR-SCH-2).
NOW = dt.datetime(2026, 9, 10, 0, 0, tzinfo=dt.UTC)
DAY = dt.date(2026, 9, 10)

WORDS = """
το νερό — вода
ο καφές — кофе
το γάλα — молоко
το ψωμί — хлеб
το τυρί — сыр
η ζάχαρη — сахар
"""


@pytest_asyncio.fixture
async def bot() -> Bot:
    """Настоящий `Bot` без сети: сессия складывает вызовы в список."""
    return Bot(
        token=FAKE_TOKEN,
        session=RecordingSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def sent(bot: Bot) -> list[Any]:
    """Отправленные сообщения — то, что человек увидел бы в переписке."""
    session = bot.session
    assert isinstance(session, RecordingSession)
    return [call for call in session.calls if type(call).__name__ == "SendMessage"]


def frames(count: int) -> str:
    """Ответ `R3` на `count` групп — по рамке на каждую."""
    return json.dumps(
        {
            "episodes": [
                {
                    "index": index,
                    "scene": f"Сцена {index}",
                    "goal": "разговориться",
                    "plan": ["позвать", "спросить"],
                    "opening": f"Καλημέρα! Ερώτηση {index};",
                }
                for index in range(count)
            ]
        }
    )


async def _planned_day(session: AsyncSession, *, count: int = 2) -> tuple[User, dialog.DayPlan]:
    """Человек с активной пачкой, широким окном и расписанным днём."""
    user, _ = await users.get_or_create(session, tg_user_id=777)
    user.tz = "Asia/Nicosia"
    user.window_start = dt.time(10, 0)
    user.window_end = dt.time(22, 0)
    user.episodes_per_day = count

    pack = await packs.create_draft(
        session,
        user_id=user.id,
        title="Кухня",
        source_type=WordSource.TEXT,
        raw_text=WORDS,
        words=parse_wordlist(WORDS).words,
    )
    await packs.activate(session, pack=pack, period_days=14, now=NOW)

    plan = await dialog.plan_day(
        session,
        ScriptedClient(frames(count)),
        user=user,
        day=DAY,
        now=NOW,
        rng=random.Random(1),
    )
    assert plan.events, "день должен быть расписан"
    return user, plan


async def _ripe_nudge(session: AsyncSession, user: User, plan: dialog.DayPlan) -> ScheduledEvent:
    """Разговор начат, ответа нет — пинок созрел."""
    event = plan.events[0]
    delivery = await dialog.prepare(session, event=event, user=user, now=event.fire_at)
    await dialog.settle(session, event=event, delivery=delivery, now=event.fire_at)

    pending = await schedule_db.pending_at(session, user_id=user.id, since=event.fire_at)
    nudges = [candidate for candidate in pending if candidate.type == EventType.NUDGE]
    assert len(nudges) == 1
    return nudges[0]


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_a_scheduled_reply_arrives_with_its_buttons(
    session: AsyncSession, engine: AsyncEngine, bot: Bot
) -> None:
    """Иначе у пришедшего по расписанию эпизода нет ни разбора, ни «не знаю»."""
    _, plan = await _planned_day(session)

    assert await sender.deliver_one(_sessions(engine), bot, now=plan.events[0].fire_at)

    calls = sent(bot)
    assert len(calls) == 1
    markup = calls[0].reply_markup
    assert markup is not None
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert any(label.endswith("Разбор") for label in labels)
    assert any(label.endswith("Не знаю") for label in labels)


async def test_a_nudge_arrives_without_buttons(
    session: AsyncSession, engine: AsyncEngine, bot: Bot
) -> None:
    """Пинок — не реплика разговора, а стук в дверь: разбирать в нём нечего."""
    user, plan = await _planned_day(session)
    event = await _ripe_nudge(session, user, plan)

    assert await sender.deliver_one(_sessions(engine), bot, now=event.fire_at)

    calls = sent(bot)
    assert len(calls) == 1
    assert calls[0].reply_markup is None


async def test_an_angle_bracket_in_the_reply_does_not_break_the_send(
    session: AsyncSession, engine: AsyncEngine, bot: Bot
) -> None:
    """Бот живёт в `ParseMode.HTML`, а реплику писала модель.

    Неэкранированная `<` дала бы `TelegramBadRequest`, которого нет в списке
    перехвата: событие осталось бы ожидающим и ретраилось каждые полминуты
    до самой просрочки.
    """
    _, plan = await _planned_day(session)
    plan.episodes[0].opening_text = "Πάμε <για> καφέ;"
    await session.commit()

    assert await sender.deliver_one(_sessions(engine), bot, now=plan.events[0].fire_at)

    assert sent(bot)[0].text == "Πάμε &lt;για&gt; καφέ;"


async def test_an_empty_queue_is_recognised_as_empty(
    session: AsyncSession, engine: AsyncEngine, bot: Bot
) -> None:
    """По этому отправщик понимает, что на тике больше делать нечего."""
    await _planned_day(session)

    assert not await sender.deliver_one(_sessions(engine), bot, now=NOW)
    assert sent(bot) == []


async def test_a_second_conversation_is_not_sent_on_top_of_the_first(
    session: AsyncSession, engine: AsyncEngine, bot: Bot
) -> None:
    """FR-SCH-5: оба события созрели, а сообщение уходит одно."""
    _, plan = await _planned_day(session)
    late = plan.events[-1].fire_at

    handled = await sender.deliver_due(_sessions(engine), bot, now=late)

    assert handled == 2, "разобраны оба события"
    assert len(sent(bot)) == 1, "отправлено одно"
