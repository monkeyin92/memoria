.PHONY: install verify dev-api dev-agent dev-web dev-h5 test e2e smoke lint typecheck offline

install:
	uv sync --frozen --all-extras
	pnpm install --frozen-lockfile
	npm --prefix apps/h5 ci

verify:
	uv run python scripts/verify_env.py

smoke:
	uv run python scripts/provider_smoke_test.py

dev-api:
	uv run uvicorn services.control_api.app.main:app --host 0.0.0.0 --port 8000 --reload

dev-agent:
	uv run python -m services.agent.src.main dev

dev-web:
	pnpm --dir apps/web dev

dev-h5:
	npm --prefix apps/h5 run dev

lint:
	uv run ruff check .
	pnpm --dir apps/web lint

typecheck:
	uv run mypy services --strict

test:
	uv run ruff check .
	uv run mypy services --strict
	uv run pytest
	pnpm --dir apps/web lint
	pnpm --dir apps/web test --run
	npm --prefix apps/h5 test
	npm --prefix apps/h5 run build

offline:
	uv run python scripts/run_e2e.py --profile offline

e2e:
	uv run python scripts/run_e2e.py --profile provider-smoke
