"""Market routing: record which market a track was planned against, and aired under.

`tracks.symbol_at_generation` is NOT NULL with a server default of XAUUSD. Existing rows
predate routing and were every one of them planned against gold, so the default states a
fact rather than filling a gap — and the §46 library filter can rely on the column being
present for every track.

`play_events.symbol_at_play` is nullable on purpose: there may genuinely be no active
market at the moment a track airs (the station keeps broadcasting from its buffer when both
feeds are closed), and writing XAUUSD then would be an invention.

Revision ID: 161f873f4932
Revises: 7bcc4aaf4511
Created: 2026-10-03 10:05:58.457712+00:00

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

revision: str = '161f873f4932'
down_revision: str | None = '7bcc4aaf4511'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('play_events', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('symbol_at_play', sa.String(length=32), nullable=True)
        )
        batch_op.create_index(
            batch_op.f('ix_play_events_symbol_at_play'),
            ['symbol_at_play'],
            unique=False,
        )

    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'symbol_at_generation',
                sa.String(length=32),
                server_default=sa.text("'XAUUSD'"),
                nullable=False,
            )
        )
        batch_op.create_index(
            batch_op.f('ix_tracks_symbol_at_generation'),
            ['symbol_at_generation'],
            unique=False,
        )



def downgrade() -> None:
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tracks_symbol_at_generation'))
        batch_op.drop_column('symbol_at_generation')

    with op.batch_alter_table('play_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_play_events_symbol_at_play'))
        batch_op.drop_column('symbol_at_play')

