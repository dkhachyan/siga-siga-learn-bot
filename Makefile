.PHONY: install fmt lint typecheck test test-db check db up down logs migrate revision

# Отдельная база под тесты: фикстура `session` сносит схему целиком,
# и делать это в рабочей базе нельзя.
TEST_DATABASE_URL ?= postgresql+asyncpg://siga:siga@localhost:5432/siga_test

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

test:  ## быстрые тесты; те, что просят базу, пропускаются
	uv run pytest

test-db:  ## все тесты, включая те, которым нужен Postgres (сначала make db)
	docker compose exec -T db psql -U siga -d siga -tAc \
		"SELECT 1 FROM pg_database WHERE datname='siga_test'" | grep -q 1 || \
		docker compose exec -T db createdb -U siga siga_test
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

check: lint typecheck test

db:  ## только Postgres, для локального запуска бота из терминала
	docker compose up -d db

up:  ## вся связка в контейнерах
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f bot scheduler sender

migrate:  ## накатить миграции на локальную базу
	uv run alembic upgrade head

revision:  ## make revision m="описание"
	uv run alembic revision --autogenerate -m "$(m)"
