"""Offline, value-free comparison of two explicitly supplied final JSON request bodies.

Does not capture traffic, import gateway/runtime, contact a provider, write files, or
claim that a shared JSON prefix is a provider token/cache prefix. Never print exception
messages, paths, arbitrary JSON keys, field values, hashes or request fragments.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
from typing import Any, Sequence

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_MESSAGES = 100_000
MAX_DEPTH = 128
KNOWN_FIELDS = (
    "model", "temperature", "top_p", "stream", "stream_options", "reasoning_effort",
    "reasoning", "tools", "functions", "tool_choice", "function_call", "max_tokens",
    "max_completion_tokens", "seed", "stop", "response_format",
)
ROLES = ("system", "developer", "user", "assistant", "tool", "function")
RESERVED_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


class ComparisonError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in values:
        if key in result:
            raise ComparisonError("duplicate_json_key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ComparisonError("non_finite_json_number")
    return result


def _reject_constant(_: str) -> None:
    raise ComparisonError("non_finite_json_number")


def _parse(raw: bytes) -> tuple[str, dict[str, Any]]:
    if len(raw) > MAX_BODY_BYTES:
        raise ComparisonError("body_too_large")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ComparisonError("invalid_utf8") from None
    try:
        body = json.loads(text, object_pairs_hook=_pairs, parse_float=_finite_float,
                          parse_constant=_reject_constant)
    except ComparisonError:
        raise
    except (ValueError, RecursionError, OverflowError):
        raise ComparisonError("invalid_json") from None
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise ComparisonError("request_messages_required")
    messages = body["messages"]
    if len(messages) > MAX_MESSAGES:
        raise ComparisonError("too_many_messages")
    if any(not isinstance(message, dict) or not isinstance(message.get("role"), str)
           for message in messages):
        raise ComparisonError("invalid_message_structure")
    # Iterative bound after parsing: no recursive custom traversal or input-provided code.
    pending = [(body, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise ComparisonError("json_too_deep")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return text, body


def _canonical(value: Any) -> str:
    # Used only inside RAM for structural equality; never output this text or its hash.
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def _role(value: str) -> str:
    return value if value in ROLES else "other"


def _shape(messages: list[dict[str, Any]]) -> dict[str, Any]:
    counts = dict.fromkeys((*ROLES, "other"), 0)
    for message in messages:
        counts[_role(message["role"])] += 1
    return {"message_count": len(messages), "role_counts": counts,
            "last_role": _role(messages[-1]["role"]) if messages else None}


def compare_bytes(left: bytes, right: bytes) -> dict[str, Any]:
    left_text, a = _parse(left)
    right_text, b = _parse(right)
    prefix = 0
    for first, second in zip(left, right):
        if first != second:
            break
        prefix += 1
    identical = left == right
    am, bm = a["messages"], b["messages"]
    common_messages = 0
    for first, second in zip(am, bm):
        if _canonical(first) != _canonical(second):
            break
        common_messages += 1
    same_messages = common_messages == len(am) == len(bm)
    known = {
        key: {"left_present": key in a, "right_present": key in b,
              "equal": (key in a) == (key in b) and _canonical(a.get(key)) == _canonical(b.get(key))}
        for key in KNOWN_FIELDS
    }
    excluded = {*KNOWN_FIELDS, "messages"}
    other_a = {key: value for key, value in a.items() if key not in excluded}
    other_b = {key: value for key, value in b.items() if key not in excluded}
    return {
        "contract": "stbrain-offline-final-json-comparison/1",
        "raw_bodies": {
            "left": {"utf8_bytes": len(left), "unicode_codepoints": len(left_text)},
            "right": {"utf8_bytes": len(right), "unicode_codepoints": len(right_text)},
        },
        "raw_comparison": {"identical": identical, "common_prefix_utf8_bytes": prefix,
                           "first_difference_utf8_byte": None if identical else prefix},
        "message_comparison": {
            "identical": same_messages, "common_prefix_messages": common_messages,
            "first_difference_message_index": None if same_messages else common_messages,
            "left": _shape(am), "right": _shape(bm),
        },
        "known_fields": known,
        "other_top_level_fields_equal": _canonical(other_a) == _canonical(other_b),
        "capture_source_verified": False,
        "provider_token_prefix_verified": False,
        "provider_cache_persistence_verified": False,
    }


def _read_local_file(name: str) -> bytes:
    # No UNC/device path, named pipe, symlink/reparse point, URL, directory or whole-tree scan.
    windows = PureWindowsPath(name)
    reserved_device = any(part.rstrip(" .").split(".")[0].upper() in RESERVED_DEVICES for part in windows.parts)
    if (name.startswith(("\\\\", "//")) or "://" in name or reserved_device or
            any(ord(character) < 32 for character in name) or
            (os.name == "nt" and ":" in name[len(windows.drive):])):
        raise ComparisonError("local_regular_file_required")
    path = Path(name)
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or
                getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ComparisonError("local_regular_file_required")
        if before.st_size > MAX_BODY_BYTES:
            raise ComparisonError("body_too_large")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ComparisonError("source_changed")
            raw = stream.read(MAX_BODY_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (opened.st_size, opened.st_mtime_ns, opened.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ComparisonError("source_changed")
        if len(raw) > MAX_BODY_BYTES:
            raise ComparisonError("body_too_large")
        return raw
    except ComparisonError:
        raise
    except (OSError, ValueError):
        raise ComparisonError("file_read_failed") from None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["-h"], ["--help"]):
        print("Usage: python scripts/compare_final_payloads.py FIRST.json SECOND.json")
        print("Offline only; reads two local JSON files; prints value-free counts/comparisons.")
        return 0
    try:
        if len(arguments) != 2:
            raise ComparisonError("expected_two_local_json_files")
        result = compare_bytes(_read_local_file(arguments[0]), _read_local_file(arguments[1]))
    except ComparisonError as error:
        print(json.dumps({"ok": False, "error": error.code}, ensure_ascii=True))
        return 2
    except (MemoryError, RecursionError):
        print('{"ok":false,"error":"comparison_resource_limit"}')
        return 2
    except Exception:
        # No traceback: JSON decoders and filesystem exceptions may include private input.
        print('{"ok":false,"error":"comparison_failed"}')
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
