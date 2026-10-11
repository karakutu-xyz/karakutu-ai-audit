"""Which model ids actually answered, per audit.

Hosted APIs can serve a different model than the one requested (Scaleway silently reroutes a
retired id), so the run record keeps what the server said it used. A `ContextVar`, like the
runner's log capture: each `run_case` sees only its own calls, even when cases run concurrently.
Kept free of `openai` imports so the runner can use it with the synthetic TestModel.
"""
from contextvars import ContextVar
from typing import Optional, Set

_SERVED: ContextVar[Optional[Set[str]]] = ContextVar("served_model_ids", default=None)


def start() -> tuple:
    """Begin collecting for the current context; returns `(ids, token)` for `stop`."""
    ids: Set[str] = set()
    return ids, _SERVED.set(ids)


def stop(token) -> None:
    _SERVED.reset(token)


def note(model_id: Optional[str]) -> None:
    ids = _SERVED.get()
    if ids is not None and model_id:
        ids.add(model_id)
