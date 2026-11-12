# OpenAPI MCP bridge — standalone application example

This branch demonstrates an OpenAPI-to-MCP adapter without replacing the
common Makefile or checker pins. Application commands live in `project.mk`;
application dependencies live in `pyproject.toml` and a native `uv.lock`.

## Runtime inputs

The installed application has **no built-in OpenAPI document or exposure
policy**. Supply your own JSON/YAML files at runtime:

- `--openapi FILE.json|yaml|yml`: the API's OpenAPI document.
- `--allowlist FILE.json|yaml|yml`: an object such as
  `{"operationIds":["listDocuments","getDocument"]}`.
- `--api-base ORIGIN`: the API server's origin, supplied separately so a
  captured specification cannot silently redirect requests.

Only operations named in the allowlist are passed to FastMCP. An unknown,
empty or malformed allowlist fails closed. This selection controls which MCP
tools exist; it does **not** implement API authentication or replace the
application's own authorization. HTTP is allowed only for loopback origins;
other origins require HTTPS. Redirects and inherited proxy settings are off.

The demonstration specification and two allowlists live under
`tests/application/fixtures/` **only**. They are not installed in the wheel
and are not application defaults.

## Qualification sequence

1. `make help` (read-only), and separately
   `make repository/check DOCKER_CONTEXT=<local-rootless-context>`.
2. `make repository/make/check` (real GNU Make tests, no Docker).
3. `make app/dependencies/acquire` (explicitly networked; generates `uv.lock`).
4. Review and commit `uv.lock`; then `make app/dependencies/check`.
5. `make app/dependencies/prepare` (locked, offline).
6. `make app/tests/run` (external JSON/YAML input tests and real MCP/HTTP tests).
7. `make app/package/build` (offline wheel and sdist).

To try the example, start `make app/demo/run` in one terminal and run the
following in another. A real service supplies its own spec and allowlist paths.

```sh
APP_API_BASE=http://127.0.0.1:8765 \
APP_OPENAPI=tests/application/fixtures/documents.openapi.json \
APP_ALLOWLIST=tests/application/fixtures/read-only.json \
make app/mcp/run
```

Use the `with-create.json` test allowlist only when intentionally testing
mutation. MCP stdio must contain only MCP protocol messages.

## Qualification gaps

This branch has not acquired or committed `uv.lock`. The installed FastMCP
runtime, built-wheel execution, and local rootless Docker qualification must
run where the dependency closure and Docker checker are available. Common
`repository/check` remains stdlib-only and does not acquire app dependencies.
