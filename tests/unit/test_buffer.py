"""Predictive buffer health (§4.5).

The §4.5 requirement that needed the most care is ``time_to_buffer_failure``. A level alone
says where the station *is*; the prediction says whether it is going to be in trouble, which
is the only version of the question a music director can act on — generating a track takes
twenty seconds and lasts three minutes, so a monitor that waits until the buffer is empty is
reporting history.

The figures below are deliberately arithmetic rather than approximate. A prediction nobody can
reason about gets ignored the first time it is wrong.
"""

from __future__ import annotations

import pytest

from tradefix_radio.config.schema import AppSettings, RadioSettings
from tradefix_radio.generation.capacity import (
    COLD_START_RATIO,
    CapacitySnapshot,
    CapacityTracker,
)
from tradefix_radio.radio.buffer import (
    EMPTY_THRESHOLD_MINUTES,
    BufferLevel,
    BufferMonitor,
    BufferTrajectory,
)


@pytest.fixture
def radio(settings: AppSettings) -> RadioSettings:
    return settings.radio


@pytest.fixture
def monitor(radio: RadioSettings) -> BufferMonitor:
    return BufferMonitor(radio)


def capacity(
    ratio: float = 2.0,
    *,
    samples: int = 10,
    p50: float = 20.0,
    p95: float = 25.0,
    failures: int = 0,
    mean_audio: float = 180.0,
) -> CapacitySnapshot:
    return CapacitySnapshot(
        capacity_ratio=ratio,
        latency_p50_seconds=p50,
        latency_p95_seconds=p95,
        samples=samples,
        failures=failures,
        timeouts=0,
        retries=0,
        mean_audio_seconds=mean_audio,
    )


def assess(
    monitor: BufferMonitor,
    *,
    ready_minutes: float,
    pending_minutes: float = 0.0,
    cap: CapacitySnapshot | None = None,
    now: float = 0.0,
    generation_active: bool = True,
):
    return monitor.assess(
        ready_seconds=ready_minutes * 60.0,
        pending_seconds=pending_minutes * 60.0,
        capacity=cap if cap is not None else capacity(),
        monotonic_now=now,
        generation_active=generation_active,
    )


# -- levels ----------------------------------------------------------------


def test_a_a_full_buffer_is_healthy(monitor: BufferMonitor) -> None:
    assert assess(monitor, ready_minutes=45.0).level is BufferLevel.HEALTHY


def test_b_below_the_minimum_is_low(monitor: BufferMonitor, radio: RadioSettings) -> None:
    result = assess(monitor, ready_minutes=radio.minimum_buffer_minutes - 1.0)
    assert result.level is BufferLevel.LOW


def test_c_exactly_at_the_minimum_is_still_healthy(
    monitor: BufferMonitor, radio: RadioSettings
) -> None:
    """The minimum is a floor, not a trigger. Firing at the boundary would make a
    station sitting exactly on target alternate between states every assessment."""
    result = assess(monitor, ready_minutes=radio.minimum_buffer_minutes)
    assert result.level is BufferLevel.HEALTHY


def test_d_a_nearly_empty_buffer_is_critical(
    monitor: BufferMonitor, radio: RadioSettings
) -> None:
    """The critical line defaults to half the configured minimum rather than a fixed
    number of minutes, so a station with a 60-minute minimum does not call 5 minutes
    merely "low"."""
    critical = radio.minimum_buffer_minutes / 2.0
    assert assess(monitor, ready_minutes=critical - 0.1).level is BufferLevel.CRITICAL
    assert assess(monitor, ready_minutes=critical + 0.1).level is BufferLevel.LOW


def test_e_an_empty_buffer_is_empty(monitor: BufferMonitor) -> None:
    result = assess(monitor, ready_minutes=0.0)
    assert result.level is BufferLevel.EMPTY
    assert result.level.is_urgent


def test_f_a_sliver_of_audio_still_counts_as_empty(monitor: BufferMonitor) -> None:
    """Half a minute of audio is not a buffer; it is the tail of the current track.
    Reporting it as merely CRITICAL would delay the emergency tiers by one track."""
    result = assess(monitor, ready_minutes=EMPTY_THRESHOLD_MINUTES * 0.5)
    assert result.level is BufferLevel.EMPTY


def test_g_levels_classify_their_own_urgency() -> None:
    assert not BufferLevel.HEALTHY.is_degraded
    assert BufferLevel.LOW.is_degraded and not BufferLevel.LOW.is_urgent
    assert BufferLevel.CRITICAL.is_urgent
    assert BufferLevel.EMPTY.is_urgent


# -- derived figures -------------------------------------------------------


def test_h_fill_ratio_is_measured_against_target(
    monitor: BufferMonitor, radio: RadioSettings
) -> None:
    result = assess(monitor, ready_minutes=radio.target_buffer_minutes / 2.0)
    assert result.fill_ratio == pytest.approx(0.5)


def test_i_deficit_and_headroom_are_complementary(
    monitor: BufferMonitor, radio: RadioSettings
) -> None:
    result = assess(monitor, ready_minutes=10.0)
    assert result.deficit_minutes == pytest.approx(radio.target_buffer_minutes - 10.0)
    assert result.headroom_minutes == pytest.approx(radio.maximum_buffer_minutes - 10.0)


def test_j_an_overfull_buffer_reports_no_deficit(
    monitor: BufferMonitor, radio: RadioSettings
) -> None:
    """A negative deficit would read as "generate minus four tracks" to the scheduler."""
    result = assess(monitor, ready_minutes=radio.maximum_buffer_minutes + 20.0)
    assert result.deficit_minutes == 0.0
    assert result.headroom_minutes == 0.0


# -- trajectory ------------------------------------------------------------


def test_k_capacity_above_break_even_fills(monitor: BufferMonitor) -> None:
    result = assess(monitor, ready_minutes=30.0, pending_minutes=6.0, cap=capacity(2.0))
    assert result.trajectory is BufferTrajectory.FILLING


def test_l_capacity_below_break_even_drains(monitor: BufferMonitor) -> None:
    """Capacity below 1.0 means the station generates less audio than it plays. That is
    the §93 figure the whole adaptive system exists to notice."""
    result = assess(monitor, ready_minutes=30.0, cap=capacity(0.5))
    assert result.trajectory is BufferTrajectory.DRAINING
    assert result.capacity_ratio == pytest.approx(0.5)


def test_m_break_even_capacity_holds(monitor: BufferMonitor) -> None:
    result = assess(monitor, ready_minutes=30.0, pending_minutes=6.0, cap=capacity(1.0))
    assert result.trajectory is BufferTrajectory.HOLDING


def test_n_a_dead_band_keeps_the_trajectory_from_flapping(
    monitor: BufferMonitor,
) -> None:
    """Capacity wobbles by a few percent between tracks. Without a dead band the
    station would log a trajectory change on nearly every assessment, and §57's alerts
    become noise nobody reads."""
    for ratio in (0.96, 1.0, 1.04):
        result = assess(monitor, ready_minutes=30.0, pending_minutes=6.0, cap=capacity(ratio))
        assert result.trajectory is BufferTrajectory.HOLDING, ratio


def test_o_no_deliveries_for_a_long_time_is_stalled(monitor: BufferMonitor) -> None:
    """The failure this catches is a generator that is *up* and producing nothing —
    capacity still reads healthy from historical samples while nothing arrives."""
    monitor.record_delivery(0.0)
    result = assess(monitor, ready_minutes=30.0, now=4_000.0)
    assert result.trajectory is BufferTrajectory.STALLED


def test_p_work_in_flight_is_not_a_stall(monitor: BufferMonitor) -> None:
    """Silence from the generator only means a stall when the pipeline is also empty.
    Tracks already generating are audio on its way, and calling that a stall would
    escalate to the emergency tiers while the thing that fixes it is in progress."""
    monitor.record_delivery(0.0)
    result = assess(monitor, ready_minutes=30.0, pending_minutes=6.0, now=4_000.0)
    assert result.trajectory is not BufferTrajectory.STALLED


def test_p2_stopped_generation_reports_a_stall_with_its_reason(
    monitor: BufferMonitor,
) -> None:
    """Trajectory describes the buffer, not blame: with generation off, nothing is
    arriving. The distinction an operator needs is carried by the reason, which says the
    generator is not running rather than that it has gone quiet."""
    result = assess(monitor, ready_minutes=30.0, now=4_000.0, generation_active=False)
    assert result.trajectory is BufferTrajectory.STALLED
    assert "not running" in result.reason


def test_q_a_cold_start_is_not_treated_as_a_failing_station(
    monitor: BufferMonitor,
) -> None:
    """With no measurements there is nothing to predict from. Assuming the worst would
    make every start look like an emergency and drive the creative risk controller to
    its most conservative setting for the first few minutes of every run."""
    cold = CapacityTracker().snapshot()
    assert cold.samples == 0
    result = assess(monitor, ready_minutes=0.0, cap=cold)
    assert result.capacity_ratio == pytest.approx(COLD_START_RATIO)
    assert result.trajectory is not BufferTrajectory.DRAINING


# -- prediction ------------------------------------------------------------


def test_r_a_filling_buffer_predicts_no_failure(monitor: BufferMonitor) -> None:
    result = assess(monitor, ready_minutes=30.0, pending_minutes=6.0, cap=capacity(2.0))
    assert result.seconds_to_failure is None
    assert not result.is_failing_soon


def test_s_a_draining_buffer_predicts_when_it_runs_out(monitor: BufferMonitor) -> None:
    """Ten minutes of audio draining at half capacity: the station consumes a second of
    audio per second and replaces half of it, so the buffer loses half a second per
    second and twenty minutes of wall time remain."""
    result = assess(monitor, ready_minutes=10.0, cap=capacity(0.5))
    assert result.seconds_to_failure == pytest.approx(20 * 60.0, rel=0.05)


def test_t_pending_work_extends_the_prediction(monitor: BufferMonitor) -> None:
    """Audio already being generated counts, otherwise the monitor panics about a gap
    that four in-flight tracks are already closing."""
    without = assess(monitor, ready_minutes=10.0, cap=capacity(0.5))
    with_pending = assess(
        monitor, ready_minutes=10.0, pending_minutes=10.0, cap=capacity(0.5)
    )
    assert without.seconds_to_failure is not None
    assert with_pending.seconds_to_failure is not None
    assert with_pending.seconds_to_failure > without.seconds_to_failure


def test_u_an_empty_stalled_buffer_predicts_immediate_failure(
    monitor: BufferMonitor,
) -> None:
    monitor.record_delivery(0.0)
    result = assess(monitor, ready_minutes=0.0, now=4_000.0)
    assert result.seconds_to_failure == pytest.approx(0.0)
    assert result.is_failing_soon


def test_v_the_assessment_explains_itself(monitor: BufferMonitor) -> None:
    """§50 and §57 both show this to a human. A trajectory with no stated reason is a
    number an operator cannot act on."""
    result = assess(monitor, ready_minutes=4.0, cap=capacity(0.4))
    assert result.reason
    assert not result.reason.endswith(".")


# -- contract shape --------------------------------------------------------


def test_w_the_assessment_converts_to_the_published_contract(
    monitor: BufferMonitor, radio: RadioSettings
) -> None:
    result = assess(monitor, ready_minutes=12.0, pending_minutes=6.0)
    contract = result.to_contract(ready_count=4, in_flight_count=2)
    assert contract.ready_count == 4
    assert contract.in_flight_count == 2
    assert contract.minutes_ready == pytest.approx(12.0)
    assert contract.minutes_in_flight == pytest.approx(6.0)
    assert contract.target_minutes == pytest.approx(radio.target_buffer_minutes)


def test_x_delivery_tracking_reports_elapsed_time(monitor: BufferMonitor) -> None:
    monitor.record_delivery(100.0)
    assert monitor.seconds_since_delivery(160.0) == pytest.approx(60.0)


def test_y_nothing_delivered_yet_is_not_time_since_delivery(
    monitor: BufferMonitor,
) -> None:
    """Zero, not elapsed-since-start. At startup nothing has been delivered *yet*,
    which is not the same as generation having stopped; measuring from process start
    would report a stall during the first minutes of every cold run, before the first
    track has had time to exist. An empty buffer is still caught — by the level.
    """
    assert monitor.seconds_since_delivery(30.0) == 0.0
    assert assess(monitor, ready_minutes=0.0, now=10_000.0).level is BufferLevel.EMPTY


def test_z_a_custom_critical_threshold_is_honoured(radio: RadioSettings) -> None:
    custom = radio.minimum_buffer_minutes * 0.75
    monitor = BufferMonitor(radio, critical_minutes=custom)
    assert assess(monitor, ready_minutes=custom - 0.1).level is BufferLevel.CRITICAL
    assert assess(monitor, ready_minutes=custom + 0.1).level is BufferLevel.LOW
