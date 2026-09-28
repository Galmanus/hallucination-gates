# eval

Measurements for `grounding.py`. Results are in [`RESULTS.md`](RESULTS.md).

The other four gates are not covered here yet. `selfcheck.py` gets a public-benchmark run (SimpleQA) as a separate milestone, because it needs paid API sampling. `corroborate`, `repro` and `claim` are process gates, and no public benchmark exists for them.

## Pipeline

```bash
python3 eval/corpus.py      # real transcripts -> ~/.cache/hg-eval/turns.jsonl (PRIVATE)
python3 eval/synth.py  --out ~/.cache/hg-eval/synth_final.json
python3 eval/realrun.py     # -> realrun.json + a private label sample
python3 eval/postblock.py   # what the agent did after each production block
```

| script | question it answers | ground truth |
|---|---|---|
| `synth.py` | On real ledgers, does the matcher block numbers that are not there, and pass numbers that are? | constructed: fabricated tokens pass a strict absence test that doesn't use the gate, and grounded tokens are copied verbatim from the ledger |
| `realrun.py` | On real agent drafts, what does the gate flag, and where did each flagged token actually come from? | none for truth, so provenance is traced, then a random sample of untraceable flags is labeled by hand |
| `postblock.py` | After a production block, did the agent ground, declare, drop, or keep the token bare? | the transcript itself |

`baselines/grounding_v21.py` is v2.1 as committed at `0d498fc`: the version the production hook ran, and the one the fixes were measured against. `baselines/grounding_v23pre.py` is v2.3 before the three leaks the research pass reproduced (suffix base, sign, exempt words). It is kept so the leak closure itself can be measured. `baselines/grounding_v1.py` is the original substring matcher. It is taken from the agent's own git history (commit `37e759f`, 2026-09-07), verbatim except for one anonymized vendor name, and kept only so that v1 and v2 can be compared on the same data.

## Privacy

The corpus is the author's own agent transcripts, which include tool output, private conversations and credentials that appeared in logs. None of it is committed. The scripts write it to `~/.cache/hg-eval/`, and only aggregate counts are copied into `RESULTS.md`. To reproduce on your own agent, point `corpus.py --projects` at your Claude Code transcripts directory.

## The corpus is a snapshot

Claude Code deletes old session transcripts. A rebuild of the corpus 3 hours after the first one had 26 fewer sessions, all from the oldest days (2026-08-26 to 08-29). The frozen `turns.jsonl` from the first build is the reference for `RESULTS.md`. A rebuild months later will have fewer turns and slightly different rates.

## Known limits of the method

- **The synthetic fabrications come from my generator, not the model's error distribution.** They measure the matcher, not the rate at which a real model's fabrications get caught.
- **The labeler is from the same model family as the agent.** The 80-token label sample was labeled by Claude. It needs a human second pass before any rate derived from it is treated as final.
- **Static context is approximated.** Memory files and CLAUDE.md are read in their current version, not the version that was live when each turn ran. That can inflate `carried_static`.
- **The token-occurrence check is strict about suffixes.** A draft `R$40` does not count as carried from a context that says `R$40k`. That deflates `carried` and inflates `orphan`.
