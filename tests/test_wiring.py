"""Дымовые проверки сборки: диспетчер собирается, слои не путаются местами."""

from __future__ import annotations

import pkgutil
from pathlib import Path
from typing import cast

from aiogram import Dispatcher

from siga.db.base import Base

#: §9: `core` — чистый домен. Клиент LLM тут тоже вне закона: правила зачёта
#: и SRS должны считаться без сети, иначе их не покрыть тестами.
FORBIDDEN_IN_CORE = ("aiogram", "aiohttp", "sqlalchemy", "siga.llm")


def test_dispatcher_builds_and_listens_to_messages(dispatcher: Dispatcher) -> None:
    used = dispatcher.resolve_used_update_types()
    assert "message" in used
    assert "callback_query" in used, "без этого кнопки экрана подтверждения мертвы"


def test_users_table_is_registered() -> None:
    assert "users" in Base.metadata.tables


def test_core_stays_free_of_infrastructure() -> None:
    """§9: `core` — чистый домен, чтобы тестироваться без базы и сети.

    Проверяем текстом по исходникам: импорт-граф тут избыточен, а нарушение
    границы всегда выглядит как строка `import aiogram` в файле.
    """
    import siga.core

    core_dir = Path(cast(str, siga.core.__file__)).parent
    offenders = []
    for module in pkgutil.walk_packages([str(core_dir)]):
        source = (core_dir / f"{module.name}.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if any(pkg in stripped for pkg in FORBIDDEN_IN_CORE):
                offenders.append(f"{module.name}: {stripped}")

    assert not offenders, "в core просочилась инфраструктура: " + "; ".join(offenders)
