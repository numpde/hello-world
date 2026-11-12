.DEFAULT_GOAL := help
SHELL := /bin/sh
.SHELLFLAGS := -eu -c
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: app/toolchain/acquire app/toolchain/check app/dependencies/lock/update app/dependencies/acquire app/dependencies/check app/tests/run app/package/build app/package/check

app/toolchain/acquire: ## Acquire digest-pinned Python and uv images; build toolchain offline.
	@python3 -B -m app.operations toolchain/acquire

app/toolchain/check: ## Verify the local toolchain without network access.
	@python3 -B -m app.operations toolchain/check

app/dependencies/lock/update: ## Resolve or update uv.lock in a networked container.
	@python3 -B -m app.operations dependencies/lock/update

app/dependencies/acquire: ## Prepare locked dependencies in a networked container.
	@python3 -B -m app.operations dependencies/acquire

app/dependencies/check: ## Check the prepared environment offline.
	@python3 -B -m app.operations dependencies/check

app/tests/run: ## Run real application tests offline in a contained environment.
	@python3 -B -m app.operations tests/run

app/package/build: ## Build wheel and sdist offline without installing host tools.
	@python3 -B -m app.operations package/build

app/package/check: ## Install wheel separately and run application tests offline.
	@python3 -B -m app.operations package/check
