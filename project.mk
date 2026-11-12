.DEFAULT_GOAL := help
SHELL := /bin/sh
.SHELLFLAGS := -eu -c

.PHONY: help app/demo/run app/toolchain/acquire app/toolchain/check app/dependencies/lock/update app/dependencies/acquire app/dependencies/check app/tests/run app/package/build app/package/check

help: ## List application commands without evaluating application operations.
	@awk -F ':[^#]*## ' '/^[a-z][a-z0-9/-]*:.*## / { printf "  %-34s %s\n", $$1, $$2 }' project.mk

app/toolchain/acquire: ## Acquire digest-pinned Python and uv images; build toolchain offline.
	@./infrastructure/host-python -m app.operations toolchain/acquire

app/toolchain/check: ## Verify the local toolchain without network access.
	@./infrastructure/host-python -m app.operations toolchain/check

app/dependencies/lock/update: ## Resolve or update uv.lock in a networked container.
	@./infrastructure/host-python -m app.operations dependencies/lock/update

app/dependencies/acquire: ## Prepare locked dependencies in a networked container.
	@./infrastructure/host-python -m app.operations dependencies/acquire

app/dependencies/check: ## Check the prepared environment offline.
	@./infrastructure/host-python -m app.operations dependencies/check

app/tests/run: ## Run real application tests offline in a contained environment.
	@./infrastructure/host-python -m app.operations tests/run

app/package/build: ## Build wheel and sdist offline without installing host tools.
	@./infrastructure/host-python -m app.operations package/build

app/package/check: ## Install wheel separately and run application tests offline.
	@./infrastructure/host-python -m app.operations package/check

app/demo/run: ## Run the stdlib documents API fixture on host loopback; Ctrl-C stops it.
	@./infrastructure/host-python -m tests.application.demo_api
