"""Отправщик: разбирает созревшие события расписания (FR-SCH-3).

Тик — полминуты. Точность выше не нужна: человек не заметит разницы между
15:20 и 15:20:30, а вот опоздание на минуты уже видно, поэтому реже нельзя.

Модель здесь не зовётся ни разу. Первая реплика придумана ночью и лежит в
эпизоде, пинок написан заранее в персоне, — отправщик только достаёт готовый
текст. Это нарочно: процесс, который пишет человеку по часам, должен быть
самым скучным и самым надёжным в системе.

События берутся по одному, каждое в своей транзакции. Пачкой было бы меньше
запросов, но `claim` держит строки блокировкой до конца транзакции, а коммит
после первой же отправки снял бы её и с остальных — и их успел бы забрать
второй отправщик.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from siga import dialog
from siga.bot import create_bot, keyboards, render
from siga.config import Settings
from siga.core.enums import EventStatus
from siga.db import create_engine, create_session_factory
from siga.db import episodes as episodes_db
from siga.db import schedule as schedule_db
from siga.db.models import User
from siga.db.session import SessionFactory
from siga.logging import setup_logging
from siga.service import load_settings, stop_event, tick_forever

log = logging.getLogger(__name__)

#: Как часто заглядываем в очередь, FR-SCH-3.
TICK = dt.timedelta(seconds=30)

#: Сколько событий разбираем за тик. Потолок нужен на случай, когда отправщик
#: долго лежал: вывалить человеку разом всё пропущенное нельзя, а событиям
#: старше полутора часов всё равно предстоит истечь (FR-SCH-4).
BATCH = 20


async def deliver_one(session_factory: SessionFactory, bot: Bot, *, now: dt.datetime) -> bool:
    """Взять одно созревшее событие и довести его до конца.

    Порядок жёсткий: решаем → отправляем в Telegram → пишем в базу. Всё, что
    пишется, пишется одним коммитом уже после успешной отправки (NFR-1).
    Если процесс умрёт между отправкой и коммитом, человек в худшем случае
    получит одно сообщение дважды; обратный порядок терял бы разговор молча,
    а это хуже.

    Возвращает, было ли что разбирать, — по этому отправщик понимает, что
    очередь кончилась и на этом тике можно остановиться.
    """
    async with session_factory() as session:
        events = await schedule_db.claim(session, now=now, limit=1)
        if not events:
            return False
        event = events[0]

        user = await session.get(User, event.user_id)
        if user is None:  # pragma: no cover — только при удалении между тиками
            schedule_db.mark(event, status=EventStatus.SKIPPED, now=now)
            await session.commit()
            return True

        delivery = await dialog.prepare(session, event=event, user=user, now=now)

        if delivery.text:
            try:
                # Реплика идёт теми же двумя правилами, что и в чате: текст
                # экранируется (бот живёт в `ParseMode.HTML`, и одна `<` в
                # реплике модели иначе даёт `TelegramBadRequest`, которого нет
                # в списке ниже), а под эпизодом висят кнопки. Пинок — не
                # реплика разговора, и кнопок под ним быть не должно.
                await bot.send_message(
                    user.tg_user_id,
                    render.plain(delivery.text),
                    reply_markup=(
                        keyboards.reply_actions(
                            episode_id=delivery.episode.id, turn_idx=keyboards.OPENING_TURN_IDX
                        )
                        if delivery.episode is not None
                        else None
                    ),
                )
            except TelegramForbiddenError:
                # Человек заблокировал бота. Событие закрываем — иначе оно
                # будет проситься наружу до самой просрочки.
                log.info("пользователь %s заблокировал бота", user.id)
                await dialog.settle(
                    session,
                    event=event,
                    delivery=dialog.Delivery(EventStatus.SKIPPED, episode=delivery.episode),
                    now=now,
                )
                return True
            except (TelegramRetryAfter, TimeoutError, OSError) as exc:
                # Не наша ошибка и не решение. Откатываемся, событие остаётся
                # ожидающим: следующий тик через полминуты, а через полтора
                # часа оно истечёт само.
                log.warning("не отправилось событие %s: %s", event.id, exc)
                await session.rollback()
                return True

        await dialog.settle(session, event=event, delivery=delivery, now=now)
        log.info(
            "событие %s (%s) → %s, пользователь %s",
            event.id,
            event.type,
            delivery.status,
            user.id,
        )
        return True


async def deliver_due(session_factory: SessionFactory, bot: Bot, *, now: dt.datetime) -> int:
    """Разобрать очередь до пустоты или до потолка пачки."""
    handled = 0
    while handled < BATCH:
        if not await deliver_one(session_factory, bot, now=now):
            break
        handled += 1
    return handled


async def sweep(session_factory: SessionFactory, *, now: dt.datetime) -> int:
    """Развести по домам эпизоды, до которых человек так и не дошёл (FR-SCH-6).

    Живёт в отправщике, а не отдельным процессом: TTL — это тот же разговор по
    часам, только с другого конца, и держать ради него третий контейнер незачем.
    """
    async with session_factory() as session:
        stale = await episodes_db.abandon_stale(session, now=now)
    if stale:
        log.info("закрыто по TTL эпизодов: %s", len(stale))
    return len(stale)


async def tick(session_factory: SessionFactory, bot: Bot, *, now: dt.datetime) -> None:
    await sweep(session_factory, now=now)
    await deliver_due(session_factory, bot, now=now)


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    bot = create_bot(settings)
    stop = stop_event()

    log.info("отправщик запущен, тик %s с", int(TICK.total_seconds()))
    try:
        await tick_forever(
            name="отправщик",
            interval=TICK.total_seconds(),
            step=lambda: tick(session_factory, bot, now=dt.datetime.now(dt.UTC)),
            stop=stop,
        )
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(load_settings()))


if __name__ == "__main__":  # python -m siga.workers.sender
    main()
