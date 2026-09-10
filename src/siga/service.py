"""Общее для всех процессов: настройки, остановка, периодический тик.

Процессов у нас три — бот и два воркера (§9), — и каждому нужно одно и то же:
прочитать `.env` и понятно упасть, если чего-то не хватает; остановиться по
сигналу, а не по SIGKILL. Держим это в одном месте, чтобы поведение при
остановке не разъезжалось между контейнерами.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from siga.config import Settings, get_settings

log = logging.getLogger(__name__)


def load_settings() -> Settings:
    """Настройки или понятная смерть.

    Ошибка конфигурации — это не баг, а незаполненный `.env`. Стек-трейс тут
    только мешает прочитать, чего именно не хватает.
    """
    try:
        return get_settings()
    except ValidationError as exc:
        print("Не могу запуститься — проблема в настройках:\n", file=sys.stderr)
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "конфигурация"
            print(
                f"  {field.upper()}: {error['msg'].removeprefix('Value error, ')}", file=sys.stderr
            )
        print("\nСм. .env.example", file=sys.stderr)
        raise SystemExit(2) from None


def stop_event() -> asyncio.Event:
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


async def tick_forever(
    *,
    name: str,
    interval: float,
    step: Callable[[], Awaitable[object]],
    stop: asyncio.Event,
) -> None:
    """Звать `step` раз в `interval` секунд, пока не попросят остановиться.

    Упавший тик не роняет воркер: сеть моргнула, база перезагрузилась, модель
    ответила ерундой — всё это поводы попробовать снова через полминуты, а не
    оставить человека без расписания до следующего деплоя. Молчать при этом
    нельзя, поэтому исключение уходит в лог целиком.

    Интервал считается от начала тика, а не от конца: иначе медленный тик
    растягивал бы паузу и расписание уезжало бы само по себе. Ждём на событии
    остановки, а не на `sleep`, — тогда SIGTERM не приходится ждать полминуты.
    """
    while not stop.is_set():
        started = time.monotonic()
        try:
            await step()
        except Exception:
            log.exception("%s: тик не удался", name)

        delay = max(0.0, interval - (time.monotonic() - started))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
    log.info("%s: остановлен", name)


__all__ = ["load_settings", "stop_event", "tick_forever"]
