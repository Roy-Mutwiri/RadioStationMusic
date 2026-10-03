"""Audio fingerprinting (§6.3, §6.4).

Two separate jobs that are easy to conflate:

**Exact duplication** is a hash. `canonical_sha256` hashes the decoded PCM, not the file, so
the same audio written twice with different WAV metadata — a different `LIST` chunk, a
different encoder string, FLAC versus WAV — produces the same digest. Hashing file bytes would
let a container difference defeat duplicate detection entirely, which §6.3 names as a required
test.

**Perceptual similarity** is a fingerprint. Chromaprint is the right tool and this module
adapts to it when `fpcalc` is on PATH. When it is not — which is the case on this development
machine — a built-in chroma fingerprint stands in, computed from the features already
extracted. It is weaker, and the provider name recorded alongside every fingerprint says which
one produced it, so a comparison between two different providers is never made silently.

What a fingerprint is not
-------------------------
It is not evidence that a track is original. It compares a candidate against **this station's
own library** and nothing else. §86 forbids claiming fingerprints guarantee copyright
uniqueness, and nothing here does.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import numpy as np
import structlog

from tradefix_radio.audio.analysis import AudioFeatures
from tradefix_radio.audio.pcm import AudioBuffer

_log = structlog.get_logger(__name__)

__all__ = [
    "AudioFingerprint",
    "AudioFingerprintProvider",
    "ChromaFingerprintProvider",
    "ChromaprintProvider",
    "canonical_sha256",
    "default_provider",
    "file_sha256",
    "fingerprint_capability",
]

#: Rate and layout the canonical hash is computed at.
#:
#: Fixed so the digest is a property of the *audio*, not of how it happened to be stored. Two
#: files that sound identical must hash identically even if one is 44.1 kHz mono and the other
#: 48 kHz stereo — otherwise a trivial format change defeats exact-duplicate detection.
CANONICAL_HASH_RATE: Final = 22_050

#: Quantisation step for the canonical hash, in amplitude.
#:
#: 16-bit. Hashing float32 directly would make a difference of one ULP — inaudible, and
#: produced routinely by re-encoding — look like a different track. 16-bit is the resolution
#: at which two files are the same recording for any practical purpose.
_HASH_QUANTISATION: Final = 32767.0


def canonical_sha256(buffer: AudioBuffer) -> str:
    """SHA-256 of the decoded audio, independent of container and metadata.

    The normalisation is deliberate and each step earns its place: fold to mono so a channel
    swap does not change the digest, resample to a fixed rate so a rate change does not, and
    quantise to 16-bit so sub-LSB noise does not.

    **Scope, measured rather than assumed.** This catches the same decoded audio written
    twice, and the same PCM carrying different WAV metadata — both §6.3 cases, both verified.
    It does **not** survive a change of sample format: writing float32 audio to 24-bit FLAC
    perturbs samples by ~6e-08, which is inaudible but crosses a quantisation boundary often
    enough that the digest differs. No hash can absorb that, because a hash has boundaries by
    construction.

    That gap is covered deliberately rather than papered over: a re-encode of an existing
    track is caught by the similarity engine, whose exact-match threshold treats a near-1.0
    audio score as a duplicate. Exact detection is a hash; near-exact detection is a
    comparison. Conflating them would mean trusting a hash to do something it cannot.
    """
    samples = np.asarray(buffer.samples, dtype=np.float32)
    if samples.ndim == 2 and samples.shape[1] > 1:
        mono = samples.mean(axis=1)
    else:
        mono = samples.reshape(-1)
    mono = np.nan_to_num(mono, nan=0.0, posinf=0.0, neginf=0.0)

    if buffer.sample_rate != CANONICAL_HASH_RATE and mono.size:
        target_length = max(1, round(mono.size / buffer.sample_rate * CANONICAL_HASH_RATE))
        mono = np.interp(
            np.linspace(0.0, mono.size - 1, num=target_length),
            np.linspace(0.0, mono.size - 1, num=mono.size),
            mono,
        )

    quantised = np.clip(np.rint(mono * _HASH_QUANTISATION), -32768, 32767).astype(np.int16)
    digest = hashlib.sha256()
    digest.update(b"tradefix-canonical-v1")
    digest.update(quantised.tobytes())
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    """SHA-256 of the file bytes. For retention bookkeeping, not duplicate detection."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class AudioFingerprint:
    """A perceptual fingerprint, and who computed it."""

    provider: str
    provider_version: str
    #: Opaque to everything but the provider that made it.
    fingerprint: str
    duration_seconds: float
    #: Vector form, when the provider offers one, for cosine comparison.
    vector: tuple[float, ...] = ()

    def comparable_with(self, other: AudioFingerprint) -> bool:
        """Whether two fingerprints may be compared at all.

        Different providers produce incomparable fingerprints, and comparing them would
        generate a similarity number that means nothing. The engine checks this rather than
        trusting that a library was built by one provider.
        """
        return self.provider == other.provider


class AudioFingerprintProvider(Protocol):
    """The seam §6.4 asks for, so the implementation can change later."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def available(self) -> bool: ...

    def compute(
        self, path: Path, buffer: AudioBuffer, features: AudioFeatures
    ) -> AudioFingerprint | None:
        """Fingerprint one track, or ``None`` if this provider cannot."""
        ...


class ChromaprintProvider:
    """Chromaprint via the `fpcalc` binary.

    The industry standard, and what AcoustID is built on. Absent on this development machine,
    which is the normal case the brief asks to be handled gracefully — `available` reports
    false, `tradefix doctor` says so, and the pipeline falls back.
    """

    #: Chromaprint's own fingerprints are computed over a bounded window; longer tracks are
    #: truncated by fpcalc itself. Stated so the stored duration is not mistaken for coverage.
    _TIMEOUT_SECONDS: Final = 30

    def __init__(self, executable: str = "fpcalc") -> None:
        self._executable = executable
        self._resolved = shutil.which(executable)
        self._version = ""

    @property
    def name(self) -> str:
        return "chromaprint"

    @property
    def version(self) -> str:
        if self._version or not self._resolved:
            return self._version
        try:  # pragma: no cover - depends on the host
            output = subprocess.run(  # noqa: S603 - fixed executable, no shell
                [self._resolved, "-version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self._version = (output.stdout or output.stderr).strip().splitlines()[0][:48]
        except Exception:  # noqa: BLE001 - a version probe must never be fatal
            self._version = "unknown"
        return self._version

    @property
    def available(self) -> bool:
        return self._resolved is not None

    def compute(
        self,
        path: Path,
        buffer: AudioBuffer,  # noqa: ARG002 - fpcalc reads the file, not the decoded buffer
        features: AudioFeatures,
    ) -> AudioFingerprint | None:
        if not self._resolved:
            return None
        try:  # pragma: no cover - depends on the host
            completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
                [self._resolved, "-raw", "-json", str(path)],
                capture_output=True,
                text=True,
                timeout=self._TIMEOUT_SECONDS,
                check=False,
            )
        except Exception as error:  # noqa: BLE001 - fall back rather than fail the track
            _log.warning(
                "fingerprint.chromaprint_failed",
                error_type=type(error).__name__,
                error=str(error),
            )
            return None
        if completed.returncode != 0:
            _log.warning("fingerprint.chromaprint_error", stderr=completed.stderr[:200])
            return None
        import json  # noqa: PLC0415

        try:
            payload = json.loads(completed.stdout)
            raw = payload["fingerprint"]
        except Exception:  # noqa: BLE001 - unparseable output is a missing fingerprint
            return None
        values = raw if isinstance(raw, list) else []
        return AudioFingerprint(
            provider=self.name,
            provider_version=self.version,
            fingerprint=",".join(str(int(v)) for v in values[:512]),
            duration_seconds=float(payload.get("duration", features.duration_seconds)),
            vector=tuple(float(v) for v in values[:128]),
        )


class ChromaFingerprintProvider:
    """A built-in fingerprint from the features already extracted.

    Deliberately modest. It is a quantised chroma-and-timbre signature, which captures
    harmonic and tonal character well enough to catch a near-duplicate and nothing like well
    enough to be called an acoustic ID. It exists so the pipeline has a working fingerprint
    without a system dependency, and it records its own name so nobody mistakes it for
    Chromaprint later.

    Needs no binary, so it is always available.
    """

    #: Quantisation levels for the printable form. Coarse on purpose: the string is for
    #: equality-ish lookup and display, while the vector is what similarity actually uses.
    _LEVELS: Final = 16

    @property
    def name(self) -> str:
        return "chroma-builtin"

    @property
    def version(self) -> str:
        return "1"

    @property
    def available(self) -> bool:
        return True

    def compute(
        self,
        path: Path,  # noqa: ARG002 - computed from features; the file is never opened
        buffer: AudioBuffer,  # noqa: ARG002 - likewise
        features: AudioFeatures,
    ) -> AudioFingerprint | None:
        vector = features.embedding()
        if vector.size == 0:
            return None
        quantised = np.clip(
            np.rint((vector - vector.min()) / (np.ptp(vector) or 1.0) * (self._LEVELS - 1)),
            0,
            self._LEVELS - 1,
        ).astype(int)
        # No ``vector``, deliberately.
        #
        # This provider's vector *is* `features.embedding()` — the identical array the
        # similarity engine already compares as its `embedding` component. Publishing it
        # here too made the engine score the same measurement twice under two names, with
        # 0.48 of the combined weight between them, which is exactly the "pretend these
        # numbers are independent" error §6.5 warns against. What this provider genuinely
        # adds is the quantised signature below: a coarse equality check that the continuous
        # embedding comparison does not provide.
        return AudioFingerprint(
            provider=self.name,
            provider_version=self.version,
            fingerprint="".join(f"{value:x}" for value in quantised),
            duration_seconds=features.duration_seconds,
        )


def default_provider() -> AudioFingerprintProvider:
    """Chromaprint when it is installed, the built-in otherwise."""
    chromaprint = ChromaprintProvider()
    if chromaprint.available:
        return chromaprint
    return ChromaFingerprintProvider()


def fingerprint_capability() -> dict[str, object]:
    """What `tradefix doctor` reports."""
    chromaprint = ChromaprintProvider()
    return {
        "chromaprint_available": chromaprint.available,
        "chromaprint_version": chromaprint.version if chromaprint.available else None,
        "active_provider": default_provider().name,
        "detail": (
            "Chromaprint is installed and will be used."
            if chromaprint.available
            else (
                "fpcalc is not on PATH; the built-in chroma fingerprint is used instead. It is "
                "weaker at near-duplicate detection. Install Chromaprint to improve it."
            )
        ),
    }
