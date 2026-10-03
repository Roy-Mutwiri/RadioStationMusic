"""Review resolution: what the second stage decided, beside what the first stage said.

`verdict` keeps the first-stage answer and is never overwritten. These columns record
what `ReviewResolver` then did with a REVIEW, which class of evidence decided it, and the
resolver version — without which a stored disposition cannot be interpreted later, since
"approved" means something different under different rules.

Revision ID: a8fa42dca4ec
Revises: debd37e50547
Created: 2026-10-03 19:07:48.839816+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders our custom column types by their fully qualified name
# (`tradefix_radio.persistence.types.UtcDateTime()`), so the top-level package name
# must be bound in this module. A plain `import a.b.c` binds `a`, which is exactly
# what that rendering needs — an aliased import would not.
import tradefix_radio.persistence.types  # noqa: F401 - referenced by rendered types

revision: str = 'a8fa42dca4ec'
down_revision: str | None = 'debd37e50547'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('similarity_results', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('final_disposition', sa.String(length=24), nullable=True)
        )
        batch_op.add_column(
            sa.Column('evidence_class', sa.String(length=40), nullable=True)
        )
        batch_op.add_column(
            sa.Column('resolution_reason', sa.String(length=600), nullable=True)
        )
        batch_op.add_column(
            sa.Column('resolver_version', sa.String(length=16), nullable=True)
        )
        batch_op.add_column(
            sa.Column('duplication_risk', sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('creative_similarity', sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('production_references', sa.Integer(), nullable=True)
        )
        batch_op.create_index(
            batch_op.f('ix_similarity_results_evidence_class'), ['evidence_class'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_similarity_results_final_disposition'),
            ['final_disposition'],
            unique=False,
        )



def downgrade() -> None:
    with op.batch_alter_table('similarity_results', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_similarity_results_final_disposition'))
        batch_op.drop_index(batch_op.f('ix_similarity_results_evidence_class'))
        batch_op.drop_column('production_references')
        batch_op.drop_column('creative_similarity')
        batch_op.drop_column('duplication_risk')
        batch_op.drop_column('resolver_version')
        batch_op.drop_column('resolution_reason')
        batch_op.drop_column('evidence_class')
        batch_op.drop_column('final_disposition')

