"""Shared base for every versioned contract (§90).

§90 says: "Use explicit versioned schemas where services communicate. Do not pass
arbitrary dictionaries around." Three properties are enforced here rather than
re-stated in every model:

1. ``extra="forbid"`` — a typo in a field name is an error, not a silently
   ignored key. On a system meant to run unattended for weeks, a silently
   dropped ``bpm`` would surface as mysteriously boring programming.
2. ``frozen=True`` — contracts are *facts in transit*, not mutable state. A
   ``MarketState`` handed to three subsystems must look identical to all three.
   Mutation goes through ``model_copy(update=...)``, which is explicit and
   greppable.
3. ``schema_version`` — a literal on every contract. When a V2 arrives, a
   consumer can branch on the discriminator instead of guessing from field
   presence.

``validate_assignment`` is pointless under ``frozen`` and is therefore not set.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator


def _strip_computed(cls: type[BaseModel], data: Any) -> Any:
    """Remove computed-field keys from input so a contract can round-trip itself.

    ``model_dump`` includes computed fields — ``mid``, ``spread``,
    ``is_instrumental``, ``progress`` — because the frontend and the §51 OBS
    sources want them without recomputing. But ``extra="forbid"`` then rejects
    those same keys on the way back in, which would make
    ``T.model_validate(t.to_json_dict())`` fail.

    That is not a theoretical concern: blueprints are persisted as
    ``to_json_dict()`` and rehydrated with ``model_validate`` on every read, and
    WebSocket frames make the same round trip. Stripping here keeps strict
    unknown-field rejection for genuine typos while making serialise/deserialise
    an identity.
    """
    computed = getattr(cls, "model_computed_fields", None)
    if not computed or not isinstance(data, dict):
        return data
    overlap = computed.keys() & data.keys()
    if not overlap:
        return data
    return {key: value for key, value in data.items() if key not in overlap}


class Contract(BaseModel):
    """Immutable, strictly-validated, versioned payload."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        ser_json_inf_nan="null",
        # Enum values serialise as their string value, which keeps the JSON
        # stored in the database and sent over the WebSocket human-readable.
        use_enum_values=False,
    )

    #: Bumped only on a breaking change. Subclasses override.
    schema_version: ClassVar[int] = 1

    @model_validator(mode="before")
    @classmethod
    def _allow_computed_fields_on_input(cls, data: Any) -> Any:
        return _strip_computed(cls, data)

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-safe dict for persistence and transport.

        ``mode="json"`` matters: it turns datetimes into ISO-8601 strings and
        enums into their values, so the result survives a round trip through
        SQLite's TEXT columns and through a WebSocket frame unchanged.
        """
        return self.model_dump(mode="json")


class MutableContract(BaseModel):
    """Escape hatch for payloads that genuinely accumulate state in place.

    Used sparingly — currently only for queue items, whose status and progress
    change many times per track and for which copy-on-write would be pure noise.
    Still strict about unknown fields.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
        validate_default=True,
        ser_json_inf_nan="null",
    )

    schema_version: ClassVar[int] = 1

    @model_validator(mode="before")
    @classmethod
    def _allow_computed_fields_on_input(cls, data: Any) -> Any:
        return _strip_computed(cls, data)

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


__all__ = ["Contract", "MutableContract"]
