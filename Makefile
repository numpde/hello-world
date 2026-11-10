.DEFAULT_GOAL := help

# Never inspect Docker, prompt, or evaluate caller-controlled values during
# Make parsing. Capture only an explicit command-line selection, literally.
unexport DOCKER_CONTEXT
unexport HELLO_WORLD_DOCKER_CONTEXT
ifeq ($(origin DOCKER_CONTEXT),command line)
override export HELLO_WORLD_DOCKER_CONTEXT := $(value DOCKER_CONTEXT)
endif

.PHONY: help repository/check repository/image/acquire

help: ## Show maintained commands; no Docker, network, prompts, or writes.
	@printf '%s\n' 'Hello World — common infrastructure' '' \
	  'Docker operations require DOCKER_CONTEXT=<local rootless context>.' ''
	@awk -F ':[^#]*## ' '/^[a-z][a-z0-9/-]*:.*## / { printf "  %-28s %s\n", $$1, $$2 }' Makefile

repository/check: ## Run repository checks offline in a confined Docker container.
	@python3 -m tools.repository.checker check

repository/image/acquire: ## Explicitly acquire the pinned repository-checker image.
	@python3 -m tools.repository.checker acquire
