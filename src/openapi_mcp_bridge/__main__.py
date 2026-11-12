"""Run the MCP bridge against a caller-supplied OpenAPI JSON/YAML file."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .bridge import api_client, create_server
from .selection import load_document, load_operations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--api-base", required=True, help="Explicit API origin, without a path")
    parser.add_argument("--openapi", type=Path, required=True, help="External .json/.yaml OpenAPI document")
    parser.add_argument(
        "--allowlist", type=Path, required=True,
        help="External .json/.yaml file containing an operationIds list",
    )
    arguments = parser.parse_args(argv)

    try:
        spec = load_document(arguments.openapi)
        operation_ids = load_operations(arguments.allowlist)
        client = api_client(arguments.api_base)
    except ValueError as error:
        parser.error(str(error))

    try:
        try:
            server = create_server(client, spec, operation_ids)
        except ValueError as error:
            parser.error(str(error))
        server.run(transport="stdio")
    finally:
        asyncio.run(client.aclose())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
