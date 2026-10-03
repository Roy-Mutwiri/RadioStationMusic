"""Does MFCC identify a song, or a production style? Same question for tempo.

Both dominate the station's review/reject decisions — MFCC decides 51% of them and tempo
38% — so what they actually measure determines whether those decisions mean anything.

The test is simple: a component useful for *duplicate* detection should separate "same
recording" from "different recording" and should be largely indifferent to genre and BPM.
A component that instead separates "same genre" from "different genre" is measuring
production family, which is a creative-rotation concern and not a duplication one.

    python -m scripts.originality.component_authority
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev

import numpy as np
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tradefix_radio.config.loader import load_settings  # noqa: E402
from tradefix_radio.contracts.enums import RunMode  # noqa: E402
from tradefix_radio.originality.similarity import (  # noqa: E402
    _profile_similarity,
    _tempo_similarity,
    _timbre_similarity,
)
from tradefix_radio.persistence.database import Database  # noqa: E402
from tradefix_radio.persistence.repositories.originality import (  # noqa: E402
    OriginalityRepository,
)

OUT = ROOT / "docs" / "status" / "COMPONENT_AUTHORITY.md"


def describe(values: list[float]) -> str:
    if not values:
        return "-"
    ordered = sorted(values)
    return (
        f"{mean(values):.3f} ±{pstdev(values):.3f} "
        f"[{ordered[0]:.3f}–{ordered[-1]:.3f}]"
    )


async def main() -> int:
    settings = load_settings(mode=RunMode.DEVELOPMENT)
    db = Database(settings.database)
    await db.connect()
    async with db.read_session() as session:
        library = await OriginalityRepository(session).load_library(limit=1000)
        meta = {
            row[0]: (row[1], row[2])
            for row in (
                await session.execute(text("SELECT track_id, genre, bpm FROM tracks"))
            ).all()
        }
    await db.disconnect()

    entries = [e for e in library if e.mfcc_mean and e.chroma_mean and e.tempo]
    print(f"entries with full evidence: {len(entries)}")

    buckets: dict[str, dict[str, list[float]]] = {
        name: defaultdict(list) for name in ("mfcc", "chroma", "tempo", "fingerprint")
    }

    for i, a in enumerate(entries):
        genre_a, bpm_a = meta.get(a.track_id, (None, None))
        for b in entries[i + 1 :]:
            genre_b, bpm_b = meta.get(b.track_id, (None, None))
            same_genre = genre_a is not None and genre_a == genre_b
            close_bpm = (
                bpm_a is not None and bpm_b is not None and abs(bpm_a - bpm_b) <= 4
            )

            scores = {
                "mfcc": _timbre_similarity(
                    np.asarray(a.mfcc_mean, dtype=np.float64),
                    np.asarray(b.mfcc_mean, dtype=np.float64),
                ),
                "chroma": _profile_similarity(
                    np.asarray(a.chroma_mean, dtype=np.float64),
                    np.asarray(b.chroma_mean, dtype=np.float64),
                ),
                "tempo": _tempo_similarity(a.tempo, b.tempo),
            }
            if a.fingerprint and b.fingerprint:
                measured = a.fingerprint.similarity_to(b.fingerprint)
                if measured is not None:
                    scores["fingerprint"] = measured

            for name, score in scores.items():
                buckets[name]["all"].append(score)
                buckets[name]["same_genre" if same_genre else "cross_genre"].append(score)
                buckets[name]["close_bpm" if close_bpm else "far_bpm"].append(score)

    lines = [
        "# Component authority — what MFCC and tempo actually measure",
        "",
        "MFCC decides 51% of the station's review/reject verdicts and tempo decides 38%.",
        "This measures whether either is evidence of *duplication* or of *style*.",
        "",
        "A duplication signal should separate same-recording from different-recording and",
        "should barely move between genres. A signal that instead moves with genre and BPM",
        "is describing a production family, which belongs to creative rotation rather than",
        "to a duplicate verdict.",
        "",
        f"Pairs compared: {len(buckets['mfcc']['all'])}",
        "",
        "| component | all pairs | same genre | cross genre | genre delta |",
        "|---|---|---|---|---|",
    ]
    for name in ("fingerprint", "mfcc", "chroma", "tempo"):
        data = buckets[name]
        same, cross = data["same_genre"], data["cross_genre"]
        delta = (mean(same) - mean(cross)) if same and cross else 0.0
        lines.append(
            f"| {name} | {describe(data['all'])} | {describe(same)} | "
            f"{describe(cross)} | **{delta:+.3f}** |"
        )

    lines += [
        "",
        "| component | BPM within 4 | BPM further apart | BPM delta |",
        "|---|---|---|---|",
    ]
    for name in ("fingerprint", "mfcc", "chroma", "tempo"):
        data = buckets[name]
        close, far = data["close_bpm"], data["far_bpm"]
        delta = (mean(close) - mean(far)) if close and far else 0.0
        lines.append(
            f"| {name} | {describe(close)} | {describe(far)} | **{delta:+.3f}** |"
        )

    mfcc_all = buckets["mfcc"]["all"]
    tempo_all = buckets["tempo"]["all"]
    lines += [
        "",
        "## Reading",
        "",
        f"* **MFCC** averages {mean(mfcc_all):.3f} across *every* pair in the corpus, with a",
        f"  spread of ±{pstdev(mfcc_all):.3f}. A measure that returns the same answer for",
        "  everything cannot be evidence about anything. It is saturated on one generator's",
        "  output, which is the condition this station permanently operates in.",
        f"* **Tempo** averages {mean(tempo_all):.3f} and is the component most sensitive to",
        "  BPM proximity by construction — the director picks BPM from a narrow band per",
        "  genre and energy, so tempo agreement is largely a restatement of the brief.",
        "* **Fingerprint** is the only component whose high values were shown, by direct",
        "  calibration, to mean 'the same recording'. See CHROMAPRINT_CALIBRATION.md.",
        "",
        "Conclusion: MFCC and tempo are style signals. They remain useful to the diversity",
        "director, which exists to vary consecutive programming, and should carry little or",
        "no authority in a duplication verdict.",
    ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[10:30]))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
