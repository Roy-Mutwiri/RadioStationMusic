"""MusicDirector — turns a MarketState into a MusicBlueprint (§8, §9).

The artistic brain. §9 lists its inputs: market state, recent track history, last N
genres / BPMs / keys / lyrical themes / voices, time and session, the current radio energy
curve, and the station diversity score. All of them are here, and the output is a complete
:class:`~tradefix_radio.contracts.music.MusicBlueprintV1`.

The structure of every decision is the same, deliberately:

1. build a :class:`~tradefix_radio.director.selection.Candidate` per option;
2. apply **named multiplicative factors** — energy fit, regime affinity, session bias,
   recency penalty — each recorded on the candidate;
3. apply §11's **hard constraints** as vetoes;
4. sample with the §95 creative temperature.

Keeping that shape uniform is what makes the director explainable: every blueprint carries
a ``rationale``, and ``explain_last()`` can reproduce the full candidate table for the §47
and §48 pages. A director that could not say *why* it chose a genre would be impossible to
tune, and §11's collapse failures are only diagnosable by looking at the weights.

Note what is absent: no ``if regime == BULLISH_BREAKOUT: genre = "trap"`` anywhere. §1
forbids that, and the weighted library is what replaces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import structlog

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import (
    GenerationPriority,
    MarketDirection,
    MarketRegime,
    VocalStyle,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.music import (
    BlueprintMarketContextV1,
    CompositionSpecV1,
    MusicBlueprintV1,
    NoveltySpecV1,
    VocalSpecV1,
)
from tradefix_radio.contracts.queue import BufferHealthV1
from tradefix_radio.director import keys as key_library
from tradefix_radio.director.diversity import DivergencePressure, DiversityDirector
from tradefix_radio.director.energy_curve import EnergyPlan, RadioEnergyPlanner
from tradefix_radio.director.history import ProgrammingHistory
from tradefix_radio.director.library import ContentLibrary, GenreDefinition, PersonaDefinition
from tradefix_radio.director.selection import (
    Candidate,
    SelectionResult,
    WeightedSelector,
    band_fit,
)
from tradefix_radio.director.temperature import CreativeGovernor, CreativeStance
from tradefix_radio.director.titles import TitleContext, TitleGenerator
from tradefix_radio.lyrics.director import LyricPlan, LyricsDirector

_log = structlog.get_logger(__name__)

#: Probability a track carries a secondary genre. §8 allows one; always having one would
#: make every track a fusion, which is its own kind of monotony.
SECONDARY_GENRE_PROBABILITY = 0.45

#: Seed space. 2**53 keeps every value exactly representable as a float, which matters
#: because seeds pass through JSON (where numbers are doubles) on the way to a provider.
SEED_SPACE = 2**53

#: Energy points a genre may sit outside its declared band before it is excluded outright.
#:
#: Roughly 2.5x the weight falloff, so the soft penalty does the work in the plausible
#: range and this only catches the genuinely wrong choices — lo-fi during a violent
#: breakout, drum & bass during a dead session.
MAX_ENERGY_DISTANCE = 26.0

#: Attempts to find an unused blueprint signature before accepting a repeat.
#:
#: Each attempt is a full re-decide, which is cheap (pure computation, no I/O) relative to
#: generating audio. Six is enough that a collision survives only when the creative space
#: genuinely has no room left, which over a 28-genre library is vanishingly rare.
MAX_SIGNATURE_ATTEMPTS = 6

#: Weight applied to a genre whose decision just collided with an existing signature.
#:
#: Small but not zero. Re-rolling the same weights was the original retry strategy and it
#: barely worked: the selection is stochastic, but in a narrow market one genre dominates the
#: weights, so six retries kept landing on the same creative decision and the director accepted
#: a repeat. A soak measured two repeated signatures across 54 tracks.
#:
#: A penalty rather than an exclusion, because §1's energy mapping outranks novelty — if the
#: market only justifies one genre, repeating it is better than airing something that does not
#: fit. 0.02 means the collided genre only wins again when nothing else is viable at all.
SIGNATURE_RETRY_PENALTY = 0.02


@dataclass
class DirectorDecision:
    """A blueprint plus the complete reasoning behind it."""

    blueprint: MusicBlueprintV1
    energy_plan: EnergyPlan
    stance: CreativeStance
    divergence: DivergencePressure
    diversity_score: float
    lyric_plan: LyricPlan
    #: Candidate tables per decision, for the §47/§48 explainability panes.
    selections: dict[str, SelectionResult[object]] = field(default_factory=dict)

    @property
    def relaxed_constraints(self) -> tuple[str, ...]:
        """§11 rules that had to give way to reach this blueprint.

        Surfaced because the honest answer to "is §11 being enforced?" is a rate, not a
        boolean. A narrow genre's 13 available BPMs cannot always satisfy "no BPM within
        +/-4 of the last four tracks", so relaxation is expected — but if it happens on
        every track, the rule is decorative and the §3.12 report should say so.
        """
        names: list[str] = []
        for result in self.selections.values():
            names.extend(result.relaxed)
        return tuple(dict.fromkeys(names))

    @property
    def was_forced(self) -> bool:
        """Whether any decision fell through to the unconstrained fallback."""
        return any(result.was_forced for result in self.selections.values())

    def explain(self) -> str:
        """Full human-readable account of how this blueprint was reached."""
        lines = [
            f"blueprint {self.blueprint.track_id} — {self.blueprint.title!r}",
            f"  market      {self.blueprint.market.regime.value} "
            f"energy={self.blueprint.market.energy:.1f} "
            f"direction={self.blueprint.market.direction.value}",
            f"  energy plan target={self.energy_plan.target_energy:.1f} "
            f"step={self.energy_plan.step:+.1f} ({self.energy_plan.reason})",
            f"  stance      {self.stance.priority.value} "
            f"temperature={self.stance.temperature:.2f} ({self.stance.reason})",
            f"  diversity   {self.diversity_score:.1f}"
            + (
                f" — divergence {self.divergence.strength:.2f} toward "
                f"{', '.join(self.divergence.dimensions)}"
                if self.divergence.is_active
                else ""
            ),
        ]
        for name, result in self.selections.items():
            lines.append(f"  {name}:")
            lines.extend(
                f"    {line}" for line in result.explain(limit=5).splitlines()
            )
        return "\n".join(lines)


class MusicDirector:
    """Produces MusicBlueprints from market state and programming history."""

    def __init__(
        self,
        settings: AppSettings,
        library: ContentLibrary,
        *,
        selector: WeightedSelector | None = None,
        diversity: DiversityDirector | None = None,
        energy_planner: RadioEnergyPlanner | None = None,
        governor: CreativeGovernor | None = None,
        lyrics_director: LyricsDirector | None = None,
        titles: TitleGenerator | None = None,
    ) -> None:
        self._settings = settings
        self._library = library
        self._selector = selector or WeightedSelector()
        self._diversity = diversity or DiversityDirector(settings.diversity)
        self._energy = energy_planner or RadioEnergyPlanner()
        self._governor = governor or CreativeGovernor(settings.generation)
        self._lyrics = lyrics_director or LyricsDirector(
            library, settings.lyrics, self._diversity, self._selector
        )
        self._titles = titles or TitleGenerator(self._selector)
        self._last_decision: DirectorDecision | None = None

    # -- accessors ---------------------------------------------------------

    @property
    def library(self) -> ContentLibrary:
        return self._library

    @property
    def diversity(self) -> DiversityDirector:
        return self._diversity

    @property
    def energy_planner(self) -> RadioEnergyPlanner:
        return self._energy

    @property
    def selector(self) -> WeightedSelector:
        return self._selector

    @property
    def lyrics_director(self) -> LyricsDirector:
        """The subject planner, for callers that compose the words themselves.

        Exposed because lyric *composition* deliberately lives outside this class: it
        needs lyric history from the database, and a director that reached for a session
        would stop being testable against a plain list.
        """
        return self._lyrics

    @property
    def last_decision(self) -> DirectorDecision | None:
        return self._last_decision

    # -- the main entry point ---------------------------------------------

    def create_blueprint(
        self,
        *,
        track_id: str,
        state: MarketStateV1,
        history: ProgrammingHistory,
        buffer: BufferHealthV1,
        now: datetime,
        capacity_ratio: float = 1.0,
        consecutive_failures: int = 0,
        provider_healthy: bool = True,
        used_titles: tuple[str, ...] = (),
        recent_titles: tuple[str, ...] = (),
        used_seeds: frozenset[int] = frozenset(),
        used_signatures: frozenset[str] = frozenset(),
    ) -> DirectorDecision:
        """Decide everything about one track, avoiding a repeated blueprint.

        §11's "same blueprint: never repeat" cannot be a selection constraint: the
        signature is a hash of *all* the decisions, so it does not exist until every
        choice has been made. It is enforced here instead, by re-deciding when the result
        collides. The selector's RNG advances on each attempt, so a retry genuinely
        explores elsewhere rather than reproducing the same answer.

        ``used_signatures`` must come from the **full** library — the repository's
        ``blueprint_signature_exists`` query — not from the windowed ``history``. A
        rolling window silently permits a repeat from beyond its edge, which is exactly
        what the §3.12 report caught over 2 800 decisions.

        After :data:`MAX_SIGNATURE_ATTEMPTS` the decision is accepted with a logged
        warning. A station that refuses to produce anything is worse than one that
        repeats a creative decision once in several thousand tracks, and §11's intent is
        served by making it vanishingly rare rather than impossible.

        ``state`` may be a neutralised state (§63-E) when the feed is dead, in which case
        the director works from mid energy and an UNKNOWN regime — producing deliberately
        unremarkable programming rather than refusing to produce any.
        """
        # Signatures already in the window count too, so a caller that passes only the
        # history still gets the protection a window can give.
        known = used_signatures | history.signatures()

        decision = self._decide_once(
            track_id=track_id,
            state=state,
            history=history,
            buffer=buffer,
            now=now,
            capacity_ratio=capacity_ratio,
            consecutive_failures=consecutive_failures,
            provider_healthy=provider_healthy,
            used_titles=used_titles,
            recent_titles=recent_titles,
            used_seeds=used_seeds,
        )
        collided: set[str] = set()
        for attempt in range(2, MAX_SIGNATURE_ATTEMPTS + 1):
            if decision.blueprint.signature() not in known:
                break
            # Remember *what* collided, not just that something did. Retrying with identical
            # inputs re-rolls the same weights, and in a narrow market that keeps landing on
            # the same decision — which is how six attempts ended in an accepted repeat.
            collided.add(decision.blueprint.composition.genre)
            _log.debug(
                "director.signature_collision_retry",
                track_id=track_id,
                attempt=attempt,
                signature=decision.blueprint.signature()[:12],
                discouraging=sorted(collided),
            )
            decision = self._decide_once(
                track_id=track_id,
                state=state,
                history=history,
                buffer=buffer,
                now=now,
                capacity_ratio=capacity_ratio,
                consecutive_failures=consecutive_failures,
                provider_healthy=provider_healthy,
                used_titles=used_titles,
                recent_titles=recent_titles,
                used_seeds=used_seeds,
                discourage_genres=frozenset(collided),
            )
        else:
            if decision.blueprint.signature() in known:
                _log.warning(
                    "director.signature_collision_accepted",
                    track_id=track_id,
                    attempts=MAX_SIGNATURE_ATTEMPTS,
                    signature=decision.blueprint.signature()[:12],
                    detail=(
                        "could not find an unused blueprint signature; accepting a repeat "
                        "rather than stalling the queue"
                    ),
                )

        self._last_decision = decision
        return decision

    def _decide_once(
        self,
        *,
        track_id: str,
        state: MarketStateV1,
        history: ProgrammingHistory,
        buffer: BufferHealthV1,
        now: datetime,
        capacity_ratio: float,
        consecutive_failures: int,
        provider_healthy: bool,
        used_titles: tuple[str, ...],
        recent_titles: tuple[str, ...],
        used_seeds: frozenset[int],
        discourage_genres: frozenset[str] = frozenset(),
    ) -> DirectorDecision:
        """One full pass of the decision pipeline."""
        rationale: list[str] = []
        selections: dict[str, SelectionResult[object]] = {}

        # --- operational stance first: it gates everything creative (§94, §95).
        divergence = self._diversity.divergence_pressure(history)
        diversity_report = self._diversity.score(history)
        stance = self._governor.stance(
            buffer=buffer,
            capacity_ratio=capacity_ratio,
            consecutive_failures=consecutive_failures,
            diversity_pressure=divergence.strength,
            provider_healthy=provider_healthy,
        )
        rationale.append(f"{stance.priority.value} priority ({stance.reason})")
        if divergence.is_active:
            rationale.append(
                f"diversity {diversity_report.score:.0f} below floor; diverging on "
                f"{', '.join(divergence.dimensions)}"
            )

        # --- station energy (§98), which drives genre and BPM.
        energy_plan = self._energy.plan(
            market_energy=state.energy,
            energy_velocity=state.energy_velocity,
            divergence_strength=divergence.strength,
        )
        energy = energy_plan.target_energy
        rationale.append(
            f"target energy {energy:.0f} from market {state.energy:.0f} "
            f"({energy_plan.reason})"
        )

        # --- genre (§10). The primary creative decision.
        genre_result = self._select_genre(
            history=history,
            state=state,
            energy=energy,
            stance=stance,
            discourage=discourage_genres,
        )
        genre = self._library.genre(genre_result.chosen)
        selections["genre"] = genre_result  # type: ignore[assignment]
        rationale.append(f"genre {genre.key}")
        if genre_result.relaxed:
            rationale.append(f"relaxed: {', '.join(genre_result.relaxed)}")

        secondary_genre = self._select_secondary_genre(genre, energy, stance)
        if secondary_genre is not None:
            rationale.append(f"secondary genre {secondary_genre}")

        # --- vocals (§1, §8, §11). Decided before persona, since persona depends on it.
        instrumental = self._decide_instrumental(
            history=history, genre=genre, energy=energy
        )
        rationale.append("instrumental" if instrumental else "vocal")

        persona = self._select_persona(
            history=history, genre=genre, energy=energy, instrumental=instrumental,
            stance=stance,
        )
        if persona is not None:
            rationale.append(f"persona {persona.call_sign}")

        vocal_style = (
            VocalStyle.NONE
            if instrumental
            else self._select_vocal_style(
                history=history, genre=genre, persona=persona, stance=stance
            )
        )

        # --- lyrics (§13). Delegated: the LyricsDirector owns subject selection.
        lyric_plan = self._lyrics.plan(
            history=history,
            regime=state.regime,
            session=state.session,
            energy=energy,
            persona=persona,
            vocal_style=vocal_style,
            instrumental=instrumental,
            # The active symbol travels *with* the state rather than as a second
            # parameter, so the director cannot be handed a gold reading and a Bitcoin
            # symbol. One object, one market.
            symbol=state.symbol,
            temperature=stance.temperature,
            divergence_strength=divergence.strength,
        )
        rationale.extend(lyric_plan.rationale)

        # --- tempo, key, duration, structure.
        bpm_result = self._select_bpm(history=history, genre=genre, energy=energy, stance=stance)
        selections["bpm"] = bpm_result  # type: ignore[assignment]
        bpm = bpm_result.chosen
        rationale.append(f"{bpm} BPM")

        key_result = self._select_key(history=history, state=state, energy=energy, stance=stance)
        selections["key"] = key_result  # type: ignore[assignment]
        musical_key = key_result.chosen

        duration_result = self._select_duration(
            history=history, stance=stance, energy=energy
        )
        selections["duration"] = duration_result  # type: ignore[assignment]
        duration = duration_result.chosen
        structure = self._select_structure(genre, stance)

        # --- intensities, derived from the genre's defaults and the target energy.
        intensities = self._intensities(genre, energy)

        seed = self._select_seed(used_seeds)
        title = self._titles.generate(
            TitleContext(
                regime=state.regime,
                direction=state.direction,
                session=state.session,
                energy=energy,
                genre=genre.key,
                primary_topic=lyric_plan.spec.primary_topic,
                instrumental=instrumental,
            ),
            used_titles=used_titles,
            recent_titles=recent_titles,
        )

        blueprint = MusicBlueprintV1(
            track_id=track_id,
            created_at_iso=now.isoformat(),
            market=BlueprintMarketContextV1(
                symbol=state.symbol,
                regime=state.regime,
                direction=state.direction,
                energy=state.energy,
                trend_strength=state.trend_strength,
                volatility=state.volatility,
                confidence=state.confidence,
                session=state.session.value,
            ),
            composition=CompositionSpecV1(
                genre=genre.key,
                secondary_genre=secondary_genre,
                bpm=bpm,
                key=musical_key,
                duration_seconds=duration,
                energy=energy / 100.0,
                rhythm_density=intensities["rhythm_density"],
                bass_intensity=intensities["bass_intensity"],
                drum_intensity=intensities["drum_intensity"],
                melodic_complexity=intensities["melodic_complexity"],
                structure=structure,
                instrumentation=self._select_instrumentation(genre, secondary_genre),
                mood=self._select_mood(genre, state, energy),
            ),
            vocal=VocalSpecV1(
                enabled=not instrumental,
                style=vocal_style,
                density=(
                    0.0
                    if instrumental
                    else self._vocal_density(lyric_plan, genre)
                ),
            ),
            lyrics=lyric_plan.spec,
            novelty=NoveltySpecV1(target=self._novelty_target(stance)),
            persona_id=persona.key if persona is not None else None,
            title=title,
            priority=stance.priority,
            creative_temperature=min(1.0, stance.temperature / 1.5),
            seed=seed,
            rationale=tuple(rationale),
        )

        decision = DirectorDecision(
            blueprint=blueprint,
            energy_plan=energy_plan,
            stance=stance,
            divergence=divergence,
            diversity_score=diversity_report.score,
            lyric_plan=lyric_plan,
            selections=selections,
        )
        _log.debug(
            "director.blueprint_created",
            track_id=track_id,
            genre=genre.key,
            bpm=bpm,
            energy=round(energy, 1),
            regime=state.regime.value,
            instrumental=instrumental,
            priority=stance.priority.value,
            diversity=round(diversity_report.score, 1),
        )
        return decision

    # -- genre -------------------------------------------------------------

    def _select_genre(
        self,
        *,
        history: ProgrammingHistory,
        state: MarketStateV1,
        energy: float,
        stance: CreativeStance,
        discourage: frozenset[str] = frozenset(),
    ) -> SelectionResult[str]:
        """§1's energy mapping, as weights rather than rules.

        ``discourage`` holds genres whose decision already collided with an existing
        blueprint signature on an earlier attempt. It is a heavy *weight*, not a veto:
        §1's mapping has to keep holding, and in a market so narrow that only one genre
        fits, airing that genre again beats airing something the market does not justify.

        Selects over genre **keys**, not ``GenreDefinition`` objects. That is not a
        stylistic choice: §11's constraints are predicates over keys, so handing them
        objects would make every comparison ``GenreDefinition != str`` — trivially true —
        and the "same genre maximum 2 consecutive" rule would silently never fire. The
        type checker caught exactly that; selecting over keys makes the two sides agree by
        construction.
        """
        candidates: list[Candidate[str]] = []
        session_strength = self._settings.music.session_bias_strength

        for genre in self._library.genres.values():
            candidate = Candidate(value=genre.key)
            # Energy fit is the dominant factor. This is where §1's "quiet -> lo-fi,
            # breakout -> aggressive trap" actually comes from.
            #
            # The falloff is deliberately tight. At 22 a genre eight points outside its
            # band scored 0.94 — effectively unpenalised — and deep house was turning up
            # during breakouts at energy 86. At 11, eight points out costs ~25 % and
            # twenty points out is close to excluded, which is what §1's mapping implies.
            candidate.multiply(
                "energy_fit",
                band_fit(
                    energy, genre.energy.min, genre.energy.max, falloff=11.0, floor=0.01
                ),
            )
            candidate.multiply("regime", genre.affinity_for(state.regime))
            # §97: session bias is real but subordinate to market energy, so it is damped
            # by an exponent below 1 rather than applied at full strength.
            candidate.multiply(
                "session", genre.session_bias(state.session) ** session_strength
            )
            candidate.multiply("recency", self._diversity.genre_penalty(history, genre.key))
            if genre.key in discourage:
                candidate.multiply("signature_collision", SIGNATURE_RETRY_PENALTY)
            if genre.experimental and not stance.allow_experimental:
                candidate.veto(
                    f"experimental genre withheld at {stance.priority.value} priority"
                )
            # Hard exclusion far outside the band. The weight floor alone was not enough:
            # with 28 candidates and a high creative temperature, a genre 55 points out of
            # band still won occasionally, and the report showed a 172 BPM drum & bass
            # track airing in a dead-quiet market. §1's mapping has to hold.
            #
            # Safe to veto because the library validator guarantees that every point on the
            # 0-100 energy scale is covered by at least one non-experimental genre, so
            # something always survives.
            distance = genre.energy.distance_to(energy)
            if distance > MAX_ENERGY_DISTANCE:
                candidate.veto(
                    f"energy {energy:.0f} is {distance:.0f} points outside the "
                    f"{genre.energy.min:.0f}-{genre.energy.max:.0f} band"
                )
            candidates.append(candidate)

        constraints = self._diversity.genre_constraints(history)
        return self._selector.select(
            candidates, constraints, temperature=stance.temperature
        )

    def _select_secondary_genre(
        self, genre: GenreDefinition, energy: float, stance: CreativeStance
    ) -> str | None:
        """An optional §8 secondary genre, drawn from the primary's declared pairings."""
        if not genre.pairs_with:
            return None
        if self._selector.rng.random() > SECONDARY_GENRE_PROBABILITY:
            return None
        candidates: list[Candidate[str]] = []
        for key in genre.pairs_with:
            if key not in self._library.genres:
                continue
            other = self._library.genre(key)
            candidate = Candidate(value=key)
            candidate.multiply(
                "energy_fit",
                band_fit(energy, other.energy.min, other.energy.max, falloff=30.0),
            )
            if other.experimental and not stance.allow_experimental:
                candidate.veto("experimental secondary withheld")
            candidates.append(candidate)
        if not candidates:
            return None
        return self._selector.select(candidates, temperature=stance.temperature).chosen

    # -- vocals ------------------------------------------------------------

    def _decide_instrumental(
        self, *, history: ProgrammingHistory, genre: GenreDefinition, energy: float
    ) -> bool:
        """Whether the next track is instrumental.

        Two independent inputs multiply: §1's energy-based vocal probability (vocals get
        likelier as energy rises) and the genre's own ``vocal_affinity`` (ambient barely
        carries vocals at any energy). Then §11's run limits apply as hard overrides,
        because "maximum 4 consecutive instrumentals" is a rule, not a preference.
        """
        energy_probability = self._settings.music.vocal_probability_for(energy)
        # The genre factor is centred on 1.0 at the average affinity of 0.5, so it
        # modulates the configured probability rather than only ever reducing it. The
        # earlier form (0.35 + 0.65 * affinity) peaked at 1.0 only for a maximally
        # vocal genre, which meant the realised vocal rate sat permanently below the
        # configured value — a station configured for 50 % vocals delivered 34 %, and
        # the instrumental ratio drifted to two thirds.
        genre_factor = 0.6 + 0.8 * genre.vocal_affinity
        vocal_probability = min(0.97, energy_probability * genre_factor)

        constraints = self._diversity.vocal_constraints(history)
        candidates = [
            Candidate(value=True, weight=max(0.01, 1.0 - vocal_probability)),
            Candidate(value=False, weight=max(0.01, vocal_probability)),
        ]
        return self._selector.select(candidates, constraints).chosen

    def _select_persona(
        self,
        *,
        history: ProgrammingHistory,
        genre: GenreDefinition,
        energy: float,
        instrumental: bool,
        stance: CreativeStance,
    ) -> PersonaDefinition | None:
        """§100 persona, matched to genre and energy.

        An instrumental track may still carry a persona — a producer credit — but only if
        that persona plausibly releases instrumentals, which is what
        ``instrumental_affinity`` records.
        """
        pool = self._library.personas_for(genre.key, energy)
        if not pool:
            # Relax the energy requirement before giving up: a genre with no
            # energy-matched persona is better served by an adjacent one than by none.
            pool = tuple(
                persona
                for persona in self._library.personas.values()
                if genre.key in persona.genres
            )
        if not pool:
            return None

        # Keys, not objects: the persona recency constraint is a predicate over keys.
        candidates: list[Candidate[str]] = []
        for persona in pool:
            candidate = Candidate(value=persona.key)
            candidate.multiply(
                "energy_fit",
                band_fit(energy, persona.energy.min, persona.energy.max, falloff=25.0),
            )
            candidate.multiply(
                "recency", self._diversity.persona_penalty(history, persona.key)
            )
            if instrumental:
                candidate.multiply("instrumental", max(0.05, persona.instrumental_affinity))
            else:
                candidate.multiply(
                    "vocal", max(0.05, 1.0 - persona.instrumental_affinity * 0.5)
                )
                # Prefer a persona whose own vocal styles the genre actually supports.
                # Without this the pairing could be contradictory — a spoken-word persona
                # credited on a genre whose declared styles are sung and chopped-hook —
                # and the only way out downstream was to override one or the other.
                # Handled here, where both are still in view, rather than patched later.
                if set(persona.vocal_styles) & set(genre.vocal_styles):
                    candidate.multiply("style_match", 1.8)
            candidates.append(candidate)

        constraints = [self._diversity.persona_constraint(history)]
        chosen = self._selector.select(
            candidates, constraints, temperature=stance.temperature
        ).chosen
        return self._library.persona(chosen)

    def _select_vocal_style(
        self,
        *,
        history: ProgrammingHistory,
        genre: GenreDefinition,
        persona: PersonaDefinition | None,
        stance: CreativeStance,
    ) -> VocalStyle:
        """A style both the genre and the persona support (§11 run limit applies)."""
        genre_styles = set(genre.vocal_styles)
        if persona is not None:
            shared = genre_styles & set(persona.vocal_styles)
            # Intersection preferred. When there is none, the **genre** wins, not the
            # persona: the genre's declared styles are a statement about what the music
            # sounds like, and a style the genre does not list produces a §19 prompt that
            # contradicts itself ("melodic techno, spoken-word vocal"). A persona is a
            # credit; it does not change the arrangement.
            #
            # The earlier fallback preferred the persona on the grounds that it beat having
            # no vocal at all — but the genre's style list is validated non-empty, so that
            # case does not exist, and the fallback was simply choosing the contradiction.
            pool = tuple(shared) if shared else tuple(genre_styles)
        else:
            pool = tuple(genre_styles)
        if not pool:
            pool = (VocalStyle.RAP,)

        # Style *values*, because history stores the style as a plain string and the run
        # constraint compares strings. VocalStyle subclasses str, so the comparison would
        # happen to work — but relying on that is how the other three constraints came to
        # be silently inert.
        candidates = [Candidate(value=style.value) for style in pool]
        constraint = self._diversity.vocal_style_constraint(history)
        constraints = [constraint] if constraint.reason else []
        chosen = self._selector.select(
            candidates, constraints, temperature=stance.temperature
        ).chosen
        return VocalStyle(chosen)

    def _vocal_density(self, lyric_plan: LyricPlan, genre: GenreDefinition) -> float:
        """§8 vocal density, from the lyric format and the genre's tolerance."""
        base = lyric_plan.lyric_format.density if lyric_plan.lyric_format else 0.5
        # A genre with low vocal affinity thins the vocal even when the format is dense.
        return max(0.05, min(1.0, base * (0.5 + 0.5 * genre.vocal_affinity)))

    # -- tempo, key, duration, structure ----------------------------------

    def _select_bpm(
        self,
        *,
        history: ProgrammingHistory,
        genre: GenreDefinition,
        energy: float,
        stance: CreativeStance,
    ) -> SelectionResult[int]:
        """A tempo inside both the genre's range and the energy band's.

        The intersection can be empty — a 170 BPM genre selected at an energy whose band
        tops out at 125 — in which case the genre's own range wins. The genre was chosen
        deliberately and playing it at the wrong tempo would be worse than letting the
        tempo sit outside the energy band.
        """
        band = self._settings.music.bpm_band_for(energy)
        low = max(genre.bpm.min, float(band.low))
        high = min(genre.bpm.max, float(band.high))
        if low > high:
            low, high = genre.bpm.min, genre.bpm.max

        # Position within the range follows energy, so a genre's fast end is reserved for
        # its high-energy appearances.
        span = genre.energy.width or 1.0
        position = max(0.0, min(1.0, (energy - genre.energy.min) / span))
        centre = low + (high - low) * (0.3 + 0.7 * position)

        candidates: list[Candidate[int]] = []
        for bpm in range(int(low), int(high) + 1):
            candidate = Candidate(value=bpm)
            # Triangular preference around the energy-derived centre.
            distance = abs(bpm - centre)
            candidate.multiply(
                "centre_fit", max(0.05, 1.0 - distance / max(1.0, (high - low) / 2 + 1))
            )
            candidates.append(candidate)

        constraints = [self._diversity.bpm_constraint(history)]
        return self._selector.select(
            candidates, constraints, temperature=stance.temperature
        )

    def _select_key(
        self,
        *,
        history: ProgrammingHistory,
        state: MarketStateV1,
        energy: float,
        stance: CreativeStance,
    ) -> SelectionResult[str]:
        """A key whose mode suits the market, preferring harmonic distance from the last."""
        pool = key_library.key_candidates(
            energy=energy, direction=state.direction, regime=state.regime
        )
        previous = history.most_recent.musical_key if history.most_recent else None

        candidates: list[Candidate[str]] = []
        for key in pool:
            candidate = Candidate(value=key)
            candidate.multiply("idiomatic", key_library.key_weight(key))
            candidate.multiply("recency", self._diversity.key_penalty(history, key))
            if previous is not None:
                # Prefer harmonic distance: a semitone shift sounds like a mistake, a
                # tritone or a fourth sounds like a decision.
                distance = key_library.relative_distance(previous, key)
                candidate.multiply("harmonic_distance", 0.5 + 0.1 * distance)
            candidates.append(candidate)

        constraints = [self._diversity.key_constraint(history)]
        return self._selector.select(
            candidates, constraints, temperature=stance.temperature
        )

    def _select_duration(
        self, *, history: ProgrammingHistory, stance: CreativeStance, energy: float
    ) -> SelectionResult[int]:
        """Track length, bounded by the §95 duration reach."""
        music = self._settings.music
        low = music.min_duration_seconds
        full_high = music.max_duration_seconds
        high = int(low + (full_high - low) * stance.duration_reach)
        if high <= low:
            high = low + 1

        # Higher energy trends slightly shorter: an intense track outstays its welcome
        # sooner than a quiet one.
        energy_bias = 1.0 - 0.25 * (energy / 100.0)
        centre = low + (high - low) * 0.55 * energy_bias

        candidates: list[Candidate[int]] = []
        for duration in range(low, high + 1, 5):
            candidate = Candidate(value=duration)
            distance = abs(duration - centre)
            candidate.multiply(
                "centre_fit", max(0.05, 1.0 - distance / max(1.0, (high - low) / 2 + 1))
            )
            candidates.append(candidate)
        if not candidates:
            candidates = [Candidate(value=low)]

        constraints = [self._diversity.duration_constraint(history)]
        return self._selector.select(
            candidates, constraints, temperature=stance.temperature
        )

    def _select_structure(
        self, genre: GenreDefinition, stance: CreativeStance
    ) -> tuple[str, ...]:
        """A section sequence from the genre's templates.

        With structural novelty permitted (§95), the chosen template may be varied by
        repeating or dropping one section — a small, safe deviation. Inventing an
        arbitrary section order would produce prompts the generator handles badly.
        """
        template = self._selector.choose_from(genre.structures)
        if not stance.allow_structural_novelty or len(template) < 4:
            return template
        if self._selector.rng.random() > 0.3:
            return template

        sections = list(template)
        if self._selector.rng.random() < 0.5:
            # Repeat an interior section.
            index = self._selector.rng.randrange(1, len(sections) - 1)
            sections.insert(index, sections[index])
        else:
            # Drop an interior section, never the first or last.
            index = self._selector.rng.randrange(1, len(sections) - 1)
            del sections[index]
        return tuple(sections)

    # -- texture -----------------------------------------------------------

    def _intensities(self, genre: GenreDefinition, energy: float) -> dict[str, float]:
        """Blend the genre's defaults toward the target energy.

        §1 lists drum intensity, bass intensity and rhythm density as things the market
        influences. The genre sets the character and energy modulates it, rather than
        energy overriding the genre — otherwise every genre would sound the same at the
        same energy, which is the collapse §11 exists to prevent.
        """
        # -1 at zero energy, +1 at maximum, so the genre's default sits at mid energy.
        lean = (energy - 50.0) / 50.0
        def blend(base: float, sensitivity: float) -> float:
            return max(0.05, min(1.0, base + lean * sensitivity))

        return {
            "rhythm_density": blend(genre.rhythm_density, 0.18),
            "bass_intensity": blend(genre.bass_intensity, 0.15),
            "drum_intensity": blend(genre.drum_intensity, 0.20),
            # Melodic complexity moves *against* energy: loud music is usually simpler.
            "melodic_complexity": blend(genre.melodic_complexity, -0.12),
        }

    def _select_instrumentation(
        self, genre: GenreDefinition, secondary: str | None
    ) -> tuple[str, ...]:
        """Prompt hints, mostly from the primary genre with an occasional borrow."""
        hints = list(genre.instrumentation)
        if secondary and secondary in self._library.genres:
            other = self._library.genre(secondary).instrumentation
            if other:
                hints.append(self._selector.choose_from(other))
        if len(hints) <= 4:
            return tuple(hints)
        return tuple(self._selector.shuffled(hints)[:4])

    def _select_mood(
        self, genre: GenreDefinition, state: MarketStateV1, energy: float
    ) -> tuple[str, ...]:
        """Emotional tone words (§1), from the genre plus the market's character."""
        words = list(genre.mood)
        if state.regime in {MarketRegime.COMPRESSION, MarketRegime.BREAKOUT_BUILDUP}:
            words.append("coiled")
        elif state.regime is MarketRegime.EXTREME_VOLATILITY:
            words.append("urgent")
        elif state.regime is MarketRegime.REVERSAL:
            words.append("turning")
        elif state.regime is MarketRegime.QUIET:
            words.append("becalmed")
        if state.direction is MarketDirection.BEARISH and energy > 55:
            words.append("downcast")
        # Deduplicate while preserving order; a repeated mood word in a prompt reads as
        # emphasis the director did not intend.
        seen: set[str] = set()
        unique: list[str] = []
        for word in words:
            if word not in seen:
                seen.add(word)
                unique.append(word)
        return tuple(unique[:5])

    # -- novelty and seed --------------------------------------------------

    def _novelty_target(self, stance: CreativeStance) -> float:
        """§8 novelty target. Higher when the station can afford a regeneration.

        At CRITICAL priority the target drops: a strict novelty requirement means more
        rejections and more regeneration, which a nearly-empty queue cannot pay for.
        Phrased as a *target*, never a guarantee (§21).
        """
        if stance.priority is GenerationPriority.CRITICAL:
            return 0.70
        if stance.priority is GenerationPriority.HIGH:
            return 0.82
        return 0.90

    def _select_seed(self, used_seeds: frozenset[int]) -> int:
        """A seed not previously used (§23).

        §23 is explicit that seeds are metadata rather than proof of uniqueness, so this
        only avoids *intentional* reuse. After a bounded number of attempts it returns a
        fresh draw regardless: with a 2**53 space a collision is vanishingly unlikely, and
        looping forever to avoid one would be a worse failure than reusing a seed.
        """
        for _ in range(24):
            seed = self._selector.rng.randrange(SEED_SPACE)
            if seed not in used_seeds:
                return seed
        return self._selector.rng.randrange(SEED_SPACE)


__all__ = [
    "SECONDARY_GENRE_PROBABILITY",
    "SEED_SPACE",
    "DirectorDecision",
    "MusicDirector",
]
