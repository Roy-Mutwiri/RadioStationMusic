"""The three emergency tiers (§33, milestone 4.8).

The promise this subsystem exists to keep: **no dead air**. Everything else about it is
negotiable; that is not.

``Tier 1`` normal generated programming. The queue.
``Tier 2`` an approved reserve, held back and never touched by normal scheduling. Already
           validated, already mastered, immediately playable, and — the important part —
           **independent of the current generator**. A reserve that needed the generator would
           be useless in the one situation it exists for.
``Tier 3`` procedurally synthesised audio. Needs nothing: no files, no model, no disk. It is
           the floor beneath which the station cannot fall while the process is alive.

**Tier 3 must not sound like a loop.** §33 says so explicitly, and a sixty-second loop is the
obvious implementation. The requirement is real rather than aesthetic: Tier 3 is what plays
during a long outage, and a listener who hears the same minute forty times knows the station is
broken in a way that a listener hearing varied ambient does not. So each Tier 3 block is
rendered with a progressing seed, a rotating chord centre and drifting density — see
:class:`ProceduralSource`.

**Escalation is automatic and so is recovery.** Falling to Tier 3 and staying there would turn a
ninety-second generator hiccup into an hour of ambient. The manager returns to Tier 1 the instant
Tier 1 has something to play — immediately, with no dwell, because a dwell does not delay a
status, it keeps procedural noise on air while a finished track waits. See :meth:`choose_tier`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from tradefix_radio.audio.format import PLAYOUT_CHANNELS, PLAYOUT_SAMPLE_RATE, conform
from tradefix_radio.audio.io import read_audio, read_info
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.audio.synthesis import SynthesisSpec, render
from tradefix_radio.contracts.enums import PlayoutTier
from tradefix_radio.core.errors import AudioError

_log = structlog.get_logger(__name__)

#: Length of one procedurally generated block.
#:
#: Long enough that the synthesiser's own arrangement has room to develop, short enough that
#: returning to Tier 1 does not wait minutes for the current block to finish.
PROCEDURAL_BLOCK_SECONDS = 45.0

#: Chord roots the procedural source rotates through, as semitone offsets from A.
#:
#: Four, not one. A single tonal centre over an hour is exactly the "short loop" impression §33
#: forbids, even when every block is otherwise different.
_PROCEDURAL_ROOTS = ("A minor", "D minor", "F major", "C major")


@dataclass(frozen=True)
class EmergencyTrack:
    """One Tier 2 reserve item.

    Carries its measured duration so the reserve's size in minutes is a fact rather than an
    estimate — §33 specifies the reserve in minutes, and a wrong total would mean believing
    there is half an hour of cover when there is ten minutes.
    """

    track_id: str
    audio_path: Path
    duration_seconds: float

    @property
    def exists(self) -> bool:
        return self.audio_path.is_file()


@dataclass
class EmergencyStats:
    tier2_activations: int = 0
    tier3_activations: int = 0
    tier2_tracks_played: int = 0
    tier3_blocks_played: int = 0
    recoveries: int = 0
    reserve_exhausted: int = 0
    seconds_in_tier2: float = 0.0
    seconds_in_tier3: float = 0.0
    by_reason: dict[str, int] = field(default_factory=dict)

    def record_reason(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1


class ProceduralSource:
    """Tier 3: synthesised audio that never repeats (§33).

    Built on the same synthesiser the mock provider uses, which is deliberate — it is known to
    produce valid, measurable audio, and Tier 3 is not the place to debug a second DSP path.

    Variation comes from four things moving at different rates, so the combination does not
    recur on any short cycle: the seed advances every block, the tonal centre rotates every
    fourth, density follows a slow sine, and the section list alternates. The result is not
    good music. It is audio that a listener recognises as "the station is in a quiet mode"
    rather than as "the station is stuck".
    """

    def __init__(
        self,
        *,
        sample_rate: int = PLAYOUT_SAMPLE_RATE,
        channels: int = PLAYOUT_CHANNELS,
        block_seconds: float = PROCEDURAL_BLOCK_SECONDS,
        seed: int = 0,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._block_seconds = block_seconds
        self._seed = seed
        self._block_index = 0

    @property
    def blocks_rendered(self) -> int:
        return self._block_index

    def next_block(self, *, energy: float = 0.25) -> AudioBuffer:
        """Render the next block. Never the same as the last.

        ``energy`` lets Tier 3 still track the market a little — there is no reason for the
        emergency floor to be equally sleepy during a violent session — but it stays in a
        narrow, low band because this is cover, not programming.
        """
        index = self._block_index
        self._block_index += 1

        # Four cycles at different periods, so the combination does not recur quickly.
        key = _PROCEDURAL_ROOTS[(index // 4) % len(_PROCEDURAL_ROOTS)]
        drift = 0.5 + 0.5 * _sine(index, period=7.0)
        sections = (
            ("intro", "verse", "bridge", "outro")
            if index % 2 == 0
            else ("intro", "passage", "verse", "outro")
        )
        held = max(0.05, min(0.45, energy))

        spec = SynthesisSpec(
            duration_seconds=self._block_seconds,
            # A slow tempo band: Tier 3 should read as ambient, not as a track.
            bpm=int(64 + 18 * drift),
            key=key,
            energy=held,
            rhythm_density=0.12 + 0.28 * drift,
            bass_intensity=0.30 + 0.25 * (1.0 - drift),
            drum_intensity=0.10 + 0.25 * drift,
            # Below the synthesiser's 0.25 lead threshold half the time, so the melodic layer
            # itself comes and goes rather than being present in every block.
            melodic_complexity=0.18 + 0.30 * _sine(index, period=5.0),
            sample_rate=self._sample_rate,
            channels=self._channels,
            sections=sections,
            seed=self._seed + index * 7919,
        )
        return render(spec)

    def reset(self) -> None:
        self._block_index = 0


def _sine(index: int, *, period: float) -> float:
    """0–1 sine over ``period`` blocks. Keeps the drift smooth rather than random."""
    return 0.5 + 0.5 * math.sin(2.0 * math.pi * index / period)


class EmergencyManager:
    """Escalates and de-escalates through §33's tiers.

    Holds no audio of its own beyond the Tier 2 index: Tier 1 lives in the queue, Tier 3 is
    generated on demand. That keeps the memory cost flat however long an outage lasts.
    """

    def __init__(
        self,
        *,
        reserve: Sequence[EmergencyTrack] = (),
        procedural: ProceduralSource | None = None,
    ) -> None:
        self._reserve = list(reserve)
        self._procedural = procedural or ProceduralSource()
        self._tier = PlayoutTier.SCHEDULED
        self._tier_since_monotonic = 0.0
        self._reserve_index = 0
        self._stats = EmergencyStats()

    # -- introspection -----------------------------------------------------

    @property
    def tier(self) -> PlayoutTier:
        return self._tier

    @property
    def stats(self) -> EmergencyStats:
        return self._stats

    @property
    def reserve_minutes(self) -> float:
        """Minutes of reserve still available, counting only files that exist."""
        return (
            sum(
                track.duration_seconds
                for track in self._reserve[self._reserve_index :]
                if track.exists
            )
            / 60.0
        )

    @property
    def reserve_remaining(self) -> int:
        return max(0, len(self._reserve) - self._reserve_index)

    @property
    def is_degraded(self) -> bool:
        return self._tier is not PlayoutTier.SCHEDULED

    def load_reserve(self, tracks: Sequence[EmergencyTrack]) -> None:
        """Replace the reserve index. Missing files are dropped with a loud warning.

        Dropped rather than kept, because a reserve whose size is a lie is worse than a smaller
        honest one: the station would decline to escalate to Tier 3 believing it had cover.
        """
        usable: list[EmergencyTrack] = []
        missing: list[EmergencyTrack] = []
        for track in tracks:
            (usable if track.exists else missing).append(track)
        if missing:
            _log.error(
                "emergency.reserve_files_missing",
                count=len(missing),
                track_ids=[t.track_id for t in missing][:10],
                detail="dropped from the reserve; its reported size would otherwise be wrong",
            )
        self._reserve = usable
        self._reserve_index = 0
        _log.info(
            "emergency.reserve_loaded",
            tracks=len(usable),
            minutes=round(self.reserve_minutes, 1),
        )

    # -- escalation --------------------------------------------------------

    def choose_tier(
        self,
        *,
        tier1_available: bool,
        monotonic_now: float,
        reason: str = "",
    ) -> PlayoutTier:
        """Decide which tier should be carrying the output right now.

        **Both directions are immediate**, and the return direction took a measurement to get
        right. Escalation is obvious: the alternative to falling back is silence.

        De-escalation originally dwelled for 30 seconds, on the reasoning that a queue which
        gains and loses a single ready track would otherwise report a tier change every few
        seconds. The reasoning was sound and the behaviour was wrong, because withholding tier 1
        does not merely delay a *status*: it keeps procedural noise on air while a finished track
        waits. A gate test on a station whose generator ran slower than playback aired 52
        procedural blocks against 5 scheduled tracks, with ready tracks sitting in the queue
        through most of it — and the dwell could never elapse anyway, because the one ready track
        was consumed before 30 seconds were up and consumption reset the timer.

        Generated music is the most expensive thing the station owns and the only thing a
        listener wants. It wins the moment it exists. What flaps when the buffer is thin is then
        the *reported* tier, and that is honest: a station alternating between real music and
        procedural cover genuinely is in trouble, and §57 should say so rather than present a
        smoothed version of it.
        """
        if tier1_available:
            if self._tier is PlayoutTier.SCHEDULED:
                return self._tier
            return self._enter(PlayoutTier.SCHEDULED, monotonic_now, "tier 1 recovered")

        if self.reserve_remaining > 0:
            if self._tier is not PlayoutTier.EMERGENCY_RESERVE:
                return self._enter(
                    PlayoutTier.EMERGENCY_RESERVE,
                    monotonic_now,
                    reason or "no scheduled track is playable",
                )
            return self._tier

        if self._tier is not PlayoutTier.PROCEDURAL:
            if self._reserve and self._reserve_index >= len(self._reserve):
                self._stats.reserve_exhausted += 1
            return self._enter(
                PlayoutTier.PROCEDURAL,
                monotonic_now,
                reason or "scheduled queue and reserve are both unavailable",
            )
        return self._tier

    def _enter(self, tier: PlayoutTier, monotonic_now: float, reason: str) -> PlayoutTier:
        previous = self._tier
        if previous is tier:
            return tier

        elapsed = max(0.0, monotonic_now - self._tier_since_monotonic)
        if previous is PlayoutTier.EMERGENCY_RESERVE:
            self._stats.seconds_in_tier2 += elapsed
        elif previous is PlayoutTier.PROCEDURAL:
            self._stats.seconds_in_tier3 += elapsed

        self._tier = tier
        self._tier_since_monotonic = monotonic_now
        self._stats.record_reason(reason)

        if tier is PlayoutTier.EMERGENCY_RESERVE:
            self._stats.tier2_activations += 1
        elif tier is PlayoutTier.PROCEDURAL:
            self._stats.tier3_activations += 1
        elif tier is PlayoutTier.SCHEDULED:
            self._stats.recoveries += 1

        log = _log.warning if tier is not PlayoutTier.SCHEDULED else _log.info
        log(
            "emergency.tier_changed",
            previous=previous.value,
            tier=tier.value,
            reason=reason,
            reserve_remaining=self.reserve_remaining,
        )
        return tier

    # -- audio -------------------------------------------------------------

    def next_reserve_track(self) -> tuple[EmergencyTrack, AudioBuffer] | None:
        """The next Tier 2 item, loaded and conformed. ``None`` when exhausted.

        A file that fails to load is skipped rather than fatal: the reserve exists for the case
        where everything else has already failed, so one unreadable item must not take the
        whole tier down with it.
        """
        while self._reserve_index < len(self._reserve):
            track = self._reserve[self._reserve_index]
            self._reserve_index += 1
            try:
                audio = conform(read_audio(track.audio_path))
            except AudioError as error:
                _log.error(
                    "emergency.reserve_track_unreadable",
                    track_id=track.track_id,
                    path=str(track.audio_path),
                    error=str(error),
                )
                continue
            self._stats.tier2_tracks_played += 1
            return track, audio
        return None

    def next_procedural_block(self, *, energy: float = 0.25) -> AudioBuffer:
        """The next Tier 3 block. Always succeeds — it needs nothing but CPU."""
        self._stats.tier3_blocks_played += 1
        return self._procedural.next_block(energy=energy)

    # -- persistence (§96) -------------------------------------------------

    def export_state(self) -> dict[str, object]:
        return {
            "tier": self._tier.value,
            "reserve_index": self._reserve_index,
            "procedural_blocks": self._procedural.blocks_rendered,
        }

    def restore_state(self, state: dict[str, object]) -> None:
        """Reload after a restart.

        The **tier is deliberately not restored.** It is derived from whether Tier 1 has
        anything playable, and that is re-evaluated on the first cycle anyway — so restoring a
        persisted tier would mean starting in Tier 3 because the station was in Tier 3 when it
        died, even though the queue recovered. The reserve position *is* restored, because
        replaying reserve tracks already used is exactly the repetition §33 is avoiding.
        """
        index = state.get("reserve_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            if index is not None:
                _log.warning(
                    "emergency.reserve_index_invalid",
                    value=repr(index),
                    detail="starting the reserve from the beginning",
                )
        elif index <= len(self._reserve):
            self._reserve_index = index
        else:
            # The reserve directory shrank between runs, so the saved position says nothing
            # about which of *these* files have aired. Starting from the beginning is the
            # deliberate choice: it risks a repeat, where treating the reserve as spent would
            # drop a restarted station to Tier 3 while real music sat on disk unused. During a
            # crash recovery, having cover matters more than having novel cover.
            _log.warning(
                "emergency.reserve_index_past_end",
                saved_index=index,
                reserve_size=len(self._reserve),
                detail="reserve shrank between runs; starting from the beginning",
            )
        blocks = state.get("procedural_blocks")
        if isinstance(blocks, int) and blocks >= 0:
            # Continue the seed progression rather than restarting it, so a station that
            # restarts during an outage does not replay the same ambient it just played.
            self._procedural._block_index = blocks


def reserve_from_directory(
    directory: Path, *, extensions: Sequence[str] = (".flac", ".wav")
) -> list[EmergencyTrack]:
    """Index the Tier 2 reserve from disk.

    Durations are **measured** from the files rather than assumed. §33 specifies the reserve in
    minutes, and an assumed length would make the station's belief about its own cover wrong in
    the one situation where that belief matters.
    """
    if not directory.is_dir():
        return []
    tracks: list[EmergencyTrack] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in extensions:
            continue
        try:
            info = read_info(path)
        except AudioError as error:
            _log.error(
                "emergency.reserve_unreadable", path=str(path), error=str(error)
            )
            continue
        tracks.append(
            EmergencyTrack(
                track_id=path.stem,
                audio_path=path,
                duration_seconds=info.duration_seconds,
            )
        )
    return tracks


__all__ = [
    "PROCEDURAL_BLOCK_SECONDS",
    "EmergencyManager",
    "EmergencyStats",
    "EmergencyTrack",
    "ProceduralSource",
    "reserve_from_directory",
]
