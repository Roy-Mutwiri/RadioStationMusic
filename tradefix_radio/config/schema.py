"""Validated configuration schema (§50, §71).

§71 says "avoid magic numbers" and "validate configuration on startup; fail
clearly if invalid". Both are structural here:

* Every tunable in the station has a field in this file. If a number appears in
  code without a corresponding setting, that is a bug.
* Validation happens at import-into-process time, not at first use. A station
  that starts and then dies four hours later because ``crossfade_seconds``
  exceeded ``min_track_seconds`` is worse than one that refuses to start.
* Cross-field invariants are checked, not just types. A config where
  ``minimum_buffer_minutes > target_buffer_minutes`` type-checks perfectly and is
  nonsense; those checks are the valuable ones.

Secrets use :class:`~pydantic.SecretStr`, which means an accidental ``print`` or
log of a settings object renders ``**********`` rather than the password (§50,
§68). :meth:`AppSettings.masked_dump` is the only sanctioned way to serialise
settings for the API.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from tradefix_radio.contracts.enums import RunMode


def _default_root() -> Path:
    """Repository root, overridable by ``TRADEFIX_ROOT``.

    Derived from the package location rather than the working directory, because
    the API, worker and playout processes (ADR-08) may be launched from different
    CWDs and must agree on where ``generated/`` lives.
    """
    override = os.environ.get("TRADEFIX_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent


class Section(BaseModel):
    """Base for every settings section: strict, and mutable only via copy."""

    model_config = ConfigDict(extra="forbid", validate_default=True)


# ============================================================ paths


#: Path fields resolved against ``root_dir`` when given as relative.
_RELATIVE_PATH_FIELDS = (
    "data_dir",
    "generated_dir",
    "emergency_dir",
    "log_dir",
    "artwork_dir",
    "models_dir",
    "report_dir",
)


class PathSettings(Section):
    """Filesystem layout. All paths are absolute once validated."""

    root_dir: Path = Field(default_factory=_default_root)
    data_dir: Path = Path("data")
    generated_dir: Path = Path("generated")
    emergency_dir: Path = Path("emergency")
    log_dir: Path = Path("logs")
    artwork_dir: Path = Path("artwork")
    #: Where provider checkpoints live, if a provider needs a local path.
    models_dir: Path = Path("models")
    #: Benchmark/endurance output.
    report_dir: Path = Path("reports")

    @model_validator(mode="before")
    @classmethod
    def _absolutise(cls, data: Any) -> Any:
        """Resolve every relative path against ``root_dir`` before construction.

        A ``before`` validator rather than ``after``: resolving after construction
        would require returning a modified copy, which pydantic v2 warns about and
        which silently does not apply when the model is built via ``__init__``.
        Doing it here means a :class:`PathSettings` instance is *never* observed in
        a half-resolved state, so no subsystem has to wonder what a relative path
        is relative to.
        """
        if not isinstance(data, dict):
            return data
        values = dict(data)
        raw_root = values.get("root_dir")
        root = Path(raw_root).expanduser().resolve() if raw_root else _default_root()
        values["root_dir"] = root
        for name in _RELATIVE_PATH_FIELDS:
            raw = values.get(name)
            if raw is None:
                continue
            candidate = Path(raw).expanduser()
            values[name] = candidate if candidate.is_absolute() else (root / candidate)
        # Fields absent from the input keep their class default, which is
        # relative; fill them in explicitly so the invariant holds for every field.
        for name in _RELATIVE_PATH_FIELDS:
            if name not in values:
                default = cls.model_fields[name].default
                if isinstance(default, Path) and not default.is_absolute():
                    values[name] = root / default
        return values

    def all_directories(self) -> tuple[Path, ...]:
        """Every directory the station needs to exist (§73 startup check)."""
        return (
            self.data_dir,
            self.generated_dir,
            self.emergency_dir,
            self.log_dir,
            self.artwork_dir,
            self.report_dir,
        )


# ============================================================ database


class DatabaseSettings(Section):
    #: A relative SQLite path is resolved against ``paths.data_dir`` by
    #: :meth:`AppSettings._resolve_sqlite_path`, never against the working
    #: directory — the three station processes (ADR-08) may be launched from
    #: different CWDs and must agree on which file is the database.
    url: str = "sqlite+aiosqlite:///tradefix.db"
    echo: bool = False
    pool_size: int = Field(default=5, ge=1, le=100)
    max_overflow: int = Field(default=10, ge=0, le=100)
    #: SQLite only: how long to wait on a write lock before erroring. Matters
    #: because three processes share the file (ADR-05/ADR-08).
    sqlite_busy_timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    statement_timeout_seconds: float = Field(default=30.0, gt=0.0)

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @field_validator("url")
    @classmethod
    def _known_dialect(cls, value: str) -> str:
        if not value.startswith(("sqlite+aiosqlite:", "postgresql+asyncpg:")):
            raise ValueError(
                "database.url must use sqlite+aiosqlite or postgresql+asyncpg "
                f"(async drivers are required); got {value.split(':')[0]!r}"
            )
        return value


# ============================================================ logging


class LoggingSettings(Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console_format: Literal["console", "json"] = "console"
    file_enabled: bool = True
    #: Rotation size. §55 requires logs to rotate and not fill the disk.
    max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    backup_count: int = Field(default=10, ge=1, le=200)
    #: Emit one structured heartbeat line at this interval so a silent log is
    #: distinguishable from a dead process.
    heartbeat_seconds: float = Field(default=60.0, gt=0.0)


# ============================================================ market


class MarketSettings(Section):
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)
    feed: Literal["simulated", "metatrader5", "rest", "replay"] = "simulated"
    #: Brokers name gold inconsistently; tried in order (ADR-03).
    symbol_aliases: tuple[str, ...] = (
        "XAUUSD",
        "XAUUSD.m",
        "XAUUSDm",
        "GOLD",
        "GOLD.spot",
        "XAUUSD_",
    )
    poll_interval_seconds: float = Field(default=1.0, gt=0.0, le=60.0)
    #: Beyond this age data is STALE and prices must not be displayed (§32).
    stale_after_seconds: float = Field(default=15.0, gt=0.0)
    #: Beyond this the feed is DISCONNECTED and programming goes neutral (§63-E).
    disconnected_after_seconds: float = Field(default=90.0, gt=0.0)

    #: Candle interval the feature engine aggregates to.
    bar_seconds: int = Field(default=60, ge=1, le=3600)
    #: Rolling window for percentile normalisation, in bars (§6).
    percentile_window_bars: int = Field(default=720, ge=30, le=20_000)
    #: Bars required before features are trustworthy.
    warmup_bars: int = Field(default=60, ge=5, le=5_000)

    rest_base_url: str = ""
    rest_api_key: SecretStr = SecretStr("")
    replay_file: Path | None = None

    @model_validator(mode="after")
    def _check(self) -> MarketSettings:
        if self.disconnected_after_seconds <= self.stale_after_seconds:
            raise ValueError(
                "market.disconnected_after_seconds must exceed "
                f"market.stale_after_seconds ({self.disconnected_after_seconds} "
                f"<= {self.stale_after_seconds})"
            )
        if self.percentile_window_bars <= self.warmup_bars:
            raise ValueError(
                "market.percentile_window_bars must exceed market.warmup_bars "
                f"({self.percentile_window_bars} <= {self.warmup_bars})"
            )
        if self.feed == "rest" and not self.rest_base_url:
            raise ValueError("market.rest_base_url is required when market.feed='rest'")
        if self.feed == "replay" and self.replay_file is None:
            raise ValueError("market.replay_file is required when market.feed='replay'")
        if self.symbol not in self.symbol_aliases:
            # Not fatal, but almost always a mistake: the configured symbol should
            # be the first thing the discovery step tries.
            raise ValueError(
                f"market.symbol {self.symbol!r} is absent from market.symbol_aliases; "
                "add it so symbol discovery tries it first"
            )
        return self


class EnergyWeights(Section):
    """§6 energy inputs. Weights are normalised at load, so they need not sum to 1."""

    atr_percentile: float = Field(default=0.22, ge=0.0, le=1.0)
    realized_volatility: float = Field(default=0.18, ge=0.0, le=1.0)
    price_velocity: float = Field(default=0.14, ge=0.0, le=1.0)
    trend_strength: float = Field(default=0.14, ge=0.0, le=1.0)
    volume_percentile: float = Field(default=0.08, ge=0.0, le=1.0)
    breakout_strength: float = Field(default=0.12, ge=0.0, le=1.0)
    range_expansion: float = Field(default=0.07, ge=0.0, le=1.0)
    momentum: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _non_zero(self) -> EnergyWeights:
        if self.total <= 0:
            raise ValueError("energy.weights must not all be zero")
        return self

    @property
    def total(self) -> float:
        return sum(self.as_dict().values())

    def as_dict(self) -> dict[str, float]:
        return {
            "atr_percentile": self.atr_percentile,
            "realized_volatility": self.realized_volatility,
            "price_velocity": self.price_velocity,
            "trend_strength": self.trend_strength,
            "volume_percentile": self.volume_percentile,
            "breakout_strength": self.breakout_strength,
            "range_expansion": self.range_expansion,
            "momentum": self.momentum,
        }

    def normalised(self) -> dict[str, float]:
        """Weights scaled to sum to 1.0 (§6 'configurable weights')."""
        total = self.total
        return {key: value / total for key, value in self.as_dict().items()}


class EnergySettings(Section):
    weights: EnergyWeights = Field(default_factory=EnergyWeights)
    #: Exponential smoothing factor for ``smoothed_energy``. Lower = smoother.
    smoothing_alpha: float = Field(default=0.15, gt=0.0, le=1.0)
    #: Window over which ``energy_velocity`` is measured, in bars.
    velocity_window_bars: int = Field(default=5, ge=2, le=200)


class RegimeSettings(Section):
    """§5 anti-flicker controls.

    These four settings together are what stop RANGE→BREAKOUT→RANGE oscillation.
    They are separate knobs rather than one "stability" number because they fix
    different failure modes: ``min_duration`` stops rapid churn,
    ``confirmation_bars`` stops single-candle false positives,
    ``hysteresis_margin`` stops dithering at a threshold boundary, and
    ``cooldown`` stops immediately returning to a regime we just left.
    """

    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    min_duration_seconds: float = Field(default=180.0, ge=0.0)
    #: Consecutive bars a candidate regime must win before it is adopted.
    confirmation_bars: int = Field(default=3, ge=1, le=100)
    #: Extra score a challenger must beat the incumbent by, in points.
    hysteresis_margin: float = Field(default=8.0, ge=0.0, le=50.0)
    #: After leaving a regime, how long before it may be re-entered.
    cooldown_seconds: float = Field(default=120.0, ge=0.0)
    #: Score smoothing applied before classification.
    score_smoothing_alpha: float = Field(default=0.3, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _sane(self) -> RegimeSettings:
        # Only meaningful when a minimum duration is actually set. With
        # min_duration_seconds = 0 ("no minimum", a legitimate choice for testing and
        # for a deliberately twitchy station) the ratio degenerates and any positive
        # cooldown would be rejected.
        if self.min_duration_seconds > 0 and self.cooldown_seconds > self.min_duration_seconds * 10:
            raise ValueError(
                "regime.cooldown_seconds is implausibly large relative to "
                f"min_duration_seconds ({self.cooldown_seconds} > "
                f"{self.min_duration_seconds * 10}); the engine would be unable to react"
            )
        return self


# ============================================================ music & diversity


class BpmBand(Section):
    """A BPM range, used per energy band and per regime."""

    low: int = Field(ge=40, le=220)
    high: int = Field(ge=40, le=220)

    @model_validator(mode="after")
    def _ordered(self) -> BpmBand:
        if self.low > self.high:
            raise ValueError(f"bpm band low ({self.low}) exceeds high ({self.high})")
        return self


class MusicSettings(Section):
    """§8–§10 composition policy. Genre *content* lives in genres.yaml."""

    #: Path to the genre library, relative to the config directory.
    genre_library_file: str = "genres.yaml"
    persona_file: str = "personas.yaml"

    min_duration_seconds: int = Field(default=150, ge=30, le=600)
    max_duration_seconds: int = Field(default=260, ge=30, le=600)

    #: Energy-to-BPM mapping, keyed by the lower bound of the energy band.
    #: Mirrors the §1 examples without hard-coding them as if/else (§1).
    bpm_bands: dict[int, BpmBand] = Field(
        default_factory=lambda: {
            0: BpmBand(low=72, high=92),
            25: BpmBand(low=86, high=104),
            45: BpmBand(low=98, high=124),
            65: BpmBand(low=118, high=142),
            82: BpmBand(low=134, high=160),
        }
    )

    #: Probability vocals are enabled, by energy band lower bound (§1: stronger
    #: vocal probability at high energy).
    vocal_probability_by_energy: dict[int, float] = Field(
        default_factory=lambda: {0: 0.25, 25: 0.35, 45: 0.5, 65: 0.62, 82: 0.7}
    )

    #: Weighting applied to session personality bias (§97). Market energy has
    #: higher priority, so this is deliberately below 1.0.
    session_bias_strength: float = Field(default=0.35, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> MusicSettings:
        if self.min_duration_seconds >= self.max_duration_seconds:
            raise ValueError(
                "music.min_duration_seconds must be below max_duration_seconds "
                f"({self.min_duration_seconds} >= {self.max_duration_seconds})"
            )
        if not self.bpm_bands:
            raise ValueError("music.bpm_bands must not be empty")
        if 0 not in self.bpm_bands:
            raise ValueError("music.bpm_bands must include a band starting at 0")
        for bound in self.bpm_bands:
            if not 0 <= bound <= 100:
                raise ValueError(f"music.bpm_bands key {bound} is outside 0-100")
        if 0 not in self.vocal_probability_by_energy:
            raise ValueError("music.vocal_probability_by_energy must include a 0 band")
        for bound, probability in self.vocal_probability_by_energy.items():
            if not 0 <= bound <= 100:
                raise ValueError(f"music.vocal_probability_by_energy key {bound} outside 0-100")
            if not 0.0 <= probability <= 1.0:
                raise ValueError(
                    f"music.vocal_probability_by_energy[{bound}] = {probability} outside 0-1"
                )
        return self

    def bpm_band_for(self, energy: float) -> BpmBand:
        """The band whose lower bound is the greatest not exceeding ``energy``."""
        bound = max(b for b in self.bpm_bands if b <= max(0.0, min(100.0, energy)))
        return self.bpm_bands[bound]

    def vocal_probability_for(self, energy: float) -> float:
        bound = max(
            b for b in self.vocal_probability_by_energy if b <= max(0.0, min(100.0, energy))
        )
        return self.vocal_probability_by_energy[bound]


class DiversitySettings(Section):
    """§11 anti-boredom rules and §12 horizons."""

    max_same_genre_consecutive: int = Field(default=2, ge=1, le=10)
    max_same_vocal_type_consecutive: int = Field(default=2, ge=1, le=10)
    max_instrumental_consecutive: int = Field(default=4, ge=1, le=20)
    #: BPM within +/- this value is "the same" for repetition purposes.
    bpm_tolerance: int = Field(default=4, ge=0, le=20)
    bpm_repeat_horizon: int = Field(default=4, ge=1, le=50)
    key_repeat_horizon: int = Field(default=4, ge=1, le=50)
    topic_repeat_horizon: int = Field(default=10, ge=1, le=500)
    persona_repeat_horizon: int = Field(default=3, ge=1, le=50)
    duration_tolerance_seconds: int = Field(default=10, ge=0, le=120)
    duration_repeat_horizon: int = Field(default=3, ge=1, le=50)
    #: Genre share ceiling over the medium horizon, as a fraction.
    max_genre_share_medium: float = Field(default=0.35, gt=0.0, le=1.0)
    #: Below this diversity score the director is pushed toward novelty (§11).
    diversity_floor: float = Field(default=55.0, ge=0.0, le=100.0)

    #: §12 horizons, in track counts.
    horizon_short: int = Field(default=5, ge=1, le=100)
    horizon_medium: int = Field(default=20, ge=2, le=1_000)
    horizon_long: int = Field(default=100, ge=3, le=100_000)

    @model_validator(mode="after")
    def _ordered_horizons(self) -> DiversitySettings:
        if not self.horizon_short < self.horizon_medium < self.horizon_long:
            raise ValueError(
                "diversity horizons must be strictly increasing: "
                f"short={self.horizon_short} medium={self.horizon_medium} "
                f"long={self.horizon_long}"
            )
        return self


class LyricsSettings(Section):
    """§13–§17 lyric policy. Topic *content* lives in topics.yaml."""

    topic_file: str = "topics.yaml"
    #: Probability distribution over Trade Fix mention counts (§16).
    tradefix_mention_weights: dict[int, float] = Field(
        default_factory=lambda: {0: 0.55, 1: 0.35, 2: 0.10}
    )
    educational_intensity_range: tuple[float, float] = (0.15, 0.75)
    max_lyric_words: int = Field(default=420, ge=20, le=4_000)
    min_lyric_words: int = Field(default=40, ge=4, le=1_000)
    #: Jaccard similarity above which lyrics count as a near-duplicate (§17).
    similarity_threshold: float = Field(default=0.55, gt=0.0, le=1.0)
    #: A single phrase repeated more than this many times is rejected (§17).
    max_phrase_repetitions: int = Field(default=6, ge=2, le=50)
    #: Minimum distinct-word ratio; catches gibberish and padding (§17).
    min_lexical_diversity: float = Field(default=0.22, gt=0.0, le=1.0)

    #: What to do when a vocal blueprint cannot be given validated lyrics.
    #:
    #: ``instrumental`` realises the track without words and records why; ``fail`` gives
    #: up on the track entirely. Never a third option — the provider is not permitted to
    #: invent its own trading lyrics, which is the §7.10 boundary this whole path exists
    #: to hold.
    #:
    #: Instrumental by default, because a radio station with a draining buffer is better
    #: served by a track without words than by no track, and the fallback is recorded
    #: rather than silent.
    on_lyric_failure: Literal["instrumental", "fail"] = "instrumental"

    #: Buffer levels at which the station stops *asking* for vocals.
    #:
    #: Vocals cost two composition attempts and a validation pass before the GPU is even
    #: touched, and they fail more often than instrumentals. When the buffer is the
    #: emergency, survival outranks variety — but only for new requests: vocals are never
    #: switched off permanently, and a healthy buffer restores normal diversity.
    suppress_vocals_at_buffer: tuple[str, ...] = ("critical",)

    @model_validator(mode="after")
    def _check(self) -> LyricsSettings:
        if self.min_lyric_words >= self.max_lyric_words:
            raise ValueError(
                "lyrics.min_lyric_words must be below max_lyric_words "
                f"({self.min_lyric_words} >= {self.max_lyric_words})"
            )
        if not self.tradefix_mention_weights:
            raise ValueError("lyrics.tradefix_mention_weights must not be empty")
        for count, weight in self.tradefix_mention_weights.items():
            if count < 0 or count > 2:
                raise ValueError(
                    f"lyrics.tradefix_mention_weights key {count} outside 0-2; "
                    "more than two mentions per song is brand spam (§16)"
                )
            if weight < 0:
                raise ValueError(f"lyrics.tradefix_mention_weights[{count}] is negative")
        if sum(self.tradefix_mention_weights.values()) <= 0:
            raise ValueError("lyrics.tradefix_mention_weights must not sum to zero")
        low, high = self.educational_intensity_range
        if not 0.0 <= low <= high <= 1.0:
            raise ValueError(
                f"lyrics.educational_intensity_range must satisfy 0 <= low <= high <= 1, "
                f"got ({low}, {high})"
            )
        return self


# ============================================================ radio


class RadioSettings(Section):
    """§26–§31 playout policy."""

    minimum_buffer_minutes: float = Field(default=20.0, gt=0.0, le=600.0)
    target_buffer_minutes: float = Field(default=45.0, gt=0.0, le=600.0)
    maximum_buffer_minutes: float = Field(default=90.0, gt=0.0, le=600.0)

    #: §28 layered locking: how many leading slots are hard-locked / semi-locked.
    locked_slots: int = Field(default=2, ge=1, le=10)
    semi_locked_slots: int = Field(default=2, ge=0, le=10)

    default_crossfade_seconds: float = Field(default=4.0, ge=0.0, le=30.0)
    #: §30 forbids silence between tracks; this is the hard ceiling checked in test.
    max_gap_milliseconds: int = Field(default=0, ge=0, le=250)

    #: §31 station ID frequency.
    station_id_every_n_tracks: int = Field(default=6, ge=1, le=100)
    station_id_repeat_horizon: int = Field(default=8, ge=1, le=200)

    #: §32 AI DJ frequency limits. Decision layer only in V1.
    dj_enabled: bool = False
    dj_min_tracks_between: int = Field(default=5, ge=1, le=100)

    #: §33 Tier 2 reserve size, in minutes of approved audio held back.
    emergency_reserve_minutes: float = Field(default=30.0, ge=0.0, le=600.0)

    @model_validator(mode="after")
    def _check(self) -> RadioSettings:
        ordered = (
            self.minimum_buffer_minutes
            < self.target_buffer_minutes
            <= self.maximum_buffer_minutes
        )
        if not ordered:
            raise ValueError(
                "radio buffers must satisfy minimum < target <= maximum, got "
                f"minimum={self.minimum_buffer_minutes} "
                f"target={self.target_buffer_minutes} "
                f"maximum={self.maximum_buffer_minutes}"
            )
        return self


# ============================================================ generation


class AceStepProfileSettings(Section):
    """One §7.8 generation preset.

    Generation parameters only. §7.20 is explicit that buffer pressure may change *how the
    model is asked*, never what the audio has to pass afterwards — so there is deliberately
    nowhere in this type to put a QC threshold. The restriction is structural rather than a
    rule someone has to remember.
    """

    inference_steps: int = Field(default=8, ge=1, le=200)
    guidance_scale: float = Field(default=3.0, ge=0.0, le=30.0)
    #: Scales the request timeout. A quality preset may legitimately take longer before it
    #: is called hung.
    timeout_multiplier: float = Field(default=1.0, gt=0.0, le=10.0)
    max_duration_seconds: float | None = Field(default=None, gt=0.0, le=600.0)
    description: str = ""


def _default_profiles() -> dict[str, AceStepProfileSettings]:
    """Starting points, to be revisited against the §7.21 benchmark.

    Step counts follow ACE-Step's documented turbo range (1-20).
    """
    return {
        "fast": AceStepProfileSettings(
            inference_steps=4,
            guidance_scale=2.0,
            timeout_multiplier=0.6,
            description="Fewest steps, for buffer pressure.",
        ),
        "balanced": AceStepProfileSettings(
            inference_steps=8,
            guidance_scale=3.0,
            timeout_multiplier=1.0,
            description="The documented turbo default. Normal operation.",
        ),
        "quality": AceStepProfileSettings(
            inference_steps=16,
            guidance_scale=4.5,
            timeout_multiplier=1.8,
            description="More steps and stronger guidance, when the buffer is healthy.",
        ),
        "vocal": AceStepProfileSettings(
            inference_steps=28,
            guidance_scale=7.5,
            timeout_multiplier=2.6,
            description="For tracks with words. Enough steps for diction to resolve.",
        ),
    }


class AceStepSettings(Section):
    """ACE-Step provider settings (ADR-02, ADR-04, §7.29).

    ``base_url`` rather than a model path, because ACE-Step runs out of process. That is not
    a preference: ACE-Step 1.5 declares ``requires-python = ">=3.11,<3.13"`` and ADR-01 pins
    this project to 3.10 for librosa's numba wheels, so the two cannot share an interpreter
    at any price. See ``docs/status/PHASE_7_ENVIRONMENT.md``.

    §7.29: every ACE-Step setting lives here, so none of them is scattered through code.
    """

    base_url: str = "http://127.0.0.1:8001"
    dit_model: str = "acestep-v15-turbo"
    lm_model: str = "acestep-5Hz-lm-0.6B"
    #: Bearer token, when the service was started with ``ACESTEP_API_KEY``.
    #:
    #: ``SecretStr`` so §50's "secrets must never display unmasked after save" holds for it
    #: the same way it does for the OBS password and the broker credentials.
    api_key: SecretStr | None = None

    inference_steps: int = Field(default=8, ge=1, le=200)
    guidance_scale: float = Field(default=3.0, ge=0.0, le=30.0)
    precision: Literal["bf16", "fp16", "fp32"] = "bf16"
    offload: bool = True
    #: Let ACE-Step's 5Hz LM expand tags and lyrics before diffusion.
    #:
    #: Off by default. The station already composed a deliberate caption and validated the
    #: lyrics against §14 and §17; letting the model rewrite them would put unvalidated words
    #: about trading into a broadcast, which is the one place this project cannot be casual.
    thinking: bool = False

    #: Which §7.8 preset to use when the buffer is healthy.
    profile: str = "balanced"

    #: The preset used when the request actually carries lyrics.
    #:
    #: Vocals need more denoising steps than instruments do, and this is measured rather
    #: than assumed. At the `balanced` default — 8 steps, guidance 3.0 — the model renders
    #: the arrangement convincingly and the words never resolve: two listening tests on real
    #: station output came back "no vocals at all" while the submission record showed the
    #: full validated lyric had been sent. The same prompt and lyric at 28 steps and
    #: guidance 7.5 produced clear, intelligible rap.
    #:
    #: It is a separate profile rather than a higher global default because the cost is real
    #: and falls only where it is needed. Measured on a 215-second track: 8 steps took 67 s
    #: (3.22x faster than real time), 28 steps took 145 s (1.48x). Charging every ambient
    #: instrumental for diction nobody is singing would halve §93 capacity for nothing.
    #:
    #: Set to the same value as `profile` to disable the distinction.
    vocal_profile: str = "vocal"

    profiles: dict[str, AceStepProfileSettings] = Field(default_factory=_default_profiles)
    #: §7.20: let buffer health step the profile down toward `fast`. Never up.
    buffer_aware_profile: bool = True

    timeout_seconds: float = Field(default=300.0, gt=0.0, le=3600.0)
    #: Cold start pulls a checkpoint into VRAM and may download it, so it gets its own,
    #: much longer budget than a generation.
    load_timeout_seconds: float = Field(default=1_800.0, gt=0.0, le=14_400.0)
    #: Refuse to start a job below this much free VRAM (§20, §7.7 pre-flight).
    #:
    #: 1800 MB, **measured** in the Phase 7 benchmark rather than estimated. ADR-04 marked
    #: the original 5200 as a placeholder to revisit on real hardware, and the measurement
    #: inverted it: with the model already resident the card has only ~2.3-2.6 GB free, and
    #: a generation adds 1077-1170 MB on top. A 5200 MB floor would have refused every job
    #: on the very hardware it was written for.
    #:
    #: The figure is the measured demand plus roughly 50% margin. It is a *pre-flight*, not
    #: a guarantee: the real OOM path (§7.7) still exists because a desktop can allocate
    #: VRAM between the check and the generation.
    min_free_vram_mb: int = Field(default=1_800, ge=0, le=100_000)
    #: Where the external service writes audio; must be readable by us.
    output_dir: Path | None = None
    #: Run the service as a child process managed by the station (§7.5).
    #:
    #: Default off: on a workstation the service is usually already running and owned by the
    #: operator, and a station that silently spawned a second copy would contend for the GPU
    #: with the first.
    worker_process: bool = False
    #: Where ACE-Step is installed, when the station is to launch it.
    worker_directory: Path | None = None
    worker_command: tuple[str, ...] = ("uv", "run", "acestep-api")
    worker_startup_timeout_seconds: float = Field(default=600.0, gt=0.0, le=7_200.0)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"generation.ace_step.base_url must be http(s), got {value!r}")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_profile(self) -> AceStepSettings:
        if self.profile not in self.profiles:
            raise ValueError(
                f"generation.ace_step.profile={self.profile!r} is not among the configured "
                f"profiles ({', '.join(sorted(self.profiles))})"
            )
        if self.vocal_profile not in self.profiles:
            raise ValueError(
                f"generation.ace_step.vocal_profile={self.vocal_profile!r} is not among the "
                f"configured profiles ({', '.join(sorted(self.profiles))})"
            )
        if self.worker_process and self.worker_directory is None:
            raise ValueError(
                "generation.ace_step.worker_process requires worker_directory, so the "
                "station knows which installation to launch"
            )
        return self


class MockProviderSettings(Section):
    """§62 mock provider. Must produce *real* audio, quickly."""

    sample_rate: int = Field(default=44_100, ge=8_000, le=192_000)
    #: Simulated generation latency, so capacity maths (§93) is exercised.
    latency_seconds: float = Field(default=0.4, ge=0.0, le=600.0)
    #: Injectable failure rate for chaos testing (§66). Zero in normal runs.
    failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Produce deliberately broken audio at this rate, to exercise QC (§24).
    defect_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class GenerationSettings(Section):
    provider: Literal["mock", "ace_step"] = "mock"
    ace_step: AceStepSettings = Field(default_factory=AceStepSettings)
    mock: MockProviderSettings = Field(default_factory=MockProviderSettings)

    max_concurrent_jobs: int = Field(default=1, ge=1, le=16)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=5.0, ge=0.0, le=600.0)
    retry_backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    #: §70: a lease older than this is reclaimable, so a crashed worker cannot
    #: lock a job permanently.
    job_lease_seconds: float = Field(default=600.0, gt=0.0, le=7_200.0)

    #: §93 capacity tracking window, in completed jobs.
    capacity_window_jobs: int = Field(default=20, ge=3, le=1_000)
    #: §94/§95: buffer fill ratio above which experimentation is permitted.
    experimental_fill_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    critical_fill_ratio: float = Field(default=0.3, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> GenerationSettings:
        if self.critical_fill_ratio >= self.experimental_fill_ratio:
            raise ValueError(
                "generation.critical_fill_ratio must be below experimental_fill_ratio "
                f"({self.critical_fill_ratio} >= {self.experimental_fill_ratio})"
            )
        if self.max_concurrent_jobs > 1 and self.provider == "ace_step":
            raise ValueError(
                "generation.max_concurrent_jobs > 1 with the ace_step provider will "
                "contend for VRAM on a single GPU; run multiple workers instead (§89)"
            )
        return self


# ============================================================ audio


class MarketSymbolSettings(Section):
    """Per-symbol availability thresholds.

    Separate from `MarketSettings` because gold and Bitcoin have genuinely different
    tolerances: a 90-second gap in gold during the London session is a fault, while a
    90-second gap in a thin crypto pair at 4 a.m. is a quiet market.
    """

    #: Data older than this is STALE — late, not closed. The station keeps using it.
    stale_after_seconds: float = Field(default=90.0, gt=0.0, le=3_600.0)
    #: Data older than this is UNAVAILABLE: the feed is presumed broken.
    #:
    #: Note what this is *not*: it is never promoted to CLOSED. A market that the calendar
    #: says is trading cannot be declared shut because our data stopped — that inference is
    #: exactly the bug the routing subsystem is built to avoid.
    unavailable_after_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)

    #: Broker spellings to try when discovering this symbol, in order.
    #:
    #: Empty means "no routed aliases configured". For the symbol named by
    #: ``market.symbol`` the feed factory then falls back to ``market.symbol_aliases``,
    #: which is where a single-market deployment has always put them; for any other routed
    #: symbol it tries the symbol itself and nothing else, rather than offering a broker
    #: gold's spellings while asking for Bitcoin.
    aliases: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> MarketSymbolSettings:
        if self.unavailable_after_seconds <= self.stale_after_seconds:
            raise ValueError(
                "unavailable_after_seconds must exceed stale_after_seconds "
                f"({self.unavailable_after_seconds} <= {self.stale_after_seconds})"
            )
        return self


class MarketRoutingSettings(Section):
    """Which market the station programmes against, and when it may change.

    V1 policy: XAUUSD whenever it is confirmed available, BTCUSD when XAUUSD is confirmed
    **closed**. A list rather than a single fallback so a third market needs configuration
    rather than code.
    """

    primary: str = "XAUUSD"
    fallback: tuple[str, ...] = ("BTCUSD",)

    #: How long a closure must persist before the station moves off a market.
    #:
    #: 120 s. Long enough to absorb the straggling ticks either side of a session boundary,
    #: short enough that a genuine Friday close does not leave the director planning against
    #: a dead market for long.
    switch_confirmation_seconds: float = Field(default=120.0, ge=0.0, le=3_600.0)
    #: How long a reopening must persist before the station moves back.
    #:
    #: 300 s, deliberately longer than the close window. The asymmetry is the anti-flap
    #: measure: leaving a closed market early costs nothing, while returning early on one
    #: premature tick means crossing back and forth, which a listener hears as the station
    #: changing its mind.
    reopen_confirmation_seconds: float = Field(default=300.0, ge=0.0, le=7_200.0)
    #: A market that just became active cannot be replaced for at least this long.
    minimum_active_market_seconds: float = Field(default=180.0, ge=0.0, le=7_200.0)

    #: Availability thresholds, per symbol. Missing symbols use the defaults.
    symbols: dict[str, MarketSymbolSettings] = Field(default_factory=dict)

    @property
    def symbols_in_order(self) -> tuple[str, ...]:
        """Every configured symbol, primary first.

        Order matters to the caller that builds the feeds: index 0 is the market the
        station prefers, and the simulator varies the others off it.
        """
        return (self.primary, *self.fallback)

    def for_symbol(self, symbol: str) -> MarketSymbolSettings:
        return self.symbols.get(symbol.upper(), MarketSymbolSettings())

    @model_validator(mode="after")
    def _check(self) -> MarketRoutingSettings:
        if not self.primary.strip():
            raise ValueError("markets.primary must be a symbol")
        if self.primary in self.fallback:
            raise ValueError(
                f"markets.primary {self.primary!r} must not also appear in markets.fallback"
            )
        if len(set(self.fallback)) != len(self.fallback):
            raise ValueError("markets.fallback contains duplicates")
        return self


class AudioSettings(Section):
    """§ADR-06 playout output."""

    # Spelled "null_sink" rather than "null" on purpose: bare `null` in YAML
    # parses as None, so a config file saying `sink: null` would silently fail
    # validation with a confusing message.
    sink: Literal["sounddevice", "null_sink", "wav_file"] = "null_sink"
    #: Substring of the output device name, or a PortAudio index as a string.
    device_name: str = "CABLE Input"
    #: Which host API to use when the name matches several — the normal Windows case.
    #:
    #: Windows enumerates every device once per host API (MME, DirectSound, WASAPI,
    #: WDM-KS), so `device_name` alone is ambiguous on essentially every Windows machine,
    #: including for the shipped "CABLE Input" value. Left unset the sink picks by a
    #: documented preference order (WASAPI first) and logs which one it opened; set this to
    #: pin it, e.g. "WASAPI" or "MME". Run `tradefix audio devices` to see the options.
    device_host_api: str | None = None
    sample_rate: int = Field(default=44_100, ge=8_000, le=192_000)
    channels: int = Field(default=2, ge=1, le=2)
    #: Mixer block size in frames. Smaller = lower latency, more CPU wakeups.
    block_frames: int = Field(default=1_024, ge=64, le=16_384)
    #: Frames buffered ahead of the device. Too small underruns; too large makes
    #: operator skip feel laggy.
    buffer_blocks: int = Field(default=8, ge=2, le=256)


class MasteringSettings(Section):
    """§25 pipeline targets."""

    enabled: bool = True
    target_lufs: float = Field(default=-14.0, ge=-30.0, le=-5.0)
    true_peak_ceiling_dbtp: float = Field(default=-1.0, ge=-6.0, le=0.0)
    #: §25 warns against destroying dynamics; mastering fails if the loudness
    #: range drops below this fraction of the original.
    min_dynamics_retained: float = Field(default=0.55, gt=0.0, le=1.0)
    trim_silence: bool = True
    silence_threshold_dbfs: float = Field(default=-60.0, ge=-120.0, le=-20.0)
    max_trim_seconds: float = Field(default=6.0, ge=0.0, le=60.0)
    apply_compression: bool = False
    apply_eq: bool = False
    output_format: Literal["flac", "wav"] = "flac"


class QualityControlSettings(Section):
    """§24 rejection thresholds."""

    min_duration_seconds: float = Field(default=45.0, gt=0.0)
    max_silence_ratio: float = Field(default=0.35, gt=0.0, le=1.0)
    max_clipped_sample_ratio: float = Field(default=0.002, ge=0.0, le=1.0)
    max_dc_offset: float = Field(default=0.02, gt=0.0, le=1.0)
    min_loudness_lufs: float = Field(default=-40.0, ge=-80.0, le=-5.0)
    max_loudness_lufs: float = Field(default=-3.0, ge=-30.0, le=0.0)
    #: Duration may differ from the request by at most this fraction.
    max_duration_deviation: float = Field(default=0.25, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> QualityControlSettings:
        if self.min_loudness_lufs >= self.max_loudness_lufs:
            raise ValueError(
                "qc.min_loudness_lufs must be below max_loudness_lufs "
                f"({self.min_loudness_lufs} >= {self.max_loudness_lufs})"
            )
        return self


class OriginalityWeights(Section):
    """§22 novelty score components. Redistributed when an input is missing."""

    audio_fingerprint: float = Field(default=0.22, ge=0.0, le=1.0)
    embedding: float = Field(default=0.26, ge=0.0, le=1.0)
    chroma: float = Field(default=0.14, ge=0.0, le=1.0)
    mfcc: float = Field(default=0.14, ge=0.0, le=1.0)
    tempo: float = Field(default=0.06, ge=0.0, le=1.0)
    lyrics: float = Field(default=0.10, ge=0.0, le=1.0)
    blueprint: float = Field(default=0.08, ge=0.0, le=1.0)

    def as_dict(self) -> dict[str, float]:
        return {
            "audio_fingerprint": self.audio_fingerprint,
            "embedding": self.embedding,
            "chroma": self.chroma,
            "mfcc": self.mfcc,
            "tempo": self.tempo,
            "lyrics": self.lyrics,
            "blueprint": self.blueprint,
        }

    @model_validator(mode="after")
    def _non_zero(self) -> OriginalityWeights:
        if sum(self.as_dict().values()) <= 0:
            raise ValueError("originality.weights must not all be zero")
        return self


class OriginalitySettings(Section):
    """§21–§23 duplication prevention.

    Named for what it does — prevent the station repeating itself — not for a
    guarantee it cannot make (§21, §86).
    """

    weights: OriginalityWeights = Field(default_factory=OriginalityWeights)
    #: Above this combined similarity a candidate is REJECTED (§22).
    reject_similarity: float = Field(default=0.84, gt=0.0, le=1.0)
    #: Between review and reject the verdict is REVIEW.
    review_similarity: float = Field(default=0.72, gt=0.0, le=1.0)
    #: §22: in autonomous mode REVIEW regenerates by default.
    regenerate_on_review: bool = True
    #: How many library tracks to compare against. 0 = all.
    compare_limit: int = Field(default=0, ge=0)
    #: Exact-hash duplicates are always rejected regardless of weights.
    reject_exact_hash: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> OriginalitySettings:
        if self.review_similarity >= self.reject_similarity:
            raise ValueError(
                "originality.review_similarity must be below reject_similarity "
                f"({self.review_similarity} >= {self.reject_similarity})"
            )
        return self


class RetentionSettings(Section):
    """§36 disk management. See ADR-07 for why this is a Phase 1 concern."""

    enabled: bool = True
    #: Delete audio bytes for played tracks older than this. Metadata, lyrics
    #: and fingerprints are retained permanently regardless.
    audio_retention_days: int = Field(default=14, ge=1, le=3_650)
    #: Never sweep below this free-space figure; sweep harder above it.
    min_free_gb: float = Field(default=20.0, ge=1.0, le=10_000.0)
    #: §57 alert threshold.
    alert_free_gb: float = Field(default=35.0, ge=1.0, le=10_000.0)
    sweep_interval_seconds: float = Field(default=3_600.0, gt=0.0)
    #: Keep this many played masters regardless of age, as a listening archive.
    keep_recent_masters: int = Field(default=200, ge=0, le=100_000)

    @model_validator(mode="after")
    def _ordered(self) -> RetentionSettings:
        if self.alert_free_gb < self.min_free_gb:
            raise ValueError(
                "retention.alert_free_gb must be at or above min_free_gb so the "
                f"alert fires before emergency sweeping ({self.alert_free_gb} < "
                f"{self.min_free_gb})"
            )
        return self


# ============================================================ integrations


class ObsSettings(Section):
    """§51 OBS WebSocket 5.x."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=4455, ge=1, le=65_535)
    password: SecretStr = SecretStr("")
    reconnect_seconds: float = Field(default=10.0, gt=0.0, le=600.0)
    #: §51: keep source names configurable.
    sources: dict[str, str] = Field(
        default_factory=lambda: {
            "title": "TF_NOW_PLAYING_TITLE",
            "artist": "TF_NOW_PLAYING_ARTIST",
            "genre": "TF_NOW_PLAYING_GENRE",
            "bpm": "TF_NOW_PLAYING_BPM",
            "regime": "TF_MARKET_REGIME",
            "energy": "TF_MARKET_ENERGY",
            "artwork": "TF_TRACK_ART",
        }
    )
    scene: str = ""


class ApiSettings(Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8_080, ge=1, le=65_535)
    #: §68: control endpoints require this token. Empty is tolerated only on
    #: loopback in non-production, enforced in AppSettings.
    control_token: SecretStr = SecretStr("")
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    websocket_snapshot_interval_seconds: float = Field(default=1.0, gt=0.0, le=60.0)
    #: Drop a WebSocket client that falls this far behind rather than buffering
    #: without limit — a 24/7 process cannot grow a queue forever.
    websocket_max_queued_messages: int = Field(default=64, ge=4, le=10_000)


class MonitoringSettings(Section):
    """§35, §56, §57."""

    watchdog_interval_seconds: float = Field(default=10.0, gt=0.0, le=600.0)
    restart_backoff_seconds: float = Field(default=5.0, gt=0.0, le=600.0)
    restart_backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    max_restart_backoff_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)
    #: §35 forbids infinite crash loops: give up and alert after this many.
    max_consecutive_restarts: int = Field(default=8, ge=1, le=1_000)
    metrics_interval_seconds: float = Field(default=15.0, gt=0.0, le=600.0)
    #: §20 GPU thermal warning.
    gpu_temperature_warning_c: float = Field(default=82.0, gt=0.0, le=120.0)

    @model_validator(mode="after")
    def _ordered(self) -> MonitoringSettings:
        if self.max_restart_backoff_seconds < self.restart_backoff_seconds:
            raise ValueError(
                "monitoring.max_restart_backoff_seconds must be at or above "
                "restart_backoff_seconds"
            )
        return self


# ============================================================ root


class AppSettings(Section):
    """The complete validated configuration.

    Constructed by :func:`tradefix_radio.config.loader.load_settings`, never
    directly in application code — the loader is what applies YAML, ``.env`` and
    environment layering in the right order.
    """

    mode: RunMode = RunMode.DEVELOPMENT

    paths: PathSettings = Field(default_factory=PathSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    market: MarketSettings = Field(default_factory=MarketSettings)
    #: True when an interactive test run has lowered the buffer targets.
    #:
    #: Set only by `tradefix station start --test-mode`, never by a configuration file —
    #: it describes *this process*, not a deployment. It exists so the Control Center can
    #: say so: a dashboard showing a 12-minute buffer target where production uses 45 is
    #: showing a number that would be alarming if it were real, and an operator has no way
    #: to tell the difference from the figure alone.
    #:
    #: It gates nothing. QC thresholds, originality thresholds, mastering requirements,
    #: lock rules and retry policy are not reachable from here, which is deliberate: test
    #: mode may reduce waiting, never the gates that decide what is fit to broadcast.
    test_mode: bool = False

    #: Which symbol the station programmes against, and when it may change.
    markets: MarketRoutingSettings = Field(default_factory=MarketRoutingSettings)
    energy: EnergySettings = Field(default_factory=EnergySettings)
    regime: RegimeSettings = Field(default_factory=RegimeSettings)

    music: MusicSettings = Field(default_factory=MusicSettings)
    diversity: DiversitySettings = Field(default_factory=DiversitySettings)
    lyrics: LyricsSettings = Field(default_factory=LyricsSettings)

    radio: RadioSettings = Field(default_factory=RadioSettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    mastering: MasteringSettings = Field(default_factory=MasteringSettings)
    qc: QualityControlSettings = Field(default_factory=QualityControlSettings)
    originality: OriginalitySettings = Field(default_factory=OriginalitySettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

    obs: ObsSettings = Field(default_factory=ObsSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)

    @model_validator(mode="before")
    @classmethod
    def _resolve_sqlite_path(cls, data: Any) -> Any:
        """Anchor a relative SQLite file to ``paths.data_dir``.

        Without this, ``sqlite+aiosqlite:///tradefix.db`` means "relative to
        whatever directory this process happened to start in". The API, worker and
        playout processes would then each open a *different* database and the
        station would appear to lose its queue. Resolving here makes the DSN
        unambiguous everywhere, including under Alembic and pytest.

        Absolute paths, in-memory databases and PostgreSQL DSNs pass through
        untouched.
        """
        if not isinstance(data, dict):
            return data
        values = dict(data)
        raw_database = values.get("database")
        # When nothing is supplied we still build a dict, so the class default URL
        # gets anchored rather than silently staying CWD-relative.
        database = dict(raw_database) if isinstance(raw_database, dict) else {}
        url = database.get("url") or DatabaseSettings.model_fields["url"].default
        if not isinstance(url, str) or not url.startswith("sqlite"):
            values["database"] = {**database, "url": url}
            return values

        prefix, marker, raw_path = url.partition(":///")
        if not marker or not raw_path or raw_path == ":memory:":
            values["database"] = {**database, "url": url}
            return values

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            paths_input = values.get("paths")
            paths = PathSettings.model_validate(
                paths_input if isinstance(paths_input, dict) else {}
            )
            candidate = paths.data_dir / candidate
        # Forward slashes: SQLAlchemy's SQLite URL parsing treats backslashes
        # inconsistently across platforms.
        database["url"] = f"{prefix}:///{candidate.as_posix()}"
        values["database"] = database
        return values

    @model_validator(mode="after")
    def _cross_section_checks(self) -> AppSettings:
        """Invariants that span sections — the ones type checking cannot catch."""
        errors: list[str] = []

        # A crossfade longer than the shortest track would overlap three tracks.
        if self.radio.default_crossfade_seconds * 2 >= self.music.min_duration_seconds:
            errors.append(
                f"radio.default_crossfade_seconds ({self.radio.default_crossfade_seconds}) "
                f"is too long for music.min_duration_seconds "
                f"({self.music.min_duration_seconds}); two crossfades would overlap"
            )

        # Locked slots must fit inside the buffer the scheduler maintains.
        locked_total = self.radio.locked_slots + self.radio.semi_locked_slots
        average_track_minutes = (
            (self.music.min_duration_seconds + self.music.max_duration_seconds) / 2 / 60
        )
        if locked_total * average_track_minutes > self.radio.target_buffer_minutes:
            errors.append(
                f"radio.locked_slots + semi_locked_slots ({locked_total}) at an average "
                f"{average_track_minutes:.1f} min per track exceeds "
                f"radio.target_buffer_minutes ({self.radio.target_buffer_minutes}); "
                "replanning would have nothing left to adjust"
            )

        # QC must accept the durations the director is allowed to request.
        if self.qc.min_duration_seconds > self.music.min_duration_seconds:
            errors.append(
                f"qc.min_duration_seconds ({self.qc.min_duration_seconds}) exceeds "
                f"music.min_duration_seconds ({self.music.min_duration_seconds}); "
                "every short track would be rejected"
            )

        # Mastering target must sit inside the QC acceptance window, or every
        # mastered track would immediately fail re-analysis.
        if not (
            self.qc.min_loudness_lufs <= self.mastering.target_lufs <= self.qc.max_loudness_lufs
        ):
            errors.append(
                f"mastering.target_lufs ({self.mastering.target_lufs}) is outside the QC "
                f"window [{self.qc.min_loudness_lufs}, {self.qc.max_loudness_lufs}]"
            )

        # §68: a reachable control API with no token is a real exposure.
        if (
            self.mode is RunMode.PRODUCTION
            and not self.api.control_token.get_secret_value()
        ):
            errors.append(
                "api.control_token is required in production mode: unauthenticated "
                "control endpoints would allow anyone on the network to stop the "
                "broadcast (§68)"
            )
        if (
            self.api.host not in {"127.0.0.1", "localhost", "::1"}
            and not self.api.control_token.get_secret_value()
        ):
            errors.append(
                f"api.control_token is required when api.host is {self.api.host!r} "
                "(non-loopback); set a long random value (§68)"
            )

        # OBS with auth enabled but no password configured fails at connect time;
        # catching it here means the operator learns at startup instead.
        if (
            self.obs.enabled
            and self.mode is RunMode.PRODUCTION
            and not self.obs.password.get_secret_value()
        ):
            errors.append(
                "obs.password is empty while obs.enabled is true in production; "
                "OBS WebSocket 5.x rejects unauthenticated clients by default"
            )

        # Production must not silently run on synthetic market data.
        if self.mode is RunMode.PRODUCTION and self.market.feed == "simulated":
            errors.append(
                "market.feed='simulated' in production mode: the station would "
                "broadcast market commentary driven by fake data (§86)"
            )
        if self.mode is RunMode.PRODUCTION and self.generation.provider == "mock":
            errors.append(
                "generation.provider='mock' in production mode: the station would "
                "broadcast synthetic test tones"
            )

        # The emergency reserve cannot exceed the buffer that feeds it.
        if self.radio.emergency_reserve_minutes > self.radio.maximum_buffer_minutes:
            errors.append(
                f"radio.emergency_reserve_minutes ({self.radio.emergency_reserve_minutes}) "
                f"exceeds radio.maximum_buffer_minutes "
                f"({self.radio.maximum_buffer_minutes})"
            )

        if errors:
            joined = "\n".join(f"  - {message}" for message in errors)
            raise ValueError(f"configuration is invalid:\n{joined}")
        return self

    # -- safe serialisation -------------------------------------------------

    def masked_dump(self) -> dict[str, Any]:
        """JSON-safe settings with every secret replaced by a mask.

        The only sanctioned path to the API and the §50 settings page. §50 states
        secrets must never display unmasked after save, so the mask is applied at
        serialisation rather than in the UI — a UI-side mask would still have sent
        the secret over the wire.
        """
        payload = self.model_dump(mode="json")
        payload["market"]["rest_api_key"] = _mask(self.market.rest_api_key)
        payload["obs"]["password"] = _mask(self.obs.password)
        payload["api"]["control_token"] = _mask(self.api.control_token)
        return payload

    @property
    def is_production(self) -> bool:
        return self.mode is RunMode.PRODUCTION

    @property
    def uses_simulated_market(self) -> bool:
        return self.market.feed == "simulated"


def _mask(secret: SecretStr) -> str:
    """Render a secret as a non-reversible indicator of presence.

    Reports *that* a value is set and how long it is, never any of its content,
    so an operator can answer the only question a settings page needs to answer
    — "is this configured?" — without the secret leaving the process (§50, §68).
    """
    value = secret.get_secret_value()
    if not value:
        return ""
    return "•" * 8 + f" (set, {len(value)} chars)"


__all__ = [
    "AceStepSettings",
    "ApiSettings",
    "AppSettings",
    "AudioSettings",
    "BpmBand",
    "DatabaseSettings",
    "DiversitySettings",
    "EnergySettings",
    "EnergyWeights",
    "GenerationSettings",
    "LoggingSettings",
    "LyricsSettings",
    "MarketSettings",
    "MasteringSettings",
    "MockProviderSettings",
    "MonitoringSettings",
    "MusicSettings",
    "ObsSettings",
    "OriginalitySettings",
    "OriginalityWeights",
    "PathSettings",
    "QualityControlSettings",
    "RadioSettings",
    "RegimeSettings",
    "RetentionSettings",
    "Section",
]
