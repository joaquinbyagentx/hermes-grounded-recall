"""grounded-recall: the per-turn recall hook and the key-facts system prompt section."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import audit, render, secret_titles, semantic
from .config import DEFAULT_RULE, Settings, existing_roots, expand_path, load_settings
from .freshness import RANK
from .keywords import extract_keywords, message_text, strip_transport
from .search import search_roots
from .sessions import search_sessions

log = logging.getLogger(__name__)

HARD_CAP_S = 1.5            # absolute wall-clock ceiling for one turn's recall
SECTION_MAX_CHARS = 4000    # host limit for one system prompt section
_SETTINGS_TTL_S = 30.0


class GroundedRecall:
    def __init__(self, get_config: Optional[Callable[[str, Any], Any]] = None,
                 environ: Optional[dict] = None):
        self._get_config = get_config
        self._environ = environ
        self._settings: Optional[Settings] = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    # -- settings ------------------------------------------------------------------------------
    def settings(self) -> Settings:
        with self._lock:
            if self._settings is None or time.monotonic() - self._loaded_at > _SETTINGS_TTL_S:
                self._settings = load_settings(self._get_config, self._environ)
                self._loaded_at = time.monotonic()
            return self._settings

    # -- system prompt section -----------------------------------------------------------------
    def wants_section(self) -> bool:
        s = self.settings()
        return s.enabled and bool(s.key_facts_file or (s.rule_enabled and existing_roots(s)))

    def key_facts_body(self, s: Settings) -> str:
        path = expand_path(s.key_facts_file)
        try:
            if path is None or not path.is_file() or path.stat().st_size > 2_000_000:
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        heading = s.key_facts_heading.strip()
        if heading:
            m = re.search(r"(?m)^(#{1,6})\s+.*" + re.escape(heading) + r".*$", text)
            if not m:
                return ""
            level = len(m.group(1))
            rest = text[m.end():]
            end = re.search(r"(?m)^#{1,%d}\s+" % level, rest)
            text = m.group(0) + rest[: end.start() if end else len(rest)]
        return text.strip()

    def section(self, _session_info: Any = None) -> str:
        """Content of the ``grounded-recall.key-facts`` section ('' = skipped). Never raises."""
        try:
            s = self.settings()
            if not s.enabled:
                return ""
            parts = []
            if s.rule_enabled:
                parts.append("# Grounded recall\n" + (s.rule_text.strip() or DEFAULT_RULE))
            body = self.key_facts_body(s)
            if body and s.key_facts_max_chars > 0:
                parts.append(render.truncate(body, s.key_facts_max_chars,
                                             "[truncated: open the key-facts file for the rest]"))
            out = "\n\n".join(parts)
            return render.truncate(out, SECTION_MAX_CHARS - 10, "[truncated]")
        except Exception:
            log.debug("grounded-recall section failed", exc_info=True)
            return ""

    # -- per-turn recall -----------------------------------------------------------------------
    def recall(self, session_id: str = "", user_message: Any = "", conversation_history: Any = None,
               **_kwargs: Any) -> Optional[Dict[str, str]]:
        """``pre_llm_call`` hook. Returns ``{"context": ...}`` or None. Never raises, never
        blocks longer than ``min(time_budget_ms + 300ms, 1.5 s)``."""
        try:
            s = self.settings()
            if not s.enabled:
                return None
        except Exception:
            return None
        box: Dict[str, Any] = {}
        started = time.monotonic()
        limit = min(HARD_CAP_S, s.time_budget_ms / 1000.0 + 0.3)

        def work() -> None:
            try:
                box["result"] = self._recall(s, session_id, user_message, conversation_history,
                                             started + s.time_budget_ms / 1000.0)
            except Exception:
                log.debug("grounded-recall failed", exc_info=True)
                box["result"] = None

        t = threading.Thread(target=work, name="grounded-recall", daemon=True)
        t.start()
        t.join(limit)
        if t.is_alive():
            if s.audit_log:
                audit.write({"session": session_id, "timeout": True,
                             "ms": int((time.monotonic() - started) * 1000)}, max_bytes=s.audit_max_bytes)
            return None
        return box.get("result")

    def _recall(self, s: Settings, session_id: str, user_message: Any, history: Any,
                deadline: float) -> Optional[Dict[str, str]]:
        t0 = time.monotonic()
        kws = extract_keywords(user_message, max_keywords=s.max_keywords,
                               min_len=s.min_keyword_length, history=history or ())
        if not kws:
            return None
        roots = existing_roots(s)
        sources: List[str] = []
        sem_state = "off"

        # 1) Notes: lexical always; optional semantic re-rank of lexical candidates.
        hits = []
        if roots and s.max_hits > 0:
            sources.append("notes")
            want_sem = s.semantic_enabled and bool(s.semantic_url and s.semantic_model)
            pool = max(s.max_hits, s.semantic_candidates) if want_sem else s.max_hits
            hits = search_roots(roots, kws, include=s.include_globs, exclude=s.exclude_globs,
                                deadline=deadline, max_hits=pool, max_per_file=s.max_hits_per_file,
                                max_age_days=s.current_max_age_days, use_ripgrep=s.use_ripgrep,
                                skip_files=[p for p in [expand_path(s.key_facts_file)] if p])
            if want_sem and len(hits) > 1:
                remaining = min(s.semantic_timeout_ms / 1000.0, deadline - time.monotonic(),
                                semantic.MAX_TIMEOUT_S)
                scores = None
                if remaining > 0.05:
                    query = strip_transport(message_text(user_message))[:500] or " ".join(kws)
                    scores = semantic.rerank_scores(query, [h.text for h in hits], url=s.semantic_url,
                                                    model=s.semantic_model, timeout_s=remaining,
                                                    api_key_env=s.semantic_api_key_env)
                if scores is not None:
                    for h, sc in zip(hits, scores):
                        h.semantic = sc
                    hits.sort(key=lambda h: (RANK[h.label], h.semantic), reverse=True)
                    sem_state = "used"
                else:
                    sem_state = "fallback"
            hits = hits[: s.max_hits]

        # 2) Secret-manager titles (never values).
        titles: List[str] = []
        if s.secret_titles_file or s.secret_titles_command:
            sources.append(s.secret_manager_label)
            cache = audit.state_dir() / "secret-titles.json"
            if s.secret_titles_command:
                secret_titles.refresh_in_background(s.secret_titles_command, cache,
                                                    s.secret_titles_refresh_hours,
                                                    s.secret_titles_command_timeout_s)
            pool_titles = set(secret_titles.read_titles_file(expand_path(s.secret_titles_file)))
            if s.secret_titles_command:
                pool_titles |= set(secret_titles.read_titles_file(cache))
            titles = secret_titles.match_titles(sorted(pool_titles), kws, s.max_secret_hits)

        # 3) Past sessions of this profile.
        sess = []
        if s.sessions_enabled and s.max_session_hits > 0:
            sources.append("past sessions")
            sess = search_sessions(kws, current_session=session_id, lookback_days=s.sessions_lookback_days,
                                   limit=s.max_session_hits, deadline=deadline)

        if not (hits or titles or sess) and not roots:
            return None   # zero-config and nothing found: stay silent
        notes = [render.note_line(h, kws, s.excerpt_chars) for h in hits]
        sess_lines = [render.session_line(x, kws, s.excerpt_chars) for x in sess]
        block = render.build_block(kws, notes, titles, sess_lines, max_chars=s.max_context_chars,
                                   secret_label=s.secret_manager_label, searched=sources)
        if s.audit_log:
            audit.write({
                "session": session_id, "budget": s.budget, "keywords": kws,
                "notes": [f"{h.rel}:{h.line_no}|{h.label}" for h in hits if f"{h.rel}:{h.line_no}" in block],
                "titles": sum(1 for t in titles if t in block),
                "sessions": [x.session_id for x in sess if x.session_id in block],
                "semantic": sem_state, "chars": len(block), "sha": audit.digest(block),
                "ms": int((time.monotonic() - t0) * 1000),
            }, max_bytes=s.audit_max_bytes)
        return {"context": block}
