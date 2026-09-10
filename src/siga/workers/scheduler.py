"""Планировщик: раз в сутки собирает человеку день (FR-SCH-2).

Просыпается часто, а работает редко: тик десять минут, но день собирается
только у того, у кого сейчас три часа ночи и на сегодня ещё ничего не
расписано. Так один процесс обслуживает любые часовые пояса, не заводя по
таймеру на пользователя, и переживает рестарт: проснувшись в 03:40 вместо
03:00, он всё ещё попадает в свой час.

Почему ночью, а не утром перед первым эпизодом: `R3` на весь день — самый
долгий вызов в системе, и ждать его в момент, когда человеку уже пора писать,
значило бы отдавать эту задержку человеку.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging

from siga import dialog
from siga.config import Settings
from siga.core import schedule as clock
from siga.db import create_engine, create_session_factory
from siga.db import schedule as schedule_db
from siga.db import users as users_db
from siga.db.models import User
from siga.db.session import SessionFactory
from siga.llm.base import LlmClient
from siga.llm.factory import create_llm_client
from siga.logging import setup_logging
from siga.service import load_settings, stop_event, tick_forever

log = logging.getLogger(__name__)

#: Как часто планировщик оглядывается по сторонам. Меньше часа — чтобы попасть
#: в час планирования; не каждую минуту — потому что делать ему почти всегда
#: нечего, а лишний запрос к базе раз в минуту живёт вечно.
TICK = dt.timedelta(minutes=10)


async def plan_user(
    session_factory: SessionFactory, llm: LlmClient, *, user_id: int, now: dt.datetime
) -> int:
    """Собрать день одному человеку, если ему сейчас пора. Вернёт число слотов."""
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None or user.paused_at is not None:
            return 0

        tz = clock.zone(user.tz)
        if not clock.is_planning_time(now, tz=tz):
            return 0

        day = clock.local_day(now, tz=tz)
        opens, closes = clock.window_bounds(
            day, tz=tz, start=user.window_start, end=user.window_end
        )
        if await schedule_db.has_plan_for(session, user_id=user.id, since=opens, until=closes):
            # Час планирования длиннее тика, и попасть в него можно несколько
            # раз. Второго плана на тот же день быть не должно.
            return 0

        plan = await dialog.plan_day(session, llm, user=user, day=day, now=now)
        return len(plan.events)


async def plan_everyone(
    session_factory: SessionFactory, llm: LlmClient, *, now: dt.datetime
) -> int:
    """Пройтись по всем, кому бот пишет, и расписать тех, чей час настал.

    Каждый человек — своя сессия и свой `try`: упавший `R3` у одного не должен
    оставить без расписания остальных, а день у них у всех сегодня один.
    """
    async with session_factory() as session:
        user_ids = [user.id for user in await users_db.scheduled(session)]

    planned = 0
    for user_id in user_ids:
        try:
            planned += await plan_user(session_factory, llm, user_id=user_id, now=now)
        except Exception:
            log.exception("не смог расписать день пользователю %s", user_id)
    return planned


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    llm = create_llm_client(settings)
    stop = stop_event()

    log.info("планировщик запущен, тик %s мин", int(TICK.total_seconds() // 60))
    try:
        await tick_forever(
            name="планировщик",
            interval=TICK.total_seconds(),
            step=lambda: plan_everyone(session_factory, llm, now=dt.datetime.now(dt.UTC)),
            stop=stop,
        )
    finally:
        await llm.close()
        await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(load_settings()))


if __name__ == "__main__":  # python -m siga.workers.scheduler
    main()
