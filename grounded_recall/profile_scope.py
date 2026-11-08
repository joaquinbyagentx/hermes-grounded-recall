"""Profile-local environment overlay without mutating the process environment."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional


def routed_values() -> Optional[Mapping[str, str]]:
    """None means single-profile; routed calls return only their validated scope.

    Do not use get_secret for arbitrary names here: Hermes intentionally treats
    some names/prefixes as process-global, even when a secret scope is installed.
    """
    try:
        from agent.secret_scope import (serves_routed_profile, current_secret_scope,
                                        current_secret_scope_home)
    except ModuleNotFoundError as exc:
        if exc.name in ("agent", "agent.secret_scope"):
            return None  # standalone library, no Hermes routing
        raise
    if not serves_routed_profile():
        return None
    values = current_secret_scope()
    if values is None:
        raise RuntimeError("routed recall requires a profile scope")
    bound_home = current_secret_scope_home()
    if bound_home is not None:
        from .sessions import hermes_home
        if Path(bound_home).resolve() != hermes_home().resolve():
            raise RuntimeError("recall scope does not match the active home")
    return dict(values)
