"""Recent past sessions of the active profile (read-only, deadline-bounded SQLite)."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .keywords import fold, strip_transport


@dataclass
class SessionHit:
    session_id: str
    timestamp: float
    role: str
    text: str


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # public API
        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env) if env else Path.home() / ".hermes"


def profile_name(home: Optional[Path] = None) -> str:
    home = home or hermes_home()
    m = re.search(r"[/\\]profiles[/\\]([^/\\]+)", str(home))
    name = m.group(1) if m else "default"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:64] or "default"


def _query(conn: sqlite3.Connection, kws: Sequence[str], since: float, exclude: str, limit: int):
    fts = " OR ".join('"' + k.replace('"', "") + '"' for k in kws[:6] if "." not in k and k)
    if fts:
        try:
            return conn.execute(
                "SELECT m.session_id, m.timestamp, m.role, m.content FROM messages_fts "
                "JOIN messages m ON m.id = messages_fts.rowid WHERE messages_fts MATCH ? "
                "AND m.role IN ('user','assistant') AND m.timestamp > ? AND m.session_id != ? "
                "ORDER BY rank, m.timestamp DESC LIMIT ?", (fts, since, exclude, limit)).fetchall()
        except sqlite3.Error as exc:
            if "interrupt" in str(exc).lower():
                return []
    like = [k for k in kws[:4] if k]
    if not like:
        return []
    where = " OR ".join("lower(content) LIKE ?" for _ in like)
    return conn.execute(
        "SELECT session_id, timestamp, role, content FROM messages WHERE role IN ('user','assistant') "
        f"AND timestamp > ? AND session_id != ? AND ({where}) ORDER BY timestamp DESC LIMIT ?",
        (since, exclude, *[f"%{k}%" for k in like], limit)).fetchall()


def search_sessions(kws: Sequence[str], *, current_session: str = "", lookback_days: int = 7,
                    limit: int = 3, deadline: float, db_path: Optional[Path] = None) -> List[SessionHit]:
    if not kws or limit <= 0 or lookback_days <= 0:
        return []
    db = db_path or hermes_home() / "state.db"
    if not db.is_file() or time.monotonic() >= deadline:
        return []
    rows = []
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True,
                                     timeout=max(0.05, min(1.0, deadline - time.monotonic())))) as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = _query(conn, kws, time.time() - lookback_days * 86400, current_session or "", 60)
    except Exception:
        return []
    out: List[SessionHit] = []
    seen = set()
    for sid, ts, role, content in rows:
        if not isinstance(content, str):
            continue
        text = strip_transport(content)
        low = fold(text)
        if not any(k in low for k in kws):   # FTS may have matched only a quoted part
            continue
        key = (role, re.sub(r"[\W_]+", " ", low).strip()[:500])
        if key in seen:
            continue
        seen.add(key)
        out.append(SessionHit(str(sid), float(ts or 0), str(role), text))
        if len(out) >= limit:
            break
    return out
