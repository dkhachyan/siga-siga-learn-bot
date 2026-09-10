"""Хендлеры, сгруппированные по сценариям."""

from aiogram import Router

from siga.bot.handlers import cards, common, episodes, imports, memory, settings, start


def build_router() -> Router:
    """Собрать корневой роутер.

    Порядок важен: `common` содержит перехватчик всего остального и должен
    подключаться последним. `episodes` — предпоследним: он ловит свободный
    текст, но только когда разговор идёт, иначе пропускает ход дальше.
    """
    router = Router(name="root")
    router.include_router(start.router)
    router.include_router(imports.router)
    router.include_router(cards.router)
    router.include_router(memory.router)
    router.include_router(settings.router)
    router.include_router(episodes.router)
    router.include_router(common.router)
    return router


__all__ = ["build_router"]
