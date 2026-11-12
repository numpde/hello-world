# OpenAPI MCP bridge — standalone application example

This semantic branch inherits the root Makefile and infrastructure from main.
Application commands live in project.mk; application pins live in pyproject.toml
and its native uv.lock. Neither the root Makefile nor checker pins are overridden.

The executable example derives FastMCP tools from a captured local OpenAPI spec.
Only explicitly selected operations are supplied to FastMCP. The read-only
policy exposes listDocuments and getDocument; with-create adds createDocument.
adminKeys is deliberately absent from both policies.

## Qualification sequence

1. `make -f project.mk help` (read-only).
2. `make -f project.mk app/dependencies/acquire` (explicitly networked).
3. Review the generated uv.lock and commit it. Do not manufacture a lockfile.
4. `make -f project.mk app/dependencies/check` (read-only, offline).
5. `make -f project.mk app/dependencies/prepare` (install from cache only).
6. `make -f project.mk app/tests/run` (real MCP-client and HTTP tests).
7. `make -f project.mk app/package/build` (offline wheel and sdist).

To explore manually, start `make -f project.mk app/demo/run`, then set
`APP_API_BASE=http://127.0.0.1:8765` and run `make -f project.mk app/mcp/run`.
Only MCP protocol messages belong on the MCP process's stdout.

## Known qualification gaps

No generated uv.lock or downloaded dependency closure is available in the
current implementation environment. FastMCP's installed runtime, stdio and
built-wheel qualification must run on a machine with dependencies available.
The common checker also needs an independently reviewed mainline update to
classify src/, project.mk and uv.lock before repository/check can run here.
That need does not authorize an application-branch override of main.
