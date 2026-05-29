# =============================================================================
# Makefile — NL SQL Agent
# =============================================================================
# Common commands for development, testing, and deployment.
# Run `make help` to see all available targets.

.PHONY: help dev lint test typecheck docker-up docker-down clean \
       lint-backend lint-frontend test-backend test-frontend typecheck-backend typecheck-frontend

# Default target
help: ## Show this help message
	@echo "NL SQL Agent — Available commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

docker-up: ## Start all Docker services
	docker compose -f docker/compose.dev.yml up -d --build

docker-down: ## Stop all Docker services
	docker compose -f docker/compose.dev.yml down

dev: docker-up ## Start full dev environment
	@echo ""
	@echo "  Backend:  http://localhost:8000"
	@echo "  Frontend: http://localhost:3000"
	@echo "  Postgres: localhost:5432"
	@echo "  Redis:    localhost:6379"
	@echo ""

# ---------------------------------------------------------------------------
# Linting
# ---------------------------------------------------------------------------

lint: lint-backend lint-frontend ## Run all linters

lint-backend: ## Lint Python code with ruff
	cd backend && uv run ruff check app/ tests/

lint-frontend: ## Lint TypeScript/Next.js code
	cd frontend && npm run lint

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test: test-backend test-frontend ## Run all tests

test-backend: ## Run Python tests with pytest
	cd backend && uv run pytest tests/ -v

test-frontend: ## Run frontend tests
	cd frontend && npm run test

# ---------------------------------------------------------------------------
# Type Checking
# ---------------------------------------------------------------------------

typecheck: typecheck-backend typecheck-frontend ## Run all type checkers

typecheck-backend: ## Type-check Python code with mypy
	cd backend && uv run mypy app/

typecheck-frontend: ## Type-check TypeScript code
	cd frontend && npm run typecheck

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

clean: ## Remove build artifacts and caches
	rm -rf backend/.mypy_cache backend/.ruff_cache backend/.pytest_cache backend/__pycache__
	rm -rf frontend/.next frontend/node_modules/.cache
	@echo "Cleaned."

