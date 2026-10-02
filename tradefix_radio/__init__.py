"""Trade Fix Radio — autonomous 24/7 market-reactive AI music radio station.

The package is layered; higher layers may import lower ones, never the reverse:

    core          primitives: errors, clock, ids, event bus, state machine
    contracts     versioned cross-boundary schemas (§90)
    config        validated configuration
    monitoring    logging, health, metrics
    persistence   database, models, repositories
    storage       filesystem layout and retention
    market        feeds, features, regimes, sessions
    director      music/lyric/diversity direction
    lyrics        lyric generation and validation
    generation    provider abstraction and job management
    audio         analysis, mastering, transitions, playout
    originality   fingerprints and similarity
    radio         queue, scheduler, rotation, emergency
    obs           broadcast software integration

See docs/ARCHITECTURE.md.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
