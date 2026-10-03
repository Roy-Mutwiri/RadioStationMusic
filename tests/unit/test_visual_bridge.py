"""The visual state bridge, and the isolation it is responsible for.

Two things are being tested here, and the second matters as much as the first:

**Derivation** — the station's live frame becomes a `VisualStateV1` correctly, with
absence preserved as absence and the band mapping as the only place a market regime is
read.

**Isolation** — the visual layer cannot write to the station. Asserted structurally by
reading the source rather than by trusting the docstrings, because "read-only" is the
kind of property that decays silently the first time someone needs one small write.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tradefix_radio.contracts.enums import MarketRegime
from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.visual.bridge import (
    BAND_STEP_INTERVAL_SECONDS,
    DEGRADE_DECAY_SECONDS,
    DEGRADE_DORMANT_SECONDS,
    DEGRADE_HOLD_SECONDS,
    _BAND_OF_REGIME,
    VisualStateBridge,
    default_station_url,
)
from tradefix_radio.visual.contracts import (
    NO_ACTIVE_MARKET,
    FeedTrust,
    IntensityBand,
    StationMode,
    TransitionState,
)

VISUAL_PACKAGE = Path(__file__).resolve().parents[2] / "tradefix_radio" / "visual"


def _frame(**overrides: Any) -> dict[str, Any]:
    """A `LiveStateV1`-shaped frame, as the station's `/ws` serialises it."""
    frame: dict[str, Any] = {
        "status": {"is_broadcasting": True, "playout_state": "playing"},
        "market": {
            "symbol": "XAUUSD",
            "regime": "normal_range",
            "regime_confidence": 0.78,
            "regime_age_seconds": 240.0,
            "direction": "neutral",
            "session": "london",
            "energy": 48.0,
            "energy_velocity": 0.6,
            "feed_status": "live",
            "data_age_seconds": 0.8,
            "is_stale": False,
            "is_simulated": False,
        },
        "routing": {"active_symbol": "XAUUSD", "has_active_market": True},
        "now_playing": {
            "bpm": 112,
            "genre": "jazzhop",
            "planned_energy": 0.52,
            "vocal_style": "none",
            "progress": 0.4,
            "remaining_seconds": 120.0,
            "is_station_id": False,
            "tier": "scheduled",
        },
        "buffer": {"level": "healthy"},
        "emergency": {"tier": "scheduled"},
    }
    for key, value in overrides.items():
        if value is None:
            frame.pop(key, None)
        elif isinstance(value, dict) and isinstance(frame.get(key), dict):
            frame[key] = {**frame[key], **value}
        else:
            frame[key] = value
    return frame


def _bridge() -> tuple[VisualStateBridge, VirtualClock]:
    clock = VirtualClock(start=datetime(2026, 10, 3, 22, 0, tzinfo=UTC))
    return VisualStateBridge(clock=clock), clock


# ============================================================ band mapping


def test_every_regime_maps_to_a_band() -> None:
    """A regime with no band would silently fall to dormant and kill reactivity."""
    missing = sorted(r.value for r in MarketRegime if r not in _BAND_OF_REGIME)
    assert not missing, f"regimes with no band: {missing}"


def test_band_mapping_is_the_only_place_a_regime_is_read() -> None:
    """The architectural seam, asserted structurally.

    If a second module imports `MarketRegime`, a new regime stops being a one-line
    change and market vocabulary has leaked into behaviour.
    """
    offenders: list[str] = []
    for path in sorted(VISUAL_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "tradefix_radio.contracts.enums"
            ):
                names = {alias.name for alias in node.names}
                if "MarketRegime" in names and path.name != "bridge.py":
                    offenders.append(path.name)
    assert not offenders, f"MarketRegime is imported outside bridge.py: {offenders}"


def test_extreme_volatility_is_the_only_peak_regime() -> None:
    peaks = [r.value for r, b in _BAND_OF_REGIME.items() if b is IntensityBand.B5_PEAK]
    assert peaks == ["extreme_volatility"]


# ============================================================ derivation


def test_derives_a_coherent_state() -> None:
    bridge, _ = _bridge()
    state = bridge.derive(_frame())
    assert state.active_symbol == "XAUUSD"
    assert state.intensity_band is IntensityBand.B2_STEADY
    assert state.market_energy == 48.0
    assert state.music_bpm == 112
    assert state.music_genre == "jazzhop"
    assert state.music_energy == 0.52
    assert state.feed_trust is FeedTrust.LIVE
    assert state.station_mode is StationMode.NORMAL
    assert state.broadcasting is True
    assert 0.0 < state.behavior_energy < 1.0


def test_absent_music_stays_absent() -> None:
    """The brief: never replace unavailable values with fake zeros."""
    bridge, _ = _bridge()
    state = bridge.derive(_frame(now_playing=None))
    assert state.music_bpm is None
    assert state.music_energy is None
    assert state.music_genre is None
    assert not state.music_present


def test_accepts_a_json_string_and_a_dict_identically() -> None:
    import json

    frame = _frame()
    first, _ = _bridge()
    second, _ = _bridge()
    from_dict = first.derive(frame)
    from_json = second.derive(json.dumps(frame))
    assert from_dict.intensity_band is from_json.intensity_band
    assert from_dict.music_bpm == from_json.music_bpm


def test_non_finite_numbers_become_absent() -> None:
    """A NaN reaching the director would propagate silently through every multiplier."""
    bridge, _ = _bridge()
    state = bridge.derive(_frame(now_playing={"bpm": None, "planned_energy": float("nan")}))
    assert state.music_energy is None


def test_unknown_enum_values_do_not_crash() -> None:
    bridge, _ = _bridge()
    state = bridge.derive(_frame(market={"regime": "a_regime_from_the_future"}))
    assert state.intensity_band is IntensityBand.B0_DORMANT


# ============================================================ honesty overrides


def test_stale_feed_forces_dormant() -> None:
    """Behaviour driven by a frozen regime performs activity that is not happening."""
    bridge, _ = _bridge()
    state = bridge.derive(
        _frame(market={"regime": "extreme_volatility", "feed_status": "stale"})
    )
    assert state.feed_trust is FeedTrust.STALE
    assert state.intensity_band is IntensityBand.B0_DORMANT
    assert state.market_energy is None
    assert state.reaction_salience == 0.0
    assert not state.market_reactive


def test_disconnected_feed_forces_dormant() -> None:
    bridge, _ = _bridge()
    state = bridge.derive(_frame(market={"feed_status": "disconnected"}))
    assert state.intensity_band is IntensityBand.B0_DORMANT


def test_simulated_feed_is_usable_for_behaviour() -> None:
    """Simulated data is real, coherent and synthetic. The badge is the renderer's job."""
    bridge, _ = _bridge()
    state = bridge.derive(_frame(market={"feed_status": "simulated"}))
    assert state.feed_trust is FeedTrust.LIVE
    assert state.market_reactive


def test_closed_session_caps_the_band() -> None:
    """Gold is not continuously traded; he is not reacting to a market that is shut."""
    bridge, clock = _bridge()
    bridge.derive(_frame(market={"regime": "extreme_volatility", "session": "closed"}))
    for _ in range(8):
        clock.advance_sync(BAND_STEP_INTERVAL_SECONDS + 1.0)
        state = bridge.derive(
            _frame(market={"regime": "extreme_volatility", "session": "closed"})
        )
    assert state.intensity_band.rank <= IntensityBand.B1_QUIET.rank


def test_no_active_market_carries_no_regime() -> None:
    bridge, _ = _bridge()
    state = bridge.derive(_frame(routing={"has_active_market": False}))
    assert state.active_symbol == NO_ACTIVE_MARKET
    assert state.market_regime is None
    assert state.intensity_band is IntensityBand.B0_DORMANT


# ============================================================ band damping


def test_band_moves_one_step_at_a_time() -> None:
    """A flapping classification must not produce a character who oscillates."""
    bridge, clock = _bridge()
    bridge.derive(_frame())  # settles at B2
    clock.advance_sync(BAND_STEP_INTERVAL_SECONDS + 1.0)
    state = bridge.derive(_frame(market={"regime": "extreme_volatility"}))
    assert state.intensity_band is IntensityBand.B3_FOCUSED, "jumped more than one step"
    clock.advance_sync(BAND_STEP_INTERVAL_SECONDS + 1.0)
    state = bridge.derive(_frame(market={"regime": "extreme_volatility"}))
    assert state.intensity_band is IntensityBand.B4_ALERT


def test_band_will_not_move_inside_the_step_interval() -> None:
    bridge, clock = _bridge()
    bridge.derive(_frame())
    clock.advance_sync(1.0)
    state = bridge.derive(_frame(market={"regime": "extreme_volatility"}))
    assert state.intensity_band is IntensityBand.B2_STEADY


def test_calming_down_is_slower_than_waking_up() -> None:
    """More realistic, and more forgiving of a noisy classification."""
    bridge, clock = _bridge()
    bridge.derive(_frame(market={"regime": "extreme_volatility"}))
    for _ in range(6):
        clock.advance_sync(BAND_STEP_INTERVAL_SECONDS + 1.0)
        bridge.derive(_frame(market={"regime": "extreme_volatility"}))
    risen = bridge.derive(_frame(market={"regime": "extreme_volatility"}))

    # Request quiet, but only wait the upward interval: the band must not fall yet.
    clock.advance_sync(BAND_STEP_INTERVAL_SECONDS + 1.0)
    held = bridge.derive(_frame(market={"regime": "quiet"}))
    assert held.intensity_band is risen.intensity_band


# ============================================================ station mode


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": {"is_broadcasting": False}}, StationMode.OFFLINE),
        ({"emergency": {"tier": "procedural"}}, StationMode.PROCEDURAL),
        ({"emergency": {"tier": "emergency_reserve"}}, StationMode.RESERVE),
        ({"buffer": {"level": "critical"}}, StationMode.BUFFER_LOW),
        ({}, StationMode.NORMAL),
    ],
)
def test_station_mode_priority(overrides: dict[str, Any], expected: StationMode) -> None:
    bridge, _ = _bridge()
    assert bridge.derive(_frame(**overrides)).station_mode is expected


def test_symbol_change_is_visible_as_switching() -> None:
    bridge, clock = _bridge()
    bridge.derive(_frame())
    clock.advance_sync(5.0)
    state = bridge.derive(
        _frame(routing={"active_symbol": "BTCUSD"}, market={"symbol": "BTCUSD"})
    )
    assert state.active_symbol == "BTCUSD"
    assert state.station_mode is StationMode.SWITCHING_MARKET
    assert state.symbol_changed_seconds_ago is not None


def test_station_id_is_a_transition_state() -> None:
    bridge, _ = _bridge()
    state = bridge.derive(_frame(now_playing={"is_station_id": True}))
    assert state.transition_state is TransitionState.STATION_ID


def test_track_ending_is_outbound() -> None:
    bridge, _ = _bridge()
    state = bridge.derive(_frame(now_playing={"remaining_seconds": 4.0}))
    assert state.transition_state is TransitionState.OUTBOUND


# ============================================================ degradation ladder


def test_a_brief_gap_changes_nothing() -> None:
    """Shorter than three frame intervals. A blip must not be visible."""
    bridge, clock = _bridge()
    live = bridge.derive(_frame())
    clock.advance_sync(DEGRADE_HOLD_SECONDS - 1.0)
    held = bridge.degraded_state()
    assert held.intensity_band is live.intensity_band
    assert held.feed_trust is FeedTrust.LIVE


def test_a_medium_gap_decays_toward_steady_and_disables_reactions() -> None:
    bridge, clock = _bridge()
    bridge.derive(_frame(market={"regime": "extreme_volatility"}))
    clock.advance_sync(DEGRADE_HOLD_SECONDS + 2.0)
    state = bridge.degraded_state()
    assert state.feed_trust is FeedTrust.DEGRADED
    assert state.reaction_salience == 0.0
    assert state.degraded_reason is not None


def test_a_long_gap_forces_dormant() -> None:
    bridge, clock = _bridge()
    bridge.derive(_frame())
    clock.advance_sync(DEGRADE_DECAY_SECONDS + 2.0)
    state = bridge.degraded_state()
    assert state.intensity_band is IntensityBand.B0_DORMANT
    assert state.feed_trust is FeedTrust.STALE
    assert state.market_energy is None


def test_a_very_long_gap_yields_neutral_idle() -> None:
    """He keeps breathing and drinking coffee. He has nothing to react to."""
    bridge, clock = _bridge()
    bridge.derive(_frame())
    clock.advance_sync(DEGRADE_DORMANT_SECONDS + 2.0)
    state = bridge.degraded_state()
    assert state.active_symbol == NO_ACTIVE_MARKET
    assert state.station_mode is StationMode.OFFLINE
    assert not state.broadcasting
    assert not state.market_reactive


def test_never_connected_yields_neutral_idle() -> None:
    bridge, _ = _bridge()
    state = bridge.degraded_state()
    assert state.degraded_reason == "never connected"
    assert not state.market_reactive


# ============================================================ salience


def test_salience_rises_with_an_energy_move() -> None:
    bridge, clock = _bridge()
    bridge.derive(_frame(market={"energy": 30.0}))
    clock.advance_sync(5.0)
    calm = bridge.derive(_frame(market={"energy": 31.0}))
    bridge2, clock2 = _bridge()
    bridge2.derive(_frame(market={"energy": 30.0}))
    clock2.advance_sync(5.0)
    moved = bridge2.derive(_frame(market={"energy": 70.0}))
    assert moved.reaction_salience > calm.reaction_salience


def test_salience_components_are_reported() -> None:
    """The debug overlay must be able to explain a reaction, not just assert one."""
    bridge, clock = _bridge()
    bridge.derive(_frame(market={"energy": 30.0}))
    clock.advance_sync(5.0)
    state = bridge.derive(_frame(market={"energy": 70.0, "energy_velocity": 6.0}))
    assert "energy_delta" in state.salience_components
    assert "velocity" in state.salience_components


# ============================================================ isolation


def test_visual_package_never_writes_to_the_station() -> None:
    """Read-only, asserted structurally rather than trusted.

    "Read-only" is exactly the kind of property that decays silently the first time
    someone needs one small write, so this reads the source instead of the docstrings.
    """
    forbidden_imports = {
        "tradefix_radio.persistence",
        "tradefix_radio.core.events",
        "tradefix_radio.radio",
        "tradefix_radio.generation",
    }
    offenders: list[str] = []
    for path in sorted(VISUAL_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            if module and any(module.startswith(bad) for bad in forbidden_imports):
                offenders.append(f"{path.name} imports {module}")
    assert not offenders, offenders


def test_station_link_sends_nothing_but_a_keepalive() -> None:
    """The only message the visual layer may ever send is a ping."""
    source = (VISUAL_PACKAGE / "bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    sends = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"send", "send_text", "send_json"}
    ]
    assert not sends, "the bridge sends data to the station"


def test_default_station_url_points_at_the_existing_socket() -> None:
    """The bridge consumes an endpoint that already exists. No station change."""
    assert default_station_url() == "ws://127.0.0.1:8080/ws"
    assert default_station_url("localhost", 9000) == "ws://localhost:9000/ws"
