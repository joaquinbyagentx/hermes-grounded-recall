"""Optional semantic re-ranking over lexical candidates (OFF by default).

Design for small machines: no index, no local model in this process. When enabled,
one request to any OpenAI-compatible ``POST <semantic_url>/embeddings`` endpoint
embeds the query plus the lexical candidate lines (bounded by
``semantic_candidates``), and candidates are re-ordered by cosine similarity
within their freshness tier. Recommended: a tiny embedding model (~0.6B params)
served by Ollama, LM Studio or a llama.cpp server, or any cloud embedding API.

Any error, non-200, malformed payload or timeout returns ``None`` and the caller
keeps the lexical order. The timeout is hard-capped at 1.5 s.
"""
from __future__ import annotations

import json
import math
import urllib.request
from typing import List, Optional, Sequence

from .profile_scope import routed_values

MAX_TIMEOUT_S = 1.5


def _api_key(env_name: str) -> str:
    if not env_name:
        return ""
    scoped = routed_values()
    if scoped is not None:
        return scoped.get(env_name) or ""
    from agent.secret_scope import get_secret
    return get_secret(env_name) or ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """An embeddings endpoint must never forward credentials to another origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if url.endswith("/embeddings") else url + "/embeddings"


def embed(texts: Sequence[str], *, url: str, model: str, timeout_s: float,
          api_key_env: str = "") -> Optional[List[List[float]]]:
    if not url or not model or not texts:
        return None
    if not url.lower().startswith(("http://", "https://")):
        return None
    body = json.dumps({"model": model, "input": [t[:2000] for t in texts]}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "hermes-grounded-recall"}
    try:
        key = _api_key(api_key_env)
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(_endpoint(url), data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=max(0.05, min(MAX_TIMEOUT_S, timeout_s))) as resp:
            payload = json.loads(resp.read(20_000_000))
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            return None
        data = sorted(data, key=lambda d: d.get("index", 0)) if all(isinstance(d, dict) for d in data) else data
        vecs = [d.get("embedding") for d in data]
        if not all(isinstance(v, list) and v for v in vecs):
            return None
        return [[float(x) for x in v] for v in vecs]
    except Exception:
        return None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def rerank_scores(query: str, candidates: Sequence[str], *, url: str, model: str,
                  timeout_s: float, api_key_env: str = "") -> Optional[List[float]]:
    """Similarity of each candidate to *query*, or None (caller keeps lexical order)."""
    if not candidates:
        return None
    vecs = embed([query, *candidates], url=url, model=model, timeout_s=timeout_s, api_key_env=api_key_env)
    if not vecs:
        return None
    q = vecs[0]
    return [cosine(q, v) for v in vecs[1:]]
