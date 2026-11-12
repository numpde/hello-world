# OpenAPI-to-MCP example (generic external JSON/YAML inputs)

This branch adds an OpenAPI-to-MCP application without replacing the root
Makefile or infrastructure commands. Runtime `--openapi` and `--allowlist`
files come from the operator, not from a package data directory. The example
documents API and selection policies exist only in `tests/application/`.

## Development toolchain: no host uv

The application uses the common rootless Docker context selector and owned
finite-job lifecycle. Pinned Docker images supply Python 3.12 and uv, combined
by an **offline** Docker build; neither uv nor application dependencies are
installed into the host Python interpreter.

Run in this order on a local rootless Docker context (for example `science`):

```sh
make repository/make/check
make repository/check DOCKER_CONTEXT=science
make app/toolchain/acquire DOCKER_CONTEXT=science
make app/toolchain/check DOCKER_CONTEXT=science
make app/dependencies/lock/update DOCKER_CONTEXT=science
# Review and commit the generated uv.lock.
make app/dependencies/acquire DOCKER_CONTEXT=science
make app/dependencies/check DOCKER_CONTEXT=science
make app/tests/run DOCKER_CONTEXT=science
make app/package/build DOCKER_CONTEXT=science
make app/package/check DOCKER_CONTEXT=science
```

Acquisition and lock updates are the **only** networked phases. They run
against the explicitly selected rootless engine and do not expose published
ports or the Docker socket to the container. The toolchain build, dependency
checks, application tests, packaging and installed-wheel qualification use
networkless, confined jobs. Missing prerequisites fail with a next command;
no test or build automatically acquires packages or changes the lock.

Application cache, prepared environment and build artifacts live under
`~/.cache/hello-world/openapi-mcp-bridge/<checkout-id>/`, not the repository.
`uv.lock` is the only committed application dependency authority. Build
backends needed for offline packaging are locked in the normal dev group.
Only `app/dependencies/lock/update` writes the checkout lockfile.

## Runtime interface

Install the built wheel into the *consumer's* supported Python environment,
then invoke the actual package entrypoint:

```sh
openapi-mcp-bridge --openapi /path/to/api.yaml \
  --allowlist /path/to/allowed.json --api-base https://api.example.org
```

The allowlist is JSON or YAML containing an `operationIds` string list, such
as `{"operationIds":["listDocuments","getDocument"]}`. Tools are exposed
only when explicitly selected. Unknown operation IDs fail; an empty selection
is not permitted. API authentication remains an application/client concern.
HTTP origins must be loopback; remote APIs require HTTPS.

The demo HTTP server is a *test fixture*, not a deployed service or app default.
Use `make app/demo/run` to start that local stdlib-only fixture on loopback;
this operation needs no `uv`, dependency installation or Docker context.
`app/package/check` installs the wheel into a fresh virtual environment and
runs the real application tests **without mounting package source code**.

## Qualification status

The repository and host Make checks previously passed (58 and 3 tests
respectively). This new dependency workflow needs qualification on a local
rootless Docker engine. No installer script or host uv prerequisite is used,
and no lockfile has been fabricated without running the resolver.
