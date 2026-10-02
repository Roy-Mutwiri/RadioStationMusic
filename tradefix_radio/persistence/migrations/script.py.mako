"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

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

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
