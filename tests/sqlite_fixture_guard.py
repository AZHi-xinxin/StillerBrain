"""Permit only a fixture's exact SQLite paths and bounded existing-file URIs."""
from pathlib import Path


def is_synthetic_sqlite_path(path, allowed, *, uri=False):
    if not isinstance(path, (str, Path)):
        return False
    permitted = {Path(item).resolve() for item in allowed}
    if isinstance(path, str) and path.startswith("file:"):
        # No decoding, arbitrary URI query, authority, alias, or file creation.
        # The caller supplies exact temporary fixture paths, never a directory.
        return uri is True and any(
            item.is_file() and path in {item.as_uri() + "?mode=ro", item.as_uri() + "?mode=rw"}
            for item in permitted
        )
    return Path(path).resolve() in permitted
