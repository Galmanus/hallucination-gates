# grounding.py: measured results

Run on 2026-09-28. Four versions of the matcher are compared on the same data:

| label | what it is | file |
|---|---|---|
| **v1** | the original substring matcher | `baselines/grounding_v1.py` |
| **v2.1** | the typed-fact matcher committed at `0d498fc`; this is the version the author's production stop-hook ran on the day | `baselines/grounding_v21.py` |
| **v2.3-pre** | v2.3 as it was before a research pass found three leaks in it | `baselines/grounding_v23pre.py` |
| **v2.4** | the current `grounding.py`, with 92 selftests | `grounding.py` |

Rates carry Wilson 95% intervals. Seed 42.

**Corpus.** 2,711 real agent turns, dated 2026-08-26 to 2026-09-28, from 1,337 Claude Code transcripts on the author's machine. 1,269 of the turns come from the Wave agent and 1,403 from interactive sessions. The corpus is private and not committed, and it is a snapshot (see `README.md`).

## Summary

**Synthetic injection into real tool output** (`synth.py`). Every row is the share of injected tokens the gate got wrong.

| injected into real ledgers | n | v1 | v2.1 | v2.3-pre | **v2.4** |
|---|---|---|---|---|---|
| fabricated token, all 14 shapes | 19,110 | 43.7% | 30.7% | 5.1% | **0.01%** [0.00, 0.03] |
| fabricated number, date, time, IP, hash (8 shapes) | 9,629 | 11.7% | 0.15% | 0.00% | **0.00%** [0.00, 0.04] |
| fabricated value with magnitude suffix (`R$ 412 mil`, `R$ 412K`, `US$ 25M`) | 4,735 | 52.3% | 23.3% | 20.7% | **0.02%** [0.00, 0.12] |
| fabricated receipt (`0x…` hash, Stellar strkey, base58) | 4,746 | 100% | 100% | 0.00% | **0.00%** [0.00, 0.08] |
| real percent with its sign flipped (`5%` → `-5%`) | 378 | 84.9% | 84.9% | 100% | **1.1%** [0.4, 2.7] |
| fabricated token on a line with a formerly exempting word (`convertido`, `soma`, `range`, `derivado`) | 2,385 | 33.5% | 52.4% | 100% | **0.00%** [0.00, 0.16] |
| composed ratio (both operands real, the ratio is not) | 1,512 | 10.3% | 0.07% | 0.00% | **0.00%** [0.00, 0.25] |
| shown sum with the wrong result | 1,081 | 33.4% | 0.74% | 0.00% | **0.00%** [0.00, 0.35] |
| shown product with a fabricated factor | 607 | 4.0% | 0.00% | 0.00% | **0.00%** [0.00, 0.63] |
| **correct** shown sum, wrongly blocked | 1,254 | 54.2% | 84.3% | 84.7% | **0.00%** [0.00, 0.31] |
| **real** token copied from the ledger, wrongly blocked | 2,926 | 2.0% | 2.5% | 0.10% | **0.10%** [0.03, 0.30] |

**Replay on real drafts** (`realrun.py`, 2,711 turns):

| | v1 | v2.1 | v2.3-pre | **v2.4** |
|---|---|---|---|---|
| tokens flagged | 1,755 | 2,408 | 1,893 | **2,292** |
| turns blocked | 738 | 895 | 782 | **824** |
| flags whose value was verbatim in this turn's ledger (gate error) | n/a | 40 | 4 | **4** |

**Why v2.4 flags more than v2.3-pre on real drafts (+399).** v2.4 sees money it could not see before:
- **289 of the extra flags** are bare `$`, `USD`, `BRL` or `EUR` amounts that used to be invisible.
- **48** are suffixed values that v2.3-pre let through by colliding with their bare base. Those were leaks, and the new flags are correct.
- **21** come from soft hedges that the production hook already refuses to count as declarations.
- **The rest** are lines that used to be exempted by a word.

v2.4 still blocks 71 fewer turns than v2.1, the version that was running in production.

## 1. How the synthetic benchmark works, and what it got wrong along the way

**The two token classes.** For every turn, the gate is given the ledger that turn really produced:
- **Fabricated** tokens pass a strict absence test that does not use the gate.
- **Grounded** tokens are copied from the ledger as whole tokens.

**The probes:**
- **Sign flips** take a real positive percentage from the ledger and assert it as negative.
- **Exempt probes** put a fabricated token on a line containing a word that used to exempt the whole line.
- **Derived probes** build `R$ a + R$ b = R$ c` from real ledger integers. The sum is either right or wrong, and a laundering variant multiplies by a fabricated factor.

The benchmark itself was wrong three times during this work. Every correction is disclosed here, and each was applied identically to all versions:

1. **Loose positives.** A first run sliced 43-88-character "base58" windows out of base64 blobs, which gave v2.2 an 85% false-block rate on base58. All 208 of those were artifacts. Positives are now taken as whole tokens only.
2. **A blind spot that hid a real leak.** For suffixed fabrications, the absence test first required that even the bare base (`412`) be absent from the ledger. That excluded exactly the collision case, and the benchmark reported 0.00% while v2.3 was letting `R$ 412 mil` ground on `rows 412`. The leak was found by the adversarial research pass, not by this benchmark. The test now requires only that the scaled value and its rounding window be absent.
3. **Operands inside other tokens.** Derived probes first picked operands from any digit run, including `38` inside `10:38`, and blamed the gate for refusing them. Operands are now standalone integers.

**Remaining v2.4 errors in the final run.** Every one is explained:
- **Sign flips (4).** They ground on a real negative raw number elsewhere in the ledger, for example `(-1,-1)` or a `-2` field. This is the documented leniency that lets a unit-less tool value ground a percent.
- **`US$ 25M` (1).** It grounds by rounding on a ledger `25.3M`, which the absence test does not model.
- **False blocks (3).** Two are `0.22.04.x` cut out of an Ubuntu version and read as an IP. The third is a ratio still being inspected.

**What changed, v2.1 → v2.4.** Each change is pinned by a selftest:

| traced error | fix |
|---|---|
| `2.0` grounded `20%`; `125` grounded `12.5%` | locale-aware VALUES (pt-BR and en), not flattened digits |
| `5%` blocked against `5%` | single-digit values ground, but only against the same type |
| values glued to identifiers (`U_e2629…`, `…_03:12`, `…T10:55:00Z`, `3375:06:09:18`) not extracted | glue-tolerant boundaries on the ledger side |
| `0x…`, Stellar G/C strkeys and base58 signatures invisible | structural receipt tokens |
| `28/09` blocked against `2026-09-28`, `2026/09/28` or `set 21` | ISO, path, month-name (PT/EN) and US-order dates normalized to one calendar key |
| `R$296K` blocked against `R$296,332`; `R$5M` not extracted | suffixes: exact scaled value, or rounding at the precision shown, never the reverse |
| **`R$ 412 mil` grounded on a bare `412` (v2.3 leak)** | the draft asserts only the scaled value |
| **`-5%` grounded on `5%` (sign ignored since v1)** | signed values; an unsigned draft still grounds on a signed source |
| **`convertido`, `soma`, `range` or `derivado` anywhere on a line exempted everything on it** | explicit declarations only (`INFERRED`/`INFERIDO`, `RECALL`, `não verificado`, `estimate`, `±`, a numeric `banda`) |
| derived values blocked, or exempted by a word | shown arithmetic is RECOMPUTED: every operand must be grounded or a unit constant, and the result must hold at the precision shown |
| bare `$` / USD / BRL / EUR amounts invisible | extracted, except `$1`-style shell arguments |
| `PR-0057` grounded `57%`; a UUID fragment `-579c` backtracked into `-57` | zero-padded integers are identifiers; signed numbers are never glued to letters or digits |
| `31/12/2026` also produced ratio `31/12`; a CNPJ or version list produced ratios; `11/4/1/1` produced a date | span rules in extraction |
| `226 of 100` grounded on `100 of 226` | ratios are ordered |

## 2. Replay on real drafts: what a flag means

Each turn's first draft is checked against that turn's tool output, the same slice the production stop-hook sees. A flag means "this token did not trace to this turn's tool output". It does not mean "this token is false".

**Where each v2.4-flagged token came from.** This uses the corpus rebuild that separates prior tool output (2,630 turns, 2,209 flags):

| provenance | share |
|---|---|
| verbatim in this turn's ledger (gate error) | 0.2% |
| earlier **tool output** in the same session: sourced, but stale | 13.9% |
| earlier prompt or assistant text in the session | 26.0% |
| static context (memory files, CLAUDE.md, system prompt) | 25.7% |
| found nowhere (orphan) | 34.2% |

**Hand labels on two 80-token orphan samples.** One sample was drawn from each version's run. The labels came from Claude, the same family as the agent, and are provisional until a human pass:

| label | v2.1 orphans | v2.3 orphans |
|---|---|---|
| unsourced factual assertion (the class the gate exists for) | 31.2% [22.2, 42.1] | **47.5%** [36.9, 58.3] |
| not a claim, or a parse artifact | 31.2% [22.2, 42.1] | 23.8% [15.8, 34.1] |
| derived arithmetic | 21.2% [13.7, 31.4] | 15.0% [8.8, 24.4] |
| forecast or proposal | 15.0% [8.8, 24.4] | 12.5% [6.9, 21.5] |
| declared uncertain, but unrecognized | 1.2% [0.2, 6.7] | 1.2% [0.2, 6.7] |

Some flags cannot be fixed by parsing:
- values the agent read from a screenshot are real observations, but they appear in no text ledger;
- well-known constants such as `169.254.169.254` or the BN254 modulus prefix are correct, but unsourced.

## 3. What the agent does after a block (`postblock.py`)

Over 288 production blocks covering 738 flagged tokens, under v2.1 and the current hook:

| outcome | share |
|---|---|
| grounded with a new tool call | 11.9% |
| kept, with the source or uncertainty declared | 30.4% |
| not repeated in the revision | 29.7% |
| **kept bare** | **28.0%** |

The production hook allows one forced revision and never checks it. A revision-checking hook is staged but not applied, because it changes a live agent's runtime.

## Predictions registered before the runs, scored

1. *"v1 passes more than 40% of fabricated 2-digit numbers on ledgers above 500 digits; v2 passes less than 5%."* **Confirmed.** v1: 56.5% at 100-999 digits and 94.2% at 1,000-9,999. v2.1: 0.7% and 2.3%.
2. *"Precision on real blocks is below 50%: most blocks are true-but-unsourced, not fabrication."* **Confirmed.** About 66% of flags are carried context, and about 16% are unsourced factual assertions (v2.3 orphan share × orphan labels).
3. *(research pass)* *"After closing the suffix leak, the suffix probe falls from 13.2% to below 0.5%, and false blocks on suffixed tokens stay below 1%."* **Confirmed** on the corrected probe: 20.7% → 0.02%, with overall false blocks unchanged at 0.10%.

## What this does not measure

- **Whether a flagged assertion was actually false.** That needs a truth label per claim. Unsourced is not the same as wrong.
- **Recall on the model's own fabrications.** The synthetic set measures the matcher against a fabrication generator, not the model's error distribution. "0.01%" means that almost no fabricated token of these 14 shapes got through. It does not mean that almost no hallucination gets through.
- **Anything outside the gate's vocabulary.** Non-numeric claims (names, causal claims, "I ran the tests", "nothing found") are not danger tokens, and they pass untouched.
- **`selfcheck`, `corroborate`, `repro` and `claim`.**
- **Independent labels.** The labels came from the same model family as the agent.
