"""Bind a captured OpenAPI contract to FastMCP and one selected API origin."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx2
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import OpenAPIProvider

from .selection import captured_contract, enabled_operations, selected_contract


def require_api_origin(value: str) -> str:
    """Require an explicit origin, with HTTP limited to the local demo API."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("API origin has an invalid port or URL") from error

    if not parsed.hostname or not parsed.netloc or not parsed.scheme or parsed.username or parsed.password:
        raise ValueError("API origin must be an absolute URL without embedded credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or port == 0:
        raise ValueError("API origin must not contain a path, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return value
    if parsed.scheme == "https":
        return value
    raise ValueError("Use HTTPS for remote APIs; plain HTTP is limited to loopback")


def create_server(client: httpx2.AsyncClient, policy: str = "read-only") -> FastMCP:
    """Let FastMCP construct executable tools from the selected contract."""
    contract = selected_contract(captured_contract(), enabled_operations(policy))
    return FastMCP("Document API bridge", providers=[OpenAPIProvider(contract, client)])


def api_client(api_origin: str) -> httpx2.AsyncClient:
    """Keep transport bounded and independent of ambient proxy credentials."""
    return httpx2.AsyncClient(
        base_url=require_api_origin(api_origin),
        timeout=10.0,
        follow_redirects=False,
        trust_env=False,
    )
