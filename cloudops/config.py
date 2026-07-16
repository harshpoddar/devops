"""Credential discovery, local state directory, cache and audit log."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

CLOUDOPS_HOME = Path(os.environ.get("CLOUDOPS_HOME", str(Path.home() / ".cloudops")))
CACHE_DIR = CLOUDOPS_HOME / "cache"
AUDIT_LOG = CLOUDOPS_HOME / "audit.log"

# Every resource this skill creates carries this tag so we can tell
# skill-managed instances apart from anything else in the account.
MANAGED_TAG_KEY = "managed-by"
MANAGED_TAG_VALUE = "cloudops-skill"


def ensure_home() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def audit(event: str, **fields: Any) -> None:
    """Append a JSON line to ~/.cloudops/audit.log (spawn approvals, terminations)."""
    ensure_home()
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
    record.update(fields)
    with AUDIT_LOG.open("a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def vast_api_key() -> Optional[str]:
    key = os.environ.get("VAST_API_KEY")
    if key and key.strip():
        return key.strip()
    for candidate in (
        Path.home() / ".vast_api_key",
        Path.home() / ".config" / "vastai" / "vast_api_key",  # where the official vastai CLI stores it
    ):
        try:
            if candidate.is_file():
                text = candidate.read_text().strip()
                if text:
                    return text
        except OSError:
            continue
    return None


def cache_get(name: str, max_age_seconds: int) -> Optional[Any]:
    path = CACHE_DIR / f"{name}.json"
    try:
        if not path.is_file() or time.time() - path.stat().st_mtime > max_age_seconds:
            return None
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return None


def cache_put(name: str, data: Any) -> None:
    ensure_home()
    (CACHE_DIR / f"{name}.json").write_text(json.dumps(data))
