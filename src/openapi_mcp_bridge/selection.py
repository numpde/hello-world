"""Load an external OpenAPI document and select only explicitly allowed tools."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})


def load_document(path: str | Path) -> dict[str, Any]:
    """Read a JSON or safe YAML mapping supplied by the caller, not the package."""
    source = Path(path)
    extension = source.suffix.lower()
    if extension not in {".json", ".yaml", ".yml"}:
        raise ValueError(f"Expected a .json, .yaml or .yml file: {source}")

    try:
        content = source.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Cannot read {source}: {error}") from error

    try:
        document = json.loads(content) if extension == ".json" else yaml.safe_load(content)
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError(f"Invalid JSON/YAML in {source}: {error}") from error

    if not isinstance(document, dict):
        raise ValueError(f"Expected a JSON/YAML object in {source}")
    return document


def load_operations(path: str | Path) -> frozenset[str]:
    """Read a caller-owned positive operation allowlist (JSON or YAML)."""
    document = load_document(path)
    identifiers = document.get("operationIds")
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or any(not isinstance(identifier, str) or not identifier for identifier in identifiers)
    ):
        raise ValueError(f"Expected nonempty operationIds string list in {path}")
    return frozenset(identifiers)


def selected_contract(spec: dict[str, Any], enabled: frozenset[str]) -> dict[str, Any]:
    """Present FastMCP only the selected operations, never the entire source.

    Leave non-operation path metadata and reusable components available to
    FastMCP. Operations without an operationId are valid but cannot be exposed
    through this explicitly named allowlist.
    """
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI document must contain a paths object")

    operation_ids: set[str] = set()
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            raise ValueError("OpenAPI path items must be objects")
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                raise ValueError("OpenAPI operations must be objects")
            operation_id = operation.get("operationId")
            if operation_id is None:
                continue
            if operation_id in operation_ids:
                raise ValueError(f"Duplicate OpenAPI {operation_id = }")
            operation_ids.add(operation_id)

    missing = enabled - operation_ids
    if not enabled or missing:
        raise ValueError(f"Allowlist must name available operations: {missing = }")

    selected = deepcopy(spec)
    selected["paths"] = {}
    for path, path_item in paths.items():
        admitted = {
            method: deepcopy(operation)
            for method, operation in path_item.items()
            if method in HTTP_METHODS and operation.get("operationId") in enabled
        }
        if admitted:
            selected["paths"][path] = {
                **{key: deepcopy(value) for key, value in path_item.items() if key not in HTTP_METHODS},
                **admitted,
            }
    return selected
