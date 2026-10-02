"""Identifier generation.

Track identifiers are **human-readable and sortable** on purpose: operators read
them aloud, paste them into the originality page, and grep logs for them. §48's
example rejection message cites ``TF-20261001-00917``, so that is the format.

The daily counter is supplied by the caller (normally a database sequence or a
repository count) rather than held in module state, because the API, worker and
playout are separate processes (ADR-08) and module state would collide.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_track_id(when: datetime, daily_sequence: int) -> str:
    """Build a sortable public track id, e.g. ``TF-20261001-00917``.

    ``daily_sequence`` is 1-based and zero-padded to five digits, which allows
    99 999 tracks per day — roughly 240× the realistic rate, so the width will
    not be exhausted.
    """
    if daily_sequence < 1:
        raise ValueError("daily_sequence is 1-based")
    if daily_sequence > 99_999:
        raise ValueError("daily_sequence exceeds the 5-digit id width")
    return f"TF-{when:%Y%m%d}-{daily_sequence:05d}"


def new_job_id() -> str:
    """Opaque generation-job identifier. UUID4 — never user-facing."""
    return str(uuid.uuid4())


def short_id(length: int = 8) -> str:
    """Short, URL-safe, non-sequential id for filenames and correlation keys.

    Not cryptographically meaningful as an authorisation token, but generated
    with ``secrets`` so it is unguessable enough to be safe in a path.
    """
    if length < 4:
        raise ValueError("short_id length must be at least 4")
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


__all__ = ["new_job_id", "new_track_id", "short_id"]
