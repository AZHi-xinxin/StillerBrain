"""Load a private development configuration without printing credential values."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {"mcp": "mcp_server.server", "control": "mcp_server.control_server", "gateway": "rikkahub_gateway.server"}
SECRET_KEYS = ("STBRAIN_MCP_TOKEN", "STBRAIN_WAKE_SECRET", "STBRAIN_HOST_TOKEN", "STBRAIN_HUMAN_TOKEN", "STBRAIN_GATEWAY_TOKEN")
DB_KEYS = ("STBRAIN_DB_PATH", "STBRAIN_LEARNING_IDEA_DB_PATH", "STBRAIN_HALLUCINATION_VAULT_DB_PATH")


def validate_self_password_file(value: str, root: Path = ROOT) -> None:
    """Check only an explicitly configured private hash; never disclose its data."""
    if not value:
        return
    try:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError
        if any(part.is_symlink() or getattr(part, "is_junction", lambda: False)()
               for part in (path, *path.parents)):
            raise ValueError
        path = path.resolve(strict=True)
        resolved_root = root.resolve()
        if path == resolved_root or resolved_root in path.parents or not path.is_file():
            raise ValueError
        with path.open("rb") as handle:
            raw = handle.read(1025)
        if len(raw) > 1024:
            raise ValueError
        record = json.loads(raw)
        if (not isinstance(record, dict)
                or set(record) != {"format", "salt_hex", "digest_hex"}
                or record["format"] != "st-self-password-scrypt/1"
                or not isinstance(record["salt_hex"], str)
                or not re.fullmatch(r"[0-9a-fA-F]{32}", record["salt_hex"])
                or not isinstance(record["digest_hex"], str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", record["digest_hex"])):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ValueError("Self-password hash must be a valid private file outside the source repository") from None


def parse_config(text: str) -> dict[str, str]:
    result = {}
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"STBRAIN_[A-Z0-9_]+", key) or key in result:
            raise ValueError(f"Invalid or duplicate config key on line {index}")
        if value.startswith(('"', "'")) or "\x00" in value:
            raise ValueError(f"Unsupported value format on line {index}")
        result[key] = value
    return result


def validate(config: dict[str, str], component: str, root: Path = ROOT) -> None:
    if component not in COMPONENTS:
        raise ValueError("Unknown component")
    required = (*SECRET_KEYS, *DB_KEYS, "STBRAIN_OWNER_ID", "STBRAIN_MODEL_ID", "STBRAIN_HUMAN_ACTOR_ID", "STBRAIN_EXECUTION_EPOCH")
    for key in required:
        if not config.get(key, "").strip() or config[key].upper().startswith("REPLACE_"):
            raise ValueError(f"Missing private configuration: {key}")
    secrets = [config[key] for key in SECRET_KEYS]
    if any(len(value) < 32 for value in secrets) or len(set(secrets)) != len(secrets):
        raise ValueError("Local credentials must be distinct and at least 32 characters")
    if config.get("STBRAIN_REQUIRE_EXECUTION_BINDING") != "1":
        raise ValueError("Strict execution binding must be enabled for all components")
    if config.get("STBRAIN_GATEWAY_CONTEXT_LAYOUT", "legacy") not in {"legacy", "anchored-v1", "tail-context-v2"}:
        raise ValueError("Context layout must be legacy, anchored-v1 or tail-context-v2")
    # Absent/empty/legacy preserves the previous access rules. New installations
    # opt in explicitly through .env.example; a typo must not silently downgrade.
    if config.get("STBRAIN_ACCESS_PROFILE", "") not in {"", "legacy", "simple-memory-v1"}:
        raise ValueError("Access profile must be legacy or simple-memory-v1")
    validate_self_password_file(config.get("STBRAIN_SELF_PASSWORD_HASH_FILE", ""), root)
    if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", config["STBRAIN_EXECUTION_EPOCH"]):
        raise ValueError("Invalid deployment epoch")
    root = root.resolve()
    resolved_databases = []
    for key in DB_KEYS:
        path = Path(config[key]).expanduser()
        if not path.is_absolute():
            raise ValueError(f"Database path must be absolute: {key}")
        path = path.resolve()
        if path == root or root in path.parents:
            raise ValueError(f"Database must be outside the source repository: {key}")
        resolved_databases.append(path)
    if len(set(resolved_databases)) != len(DB_KEYS):
        raise ValueError("Main, ideas and vault databases must be separate files")
    ports = []
    for name in ("MCP", "CONTROL", "GATEWAY"):
        if config.get(f"STBRAIN_{name}_HOST") != "127.0.0.1":
            raise ValueError("Development starter only supports loopback listeners")
        try:
            port = int(config[f"STBRAIN_{name}_PORT"])
        except (KeyError, ValueError):
            raise ValueError(f"Invalid development port: {name}") from None
        if not 1024 <= port <= 65535:
            raise ValueError(f"Invalid development port: {name}")
        ports.append(port)
    if len(set(ports)) != len(ports):
        raise ValueError("Components need distinct ports")
    if config.get("STBRAIN_CONTROL_URL") != f"http://127.0.0.1:{ports[1]}":
        raise ValueError("Control URL must match the local control port")
    if component == "gateway":
        for key in ("STBRAIN_UPSTREAM_API_KEY", "STBRAIN_GATEWAY_MODEL", "STBRAIN_UPSTREAM_MODEL"):
            if not config.get(key, "").strip() or config[key].upper().startswith("REPLACE_"):
                raise ValueError(f"Missing private configuration: {key}")
        upstream = urlsplit(config.get("STBRAIN_UPSTREAM_BASE_URL", ""))
        if upstream.scheme != "https" or not upstream.hostname or upstream.hostname.endswith(".example") or upstream.username or upstream.password or upstream.query or upstream.fragment:
            raise ValueError("A credential-free HTTPS upstream base URL is required")
        if config["STBRAIN_UPSTREAM_API_KEY"] in secrets:
            raise ValueError("Upstream key must be different from local credentials")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--component", required=True, choices=COMPONENTS)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        path = args.config.expanduser().resolve(strict=True)
        if path == ROOT or ROOT in path.parents:
            raise ValueError("Private config must be outside the source repository")
        if path.stat().st_size > 65536:
            raise ValueError("Private config exceeds 64 KiB")
        config = parse_config(path.read_text(encoding="utf-8-sig"))
        validate(config, args.component)
    except (ValueError, OSError, UnicodeError) as exc:
        # Do not echo arbitrary OS messages or file contents containing secrets.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else "Private config could not be read safely"
        print(message, file=sys.stderr)
        return 2
    if args.check:
        print(f"Configuration check passed for {args.component}; no service started")
        return 0
    environment = {key: value for key, value in os.environ.items() if not key.startswith("STBRAIN_")}
    environment.update(config)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.call([sys.executable, "-B", "-m", COMPONENTS[args.component]], cwd=ROOT, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
