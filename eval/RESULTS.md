# grounding.py and action_gate.py: measured results

Run on 2026-09-28 (sections 5 and 6 on 2026-09-28/29). Six versions of the matcher are compared on the same data:

| label | what it is | file |
|---|---|---|
| **v1** | the original substring matcher | `baselines/grounding_v1.py` |
| **v2.1** | the typed-fact matcher committed at `0d498fc`; this is the version the author's production stop-hook ran on the day | `baselines/grounding_v21.py` |
| **v2.3-pre** | v2.3 as it was before a research pass found three leaks in it | `baselines/grounding_v23pre.py` |
| **v2.4** | commit `33d583e`, 92 selftests: the version published first | `baselines/grounding_v24.py` |
| **v2.5** | commit `b450533`, 109 selftests. Its extra fixes came from an adversarial review of `action_gate.py` | `baselines/grounding_v25.py` |
| **v2.6** | the current `grounding.py`, 137 selftests: v2.5 plus the look-elsewhere test of section 5 and a Claude Code Stop hook mode (`--hook`). Sections 1 to 4 were measured on v2.5 | `grounding.py` |

Rates carry Wilson 95% intervals. Seed 42.

**Corpus.** 2,711 real agent turns, dated 2026-08-26 to 2026-09-28, from 1,337 Claude Code transcripts on the author's machine. 1,269 of the turns come from the Wave agent, 1,403 from interactive sessions and 39 from other project directories. The corpus is private and not committed, and it is a snapshot (see `README.md`).

## Summary

**Synthetic injection into real tool output** (`synth.py`). Every row is the share of injected tokens the gate got wrong.

| injected into real ledgers | n | v1 | v2.1 | v2.3-pre | v2.4 | **v2.5** |
|---|---|---|---|---|---|---|
| fabricated token, all 14 shapes | 19,110 | 43.7% | 30.7% | 5.1% | 0.01% | **0.01%** [0.00, 0.04] |
| fabricated number, date, time, IP, hash (8 shapes) | 9,629 | 11.7% | 0.15% | 0.00% | 0.00% | **0.01%** [0.00, 0.06] |
| fabricated value with magnitude suffix (`R$ 412 mil`, `R$ 412K`, `US$ 25M`) | 4,735 | 52.3% | 23.3% | 20.7% | 0.02% | **0.02%** [0.00, 0.12] |
| fabricated receipt (`0x…` hash, Stellar strkey, base58) | 4,746 | 100% | 100% | 0.00% | 0.00% | **0.00%** [0.00, 0.08] |
| real percent with its sign flipped (`5%` → `-5%`) | 378 | 84.9% | 84.9% | 100% | 1.1% | **1.1%** [0.4, 2.7] |
| fabricated token on a line with a formerly exempting word (`convertido`, `soma`, `range`, `derivado`) | 2,385 | 33.5% | 52.4% | 100% | 0.00% | **0.04%** [0.01, 0.24] |
| composed ratio (both operands real, the ratio is not) | 1,512 | 10.3% | 0.07% | 0.00% | 0.00% | **0.00%** [0.00, 0.25] |
| shown sum with the wrong result | 1,081 | 33.4% | 0.74% | 0.00% | 0.00% | **0.00%** [0.00, 0.35] |
| shown product with a fabricated factor | 607 | 4.0% | 0.00% | 0.00% | 0.00% | **0.00%** [0.00, 0.63] |
| **correct** shown sum, wrongly blocked | 1,254 | 54.2% | 84.3% | 84.7% | 0.00% | **0.00%** [0.00, 0.31] |
| **real** token copied from the ledger, wrongly blocked | 2,926 | 2.0% | 2.5% | 0.10% | 0.10% | **0.10%** [0.03, 0.30] |

**Replay on real drafts** (`realrun.py`, 2,711 turns):

| | v1 | v2.1 | v2.3-pre | v2.4 | **v2.5** |
|---|---|---|---|---|---|
| tokens flagged | 1,755 | 2,408 | 1,893 | 2,292 | **2,374** |
| turns blocked | 738 | 895 | 782 | 824 | **850** |
| flags whose value was verbatim in this turn's ledger (gate error) | n/a | 40 | 4 | 4 | **4** |

**Why v2.5 flags more than v2.4 (+82 tokens, +26 turns).** It reads ISO and month-name dates in the draft, which v2.4 could not see. Coverage rose; the gate-error count did not move.

**Why v2.4 flags more than v2.3-pre on real drafts (+399).** v2.4 sees money it could not see before:
- **289 of the extra flags** are bare `$`, `USD`, `BRL` or `EUR` amounts that used to be invisible.
- **48** are suffixed values that v2.3-pre let through by colliding with their bare base. Those were leaks, and the new flags are correct.
- **21** come from soft hedges that the production hook already refuses to count as declarations.
- **The rest** are lines that used to be exempted by a word.

v2.4 still blocks 71 fewer turns than v2.1, the version that was running in production, and v2.5 45 fewer.

**The 0.01% above excludes coincidence by construction.** `synth.py` only injects values whose digits are absent from the ledger. Section 5 measures what it cannot: an invented value that happens to be present. On v2.5 an invented integer percent passed as sourced on 71.04% of 100k-character ledgers; v2.6 cuts that to 10.21%.

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
- **v2.5 adds two accepted fabrications** (`22%`, `82%`). Both ground by the new percent rounding on a more precise printed percent the absence test does not model (a ledger `21.6%` rounds to `22%`). This is the price of accepting `87%` for a printed `87.38%`, and it is disclosed rather than tuned away.

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
| *(v2.5)* ISO and month-name dates in the draft invisible; `T15:00:00-03:00` read as the times `00:00` and `03:00` | draft-side date kinds; an ISO datetime is one claim (date + HH:MM) |
| *(v2.5)* `8491,1200.00` (a CSV row) read as 84911200 | a malformed thousands grouping is a list of separate numbers |
| *(v2.5)* `87%` blocked against a printed `87.38%`; `90/90` blocked against `Tests 90 passed (90)` | percent rounding (never the reverse); test-runner summaries are ratios |
| *(v2.5)* `2026-13-45` (an error-message example) flagged as a date; `I set 5 retries` read as a date | impossible dates and prose verbs are not claims |

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

The production hook allowed one forced revision and never checked it. A revision-checking hook (second block if a value comes back bare, an `escape` metric at the cap, an `amplified` metric for new unsourced values) went into production on 2026-09-28 13:33, with the v2.4 matcher. Registered prediction, checked on 2026-10-12 by re-running `postblock.py`: bare-after-block falls from 28.0% to below 10%.

## 4. `action_gate.py` on real tool calls (`actions.py`)

`grounding.py` gates what an agent says. `action_gate.py` gates what it does: it runs before a side-effecting tool call and denies it when a value in the payload (amount, address, hash, date, identifier, recipient) traces to nothing the user said or a tool returned. `actions.py` replays every real tool call in the corpus in order; for each side-effecting one, the gate decides with only the evidence that existed at that moment.

| | v1.0 | **v1.1** |
|---|---|---|
| side-effecting calls judged | 640 | 709 |
| allowed | 55.6% | **96.6%** [95.0, 97.7] |
| asked for human confirmation | 0.8% | 3.2% [2.2, 4.8] |
| denied | 43.6% | **0.14%** [0.02, 0.79] (1 call) |
| one-character mutation of an allowed value, caught | 89.1% [83.7, 92.8] (n=183) | **100%** [98.9, 100] (n=329) |
| latency p50 / p95 per decision | 7 / 374 ms | 223 / 378 ms |

Real calls were mostly legitimate, so deny + ask is an upper bound on friction, not a precision. The 709 vs 640 difference is classification: v1.1 gates tools v1.0 missed (unlisted MCP verbs, `git -C push`, `stellar -q`, remote `ssh`) and stops gating reads that POST (JSON-RPC `getTransaction`, `eth_call`, GraphQL queries) and agent-to-agent messages.

**How v1.0 became v1.1.**
- **Replay.** v1.0 checked every token in a shell command. Its 279 denials were mostly hashed asset file names in `rsync dist/` paths. v1.1 checks the payload only: HTTP bodies and query strings, chain-CLI flags and positional amounts, message bodies, SQL, and for transports only the remote host.
- **Adversarial review.** Three independent hunters (bypass, false block, classification) reported 29 executed cases; a verifier reproduced all 29 and confirmed 27 as real. Among them:
  - the gate's own deny message grounded the retry of the same call;
  - web content relayed through a subagent counted as trusted;
  - an `echo` or a Write-then-Read laundered the agent's own value;
  - the correct Stellar amount in stroops was denied while the wrong-unit amount was allowed;
  - 17 MCP write verbs were not gated at all.

  Each is now a selftest (100 in total).

**The remaining non-allows.**
- **The one denial** is a connector UUID that appears nowhere in the session transcript; it most likely came from system context.
- **Most of the 23 asks** come from URLs built at run time (`"$U/auth/v1/signup"` with `U=$(cat file)`; 20 such URL values), which the gate cannot see.

**What v1.1 does not do:** it checks values, not intent; a value the agent computes in a tool call without echoing it literally counts as tool output; exfiltrating a secret the agent legitimately read is not a provenance violation; and side-effect detection is a vocabulary, so an unknown shell program that writes remotely is not gated.

## 5. Coincidence in large ledgers (`coincidence.py`, v2.6)

`synth.py` never tests an invented value that happens to be in the ledger. That case is the common one on big turns: v2.5 lets a percent or a currency ground on a unit-less ledger number of the same value (tools print `"port": 8443`), so an invented `37%` grounds on any bare `37` anywhere in the turn's tool output. The idea of measuring it came from the README of [laya](https://github.com/NandhaKishorM/laya), which warns that a maximum over sliding windows drifts up with the number of windows even with no signal. The statistical name is the look-elsewhere effect.

**Method.** Real ledgers from the corpus are concatenated to five controlled sizes (1k, 10k, 100k, 1M and 4M characters), 12 replicates each. Into each, 40 random values of each shape are checked, with no absence filter, so every acceptance is a coincidence. Two controls: `copied` takes a danger token verbatim from the ledger and gives it a random label; `copied_ctx` gives it the nearest word before it in the ledger, which is how a real answer quotes a value. Seed 11, n = 480 per random cell. Uniform draws are a lower bound: a real fabrication tends to sit near real values of its context.

**v2.6.** For each ledger match, the gate computes the share of that value's shape space the ledger already covers with distinct facts of the same kind (2-digit integers: 90 values; times: 1,440; dates: 366). Above `ALPHA = 0.10`, the match needs an anchor: a word of 4+ letters or a number of 3+ digits from the same draft line within 240 characters of an occurrence of the value. Without one the verdict is `COINCIDENT` (severity 1, blocks). Ordered ratios are exempt; they already need both operands in order.

| invented value accepted as sourced | ledger 10k | 100k | 1M | 4M |
|---|--:|--:|--:|--:|
| integer percent, v2.5 path | 12.08% | 71.04% | 91.25% | 93.12% |
| integer percent, **v2.6** | **3.54%** [2.22, 5.60] | **10.21%** [7.81, 13.24] | **24.38%** [20.75, 28.41] | **47.29%** [42.86, 51.76] |
| decimal percent, v2.5 → v2.6 | 0.83% → 0.42% | 6.67% → 1.25% | 10.62% → 3.12% | 15.42% → 7.29% |
| `dd/mm` date, v2.5 → v2.6 | 1.46% → 1.46% | 7.50% → 1.67% | 12.92% → 3.12% | 32.50% → 2.08% |
| ISO date, v2.5 → v2.6 | 1.04% → 1.04% | 4.17% → 0.62% | 6.88% → 0.83% | 22.50% → 1.46% |
| time, v2.5 → v2.6 | 0.42% → 0.42% | 1.25% → 1.25% | 0.62% → 0.62% | 8.96% → 4.38% |
| `R$` amount with cents, 8-hex and 64-hex hashes | 0% | 0% | 0% | 0% |
| **legit copy with its label** (`copied_ctx`), v2.6 | 98.84% | 100% | 100% | 99.79% |
| legit copy with a random label (`copied`), v2.6 | 99.71% | 84.17% | 89.79% | 70.00% |

The v2.5 path is the same code with the look-elsewhere test off. `4.812`, `17 testes` and `238 ms` are not danger tokens at all and were not scored.

**Weighted by real ledger sizes.** Non-empty turn ledgers in the corpus have a median of 2,244 characters and a 90th percentile of 135,885. Interpolating the table over the 1,616 non-empty ledgers, an invented integer percent passes on 37.5% of turns under v2.5 and 7.6% under v2.6 (interpolated, not measured per turn).

**Cost on real drafts** (2,711 turns, v2.5 path vs v2.6 on the same index): 70 of 6,539 token verdicts change (56 percents, 9 currency amounts and 2 dates become `COINCIDENT`; 3 lines become `DECLARED`), and blocked turns go from 850 to 871. Every `COINCIDENT` value is, by definition, present in the ledger, so section 2's "verbatim in ledger" error count no longer applies to v2.6. A hand-read sample of 25 changed verdicts (labelled by Claude, provisional) held about 6 real catches (opinion estimates that had grounded by chance) and about 10 false blocks. Two of the false blocks were a bug, fixed and turned into a selftest (a ratio's concatenated digits were being density-tested). The remaining false blocks: a Portuguese draft quoting an English ledger (no shared anchor word), CSS values, and round constants such as a 95% confidence level.

**The synthetic probes on v2.6** (`synth.py`, seed 42, the same draws as v2.5):

| probe | n | v2.5 | **v2.6** |
|---|--:|--:|--:|
| fabricated token let through, 14 shapes, non-empty ledgers | 19,110 | 2 (0.01%) | **2 (0.01%)** [0.00, 0.04] |
| fabricated receipt let through | 4,746 | 0 | **0** [0.00, 0.08] |
| real percent with its sign flipped, let through | 378 | 4 (1.1%) | **0** [0.00, 1.01] |
| real token copied from the ledger, wrongly blocked | 2,926 | 3 (0.10%) | **22 (0.75%)** [0.50, 1.14] |
| **correct** shown sum, wrongly blocked | 1,254 | 0 | **655 (52.23%)** [49.47, 54.99] |

The last two rows are the price of the look-elsewhere test, and the probes are its worst case. `synth.py` copies a real token onto a line with a random label (`row gh: 37%`), and builds its "correct sum" from two integers picked at random anywhere in the ledger, with `R$` put in front. Two unrelated bare numbers with no label are exactly what a coincidence looks like, so v2.6 blocks them when the ledger is dense. A real answer that copies a value with its label passes (the `copied_ctx` control above), and on real drafts the blocked turns rose from 850 to 871. The sign-flip leak closed for the same reason: a flipped percent used to ground on an unrelated negative number elsewhere in the ledger.

**Parity and latency.** The production copy of the matcher gives the same verdict as `grounding.py` on all 2,711 corpus turns. `check()` latency is unchanged: p50 3,101 ms vs 3,096 ms on the 15 largest ledgers (up to 3.8 MB), 10 ms vs 13 ms on 150 random turns.

**Not solved.** At 4M characters, 47.29% of invented integer percents still pass, because common words (`item`, `total`) sit near some occurrence of almost any 2-digit number. Anchors are not weighted by their own frequency, and anchors do not cross languages.

## 6. End-to-end A/B pilot (`ab/`)

Every number above measures the matcher. `ab/` measures the agent: the same headless Claude Code (`claude -p`, Opus 5.5, read-only tools, 12 turns) answers the same question twice, arm A with all hooks disabled and arm B with the production hooks. Tasks are fictional (invented carriers, invoices, repos), so neither the web nor the model's memory has the answer. Each task runs with its fixture directory as working directory, so no project instructions or memory reach the agent. An independent judge (`ab/score.py`, its own regexes, truth known by construction, 10 regression cases in `ab/test_score.py`) scores the answers.

Pilot, 2026-09-28: 36 tasks × 2 arms, 72 runs, US$ 21.56.

| | arm A (no hooks) | arm B (hooks) |
|---|--:|--:|
| asked value absent: invented and asserted | 0/27 [0, 12.5] | 0/27 [0, 12.5] |
| asked value absent: labelled guess ("rough estimate: 12:40") | 3/27 (11.1%) [3.9, 28.1] | 0/27 |
| asked value present or derivable: correct | 100% (9) | 100% (9) |
| mean seconds per run | 17.6 | 22.6 |
| cost | US$ 10.44 | US$ 11.12 |

**No reduction could be measured, because the base rate was zero.** The fixtures announced their own gaps (`"total": null`, `not computed`, `pending`), and with the gap written in the file Opus 5.5 did not invent. The hooks blocked 13 of 36 arm-B runs: 4 blocks removed guesses the draft itself labelled as estimates (all 3 the judge caught, Fisher p = 0.236), and 9 blocked correct arithmetic the matcher does not recognise as derived (a subtotal of listed items, per-row coverage, a UTC to BRT conversion, a sum written as `A 39.000 + B 11.000 + C 72.000`). The judge's first scoring said 25.9% vs 22.2% fabricated. Reading the 13 flagged answers showed every one was a judge false positive (a file mtime seen through `ls -l`, a duration read as a clock time, per-row arithmetic). Those cases are now the judge's regression tests.

A harder set is designed and not yet run: fixtures that do not announce the gap, a user who pushes for a number, and a weaker model as a second arm.

## Predictions registered before the runs, scored

1. *"v1 passes more than 40% of fabricated 2-digit numbers on ledgers above 500 digits; v2 passes less than 5%."* **Confirmed.** v1: 56.5% at 100-999 digits and 94.2% at 1,000-9,999. v2.1: 0.7% and 2.3%.
2. *"Precision on real blocks is below 50%: most blocks are true-but-unsourced, not fabrication."* **Confirmed.** About 66% of flags are carried context, and about 16% are unsourced factual assertions (v2.3 orphan share × orphan labels).
3. *(research pass)* *"After closing the suffix leak, the suffix probe falls from 13.2% to below 0.5%, and false blocks on suffixed tokens stay below 1%."* **Confirmed** on the corrected probe: 20.7% → 0.02%, with overall false blocks unchanged at 0.10%.
4. *(pending, 2026-10-12)* With the revision-checking hook in production, the share of flagged values kept bare after a block falls from 28.0% to below 10%.
5. *(pending, not yet run)* On the hard A/B set, a weaker model without hooks asserts an invented value on at least 10% of absent-value tasks. Below 5% would mean the grounding gate's value is in long sessions, not single-file tasks.

## What this does not measure

- **Whether a flagged assertion was actually false.** That needs a truth label per claim. Unsourced is not the same as wrong.
- **Recall on the model's own fabrications.** The synthetic set measures the matcher against a fabrication generator, not the model's error distribution. "0.01%" means that almost no fabricated token of these 14 shapes got through. It does not mean that almost no hallucination gets through.
- **Coincidence in ledgers above 4M characters, or anchors weighted by frequency.** Section 5 stops at 4M and treats every anchor word alike.
- **An end-to-end reduction.** The A/B pilot's base rate was zero (section 6).
- **Anything outside the gate's vocabulary.** Non-numeric claims (names, causal claims, "I ran the tests", "nothing found") are not danger tokens, and they pass untouched.
- **`selfcheck`, `corroborate`, `repro` and `claim`.**
- **Whether `action_gate.py` stops a real attack.** The replay is the author's own benign traffic plus constructed mutations; no live prompt-injection campaign was run against it.
- **Independent labels.** The labels came from the same model family as the agent.
