#!/usr/bin/env python3
"""
realrun.py -- replay grounding v1 and v2 over REAL agent turns, and ask where
every flagged token actually came from.

The synthetic benchmark (synth.py) measures the matcher. This one measures the
gate on real traffic, where the ground truth is unknown. A block is not a
hallucination; it is "this token did not trace to this turn's tool output".
So every token v2 flags is traced, in order, against:

  1. this turn's ledger, verbatim      -> the gate was WRONG (value was there)
  2. prior context in the session      -> CARRIED: true-looking, but unsourced
     (earlier prompts, tool output,       this turn; the gate asks for a
     assistant text, injected context)    re-check, it did not catch a lie
  3. static context loaded at start    -> CARRIED (same meaning). Approximated
     (memory files, CLAUDE.md, the        with the CURRENT versions of those
     agent's system prompt)               files, stated as a limit.
  4. nowhere                           -> ORPHAN: fabricated, derived by the
                                          agent (arithmetic, a date it computed),
                                          or a code/example literal. Needs a
                                          human label; a random sample is
                                          written to a private file for that.

Output: aggregate JSON (safe to publish) + private label sample.
"""
import argparse, glob, json, os, random, re, sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "baselines"))
import grounding as v2          # noqa: E402
import grounding_v1 as v1       # noqa: E402
import grounding_v21 as v21     # noqa: E402
import grounding_v23pre as v23p  # noqa: E402
import grounding_v24 as v24     # noqa: E402

# context an agent loads at session start (memory, instructions, its own system
# prompt). Defaults are generic Claude Code locations; add your agent's prompt
# files with --static (the published run added the agent's CLAUDE.md files and
# the file holding its system prompt).
STATIC = ["~/CLAUDE.md", "~/.claude/CLAUDE.md", "~/.claude/projects/*/memory/*.md"]


def static_context(extra=()):
    parts = []
    for pat in list(STATIC) + list(extra):
        for p in glob.glob(os.path.expanduser(pat)):
            try:
                parts.append(open(p, encoding="utf-8", errors="replace").read())
            except Exception:
                pass
    return "\n".join(parts)


def occurs(tok, text):
    return re.search(r'(?<![\w.])' + re.escape(tok) + r'(?![\w])', text) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.path.expanduser("~/.cache/hg-eval/turns.jsonl"))
    ap.add_argument("--out", default=os.path.expanduser("~/.cache/hg-eval/realrun.json"))
    ap.add_argument("--sample", default=os.path.expanduser("~/.cache/hg-eval/label_sample.jsonl"))
    ap.add_argument("--n-sample", type=int, default=80)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--static", action="append", default=[],
                    help="extra glob of static context files (repeatable)")
    a = ap.parse_args()

    static = static_context(a.static)
    turns = 0
    blocked = Counter()
    flagged_tokens = Counter()
    prod_blocked = 0
    prov = Counter()
    prov_by_kind = defaultdict(Counter)
    verdicts = Counter()
    orphans = []
    with open(a.corpus, encoding="utf-8") as f:
        for line in f:
            t = json.loads(line)
            turns += 1
            prod_blocked += t["blocked_in_prod"]
            r1 = v1.check(t["draft"], t["ledger"])
            r21 = v21.check(t["draft"], t["ledger"])
            r23 = v23p.check(t["draft"], t["ledger"])
            r24 = v24.check(t["draft"], t["ledger"])
            r2 = v2.check(t["draft"], t["ledger"])
            for g, rr in (("v1", r1), ("v2.1", r21), ("v2.3-pre", r23), ("v2.4", r24), (f"v{v2.VERSION}", r2)):
                blocked[g] += rr["block"]; flagged_tokens[g] += len(rr["flagged"])
            for fl in r2["flagged"]:
                tok = fl["token"]
                verdicts[fl["verdict"]] += 1
                if occurs(tok, t["ledger"]):
                    p = "in_ledger_verbatim"
                elif occurs(tok, t.get("context_tools", "")):
                    p = "carried_prior_tool_output"
                elif occurs(tok, t["context"]):
                    p = "carried_session_text"
                elif occurs(tok, static):
                    p = "carried_static"
                else:
                    p = "orphan"
                prov[p] += 1
                prov_by_kind[fl["kind"]][p] += 1
                if p == "orphan":
                    orphans.append({"session": t["session"], "ts": t["ts"], "token": tok,
                                    "kind": fl["kind"], "verdict": fl["verdict"], "line": fl["line"],
                                    "prompt_tail": t["context"][-600:]})
    rng = random.Random(a.seed)
    sample = rng.sample(orphans, min(a.n_sample, len(orphans)))
    with open(a.sample, "w", encoding="utf-8") as f:
        for s in sample:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    tot = sum(prov.values())
    res = {
        "turns": turns,
        "blocked_in_production_log": prod_blocked,
        "replay_block_rate": {g: round(blocked[g] / turns, 4) for g in ("v1", "v2.1", "v2.3-pre", "v2.4", f"v{v2.VERSION}")},
        "replay_blocked_turns": dict(blocked),
        "flagged_tokens": dict(flagged_tokens),
        "current_flag_verdicts": dict(verdicts),
        "current_flag_provenance": {k: {"n": v, "share": round(v / tot, 4)} for k, v in prov.most_common()},
        "current_flag_provenance_by_kind": {k: dict(v) for k, v in prov_by_kind.items()},
        "orphans_total": len(orphans),
        "label_sample_size": len(sample),
    }
    json.dump(res, open(a.out, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
