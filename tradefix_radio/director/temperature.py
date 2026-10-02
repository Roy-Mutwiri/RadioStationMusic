"""Creative governor: job priority and creative temperature (§94, §95).

The idea these two sections share is the best in the brief:

> §94: "When queue is low: only safe proven generation. When queue is healthy: allow
> more experimental genres/styles. This means artistic risk automatically falls when
> operational risk rises."

So the station's willingness to be interesting is a *function of how safe it currently
is*. A healthy forty-minute buffer earns the right to try an odd genre, an unusual
structure, a longer track. Eight minutes of buffer does not.

Inputs, and what each contributes:

``buffer fill ratio``   the primary signal. §26's forward buffer as a fraction of target.
``capacity ratio``      §93's generation-versus-consumption rate. A full buffer that is
                        *draining* deserves less confidence than a smaller one that is
                        filling — a level-only view would miss the direction entirely.
``consecutive failures``a provider that has failed three times running is not a safe
                        place to spend an experiment, whatever the buffer says.
``diversity pressure``  §11 pushes temperature *up*. This is the one input that argues
                        for more risk, and it has to be bounded by the others: a stale
                        station with a nearly-empty queue must still play something safe.

Temperature feeds :class:`~tradefix_radio.director.selection.WeightedSelector`, where it
sharpens or flattens the weighted sample. Low temperature concentrates on the strongest
candidate; high temperature spreads across plausible ones. It is not a randomness
multiplier — it changes *how decisively* the best option wins, never the ordering.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradefix_radio.config.schema import GenerationSettings
from tradefix_radio.contracts.enums import GenerationPriority
from tradefix_radio.contracts.queue import BufferHealthV1

#: Temperature floor. Never zero: a fully deterministic director would produce the same
#: blueprint from the same state forever, which §11's "same blueprint: never repeat"
#: would then have to veto every single time.
MIN_TEMPERATURE = 0.25

#: Temperature ceiling. Above roughly this the weighted sample is close to uniform and
#: the market's influence on genre selection becomes inaudible — which would break §1.
MAX_TEMPERATURE = 1.45

#: Consecutive provider failures after which experimentation is withdrawn regardless of
#: buffer. Three is enough to establish a pattern rather than a coincidence.
FAILURE_LOCKOUT = 3

#: Capacity ratio (§93) below which generation is losing to playback. Below 1.0 the
#: buffer is shrinking no matter how full it currently looks.
CAPACITY_BREAK_EVEN = 1.0


@dataclass(frozen=True)
class CreativeStance:
    """How adventurous the next decision is allowed to be."""

    priority: GenerationPriority
    temperature: float
    #: Whether experimental genres may be considered at all (§94).
    allow_experimental: bool
    #: Whether unusual structures and instrumentation are permitted (§95).
    allow_structural_novelty: bool
    #: Fraction of the configured duration range the director may reach toward. Long
    #: tracks are a bet: they commit the station for longer and take longer to generate.
    duration_reach: float
    reason: str

    @property
    def is_conservative(self) -> bool:
        return self.priority in {GenerationPriority.CRITICAL, GenerationPriority.HIGH}


class CreativeGovernor:
    """Derives §94 priority and §95 temperature from operational health."""

    def __init__(self, settings: GenerationSettings) -> None:
        self._settings = settings

    def stance(
        self,
        *,
        buffer: BufferHealthV1,
        capacity_ratio: float = 1.0,
        consecutive_failures: int = 0,
        diversity_pressure: float = 0.0,
        provider_healthy: bool = True,
    ) -> CreativeStance:
        """Decide the stance for the next generation decision.

        Parameters
        ----------
        buffer:
            Current forward-buffer health (§26).
        capacity_ratio:
            §93's ``generation_capacity_ratio``. Above 1.0 the station generates faster
            than it broadcasts.
        consecutive_failures:
            Provider failures in a row.
        diversity_pressure:
            0–1 from §11. The only input arguing *for* more risk.
        provider_healthy:
            Result of the provider healthcheck (§18).
        """
        settings = self._settings
        fill = buffer.fill_ratio
        reasons: list[str] = []

        # --- priority (§94). Determined by the worst applicable condition, because
        # operational safety is not an average.
        if buffer.is_critical or not provider_healthy:
            priority = GenerationPriority.CRITICAL
            reasons.append(
                "buffer critical" if buffer.is_critical else "provider unhealthy"
            )
        elif buffer.is_below_minimum or consecutive_failures >= FAILURE_LOCKOUT:
            priority = GenerationPriority.HIGH
            reasons.append(
                "buffer below minimum"
                if buffer.is_below_minimum
                else f"{consecutive_failures} consecutive provider failures"
            )
        elif (
            fill >= settings.experimental_fill_ratio
            and capacity_ratio >= CAPACITY_BREAK_EVEN
            and consecutive_failures == 0
        ):
            priority = GenerationPriority.EXPERIMENTAL
            reasons.append(
                f"buffer {fill:.0%} of target and generating at {capacity_ratio:.1f}x"
            )
        else:
            priority = GenerationPriority.NORMAL
            reasons.append(f"buffer {fill:.0%} of target")

        # --- temperature (§95). Starts from buffer fill, then adjusted.
        #
        # Deliberately NOT a pure function of the priority band: the bands are coarse
        # (four values) and temperature needs to move continuously, otherwise the station
        # would make a visible creative jump every time the buffer crossed a threshold.
        temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * _ease(fill)

        if capacity_ratio < CAPACITY_BREAK_EVEN:
            # The buffer is draining. Pull temperature toward the floor in proportion to
            # how badly we are losing, regardless of the current level.
            shortfall = max(0.0, CAPACITY_BREAK_EVEN - capacity_ratio)
            temperature -= (temperature - MIN_TEMPERATURE) * min(1.0, shortfall)
            reasons.append(f"capacity {capacity_ratio:.2f}x, buffer draining")

        if consecutive_failures:
            temperature -= (temperature - MIN_TEMPERATURE) * min(
                1.0, consecutive_failures / FAILURE_LOCKOUT
            )
            reasons.append(f"{consecutive_failures} recent failures")

        if diversity_pressure > 0:
            # §11 pushes up, but the boost is scaled by the safety that *survived* every
            # other signal — not merely clipped to the headroom those signals left.
            #
            # Clipping was not enough, and the difference matters. With a plain
            # ``headroom * pressure``, maximum pressure consumed the entire remaining
            # headroom, so a station with two minutes of buffer and a draining queue
            # reached the temperature ceiling — identical to a station with a full buffer
            # generating at 1.4x. That is §94 exactly inverted: artistic risk became
            # independent of operational risk at the one moment the coupling matters.
            #
            # Multiplying by ``safety`` makes the coupling structural: pressure can only
            # spend safety that exists, so a starving station stays near the floor however
            # stale its programming has become.
            safety = (temperature - MIN_TEMPERATURE) / (MAX_TEMPERATURE - MIN_TEMPERATURE)
            headroom = MAX_TEMPERATURE - temperature
            temperature += headroom * min(1.0, diversity_pressure) * safety
            reasons.append(f"diversity pressure {diversity_pressure:.2f}")

        temperature = max(MIN_TEMPERATURE, min(MAX_TEMPERATURE, temperature))

        allow_experimental = priority.allows_experimentation and capacity_ratio >= (
            CAPACITY_BREAK_EVEN
        )
        if consecutive_failures >= FAILURE_LOCKOUT:
            allow_experimental = False

        return CreativeStance(
            priority=priority,
            temperature=temperature,
            allow_experimental=allow_experimental,
            # Structural novelty is permitted a little more readily than a wholly
            # experimental genre: an unusual section order is a smaller bet than a genre
            # the station has no track record with.
            allow_structural_novelty=(
                priority is not GenerationPriority.CRITICAL
                and fill >= settings.critical_fill_ratio
            ),
            duration_reach=_duration_reach(fill, priority),
            reason="; ".join(reasons),
        )


def _ease(fill: float) -> float:
    """Smoothstep over the buffer fill ratio, 0–1.

    Smoothstep rather than linear so temperature is flat at both ends: near-empty and
    near-full buffers should both be *stable* regions, with the responsiveness
    concentrated in the middle where the operational situation is genuinely changing.
    """
    clamped = max(0.0, min(1.0, fill))
    return clamped * clamped * (3.0 - 2.0 * clamped)


def _duration_reach(fill: float, priority: GenerationPriority) -> float:
    """How far up the configured duration range the director may reach, 0–1.

    A long track is a commitment: it takes longer to generate and holds a queue slot
    for longer. With a thin buffer, shorter tracks mean more decision points and faster
    recovery, so the reach contracts.
    """
    if priority is GenerationPriority.CRITICAL:
        return 0.15
    if priority is GenerationPriority.HIGH:
        return 0.45
    return max(0.45, min(1.0, fill))


__all__ = [
    "CAPACITY_BREAK_EVEN",
    "FAILURE_LOCKOUT",
    "MAX_TEMPERATURE",
    "MIN_TEMPERATURE",
    "CreativeGovernor",
    "CreativeStance",
]
