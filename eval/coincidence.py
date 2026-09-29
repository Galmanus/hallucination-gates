#!/usr/bin/env python3
"""
coincidence.py -- does an invented value pass as "sourced" just because the
ledger is big?

synth.py builds its fabricated tokens so that their digits provably do NOT
occur in the ledger, so it measures matcher bugs and, by construction, never
a chance collision. This measures the collision: random plausible values of
common formats, with no absence filter, checked against real ledgers grown to
controlled sizes. Any acceptance is a coincidence (the value was drawn at
random), so the acceptance rate is the gate's false-accept floor for a
fabrication of that format in a ledger of that size.

Idea borrowed from laya's README (github.com/NandhaKishorM/laya): a `noul`
max over sliding windows drifts up with the window count even with no signal.
Our analogue: a match over a larger ledger succeeds more often by chance.

Uniform draws are a LOWER bound: a real fabrication tends to sit near real
values of its context (plausible amounts, nearby times), which collides more.

Controls, reported with the result:
  copied      a danger token taken verbatim from the ledger, on a line with a
              random label: the no-context worst case for a legitimate copy
  copied_ctx  the same, labelled with the nearest word before it in the ledger:
              how a real answer quotes a value ("coverage 87%")
  hex64       a random 64-hex hash: must never be accepted
--no-index reproduces the v2.5 path (no look-elsewhere test).
"""
import argparse, json, math, os, random, re, sys, time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import grounding as g  # noqa: E402

SIZES = [1_000, 10_000, 100_000, 1_000_000, 4_000_000]
ACCEPT = {"GROUNDED", "DERIVED"}


def brl(v):
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


GEN = {
    "pct_int": lambda r: f"{r.randint(1, 99)}%",
    "pct_dec": lambda r: f"{r.randint(1, 99)},{r.randint(0, 9)}%",
    "brl": lambda r: brl(r.randint(10, 99999) + r.randint(0, 99) / 100),
    "usd": lambda r: f"${r.randint(100, 99999):,}",
    "time": lambda r: f"{r.randint(0, 23):02d}:{r.randint(0, 59):02d}",
    "date_br": lambda r: f"{r.randint(1, 28):02d}/{r.randint(1, 12):02d}",
    "date_iso": lambda r: f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}",
    "int_dot": lambda r: f"{r.randint(1000, 99999):,}".replace(",", "."),
    "int_unit": lambda r: f"{r.randint(2, 999)} testes",
    "ms": lambda r: f"{r.randint(10, 999)} ms",
    "hex8": lambda r: "".join(r.choice("0123456789abcdef") for _ in range(8)),
    "hex64": lambda r: "".join(r.choice("0123456789abcdef") for _ in range(64)),
}
LABELS = [a + b for a in "ghjkmnpqrstuvwxyz" for b in "ghjkmnpqrstuvwxyz"]


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * max(0, c - h), 2), round(100 * min(1, c + h), 2)]


def grow(pool, size, rng):
    parts, n = [], 0
    while n < size:
        s = rng.choice(pool)
        parts.append(s)
        n += len(s) + 1
    return "\n".join(parts)[:size]


def judge_lines(values, facts, ratio_pairs, index=None):
    """-> list of verdicts aligned with values; None when the gate does not treat it as a danger token.
    a value may be a (label, value) pair; otherwise it gets a random label."""
    out = []
    for i, v in enumerate(values):
        label, v = v if isinstance(v, tuple) else (f"item {LABELS[i % len(LABELS)]}", v)
        line = f"- {label}: {v}"
        toks = list(g.extract(line))
        if not toks:
            out.append(None)
            continue
        verdicts = [g.classify(t, k, ln, facts, ratio_pairs, index=index) if index is not None
                    else g.classify(t, k, ln, facts, ratio_pairs) for t, k, ln in toks]
        out.append("ACCEPT" if all(x in ACCEPT for x in verdicts) else "FLAG")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", default=os.path.expanduser("~/.cache/hg-eval/turns.jsonl"))
    ap.add_argument("--reps", type=int, default=12)
    ap.add_argument("--per-kind", type=int, default=40)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", default="")
    ap.add_argument("--no-index", action="store_true", help="v2.5 path: no look-elsewhere test")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    pool = [r["ledger"] for r in map(json.loads, open(a.turns, encoding="utf-8")) if len(r.get("ledger") or "") > 200]
    res = defaultdict(lambda: [0, 0, 0])  # (size, kind) -> accepted, flagged, not_danger
    t_facts = defaultdict(list)
    for size in SIZES:
        for rep in range(a.reps):
            ledger = grow(pool, size, rng)
            t0 = time.time()
            if a.no_index:
                facts, ratio_pairs = g.ledger_facts(ledger)
                index = None
            else:
                index, ratio_pairs = g.ledger_index(ledger)
                facts = index
            t_facts[size].append(time.time() - t0)
            # positive controls: danger tokens copied verbatim from this ledger
            head = ledger[:200_000]
            copied, copied_ctx = [], []
            for tok, _, _ in g.extract(head):
                pos = head.find(tok)
                words = re.findall(r'[A-Za-z_][\w.\-]{3,}', head[max(0, pos - 80):pos])
                copied.append(tok)
                if words:
                    copied_ctx.append((words[-1], tok))
            rng.shuffle(copied)
            rng.shuffle(copied_ctx)
            copied, copied_ctx = copied[:a.per_kind], copied_ctx[:a.per_kind]
            for kind, vals in [("copied", copied), ("copied_ctx", copied_ctx)] + \
                    [(k, [f(rng) for _ in range(a.per_kind)]) for k, f in GEN.items()]:
                for v in judge_lines(vals, facts, ratio_pairs, index):
                    cell = res[(size, kind)]
                    cell[0 if v == "ACCEPT" else 1 if v == "FLAG" else 2] += 1
        print(f"size {size}: {len(pool)} pool ledgers, facts {sum(t_facts[size]) / len(t_facts[size]):.2f}s mean",
              file=sys.stderr, flush=True)
    table = {}
    for (size, kind), (acc, flag, nd) in sorted(res.items()):
        n = acc + flag
        table.setdefault(kind, {})[size] = {"n": n, "accepted": acc, "not_danger": nd,
                                            "accept_pct": round(100 * acc / n, 2) if n else None,
                                            "ci95": wilson(acc, n)}
    out = {"gate": f"v{g.VERSION}" + (" (no index = v2.5 path)" if a.no_index else ""), "sizes": SIZES, "reps": a.reps, "per_kind": a.per_kind, "seed": a.seed,
           "facts_seconds_mean": {s: round(sum(v) / len(v), 3) for s, v in t_facts.items()}, "table": table}
    js = json.dumps(out, indent=1)
    if a.out:
        open(a.out, "w").write(js)
    print(f"{'kind':<10}" + "".join(f"{s:>16}" for s in SIZES))
    for kind, row in table.items():
        print(f"{kind:<10}" + "".join(f"{(str(row[s]['accept_pct']) + '% n' + str(row[s]['n'])) if s in row and row[s]['n'] else '-':>16}"
                                      for s in SIZES))


if __name__ == "__main__":
    main()
