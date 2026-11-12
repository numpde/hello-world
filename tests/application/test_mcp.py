"""Installed third-party FastMCP is exercised through an actual MCP client."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from fastmcp import Client
from openapi_mcp_bridge.bridge import api_client, create_server, require_api_origin
from tests.application.demo_api import DocumentAPI
from openapi_mcp_bridge.selection import load_document, load_operations


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class OriginContracts(unittest.TestCase):
    def test_only_explicit_https_or_loopback_http(self):
        self.assertEqual(require_api_origin("http://127.0.0.1:8765"), "http://127.0.0.1:8765")
        self.assertEqual(require_api_origin("https://api.example.com"), "https://api.example.com")
        for invalid in (
            "http://example.com", "http://0.0.0.0:8765", "http://127.0.0.1:8765/admin",
            "https://user:secret@api.example.com", "https://api.example.com/?x=1", "localhost",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                require_api_origin(invalid)


class ExecutableMCPContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = DocumentAPI(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
        self.http = api_client(f"http://127.0.0.1:{self.api.server_port}")
        self.spec = load_document(FIXTURES / "documents.openapi.json")

    async def asyncTearDown(self):
        await self.http.aclose()
        self.api.shutdown()
        self.api.server_close()
        self.thread.join(timeout=5)

    async def test_read_only_tools_invoke_real_http_but_hide_other_routes(self):
        operations = load_operations(FIXTURES / "read-only.json")
        async with Client(create_server(self.http, self.spec, operations)) as mcp:
            names = {tool.name for tool in await mcp.list_tools()}
            self.assertEqual(names, {"listDocuments", "getDocument"})
            listing = await mcp.call_tool("listDocuments", {})
            self.assertFalse(listing.is_error)
            self.assertTrue(any("Read me" in str(content) for content in listing.content))
            result = await mcp.call_tool("getDocument", {"documentId": "doc-1"})
            self.assertFalse(result.is_error)
            self.assertTrue(any("doc-1" in str(content) for content in result.content))
            self.assertEqual(self.api.seen_requests, [("GET", "/documents"), ("GET", "/documents/doc-1")])

            for excluded in ("createDocument", "adminKeys"):
                with self.subTest(excluded=excluded):
                    try:
                        response = await mcp.call_tool(excluded, {})
                    except Exception:
                        pass
                    else:
                        self.assertTrue(response.is_error)
            self.assertEqual(len(self.api.seen_requests), 2)

    async def test_explicit_write_policy_enables_mutation_but_not_admin(self):
        operations = load_operations(FIXTURES / "with-create.json")
        async with Client(create_server(self.http, self.spec, operations)) as mcp:
            names = {tool.name for tool in await mcp.list_tools()}
            self.assertEqual(names, {"listDocuments", "getDocument", "createDocument"})
            response = await mcp.call_tool("createDocument", {"title": "Added by MCP"})
            self.assertFalse(response.is_error)
            self.assertEqual(self.api.documents["doc-2"]["title"], "Added by MCP")
            self.assertNotIn(("GET", "/admin/keys"), self.api.seen_requests)


if __name__ == "__main__":
    unittest.main()
