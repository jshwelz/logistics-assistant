#!/bin/sh
# Lint and fake-provider tests. Postgres and Redis come from this Compose project.
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
# Start only when needed. Do not recreate a running database; that would change its host port.
docker compose up -d --no-recreate --no-deps postgres redis
postgres_port=$(docker compose port postgres 5432 | sed 's/.*://')
redis_port=$(docker compose port redis 6379 | sed 's/.*://')
cd backend
uv sync --frozen --extra dev
uv run ruff check
TEST_DATABASE_URL="postgresql+psycopg://logistics:logistics@127.0.0.1:${postgres_port}/logistics_test" \
TEST_REDIS_URL="redis://127.0.0.1:${redis_port}/1" \
EMBEDDING_PROVIDER=fake \
EMBEDDING_MODEL=fake-token-v1 \
ANSWER_PROVIDER=fake \
OTEL_SDK_DISABLED=true \
uv run pytest
