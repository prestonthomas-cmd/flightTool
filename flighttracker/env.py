"""A very small `.env` reader, so local runs need no shell setup.

Deliberately not `python-dotenv`: this is a handful of lines and keeps the
dependency list short enough to audit at a glance. Real environment variables
always win, so a cron or CI secret is never overwritten by a stale file.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path | str = ".env") -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        return {}

    loaded: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded
