"""MLflow tracing for the core service (Phase 10).

    configure(settings)      once at startup: on only when MLFLOW_TRACING=true
    with span("prepare", "CHAIN", tags={...}) as s: ...

One trace per core operation (prepare a member's script, render audio, answer a callback
question, search benefits). Model calls, embeddings, compliance checks and TTS are child
spans. Spans that would start a trace of their own are only created by ``root=True``
callers, so eval runs (thousands of model calls) never fill MLflow with stray traces.

Tracing never fails a request: with tracing off, or MLflow down, ``span`` yields a no-op
and ``trace_id()`` returns None. Spans carry only what prompts already carry (allowed
member fields, measures, script text).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Protocol

log = logging.getLogger(__name__)

EXPERIMENT = "caregap-traces"

_enabled = False


class Span(Protocol):
    def set_inputs(self, inputs: Any) -> None: ...
    def set_outputs(self, outputs: Any) -> None: ...
    def set_attribute(self, key: str, value: Any) -> None: ...


class _NoSpan:
    def set_inputs(self, inputs: Any) -> None:
        pass

    def set_outputs(self, outputs: Any) -> None:
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass


def configure(enabled: bool, tracking_uri: str = "") -> bool:
    """Turn tracing on or off for this process. Returns whether it is on."""
    global _enabled
    import mlflow

    if not enabled:
        mlflow.tracing.disable()
        _enabled = False
        return False
    try:
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(EXPERIMENT)
        mlflow.tracing.enable()
        _enabled = True
    except Exception as exc:  # MLflow down at startup: run without traces
        log.warning("tracing off: MLflow unavailable (%s)", exc)
        _enabled = False
    return _enabled


def enabled() -> bool:
    return _enabled


@contextmanager
def span(
    name: str, span_type: str = "UNKNOWN", *, root: bool = False,
    attributes: Mapping[str, Any] | None = None, tags: Mapping[str, Any] | None = None,
) -> Iterator[Span]:  # fmt: skip
    """A child span of the current trace, or a new trace when ``root``. ``tags`` go on
    the trace (searchable in MLflow: customer, campaign_id, member_id, ...)."""
    if not _enabled:
        yield _NoSpan()
        return
    import mlflow

    if not root and mlflow.get_current_active_span() is None:
        yield _NoSpan()
        return
    try:
        cm = mlflow.start_span(name, span_type=span_type, attributes=dict(attributes or {}))
        live = cm.__enter__()
    except Exception as exc:
        log.warning("tracing: could not start span %s (%s)", name, exc)
        yield _NoSpan()
        return
    if tags:
        try:
            mlflow.update_current_trace(tags={k: str(v) for k, v in tags.items() if v is not None})
        except Exception as exc:
            log.warning("tracing: could not tag trace (%s)", exc)
    error: BaseException | None = None
    try:
        yield live
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            if error is None:
                cm.__exit__(None, None, None)
            else:
                cm.__exit__(type(error), error, error.__traceback__)
        except Exception as exc:
            log.warning("tracing: could not end span %s (%s)", name, exc)


def trace_id() -> str | None:
    """The current trace's id, to store next to what the trace produced."""
    if not _enabled:
        return None
    import mlflow

    try:
        return mlflow.get_active_trace_id()
    except Exception:
        return None
