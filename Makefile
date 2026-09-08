.PHONY: install fmt lint typecheck test check db up down logs migrate revision

install:  ## поставить зависимости в .venv
	uv sync

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest

check: lint typecheck test

db:  ## только Postgres, для локального запуска бота из терминала
	docker compose up -d db

up:  ## вся связка в контейнерах
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f bot

migrate:  ## накатить миграции на локальную базу
	uv run alembic upgrade head

revision:  ## make revision m="описание"
	uv run alembic revision --autogenerate -m "$(m)"
