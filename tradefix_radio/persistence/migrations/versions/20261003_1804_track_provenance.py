"""Track provenance: where a track came from, so bench output stops aging real music.

Every existing row becomes `unknown`, deliberately and not as a shortcut. Provenance was
not recorded when those tracks were written, so no column, log or filename proves whether
a given row came from a Phase 7 bench script or a station run — and a provenance column
whose history contains a guess is worse than one that admits it does not know.

`unknown` is excluded from graded creative novelty along with the other non-production
classes: what is actually known about these rows is that nothing proves a listener ever
heard them. Exact-duplicate protection still spans every class, so this loses no safety.

Revision ID: debd37e50547
Revises: cda89a476c06
Created: 2026-10-03 18:04:23.102803+00:00

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

revision: str = 'debd37e50547'
down_revision: str | None = 'cda89a476c06'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'provenance',
                sa.String(length=24),
                server_default=sa.text("'unknown'"),
                nullable=False,
            )
        )
        batch_op.create_index(batch_op.f('ix_tracks_provenance'), ['provenance'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tracks_provenance'))
        batch_op.drop_column('provenance')

