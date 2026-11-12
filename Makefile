.DEFAULT_GOAL := help

# Never inspect Docker, prompt, or evaluate caller-controlled values during
# Make parsing. Capture only an explicit command-line selection, literally.
unexport DOCKER_CONTEXT
unexport HELLO_WORLD_DOCKER_CONTEXT
ifeq ($(origin DOCKER_CONTEXT),command line)
override export HELLO_WORLD_DOCKER_CONTEXT := $(value DOCKER_CONTEXT)
endif

.PHONY: help repository/check repository/image/acquire repository/make/check
.PHONY: __app_dispatch_force

help: ## Show commands, effects and next steps without touching Docker.
	@printf '%s\n' 'Hello World — common infrastructure' '' \
	  'Docker operations require DOCKER_CONTEXT=<local rootless context>.' \
	  'Example (replace example-rootless with your existing context):' \
	  '  make repository/image/acquire DOCKER_CONTEXT=example-rootless' \
	  '  make repository/check DOCKER_CONTEXT=example-rootless' \
	  'Acquisition can download images; repository/check stays offline.' ''
	@awk -F ':[^#]*## ' '/^[a-z][a-z0-9/-]*:.*## / { printf "  %-28s %s\n", $$1, $$2 }' Makefile
	@if test -f project.mk; then \
		printf '%s\n' '' 'Application commands:'; \
		awk -F ':[^#]*## ' '/^app\/[a-z0-9][a-z0-9_/-]*:.*## / { printf "  %-28s %s\n", $$1, $$2 }' project.mk; \
	fi

repository/check: ## Run repository checks offline in a confined Docker container.
	@python3 -m infrastructure.commands.repository check

repository/image/acquire: ## Explicitly acquire the pinned repository-checker image.
	@python3 -m infrastructure.commands.repository acquire

repository/make/check: ## Test real Make help, dry runs and app delegation without Docker or network.
	@python3 -B -m unittest discover -s tests/host -t . -p 'test_*.py' -v

# Examples keep their recipes in a separate Make process. Do not include
# project.mk: it must not be evaluated during common help or dry runs.
unexport HELLO_WORLD_APP_TARGET

app/%: override export HELLO_WORLD_APP_TARGET = $@
app/%: __app_dispatch_force
	@python3 -B -m infrastructure.commands.application

__app_dispatch_force:
