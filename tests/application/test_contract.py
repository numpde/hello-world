"""External OpenAPI and allowlist selection, plus the demo HTTP API."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml
from openapi_mcp_bridge.demo_api import DocumentAPI
from openapi_mcp_bridge.selection import load_document, load_operations, selected_contract


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ContractSelection(unittest.TestCase):
    def setUp(self):
        self.source = load_document(FIXTURES / "documents.openapi.json")
        self.read_only = load_operations(FIXTURES / "read-only.json")
        self.with_create = load_operations(FIXTURES / "with-create.json")

    def test_read_only_policy_does_not_expose_mutation_or_administration(self):
        original = deepcopy(self.source)
        selected = selected_contract(self.source, self.read_only)
        self.assertEqual(self.source, original)
        self.assertEqual(set(selected["paths"]), {"/documents", "/documents/{documentId}"})
        self.assertEqual(set(selected["paths"]["/documents"]), {"get"})
        self.assertEqual(set(selected["paths"]["/documents/{documentId}"]), {"get"})
        self.assertEqual(self.source["components"], selected["components"])

    def test_write_policy_enables_only_declared_mutation(self):
        selected = selected_contract(self.source, self.with_create)
        self.assertEqual(set(selected["paths"]["/documents"]), {"get", "post"})
        self.assertNotIn("/admin/keys", selected["paths"])

    def test_external_yaml_for_spec_and_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            spec_path = Path(directory) / "incoming.yaml"
            allowed_path = Path(directory) / "allowed.yml"
            spec_path.write_text(yaml.safe_dump(self.source), encoding="utf-8")
            allowed_path.write_text(yaml.safe_dump({"operationIds": sorted(self.read_only)}), encoding="utf-8")
            self.assertEqual(load_document(spec_path), self.source)
            self.assertEqual(load_operations(allowed_path), self.read_only)
            selected = selected_contract(load_document(spec_path), load_operations(allowed_path))
            self.assertEqual(set(selected["paths"]), {"/documents", "/documents/{documentId}"})

    def test_invalid_or_missing_external_sources_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, body in (
                ("bad.json", "not JSON"),
                ("scalar.yaml", "just-a-string\n"),
                ("unsafe.yaml", "!!python/object/apply:os.system ['exit 42']\n"),
                ("wrong.txt", "{}"),
            ):
                path = base / name
                path.write_text(body, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ValueError):
                    load_document(path)
            with self.assertRaises(ValueError):
                load_document(base / "missing.json")
            for value in ({}, {"operationIds": []}, {"operationIds": "listDocuments"}, {"operationIds": [5]}):
                path = base / "policy.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_operations(path)

    def test_missing_operations_and_duplicate_ids_fail(self):
        for ids in (frozenset(), frozenset({"missingOperation"})):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                selected_contract(self.source, ids)
        self.source["paths"]["/admin/keys"]["get"]["operationId"] = "listDocuments"
        with self.assertRaises(ValueError):
            selected_contract(self.source, self.read_only)

    def test_unselected_operation_without_id_is_legal(self):
        del self.source["paths"]["/admin/keys"]["get"]["operationId"]
        selected = selected_contract(self.source, self.read_only)
        self.assertNotIn("/admin/keys", selected["paths"])


class EntryPointContracts(unittest.TestCase):
    def test_required_external_inputs(self):
        from openapi_mcp_bridge.__main__ import main
        for missing in (
            ["--api-base", "http://127.0.0.1:8765"],
            ["--api-base", "http://127.0.0.1:8765", "--openapi", str(FIXTURES / "documents.openapi.json")],
        ):
            with self.subTest(args=missing), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                main(missing)
            self.assertEqual(failure.exception.code, 2)

    def test_entry_point_supplies_files_to_bridge(self):
        from openapi_mcp_bridge.__main__ import main
        with (
            patch("openapi_mcp_bridge.__main__.api_client") as client_factory,
            patch("openapi_mcp_bridge.__main__.create_server") as server_factory,
            patch("openapi_mcp_bridge.__main__.asyncio.run") as close,
        ):
            self.assertEqual(main([
                "--api-base", "https://api.example.org",
                "--openapi", str(FIXTURES / "documents.openapi.json"),
                "--allowlist", str(FIXTURES / "read-only.json"),
            ]), 0)
            client_factory.assert_called_once_with("https://api.example.org")
            client, spec, allowed = server_factory.call_args.args
            self.assertIs(client, client_factory.return_value)
            self.assertIn("/documents", spec["paths"])
            self.assertEqual(allowed, frozenset({"listDocuments", "getDocument"}))
            server_factory.return_value.run.assert_called_once_with(transport="stdio")
            close.assert_called_once()


class RealHTTPBoundary(unittest.TestCase):
    def setUp(self):
        self.api = DocumentAPI(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.api.server_port}"
        self.addCleanup(self.close_api)

    def close_api(self):
        self.api.shutdown()
        self.api.server_close()
        self.thread.join(timeout=5)

    def request(self, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), json.load(response)

    def test_real_read_write_and_metadata(self):
        (status, headers, listed) = self.request("/documents")
        self.assertEqual(status, 200)
        self.assertEqual(headers["ETag"], '"demo-1"')
        self.assertEqual(headers["X-Request-ID"], "demo-request")
        self.assertEqual(listed["documents"][0]["id"], "doc-1")

        (status, _, created) = self.request("/documents", {"title": "Second"})
        self.assertEqual(status, 201)
        self.assertEqual(created["title"], "Second")
        (status, _, observed) = self.request("/documents/doc-2")
        self.assertEqual(status, 200)
        self.assertEqual(observed, created)

    def test_invalid_document_is_rejected_without_mutation(self):
        with self.assertRaises(HTTPError) as response:
            self.request("/documents", {"wrong": "shape"})
        self.assertEqual(response.exception.code, 400)
        self.assertEqual(len(self.api.documents), 1)


if __name__ == "__main__":
    unittest.main()
