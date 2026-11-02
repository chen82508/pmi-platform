"""pmi-workers — scheduled + queued worker pool over pmi-core.

P0 surface: `run-job <name>` CLI driven by supercronic. Mirrors the
`micah-job-executor` contract so legacy crontab entries port over verbatim.

P1 surface (`.[arq]` extra): Arq workers for fire-and-forget tasks
(webhook fan-out, WS-triggered single-market re-eval). See README.
"""

from __future__ import annotations

# Importing the jobs package triggers @register decorators so the runner can
# look up jobs by name without callers having to remember to import them.
from pmi_workers import jobs  # noqa: F401  (registration side-effect)
