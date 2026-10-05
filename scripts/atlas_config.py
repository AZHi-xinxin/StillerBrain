"""Pure atlas configuration construction; no files, databases or service calls."""
from __future__ import annotations

import hashlib
import json
import re
import secrets

ENABLED = "STBRAIN_ATLAS_BOOTSTRAP_ENABLED"
ID_KEY = "STBRAIN_ATLAS_ID_KEY"
FORBIDDEN = "STBRAIN_ATLAS_FORBIDDEN_VERIFIERS_JSON"
ATLAS_KEYS = (ENABLED, ID_KEY, FORBIDDEN)
REQUIRED_SECRETS = (
    "STBRAIN_MCP_TOKEN", "STBRAIN_WAKE_SECRET", "STBRAIN_HOST_TOKEN",
    "STBRAIN_HUMAN_TOKEN", "STBRAIN_GATEWAY_TOKEN", "STBRAIN_UPSTREAM_API_KEY",
)
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secret_values(config):
    """Include all configured route API keys and optional channel credentials."""
    if any(not isinstance(config.get(name), str) or not config[name] for name in REQUIRED_SECRETS):
        raise ValueError("atlas_required_credentials_missing")
    return frozenset(value for name, value in config.items()
                     if isinstance(value, str) and value and
                     (name == ID_KEY or name in REQUIRED_SECRETS or
                      (name.startswith("STBRAIN_") and name.endswith(("_API_KEY", "_TOKEN", "_SECRET")))))


def validate(config):
    """Validate without instantiating a runtime store (which would write schema)."""
    flag, key, raw = (config.get(name) for name in ATLAS_KEYS)
    if flag in (None, "0") and key is None and raw is None:
        return
    try:
        if flag not in ("0", "1") or not isinstance(key, str) or not HEX64.fullmatch(key):
            raise ValueError()
        if not isinstance(raw, str) or len(raw) > 4096:
            raise ValueError()
        hashes = json.loads(raw)
        if (not isinstance(hashes, list) or not 1 <= len(hashes) <= 32 or
                any(not isinstance(item, str) or not HEX64.fullmatch(item) for item in hashes) or
                len(set(hashes)) != len(hashes) or digest(key) not in hashes):
            raise ValueError()
    except (TypeError, ValueError, RecursionError):
        raise ValueError("atlas_configuration_invalid") from None


def prepare(config, *, enable_missing=False, prior_atlas_schema=False):
    """Return a candidate; never override explicit disable or an existing key.

    A partial group is refused, except for a lone explicit 0. Recovering a lost
    ID key requires its original private backup, not a new random identity.
    """
    validate(config)
    result = dict(config)
    flag = result.get(ENABLED)
    if flag is None and not enable_missing:
        raise ValueError("atlas_enable_requires_explicit_option")
    if flag == "0" and ID_KEY not in result:
        return result
    if ID_KEY not in result:
        if prior_atlas_schema:
            raise ValueError("atlas_existing_schema_requires_original_key")
        result[ID_KEY] = secrets.token_hex(32)
        result[ENABLED] = "1"
    values = secret_values(result)
    # ID key is a distinct identity secret, never an alias for a channel key.
    if any(value == result[ID_KEY] for name, value in result.items()
           if name != ID_KEY and (name in REQUIRED_SECRETS or
                                 name.endswith(("_API_KEY", "_TOKEN", "_SECRET")))):
        raise ValueError("atlas_id_key_must_be_independent")
    existing = json.loads(result[FORBIDDEN]) if FORBIDDEN in result else []
    hashes = sorted(set(existing) | {digest(value) for value in values})
    if len(hashes) > 32:
        raise ValueError("atlas_forbidden_limit_exceeded")
    result[FORBIDDEN] = json.dumps(hashes, separators=(",", ":"))
    validate(result)
    return result


def configure_new(config):
    if any(name in config for name in ATLAS_KEYS):
        raise ValueError("atlas_new_configuration_expected")
    return prepare(config, enable_missing=True)
