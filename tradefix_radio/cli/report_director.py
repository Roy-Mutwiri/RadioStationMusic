"""``tradefix report-director`` — the §3.12 statistical report.

Milestone 3.12's exit test: "10 000 simulated decisions across all regimes... emits
genre/BPM/key/topic/vocal distributions + diversity-over-time; asserts no collapse (no
genre > configured share; entropy above floor)."

Why a statistical report rather than more unit tests: §11's failure mode is not a wrong
answer on one track, it is a *distribution* that narrows over hundreds of tracks. No
individual decision looks wrong when a station collapses into four genres — each one is
locally plausible. Only the aggregate shows it.

The report therefore measures what §81-18 asks to be confirmed, and **fails loudly** when
a threshold is breached, so it works as a test as well as a document.

Each regime is run with *sustained* conditions, because §98's energy planner ramps the
station over several tracks. Sampling one decision per regime would measure the ramp
rather than the settled programming.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import (
    FeedStatus,
    MarketDirection,
    MarketRegime,
    TradingSession,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.queue import BufferHealthV1
from tradefix_radio.core.clock import UTC
from tradefix_radio.director.history import HistoryEntry, ProgrammingHistory, shannon_entropy
from tradefix_radio.director.library import load_content_library
from tradefix_radio.director.music_director import MusicDirector
from tradefix_radio.director.selection import WeightedSelector

#: Tracks kept in the rolling history. Beyond the longest §12 horizon there is nothing
#: more to learn, and an unbounded list over 10 000 decisions is itself a leak.
HISTORY_CAP = 400

#: Minimum normalised entropy for genre and topic distributions. Below this the station is
#: measurably narrowing. 0.75 over a 28-genre library leaves room for the market to shape
#: programming while ruling out collapse onto a handful.
ENTROPY_FLOOR = 0.75

#: Decisions after a regime change that are excluded from the per-regime distributions,
#: because §98's energy planner is still ramping from the previous regime's level.
RAMP_TRACKS = 8

#: This module's config directory, resolved once at import so the async command handler
#: does not have to touch the filesystem (``Path.resolve`` stats every component).
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

#: Market conditions per regime: (energy, direction). Sustained, so §98's planner settles.
REGIME_CONDITIONS: dict[MarketRegime, tuple[float, MarketDirection]] = {
    MarketRegime.QUIET: (7.0, MarketDirection.NEUTRAL),
    MarketRegime.LOW_VOLATILITY_RANGE: (22.0, MarketDirection.NEUTRAL),
    MarketRegime.NORMAL_RANGE: (46.0, MarketDirection.NEUTRAL),
    MarketRegime.COMPRESSION: (20.0, MarketDirection.NEUTRAL),
    MarketRegime.BREAKOUT_BUILDUP: (42.0, MarketDirection.NEUTRAL),
    MarketRegime.BULLISH_BREAKOUT: (86.0, MarketDirection.BULLISH),
    MarketRegime.BEARISH_BREAKOUT: (87.0, MarketDirection.BEARISH),
    MarketRegime.BULLISH_TREND: (64.0, MarketDirection.BULLISH),
    MarketRegime.BEARISH_TREND: (63.0, MarketDirection.BEARISH),
    MarketRegime.HIGH_VOLATILITY_RANGE: (74.0, MarketDirection.NEUTRAL),
    MarketRegime.EXTREME_VOLATILITY: (96.0, MarketDirection.NEUTRAL),
    MarketRegime.REVERSAL: (58.0, MarketDirection.NEUTRAL),
    MarketRegime.POST_EVENT_NORMALIZATION: (38.0, MarketDirection.NEUTRAL),
    MarketRegime.UNKNOWN: (50.0, MarketDirection.NEUTRAL),
}

SESSIONS: tuple[TradingSession, ...] = (
    TradingSession.ASIAN,
    TradingSession.LONDON,
    TradingSession.LONDON_NEW_YORK_OVERLAP,
    TradingSession.NEW_YORK,
    TradingSession.NEW_YORK_LATE,
)


@dataclass
class RegimeStats:
    """Distributions observed for one regime."""

    regime: MarketRegime
    decisions: int = 0
    genres: Counter[str] = field(default_factory=Counter)
    keys: Counter[str] = field(default_factory=Counter)
    topics: Counter[str] = field(default_factory=Counter)
    personas: Counter[str] = field(default_factory=Counter)
    vocal_styles: Counter[str] = field(default_factory=Counter)
    bpms: list[int] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)
    station_energies: list[float] = field(default_factory=list)
    instrumental: int = 0
    #: §16 mention counts, over **vocal tracks only**. An instrumental track has no
    #: lyrics, so its zero is a structural fact rather than a draw from the configured
    #: distribution. Mixing them in made the histogram read 77/18/5 against a configured
    #: 55/35/10 and look like a defect, when the vocal-only figures match.
    mentions: Counter[int] = field(default_factory=Counter)
    relaxations: Counter[str] = field(default_factory=Counter)
    forced: int = 0

    @property
    def instrumental_ratio(self) -> float:
        return self.instrumental / self.decisions if self.decisions else 0.0

    @property
    def genre_entropy(self) -> float:
        return shannon_entropy(self.genres.values())

    @property
    def top_genre_share(self) -> float:
        if not self.decisions:
            return 0.0
        return self.genres.most_common(1)[0][1] / self.decisions

    @property
    def mean_bpm(self) -> float:
        return sum(self.bpms) / len(self.bpms) if self.bpms else 0.0

    @property
    def bpm_range(self) -> tuple[int, int]:
        return (min(self.bpms), max(self.bpms)) if self.bpms else (0, 0)

    @property
    def mean_station_energy(self) -> float:
        if not self.station_energies:
            return 0.0
        return sum(self.station_energies) / len(self.station_energies)


@dataclass
class DirectorReport:
    """The whole run."""

    per_regime: dict[MarketRegime, RegimeStats] = field(default_factory=dict)
    overall: RegimeStats = field(
        default_factory=lambda: RegimeStats(regime=MarketRegime.UNKNOWN)
    )
    diversity_samples: list[tuple[int, float]] = field(default_factory=list)
    signature_collisions: int = 0
    title_collisions: int = 0
    total_decisions: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _state(
    regime: MarketRegime,
    energy: float,
    direction: MarketDirection,
    session: TradingSession,
    now: datetime,
) -> MarketStateV1:
    return MarketStateV1(
        symbol="XAUUSD",
        timestamp=now,
        regime=regime,
        direction=direction,
        session=session,
        feed_status=FeedStatus.SIMULATED,
        energy=energy,
        energy_velocity=0.0,
        volatility=energy,
        trend_strength=70.0 if direction is not MarketDirection.NEUTRAL else 12.0,
        momentum=50.0,
        compression=80.0 if regime is MarketRegime.COMPRESSION else 12.0,
        # UNKNOWN cannot carry confidence above 0.5 (contract rule).
        confidence=0.3 if regime is MarketRegime.UNKNOWN else 0.82,
        regime_age_seconds=900.0,
        data_age_seconds=1.0,
    )


def _buffer(fill: float) -> BufferHealthV1:
    return BufferHealthV1(
        minutes_ready=45.0 * fill,
        minutes_in_flight=6.0,
        minimum_minutes=20.0,
        target_minutes=45.0,
        maximum_minutes=90.0,
        ready_count=max(1, int(12 * fill)),
        in_flight_count=2,
    )


def run_report(
    settings: AppSettings,
    *,
    decisions: int,
    seed: int,
    config_dir: Path,
) -> DirectorReport:
    """Run ``decisions`` director decisions spread across every regime."""
    library = load_content_library(config_dir=config_dir)
    # Seeded for reproducibility, not for security: this is a creative simulation.
    rng = random.Random(seed)  # noqa: S311
    selector = WeightedSelector(rng)
    director = MusicDirector(settings, library, selector=selector)

    report = DirectorReport()
    regimes = list(REGIME_CONDITIONS)
    per_regime = max(1, decisions // len(regimes))

    history_entries: list[HistoryEntry] = []
    used_signatures: set[str] = set()
    used_titles: set[str] = set()
    moment = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)
    sequence = 0

    for regime in regimes:
        energy, direction = REGIME_CONDITIONS[regime]
        stats = RegimeStats(regime=regime)
        report.per_regime[regime] = stats

        for index in range(per_regime):
            sequence += 1
            session = SESSIONS[index % len(SESSIONS)]
            # Buffer fill cycles so §94/§95 stances are all exercised rather than only
            # the comfortable one.
            fill = (0.25, 0.55, 0.85, 1.0)[index % 4]
            history = ProgrammingHistory(history_entries)

            decision = director.create_blueprint(
                track_id=f"TF-20261002-{sequence:05d}",
                state=_state(regime, energy, direction, session, moment),
                history=history,
                buffer=_buffer(fill),
                now=moment,
                capacity_ratio=1.4,
                used_titles=tuple(used_titles),
                recent_titles=tuple(
                    entry.track_id for entry in history_entries[:40]
                ),
                # The FULL signature set, not the windowed history. A rolling window lets
                # a signature from beyond its edge repeat, which is what the first run of
                # this report caught.
                used_signatures=frozenset(used_signatures),
            )
            blueprint = decision.blueprint
            composition = blueprint.composition

            signature = blueprint.signature()
            if signature in used_signatures:
                report.signature_collisions += 1
            used_signatures.add(signature)

            normalised_title = blueprint.title.lower()
            if normalised_title in used_titles:
                report.title_collisions += 1
            used_titles.add(normalised_title)

            # §98's planner ramps station energy over several tracks, so the first few
            # decisions after a regime change measure the ramp rather than the settled
            # programming. They still happen — and still count toward collisions and the
            # overall figures — but are excluded from the per-regime distributions, which
            # would otherwise report a BPM range spanning two regimes.
            settled = index >= RAMP_TRACKS
            targets = (stats, report.overall) if settled else (report.overall,)
            for target in targets:
                target.decisions += 1
                target.genres[composition.genre] += 1
                target.keys[composition.key] += 1
                target.bpms.append(composition.bpm)
                target.durations.append(composition.duration_seconds)
                target.station_energies.append(composition.energy * 100.0)
                target.vocal_styles[blueprint.vocal.style.value] += 1
                if not blueprint.is_instrumental:
                    target.mentions[blueprint.lyrics.tradefix_mentions] += 1
                if blueprint.persona_id:
                    target.personas[blueprint.persona_id] += 1
                if blueprint.is_instrumental:
                    target.instrumental += 1
                if blueprint.lyrics.primary_topic:
                    target.topics[blueprint.lyrics.primary_topic] += 1
                for name in decision.relaxed_constraints:
                    target.relaxations[name] += 1
                if decision.was_forced:
                    target.forced += 1

            history_entries.insert(
                0,
                HistoryEntry(
                    track_id=blueprint.track_id,
                    genre=composition.genre,
                    secondary_genre=composition.secondary_genre,
                    bpm=composition.bpm,
                    musical_key=composition.key,
                    duration_seconds=float(composition.duration_seconds),
                    is_instrumental=blueprint.is_instrumental,
                    vocal_style=blueprint.vocal.style.value,
                    primary_topic=blueprint.lyrics.primary_topic,
                    secondary_topic=blueprint.lyrics.secondary_topic,
                    persona_id=blueprint.persona_id,
                    blueprint_signature=signature,
                    energy_at_generation=blueprint.market.energy,
                    regime_at_generation=regime.value,
                    created_at=moment,
                    played_at=moment,
                ),
            )
            del history_entries[HISTORY_CAP:]

            if sequence % 25 == 0:
                report.diversity_samples.append(
                    (sequence, director.diversity.score(ProgrammingHistory(history_entries)).score)
                )
            moment += timedelta(minutes=4)

    report.total_decisions = sequence
    _assess(report, settings)
    return report


def _assess(report: DirectorReport, settings: AppSettings) -> None:
    """Apply the §81-18 no-collapse thresholds, recording every breach."""
    overall = report.overall
    ceiling = settings.diversity.max_genre_share_medium

    if overall.genre_entropy < ENTROPY_FLOOR:
        report.failures.append(
            f"overall genre entropy {overall.genre_entropy:.3f} is below the "
            f"{ENTROPY_FLOOR} floor — programming has narrowed"
        )
    if overall.top_genre_share > ceiling:
        genre, count = overall.genres.most_common(1)[0]
        report.failures.append(
            f"genre {genre!r} is {count / overall.decisions:.1%} of all decisions, above "
            f"the {ceiling:.0%} ceiling"
        )
    topic_entropy = shannon_entropy(overall.topics.values())
    if overall.topics and topic_entropy < ENTROPY_FLOOR:
        report.failures.append(
            f"topic entropy {topic_entropy:.3f} is below the {ENTROPY_FLOOR} floor"
        )
    if report.signature_collisions:
        report.failures.append(
            f"{report.signature_collisions} repeated blueprint signatures — §11 says a "
            "blueprint must never repeat"
        )
    if not 0.12 <= overall.instrumental_ratio <= 0.62:
        report.failures.append(
            f"instrumental ratio {overall.instrumental_ratio:.0%} is outside the "
            "12-62% band; the station is drifting toward one mode"
        )
    # Every regime must produce usable variety on its own, not merely in aggregate.
    for regime, stats in report.per_regime.items():
        if stats.decisions >= 20 and len(stats.genres) < 4:
            report.failures.append(
                f"regime {regime.value} used only {len(stats.genres)} genres across "
                f"{stats.decisions} decisions"
            )


def render(
    report: DirectorReport, settings: AppSettings, *, library_genre_count: int = 0
) -> str:
    """Human-readable report."""
    lines: list[str] = []
    overall = report.overall
    lines.append("")
    lines.append(f"  DIRECTOR STATISTICAL REPORT — {report.total_decisions} decisions")
    lines.append("")
    lines.append(f"  genre entropy        {overall.genre_entropy:.3f} (floor {ENTROPY_FLOOR})")
    lines.append(
        f"  top genre share      {overall.top_genre_share:.1%} "
        f"(ceiling {settings.diversity.max_genre_share_medium:.0%})"
    )
    lines.append(f"  topic entropy        {shannon_entropy(overall.topics.values()):.3f}")
    lines.append(
        f"  distinct genres      {len(overall.genres)} / {library_genre_count}"
    )
    lines.append(f"  distinct keys        {len(overall.keys)}")
    lines.append(f"  distinct topics      {len(overall.topics)}")
    lines.append(f"  distinct personas    {len(overall.personas)}")
    lines.append(f"  instrumental ratio   {overall.instrumental_ratio:.1%}")
    lines.append(f"  BPM range            {overall.bpm_range[0]}–{overall.bpm_range[1]} "
                 f"(mean {overall.mean_bpm:.0f})")
    lines.append(f"  signature collisions {report.signature_collisions}")
    lines.append(f"  title collisions     {report.title_collisions}")
    lines.append(f"  forced selections    {overall.forced}")
    lines.append("")

    lines.append("  Trade Fix mentions (§16, vocal tracks only)")
    total_mentions = sum(overall.mentions.values()) or 1
    for count in sorted(overall.mentions):
        share = overall.mentions[count] / total_mentions
        lines.append(f"    {count} mention(s)  {share:>6.1%}  {'#' * int(share * 40)}")
    lines.append("")

    lines.append("  §11 constraint relaxations (how often a rule had to give way)")
    if overall.relaxations:
        for name, count in overall.relaxations.most_common():
            lines.append(
                f"    {name:<22}{count:>6}  {count / report.total_decisions:>6.1%}"
            )
    else:
        lines.append("    none")
    lines.append("")

    lines.append("  Per regime")
    header = (
        f"    {'regime':<28}{'n':>5}{'genres':>8}{'entropy':>9}{'top':>7}"
        f"{'bpm':>12}{'energy':>8}{'instr':>7}"
    )
    lines.append(header)
    lines.append("    " + "-" * (len(header) - 4))
    for regime, stats in report.per_regime.items():
        low, high = stats.bpm_range
        lines.append(
            f"    {regime.value:<28}{stats.decisions:>5}{len(stats.genres):>8}"
            f"{stats.genre_entropy:>9.3f}{stats.top_genre_share:>7.0%}"
            f"{f'{low}-{high}':>12}{stats.mean_station_energy:>8.1f}"
            f"{stats.instrumental_ratio:>7.0%}"
        )
    lines.append("")

    lines.append("  Top genres per regime")
    for regime, stats in report.per_regime.items():
        top = ", ".join(
            f"{genre} {count}" for genre, count in stats.genres.most_common(4)
        )
        lines.append(f"    {regime.value:<28}{top}")
    lines.append("")

    if report.diversity_samples:
        lines.append("  Diversity score over time")
        stride = max(1, len(report.diversity_samples) // 12)
        for index, score in report.diversity_samples[::stride]:
            bar = "#" * int(score / 3)
            lines.append(f"    after {index:>5} tracks  {score:>5.1f}  {bar}")
        lines.append("")

    if report.failures:
        lines.append("  FAILURES")
        for failure in report.failures:
            lines.append(f"    - {failure}")
    else:
        lines.append("  PASSED: no programming collapse detected (§81-18)")
    lines.append("")
    return "\n".join(lines)


async def command(args: argparse.Namespace, settings: AppSettings) -> int:
    """Entry point wired into the CLI parser."""
    config_dir = CONFIG_DIR
    library_genre_count = len(
        (await asyncio.to_thread(load_content_library, config_dir=config_dir)).genres
    )
    # Off the event loop: thousands of director decisions is seconds of pure CPU, and
    # blocking the loop would stall anything else sharing it.
    report = await asyncio.to_thread(
        run_report,
        settings,
        decisions=args.decisions,
        seed=args.seed,
        config_dir=config_dir,
    )

    if args.json:
        payload = {
            "total_decisions": report.total_decisions,
            "passed": report.passed,
            "failures": report.failures,
            "genre_entropy": round(report.overall.genre_entropy, 4),
            "top_genre_share": round(report.overall.top_genre_share, 4),
            "topic_entropy": round(shannon_entropy(report.overall.topics.values()), 4),
            "instrumental_ratio": round(report.overall.instrumental_ratio, 4),
            "signature_collisions": report.signature_collisions,
            "title_collisions": report.title_collisions,
            "genres": dict(report.overall.genres.most_common()),
            "topics": dict(report.overall.topics.most_common()),
            "relaxations": dict(report.overall.relaxations.most_common()),
            "diversity_over_time": report.diversity_samples,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render(report, settings, library_genre_count=library_genre_count))

    if args.out:
        text = render(report, settings, library_genre_count=library_genre_count)
        destination = Path(args.out)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_text, text, encoding="utf-8")
        print(f"  written to {destination}")

    return 0 if report.passed else 1


def register(subparsers: object) -> None:
    """Add the ``report-director`` subcommand."""
    parser = subparsers.add_parser(  # type: ignore[attr-defined]
        "report-director",
        help="run many director decisions and report the distributions (§3.12)",
    )
    parser.add_argument(
        "--decisions", type=int, default=2_800, help="total decisions to simulate"
    )
    parser.add_argument("--seed", type=int, default=2026, help="RNG seed (reproducible)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--out", default=None, help="also write the report to this path")


__all__ = [
    "ENTROPY_FLOOR",
    "HISTORY_CAP",
    "REGIME_CONDITIONS",
    "DirectorReport",
    "RegimeStats",
    "command",
    "register",
    "render",
    "run_report",
]
