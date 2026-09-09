"""Клиент для окружения без ключа провайдера.

Нужен, чтобы бот поднимался и работал текстовыми сценариями, пока ключа нет
или пока он кончился. Заглушка не выдумывает ответы: греческая грамматика,
сочинённая из ничего, попадёт человеку в карточку и будет выучена как
правда. Поэтому здесь честный отказ — тот же `LlmUnavailable`, что и при
сетевой аварии, чтобы у обоих случаев был один путь обработки и одни тесты.
"""

from __future__ import annotations

from collections.abc import Sequence

from siga.llm.base import Completion, LlmUnavailable, Message, Route

REASON = (
    "LLM не настроена: LLM_PROVIDER=offline. "
    "Поставь LLM_PROVIDER=deepseek и DEEPSEEK_API_KEY, чтобы включить."
)


class OfflineClient:
    async def complete(
        self,
        *,
        route: Route,
        messages: Sequence[Message],
        json_mode: bool = False,
        max_output_tokens: int | None = None,
    ) -> Completion:
        raise LlmUnavailable(REASON)

    async def close(self) -> None:
        return None


__all__ = ["REASON", "OfflineClient"]
