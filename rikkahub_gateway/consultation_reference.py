"""One client-executed consultation reference reader, never ST authority.

The gateway can validate the published schema and literal request path only.
The Android executor must independently enforce regular-file/no-symlink checks
and bounded UTF-8 reads inside this fixed reference library.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


REFERENCE_TOOL = "workspace_read_file"
REFERENCE_ROOT = "/workspace/orbis-reference-v02/"
_REFERENCE_PATH = re.compile(
    r"/workspace/orbis-reference-v02/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:md|txt)\Z"
)


def is_reference_tool(name: Any, schema: Any) -> bool:
    """Accept exactly the existing Android path-only contract, not aliases.

    Dev38 omits additionalProperties; accepting that declaration does not make
    extra call arguments valid. The call gate below always requires only path.
    Descriptions are harmless schema annotations. No composition, references,
    execution markers, extra fields, defaults or alternative types are allowed.
    """
    if name != REFERENCE_TOOL or not isinstance(schema, Mapping):
        return False
    if (set(schema) - {"type", "properties", "required", "additionalProperties", "description"}
            or schema.get("type") != "object" or schema.get("required") != ["path"]
            or ("additionalProperties" in schema and schema["additionalProperties"] is not False)
            or ("description" in schema and not isinstance(schema["description"], str))):
        return False
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or set(properties) != {"path"}:
        return False
    path = properties["path"]
    return bool(isinstance(path, Mapping) and path.get("type") == "string"
                and not set(path) - {"type", "description"}
                and ("description" not in path or isinstance(path["description"], str)))


def reference_read_allowed(name: Any, schema: Any, arguments: Any) -> bool:
    if (not is_reference_tool(name, schema) or not isinstance(arguments, Mapping)
            or set(arguments) != {"path"}):
        return False
    path = arguments["path"]
    if not isinstance(path, str) or _REFERENCE_PATH.fullmatch(path) is None:
        return False
    filename = path[len(REFERENCE_ROOT):]
    return len(filename) <= 128 and ".." not in filename
