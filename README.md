# hallucination-gates

**Composable, zero-dependency Python gates that catch LLM hallucination at the output boundary.**

[![selftest](https://github.com/Galmanus/hallucination-gates/actions/workflows/selftest.yml/badge.svg)](https://github.com/Galmanus/hallucination-gates/actions/workflows/selftest.yml)

Five small modules. No dependencies beyond the Python standard library. Each runs
standalone and ships an offline `--selftest`. They are meant to be composed: a claim
only leaves the pipeline if it survives every gate that applies to it.

```bash
python3 grounding.py   --selftest   # 22 checks
python3 selfcheck.py   --selftest   # 28 checks
python3 corroborate.py --selftest   # 24 checks
python3 claim.py       --selftest   # 32 checks
python3 repro.py       --selftest   # 18 checks
# 124 checks total, no install step
```

## The idea

Hallucination is two stacked failures with different causes and different fixes.

1. A **pretraining floor**: even an optimal learner must err on facts that carry no
   learnable pattern; the floor is tied to the fraction of facts seen exactly once
   (Kalai et al., *Why Language Models Hallucinate*, arXiv:2509.04664 — the singleton
   rate is the Good-Turing missing-mass estimator).
2. A **persistence incentive**: accuracy-only scoreboards reward a lucky guess over an
   honest "I don't know," so post-training keeps models guessing. The leverage there is
   not a better model, it is changing what the scoreboard rewards.

Both fixes live at layers a *model consumer* cannot reach: you don't set the pretraining
floor, and you don't rescore the industry's leaderboards. So this project takes the
consumer's dual position: **since you cannot fix the model's disposition, you gate its
output.** These five gates are that boundary.

## The five gates

| module | what it gates | method it instantiates |
|---|---|---|
| `selfcheck.py` | a single generation, by cross-sample meaning agreement | **semantic entropy** (Farquhar et al., *Nature* 2024) + complementary token signals (arXiv:2609.02679) |
| `grounding.py` | any danger-token (number, id, receipt) that does not trace to a tool result this turn | a runtime egress gate — no direct literature analogue, consumer-side |
| `corroborate.py` | a stated fact, by independence-weighted multi-source agreement | multi-source corroboration with circular-provenance collapse (in the spirit of Chain-of-Verification's independent checks) |
| `claim.py` | an outbound report, binding every claim to an accepted evidence artifact at the right class and strength | eval-layer discipline forced at egress |
| `repro.py` | a reproduction result, via a pre-committed prediction + a negative control | pre-registration against retrofit and false positives |

## Scope and honest limits

Read this before you judge the repo, because it is the part a careful reviewer checks first.

- **One gate is benchmark-measurable, four are not.** `selfcheck` implements a
  peer-reviewed *detection* method (semantic entropy) and can be scored on public
  hallucination benchmarks. The other four are **process gates**: they gate a pipeline
  rather than score a single generation, and are validated by design and by their offline
  selftests, not by an AUROC number.
- **No public-benchmark number is included yet.** There is currently no SimpleQA /
  TruthfulQA evaluation in this repo. Today the modules are validated by **124 offline
  selftests only**. A benchmark evaluation of the semantic-entropy detector is the next
  milestone. Until it lands, treat this as a reference implementation and a composition
  doctrine, not a benchmarked state-of-the-art detector.
- **Black-box by design, and capped by it.** Every gate is post-hoc and black-box; none
  reads model internals. That is the right architecture for someone consuming an API model
  they did not train, and it is also the ceiling. The residual that survives *all* gates is
  the hallucination that is (a) grounded token by token, (b) consistent across samples, and
  (c) composed into a false *relation*. `grounding`'s own measurement puts 2-digit
  false-grounding at ~12% after its typed-fact rewrite — lower than the 51.5% it started
  from, but not zero. That residual is a consequence of consuming rather than training a
  model, and it is stated, not hidden.

## What this is not

- Not a claim to beat SOTA detectors — no benchmark number is asserted here.
- Not a white-box probe. It cannot read activations and does not pretend to.
- Not a replacement for training-time fixes (abstention rewards, calibration). It gates
  output; it cannot raise a model's calibration.

## Install and run

No install. Python 3.9+ standard library only.

```bash
git clone https://github.com/Galmanus/hallucination-gates
cd hallucination-gates
./run_selftests.sh        # runs all five, exits non-zero on any failure
```

Each module also has a small CLI; run `python3 <module>.py --help`.

## References

- Kalai, Nachum, Vempala, Zhang. *Why Language Models Hallucinate.* arXiv:2509.04664, 2025.
- Kalai, Vempala. *Calibrated Language Models Must Hallucinate.* arXiv:2311.14648, 2023.
- Farquhar, Kossen, Kuhn, Gal. *Detecting hallucinations using semantic entropy.* Nature, 2024. doi:10.1038/s41586-024-07421-0.
- Manakul, Liusie, Gales. *SelfCheckGPT.* EMNLP 2023.
- Kossen et al. *Semantic Entropy Probes.* arXiv:2406.15927, 2024.
- *From Tokens to Semantics.* arXiv:2609.02679, 2026.
- Dhuliawala et al. *Chain-of-Verification.* ACL Findings 2024.
- Huang et al. *A Survey on Hallucination in LLMs.* 2024.
- Good. *The population frequencies of species and the estimation of population parameters.* Biometrika, 1953 (Good-Turing missing mass).

A longer write-up of the mechanism and the design rationale is in [`docs/DOCTRINE.md`](docs/DOCTRINE.md).

## License

MIT — see [LICENSE](LICENSE).
