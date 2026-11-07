"""Keyword search over markdown knowledge roots (ripgrep, or a pure-Python fallback).

Both paths honour a hard deadline; on timeout they return whatever was found so far.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .freshness import RANK, LineInfo, label_file
from .keywords import fold

MAX_LINE_CHARS = 2000
MAX_FILES_SCANNED = 20_000
MAX_RAW_MATCHES = 400
_SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules", "__pycache__", ".venv", "venv"}

_ACCENTS = {"a": "[aáàäâ]", "e": "[eéèëê]", "i": "[iíìïî]", "o": "[oóòöô]", "u": "[uúùüû]",
            "n": "[nñ]", "c": "[cç]"}


@dataclass
class Hit:
    root: Path
    path: Path
    line_no: int
    text: str
    label: str
    date: str
    author: str
    score: int
    semantic: Optional[float] = None

    @property
    def rel(self) -> str:
        try:
            return f"{self.root.name}/{self.path.relative_to(self.root).as_posix()}"
        except ValueError:
            return self.path.name


def accent_pattern(keyword: str) -> str:
    return "".join(_ACCENTS.get(c, re.escape(c)) for c in keyword)


_SUFFIXES = ("ing", "ed", "es", "s")


def stem(keyword: str) -> str:
    """Tiny suffix stripper so 'backups' matches 'backup' and 'retained' matches 'retain'."""
    if "." in keyword or "-" in keyword:
        return keyword
    for suf in _SUFFIXES:
        if keyword.endswith(suf) and len(keyword) - len(suf) >= 4:
            return keyword[: -len(suf)]
    return keyword


class _Kw(str):
    """Keyword carrying a weight (common words across matched files count less)."""
    weight: float = 1.0


def _weighted(rows: Sequence[tuple], kws: Sequence[str]) -> List[str]:
    files = {p for p, _ in rows}
    if len(files) < 4:
        return list(kws)
    seen: Dict[str, set] = {k: set() for k in kws}
    cache: Dict[Path, str] = {}
    for path in files:
        try:
            if path not in cache:
                with open(path, encoding="utf-8", errors="replace") as f:
                    cache[path] = fold(f.read(200_000))
        except OSError:
            continue
        for k in kws:
            if stem(k) in cache[path]:
                seen[k].add(path)
    out = []
    for k in kws:
        w = _Kw(k)
        w.weight = 0.25 if len(seen[k]) > len(files) / 2 else 1.0
        out.append(w)
    return out


def score_line(folded_line: str, kws: Sequence[str]) -> float:
    return sum((3 if "." in k else 1) * getattr(k, "weight", 1.0)
               for k in kws if k in folded_line or stem(k) in folded_line)


_META = re.compile(r"(?:status|author|date|updated)\s*:", re.I)


def _named_file(path: Path, kws: Sequence[str]) -> bool:
    """True when the user named this file explicitly (e.g. 'LEGACY-INFRA.md')."""
    name = fold(path.stem)
    return len(name) >= 4 and any(k == name or k == fold(path.name) for k in kws)


def _glob_ok(rel: str, name: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    if include and not any(fnmatch.fnmatch(name, g) for g in include):
        return False
    for g in exclude:
        g2 = g[1:] if g.startswith("!") else g
        if fnmatch.fnmatch(rel, g2) or fnmatch.fnmatch(name, g2) or fnmatch.fnmatch(rel, g2.lstrip("*/")):
            return False
    return True


def _rg_binary(use_ripgrep: bool) -> Optional[str]:
    if not use_ripgrep:
        return None
    return shutil.which("rg")


def _ripgrep(rg: str, roots: Sequence[Path], kws: Sequence[str], include: Sequence[str],
             exclude: Sequence[str], timeout: float) -> Optional[List[tuple]]:
    pattern = "|".join(accent_pattern(stem(k)) for k in kws)
    cmd = [rg, "-i", "-n", "--no-heading", "--with-filename", "--no-messages", "--color", "never",
           "-M", str(MAX_LINE_CHARS), "--max-columns-preview", "--max-count", "40", "--json"]
    for g in include:
        cmd += ["-g", g]
    for g in exclude:
        cmd += ["-g", g if g.startswith("!") else "!" + g]
    cmd += ["-e", pattern, "--", *[str(r) for r in roots]]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=max(0.05, timeout))
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=0.2)
        except Exception:
            out = ""
    rows = []
    for line in (out or "").splitlines():
        if len(rows) >= MAX_RAW_MATCHES:
            break
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "match":
            continue
        data = ev.get("data") or {}
        path = (data.get("path") or {}).get("text")
        n = data.get("line_number")
        if path and isinstance(n, int):
            rows.append((Path(path), n))
    return rows


def _python_scan(roots: Sequence[Path], kws: Sequence[str], include: Sequence[str],
                 exclude: Sequence[str], deadline: float) -> List[tuple]:
    rows: List[tuple] = []
    scanned = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if time.monotonic() > deadline or scanned >= MAX_FILES_SCANNED or len(rows) >= MAX_RAW_MATCHES:
                    return rows
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                if not _glob_ok(rel, name, include, exclude):
                    continue
                scanned += 1
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        for n, line in enumerate(f, 1):
                            if n > 50_000:
                                break
                            low = fold(line[:MAX_LINE_CHARS])
                            if any(stem(k) in low for k in kws):
                                rows.append((path, n))
                                if len(rows) >= MAX_RAW_MATCHES:
                                    break
                except OSError:
                    continue
    return rows


def _root_of(path: Path, roots: Sequence[Path]) -> Path:
    for r in roots:
        try:
            path.relative_to(r)
            return r
        except ValueError:
            continue
    return path.parent


def search_roots(roots: Sequence[Path], kws: Sequence[str], *, include: Sequence[str] = ("*.md",),
                 exclude: Sequence[str] = (), deadline: float, max_hits: int = 8,
                 max_per_file: int = 2, max_age_days: int = 7, use_ripgrep: bool = True,
                 skip_files: Sequence[Path] = ()) -> List[Hit]:
    """Return ranked, de-duplicated hits (CURRENT > UNVERIFIED > HISTORICAL, then score)."""
    if not roots or not kws or max_hits <= 0:
        return []
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return []
    rg = _rg_binary(use_ripgrep)
    rows = None
    if rg:
        rows = _ripgrep(rg, roots, kws, include, exclude, timeout=remaining)
    if rows is None:
        rows = _python_scan(roots, kws, include, exclude, deadline)
    skip = {Path(p).resolve() for p in skip_files if p}
    kws = _weighted(rows, kws)
    cache: Dict[Path, List[LineInfo]] = {}
    hits: List[Hit] = []
    for path, n in rows:
        if time.monotonic() > deadline:
            break
        try:
            if skip and path.resolve() in skip:
                continue
            if path not in cache:
                cache[path] = label_file(path, max_age_days=max_age_days)
            info = cache[path][n - 1]
        except (OSError, IndexError, UnicodeError):
            continue
        s = score_line(fold(info.text[:MAX_LINE_CHARS]), kws)
        if s <= 0:
            continue
        stripped = info.text.lstrip()
        if stripped.startswith("#") or _META.match(stripped):
            s = s / 2  # headings and Status/Author lines locate a file but carry no fact
        text = info.text[:MAX_LINE_CHARS]
        # Short fact lines often continue on the next line ("...bucket X.\nRetention is 35 days.").
        # Carry one continuation line so a no-tools model still sees the whole fact.
        try:
            nxt = cache[path][n].text.strip()
        except IndexError:
            nxt = ""
        if (nxt and len(text) < 240 and len(nxt) < 200 and not nxt.startswith(("#", "-", "*", "|"))
                and not _META.match(nxt)):
            text = text.rstrip() + " " + nxt
        hits.append(Hit(_root_of(path, roots), path, n, text, info.label,
                        info.date, info.author, s))
    # A file the user names explicitly outranks freshness: asking "what does
    # LEGACY-INFRA.md say?" is a history question even though the file is HISTORICAL.
    hits.sort(key=lambda h: (_named_file(h.path, kws), RANK[h.label], h.score, h.date), reverse=True)
    out: List[Hit] = []
    per_file: Dict[Path, int] = {}
    seen = set()
    for h in hits:
        key = re.sub(r"[\W_]+", " ", fold(h.text)).strip()
        if not key or key in seen or per_file.get(h.path, 0) >= max_per_file:
            continue
        seen.add(key)
        per_file[h.path] = per_file.get(h.path, 0) + 1
        out.append(h)
        if len(out) >= max_hits:
            break
    return out
