"""Сборка LLM-клиента по конфигу — единственное место, где выбирается провайдер."""

from __future__ import annotations

import logging

from siga.config import Settings
from siga.llm.base import LlmClient, Route
from siga.llm.deepseek import DeepSeekClient
from siga.llm.offline import OfflineClient

log = logging.getLogger(__name__)


def route_models(settings: Settings) -> dict[Route, str]:
    """Какая модель обслуживает какой маршрут.

    Картинки принимает только vision-модель, поэтому R1 отдельно; остальным
    маршрутам хватает flash — pro втрое дороже на выходе и на нашей задаче
    (грамматика уровня A1) ничего не добавляет.
    """
    return {route: settings.llm_model_text for route in Route} | {
        Route.VISION_IMPORT: settings.llm_model_vision
    }


def create_llm_client(settings: Settings) -> LlmClient:
    if settings.llm_provider == "offline":
        log.warning("LLM выключена (LLM_PROVIDER=offline): обогащение и эпизоды не работают")
        return OfflineClient()

    # Наличие ключа проверено валидатором Settings — до сюда без него не дойти.
    assert settings.deepseek_api_key is not None
    models = route_models(settings)
    log.info("LLM: deepseek, модели %s", sorted(set(models.values())))
    return DeepSeekClient(
        api_key=settings.deepseek_api_key.get_secret_value(),
        models=models,
        base_url=settings.deepseek_base_url,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )


__all__ = ["create_llm_client", "route_models"]
