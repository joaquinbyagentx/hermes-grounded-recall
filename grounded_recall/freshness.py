"""Lexical freshness labels for lines of a markdown page.

This is provenance, not truth inference:

* ``CURRENT``    — the line sits under a "Current state (<date>)" / "Estado vigente
  (<date>)" style heading (or the page's top-level current-state line) whose date
  is at most ``max_age_days`` old.
* ``HISTORICAL`` — the line sits in a history/archive/changelog section, the page
  lives in an archive-like folder, or the line itself is marked as obsolete.
  History always wins over a nested "current" marker.
* ``UNVERIFIED`` — everything else (no date, or a current marker that is stale).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import List, NamedTuple, Optional

from .keywords import fold

CURRENT = "CURRENT"
UNVERIFIED = "UNVERIFIED"
HISTORICAL = "HISTORICAL"
RANK = {HISTORICAL: 0, UNVERIFIED: 1, CURRENT: 2}

MAX_PAGE_BYTES = 1_000_000

_HISTORY = re.compile(
    r"\b(?:history|historical|historic|archived?|archives|obsolete|deprecated|superseded|changelog|"
    r"previous|legacy|old notes?|historico\w*|historial|historia|antecedentes?|previo\w*|obsolet\w*|"
    r"archivad\w*|archivo)\b|no vigente|registro anterior|no longer (?:valid|current|true)")
_HISTORY_LABEL = re.compile(
    r"^(?:history|historical|changelog|archive|previous|historial|historia|historico|antecedentes|"
    r"registro anterior)\b")
_ARCHIVE_DIRS = {"archive", "archives", "archived", "history", "old", "deprecated", "checkpoints",
                 "archivo", "historial", "historico"}
_CURRENT = re.compile(
    r"\b(?:current state|current status|status as of|state as of|last verified|estado vigente|"
    r"estado actual)\s*(?:\(|:|-|\u2014|20\d{2}|\d{1,2}[-/ ]|[a-z]{3,9}\.? \d{1,2})")
_CURRENT_START = re.compile(
    r"^(?:current state|current status|status as of|state as of|last verified|estado vigente|estado actual)")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12,
    "ene": 1, "abr": 4, "ago": 8, "dic": 12, "set": 9,
}
_MON = "(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|ene|abr|ago|dic|set)[a-z]*\\.?"
DATE_RE = re.compile(
    r"20\d\d-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?:\s+[A-Z]{2,5})?)?"
    r"|\b\d{1,2}[- ]" + _MON + r"(?:[- ,]+20\d\d)?\b"
    r"|\b" + _MON + r" \d{1,2}(?:st|nd|rd|th)?(?:,? 20\d\d)?\b", re.I)


class LineInfo(NamedTuple):
    text: str
    label: str
    date: str
    author: str


def parse_date(value: str, today: Optional[date] = None) -> Optional[date]:
    """Parse the first ISO / ``7-Nov[-2026]`` / ``Nov 7[, 2026]`` date in *value*."""
    today = today or date.today()
    if not value:
        return None
    iso = re.search(r"(20\d{2})-(\d{2})-(\d{2})", value)
    try:
        if iso:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        m = re.search(r"\b(\d{1,2})[- ]([a-z]{3})[a-z]*\.?(?:[- ,]+(20\d{2}))?", value, re.I)
        if m and m.group(2).lower() in _MONTHS:
            return date(int(m.group(3) or today.year), _MONTHS[m.group(2).lower()], int(m.group(1)))
        m = re.search(r"\b([a-z]{3})[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?(?:,? (20\d{2}))?", value, re.I)
        if m and m.group(1).lower() in _MONTHS:
            return date(int(m.group(3) or today.year), _MONTHS[m.group(1).lower()], int(m.group(2)))
    except ValueError:
        return None
    return None


def is_recent(value: str, max_age_days: int, today: Optional[date] = None) -> bool:
    today = today or date.today()
    d = parse_date(value, today)
    return d is not None and 0 <= (today - d).days <= max_age_days


def path_is_archive(path: Path) -> bool:
    parts = [fold(p) for p in Path(path).parts]
    return any(p in _ARCHIVE_DIRS for p in parts[:-1]) or bool(_HISTORY.search(fold(Path(path).stem)))


def label_text(text: str, *, archived: bool = False, max_age_days: int = 7,
               today: Optional[date] = None) -> List[LineInfo]:
    """Label every line of a markdown document."""
    today = today or date.today()
    stack: list = []          # (level, label, date)
    block = None              # (label, date) for a non-heading "Current state:" paragraph
    rows: List[LineInfo] = []
    fenced = False
    primary_seen = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
        low = fold(line)
        heading = re.match(r"^(#{1,6})\s+", line) if not fenced else None
        level = len(heading.group(1)) if heading else 0
        if heading:
            stack = [h for h in stack if h[0] < level]
            block = None
        parent_history = archived or any(h[1] == HISTORICAL for h in stack)
        historical = parent_history or bool(_HISTORY.search(low))
        plain = re.sub(r"^[\s>*_#-]+|[\s:*_]+$", "", low)
        quoted = line.startswith(">") or fenced
        marker = bool(_CURRENT.search(low))
        top_current = (marker and not primary_seen and len(stack) <= 1
                       and bool(_CURRENT_START.match(plain)))
        current = marker and (bool(heading) or top_current) and not quoted
        if top_current and current and not historical:
            primary_seen = True
        found = DATE_RE.search(line)
        inherited = block[1] if block else (stack[-1][2] if stack else "")
        if current and found:
            when = found.group(0)
        else:
            when = inherited or (found.group(0) if found else "")
        if historical:
            label = HISTORICAL
        elif current:
            label = CURRENT
        else:
            label = block[0] if block else (stack[-1][1] if stack else UNVERIFIED)
        if label == CURRENT and not is_recent(when, max_age_days, today):
            label = UNVERIFIED
        if quoted:
            label = HISTORICAL if parent_history else UNVERIFIED
        if heading:
            stack.append((level, label, when))
        elif not quoted and _HISTORY_LABEL.match(plain):
            stack.append((7, HISTORICAL, when))   # a plain "History:" label governs until next heading
            block = None
        elif current and not historical:
            block = (label, when)
        elif not line:
            block = None
        author = re.search(r"\b(?:Author|Autor|Owner|Updated by):\s*([^|;\n*]+)", line, re.I)
        rows.append(LineInfo(raw, label, when, author.group(1).strip()[:60] if author else ""))
    return rows


def label_file(path: Path, *, max_age_days: int = 7, today: Optional[date] = None,
               opener=None) -> List[LineInfo]:
    with (opener(path) if opener else open(path, encoding="utf-8", errors="replace")) as f:
        text = f.read(MAX_PAGE_BYTES + 1)
    if len(text) > MAX_PAGE_BYTES:
        return []
    return label_text(text, archived=path_is_archive(path), max_age_days=max_age_days, today=today)


def mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""
