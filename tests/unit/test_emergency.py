"""The three emergency tiers (§33, milestone 4.8).

The promise under test is **no dead air**, and the hardest part of it is the §33 clause that
Tier 3 "must NOT sound like a short loop repeating every minute". That is not an aesthetic
preference — Tier 3 is what plays during a long outage, and a listener hearing the same minute
forty times knows the station is broken in a way a listener hearing varied ambient does not.

So the Tier 3 tests measure variety numerically: distinct audio per block, no recurrence on any
short cycle, and valid audio every time. They do **not** claim the result is good music. The
module says so itself, and a test that asserted otherwise would be asserting a taste.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tradefix_radio.audio.io import write_audio
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.contracts.enums import PlayoutTier
from tradefix_radio.radio.emergency import (
    EmergencyManager,
    EmergencyTrack,
    ProceduralSource,
    reserve_from_directory,
)

#: A low rate: these tests are about selection and variety, and every second is a real render.
RATE = 8_000
BLOCK = 2.0


@pytest.fixture
def procedural() -> ProceduralSource:
    return ProceduralSource(sample_rate=RATE, channels=1, block_seconds=BLOCK, seed=3)


def reserve_track(directory: Path, name: str, *, seconds: float = 0.5) -> EmergencyTrack:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.flac"
    samples = np.full((int(RATE * seconds), 1), 0.2, dtype=np.float32)
    write_audio(path, AudioBuffer(samples, RATE))
    return EmergencyTrack(track_id=name, audio_path=path, duration_seconds=seconds)


# -- Tier 3: procedural ----------------------------------------------------


def test_a_a_procedural_block_is_valid_audio(procedural: ProceduralSource) -> None:
    block = procedural.next_block()
    assert block.sample_rate == RATE
    assert block.duration_seconds == pytest.approx(BLOCK, rel=0.05)
    assert np.isfinite(block.samples).all()
    assert block.peak() > 0.0, "Tier 3 produced silence, which is the one thing it is for"


def test_b_a_procedural_block_never_clips(procedural: ProceduralSource) -> None:
    for _ in range(12):
        assert procedural.next_block(energy=0.45).peak() <= 1.0


def test_c_consecutive_blocks_differ(procedural: ProceduralSource) -> None:
    first = procedural.next_block()
    second = procedural.next_block()
    assert not np.array_equal(first.samples, second.samples)


def test_d_blocks_do_not_recur_on_any_short_cycle(
    procedural: ProceduralSource,
) -> None:
    """§33's actual requirement, measured. Forty blocks — at the production block length
    that is half an hour of outage — and no two identical."""
    digests = [procedural.next_block().samples.tobytes() for _ in range(40)]
    assert len(set(digests)) == 40, "Tier 3 repeated itself inside forty blocks"


def test_e_the_tonal_centre_moves(procedural: ProceduralSource) -> None:
    """Forty different renders in the same key would still read as "stuck". The centre
    rotates every fourth block, so a long outage wanders rather than sitting."""
    energies = [float(np.abs(procedural.next_block().samples).mean()) for _ in range(16)]
    assert len({round(e, 6) for e in energies}) > 8


def test_f_energy_tracks_the_market_within_a_narrow_band(
    procedural: ProceduralSource,
) -> None:
    """Tier 3 is cover, not programming: it may lean with the market but must not turn
    into a loud track, which would make an outage sound like a creative decision."""
    quiet = procedural.next_block(energy=0.0).peak()
    loud = procedural.next_block(energy=1.0).peak()
    assert quiet <= 1.0 and loud <= 1.0


def test_g_reset_restarts_the_sequence(procedural: ProceduralSource) -> None:
    first = procedural.next_block().samples.copy()
    procedural.next_block()
    procedural.reset()
    assert np.array_equal(procedural.next_block().samples, first)


def test_h_blocks_rendered_is_counted(procedural: ProceduralSource) -> None:
    for _ in range(4):
        procedural.next_block()
    assert procedural.blocks_rendered == 4


# -- escalation ------------------------------------------------------------


def test_i_a_healthy_station_stays_on_tier_one(procedural: ProceduralSource) -> None:
    manager = EmergencyManager(procedural=procedural)
    assert manager.choose_tier(tier1_available=True, monotonic_now=0.0) is (
        PlayoutTier.SCHEDULED
    )
    assert not manager.is_degraded


def test_j_losing_tier_one_escalates_immediately(procedural: ProceduralSource) -> None:
    """Immediately, with no dwell: the alternative to falling back is silence."""
    manager = EmergencyManager(procedural=procedural)
    tier = manager.choose_tier(tier1_available=False, monotonic_now=0.0)
    assert tier is PlayoutTier.PROCEDURAL
    assert manager.stats.tier3_activations == 1


def test_k_a_reserve_is_preferred_over_procedural(
    tmp_path: Path, procedural: ProceduralSource
) -> None:
    """§33's ordering. Tier 2 is real, mastered music; Tier 3 is a synthesiser. Skipping
    Tier 2 would mean an outage sounded worse than it needed to."""
    manager = EmergencyManager(
        reserve=[reserve_track(tmp_path / "reserve", "r1")], procedural=procedural
    )
    tier = manager.choose_tier(tier1_available=False, monotonic_now=0.0)
    assert tier is PlayoutTier.EMERGENCY_RESERVE
    assert manager.stats.tier2_activations == 1
    assert manager.stats.tier3_activations == 0


def test_l_an_exhausted_reserve_falls_through_to_procedural(
    tmp_path: Path, procedural: ProceduralSource
) -> None:
    manager = EmergencyManager(
        reserve=[reserve_track(tmp_path / "reserve", "r1")], procedural=procedural
    )
    manager.choose_tier(tier1_available=False, monotonic_now=0.0)
    assert manager.next_reserve_track() is not None
    assert manager.next_reserve_track() is None  # drained

    tier = manager.choose_tier(tier1_available=False, monotonic_now=10.0)
    assert tier is PlayoutTier.PROCEDURAL
    assert manager.stats.reserve_exhausted == 1


def test_m_recovery_is_immediate_when_tier_one_has_something(
    procedural: ProceduralSource,
) -> None:
    """The measured fix. A 30-second recovery dwell does not delay a *status*: it keeps
    procedural noise on air while a finished track waits, and on a thin queue the dwell
    could never elapse anyway because the one ready track was consumed first and
    consumption reset the timer. Generated music wins the moment it exists.
    """
    manager = EmergencyManager(procedural=procedural)
    manager.choose_tier(tier1_available=False, monotonic_now=0.0)
    assert manager.tier is PlayoutTier.PROCEDURAL

    tier = manager.choose_tier(tier1_available=True, monotonic_now=0.5)
    assert tier is PlayoutTier.SCHEDULED
    assert manager.stats.recoveries == 1


def test_n_a_flapping_queue_is_reported_rather_than_smoothed(
    procedural: ProceduralSource,
) -> None:
    """A station alternating between real music and cover genuinely is in trouble, and
    §57 should say so rather than present a smoothed version of it."""
    manager = EmergencyManager(procedural=procedural)
    for index in range(6):
        manager.choose_tier(tier1_available=index % 2 == 0, monotonic_now=index * 1.0)
    assert manager.stats.recoveries == 2
    assert manager.stats.tier3_activations == 3


def test_o_time_in_each_tier_is_accumulated(procedural: ProceduralSource) -> None:
    """§50's panel reports outage duration, and an operator reading "3 activations" with
    no duration cannot tell a hiccup from an hour."""
    manager = EmergencyManager(procedural=procedural)
    manager.choose_tier(tier1_available=False, monotonic_now=0.0)
    manager.choose_tier(tier1_available=True, monotonic_now=120.0)
    assert manager.stats.seconds_in_tier3 == pytest.approx(120.0)


def test_p_the_escalation_reason_is_recorded(procedural: ProceduralSource) -> None:
    manager = EmergencyManager(procedural=procedural)
    manager.choose_tier(
        tier1_available=False, monotonic_now=0.0, reason="generator died"
    )
    assert "generator died" in manager.stats.by_reason


# -- Tier 2 reserve --------------------------------------------------------


def test_q_reserve_tracks_are_served_in_order(
    tmp_path: Path, procedural: ProceduralSource
) -> None:
    directory = tmp_path / "reserve"
    manager = EmergencyManager(
        reserve=[reserve_track(directory, f"r{i}") for i in range(3)],
        procedural=procedural,
    )
    served = []
    while (item := manager.next_reserve_track()) is not None:
        served.append(item[0].track_id)
    assert served == ["r0", "r1", "r2"]
    assert manager.stats.tier2_tracks_played == 3


def test_r_reserve_minutes_counts_only_files_that_exist(
    tmp_path: Path, procedural: ProceduralSource
) -> None:
    """A reserve whose size is a lie is worse than a smaller honest one: the station
    would decline to escalate to Tier 3 believing it had cover."""
    present = reserve_track(tmp_path / "reserve", "here", seconds=60.0)
    absent = EmergencyTrack(
        track_id="gone", audio_path=tmp_path / "nope.flac", duration_seconds=600.0
    )
    manager = EmergencyManager(reserve=[present, absent], procedural=procedural)
    assert manager.reserve_minutes == pytest.approx(1.0)


def test_s_loading_a_reserve_drops_missing_files(
    tmp_path: Path, procedural: ProceduralSource
) -> None:
    manager = EmergencyManager(procedural=procedural)
    manager.load_reserve(
        [
            reserve_track(tmp_path / "reserve", "here"),
            EmergencyTrack(
                track_id="gone", audio_path=tmp_path / "nope.flac", duration_seconds=1.0
            ),
        ]
    )
    assert manager.reserve_remaining == 1


def test_t_an_unreadable_reserve_track_is_skipped_not_fatal(
    tmp_path: Path, procedural: ProceduralSource
) -> None:
    """The file existed at load time and is corrupt now. §33's floor has to hold even
    when Tier 2 itself is damaged."""
    directory = tmp_path / "reserve"
    good = reserve_track(directory, "good")
    bad_path = directory / "bad.flac"
    bad_path.write_bytes(b"not audio at all")
    bad = EmergencyTrack(track_id="bad", audio_path=bad_path, duration_seconds=1.0)

    manager = EmergencyManager(reserve=[bad, good], procedural=procedural)
    item = manager.next_reserve_track()
    assert item is not None
    assert item[0].track_id == "good"


def test_u_reserve_from_a_directory_finds_audio(tmp_path: Path) -> None:
    directory = tmp_path / "reserve"
    reserve_track(directory, "b")
    reserve_track(directory, "a")
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")

    found = reserve_from_directory(directory)
    assert [t.track_id for t in found] == ["a", "b"], "order must be deterministic"
    assert all(t.duration_seconds > 0 for t in found)


def test_v_reserve_from_a_missing_directory_is_empty_not_an_error(
    tmp_path: Path,
) -> None:
    """A station with no reserve configured is a valid station — it has Tier 3."""
    assert reserve_from_directory(tmp_path / "absent") == []


# -- persistence -----------------------------------------------------------


def test_w_state_round_trips(tmp_path: Path, procedural: ProceduralSource) -> None:
    manager = EmergencyManager(
        reserve=[reserve_track(tmp_path / "reserve", f"r{i}") for i in range(3)],
        procedural=procedural,
    )
    manager.choose_tier(tier1_available=False, monotonic_now=0.0)
    manager.next_reserve_track()
    state = manager.export_state()

    revived = EmergencyManager(
        reserve=[reserve_track(tmp_path / "reserve", f"r{i}") for i in range(3)],
        procedural=procedural,
    )
    revived.restore_state(state)
    assert revived.reserve_remaining == 2, "a used reserve track came back"
    # Counters are deliberately *not* restored: they are this run's figures, and carrying
    # them over would make the §50 panel report an outage that happened yesterday.
    assert revived.stats.tier2_activations == 0


def test_x_the_tier_itself_is_deliberately_not_restored(
    tmp_path: Path, procedural: ProceduralSource
) -> None:
    """§4.9 says to classify recovered state safely rather than trust it. The tier is a
    *conclusion about right now*, and a restart has not yet looked at the queue — so it
    starts at Tier 1 and escalates again within one block if it has to. Restoring
    PROCEDURAL would keep a recovered station on emergency cover it no longer needs.
    """
    manager = EmergencyManager(procedural=procedural)
    manager.choose_tier(tier1_available=False, monotonic_now=0.0)
    assert manager.tier is PlayoutTier.PROCEDURAL

    revived = EmergencyManager(procedural=procedural)
    revived.restore_state(manager.export_state())
    assert revived.tier is PlayoutTier.SCHEDULED


def test_y_restoring_rubbish_is_tolerated(procedural: ProceduralSource) -> None:
    manager = EmergencyManager(procedural=procedural)
    manager.restore_state({"reserve_index": "not a number", "stats": 7})
    assert manager.tier is PlayoutTier.SCHEDULED
    assert manager.reserve_remaining == 0


def test_z_an_index_past_a_shrunken_reserve_starts_over_with_cover(
    tmp_path: Path, procedural: ProceduralSource
) -> None:
    """The reserve directory shrank between runs, so the saved position says nothing about
    which of *these* files have aired.

    Starting over risks a repeat; treating the reserve as spent would drop a restarted
    station to Tier 3 with real music sitting unused on disk. During a crash recovery,
    cover beats novel cover — and the remaining count must never go negative either way.
    """
    manager = EmergencyManager(
        reserve=[reserve_track(tmp_path / "reserve", "r0")], procedural=procedural
    )
    manager.restore_state({"reserve_index": 99})
    assert manager.reserve_remaining == 1
    assert manager.next_reserve_track() is not None
