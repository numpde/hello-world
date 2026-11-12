.DEFAULT_GOAL := help
SHELL := /bin/sh
.SHELLFLAGS := -eu -c
export PYTHONDONTWRITEBYTECODE := 1
export UV_NO_MANAGED_PYTHON := 1

.PHONY: help app/dependencies/acquire app/dependencies/check app/dependencies/prepare app/package/build app/tests/run app/mcp/run app/demo/run

help: ## List the application's own commands without network or Docker.
	@printf '%s\n' 'OpenAPI MCP bridge application commands (not common infrastructure)' '' \
	  'Use: make -f project.mk <target>' ''
	@awk -F ':[^#]*## ' '/^[a-z][a-z0-9/-]*:.*## / { printf "  %-30s %s\n", $$1, $$2 }' project.mk

app/dependencies/acquire: ## Explicit networked resolution/acquisition; create or update uv.lock.
	@command -v uv >/dev/null || { echo 'Install uv before acquiring application dependencies.' >&2; exit 2; }
	@uv lock --no-python-downloads
	@uv sync --locked --no-python-downloads
	@printf '%s\n' 'Next: review and commit uv.lock, then make -f project.mk app/tests/run'

app/dependencies/check: ## Inspect the native lock without downloading or installing packages.
	@test -f uv.lock || { echo 'Missing uv.lock. Next: make -f project.mk app/dependencies/acquire; review and commit the lock.' >&2; exit 2; }
	@uv lock --check --offline
	@printf '%s\n' 'Lock check passed. Next: make -f project.mk app/dependencies/prepare'

app/dependencies/prepare: ## Install the locked environment from cache without any network access.
	@test -f uv.lock || { echo 'Missing uv.lock. Next: make -f project.mk app/dependencies/acquire.' >&2; exit 2; }
	@uv sync --locked --offline
	@printf '%s\n' 'Offline environment prepared. Next: make -f project.mk app/tests/run'

app/tests/run: ## Execute actual application tests using locked, offline dependencies.
	@test -f uv.lock || { echo 'Missing uv.lock. Next: make -f project.mk app/dependencies/acquire.' >&2; exit 2; }
	@uv run --locked --offline --no-sync -- python -m unittest discover -s tests/application -p 'test_*.py' -v

app/package/build: ## Build an application wheel and sdist without accessing the network.
	@test -f uv.lock || { echo 'Missing uv.lock. Next: make -f project.mk app/dependencies/acquire.' >&2; exit 2; }
	@uv build --locked --offline --out-dir dist
	@printf '%s\n' 'Next: inspect dist/ and verify installation outside the checkout.'

app/mcp/run: ## Run the stdio MCP server; APP_API_BASE must name the intended API.
	@test -f uv.lock || { echo 'Missing uv.lock. Next: make -f project.mk app/dependencies/acquire.' >&2; exit 2; }
	@uv run --locked --offline --no-sync -- python -m openapi_mcp_bridge --api-base "$${APP_API_BASE:?Set APP_API_BASE to the API origin}" --policy read-only

app/demo/run: ## Serve the local demonstration REST API on a chosen loopback port.
	@test -f uv.lock || { echo 'Missing uv.lock. Next: make -f project.mk app/dependencies/acquire.' >&2; exit 2; }
	@uv run --locked --offline --no-sync -- python -m openapi_mcp_bridge.demo_api
