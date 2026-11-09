"""Settings for grounded-recall.

Resolution order for every key (first hit wins):

1. ``plugins.entries.grounded-recall.settings.<key>`` in ``config.yaml``
   (read through ``ctx.get_config``),
2. the ``GROUNDED_RECALL_<KEY>`` environment variable (non-secret settings only),
3. the default below.

With zero configuration there are no knowledge roots, no key-facts file and no
secret-title source: the per-turn hook still searches past sessions of the
current profile, and everything else is a silent no-op.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, List, Optional

from .profile_scope import routed_values

ENV_PREFIX = "GROUNDED_RECALL_"

DEFAULT_RULE = (
    "Hard rule: before you say that something does not exist, that you have no access, that it is not "
    "in the secret manager, that you do not know, or before you ask the user for information, search the "
    "configured knowledge sources first (notes, secret-manager item titles, past sessions). Every turn you "
    "receive a <grounded-recall> block with what is already written about the topic: read it first, and "
    "open the cited files before relying on them. If something is still missing after searching, say so "
    "and say where you looked. Never invent results."
)


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    # Knowledge roots: markdown folders (an Obsidian vault, a docs tree, ...).
    roots: List[str] = field(default_factory=list)
    include_globs: List[str] = field(default_factory=lambda: ["*.md", "*.markdown"])
    exclude_globs: List[str] = field(default_factory=list)
    use_ripgrep: bool = True
    # System prompt section.
    key_facts_file: str = ""
    key_facts_heading: str = ""
    rule_enabled: bool = True
    rule_text: str = ""
    # Per-turn block.
    max_context_chars: int = 3500
    max_hits: int = 8
    max_hits_per_file: int = 2
    excerpt_chars: int = 320
    max_keywords: int = 10
    min_keyword_length: int = 4
    time_budget_ms: int = 1200
    current_max_age_days: int = 7
    # Past sessions of the active profile.
    sessions_enabled: bool = True
    sessions_lookback_days: int = 7
    max_session_hits: int = 3
    # Secret-manager item TITLES (never values).
    secret_titles_file: str = ""
    secret_titles_command: List[str] = field(default_factory=list)
    # Extra environment variable NAMES the CLI may see (e.g. OP_SESSION_my, BW_SESSION).
    # The CLI otherwise gets only PATH/HOME/USER/LANG-style basics.
    secret_titles_env: List[str] = field(default_factory=list)
    secret_titles_refresh_hours: float = 24.0
    secret_titles_command_timeout_s: float = 20.0
    max_secret_hits: int = 8
    secret_manager_label: str = "secret manager"
    # Audit.
    audit_log: bool = True
    audit_max_bytes: int = 5_000_000
    # Size preset for small-context models: compact | normal | full. A preset only
    # fills keys you did not set explicitly.
    budget: str = "normal"
    key_facts_max_chars: int = 3000
    # Optional semantic re-ranking (OFF by default; the core path needs no model or server).
    # Any OpenAI-compatible POST <url>/embeddings works: Ollama (http://localhost:11434/v1),
    # LM Studio, llama.cpp server, or a cloud API. A tiny embedding model (~0.6B params,
    # e.g. a small multilingual embedding model) is plenty and runs on CPU.
    semantic_enabled: bool = False
    semantic_url: str = ""
    semantic_model: str = ""
    semantic_api_key_env: str = ""
    semantic_timeout_ms: int = 800
    semantic_candidates: int = 40


_LIMITS = {
    "max_context_chars": (300, 20_000),
    "max_hits": (0, 50),
    "max_hits_per_file": (1, 20),
    "excerpt_chars": (40, 2000),
    "max_keywords": (1, 40),
    "min_keyword_length": (2, 20),
    "time_budget_ms": (50, 5000),
    "current_max_age_days": (0, 3650),
    "sessions_lookback_days": (0, 3650),
    "max_session_hits": (0, 20),
    "max_secret_hits": (0, 50),
    "audit_max_bytes": (10_000, 500_000_000),
    "key_facts_max_chars": (0, 3900),
    "semantic_timeout_ms": (50, 1500),
    "semantic_candidates": (1, 200),
}

BUDGETS = {
    "compact": {"max_context_chars": 1200, "max_hits": 3, "max_hits_per_file": 1, "excerpt_chars": 160,
                "max_keywords": 6, "max_session_hits": 1, "max_secret_hits": 3, "key_facts_max_chars": 600,
                "semantic_candidates": 15},
    "normal": {},
    "full": {"max_context_chars": 6000, "max_hits": 14, "max_hits_per_file": 3, "excerpt_chars": 400,
             "max_keywords": 14, "max_session_hits": 5, "max_secret_hits": 12, "key_facts_max_chars": 3900,
             "semantic_candidates": 60},
}


def _coerce(name: str, raw: Any, default: Any) -> Any:
    try:
        if isinstance(default, bool):
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if isinstance(default, int):
            value = int(raw)
        elif isinstance(default, float):
            value = float(raw)
        elif isinstance(default, list):
            if isinstance(raw, str):
                text = raw.strip()
                if text.startswith("["):
                    raw = json.loads(text)
                elif name == "secret_titles_command":
                    import shlex
                    raw = shlex.split(text)
                else:
                    raw = [p for p in text.split(os.pathsep) if p.strip()]
            if not isinstance(raw, (list, tuple)):
                return default
            return [str(x) for x in raw if str(x).strip()]
        else:
            return "" if raw is None else str(raw)
        lo, hi = _LIMITS.get(name, (None, None))
        if lo is not None:
            value = max(lo, min(hi, value))
        return value
    except Exception:
        return default


_UNSET = object()


def load_settings(get_config: Optional[Callable[[str, Any], Any]] = None,
                  environ: Optional[dict] = None) -> Settings:
    """Build settings; an invalid routed profile scope fails closed."""
    scoped = routed_values() if environ is None else None
    env = (os.environ if scoped is None else scoped) if environ is None else environ
    defaults = Settings()
    values = {}
    explicit = set()
    for f in fields(Settings):
        default = getattr(defaults, f.name)
        raw: Any = _UNSET
        if get_config is not None:
            try:
                raw = get_config(f.name, _UNSET)
            except Exception:
                raw = _UNSET
        if raw is _UNSET or raw is None:
            raw = env.get(ENV_PREFIX + f.name.upper(), _UNSET)
        if raw is not _UNSET:
            explicit.add(f.name)
        values[f.name] = default if raw is _UNSET else _coerce(f.name, raw, default)
    budget = str(values.get("budget") or "normal").strip().lower()
    if budget not in BUDGETS:
        budget = "normal"
    values["budget"] = budget
    for key, preset in BUDGETS[budget].items():
        if key not in explicit:
            values[key] = preset
    if str(env.get(ENV_PREFIX + "DISABLE", "")).strip().lower() in ("1", "true", "yes", "on"):
        values["enabled"] = False
    return Settings(**values)


def expand_path(value: str) -> Optional[Path]:
    """Expand ``~`` and ``$VARS``; return None for empty strings."""
    if not value or not str(value).strip():
        return None
    try:
        scoped = routed_values()
        env = os.environ if scoped is None else scoped
        from .sessions import hermes_home
        def substitute(match):
            name = match.group(1) or match.group(2)
            if name == "HERMES_HOME":
                return str(hermes_home())
            return str(env.get(name, match.group(0)))
        path = re.sub(r"\$\{([^}]+)\}|\$(\w+)", substitute,
                      os.path.expanduser(str(value).strip()))
        return Path(path)
    except Exception:
        return None


def existing_roots(settings: Settings) -> List[Path]:
    out: List[Path] = []
    for raw in settings.roots:
        p = expand_path(raw)
        try:
            if p is not None and p.is_dir() and p not in out:
                out.append(p)
        except OSError:
            continue
    return out
