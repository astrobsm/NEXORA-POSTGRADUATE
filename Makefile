.DEFAULT_GOAL := help
SHELL := /bin/sh

BACKEND  := backend
FRONTEND := frontend
VENV     := $(BACKEND)/.venv
PY       := $(VENV)/bin/python
ifeq ($(OS),Windows_NT)
PY       := $(VENV)/Scripts/python.exe
endif

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---- Setup ---------------------------------------------------------------
.PHONY: install
install: install-backend install-frontend ## Install all dependencies

.PHONY: install-backend
install-backend: ## Create the venv and install Python dependencies
	python -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r $(BACKEND)/requirements.txt

.PHONY: install-frontend
install-frontend: ## Install Node dependencies
	cd $(FRONTEND) && npm install --no-audit --no-fund

# ---- Run -----------------------------------------------------------------
.PHONY: dev-api
dev-api: ## Run the API with reload on :8000
	cd $(BACKEND) && ../$(PY) -m uvicorn app.main:app --reload --port 8000

.PHONY: dev-web
dev-web: ## Run the web client on :5173
	cd $(FRONTEND) && npm run dev

# ---- Data ----------------------------------------------------------------
.PHONY: seed
seed: ## Seed reference data and the demo institution
	cd $(BACKEND) && ../$(PY) -m app.db.seed

.PHONY: seed-reference
seed-reference: ## Seed platform reference data only (no demo institution)
	cd $(BACKEND) && ../$(PY) -m app.db.seed --no-demo

.PHONY: reset
reset: ## Drop every table and re-seed. Destroys all data.
	cd $(BACKEND) && ../$(PY) -m app.db.seed --reset

.PHONY: migrate
migrate: ## Apply migrations
	cd $(BACKEND) && ../$(PY) -m alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add x"
	cd $(BACKEND) && ../$(PY) -m alembic revision --autogenerate -m "$(m)"

.PHONY: migration-check
migration-check: ## Fail if the models have drifted from the migrations
	cd $(BACKEND) && ../$(PY) -m alembic check

# ---- Quality -------------------------------------------------------------
.PHONY: test
test: ## Run the backend test suite
	cd $(BACKEND) && ../$(PY) -m pytest

.PHONY: test-cov
test-cov: ## Run tests with coverage
	cd $(BACKEND) && ../$(PY) -m pytest --cov=app --cov-report=term-missing

.PHONY: lint
lint: ## Lint the backend
	cd $(BACKEND) && ../$(PY) -m ruff check app tests

.PHONY: format
format: ## Format the backend
	cd $(BACKEND) && ../$(PY) -m ruff format app tests

.PHONY: typecheck
typecheck: ## Type-check the frontend
	cd $(FRONTEND) && npx tsc --noEmit -p tsconfig.json

.PHONY: build
build: ## Build the frontend for production
	cd $(FRONTEND) && npm run build

.PHONY: check
check: lint test typecheck build ## Everything CI runs

# ---- Docker --------------------------------------------------------------
.PHONY: up
up: ## Start the full stack (http://localhost:8080)
	docker compose up --build -d

.PHONY: down
down: ## Stop the stack
	docker compose down

.PHONY: clean
clean: ## Stop the stack and delete its volumes. Destroys all data.
	docker compose down -v

.PHONY: logs
logs: ## Tail stack logs
	docker compose logs -f --tail=100

# ---- Docs ----------------------------------------------------------------
.PHONY: openapi
openapi: ## Write the OpenAPI schema to docs/openapi.json
	cd $(BACKEND) && ../$(PY) -c "import json; from app.main import app; \
	  open('../docs/openapi.json','w').write(json.dumps(app.openapi(), indent=2))"
	@echo "Wrote docs/openapi.json"
