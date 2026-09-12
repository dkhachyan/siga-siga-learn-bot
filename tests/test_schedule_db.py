"""Расписание против настоящего Postgres: план дня, доставка, пауза.

Арифметика слотов проверена в `test_schedule` без базы. Здесь — то, что
существует только вместе с хранилищем: что план дня стоит одного вызова
модели, что два отправщика не заберут одно событие, что несостоявшаяся
отправка возвращает слова в пул, а пауза сдвигает срок пачки.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from siga import dialog
from siga.core import schedule as clock
from siga.core.enums import EpisodeStatus, EventStatus, EventType, WordSource
from siga.core.wordlist import parse_wordlist
from siga.db import episodes as episodes_db
from siga.db import packs, progress, users
from siga.db import schedule as schedule_db
from siga.db.models import Pack, ScheduledEvent, User, Word
from tests.fake_llm import ScriptedClient

NICOSIA = ZoneInfo("Asia/Nicosia")

#: Полночь UTC — 03:00 на Кипре, час ночного пересчёта (FR-SCH-2).
NOW = dt.datetime(2026, 9, 10, 0, 0, tzinfo=dt.UTC)
DAY = dt.date(2026, 9, 10)

NINE_WORDS = """
το νερό — вода
ο καφές — кофе
το γάλα — молоко
το ψωμί — хлеб
το τυρί — сыр
η ζάχαρη — сахар
το αλάτι — соль
το λάδι — масло
το αυγό — яйцо
"""


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


async def _person(session: AsyncSession, *, episodes_per_day: int = 3) -> tuple[User, Pack]:
    """Человек с активной пачкой, кипрским поясом и широким окном."""
    user, _ = await users.get_or_create(session, tg_user_id=777)
    user.tz = "Asia/Nicosia"
    user.window_start = dt.time(10, 0)
    user.window_end = dt.time(22, 0)
    user.episodes_per_day = episodes_per_day

    pack = await packs.create_draft(
        session,
        user_id=user.id,
        title="Кухня",
        source_type=WordSource.TEXT,
        raw_text=NINE_WORDS,
        words=parse_wordlist(NINE_WORDS).words,
    )
    await packs.activate(session, pack=pack, period_days=14, now=NOW)
    return user, pack


async def _plan(
    session: AsyncSession, user: User, *, count: int = 3, seed: int = 1
) -> tuple[dialog.DayPlan, ScriptedClient]:
    llm = ScriptedClient(frames(count))
    plan = await dialog.plan_day(session, llm, user=user, day=DAY, now=NOW, rng=random.Random(seed))
    return plan, llm


# --- план дня -----------------------------------------------------------------


async def test_the_whole_day_costs_one_model_call(session: AsyncSession) -> None:
    """FR-SCH-2: рамки на день делаются одним вызовом `R3`, а не по одному."""
    user, _ = await _person(session)

    plan, llm = await _plan(session, user)

    assert llm.calls == 1
    assert len(plan.episodes) == 3
    assert len(plan.events) == 3


async def test_planned_moments_land_inside_the_window(session: AsyncSession) -> None:
    user, _ = await _person(session)

    plan, _ = await _plan(session, user)

    moments = [event.fire_at for event in plan.events]
    assert moments == sorted(moments)
    for moment in moments:
        assert moment >= NOW, "слот, который уже прошёл, ставить незачем"
        assert clock.is_within_window(
            moment, tz=NICOSIA, start=user.window_start, end=user.window_end
        )


async def test_every_conversation_of_the_day_gets_its_own_words(session: AsyncSession) -> None:
    """Иначе все эпизоды дня взяли бы одну и ту же верхушку списка."""
    user, _ = await _person(session)

    plan, _ = await _plan(session, user)

    groups = [set(episode.target_word_ids) for episode in plan.episodes]
    assert all(groups)
    for index, group in enumerate(groups):
        for other in groups[index + 1 :]:
            assert not group & other


async def test_the_day_is_not_walked_through_in_pack_order(session: AsyncSession) -> None:
    """Все новые слова между собой равны, и хранить их очередь по номерам —
    значит кормить одними и теми же первыми словами каждый день подряд."""
    user, _ = await _person(session)

    plan, _ = await _plan(session, user)

    order = [word_id for episode in plan.episodes for word_id in episode.target_word_ids]
    assert sorted(order) == list(range(1, 10)), "ни одно слово дня не потерялось"
    assert order != [1, 2, 3, 4, 5, 6, 7, 8, 9], "порядок перемешан, а не по номерам"


async def test_a_day_without_words_is_not_planned(session: AsyncSession) -> None:
    """Пачки нет — расписывать нечего, и модель не зовём."""
    user, _ = await users.get_or_create(session, tg_user_id=778)
    llm = ScriptedClient()

    plan = await dialog.plan_day(session, llm, user=user, day=DAY, now=NOW)

    assert plan.episodes == []
    assert llm.calls == 0


async def test_a_paused_person_is_not_planned(session: AsyncSession) -> None:
    user, _ = await _person(session)
    await users.pause(session, user=user, now=NOW)
    llm = ScriptedClient()

    plan = await dialog.plan_day(session, llm, user=user, day=DAY, now=NOW)

    assert plan.events == []
    assert llm.calls == 0


async def test_a_planned_day_is_recognised_as_planned(session: AsyncSession) -> None:
    """Ночной пересчёт идемпотентен: второй раз тот же день не расписывается."""
    user, _ = await _person(session)
    opens, closes = clock.window_bounds(
        DAY, tz=NICOSIA, start=user.window_start, end=user.window_end
    )

    assert not await schedule_db.has_plan_for(session, user_id=user.id, since=opens, until=closes)
    await _plan(session, user)
    assert await schedule_db.has_plan_for(session, user_id=user.id, since=opens, until=closes)


# --- выборка событий ----------------------------------------------------------


async def test_only_ripe_events_are_claimed(session: AsyncSession) -> None:
    user, _ = await _person(session)
    plan, _ = await _plan(session, user)

    claimed = await schedule_db.claim(session, now=plan.events[0].fire_at, limit=10)

    assert [event.id for event in claimed] == [plan.events[0].id]


async def test_a_claimed_event_is_invisible_to_the_second_sender(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """FOR UPDATE SKIP LOCKED (FR-SCH-3): два отправщика не шлют одно дважды."""
    user, _ = await _person(session)
    plan, _ = await _plan(session, user)
    ripe = plan.events[-1].fire_at

    first = await schedule_db.claim(session, now=ripe, limit=10)
    assert first, "первый отправщик должен что-то забрать"

    other = async_sessionmaker(engine, expire_on_commit=False)
    async with other() as rival:
        second = await schedule_db.claim(rival, now=ripe, limit=10)
        assert second == [], "занятые строки второй отправщик обходит"

    await session.rollback()


# --- доставка -----------------------------------------------------------------


async def test_sending_opens_the_episode_and_arms_the_nudge(session: AsyncSession) -> None:
    user, _ = await _person(session)
    plan, _ = await _plan(session, user)
    event, episode = plan.events[0], plan.episodes[0]

    delivery = await dialog.prepare(session, event=event, user=user, now=event.fire_at)
    await dialog.settle(session, event=event, delivery=delivery, now=event.fire_at)

    assert delivery.status == EventStatus.SENT
    assert delivery.text == episode.opening_text
    assert episode.status == EpisodeStatus.OPEN
    assert event.status == EventStatus.SENT

    turns = await episodes_db.list_turns(session, episode_id=episode.id)
    assert [turn.idx for turn in turns] == [0]

    nudges = [
        pending
        for pending in await schedule_db.pending_at(session, user_id=user.id, since=event.fire_at)
        if pending.type == EventType.NUDGE
    ]
    assert len(nudges) == 1
    assert nudges[0].fire_at == event.fire_at + clock.NUDGE_AFTER


async def test_a_missed_slot_gives_its_words_back(session: AsyncSession) -> None:
    """FR-SCH-4: до человека не дошло — значит, он ни при чём."""
    user, _ = await _person(session)
    plan, _ = await _plan(session, user)
    event, episode = plan.events[0], plan.episodes[0]
    late = event.fire_at + clock.MAX_DELAY + dt.timedelta(minutes=1)

    delivery = await dialog.prepare(session, event=event, user=user, now=late)
    await dialog.settle(session, event=event, delivery=delivery, now=late)

    assert delivery.status == EventStatus.EXPIRED
    assert episode.status == EpisodeStatus.ABANDONED
    for word_id in episode.target_word_ids:
        row = await progress.get(session, word_id=word_id)
        assert row is not None
        assert row.box == 0, "бокс не наказан"
        assert row.next_due_at == late, "слово вернулось в пул"


async def test_a_closed_window_holds_the_message_back(session: AsyncSession) -> None:
    """Человек сузил окно после ночной раскладки — старый слот не срабатывает."""
    user, _ = await _person(session)
    plan, _ = await _plan(session, user)
    event = plan.events[0]
    user.window_start = dt.time(20, 0)
    user.window_end = dt.time(22, 0)

    delivery = await dialog.prepare(session, event=event, user=user, now=event.fire_at)

    assert delivery.status == EventStatus.SKIPPED


async def test_only_one_conversation_at_a_time(session: AsyncSession) -> None:
    """FR-SCH-5: разговор уже идёт — второй поверх него не начинается."""
    user, _ = await _person(session)
    plan, _ = await _plan(session, user)
    first, second = plan.events[0], plan.events[1]

    opening = await dialog.prepare(session, event=first, user=user, now=first.fire_at)
    await dialog.settle(session, event=first, delivery=opening, now=first.fire_at)

    delivery = await dialog.prepare(session, event=second, user=user, now=second.fire_at)

    assert delivery.status == EventStatus.SKIPPED
    assert plan.episodes[1].status == EpisodeStatus.PENDING


# --- пинок --------------------------------------------------------------------


async def _nudge_event(session: AsyncSession, user: User) -> ScheduledEvent:
    """Разговор начат и остался без ответа; пинок созрел."""
    plan, _ = await _plan(session, user)
    event, episode = plan.events[0], plan.episodes[0]
    delivery = await dialog.prepare(session, event=event, user=user, now=event.fire_at)
    await dialog.settle(session, event=event, delivery=delivery, now=event.fire_at)

    nudges = [
        pending
        for pending in await schedule_db.pending_at(session, user_id=user.id, since=event.fire_at)
        if pending.type == EventType.NUDGE
    ]
    assert schedule_db.episode_id_of(nudges[0]) == episode.id
    return nudges[0]


async def test_silence_earns_a_nudge(session: AsyncSession) -> None:
    user, _ = await _person(session)
    event = await _nudge_event(session, user)

    delivery = await dialog.prepare(session, event=event, user=user, now=event.fire_at)

    assert delivery.status == EventStatus.SENT
    assert delivery.text
    assert delivery.episode is None, "пинок не открывает и не закрывает эпизод"


async def test_a_nudge_is_not_written_into_the_turns(session: AsyncSession) -> None:
    """Иначе стук в дверь съел бы один из четырёх ходов эпизода (§5.5)."""
    user, _ = await _person(session)
    event = await _nudge_event(session, user)
    episode_id = schedule_db.episode_id_of(event)
    assert episode_id is not None

    delivery = await dialog.prepare(session, event=event, user=user, now=event.fire_at)
    await dialog.settle(session, event=event, delivery=delivery, now=event.fire_at)

    turns = await episodes_db.list_turns(session, episode_id=episode_id)
    assert [turn.idx for turn in turns] == [0]


async def test_an_answered_conversation_is_not_nudged(session: AsyncSession) -> None:
    user, _ = await _person(session)
    event = await _nudge_event(session, user)
    episode_id = schedule_db.episode_id_of(event)
    assert episode_id is not None
    turn = await episodes_db.last_turn(session, episode_id=episode_id)
    assert turn is not None
    await episodes_db.record_answer(
        session, turn=turn, user_text="Καλημέρα!", assessments=[], corrections=[], now=NOW
    )

    delivery = await dialog.prepare(session, event=event, user=user, now=event.fire_at)

    assert delivery.status == EventStatus.SKIPPED


# --- пауза --------------------------------------------------------------------


async def test_pause_clears_the_rest_of_the_day(session: AsyncSession) -> None:
    user, _ = await _person(session)
    plan, _ = await _plan(session, user)

    await users.pause(session, user=user, now=NOW)
    cleared = await dialog.clear_day(session, user_id=user.id, now=NOW)

    assert cleared == len(plan.events)
    assert all(event.status == EventStatus.SKIPPED for event in plan.events)
    assert all(episode.status == EpisodeStatus.ABANDONED for episode in plan.episodes)
    assert await schedule_db.pending_at(session, user_id=user.id, since=NOW) == []


async def test_resume_gives_the_period_its_days_back(session: AsyncSession) -> None:
    """FR-SCH-7: простой не должен съедать срок пачки."""
    user, pack = await _person(session)
    ends_at = pack.ends_at
    assert ends_at is not None

    await users.pause(session, user=user, now=NOW)
    days = await users.resume(session, user=user, now=NOW + dt.timedelta(days=3))

    assert days == 3
    assert user.paused_at is None
    assert pack.paused_days == 3
    assert pack.ends_at == ends_at + dt.timedelta(days=3)


async def test_resume_without_a_pause_changes_nothing(session: AsyncSession) -> None:
    user, pack = await _person(session)
    ends_at = pack.ends_at

    assert await users.resume(session, user=user, now=NOW) == 0
    assert pack.ends_at == ends_at


async def test_only_the_unpaused_are_scheduled(session: AsyncSession) -> None:
    user, _ = await _person(session)
    stranger, _ = await users.get_or_create(session, tg_user_id=779)

    assert [person.id for person in await users.scheduled(session)] == [user.id]

    await users.pause(session, user=user, now=NOW)
    assert await users.scheduled(session) == []
    assert stranger.id not in {person.id for person in await users.scheduled(session)}


# --- отбор слов ---------------------------------------------------------------


async def test_words_that_ripen_after_the_window_wait_for_tomorrow(
    session: AsyncSession,
) -> None:
    """Слово, срок которого наступит ночью, сегодняшнему дню не принадлежит."""
    user, pack = await _person(session)
    words: list[Word] = await packs.list_words(session, pack_id=pack.id)
    tomorrow = NOW + dt.timedelta(days=1)
    for word in words[:-1]:
        row = await progress.get(session, word_id=word.id)
        assert row is not None
        row.next_due_at = tomorrow
    await session.commit()

    _, closes = clock.window_bounds(DAY, tz=NICOSIA, start=user.window_start, end=user.window_end)
    groups = await progress.pick_groups(
        session, pack_id=pack.id, now=NOW, until=closes, groups=3, size=3
    )

    assert [word.id for group in groups for word, _ in group] == [words[-1].id]
