"""The radio: queue, buffer health, scheduling, playout and emergency fallback (§26-§33).

The parts that make this a station rather than an audio library. Three ideas run through all of
them:

**The queue carries explicit lock levels** (§28). Reacting to the market and preserving playback
continuity conflict directly, and the resolution is that not all future programming is equally
future — so each slot says how strongly it resists being replanned.

**Buffer health has two axes** (§26, §93). How much audio exists, and which way that number is
going. A level-only view cannot tell "45 minutes queued and generating" from "45 minutes queued
and the generator died half an hour ago", and only one of those is healthy.

**Playout knows nothing about generation** (§26, §86). It plays valid audio continuously and
escalates through the §33 tiers when there is none. A model crash cannot stop it because it has
no reference to a model.
"""

from tradefix_radio.radio.buffer import (
    BufferAssessment,
    BufferLevel,
    BufferMonitor,
    BufferTrajectory,
)
from tradefix_radio.radio.emergency import (
    EmergencyManager,
    EmergencyStats,
    EmergencyTrack,
    ProceduralSource,
    reserve_from_directory,
)
from tradefix_radio.radio.playout import (
    PlayingItem,
    PlayoutEngine,
    PlayoutState,
    PlayoutStats,
)
from tradefix_radio.radio.queue import (
    QueueEntry,
    QueueSnapshot,
    QueueStats,
    RadioQueue,
    ReadinessState,
    build_entry,
)
from tradefix_radio.radio.scheduler import (
    ScheduleDecision,
    Scheduler,
    SchedulerStats,
)
from tradefix_radio.radio.station import RadioStation, StationStats
from tradefix_radio.radio.station_ids import (
    StationIdCategory,
    StationIdLibrary,
    StationIdRecord,
    default_library,
)

__all__ = [
    "BufferAssessment",
    "BufferLevel",
    "BufferMonitor",
    "BufferTrajectory",
    "EmergencyManager",
    "EmergencyStats",
    "EmergencyTrack",
    "PlayingItem",
    "PlayoutEngine",
    "PlayoutState",
    "PlayoutStats",
    "ProceduralSource",
    "QueueEntry",
    "QueueSnapshot",
    "QueueStats",
    "RadioQueue",
    "RadioStation",
    "ReadinessState",
    "ScheduleDecision",
    "Scheduler",
    "SchedulerStats",
    "StationIdCategory",
    "StationIdLibrary",
    "StationIdRecord",
    "StationStats",
    "build_entry",
    "default_library",
    "reserve_from_directory",
]
