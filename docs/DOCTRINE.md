# Doctrine: gating hallucination at the consumer boundary

This is the longer write-up behind [`README.md`](../README.md). It states the
mechanism the gates target, why they live at the output boundary rather than
inside the model, and how they are meant to compose. It introduces no benchmark
number that the README does not already state; where a number appears it is the
repo's own selftest measurement, labeled as such.

## 1. Hallucination is two stacked failures

A confident false statement from a language model is usually treated as one
phenomenon. It is cleaner to separate it into two failures with different causes
and different fixes.

**A pretraining floor.** Even a statistically optimal learner must err on facts
that carry no learnable pattern. The clean statement of this is that the
generative error rate is lower-bounded by the fraction of facts that appear
exactly once in training, estimated by the Good-Turing missing-mass term
(Kalai, Nachum, Vempala, Zhang, *Why Language Models Hallucinate*,
arXiv:2509.04664; the singleton rate is the Good-Turing estimator, Good 1953).
A calibrated model that has seen a fact once has no basis to be certain of it,
so it will sometimes get it wrong. This floor is a property of the data, not a
bug in the architecture.

**A persistence incentive.** Post-training does not remove the guessing; it
rewards it. Accuracy-only scoreboards score a lucky guess above an honest "I
don't know," so gradient pressure keeps the model answering when it should
abstain (the calibration argument in Kalai, Vempala, *Calibrated Language Models
Must Hallucinate*, arXiv:2311.14648). The leverage here is not a better model,
it is changing what the scoreboard rewards.

## 2. Why gate the output, not the model

Both fixes live at layers a *model consumer* cannot reach. You do not set the
pretraining floor: you did not train the model. You do not rescore the
industry's leaderboards: you are not the one running the eval. If you consume an
API model, the disposition that produces hallucination is fixed before you touch
it.

So this project takes the consumer's dual position. Since you cannot fix the
model's disposition, you gate its output. The gates are that boundary: a claim
leaves the pipeline only if it survives every gate that applies to it. This is a
deliberately modest architecture. It cannot make a model more calibrated. It can
refuse to forward the specific failures each gate is built to catch.

## 3. The detection families, and where these gates sit

Hallucination detection splits into four families. Naming them locates what this
repo does and, more usefully, what it cannot do.

1. **Black-box sampling.** Resample the same prompt and measure how much the
   answers agree in *meaning*, not surface form. Semantic entropy is the
   reference method (Farquhar, Kossen, Kuhn, Gal, *Detecting hallucinations
   using semantic entropy*, Nature 2024); SelfCheckGPT (Manakul et al., EMNLP
   2023) is the earlier black-box baseline. `selfcheck.py` sits here.
2. **White-box probes.** Read the model's internal states and predict from them
   (e.g. Semantic Entropy Probes, arXiv:2406.15927). This is often the strongest
   family, and it is closed to an API consumer: you do not have the activations.
   This repo does not pretend to reach it.
3. **Retrieval / fact verification.** Check a statement against an external
   source. `corroborate.py` is adjacent: it does not fetch ground truth, it
   weighs agreement across independent sources and collapses circular provenance.
4. **Calibration / eval incentive.** The framing from §1's second failure. Not a
   detector but a discipline about what gets rewarded; `claim.py` enforces the
   consumer-side version of it at egress.

The honest consequence: the strongest family (white-box) is unavailable to the
position this repo is written from, so the gates are entirely post-hoc and
black-box. That is the right architecture for a consumer, and it is also the
ceiling.

## 4. Composition: a claim must survive every gate that applies

The gates are not a menu; they are a sequence, each catching a different failure.

- **`grounding.py`** rejects any danger-token, a number, id, hash, or receipt,
  that does not trace to a tool result produced this turn. It gates fabrication
  of specifics, not correctness of reasoning.
- **`selfcheck.py`** gates a single generation by cross-sample meaning
  agreement (semantic entropy), ideally sampling across independent backends so
  disagreement reflects genuine uncertainty rather than one model's quirk.
- **`corroborate.py`** gates a stated fact by independence-weighted multi-source
  agreement, and treats conflict as signal rather than noise.
- **`repro.py`** gates a reproduction result: a claim of "this reproduces" is
  admitted only against a pre-committed prediction (anti-retrofit) and a negative
  control (anti-false-positive).
- **`claim.py`** is the egress gate: no assertion of existence, fact, or severity
  leaves under the author's name unless it is bound to an accepted evidence
  artifact at the right class and strength, with a severity ceiling that a lab
  result cannot cross.

Each gate is aimed at a distinct failure, so the residual that survives *all* of
them is specific and nameable: a claim that is (a) grounded token by token,
(b) consistent across samples, and (c) composed into a false *relation* between
individually true parts. That residual is not zero. `grounding.py`'s own
selftest puts 2-digit false-grounding at ~12% after its typed-fact rewrite, down
from 51.5% before it, on the module's internal cases. It is stated here rather
than hidden because a composition doctrine that hides its residual is theatre.

## 5. Honest limits

- **One gate is benchmark-measurable, four are not.** `selfcheck` implements a
  peer-reviewed detection method and can be scored on public hallucination
  benchmarks. The other four are process gates: they gate a pipeline rather than
  score a single generation, and are validated by design and by their offline
  selftests, not by an AUROC number.
- **No public-benchmark number is included yet.** Today the modules are validated
  by 124 offline selftests only. A SimpleQA / TruthfulQA evaluation of the
  semantic-entropy detector is the next milestone. Until it lands, treat this as
  a reference implementation and a composition doctrine, not a benchmarked
  state-of-the-art detector.
- **Black-box by design, and capped by it.** No gate reads model internals. That
  is correct for consuming an API model you did not train, and it is the ceiling.

## References

- Kalai, Nachum, Vempala, Zhang. *Why Language Models Hallucinate.* arXiv:2509.04664, 2025.
- Kalai, Vempala. *Calibrated Language Models Must Hallucinate.* arXiv:2311.14648, 2023.
- Farquhar, Kossen, Kuhn, Gal. *Detecting hallucinations using semantic entropy.* Nature, 2024. doi:10.1038/s41586-024-07421-0.
- Manakul, Liusie, Gales. *SelfCheckGPT.* EMNLP 2023.
- Kossen et al. *Semantic Entropy Probes.* arXiv:2406.15927, 2024.
- *From Tokens to Semantics.* arXiv:2609.02679, 2026.
- Dhuliawala et al. *Chain-of-Verification.* ACL Findings 2024.
- Huang et al. *A Survey on Hallucination in Large Language Models.* 2024.
- Good. *The population frequencies of species and the estimation of population parameters.* Biometrika, 1953.
