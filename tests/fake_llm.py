"""Клиент LLM по сценарию: отдаёт заготовленные ответы, запоминает вопросы.

Нужен там, где проверяется логика вокруг вызова — разбор, сопоставление,
партии, — а не сам HTTP. Клиент по HTTP проверяется в `test_llm.py` против
локального сервера aiohttp.
"""

from __future__ import annotations

from collections.abc import Sequence

from siga.llm.base import Completion, Message, Route, Usage

#: Токены одного заготовленного ответа — чтобы в тестах было что складывать.
ANSWER_USAGE = Usage(input_tokens=10, output_tokens=5)


class ScriptedClient:
    def __init__(self, *answers: str | Exception) -> None:
        """Ответы выдаются по очереди. Исключение в списке — будет брошено."""
        self._answers = list(answers)
        self.asked: list[list[Message]] = []
        self.routes: list[Route] = []
        self.json_modes: list[bool] = []
        self.max_output: list[int | None] = []
        """§8.2 требует явный потолок выхода на каждом маршруте — значит, его
        отсутствие должно быть видно тесту, а не только в счёте провайдера."""

    @property
    def calls(self) -> int:
        return len(self.asked)

    async def complete(
        self,
        *,
        route: Route,
        messages: Sequence[Message],
        json_mode: bool = False,
        max_output_tokens: int | None = None,
    ) -> Completion:
        self.asked.append(list(messages))
        self.routes.append(route)
        self.json_modes.append(json_mode)
        self.max_output.append(max_output_tokens)
        if not self._answers:
            raise AssertionError(f"сценарий кончился, а спросили ещё раз ({self.calls}-й)")

        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Completion(answer, "test-model", ANSWER_USAGE)

    async def close(self) -> None:
        return None


__all__ = ["ANSWER_USAGE", "ScriptedClient"]
