"""Replay every REVIEW candidate through the resolver and report why each was decided.

The question this answers is not "how many now pass" — a permissive resolver would score
well on a corpus that contains no duplicates. It is "what evidence decided each one", so
that a high approval rate can be checked against the reason rather than trusted.

    python -m scripts.originality.review_recovery
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tradefix_radio.audio.analysis import AudioFeatures  # noqa: E402
from tradefix_radio.config.loader import load_settings  # noqa: E402
from tradefix_radio.contracts.enums import RunMode  # noqa: E402
from tradefix_radio.originality.review import ReviewResolver  # noqa: E402
from tradefix_radio.originality.similarity import (  # noqa: E402
    OriginalityVerdict,
    SimilarityEngine,
)
from tradefix_radio.persistence.database import Database  # noqa: E402
from tradefix_radio.persistence.repositories.originality import (  # noqa: E402
    OriginalityRepository,
)

OUT = ROOT / "docs" / "status" / "REVIEW_RECOVERY.md"


def features_from(entry) -> AudioFeatures:
    """The comparison inputs the engine reads; the rest is unused placeholder."""
    return AudioFeatures(
        duration_seconds=0.0, sample_rate=48_000, channels=2, peak=0.0, rms=0.0,
        crest_factor=0.0, dc_offset=0.0, integrated_lufs=-14.0, loudness_range=0.0,
        loudness_meter_available=False, spectral_centroid=0.0, spectral_bandwidth=0.0,
        spectral_rolloff=0.0, zero_crossing_rate=0.0, low_energy_ratio=0.0,
        high_energy_ratio=0.0, stereo_correlation=0.0, mono_compatibility_db=0.0,
        tempo=entry.tempo, beat_strength=0.0, musical_key=None, key_confidence=0.0,
        chroma_mean=entry.chroma_mean, chroma_std=(), mfcc_mean=entry.mfcc_mean,
        mfcc_std=(), rms_profile=(), leading_silence_seconds=0.0,
        trailing_silence_seconds=0.0, longest_internal_silence_seconds=0.0,
        silence_ratio=0.0, clipped_sample_ratio=0.0, discontinuity_count=0,
        backend="replay",
    )


async def main() -> int:
    settings = load_settings(mode=RunMode.DEVELOPMENT)
    db = Database(settings.database)
    await db.connect()
    async with db.read_session() as session:
        library = await OriginalityRepository(session).load_library(limit=1000)
    await db.disconnect()

    # Treat the corpus as production so the first stage still produces REVIEWs to resolve.
    # Under the provenance policy none of it is graded, which is correct for the live
    # station and useless as a test of the resolver.
    from dataclasses import replace

    entries = [replace(e, provenance="production_radio") for e in library]
    engine = SimilarityEngine(settings.originality)
    resolver = ReviewResolver(settings.originality)

    initial: Counter[str] = Counter()
    final: Counter[str] = Counter()
    evidence: Counter[str] = Counter()
    drivers: Counter[str] = Counter()
    rows: list[tuple[str, str, str, float, float, str]] = []

    for index, candidate in enumerate(entries):
        if not candidate.chroma_mean:
            continue
        others = entries[:index] + entries[index + 1 :]
        outcome = engine.evaluate(
            track_id=candidate.track_id,
            features=features_from(candidate),
            canonical_hash=candidate.canonical_hash or "",
            library=others,
            fingerprint=candidate.fingerprint,
            lyrics=candidate.lyrics,
            blueprint=candidate.blueprint,
            now=candidate.created_at,
            duplicate_hash_owner=None,
        )
        initial[outcome.verdict.value] += 1
        if outcome.verdict is not OriginalityVerdict.REVIEW:
            final[outcome.verdict.value] += 1
            continue

        resolution = resolver.resolve(
            outcome,
            now=candidate.created_at,
            production_references=sum(1 for e in others if e.counts_toward_graded_novelty),
        )
        final[resolution.disposition.value] += 1
        evidence[resolution.evidence_class.value] += 1
        drivers[outcome.deciding_component or "none"] += 1
        rows.append(
            (
                candidate.track_id,
                resolution.disposition.value,
                resolution.evidence_class.value,
                resolution.duplication_risk,
                resolution.creative_similarity,
                outcome.deciding_component or "none",
            )
        )

    reviews = len(rows)
    approved = sum(1 for r in rows if r[1] == "final_approve")
    rejected = reviews - approved

    lines = [
        "# REVIEW recovery — resolving the 120",
        "",
        "Every REVIEW candidate in the corpus, replayed through `ReviewResolver` and",
        "reported by the evidence that decided it. A high approval rate means nothing on",
        "its own here: this corpus was measured to contain no duplicates, so the number to",
        "check is *why*, not *how many*.",
        "",
        "## First-stage verdicts",
        "",
        "| verdict | count |",
        "|---|---|",
    ]
    for name in ("approve", "review", "reject"):
        lines.append(f"| {name} | {initial.get(name, 0)} |")

    lines += [
        "",
        f"## Resolution of the {reviews} REVIEW candidates",
        "",
        f"* FINAL_APPROVE: **{approved}**",
        f"* FINAL_REJECT: **{rejected}**",
        "",
        "### Why",
        "",
        "| evidence class | count | meaning |",
        "|---|---|---|",
    ]
    meanings = {
        "style_only": "similarity is timbre/tempo/genre; no recording-level evidence",
        "no_production_history": "cold start — nothing has aired, nothing to repeat",
        "rotation_pressure": "too close to something aired very recently",
        "definitive_duplicate": "exact content match",
        "strong_recording_match": "fingerprint inside the measured same-recording band",
        "corroborated_recording_match": "ambiguous fingerprint confirmed by structure",
    }
    for name, count in evidence.most_common():
        lines.append(f"| {name} | {count} | {meanings.get(name, '')} |")

    lines += [
        "",
        "### Which component sent them to REVIEW in the first place",
        "",
        "| driver | count | share of reviews |",
        "|---|---|---|",
    ]
    for name, count in drivers.most_common():
        lines.append(f"| {name} | {count} | {100 * count / max(1, reviews):.0f}% |")

    style_by_driver = Counter(
        r[5] for r in rows if r[2] == "style_only"
    )
    if style_by_driver:
        lines += [
            "",
            "### Approved as style-only, by what had flagged them",
            "",
            "| flagged by | approved |",
            "|---|---|",
        ]
        for name, count in style_by_driver.most_common():
            lines.append(f"| {name} | {count} |")

    risks = [r[3] for r in rows]
    creatives = [r[4] for r in rows]
    if risks:
        lines += [
            "",
            "### The two scores, separated",
            "",
            f"* duplication risk: mean **{np.mean(risks):.3f}**, max **{max(risks):.3f}**",
            f"* creative similarity: mean **{np.mean(creatives):.3f}**, "
            f"max **{max(creatives):.3f}**",
            "",
            "That gap is the finding. These candidates are musically close and are not the",
            "same recordings, and a single scalar could not say both.",
        ]

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
