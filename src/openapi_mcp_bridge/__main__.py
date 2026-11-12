"""Run the OpenAPI-derived MCP server using stdio transport."""

from __future__ import annotations

import argparse
import asyncio

from .bridge import api_client, create_server
from .selection import POLICIES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--api-base", required=True, help="Explicit API origin, without a path")
    parser.add_argument("--policy", choices=sorted(POLICIES), default="read-only")
    arguments = parser.parse_args(argv)

    try:
        client = api_client(arguments.api_base)
    except ValueError as error:
        parser.error(str(error))

    try:
        server = create_server(client, policy=arguments.policy)
        server.run(transport="stdio")
    finally:
        asyncio.run(client.aclose())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
