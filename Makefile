.PHONY: install verify dev-api dev-agent dev-h5 test e2e smoke lint module-budget typecheck offline

install:
	uv sync --frozen --all-extras
	npm --prefix apps/h5 ci

verify:
	uv run python scripts/verify_env.py

smoke:
	uv run python scripts/provider_smoke_test.py

dev-api:
	uv run uvicorn services.control_api.app.main:app --host 0.0.0.0 --port 8000 --reload

dev-agent:
	uv run python -m services.agent.src.main dev

dev-h5:
	npm --prefix apps/h5 run dev

lint:
	uv run ruff check .

module-budget:
	uv run python scripts/check_module_budget.py check

typecheck:
	uv run mypy services --strict

test:
	uv run ruff check .
	uv run python scripts/check_module_budget.py check
	uv run mypy services --strict
	uv run pytest
	npm --prefix apps/h5 test
	npm --prefix apps/h5 run build

offline:
	uv run python scripts/run_e2e.py --profile offline

e2e:
	uv run python scripts/run_e2e.py --profile provider-smoke
