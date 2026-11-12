"""Select explicitly enabled OpenAPI operations before exposing MCP components."""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from typing import Any


HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
POLICIES = {"read-only": "read-only.json", "with-create": "with-create.json"}


def captured_contract() -> dict[str, Any]:
    """Load the reviewed application contract from the installed wheel."""
    path = files("openapi_mcp_bridge").joinpath("data", "documents.openapi.json")
    return json.loads(path.read_text(encoding="utf-8"))


def enabled_operations(policy: str) -> frozenset[str]:
    """Load the selected, packaged exposure policy; unknown policy names fail."""
    try:
        policy_file = POLICIES[policy]
    except KeyError as error:
        raise ValueError(f"Unknown exposure policy: {policy!r}") from error
    path = files("openapi_mcp_bridge").joinpath("data", policy_file)
    return frozenset(json.loads(path.read_text(encoding="utf-8"))["operationIds"])


def selected_contract(spec: dict[str, Any], enabled: frozenset[str]) -> dict[str, Any]:
    """Remove every unselected operation before the OpenAPI provider is created.

    This is a positive-selection boundary, not FastMCP route-map filtering.
    Preserve path-item parameters and components needed by the selected routes.
    """
    operation_ids: set[str] = set()
    for path_item in spec["paths"].values():
        for (method, operation) in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation["operationId"]
            if operation_id in operation_ids:
                raise ValueError(f"Duplicate OpenAPI {operation_id = }")
            operation_ids.add(operation_id)

    missing = enabled - operation_ids
    if not enabled or missing:
        raise ValueError(f"Exposure policy must name available operations: {missing = }")

    selected = deepcopy(spec)
    selected["paths"] = {}
    for (path, path_item) in spec["paths"].items():
        admitted = {
            method: deepcopy(operation)
            for (method, operation) in path_item.items()
            if method in HTTP_METHODS and operation["operationId"] in enabled
        }
        if admitted:
            selected["paths"][path] = {
                **{key: deepcopy(value) for (key, value) in path_item.items() if key not in HTTP_METHODS},
                **admitted,
            }

    return selected
