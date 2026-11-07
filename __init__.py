"""Hermes Agent plugin entry point for grounded-recall.

Registers (public plugin API only):

* ``pre_llm_call`` hook: per-turn lexical recall (optional semantic re-rank) over
  configured markdown roots, secret-manager item titles and this profile's past
  sessions, injected as an escaped ``<grounded-recall>`` evidence block.
* ``grounded-recall.key-facts`` system prompt section: the "search before you say no"
  rule plus an optional key-facts markdown file. Registered only when there is
  something to say (a key-facts file or at least one knowledge root), so a
  zero-config install adds nothing to the prompt.
"""
from __future__ import annotations

import logging

try:
    from .grounded_recall.core import GroundedRecall
except ImportError:  # imported without a parent package (e.g. pytest collecting the repo root)
    from grounded_recall.core import GroundedRecall

SECTION_ID = "grounded-recall.key-facts"
log = logging.getLogger(__name__)


def register(ctx) -> None:
    get_config = getattr(ctx, "get_config", None)
    engine = GroundedRecall(get_config=get_config if callable(get_config) else None)
    ctx.register_hook("pre_llm_call", engine.recall)
    try:
        if engine.wants_section():
            ctx.register_system_prompt_section(SECTION_ID, engine.section,
                                               position="after_memory", max_chars=4000)
    except Exception as exc:  # older Hermes without sections, or id clash: hook still works
        log.warning("grounded-recall: system prompt section not registered: %s", exc)
