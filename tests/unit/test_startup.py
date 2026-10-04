"""Tests for fresh start programming (§FSP).

These tests verify:
1. Startup state machine transitions
2. Procedural session uniqueness
3. Fresh track priming
4. Startup hold vs unintended silence distinction
"""

import time
import uuid
from datetime import datetime, timedelta

import pytest

from tradefix_radio.contracts.enums import StartupMode, StartupState
from tradefix_radio.radio.emergency import ProceduralSource, EmergencyManager
from tradefix_radio.radio.startup import (
    StartupProgrammingPlanner,
    StartupProgress,
    StartupRequirements,
)


class TestStartupStateMachine:
    """Test startup state transitions."""

    def test_initial_state_is_booting(self) -> None:
        requirements = StartupRequirements(
            minimum_fresh_tracks=2,
            target_fresh_minutes=8.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements)
        assert planner.state is StartupState.BOOTING

    def test_transition_booting_to_market_acquire(self) -> None:
        requirements = StartupRequirements(
            minimum_fresh_tracks=2,
            target_fresh_minutes=8.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements)
        planner.transition_to(StartupState.MARKET_ACQUIRE)
        assert planner.state is StartupState.MARKET_ACQUIRE
        assert planner.progress.state is StartupState.MARKET_ACQUIRE

    def test_full_controlled_start_sequence(self) -> None:
        """Test the full CONTROLLED_START state machine sequence."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=2,
            target_fresh_minutes=8.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements, mode=StartupMode.CONTROLLED_START)

        # Initial state
        assert planner.state is StartupState.BOOTING
        assert planner.is_controlled_start is True
        assert planner.should_hold_audio is True

        # Transition through states
        planner.transition_to(StartupState.MARKET_ACQUIRE)
        assert planner.state is StartupState.MARKET_ACQUIRE
        assert planner.should_hold_audio is True

        # Market acquired
        planner.on_market_acquired("BTCUSD")
        assert planner.progress.active_symbol == "BTCUSD"
        assert planner.progress.market_ready is True

        planner.transition_to(StartupState.GENERATOR_WARMING)
        assert planner.state is StartupState.GENERATOR_WARMING
        assert planner.should_hold_audio is True

        # Generator ready
        planner.on_generator_ready()
        assert planner.progress.generator_ready is True
        assert planner.state is StartupState.PRIMING

        # First fresh track ready
        planner.on_fresh_track_ready("TF-20261004-00001", 240.0, "ambient", None)
        assert planner.progress.fresh_tracks_ready == 1
        assert planner.state is StartupState.PRIMING  # Not enough yet

        # Second fresh track ready - should transition to READY_TO_AIR
        planner.on_fresh_track_ready("TF-20261004-00002", 240.0, "house", "persona-1")
        assert planner.progress.fresh_tracks_ready == 2
        assert planner.state is StartupState.READY_TO_AIR
        assert planner.should_hold_audio is False
        assert planner.progress.ready_at is not None

        # Final transition to ON_AIR
        planner.transition_to(StartupState.ON_AIR)
        assert planner.state is StartupState.ON_AIR
        assert planner.should_hold_audio is False

    def test_live_recovery_never_holds_audio(self) -> None:
        """Test that LIVE_RECOVERY mode never holds audio."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=2,
            target_fresh_minutes=8.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements, mode=StartupMode.LIVE_RECOVERY)

        # Audio should never be held in live recovery mode
        assert planner.should_hold_audio is False

        planner.transition_to(StartupState.MARKET_ACQUIRE)
        assert planner.should_hold_audio is False

        planner.transition_to(StartupState.GENERATOR_WARMING)
        assert planner.should_hold_audio is False

        planner.transition_to(StartupState.PRIMING)
        assert planner.should_hold_audio is False

    def test_priming_by_minutes(self) -> None:
        """Test that priming can complete by reaching target minutes."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=3,  # High track requirement
            target_fresh_minutes=5.0,  # Lower minute requirement
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements)
        # Must transition through proper states
        planner.transition_to(StartupState.GENERATOR_WARMING)
        planner.on_generator_ready()  # Moves to PRIMING

        # One long track (6 minutes) should satisfy the minute requirement
        planner.on_fresh_track_ready("TF-001", 360.0, "ambient", None)
        assert planner.progress.fresh_minutes_ready == 6.0
        assert planner.state is StartupState.READY_TO_AIR  # Met by minutes

    def test_priming_disabled_skips_to_ready(self) -> None:
        """Test that when priming is disabled, we skip to READY_TO_AIR."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=2,
            target_fresh_minutes=8.0,
            require_unplayed=True,
            prime_enabled=False,  # Disabled
        )
        planner = StartupProgrammingPlanner(requirements)
        planner.transition_to(StartupState.GENERATOR_WARMING)

        # on_generator_ready should skip PRIMING when disabled
        planner.on_generator_ready()
        assert planner.state is StartupState.READY_TO_AIR


class TestProceduralSessionUniqueness:
    """Test that procedural sessions have unique seeds."""

    def test_different_sessions_have_different_seeds(self) -> None:
        """Two ProceduralSource instances must have different seeds."""
        source1 = ProceduralSource()
        source2 = ProceduralSource()

        assert source1.base_seed != source2.base_seed
        assert source1.session_id != source2.session_id

    def test_session_seed_is_nonzero(self) -> None:
        """Session seed must never be zero (the old default)."""
        for _ in range(10):
            source = ProceduralSource()
            assert source.base_seed != 0
            assert source.base_seed > 0

    def test_session_id_format(self) -> None:
        """Session ID should be 8 hex characters."""
        source = ProceduralSource()
        assert len(source.session_id) == 8
        # Should be valid hex
        int(source.session_id, 16)

    def test_explicit_seed_overrides(self) -> None:
        """Explicit seed should be used when provided."""
        source = ProceduralSource(seed=12345)
        assert source.base_seed == 12345

    def test_blocks_rendered_starts_at_zero(self) -> None:
        """Block counter starts at zero."""
        source = ProceduralSource()
        assert source.blocks_rendered == 0

    def test_block_counter_increments(self) -> None:
        """Block counter increments with each block."""
        source = ProceduralSource()
        source.next_block()
        assert source.blocks_rendered == 1
        source.next_block()
        assert source.blocks_rendered == 2

    def test_reset_clears_counter_keeps_seed(self) -> None:
        """Reset clears counter but keeps session seed."""
        source = ProceduralSource()
        original_seed = source.base_seed
        original_session = source.session_id

        source.next_block()
        source.next_block()
        assert source.blocks_rendered == 2

        source.reset()
        assert source.blocks_rendered == 0
        assert source.base_seed == original_seed
        assert source.session_id == original_session


class TestEmergencyManagerSession:
    """Test EmergencyManager procedural session exposure."""

    def test_manager_exposes_procedural_session_id(self) -> None:
        """EmergencyManager should expose the procedural session ID."""
        manager = EmergencyManager()
        session_id = manager.procedural_session_id
        assert len(session_id) == 8

    def test_manager_exposes_procedural_seed(self) -> None:
        """EmergencyManager should expose the procedural seed."""
        manager = EmergencyManager()
        seed = manager.procedural_seed
        assert seed > 0

    def test_two_managers_have_different_sessions(self) -> None:
        """Two EmergencyManager instances should have different sessions."""
        manager1 = EmergencyManager()
        manager2 = EmergencyManager()

        assert manager1.procedural_session_id != manager2.procedural_session_id
        assert manager1.procedural_seed != manager2.procedural_seed


class TestOpeningDiversity:
    """Test opening track diversity constraints."""

    def test_opening_constraints_avoid_recent_genres(self) -> None:
        """Opening constraints should avoid recently used genres."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=3,
            target_fresh_minutes=10.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements)

        # First track
        planner.on_fresh_track_ready("TF-001", 240.0, "house", "persona-1")
        constraints = planner.opening_constraints()
        assert "house" in constraints["avoid_genres"]
        assert "persona-1" in constraints["avoid_personas"]

        # Second track
        planner.on_fresh_track_ready("TF-002", 240.0, "ambient", None)
        constraints = planner.opening_constraints()
        assert "house" in constraints["avoid_genres"]
        assert "ambient" in constraints["avoid_genres"]

    def test_opening_track_index_increments(self) -> None:
        """Opening track index should increment with each track."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=3,
            target_fresh_minutes=10.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements)

        assert planner.opening_constraints()["opening_track_index"] == 0

        planner.on_fresh_track_ready("TF-001", 240.0, "house", None)
        assert planner.opening_constraints()["opening_track_index"] == 1

        planner.on_fresh_track_ready("TF-002", 240.0, "ambient", None)
        assert planner.opening_constraints()["opening_track_index"] == 2


class TestStartupProgress:
    """Test StartupProgress tracking."""

    def test_priming_progress_by_tracks(self) -> None:
        """Test priming progress calculation by track count."""
        progress = StartupProgress(
            fresh_tracks_ready=1,
            fresh_tracks_required=2,
            fresh_minutes_ready=2.0,
            fresh_minutes_target=8.0,
        )
        # 1/2 = 0.5 by tracks, 2/8 = 0.25 by minutes, max = 0.5
        assert progress.priming_progress_fraction == 0.5

    def test_priming_progress_by_minutes(self) -> None:
        """Test priming progress calculation by minutes."""
        progress = StartupProgress(
            fresh_tracks_ready=0,
            fresh_tracks_required=2,
            fresh_minutes_ready=6.0,
            fresh_minutes_target=8.0,
        )
        # 0/2 = 0.0 by tracks, 6/8 = 0.75 by minutes, max = 0.75
        assert progress.priming_progress_fraction == 0.75

    def test_is_priming_complete_by_tracks(self) -> None:
        """Test priming completion by track count."""
        progress = StartupProgress(
            fresh_tracks_ready=2,
            fresh_tracks_required=2,
            fresh_minutes_ready=4.0,  # Less than target
            fresh_minutes_target=8.0,
        )
        assert progress.is_priming_complete is True

    def test_is_priming_complete_by_minutes(self) -> None:
        """Test priming completion by minutes."""
        progress = StartupProgress(
            fresh_tracks_ready=1,  # Less than required
            fresh_tracks_required=2,
            fresh_minutes_ready=10.0,  # More than target
            fresh_minutes_target=8.0,
        )
        assert progress.is_priming_complete is True

    def test_time_to_ready_none_when_not_ready(self) -> None:
        """time_to_ready should be None when not ready yet."""
        progress = StartupProgress()
        assert progress.time_to_ready is None

    def test_time_to_ready_computed_when_ready(self) -> None:
        """time_to_ready should compute elapsed time when ready."""
        started = time.monotonic()
        progress = StartupProgress(started_at=started)
        time.sleep(0.05)  # Small delay
        progress.ready_at = time.monotonic()

        time_to_ready = progress.time_to_ready
        assert time_to_ready is not None
        assert time_to_ready >= 0.01  # Should be at least some positive time


class TestFreshEligibility:
    """Test fresh track eligibility checking."""

    def test_fresh_requires_zero_play_count(self) -> None:
        """Fresh tracks must have play_count=0 when require_unplayed is True."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=2,
            target_fresh_minutes=8.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements)

        # play_count=0 is fresh
        assert planner.check_fresh_eligibility(0, "BTCUSD", "BTCUSD") is True

        # play_count>0 is not fresh
        assert planner.check_fresh_eligibility(1, "BTCUSD", "BTCUSD") is False

    def test_market_context_must_match(self) -> None:
        """Track market context must match current market."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=2,
            target_fresh_minutes=8.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements)

        # Same market is eligible
        assert planner.check_fresh_eligibility(0, "BTCUSD", "BTCUSD") is True

        # Different market is not eligible
        assert planner.check_fresh_eligibility(0, "BTCUSD", "XAUUSD") is False

    def test_neutral_market_eligibility(self) -> None:
        """Neutral market (None) should be eligible."""
        requirements = StartupRequirements(
            minimum_fresh_tracks=2,
            target_fresh_minutes=8.0,
            require_unplayed=True,
            prime_enabled=True,
        )
        planner = StartupProgrammingPlanner(requirements)

        # None track market is neutral/compatible
        assert planner.check_fresh_eligibility(0, "BTCUSD", None) is True
        assert planner.check_fresh_eligibility(0, None, None) is True
