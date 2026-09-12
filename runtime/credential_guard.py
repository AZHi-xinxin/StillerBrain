"""Content-only credential screening, shared by every memory component.

This is a deterministic heuristic, not a guarantee that arbitrary prose contains
no secret. It performs no I/O, mutates no input and returns no matching text.
Callers retain their independent exact-value/issued-capability checks. NFKC is
used only on the detection copy. Adjacent fields are never concatenated.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
import json
import re
import unicodedata
from typing import Any


_HIGH_CONFIDENCE = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    # Union of the ten historical format families, using their lowest existing
    # length threshold. Keep these checks ahead of reference/usage exceptions.
    re.compile(r"\b(?:sk|api|ghp|xox[baprs])[-_][A-Za-z0-9_-]{12,}", re.I),
    re.compile(r"\b(?:rk|pk)_[A-Za-z0-9_-]{20,}", re.I),
    re.compile(r"\bgh[ousr]_[A-Za-z0-9]{20,}", re.I),
)
_H = r"[^\S\r\n]*"
_EN = r"(?<![A-Za-z0-9])(?:password|passwd|api[ _-]?key|private[ _-]?key|(?:access|refresh|auth)[ _-]?token|secret|token|cookie)(?![A-Za-z0-9_])"
_ZH = r"(?:密码|口令|私钥|令牌|密钥)"
_METRIC = r"(?:预算|用量|数量|计数|消耗|总量|usage|budget|count|consumption)"
_CHANGE = r"(?:轮换|更新|变更|更换|修改|重置|设置|切换)"
_EN_CHANGE = r"(?:rotation|update|replacement|rotated|updated|changed|reset|set)"
_LINK = (
    r"(?:[:=]|(?:是|为)" + _H + r"[:=]?"
    r"|(?:已|已经)?" + _CHANGE + _H + r"(?:[:=]|(?:为|成|到)" + _H + r"[:=]?)"
    r"|(?:is|was)\b" + _H + r"[:=]?"
    r"|" + _EN_CHANGE + r"\b" + _H + r"(?:[:=]|(?:to|from|as|is)\b" + _H + r"[:=]?))"
)
_ASSIGNMENT = re.compile(
    r"(?P<label>" + _EN + "|" + _ZH + r")"
    + _H + r"(?:[\"'`]" + _H + r")?"
    + r"(?:(?P<metric>" + _METRIC + r")" + _H + r")?"
    + _LINK,
    re.I,
)
_CLAUSE_BOUNDARY = re.compile(r"[\r\n。！？!?;；]")
_VALUE_BOUNDARY = re.compile(r"[\r\n。！？!?;；,，]")
_USAGE_PREFIX = re.compile(_METRIC + r"(?:统计|记录)?" + _H + r"$", re.I)
_COUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?" + _H + r"(?:tokens?|个(?:token)?|枚)?[.]?", re.I)
_EXPLICIT_COUNT_UNIT = re.compile(r"[0-9]+(?:\.[0-9]+)?" + _H + r"tokens?[.]?", re.I)
_USAGE_KEYS = frozenset({"usage", "token_usage", "token_counts", "token_budget", "token_statistics", "用量", "预算", "token用量", "token预算"})
_KEY_SUFFIX = re.compile(
    r"(?:^|_)(password|passwd|api_key|apikey|secret|token|cookie|private_key|access_token|refresh_token)$",
    re.I,
)
_ZH_KEY = re.compile(r"(?:密码|口令|私钥|令牌|密钥)(?:轮换|更新|变更|更换|修改|设置|重置)?$")
_REF_NAME = r"[A-Z_][A-Z0-9_]*"
_REFERENCE = re.compile(
    r"(?:\$" + _REF_NAME + r"|\$\{" + _REF_NAME + r"\}|%" + _REF_NAME + r"%"
    r"|\$env:" + _REF_NAME
    + r"|env(?:ironment variable| var)?(?:\s*[:=]\s*|\s+)" + _REF_NAME
    + r"|(?:环境变量|变量名?|引用变量)\s*(?:[:=]\s*)?" + _REF_NAME
    + r"|os\.environ\[[\"']" + _REF_NAME + r"[\"']\]"
    r"|os\.getenv\([\"']" + _REF_NAME + r"[\"']\))"
)
_PATH = re.compile(
    r"(?:(?:/|\./|\.\./|~/)[^\s?&#=<>\"'`,;]+/[^\s?&#=<>\"'`,;]+"
    r"|[A-Za-z]:[\\/][^?&#=<>\"'`,;\r\n]+"
    r"|\\\\[^\s?&#=<>\"'`,;]+\\[^?&#=<>\"'`,;\r\n]+)"
)
_JSON_START = re.compile(r"[\[{]")
_JSON_FRAGMENT_LIMIT = 65_536
_JSON_ATTEMPT_LIMIT = 64


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _reference_only(value: str) -> bool:
    """Require one whole explicit reference, not the presence of 'env/path'."""
    value = value.strip().rstrip("。")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'`":
        value = value[1:-1].strip()
    if value.startswith("见"):
        value = value[1:].strip()
    for prefix in ("存放于", "存储于", "保存于", "保存在"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    if _REFERENCE.fullmatch(value) is not None:
        return True
    for prefix in ("路径:", "文件:", "path:", "file:", "路径 ", "文件 "):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    return _PATH.fullmatch(value) is not None


def _label_kind(label: str) -> str | None:
    label = _normalized(label).strip()
    # Accept conventional snake/camel credential keys, not arbitrary substrings
    # such as token_budget/password_policy/password_file or cryptography text.
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", label).lower().replace("-", " ")
    snake = re.sub(r"\s+", "_", snake)
    match = _KEY_SUFFIX.search(snake)
    if match:
        return "token" if snake == "token" else "credential"
    if _ZH_KEY.search(label):
        return "credential"
    return None


def _value_segment(text: str) -> str:
    text = text.lstrip()
    # Quoted scalar assignments and embedded JSON retain their true value;
    # there is no exemption for an arbitrary JSON-shaped string.
    if text.startswith('"'):
        try:
            scalar, _ = json.JSONDecoder().raw_decode(text)
        except (ValueError, RecursionError):
            pass
        else:
            if isinstance(scalar, str):
                return scalar
    return _VALUE_BOUNDARY.split(text, maxsplit=1)[0].strip()


def contains_credential_or_secret(value: Any) -> bool:
    """Screen JSON-like data for credential values without I/O or mutation.

    Explicit references may be stored. Numeric ``token`` values are exempt only
    in a named usage container or a local count/budget expression. Secret-key
    containers propagate their value semantics down to their scalar leaves.
    Cyclic/excessively deep input is rejected conservatively rather than hung.
    """
    active: set[int] = set()

    def scan_plain(text: str) -> bool:
        for match in _ASSIGNMENT.finditer(text):
            scalar = _value_segment(text[match.end():])
            if not scalar:
                continue
            if _reference_only(scalar):
                # An unquoted reference exception covers the whole local
                # assignment, not a prefix before a comma and an actual value.
                tail = text[match.end():].lstrip()
                local_value = _CLAUSE_BOUNDARY.split(tail, maxsplit=1)[0].strip()
                if _reference_only(local_value):
                    continue
            token = match.group("label").casefold() == "token"
            prefix = _CLAUSE_BOUNDARY.split(text[:match.start()])[-1]
            metric = match.group("metric") is not None or _USAGE_PREFIX.search(prefix) is not None
            if token and _COUNT.fullmatch(scalar) and (metric or _EXPLICIT_COUNT_UNIT.fullmatch(scalar)):
                continue
            return True
        return False

    def scan_text(text: str, depth: int) -> bool:
        text = _normalized(text)
        if any(pattern.search(text) for pattern in _HIGH_CONFIDENCE):
            return True
        # A bounded JSON fragment decoder supports both fenced/prefixed data and
        # complete JSON. Every decoded value and every surrounding text segment
        # is still inspected. Never concatenate text around a decoded fragment.
        cursor = search_from = attempts = 0
        while attempts < _JSON_ATTEMPT_LIMIT:
            start_match = _JSON_START.search(text, search_from)
            if start_match is None:
                break
            start = start_match.start()
            attempts += 1
            search_from = start + 1
            try:
                parsed, length = json.JSONDecoder().raw_decode(text[start:start + _JSON_FRAGMENT_LIMIT])
            except (ValueError, RecursionError):
                continue
            if not isinstance(parsed, (dict, list)):
                continue
            prefix = text[cursor:start]
            if scan_plain(prefix):
                return True
            # Preserve sensitive-key meaning in `password: {"value": ...}`;
            # decoding the object must not hide its assignment outside JSON.
            kind = None
            for assignment in _ASSIGNMENT.finditer(prefix):
                if not prefix[assignment.end():].strip():
                    kind = _label_kind(assignment.group("label"))
            if visit(parsed, depth + 1, credential_kind=kind):
                return True
            cursor = search_from = start + length
        # On parse limits or malformed fragments, retain the ordinary lexical
        # scan rather than treating the unparsed remainder as trusted JSON.
        return scan_plain(text[cursor:])

    def secret_leaf(item: Any, kind: str, usage: bool) -> bool:
        if item is None:
            return False
        if isinstance(item, str):
            text = _normalized(item).strip()
            if not text:
                return False
            if any(pattern.search(text) for pattern in _HIGH_CONFIDENCE):
                return True
            if _reference_only(text):
                return False
            return not (kind == "token" and usage and _COUNT.fullmatch(text) is not None)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            return not (kind == "token" and usage and item >= 0)
        return isinstance(item, bool)

    def visit(item: Any, depth: int = 0, credential_kind: str | None = None, usage: bool = False) -> bool:
        if depth > 64:
            return True
        if isinstance(item, str):
            return scan_text(item, depth) or (credential_kind is not None and secret_leaf(item, credential_kind, usage))
        if isinstance(item, (Mapping, Sequence, Set)) and not isinstance(item, (str, bytes, bytearray)):
            identity = id(item)
            if identity in active:
                return True
            active.add(identity)
            try:
                if isinstance(item, Mapping):
                    for key, child in item.items():
                        key_text = _normalized(key) if isinstance(key, str) else ""
                        if key_text and scan_text(key_text, depth + 1):
                            return True
                        kind = credential_kind or _label_kind(key_text)
                        # Only the direct token counter in a named usage map is
                        # a metric. Do not carry that exception through unrelated
                        # nested objects or access_token/refresh_token fields.
                        child_usage = (usage and kind == "token" and credential_kind is None) or (
                            credential_kind is None and key_text.casefold() in _USAGE_KEYS
                        )
                        if visit(child, depth + 1, kind, child_usage):
                            return True
                    return False
                return any(visit(child, depth + 1, credential_kind, usage) for child in item)
            finally:
                active.remove(identity)
        return credential_kind is not None and secret_leaf(item, credential_kind, usage)

    return bool(visit(value))
