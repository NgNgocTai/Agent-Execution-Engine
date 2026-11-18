"""Bounded JSON-schema subset shared by skill inputs, smoke tests and runtime.

The same validator checks API arguments, review-time smoke input and script
output, so a shape accepted during review is exactly the shape enforced later.
Unknown keywords are rejected (fail closed) and objects never accept extra
properties.
"""

import json
import math
import re

MAX_DEPTH = 5
MAX_SCHEMA_NODES = 200
MAX_PROPERTIES = 50
MAX_ITEMS = 1000
MAX_STRING = 10000
MAX_ENUM = 50
MAX_VALUE_NODES = 20000
MAX_VALUE_BYTES = 256 * 1024

PROPERTY_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_SCALAR_TYPES = {"string", "integer", "number", "boolean"}
_ALLOWED = {
    "string": {"type", "description", "enum", "minLength", "maxLength"},
    "integer": {"type", "description", "enum", "minimum", "maximum"},
    "number": {"type", "description", "enum", "minimum", "maximum"},
    "boolean": {"type", "description", "enum"},
    "array": {"type", "description", "items", "minItems", "maxItems"},
    "object": {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
    },
}


class SchemaError(ValueError):
    """The declared schema is outside the supported, bounded subset."""


class ValueMismatch(ValueError):
    """A value does not satisfy the schema. The message contains no data."""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bound(schema, key, default, ceiling, floor=0):
    value = schema.get(key, default)
    if not _is_int(value) or not floor <= value <= ceiling:
        raise SchemaError(f"{key} must be an integer from {floor} to {ceiling}")
    return value


def _scalar_matches(kind, value):
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return _is_int(value)
    if kind == "number":
        return _is_number(value) and math.isfinite(value)
    return isinstance(value, bool)


def validate_schema(schema):
    """Raise SchemaError unless schema is a supported, bounded declaration."""
    nodes = 0

    def walk(node, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_SCHEMA_NODES:
            raise SchemaError("Schema has too many nodes")
        if depth > MAX_DEPTH:
            raise SchemaError("Schema is nested too deeply")
        if not isinstance(node, dict):
            raise SchemaError("Schema nodes must be objects")
        kind = node.get("type")
        if kind not in _ALLOWED:
            raise SchemaError("Unsupported schema type")
        if set(node) - _ALLOWED[kind]:
            raise SchemaError("Unsupported schema keyword")
        description = node.get("description", "")
        if not isinstance(description, str) or len(description) > 500:
            raise SchemaError("Schema description is invalid")
        if kind in _SCALAR_TYPES:
            if "enum" in node:
                values = node["enum"]
                if (
                    not isinstance(values, list)
                    or not 1 <= len(values) <= MAX_ENUM
                    or not all(_scalar_matches(kind, item) for item in values)
                ):
                    raise SchemaError("enum must contain matching scalar values")
            if kind == "string":
                low = _bound(node, "minLength", 0, MAX_STRING)
                high = _bound(node, "maxLength", MAX_STRING, MAX_STRING)
                if low > high:
                    raise SchemaError("minLength exceeds maxLength")
            elif kind in {"integer", "number"}:
                for key in ("minimum", "maximum"):
                    if key in node and not (
                        _is_number(node[key]) and math.isfinite(node[key])
                    ):
                        raise SchemaError(f"{key} must be a finite number")
                if node.get("minimum", -math.inf) > node.get("maximum", math.inf):
                    raise SchemaError("minimum exceeds maximum")
        elif kind == "array":
            if "items" not in node:
                raise SchemaError("array requires items")
            low = _bound(node, "minItems", 0, MAX_ITEMS)
            high = _bound(node, "maxItems", MAX_ITEMS, MAX_ITEMS)
            if low > high:
                raise SchemaError("minItems exceeds maxItems")
            walk(node["items"], depth + 1)
        else:
            properties = node.get("properties", {})
            if (
                not isinstance(properties, dict)
                or not properties
                or len(properties) > MAX_PROPERTIES
            ):
                raise SchemaError("object requires 1 to 50 properties")
            if node.get("additionalProperties", False) is not False:
                raise SchemaError("additionalProperties must be false")
            required = node.get("required", [])
            if (
                not isinstance(required, list)
                or len(set(required)) != len(required)
                or any(name not in properties for name in required)
            ):
                raise SchemaError("required must list declared properties once")
            for name, child in properties.items():
                if not isinstance(name, str) or not PROPERTY_NAME.fullmatch(name):
                    raise SchemaError("Invalid property name")
                walk(child, depth + 1)

    walk(schema, 1)


def public_schema(schema):
    """JSON Schema shown to the model: extra properties are explicitly closed."""
    result = dict(schema)
    if schema["type"] == "array":
        result["items"] = public_schema(schema["items"])
    elif schema["type"] == "object":
        result["additionalProperties"] = False
        result["properties"] = {
            name: public_schema(child) for name, child in schema["properties"].items()
        }
    return result


def validate_value(schema, value):
    """Return value unchanged or raise ValueMismatch naming only the path."""
    nodes = 0

    def fail(path, reason):
        raise ValueMismatch(f"{path}: {reason}")

    def walk(node, item, path):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_VALUE_NODES:
            fail(path, "value has too many elements")
        kind = node["type"]
        if kind in _SCALAR_TYPES:
            if not _scalar_matches(kind, item):
                fail(path, f"expected {kind}")
            if "enum" in node and item not in node["enum"]:
                fail(path, "not an allowed value")
            if kind == "string" and not (
                node.get("minLength", 0) <= len(item) <= node.get("maxLength", MAX_STRING)
            ):
                fail(path, "string length out of range")
            if kind in {"integer", "number"} and not (
                node.get("minimum", -math.inf) <= item <= node.get("maximum", math.inf)
            ):
                fail(path, "number out of range")
        elif kind == "array":
            if not isinstance(item, list):
                fail(path, "expected array")
            if not node.get("minItems", 0) <= len(item) <= node.get("maxItems", MAX_ITEMS):
                fail(path, "array length out of range")
            for index, element in enumerate(item):
                walk(node["items"], element, f"{path}[{index}]")
        else:
            if not isinstance(item, dict):
                fail(path, "expected object")
            properties = node["properties"]
            extra = [key for key in item if key not in properties]
            if extra:
                fail(path, "unexpected property " + repr(str(extra[0])[:64]))
            missing = [key for key in node.get("required", []) if key not in item]
            if missing:
                fail(path, "missing property " + repr(missing[0]))
            for key, element in item.items():
                walk(properties[key], element, f"{path}.{key}")

    walk(schema, value, "$")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_VALUE_BYTES:
        fail("$", "value is too large")
    return value
