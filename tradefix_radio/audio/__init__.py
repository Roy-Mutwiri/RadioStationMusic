"""Audio: PCM representation, file I/O, synthesis, mixing and output (ADR-06, §30, §62).

One representation throughout — float32, ``(frames, channels)``, nominal [-1, 1] — so QC,
mastering, mixing and playout never disagree about shape, dtype or scale. See
:mod:`tradefix_radio.audio.pcm` for why each of those three choices rules out a specific
class of bug.

Output goes through an :class:`~tradefix_radio.audio.sinks.AudioSink`, which is what lets the
same playout engine drive a real sound card, a WAV file, an ffmpeg pipe, or nothing at all.
``NullSink`` is not a test stub bolted on afterwards: §64's accelerated endurance runs and
§72's simulation mode both need a station that broadcasts to nowhere at faster than real
time, and that only works if the sink is a seam from the start.
"""

from tradefix_radio.audio.io import (
    AudioFileInfo,
    format_for,
    read_audio,
    read_info,
    write_audio,
)
from tradefix_radio.audio.pcm import (
    CLIP_THRESHOLD,
    SAMPLE_DTYPE,
    SILENCE_DBFS,
    AudioBuffer,
    Samples,
    from_dbfs,
    to_dbfs,
)
from tradefix_radio.audio.synthesis import Synthesiser, SynthesisSpec, render

__all__ = [
    "CLIP_THRESHOLD",
    "SAMPLE_DTYPE",
    "SILENCE_DBFS",
    "AudioBuffer",
    "AudioFileInfo",
    "Samples",
    "SynthesisSpec",
    "Synthesiser",
    "format_for",
    "from_dbfs",
    "read_audio",
    "read_info",
    "render",
    "to_dbfs",
    "write_audio",
]
