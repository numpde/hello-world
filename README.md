# OpenAPI MCP bridge — standalone application example

This semantic branch inherits the root Makefile and infrastructure from main.
Application commands live in project.mk; application pins live in pyproject.toml
and its native uv.lock. Neither the root Makefile nor checker pins are overridden.

The executable example derives FastMCP tools from a captured local OpenAPI spec.
Only explicitly selected operations are supplied to FastMCP. The read-only
policy exposes listDocuments and getDocument; with-create adds createDocument.
adminKeys is deliberately absent from both policies.

## Qualification sequence

1. `make help` (read-only); run `make repository/check DOCKER_CONTEXT=science`
   separately to qualify the common infrastructure on an admitted rootless engine.
2. `make app/dependencies/acquire` (explicitly networked).
3. Review the generated uv.lock and commit it. Do not manufacture a lockfile.
4. `make app/dependencies/check` (read-only, offline).
5. `make app/dependencies/prepare` (install from cache only).
6. `make app/tests/run` (real MCP-client and HTTP tests).
7. `make app/package/build` (offline wheel and sdist).

To explore manually, start `make app/demo/run`, then set
`APP_API_BASE=http://127.0.0.1:8765` and run `make app/mcp/run`.
Only MCP protocol messages belong on the MCP process's stdout.

## Known qualification gaps

No generated uv.lock or downloaded dependency closure is available in the
current implementation environment. FastMCP's installed runtime, stdio and
built-wheel qualification must run on a machine with dependencies available.
The common checker now admits src/, project.mk and uv.lock as reviewed,
read-only inputs and runs only common stdlib tests. Application tests run through
make app/tests/run in the application's separately locked environment.
Common Make targets and Docker policies remain unchanged by this example.
