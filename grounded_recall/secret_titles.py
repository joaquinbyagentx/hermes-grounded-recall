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

import contextvars
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .keywords import fold
from .note_io import NoteReader

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
    text = (text or "").lstrip("\ufeff \t\r\n").rstrip()
    if not text:
        return []
    if text[:1] in "[{":
        try:
            return titles_from_payload(json.loads(text))
        except ValueError:
            # JSON-looking output is never interpreted as plaintext titles.
            return []
    out = [_clean(line) for line in text.splitlines()[:MAX_TITLES]]
    return sorted({t for t in out if t})


def read_titles_file(path: Optional[Path]) -> List[str]:
    try:
        if path is None:
            return []
        with NoteReader([path.parent]) as reader, reader.open(path) as stream:
            if os.fstat(stream.fileno()).st_size > 5_000_000:
                return []
            text = stream.read(5_000_001)
            return titles_from_text(text) if len(text) <= 5_000_000 else []
    except Exception:
        return []


def command_allowed() -> bool:
    """Vendor CLI identity cannot be inferred from Hermes' context-local secrets.

    Fail closed in routed/multiplexed turns (including their old command cache),
    or if the host cannot establish scope. Explicit title files still work.
    """
    try:
        from agent.secret_scope import serves_routed_profile
        return not serves_routed_profile()
    except Exception:
        return False


# Variables a secret-manager CLI needs to run at all. Everything else (provider API
# keys, tokens, HERMES_* state) is withheld; vendor session variables must be
# named explicitly in ``secret_titles_env``.
BASE_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
                 "TMPDIR", "TERM", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR")


def minimal_env(passthrough: Sequence[str] = (), source: Optional[dict] = None) -> dict:
    """Allowlisted environment for the vendor CLI: base keys + explicit passthrough."""
    src = os.environ if source is None else source
    keep = set(BASE_ENV_KEYS) | {str(k).strip() for k in passthrough if str(k).strip()}
    return {k: src[k] for k in keep if k in src}


def _run_command(argv: Sequence[str], cache: Path, timeout_s: float,
                 environ: Optional[dict] = None) -> None:
    try:
        if not command_allowed():
            return
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s,
                              stdin=subprocess.DEVNULL,
                              env=minimal_env() if environ is None else environ)
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
                          timeout_s: float,
                          env_passthrough: Sequence[str] = ()) -> Optional[threading.Thread]:
    """Start a background refresh when the cache is missing or stale. Never blocks."""
    if not argv or not command_allowed():
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
    context = contextvars.copy_context()
    # Snapshot now (the permitted single-profile caller, not a later turn), allowlisted:
    # never hand provider API keys to a vendor binary.
    env = minimal_env(env_passthrough)
    t = threading.Thread(target=context.run,
                         args=(_run_command, list(argv), cache, max(1.0, timeout_s), env),
                         name="grounded-recall-secret-titles", daemon=True)
    try:
        t.start()
    except Exception:
        with _refresh_lock:
            _refreshing.discard(str(cache))
        return None
    return t


def match_titles(titles: Sequence[str], kws: Sequence[str], limit: int) -> List[str]:
    if limit <= 0:
        return []
    usable = [k for k in kws if len(k) >= 4]
    hits = [t for t in titles if any(k in fold(t) for k in usable)]
    return hits[:limit]
