"""Keyword extraction from the live user request (English + Spanish stopwords)."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, List

STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren't as at be because been before being
below between both but by can cannot could did do does doing done down during each else even ever every
few for from further get gets getting give go goes going gone got had has have having he her here hers
him his how however i if in into is isn't it its itself just know let like look make many may me might
more most much must my need needs never new no nor not now of off often ok okay on once only or other
our ours out over own please really same say see she should show so some still such sure take tell than
thank thanks that the their theirs them then there these they thing things think this those though through
to too under until up use used using very want was way we well were what when where which while who whom
why will with within without would yes yet you your yours check help fix find look looks
que con para por una uno unos unas los las del como esto esta este estos estas pero porque cuando donde
quiero quieres queremos necesito necesitas hazlo haz dale puedes puede pueden revisa revisar revisas todo
todos todas algo ahora ahorita bien mal favor porfa sobre entre desde hasta tiene tienen tengo tenemos hay
ser estar esta estan eso esa ese esos esas otra otro otras otros cosas cosa mas menos muy ya tambien
asi aqui alla alli luego despues antes hacer hace hago hecho dime dices decir sabes saber sirve trae
mismo misma nada nunca siempre solo sola cual cuales quien quienes cuanto cuanta mucho mucha poco poca
gracias hola oye neta verdad seguro listo vale
""".split())

# Words that carry no topic in follow-ups ("can you fix it?", "¿lo arreglas?").
VAGUE = frozenset("""
again anything everything something stuff nothing right wrong done still also same
arreglas arregla afinas afina escupes tonterias tonteria sino carajo
""".split())

_DOMAIN = re.compile(r"\b[\w-]+\.(?:com|net|org|io|ai|dev|app|co|mx|es|uk|de|fr|info|xyz)\b")
_WORD = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")

# Transport wrappers whose bodies are quotes, not the live request.
_MARKER = re.compile(
    r"\[(?:Replying to\s*:|CONTEXT\b|IMPORTANT\b|OUT-OF-BAND\b|grounded-recall\b)", re.I)
_OWN_BLOCK = re.compile(r"<grounded-recall\b.*?(?:</grounded-recall>|\Z)", re.S | re.I)


def fold(text: str) -> str:
    """Casefold and strip accents (``Información`` -> ``informacion``)."""
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                   if not unicodedata.combining(c))


def message_text(message: Any) -> str:
    """Plain text of a user message (str or OpenAI content-part list)."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(p.get("text", "") for p in message
                        if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def strip_transport(text: str) -> str:
    """Remove quoted/transport wrappers (reply quotes, injected context, our own block).

    A balanced scanner is used so a ``]`` inside a quoted reply does not end the
    wrapper early. An unterminated wrapper drops the rest of the text rather than
    letting quoted content become the live request.
    """
    text = _OWN_BLOCK.sub(" ", text)
    for _ in range(50):
        match = _MARKER.search(text)
        if not match:
            break
        is_reply = text[match.start():match.end()].lower().startswith("[replying")
        depth, quote, escaped, end = 1, None, False, match.end()
        for end in range(match.end(), len(text)):
            c = text[end]
            if escaped:
                escaped = False
            elif c == "\\" and quote:
                escaped = True
            elif quote:
                if c == quote:
                    quote = None
            elif is_reply and c in ('"', "\u201c"):
                quote = "\u201d" if c == "\u201c" else c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    break
        text = text[:match.start()] + (" " + text[end + 1:] if depth == 0 else "")
    return text


def keyword_list(folded: str, min_len: int = 4) -> List[str]:
    """Ordered, de-duplicated keywords from already-folded text; specific ones first."""
    kws = _DOMAIN.findall(folded)
    for w in _WORD.findall(folded):
        w = w.strip("-_")
        if len(w) < min_len or w in STOPWORDS or w in VAGUE or w.isdigit():
            continue
        kws.append(w)
    out = list(dict.fromkeys(kws))
    out.sort(key=lambda k: not ("." in k or len(k) >= 7))
    return out


def extract_keywords(message: Any, *, max_keywords: int = 10, min_len: int = 4,
                     history: Iterable[Any] = ()) -> List[str]:
    """Keywords for this turn.

    Short or vague follow-ups ("can you fix it?") borrow topic words from the
    quoted reply and then from the last assistant turns in ``history``.
    """
    raw = message_text(message)
    out = keyword_list(fold(strip_transport(raw)[:3000]), min_len)
    if len(out) < 3:
        m = re.search(r"\[Replying to\s*:\s*(.{0,4000})", raw, re.I | re.S)
        if m:
            out += [k for k in keyword_list(fold(m.group(1)[:2500]), min_len) if k not in out]
    if len(out) < 3:
        prev = []
        try:
            for item in reversed(list(history or [])[-8:]):
                if isinstance(item, dict) and item.get("role") == "assistant":
                    t = message_text(item.get("content"))
                    if len(t) > 40:
                        prev.append(t)
                if len(prev) >= 2:
                    break
        except Exception:
            prev = []
        if prev:
            borrowed = keyword_list(fold(strip_transport(" ".join(prev))[:3000]), min_len)
            out += [k for k in borrowed if k not in out][: max(0, max_keywords - len(out))]
    return out[:max_keywords]
