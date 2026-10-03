"""Per-file audio inspection commands (§6.26).

``tradefix analyze``      extract and print the features QC judges
``tradefix qc``           run the checks and show every one with its value and threshold
``tradefix fingerprint``  canonical hash plus the perceptual fingerprint
``tradefix compare``      two files, with the component breakdown
``tradefix master``       master one file and report what changed

These exist because the pipeline's decisions are otherwise only visible through the station.
When a track is rejected and the reason looks wrong, the question is "what does this *file*
measure", and answering it should not require starting a radio station. Each command is a thin
shell over the same functions the pipeline calls — not a parallel implementation, which would
drift and then disagree with production at exactly the moment someone was relying on it.

Output is plain text aligned for reading, with ``--json`` for anything that needs parsing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tradefix_radio.audio.analysis import (
    EMBEDDING_VERSION,
    extract_features,
    librosa_available,
)
from tradefix_radio.audio.fingerprint import (
    canonical_sha256,
    default_provider,
    file_sha256,
    fingerprint_capability,
)
from tradefix_radio.audio.io import read_audio
from tradefix_radio.audio.mastering import ffmpeg_available, master_track
from tradefix_radio.audio.qc import QcStage, QcStatus, run_audio_qc
from tradefix_radio.originality.lyrics import compare_lyrics, fingerprint_lyrics
from tradefix_radio.originality.similarity import (
    LibraryEntry,
    SimilarityEngine,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.audio.analysis import AudioFeatures
    from tradefix_radio.config.schema import AppSettings

__all__ = ["register"]

_STATUS_MARK = {QcStatus.PASS: "ok  ", QcStatus.WARN: "WARN", QcStatus.FAIL: "FAIL"}


def _require_file(path: Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise SystemExit(f"error: {resolved} is not a file")
    return resolved


def _require_librosa() -> None:
    if not librosa_available():
        raise SystemExit(
            "error: librosa is not installed; run `tradefix doctor` for the fix"
        )


def _number(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _feature_rows(features: AudioFeatures) -> list[tuple[str, str]]:
    return [
        ("duration", f"{features.duration_seconds:.2f} s"),
        ("format", f"{features.sample_rate} Hz, {features.channels} ch"),
        ("peak", _number(features.peak, 4)),
        ("rms", _number(features.rms, 4)),
        ("crest factor", _number(features.crest_factor, 2)),
        ("dc offset", _number(features.dc_offset, 5)),
        (
            "integrated loudness",
            "—" if features.integrated_lufs is None else f"{features.integrated_lufs:.2f} LUFS",
        ),
        ("spectral centroid", f"{features.spectral_centroid:.0f} Hz"),
        ("spectral rolloff", f"{features.spectral_rolloff:.0f} Hz"),
        ("zero-crossing rate", _number(features.zero_crossing_rate, 4)),
        ("low energy (<120 Hz)", f"{features.low_energy_ratio:.2%}"),
        ("high energy (>8 kHz)", f"{features.high_energy_ratio:.4%}"),
        ("stereo correlation", _number(features.stereo_correlation, 3)),
        ("mono fold-down loss", f"{features.mono_compatibility_db:.2f} dB"),
        ("tempo", "—" if features.tempo is None else f"{features.tempo:.1f} BPM"),
        ("key", features.musical_key or "—"),
        ("key confidence", _number(features.key_confidence, 2)),
        ("leading silence", f"{features.leading_silence_seconds:.2f} s"),
        ("trailing silence", f"{features.trailing_silence_seconds:.2f} s"),
        ("longest internal gap", f"{features.longest_internal_silence_seconds:.2f} s"),
        ("silence ratio", f"{features.silence_ratio:.2%}"),
        ("clipped samples", f"{features.clipped_sample_ratio:.4%}"),
        ("discontinuities", str(features.discontinuity_count)),
        ("analysis backend", features.backend),
    ]


def _print_table(rows: list[tuple[str, str]]) -> None:
    width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        print(f"  {label:<{width}}{value}")


# ----------------------------------------------------------------- analyze


async def _cmd_analyze(args: argparse.Namespace, _settings: AppSettings) -> int:
    _require_librosa()
    path = _require_file(args.path)
    features = extract_features(read_audio(path))
    if args.json:
        payload: dict[str, Any] = {
            label.replace(" ", "_"): value for label, value in _feature_rows(features)
        }
        payload["embedding_version"] = EMBEDDING_VERSION
        print(json.dumps(payload, indent=2))
        return 0
    print(f"\n  {path.name}\n")
    _print_table(_feature_rows(features))
    print()
    return 0


# ---------------------------------------------------------------------- qc


async def _cmd_qc(args: argparse.Namespace, settings: AppSettings) -> int:
    _require_librosa()
    path = _require_file(args.path)
    features = extract_features(read_audio(path))
    result = run_audio_qc(
        track_id=path.stem,
        features=features,
        settings=settings.qc,
        stage=QcStage.MASTERED if args.mastered else QcStage.RAW,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "track_id": result.track_id,
                    "stage": result.stage.value,
                    "status": result.status.value,
                    "summary": result.summary(),
                    "checks": [
                        {
                            "name": check.name,
                            "status": check.status.value,
                            "value": check.value,
                            "unit": check.unit,
                            "threshold": check.threshold,
                            "reason": check.reason,
                        }
                        for check in result.checks
                    ],
                },
                indent=2,
            )
        )
        return 0 if result.passed else 1

    print(f"\n  {path.name} — {result.status.value.upper()}\n")
    name_width = max(len(check.name) for check in result.checks) + 2
    for check in result.checks:
        value = "—" if check.value is None else f"{check.value:,.4g}"
        unit = f" {check.unit}" if check.unit and check.value is not None else ""
        print(
            f"  {_STATUS_MARK[check.status]}  {check.name:<{name_width}}"
            f"{value}{unit:<10}  {check.threshold}"
        )
        if check.status is not QcStatus.PASS:
            print(f"        {check.reason}")
    print(f"\n  {result.summary()}\n")
    return 0 if result.passed else 1


# -------------------------------------------------------------- fingerprint


async def _cmd_fingerprint(args: argparse.Namespace, _settings: AppSettings) -> int:
    _require_librosa()
    path = _require_file(args.path)
    buffer = read_audio(path)
    features = extract_features(buffer)
    provider = default_provider()
    fingerprint = provider.compute(path, buffer, features)

    rows = [
        ("canonical sha256", canonical_sha256(buffer)),
        ("file sha256", file_sha256(path)),
        ("provider", provider.name),
        ("provider version", provider.version or "—"),
        ("embedding version", str(EMBEDDING_VERSION)),
        ("fingerprint", (fingerprint.fingerprint[:64] + "…") if fingerprint else "—"),
    ]
    if args.json:
        print(json.dumps({label.replace(" ", "_"): value for label, value in rows}, indent=2))
        return 0
    print(f"\n  {path.name}\n")
    _print_table(rows)
    # Stated every time, because a hash printed without its scope invites the reading it
    # cannot support (§86).
    print(
        "\n  The canonical hash identifies this decoded audio within this station's own\n"
        "  library. It is not evidence of originality against any other music.\n"
    )
    if not fingerprint_capability()["chromaprint_available"]:
        print(f"  note: {fingerprint_capability()['detail']}\n")
    return 0


# ----------------------------------------------------------------- compare


async def _cmd_compare(args: argparse.Namespace, settings: AppSettings) -> int:
    _require_librosa()
    left_path = _require_file(args.left)
    right_path = _require_file(args.right)

    left_buffer, right_buffer = read_audio(left_path), read_audio(right_path)
    left = extract_features(left_buffer)
    right = extract_features(right_buffer)
    provider = default_provider()

    engine = SimilarityEngine(settings.originality)
    outcome = engine.evaluate(
        track_id=left_path.stem,
        features=left,
        canonical_hash=canonical_sha256(left_buffer),
        library=[
            LibraryEntry(
                track_id=right_path.stem,
                canonical_hash=canonical_sha256(right_buffer),
                embedding=tuple(right.embedding()),
                chroma_mean=right.chroma_mean,
                mfcc_mean=right.mfcc_mean,
                tempo=right.tempo,
                fingerprint=provider.compute(right_path, right_buffer, right),
            )
        ],
        fingerprint=provider.compute(left_path, left_buffer, left),
        duplicate_hash_owner=(
            right_path.stem
            if canonical_sha256(left_buffer) == canonical_sha256(right_buffer)
            else None
        ),
    )

    components = outcome.closest.components.as_dict() if outcome.closest else {}
    if args.json:
        print(
            json.dumps(
                {
                    "left": left_path.name,
                    "right": right_path.name,
                    "verdict": outcome.verdict.value,
                    "novelty": outcome.novelty_score,
                    "max_similarity": outcome.max_similarity,
                    "deciding_component": outcome.deciding_component,
                    "components": components,
                    "reasons": list(outcome.reasons),
                },
                indent=2,
            )
        )
        return 0

    print(f"\n  {left_path.name}  vs  {right_path.name}\n")
    _print_table(
        [
            ("verdict", outcome.verdict.value),
            ("novelty", f"{outcome.novelty_score:.3f}"),
            ("max similarity", f"{outcome.max_similarity:.3f}"),
            ("deciding component", outcome.deciding_component or "—"),
        ]
    )
    if components:
        print("\n  components")
        _print_table([(name, f"{value:.3f}") for name, value in components.items()])
    for reason in outcome.reasons:
        print(f"\n  - {reason}")
    print()
    return 0


# ------------------------------------------------------------------- master


async def _cmd_master(args: argparse.Namespace, settings: AppSettings) -> int:
    if not ffmpeg_available():
        raise SystemExit("error: FFmpeg is not on PATH; run `tradefix doctor` for the fix")
    source = _require_file(args.path)
    destination = Path(args.out) if args.out else source.with_name(f"{source.stem}.master.wav")
    result = master_track(source, destination, settings=settings.mastering)

    rows = [
        ("outcome", result.outcome.value),
        ("output", str(result.output_path) if result.output_path else "—"),
        ("target", f"{result.target_lufs:.2f} LUFS"),
        ("before", _lufs(result.measured_lufs_before)),
        ("after", _lufs(result.measured_lufs_after)),
        (
            "gain applied",
            "—" if result.gain_applied_db is None else f"{result.gain_applied_db:+.2f} dB",
        ),
        (
            "true peak",
            "—" if result.true_peak_dbtp is None else f"{result.true_peak_dbtp:.2f} dBTP",
        ),
        ("peak constrained", "yes" if result.peak_constrained else "no"),
        ("trimmed", f"{result.trimmed_start_seconds + result.trimmed_end_seconds:.2f} s"),
        ("dc removed", f"{result.dc_removed:.5f}"),
        ("elapsed", f"{result.elapsed_seconds:.2f} s"),
    ]
    if args.json:
        print(json.dumps({label.replace(" ", "_"): value for label, value in rows}, indent=2))
        return 0 if result.succeeded else 1
    print(f"\n  {source.name}\n")
    _print_table(rows)
    print(f"\n  {result.detail}\n")
    return 0 if result.succeeded else 1


def _lufs(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f} LUFS"


# ----------------------------------------------------------------- lyrics


async def _cmd_lyrics(args: argparse.Namespace, _settings: AppSettings) -> int:
    left = _require_file(args.left).read_text(encoding="utf-8")
    fingerprint = fingerprint_lyrics("left", left)
    if args.right is None:
        rows = [
            ("content hash", fingerprint.content_hash),
            ("words", str(fingerprint.word_count)),
            ("distinct word ratio", f"{fingerprint.unique_word_ratio:.2%}"),
            ("internal repetition", f"{fingerprint.internal_repetition:.2%}"),
            ("lines", str(len(fingerprint.line_hashes))),
            ("five-word shingles", str(len(fingerprint.shingles))),
        ]
        print(f"\n  {Path(args.left).name}\n")
        _print_table(rows)
        print()
        return 0

    other = fingerprint_lyrics("right", _require_file(args.right).read_text(encoding="utf-8"))
    comparison = compare_lyrics(fingerprint, other)
    print(f"\n  {Path(args.left).name}  vs  {Path(args.right).name}\n")
    _print_table(
        [
            ("score", f"{comparison.score:.3f}"),
            ("exact match", "yes" if comparison.exact_match else "no"),
            ("shared lines", f"{comparison.shared_line_ratio:.2%}"),
            ("shingle overlap", f"{comparison.shingle_jaccard:.2%}"),
            ("shared hook", "yes" if comparison.shared_hook else "no"),
            ("detail", comparison.detail),
        ]
    )
    print()
    return 0


# ---------------------------------------------------------------- registry

COMMANDS = {
    "analyze": _cmd_analyze,
    "qc": _cmd_qc,
    "fingerprint": _cmd_fingerprint,
    "compare": _cmd_compare,
    "master": _cmd_master,
    "lyrics": _cmd_lyrics,
}


def register(subparsers: object) -> None:
    """Add the §6.26 audio inspection subcommands."""
    add = subparsers.add_parser  # type: ignore[attr-defined]

    analyze = add("analyze", help="extract and print audio features (§6.2)")
    analyze.add_argument("path", help="audio file to analyse")
    analyze.add_argument("--json", action="store_true", help="machine-readable output")

    qc = add("qc", help="run audio QC and show every check (§6.1)")
    qc.add_argument("path", help="audio file to check")
    qc.add_argument(
        "--mastered",
        action="store_true",
        help="apply the stricter post-master thresholds instead of the raw ones",
    )
    qc.add_argument("--json", action="store_true", help="machine-readable output")

    fingerprint = add("fingerprint", help="canonical hash and perceptual fingerprint (§6.3, §6.4)")
    fingerprint.add_argument("path", help="audio file to fingerprint")
    fingerprint.add_argument("--json", action="store_true", help="machine-readable output")

    compare = add("compare", help="compare two files with the component breakdown (§6.5)")
    compare.add_argument("left", help="candidate audio file")
    compare.add_argument("right", help="existing audio file to compare against")
    compare.add_argument("--json", action="store_true", help="machine-readable output")

    master = add("master", help="master one file to the canonical format (§6.9, §6.11)")
    master.add_argument("path", help="audio file to master")
    master.add_argument("--out", default=None, help="output path (default: <name>.master.wav)")
    master.add_argument("--json", action="store_true", help="machine-readable output")

    lyrics = add("lyrics", help="lyric fingerprint, or compare two lyrics (§6.8)")
    lyrics.add_argument("left", help="lyric text file")
    lyrics.add_argument("right", nargs="?", default=None, help="second lyric to compare against")
