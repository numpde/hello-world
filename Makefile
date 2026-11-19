.DEFAULT_GOAL := help

# Never inspect Docker, prompt, or evaluate caller-controlled values during
# Make parsing. Capture only an explicit command-line selection, literally.
unexport DOCKER_CONTEXT
unexport HELLO_WORLD_DOCKER_CONTEXT
ifeq ($(origin DOCKER_CONTEXT),command line)
override export HELLO_WORLD_DOCKER_CONTEXT := $(value DOCKER_CONTEXT)
endif

# Runner selection is literal and command-line-only, like Docker context.
# Ambient TEST_RUNNER and HELLO_WORLD_TEST_RUNNER never select a test toolchain.
unexport TEST_RUNNER
unexport TEST_WORKERS
unexport HELLO_WORLD_TEST_RUNNER
unexport HELLO_WORLD_TEST_WORKERS
ifeq ($(origin TEST_RUNNER),command line)
override export HELLO_WORLD_TEST_RUNNER := $(value TEST_RUNNER)
endif
ifeq ($(origin TEST_WORKERS),command line)
override export HELLO_WORLD_TEST_WORKERS := $(value TEST_WORKERS)
endif

.PHONY: help repository/check repository/image/acquire repository/make/check repository/application/check repository/application/make/check examples/check examples/plan
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
	@./infrastructure/host-python -m infrastructure.commands.repository check

repository/image/acquire: ## Explicitly acquire the pinned repository-checker image.
	@./infrastructure/host-python -m infrastructure.commands.repository acquire

repository/make/check: ## Test real Make help, dry runs and app delegation without Docker or network.
	@./infrastructure/host-python -m infrastructure.testing.unittest_runner -s tests/host -t . -p 'test_*.py'

repository/application/check: ## Run application-specific stdlib operation contracts in the pinned checker.
	@./infrastructure/host-python -m infrastructure.commands.repository application-check

repository/application/make/check: ## Test application-only Make behavior without Docker or network.
	@./infrastructure/host-python -m infrastructure.testing.unittest_runner -s tests/application_host -t . -p 'test_*.py'

examples/check: ## Integrate exact main/example commits and run offline compatibility gates.
	@./infrastructure/host-python -m infrastructure.commands.example_integrations check

examples/plan: ## Resolve exact example revisions and ownership without Docker or network.
	@./infrastructure/host-python -m infrastructure.commands.example_integrations plan

# Examples keep their recipes in a separate Make process. Do not include
# project.mk: it must not be evaluated during common help or dry runs.
# Selector values are captured literally only when supplied on the command line.
# The cross-example runner resolves exact committed identities without fetching.
unexport HELLO_WORLD_EXAMPLE
unexport HELLO_WORLD_EXAMPLE_BASE
unexport HELLO_WORLD_EXAMPLE_HEAD
ifeq ($(origin EXAMPLE),command line)
override export HELLO_WORLD_EXAMPLE := $(value EXAMPLE)
endif
ifeq ($(origin BASE),command line)
override export HELLO_WORLD_EXAMPLE_BASE := $(value BASE)
endif
ifeq ($(origin EXAMPLE_HEAD),command line)
override export HELLO_WORLD_EXAMPLE_HEAD := $(value EXAMPLE_HEAD)
endif

unexport HELLO_WORLD_APP_TARGET

app/%: override export HELLO_WORLD_APP_TARGET = $@
app/%: __app_dispatch_force
	@./infrastructure/host-python -m infrastructure.commands.application

__app_dispatch_force:
