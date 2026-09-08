"""Хендлеры, сгруппированные по сценариям."""

from aiogram import Router

from siga.bot.handlers import common, imports, start


def build_router() -> Router:
    """Собрать корневой роутер.

    Порядок важен: `common` содержит перехватчик всего остального и должен
    подключаться последним.
    """
    router = Router(name="root")
    router.include_router(start.router)
    router.include_router(imports.router)
    router.include_router(common.router)
    return router


__all__ = ["build_router"]
