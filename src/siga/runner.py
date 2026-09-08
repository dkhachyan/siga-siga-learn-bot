"""Запуск бота: long polling локально, вебхук на сервере."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from pydantic import ValidationError

from siga.bot import create_bot, create_dispatcher, set_bot_commands
from siga.config import Settings, get_settings
from siga.db import create_engine, create_session_factory
from siga.logging import setup_logging

log = logging.getLogger(__name__)


async def _health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _stop_event() -> asyncio.Event:
    """Событие, которое взводится по SIGINT и SIGTERM.

    SIGTERM важен отдельно: в контейнере остановка приходит именно им, и без
    обработчика процесс умирает, не сняв вебхук и не закрыв пул соединений.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop.set)
    return stop


async def _run_polling(bot: Bot, dp: Dispatcher) -> None:
    # висящий вебхук молча съедает апдейты, если раньше бот работал на сервере
    await bot.delete_webhook(drop_pending_updates=False)
    await set_bot_commands(bot)
    log.info("режим polling")
    await dp.start_polling(bot)


async def _run_webhook(bot: Bot, dp: Dispatcher, settings: Settings) -> None:
    secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else None
    stop = _stop_event()

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


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level)

    engine = create_engine(settings.database_url, echo=settings.env == "local")
    session_factory = create_session_factory(engine)

    bot = create_bot(settings)
    dp = create_dispatcher(session_factory)

    try:
        if settings.bot_mode == "webhook":
            await _run_webhook(bot, dp, settings)
        else:
            await _run_polling(bot, dp)
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Ошибка конфигурации — это не баг, а незаполненный .env.
        # Стек-трейс тут только мешает прочитать, чего не хватает.
        print("Не могу запуститься — проблема в настройках:\n", file=sys.stderr)
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "конфигурация"
            print(
                f"  {field.upper()}: {error['msg'].removeprefix('Value error, ')}", file=sys.stderr
            )
        print("\nСм. .env.example", file=sys.stderr)
        raise SystemExit(2) from None

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))
