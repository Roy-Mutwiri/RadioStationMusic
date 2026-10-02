"""Creative direction: the market composes the radio (§8–§12, §94–§100).

The layer that turns a :class:`~tradefix_radio.contracts.market.MarketStateV1` into a
:class:`~tradefix_radio.contracts.music.MusicBlueprintV1`.

Everything creative is **data**, loaded from ``config/*.yaml``: genres, topics, personas,
lyric formats, narrator perspectives. Nothing in the code names a genre or a topic, so
changing the station's musical character is an edit to YAML, not to Python (§10, §14).

Every decision is made by the same mechanism — weighted candidates, named multiplicative
factors, hard constraints, temperature-controlled sampling — so every decision can
explain itself. There is no ``if regime == X: genre = Y`` anywhere, which §1 explicitly
forbids.
"""

from tradefix_radio.director.diversity import (
    DivergencePressure,
    DiversityComponent,
    DiversityDirector,
    DiversityReport,
)
from tradefix_radio.director.energy_curve import EnergyPlan, RadioEnergyPlanner
from tradefix_radio.director.history import (
    HistoryEntry,
    ProgrammedTrack,
    ProgrammingHistory,
)
from tradefix_radio.director.library import (
    ContentLibrary,
    GenreDefinition,
    LyricFormatDefinition,
    PersonaDefinition,
    TopicDefinition,
    load_content_library,
)
from tradefix_radio.director.music_director import DirectorDecision, MusicDirector
from tradefix_radio.director.selection import (
    Candidate,
    Constraint,
    SelectionResult,
    WeightedSelector,
    band_fit,
    recency_penalty,
)
from tradefix_radio.director.temperature import CreativeGovernor, CreativeStance
from tradefix_radio.director.titles import TitleContext, TitleGenerator

__all__ = [
    "Candidate",
    "Constraint",
    "ContentLibrary",
    "CreativeGovernor",
    "CreativeStance",
    "DirectorDecision",
    "DivergencePressure",
    "DiversityComponent",
    "DiversityDirector",
    "DiversityReport",
    "EnergyPlan",
    "GenreDefinition",
    "HistoryEntry",
    "LyricFormatDefinition",
    "MusicDirector",
    "PersonaDefinition",
    "ProgrammedTrack",
    "ProgrammingHistory",
    "RadioEnergyPlanner",
    "SelectionResult",
    "TitleContext",
    "TitleGenerator",
    "TopicDefinition",
    "WeightedSelector",
    "band_fit",
    "load_content_library",
    "recency_penalty",
]
