"""Small demo-only adapter to the plugin's actual pre_llm_call implementation.

No retrieval or evidence rendering is implemented here. The standing anti-blank
rule lives in GroundedRecall.section(); recall() returns the per-turn evidence.
"""
from __future__ import annotations


def build_injection(message: str, config: dict) -> str:
    from grounded_recall.core import GroundedRecall

    engine = GroundedRecall(get_config=config.get, environ={})
    result = engine.recall(session_id="acme-demo-dry-run", user_message=message,
                           conversation_history=[])
    if not isinstance(result, dict) or not isinstance(result.get("context"), str):
        raise RuntimeError("Real GroundedRecall.recall() returned no context; no fallback evidence")
    return result["context"]
