"""Optional Sentry error tracking (SHIP-5.2).

`init_sentry(service)` is a safe no-op unless `SENTRY_DSN` is set in the
environment AND the `sentry-sdk` package is importable. It is called from the
entrypoint of each long-running service (pmi-api, pmi-workers, pmi-ingest) so a
single env var turns error reporting on everywhere without touching code.

Design choices:
  * Never raise. A misconfigured DSN or a missing SDK must not crash the
    service — observability is best-effort.
  * Read config from the raw environment (not pydantic settings) so this stays
    importable from any package without a settings dependency cycle.
  * Tag events with the service name + release (PMI_RELEASE / git SHA) so the
    Sentry UI can slice by component and deploy.
"""

from __future__ import annotations

import os

_initialized = False


def init_sentry(service: str) -> bool:
    """Initialize Sentry for `service`. Returns True if it actually started.

    Idempotent: repeated calls after a successful init are ignored.
    """
    global _initialized
    if _initialized:
        return True

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=os.environ.get("PMI_RELEASE") or os.environ.get("IMAGE_TAG"),
            # Conservative defaults — bump traces_sample_rate when you actually
            # want performance traces (costs Sentry quota).
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0") or "0"),
        )
        sentry_sdk.set_tag("service", service)
    except Exception:
        # Swallow — see module docstring. A broken DSN can't take the app down.
        return False

    _initialized = True
    return True
