.DEFAULT_GOAL := help
SHELL := /bin/sh
.SHELLFLAGS := -eu -c

.PHONY: help app/image/acquire app/image/check app/dependencies/check app/tests/run app/package/build app/package/check

help: ## List application operations without invoking Docker.
	@awk -F ':[^#]*## ' '/^app\/[a-z0-9][a-z0-9_/-]*:.*## / { printf "  %-30s %s\n", $$1, $$2 }' project.mk

app/image/acquire: ## Acquire the digest-pinned Node runtime image explicitly.
	@./infrastructure/host-python -m app.operations image/acquire

app/image/check: ## Check the cached Node image without downloading anything.
	@./infrastructure/host-python -m app.operations image/check

app/dependencies/check: ## Validate native npm lock offline, with no packages to download.
	@./infrastructure/host-python -m app.operations dependencies/check

app/tests/run: ## Run actual loopback proxy tests in a networkless container.
	@./infrastructure/host-python -m app.operations tests/run

app/package/build: ## Package the proxy into an npm tarball without network.
	@./infrastructure/host-python -m app.operations package/build

app/package/check: ## Install the npm tarball and test without source mounts.
	@./infrastructure/host-python -m app.operations package/check
