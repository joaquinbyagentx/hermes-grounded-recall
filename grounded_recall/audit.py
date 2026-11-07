"""Per-profile JSONL audit log: what was injected, from where, how long it took."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .sessions import hermes_home, profile_name


def state_dir(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / "state" / "grounded-recall"


def audit_path(home: Optional[Path] = None) -> Path:
    home = home or hermes_home()
    return state_dir(home) / f"{profile_name(home)}.jsonl"


def write(record: Dict[str, Any], *, max_bytes: int, home: Optional[Path] = None) -> None:
    """Append one record; rotate to ``.1`` past *max_bytes*. Never raises."""
    try:
        path = audit_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > max_bytes:
                path.replace(path.with_suffix(".jsonl.1"))
        except FileNotFoundError:
            pass
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
