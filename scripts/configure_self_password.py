"""Create an optional module-one password hash through hidden local input."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]


def private_target(value: Path, root: Path = ROOT) -> Path:
    """Require an existing private parent and refuse links or existing targets."""
    path = value.expanduser()
    if not path.is_absolute():
        raise ValueError("Choose an absolute path outside the source repository")
    if any(part.is_symlink() or getattr(part, "is_junction", lambda: False)()
           for part in (path, *path.parents)):
        raise ValueError("Use a direct private path, not a symbolic link or junction")
    path = path.resolve()
    resolved_root = root.resolve()
    if path == resolved_root or resolved_root in path.parents:
        raise ValueError("Choose an absolute path outside the source repository")
    if path.exists():
        raise ValueError("The output already exists; choose a new private file")
    if not path.parent.is_dir():
        raise ValueError("Create the private parent directory before running this helper")
    return path


def create_hash(path: Path, password: str, confirmation: str, *, root: Path = ROOT) -> None:
    target = private_target(path, root)
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise ValueError("Use a password containing 1 to 1024 characters")
    if password != confirmation:
        raise ValueError("The two passwords do not match")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    data = (json.dumps({"format": "st-self-password-scrypt/1",
                        "salt_hex": salt.hex(), "digest_hex": digest.hex()},
                       separators=(",", ":")) + "\n").encode("utf-8")
    # Exclusive creation also handles a target appearing after the initial check.
    # On POSIX this starts at 0600; Windows additionally relies on the parent ACL.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="New hash file in an existing private directory outside the source tree")
    args = parser.parse_args(argv)
    try:
        # Check before prompting, and repeat immediately before exclusive creation.
        private_target(args.output)
        # getpass otherwise falls back to visible input on terminals lacking
        # echo control. Abort before it can request an unhidden password.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Module-one password: ")
            confirmation = getpass.getpass("Repeat password: ")
        create_hash(args.output, password, confirmation)
    except (EOFError, KeyboardInterrupt):
        print("Password setup cancelled; no successful creation reported", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, getpass.GetPassWarning):
        # No arbitrary OS message, file path, input value or hash enters stdout.
        print("Password setup failed. Use matching nonempty inputs and a new file in an existing private directory outside the source tree.", file=sys.stderr)
        return 2
    print("Password hash created. Set STBRAIN_SELF_PASSWORD_HASH_FILE to its absolute path in your private config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
