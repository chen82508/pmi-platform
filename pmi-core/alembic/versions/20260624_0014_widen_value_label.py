"""Widen audit_evaluations.value_label 64 -> 256 for extraction-factor JSON labels.

`type: extraction` factors (seat_race_extraction / house_race_extraction) store a
compact JSON label ({state, party, race_type}) in value_label. Under the stub the
output is short, but a real LLM emits e.g.
``{"party": "D", "race_type": "candidate", "state": "Massachusetts"}`` (65 chars),
overflowing the original VARCHAR(64) and failing the whole pipeline tick. 256 gives
ample headroom for any state name + party + race_type.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "audit_evaluations",
        "value_label",
        existing_type=sa.String(64),
        type_=sa.String(256),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "audit_evaluations",
        "value_label",
        existing_type=sa.String(256),
        type_=sa.String(64),
        existing_nullable=True,
    )
