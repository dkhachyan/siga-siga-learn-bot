"""Настройка логирования."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # aiohttp логирует каждый запрос вебхука — на INFO это шум
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
