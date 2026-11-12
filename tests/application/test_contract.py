"""Document selection and the demo API's genuine HTTP boundary."""

from __future__ import annotations

import json
import threading
import unittest
from copy import deepcopy
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from openapi_mcp_bridge.demo_api import DocumentAPI
from openapi_mcp_bridge.selection import captured_contract, enabled_operations, selected_contract


class ContractSelection(unittest.TestCase):
    def test_default_policy_does_not_expose_mutation_or_administration(self):
        source = captured_contract()
        original = deepcopy(source)
        selected = selected_contract(source, enabled_operations("read-only"))

        self.assertEqual(source, original)
        self.assertEqual(set(selected["paths"]), {"/documents", "/documents/{documentId}"})
        self.assertEqual(set(selected["paths"]["/documents"]), {"get"})
        self.assertEqual(set(selected["paths"]["/documents/{documentId}"]), {"get"})
        self.assertEqual(source["components"], selected["components"])

    def test_write_policy_enables_only_declared_mutation(self):
        selected = selected_contract(captured_contract(), enabled_operations("with-create"))
        self.assertEqual(set(selected["paths"]["/documents"]), {"get", "post"})
        self.assertNotIn("/admin/keys", selected["paths"])

    def test_unknown_or_empty_policy_fails(self):
        with self.assertRaises(ValueError):
            enabled_operations("all")
        for ids in (frozenset(), frozenset({"missingOperation"})):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                selected_contract(captured_contract(), ids)

    def test_duplicate_operation_ids_are_refused(self):
        source = captured_contract()
        source["paths"]["/admin/keys"]["get"]["operationId"] = "listDocuments"
        with self.assertRaises(ValueError):
            selected_contract(source, enabled_operations("read-only"))


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
