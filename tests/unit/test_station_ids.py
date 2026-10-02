"""Station identity records (§4.7).

Two requirements shape these tests. §4.7 says the records are **configurable data, not
hard-coded sentences** — so the library is exercised with records a test invents, and the
shipped set is only checked for shape. And "do not put one after every song", which is why
anti-repetition is tested as hard as selection is.

Every record here points at a file that exists, because a station ID whose audio is missing is
the interesting case and is tested separately rather than by accident.
"""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import FIXED_NOW
from tradefix_radio.audio.format import PLAYOUT_CHANNELS, PLAYOUT_SAMPLE_RATE
from tradefix_radio.audio.io import write_audio
from tradefix_radio.audio.pcm import AudioBuffer
from tradefix_radio.radio.station_ids import (
    StationIdCategory,
    StationIdLibrary,
    StationIdRecord,
    default_library,
)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """One short real file, shared by every record that needs to exist."""
    path = tmp_path / "ident.flac"
    samples = np.zeros((PLAYOUT_SAMPLE_RATE // 2, PLAYOUT_CHANNELS), dtype=np.float32)
    samples[:, :] = 0.1
    write_audio(path, AudioBuffer(samples, PLAYOUT_SAMPLE_RATE))
    return path


def record(
    key: str,
    category: StationIdCategory,
    audio: Path,
    *,
    recurrence: int = 20,
) -> StationIdRecord:
    return StationIdRecord(
        key=key,
        category=category,
        audio_path=audio,
        duration_seconds=0.5,
        minimum_recurrence_tracks=recurrence,
        text=f"text for {key}",
    )


@pytest.fixture
def library(audio_file: Path) -> StationIdLibrary:
    return StationIdLibrary(
        [
            record("brand-a", StationIdCategory.BRANDING, audio_file),
            record("brand-b", StationIdCategory.BRANDING, audio_file),
            record("market-a", StationIdCategory.MARKET_TRANSITION, audio_file),
            record("session-a", StationIdCategory.SESSION_TRANSITION, audio_file),
            record("energy-a", StationIdCategory.ENERGY_CHANGE, audio_file),
            record("general-a", StationIdCategory.GENERAL, audio_file),
        ],
        repeat_horizon=3,
        rng=random.Random(11),
    )


# -- records ---------------------------------------------------------------


def test_a_a_record_rejects_an_empty_key(audio_file: Path) -> None:
    with pytest.raises(ValueError, match="key"):
        StationIdRecord(
            key="",
            category=StationIdCategory.BRANDING,
            audio_path=audio_file,
            duration_seconds=1.0,
        )


def test_b_a_record_rejects_a_non_positive_duration(audio_file: Path) -> None:
    """A zero-length identity would be counted as airtime and heard as a glitch."""
    with pytest.raises(ValueError, match="duration"):
        StationIdRecord(
            key="x",
            category=StationIdCategory.BRANDING,
            audio_path=audio_file,
            duration_seconds=0.0,
        )


# -- selection -------------------------------------------------------------


def test_c_selection_returns_a_record_of_the_requested_category(
    library: StationIdLibrary,
) -> None:
    chosen = library.select(category=StationIdCategory.MARKET_TRANSITION)
    assert chosen is not None
    assert chosen.category is StationIdCategory.MARKET_TRANSITION


def test_d_selection_counts_a_request(library: StationIdLibrary) -> None:
    library.select(category=StationIdCategory.BRANDING)
    assert library.stats.requested == 1
    assert library.stats.selected == 1


def test_e_an_unpopulated_category_falls_back_rather_than_returning_nothing(
    audio_file: Path,
) -> None:
    """A station with no energy-change idents should still be able to mark the moment.
    Returning nothing would silently drop the §4.7 cue instead."""
    library = StationIdLibrary(
        [record("brand-a", StationIdCategory.BRANDING, audio_file)],
        rng=random.Random(3),
    )
    chosen = library.select(category=StationIdCategory.ENERGY_CHANGE)
    assert chosen is not None
    assert chosen.key == "brand-a"


def test_f_an_empty_library_selects_nothing_and_says_so(
    audio_file: Path,
) -> None:
    library = StationIdLibrary([], rng=random.Random(3))
    assert library.select(category=StationIdCategory.BRANDING) is None
    assert library.stats.no_candidate == 1


def test_g_a_record_whose_file_is_missing_is_never_selected(tmp_path: Path) -> None:
    """§36: retention or a disk fault can remove the file under us. Selecting it would
    hand the playout engine a path it cannot read, once per rotation, forever."""
    library = StationIdLibrary(
        [record("gone", StationIdCategory.BRANDING, tmp_path / "absent.flac")],
        rng=random.Random(3),
    )
    assert library.select(category=StationIdCategory.BRANDING) is None


# -- anti-repetition -------------------------------------------------------


def test_h_a_just_played_record_is_suppressed(library: StationIdLibrary) -> None:
    first = library.select(category=StationIdCategory.BRANDING)
    assert first is not None
    library.note_played(first.key, now=FIXED_NOW)

    second = library.select(category=StationIdCategory.BRANDING)
    assert second is not None
    assert second.key != first.key, "the same ident came back immediately"


def test_i_recurrence_is_measured_in_tracks_aired(library: StationIdLibrary) -> None:
    """§4.7's "do not put one after every song" is about *distance*, and the natural unit
    is tracks rather than seconds: an identity every six tracks sounds deliberate whether
    the tracks are two minutes or five."""
    chosen = library.select(category=StationIdCategory.BRANDING)
    assert chosen is not None
    library.note_played(chosen.key, now=FIXED_NOW)

    # Still inside the record's own recurrence window.
    for _ in range(3):
        library.note_track_aired()
    assert library.select(category=StationIdCategory.BRANDING) is not chosen

    # Past it.
    for _ in range(40):
        library.note_track_aired()
    eventual = library.select(category=StationIdCategory.BRANDING)
    assert eventual is not None


def test_j_suppression_is_counted(library: StationIdLibrary) -> None:
    """The counter is how §50's panel explains an identity that did not play."""
    chosen = library.select(category=StationIdCategory.GENERAL)
    assert chosen is not None
    library.note_played(chosen.key, now=FIXED_NOW)
    library.select(category=StationIdCategory.GENERAL)
    assert library.stats.suppressed_by_recurrence >= 1


def test_k_every_record_eventually_airs(library: StationIdLibrary) -> None:
    """Anti-repetition must not collapse into always choosing the same two records.

    Requested across the categories rather than with no category at all, because a
    category-less request is a *generic* slot and deliberately only draws on GENERAL and
    BRANDING — pulling a "market just turned" identity into a routine gap would make the
    station announce something that did not happen.
    """
    seen: set[str] = set()
    for index in range(200):
        category = list(StationIdCategory)[index % len(StationIdCategory)]
        chosen = library.select(category=category)
        if chosen is not None:
            seen.add(chosen.key)
            library.note_played(chosen.key, now=FIXED_NOW)
        library.note_track_aired()
    assert seen == set(library.keys)


def test_k2_a_generic_request_does_not_announce_a_market_event(
    library: StationIdLibrary,
) -> None:
    """The flip side: with no category, only GENERAL and BRANDING are eligible."""
    for _ in range(40):
        chosen = library.select()
        if chosen is not None:
            assert chosen.category in {
                StationIdCategory.GENERAL,
                StationIdCategory.BRANDING,
            }, chosen.key
            library.note_played(chosen.key, now=FIXED_NOW)
        library.note_track_aired()


def test_l_a_single_record_still_airs_after_its_window(audio_file: Path) -> None:
    """The degenerate library: one record. It must not be suppressed forever, because
    then the station has no identity at all.

    It was. The recent-plays window is global, so with one record and a horizon of eight
    that record entered the window on its first play and was never evicted — the station
    lost its identity permanently, and nothing logged it. The horizon is now capped at one
    short of the library size.
    """
    library = StationIdLibrary(
        [record("only", StationIdCategory.BRANDING, audio_file, recurrence=5)],
        repeat_horizon=1,
        rng=random.Random(5),
    )
    first = library.select()
    assert first is not None
    library.note_played(first.key, now=FIXED_NOW)
    for _ in range(6):
        library.note_track_aired()
    assert library.select() is not None


# -- persistence -----------------------------------------------------------


def test_m_state_survives_a_restart(library: StationIdLibrary) -> None:
    """§96: a restart must not reset creative memory. For identities that means the one
    that just aired does not air again immediately after a watchdog restart."""
    chosen = library.select(category=StationIdCategory.BRANDING)
    assert chosen is not None
    library.note_played(chosen.key, now=FIXED_NOW)
    state = library.export_state()

    revived = StationIdLibrary(
        [library.get(key) for key in library.keys],  # type: ignore[misc]
        repeat_horizon=3,
        rng=random.Random(11),
    )
    revived.restore_state(state)
    assert revived.plays()[chosen.key].play_count == 1
    assert revived.select(category=StationIdCategory.BRANDING) is not chosen


def test_n_restoring_unknown_keys_is_tolerated(library: StationIdLibrary) -> None:
    """The configured records change between releases. A removed identity in the saved
    state must not stop the station starting."""
    library.restore_state({"plays": {"retired-ident": {"play_count": 4}}})
    assert library.select() is not None


def test_o_restoring_rubbish_is_tolerated(library: StationIdLibrary) -> None:
    """Same reasoning as the director's memory: a creative hint that will not parse is
    not a reason to refuse to broadcast."""
    library.restore_state({"plays": "not a mapping"})
    assert library.select() is not None


def test_p_play_counts_and_timestamps_are_recorded(library: StationIdLibrary) -> None:
    chosen = library.select()
    assert chosen is not None
    later = FIXED_NOW + timedelta(minutes=5)
    library.note_played(chosen.key, now=FIXED_NOW)
    library.note_played(chosen.key, now=later)
    play = library.plays()[chosen.key]
    assert play.play_count == 2
    assert play.last_played_at == later


# -- the shipped set -------------------------------------------------------


def test_q_the_default_library_is_data_not_sentences(tmp_path: Path) -> None:
    """§4.7 requires configurable records. This asserts the shipped set is a list of
    records pointing into a directory — not strings embedded in the selection logic."""
    records = default_library(tmp_path / "station_ids")
    assert len(records) >= 5
    assert all(isinstance(r, StationIdRecord) for r in records)
    assert all(str(r.audio_path).startswith(str(tmp_path)) for r in records)


def test_r_the_default_library_covers_every_category(tmp_path: Path) -> None:
    """Each §4.7 category needs at least one record, or the fallback in
    :meth:`StationIdLibrary.select` carries moments it was not meant to."""
    records = default_library(tmp_path / "station_ids")
    assert {r.category for r in records} == set(StationIdCategory)


def test_s_default_keys_are_unique(tmp_path: Path) -> None:
    records = default_library(tmp_path / "station_ids")
    keys = [r.key for r in records]
    assert len(keys) == len(set(keys))
