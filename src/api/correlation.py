"""The correlation id contextvar and the logging filter that stamps it onto every record.

Lives in its own module so the job worker (src/jobs) can bind a job's id without importing
the FastAPI app. `src.api.app` re-exports both names.
"""
import logging
from contextvars import ContextVar

# One id per request (or per job, in the worker), stamped onto every log line the audit emits.
#
# Without it the per-audit narrative is unreconstructable: an audit fans out to ~4 model
# calls, 3 search passes and up to 10 page fetches, each logging its own line, and under any
# concurrency those lines interleave with no way to tell which audit they belong to.
CORRELATION_ID: ContextVar[str] = ContextVar("correlation_id", default="-")


class CorrelationIdFilter(logging.Filter):
    """Injects the current request's id into every record, so the format string can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = CORRELATION_ID.get()
        return True
