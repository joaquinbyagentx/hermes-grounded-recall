"""Secret-manager item TITLES only — never values.

Sources (both optional):

* ``secret_titles_file``: a JSON file that is either a list of strings or a list of
  objects; only the ``title`` (or ``name``) field of each object is read.
* ``secret_titles_command``: an argv list whose stdout is the same JSON shape or one
  title per line. It runs in a background thread (never on the turn path) at most
  every ``secret_titles_refresh_hours``; its titles are cached in the plugin state dir.

Only short, single-line strings survive; every other field of an object is ignored,
so a value, password or note can never reach the prompt through this module.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .keywords import fold

MAX_TITLE_CHARS = 120
MAX_TITLES = 5000
TITLE_KEYS = ("title", "name")

_refresh_lock = threading.Lock()
_refreshing = set()


def _clean(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    if not value or len(value) > MAX_TITLE_CHARS:
        return None
    return value


def titles_from_payload(payload: Any) -> List[str]:
    """Extract titles from a decoded JSON payload; anything that is not a title is dropped."""
    if isinstance(payload, dict):
        for key in ("items", "data", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            return []
    if not isinstance(payload, list):
        return []
    out: List[str] = []
    for item in payload[:MAX_TITLES]:
        if isinstance(item, dict):
            item = next((item[k] for k in TITLE_KEYS if isinstance(item.get(k), str)), None)
        t = _clean(item)
        if t:
            out.append(t)
    return sorted(set(out))


def titles_from_text(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if text[:1] in "[{":
        try:
            return titles_from_payload(json.loads(text))
        except ValueError:
            pass
    out = [_clean(line) for line in text.splitlines()[:MAX_TITLES]]
    return sorted({t for t in out if t})


def read_titles_file(path: Optional[Path]) -> List[str]:
    try:
        if path is None or not path.is_file() or path.stat().st_size > 5_000_000:
            return []
        return titles_from_text(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []


def _run_command(argv: Sequence[str], cache: Path, timeout_s: float) -> None:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s,
                              stdin=subprocess.DEVNULL)
        if proc.returncode == 0:
            titles = titles_from_text(proc.stdout)
            if titles:
                cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_suffix(".tmp")
                tmp.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
                tmp.replace(cache)
    except Exception:
        pass
    finally:
        with _refresh_lock:
            _refreshing.discard(str(cache))


def refresh_in_background(argv: Sequence[str], cache: Path, refresh_hours: float,
                          timeout_s: float) -> Optional[threading.Thread]:
    """Start a background refresh when the cache is missing or stale. Never blocks."""
    if not argv:
        return None
    try:
        fresh = cache.exists() and time.time() - cache.stat().st_mtime < refresh_hours * 3600
    except OSError:
        fresh = False
    if fresh:
        return None
    with _refresh_lock:
        if str(cache) in _refreshing:
            return None
        _refreshing.add(str(cache))
    t = threading.Thread(target=_run_command, args=(list(argv), cache, max(1.0, timeout_s)),
                         name="grounded-recall-secret-titles", daemon=True)
    t.start()
    return t


def match_titles(titles: Sequence[str], kws: Sequence[str], limit: int) -> List[str]:
    if limit <= 0:
        return []
    usable = [k for k in kws if len(k) >= 4]
    hits = [t for t in titles if any(k in fold(t) for k in usable)]
    return hits[:limit]
