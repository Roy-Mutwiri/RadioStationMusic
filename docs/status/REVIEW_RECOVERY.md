# REVIEW recovery — resolving the 120

Every REVIEW candidate in the corpus, replayed through `ReviewResolver` and
reported by the evidence that decided it. A high approval rate means nothing on
its own here: this corpus was measured to contain no duplicates, so the number to
check is *why*, not *how many*.

## First-stage verdicts

| verdict | count |
|---|---|
| approve | 10 |
| review | 120 |
| reject | 15 |

## Resolution of the 120 REVIEW candidates

* FINAL_APPROVE: **120**
* FINAL_REJECT: **0**

### Why

| evidence class | count | meaning |
|---|---|---|
| style_only | 120 | similarity is timbre/tempo/genre; no recording-level evidence |

### Which component sent them to REVIEW in the first place

| driver | count | share of reviews |
|---|---|---|
| mfcc | 73 | 61% |
| tempo | 47 | 39% |

### Approved as style-only, by what had flagged them

| flagged by | approved |
|---|---|
| mfcc | 73 |
| tempo | 47 |

### The two scores, separated

* duplication risk: mean **0.533**, max **0.613**
* creative similarity: mean **0.987**, max **1.000**

That gap is the finding. These candidates are musically close and are not the
same recordings, and a single scalar could not say both.
