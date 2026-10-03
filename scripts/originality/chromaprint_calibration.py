"""Measure what Chromaprint bit agreement actually means, before any threshold uses it.

The ReviewResolver has to distinguish "the same recording, modified" from "a different
song in the same genre". Both are legitimate outcomes of a radio station generating
repeatedly in one style, and only one of them is a duplicate. Guessing a cut-off would
repeat the mistake that produced the 7% approval rate in the first place — a number that
looked principled and was not measured.

So this builds the variants a real duplicate would arrive as (a re-encode, a gain change,
a master, a crop) alongside genuinely different material, and reports the bit agreement
for each. The bands in the output are observations, not targets.

    python -m scripts.originality.chromaprint_calibration

Writes docs/status/CHROMAPRINT_CALIBRATION.md.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tradefix_radio.audio.analysis import extract_features  # noqa: E402
from tradefix_radio.audio.fingerprint import (  # noqa: E402
    ChromaprintProvider,
)
from tradefix_radio.audio.io import read_audio, write_audio  # noqa: E402
from tradefix_radio.audio.pcm import AudioBuffer  # noqa: E402

GENERATED = ROOT / "generated"
OUT = ROOT / "docs" / "status" / "CHROMAPRINT_CALIBRATION.md"


@dataclass
class Variant:
    """One transformation of the base recording."""

    name: str
    expectation: str
    detail: str


#: Transformations grouped by what the answer *should* be, so the report can be read as a
#: check rather than as a list of numbers.
SAME_RECORDING = "same recording, modified"
DIFFERENT = "different recording"


def ffmpeg(*args: str) -> bool:
    binary = shutil.which("ffmpeg")
    if binary is None:
        return False
    result = subprocess.run(
        [binary, "-y", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"    ffmpeg failed: {result.stderr.strip()[:160]}")
    return result.returncode == 0


def gain(source: Path, destination: Path, db: float) -> bool:
    buffer = read_audio(source)
    scaled = buffer.samples * (10.0 ** (db / 20.0))
    write_audio(
        destination,
        AudioBuffer(samples=np.clip(scaled, -1.0, 1.0), sample_rate=buffer.sample_rate),
    )
    return True


def normalise(source: Path, destination: Path) -> bool:
    buffer = read_audio(source)
    peak = float(np.abs(buffer.samples).max())
    if peak == 0.0:
        return False
    scaled = buffer.samples * (0.98 / peak)
    write_audio(destination, AudioBuffer(samples=scaled, sample_rate=buffer.sample_rate))
    return True


def pad(source: Path, destination: Path, *, lead: float = 0.0, tail: float = 0.0) -> bool:
    buffer = read_audio(source)
    channels = buffer.samples.shape[1] if buffer.samples.ndim > 1 else 1
    before = np.zeros((int(lead * buffer.sample_rate), channels), dtype=np.float32)
    after = np.zeros((int(tail * buffer.sample_rate), channels), dtype=np.float32)
    samples = buffer.samples if buffer.samples.ndim > 1 else buffer.samples[:, None]
    write_audio(
        destination,
        AudioBuffer(
            samples=np.concatenate([before, samples, after], axis=0),
            sample_rate=buffer.sample_rate,
        ),
    )
    return True


def crop(source: Path, destination: Path, seconds: float) -> bool:
    buffer = read_audio(source)
    frames = int(seconds * buffer.sample_rate)
    if buffer.samples.shape[0] <= frames * 2:
        return False
    write_audio(
        destination,
        AudioBuffer(samples=buffer.samples[frames:], sample_rate=buffer.sample_rate),
    )
    return True


def main() -> int:
    provider = ChromaprintProvider()
    if not provider.available:
        print("fpcalc is not installed; nothing to calibrate")
        return 2

    sources = sorted(GENERATED.glob("TF-*.wav"))
    if len(sources) < 3:
        print(f"need at least 3 generated tracks in {GENERATED}")
        return 2

    base = sources[0]
    work = ROOT / "artifacts" / "chromaprint_calibration"
    work.mkdir(parents=True, exist_ok=True)
    print(f"base recording: {base.name}")

    builders: list[tuple[Variant, Path, object]] = []

    def add(variant: Variant, filename: str, builder) -> None:
        builders.append((variant, work / filename, builder))

    add(
        Variant("exact copy", SAME_RECORDING, "byte-identical file"),
        "exact.wav",
        lambda dst: bool(shutil.copyfile(base, dst)) or True,
    )
    add(
        Variant("WAV rewrite", SAME_RECORDING, "decoded and re-encoded, same format"),
        "rewrite.wav",
        lambda dst: (write_audio(dst, read_audio(base)), True)[1],
    )
    add(
        Variant("FLAC round trip", SAME_RECORDING, "lossless re-encode"),
        "flac.wav",
        lambda dst: ffmpeg("-i", str(base), str(work / "tmp.flac"))
        and ffmpeg("-i", str(work / "tmp.flac"), str(dst)),
    )
    add(
        Variant("MP3 192k round trip", SAME_RECORDING, "lossy re-encode"),
        "mp3.wav",
        lambda dst: ffmpeg("-i", str(base), "-b:a", "192k", str(work / "tmp.mp3"))
        and ffmpeg("-i", str(work / "tmp.mp3"), str(dst)),
    )
    add(
        Variant("gain +3 dB", SAME_RECORDING, "louder, same performance"),
        "gain_up.wav",
        lambda dst: gain(base, dst, 3.0),
    )
    add(
        Variant("gain -6 dB", SAME_RECORDING, "quieter, same performance"),
        "gain_down.wav",
        lambda dst: gain(base, dst, -6.0),
    )
    add(
        Variant("peak normalised", SAME_RECORDING, "what mastering does to level"),
        "normalised.wav",
        lambda dst: normalise(base, dst),
    )
    add(
        Variant("EQ shelf", SAME_RECORDING, "+4 dB above 4 kHz, -3 dB below 150 Hz"),
        "eq.wav",
        lambda dst: ffmpeg(
            "-i", str(base), "-af",
            "highshelf=f=4000:g=4,lowshelf=f=150:g=-3", str(dst),
        ),
    )
    add(
        Variant("dynamics compressed", SAME_RECORDING, "4:1 above -18 dB"),
        "compressed.wav",
        lambda dst: ffmpeg(
            "-i", str(base), "-af",
            "acompressor=threshold=-18dB:ratio=4:attack=5:release=120", str(dst),
        ),
    )
    add(
        Variant("0.5 s leading silence", SAME_RECORDING, "offset at the head"),
        "lead.wav",
        lambda dst: pad(base, dst, lead=0.5),
    )
    add(
        Variant("2 s trailing silence", SAME_RECORDING, "offset at the tail"),
        "tail.wav",
        lambda dst: pad(base, dst, tail=2.0),
    )
    add(
        Variant("5 s head crop", SAME_RECORDING, "the hardest same-recording case"),
        "crop.wav",
        lambda dst: crop(base, dst, 5.0),
    )

    base_print = provider.compute(base, read_audio(base), extract_features(read_audio(base)))
    if base_print is None:
        print("could not fingerprint the base recording")
        return 2

    rows: list[tuple[Variant, float | None]] = []
    for variant, destination, builder in builders:
        print(f"  building {variant.name} ...")
        try:
            ok = builder(destination)
        except Exception as error:
            print(f"    skipped: {type(error).__name__}: {error}")
            ok = False
        if not ok or not destination.is_file():
            rows.append((variant, None))
            continue
        buffer = read_audio(destination)
        other = provider.compute(destination, buffer, extract_features(buffer))
        rows.append((variant, None if other is None else base_print.similarity_to(other)))

    # Genuinely different material, including same-genre neighbours.
    different: list[tuple[str, float | None]] = []
    for other_source in sources[1:9]:
        buffer = read_audio(other_source)
        other = provider.compute(other_source, buffer, extract_features(buffer))
        different.append(
            (other_source.stem, None if other is None else base_print.similarity_to(other))
        )

    render(base, rows, different)
    print(f"\nwrote {OUT}")
    return 0


def render(
    base: Path,
    rows: list[tuple[Variant, float | None]],
    different: list[tuple[str, float | None]],
) -> None:
    same_scores = [s for v, s in rows if s is not None and v.expectation == SAME_RECORDING]
    diff_scores = [s for _n, s in different if s is not None]

    lines = [
        "# Chromaprint calibration",
        "",
        "What bit agreement between two Chromaprint signatures actually means, measured",
        "rather than assumed. The ReviewResolver's duplication thresholds are derived from",
        "this table and from nothing else.",
        "",
        f"Base recording: `{base.name}`",
        "",
        "Agreement is the fraction of matching bits across aligned 32-bit subfingerprints.",
        "Unrelated material sits near 0.5 because half the bits match by chance.",
        "",
        "## Same recording, modified",
        "",
        "| variant | what it simulates | agreement |",
        "|---|---|---|",
    ]
    for variant, score in rows:
        value = "could not build" if score is None else f"**{score:.4f}**"
        lines.append(f"| {variant.name} | {variant.detail} | {value} |")

    lines += [
        "",
        "## Different recordings",
        "",
        "| track | agreement |",
        "|---|---|",
    ]
    for name, score in different:
        lines.append(f"| {name} | {'-' if score is None else f'{score:.4f}'} |")

    lines += ["", "## Observed bands", ""]
    if same_scores:
        lines += [
            f"* same recording, modified: **{min(same_scores):.4f} – {max(same_scores):.4f}** "
            f"(n={len(same_scores)})",
        ]
    if diff_scores:
        lines += [
            f"* different recordings: **{min(diff_scores):.4f} – {max(diff_scores):.4f}** "
            f"(n={len(diff_scores)})",
        ]
    if same_scores and diff_scores:
        margin = min(same_scores) - max(diff_scores)
        lines += [
            "",
            f"Separation between the two populations: **{margin:+.4f}**.",
            "",
            (
                "A positive margin means a single threshold can separate them. "
                "A negative one means it cannot, and the resolver must require "
                "corroboration rather than treating fingerprint agreement as sufficient "
                "on its own."
                if margin <= 0
                else "A positive margin means a single threshold separates the two "
                "populations cleanly."
            ),
        ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-8:]))


if __name__ == "__main__":
    raise SystemExit(main())
