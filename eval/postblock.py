#!/usr/bin/env python3
"""
postblock.py -- what the agent DID after a production grounding block.

For every "Stop hook feedback: GROUNDING GUARD" row in the transcripts, parse
the flagged tokens, then read what the agent did before the turn closed:
  grounded     it called a tool afterwards whose output contains the token
  declared     the token stays, but its line now carries an explicit source /
               uncertainty tag (nao verificado, INFERRED, RECALL, fonte:)
  removed      the token no longer appears in the post-block text
  kept_bare    the token stays with no new tool evidence and no tag
                (the hook allows only one forced revision per turn)
"""
import glob, json, os, re, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corpus as C

TOK = re.compile(r"\] (\w+): '([^']+)'")
DECL = re.compile(r"n[aã]o[- ]verificad|unverified|INFERRED|RECALL|fonte:|source:|estimat|derivad", re.I)

out = Counter(); by_kind = {}; turns = Counter()
for p in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    rows = C._rows(p)
    for i, r in enumerate(rows):
        if not C.is_stop_feedback(r):
            continue
        c = C._content(r); fb = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        toks = TOK.findall(fb)
        if not toks:
            continue
        post_txt, post_led = [], []
        for r2 in rows[i + 1:]:
            if C.is_real_prompt(r2) or C.is_stop_feedback(r2):
                break
            post_txt.append(C.assistant_text(r2)); post_led.append(C.tool_output(r2))
        txt = "\n".join(post_txt); led = "\n".join(post_led)
        turns["blocks"] += 1
        turns["with_new_tool_call"] += bool(led.strip())
        for kind, tok in toks:
            lines = [l for l in txt.splitlines() if tok in l]
            if tok in led:
                v = "grounded"
            elif not lines:
                v = "removed"
            elif any(DECL.search(l) for l in lines):
                v = "declared"
            else:
                v = "kept_bare"
            out[v] += 1; by_kind.setdefault(kind, Counter())[v] += 1
n = sum(out.values())
print(json.dumps({"blocks": turns["blocks"], "blocks_followed_by_new_tool_call": turns["with_new_tool_call"],
                  "flagged_tokens": n, "outcome": {k: [v, round(v / n, 3)] for k, v in out.most_common()},
                  "by_kind": {k: dict(v) for k, v in by_kind.items()}}, indent=1))
