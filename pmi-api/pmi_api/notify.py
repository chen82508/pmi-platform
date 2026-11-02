"""Best-effort Slack alert for unhandled API failures (pmi-notify integration).

Mirrors `pmi_ingest.health.notify_failure` / `pmi_workers.notify.notify_failure`:
called once, centrally, at the app's top-level exception boundary
(`main.py`'s catch-all `Exception` handler) rather than scattered across
routes — the same "one hook point covers everything downstream" shape as
`PollerBase.run_once()` for pollers and `worker.py::_execute` for queue jobs.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from pmi_notify import build_failure_message, get_notifier

log = structlog.get_logger(__name__)

# Scopes the Slack alert's filtered traceback to this package's own source,
# same rationale as pmi_ingest.health._INGEST_ROOT.
_API_ROOT = str(Path(__file__).resolve().parent)


async def notify_failure(
    *,
    route: str,
    exc: Exception,
    method: str | None = None,
    request_id: str | None = None,
) -> None:
    """Best-effort Slack alert for an unhandled API exception. Never raises."""
    try:
        message = build_failure_message(route, exc, include_root=_API_ROOT)
        if method:
            message.fields["Method"] = method
        if request_id:
            message.fields["Request ID"] = request_id
        await get_notifier().send(message)
    except Exception:
        log.warning("api.notify_failed", route=route, exc_info=True)
