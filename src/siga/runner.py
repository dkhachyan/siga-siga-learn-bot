"""Запуск бота: long polling локально, вебхук на сервере."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from siga.bot import create_bot, create_dispatcher, set_bot_commands
from siga.config import Settings
from siga.db import create_engine, create_session_factory
from siga.llm.factory import create_llm_client
from siga.logging import setup_logging
from siga.service import load_settings, stop_event

log = logging.getLogger(__name__)


async def _health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _run_polling(bot: Bot, dp: Dispatcher) -> None:
    # висящий вебхук молча съедает апдейты, если раньше бот работал на сервере
    await bot.delete_webhook(drop_pending_updates=False)
    await set_bot_commands(bot)
    log.info("режим polling")
    await dp.start_polling(bot)


async def _run_webhook(bot: Bot, dp: Dispatcher, settings: Settings) -> None:
    secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else None
    stop = stop_event()

    await bot.set_webhook(
        settings.webhook_url,
        secret_token=secret,
        allowed_updates=dp.resolve_used_update_types(),
    )
    await set_bot_commands(bot)

    app = web.Application()
    app.router.add_get("/health", _health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(
        app, path=settings.webhook_path
    )
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    await site.start()
    log.info(
        "режим webhook: слушаю %s:%s, Telegram шлёт на %s",
        settings.webhook_host,
        settings.webhook_port,
        settings.webhook_url,
    )

    try:
        await stop.wait()
    finally:
        log.info("останавливаюсь")
        await runner.cleanup()


def _log_access(settings: Settings) -> None:
    """Сказать в журнал, кого бот пускает.

    Предупреждение, а не `info`: такую строку видно в любом хвосте журнала, и
    «я же закрывал» перестаёт быть догадкой.
    """
    if settings.allowed_tg_user_ids:
        # Номерами, а не числом: «я же его добавлял» перестаёт быть догадкой.
        # NFR-10 это позволяет — `tg_user_id` в журнале и так единственное,
        # чем человек в нём обозначается.
        log.info(
            "пускаю по ALLOWED_TG_USER_IDS: %s",
            ", ".join(str(number) for number in sorted(settings.allowed_tg_user_ids)),
        )
    else:
        log.warning(
            "ALLOWED_TG_USER_IDS пуст — бот не пустит никого, включая тебя. "
            "Впиши свой номер Telegram в .env и перезапусти"
        )


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level)

    engine = create_engine(settings.database_url, echo=settings.env == "local")
    session_factory = create_session_factory(engine)

    llm = create_llm_client(settings)

    bot = create_bot(settings)
    dp = create_dispatcher(session_factory, llm, allowed=settings.allowed_tg_user_ids)
    _log_access(settings)

    try:
        if settings.bot_mode == "webhook":
            await _run_webhook(bot, dp, settings)
        else:
            await _run_polling(bot, dp)
    finally:
        await bot.session.close()
        await llm.close()
        await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(load_settings()))
