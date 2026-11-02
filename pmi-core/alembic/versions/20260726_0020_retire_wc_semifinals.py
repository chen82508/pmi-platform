"""Retire wc-2026-semifinals (SCD2 close-out).

Data-only migration, same shape as 0016. The def has been `is_current` with no
YAML in the image since it was registered — `run_pipeline` opens
`index_defs/wc-2026-semifinals.yaml`, gets FileNotFoundError, and the whole
`score-all` fan-out reports failed. Every `hourly` and `daily` beat since
2026-07-14 has ended that way, and because the batch job raised a plain
RuntimeError each one also burned its full retry budget re-scoring the other
ten indexes. Its own last score is from 2026-07-14.

Retiring rather than restoring the YAML: the tournament is over (the final
resolved 2026-07-19, and /micah/wc already renders the stood-down state), so
P(reach SF) has nothing left to price.

`/wc/matchups` reads `core_markets` directly and never loads this definition,
so the page is unaffected. History is untouched — ts_index_scores rows and the
SCD2 definition history stay queryable. Idempotent: re-running matches zero rows.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETIRED_INDEX_IDS = ("wc-2026-semifinals",)


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE core_index_definitions"
            " SET is_current = false, effective_to = now()"
            " WHERE index_id IN :ids AND is_current"
        ).bindparams(sa.bindparam("ids", RETIRED_INDEX_IDS, expanding=True))
    )


def downgrade() -> None:
    # Intentionally no-op, as in 0016: reopening is_current would need the
    # original effective_to, and re-registering is a `pmi-core seed` away once
    # a YAML exists again.
    pass
