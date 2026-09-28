#!/usr/bin/env python3
"""
synth.py -- controlled-injection benchmark for grounding.py on REAL ledgers.

Question: given the tool output an agent really received in a turn, does the
gate (a) block a number that is NOT in that output, and (b) let through a
number that IS in it?

For every turn in the private corpus (see corpus.py) we take its real ledger
and build one-token-per-line drafts of two kinds:

  FABRICATED  a danger token whose digits provably do not occur in the
              ledger. The absence test is STRICT and independent of the gate:
              no digit group of the token (len >= 2) appears in the ledger as
              a standalone number, and hex hashes do not appear as substrings.
              A gate that lets one of these through has a matcher bug or a
              chance collision, never a "maybe it was there".
  COMPOSED    a ratio "A of B" where A and B both occur in the ledger as
              numbers but never together as a ratio. v2 is designed to block
              this; v1 has no concept of it.
  GROUNDED    a danger token copied verbatim from the ledger. A gate that
              blocks one of these is a false positive.

Metrics per gate (v1 = substring baseline, v2 = typed-fact matcher):
  miss rate          fabricated tokens judged GROUNDED (lower is better)
  false-block rate   grounded tokens judged not GROUNDED (lower is better)
both broken down by token kind and by ledger size in digits (the L in the
v1 collision argument). Every rate is reported with its n and a Wilson 95% CI.

Deterministic: fixed seed. Output: JSON to stdout or --out.
"""
import argparse, json, math, os, random, re, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "baselines"))
import grounding as v2          # noqa: E402
import grounding_v1 as v1       # noqa: E402
import grounding_v21 as v21     # noqa: E402
import grounding_v23pre as v23p  # noqa: E402

GATES = {"v1": v1.check, "v2.1": v21.check, "v2.3-pre": v23p.check, f"v{v2.VERSION}": v2.check}

LABELS = [a + b for a in "ghijklmnopqrstuvwxyz" for b in "ghijklmnopqrstuvwxyz"]
BUCKETS = [(0, 0), (1, 99), (100, 999), (1000, 9999), (10000, 99999), (100000, 10**12)]


def bucket(n):
    for lo, hi in BUCKETS:
        if lo <= n <= hi:
            return f"{lo}-{hi}" if hi < 10**12 else f">={lo}"
    return "?"


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0, c - h), 4), round(min(1, c + h), 4)]


class Ledger:
    def __init__(self, text):
        self.text = text
        self.low = text.lower()
        self.nums = set(re.findall(r'\d+', text))   # maximal digit runs

    def has_num(self, s):
        return s in self.nums

    def absent(self, token, kind):
        if kind == "txhash":
            return token.lower() not in self.low
        if token.endswith(("K", " mil", "M")):
            # suffixed: the SCALED value and its rounding window must be absent.
            # The bare base number MAY be present: that collision is the case to test
            # (a first version also required the base absent and hid a real leak).
            mult = 10**6 if token.endswith("M") else 1000
            base = re.sub(r'\D', '', token)
            suffix = "M" if token.endswith("M") else ("mil" if token.endswith("mil") else "K")
            alt = {"K": "(?:K|k|mil)", "mil": "(?:K|k|mil)", "M": "(?:M|MM|mi|milh)"}[suffix]
            if re.search(r'(?<![\d.,])' + base + r'\s?' + alt, self.text):
                return False  # present in suffixed form (R$121K, 300 mil)
            v = int(base) * mult
            if not hasattr(self, "_ints"):
                self._ints = [int(x) for x in re.findall(r'\d+', self.text.replace('.', '').replace(',', '')) if len(x) < 13]
            return not any(v - mult // 2 <= x < v + mult // 2 for x in self._ints)
        groups = [g for g in re.findall(r'\d+', token) if len(g) >= 2]
        core = re.sub(r'\D', '', token)
        return not any(self.has_num(g) for g in groups + [core] if g)


def fabricate(kind, rng):
    if kind == "pct2":
        return f"{rng.randint(10, 99)}%", "percent"
    if kind == "pct3":
        return f"{rng.randint(10, 99)}.{rng.randint(1, 9)}%", "percent"
    if kind == "currency":
        return f"R$ {rng.randint(100, 999)},{rng.randint(10, 99)}", "currency"
    if kind == "time":
        return f"{rng.randint(10, 23)}:{rng.randint(10, 59)}", "time"
    if kind == "date":
        return f"{rng.randint(10, 28)}/{rng.randint(10, 12)}", "date"
    if kind == "ratio":
        b = rng.randint(20, 99)
        return f"{rng.randint(10, b - 1)} of {b}", "ratio"
    if kind == "ipv4":
        return ".".join(str(rng.randint(11, 254)) for _ in range(4)), "ipv4"
    if kind == "txhash":
        return "".join(rng.choice("0123456789abcdef") for _ in range(12)), "txhash"
    if kind == "cur_k":
        return f"R$ {rng.randint(100, 999)}K", "currency"
    if kind == "cur_mil":
        return f"R$ {rng.randint(10, 999)} mil", "currency"
    if kind == "cur_m":
        return f"US$ {rng.randint(2, 99)}M", "currency"
    if kind == "hex0x":
        return "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(64)), "txhash"
    if kind == "strkey":
        return rng.choice("GC") + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(55)), "txhash"
    if kind == "base58":
        a = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        return "5Ab" + "".join(rng.choice(a) for _ in range(85)), "txhash"
    raise ValueError(kind)


FAB_KINDS = ["pct2", "pct3", "currency", "cur_k", "cur_mil", "cur_m", "time", "date", "ratio",
             "ipv4", "txhash", "hex0x", "strkey", "base58"]
SUFFIX_KINDS = {"cur_k", "cur_mil", "cur_m"}
# lines carrying words that used to exempt the whole line (v2.1-v2.3 leak lane)
EXEMPT_PROBE_LINES = ["row {lab}: valor convertido {tok} aqui", "row {lab}: soma total {tok}",
                      "row {lab}: IP range {tok} do alvo", "row {lab}: derivado: {tok}"]

# verbatim tokens we are willing to copy from a ledger as positives.
# Structural receipts are taken only as WHOLE tokens (no alphanumeric neighbour):
# a first run sliced 43-88-char "base58" windows out of base64 blobs, which no
# agent would cite (208 of 208 base58 false blocks traced to that; RESULTS.md).
_W = (r'(?<![0-9A-Za-z])', r'(?![0-9A-Za-z])')
POS_RX = {
    "percent":  re.compile(r'(?<![\w.,])\d{1,3}(?:\.\d)?%'),
    "time":     re.compile(r'(?<![\d:])(?:[01]\d|2[0-3]):[0-5]\d(?![\d:])'),
    "ipv4":     re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])'),
    "txhash":   re.compile(_W[0] + r'[0-9a-f]{12,64}' + _W[1]),
    "currency": re.compile(r'(?:R\$|US\$)\s?\d(?:[\d.,]*\d)?(?![\d.,]*\d)'),
    "ratio":    re.compile(r'(?<![\d/])\d{1,3} of \d{1,3}(?![\d/])'),
    "hex0x":    re.compile(_W[0] + r'0x[0-9a-fA-F]{8,64}' + _W[1]),
    "strkey":   re.compile(_W[0] + r'[GC][A-Z2-7]{55}' + _W[1]),
    "base58":   re.compile(_W[0] + r'(?=[1-9A-HJ-NP-Za-km-z]*\d)(?=[1-9A-HJ-NP-Za-km-z]*[a-z])(?=[1-9A-HJ-NP-Za-km-z]*[A-Z])[1-9A-HJ-NP-Za-km-z]{43,88}' + _W[1]),
}


def draft_for(tokens, templates=None):
    lines, keys = [], {}
    for i, tok in enumerate(tokens):
        lab = LABELS[i]
        tpl = (templates or {}).get(i, "row {lab}: the output reports {tok} here")
        lines.append(tpl.format(lab=lab, tok=tok))
        keys[lab] = (i, tok)
    return "\n".join(lines), keys


def verdicts(check, tokens, ledger_text, templates=None):
    """map job index -> verdict for a one-token-per-line draft."""
    draft, keys = draft_for(tokens, templates)
    r = check(draft, ledger_text)
    flagged = {}
    for f in r["flagged"]:
        lab = f["line"].split(":", 1)[0].replace("row ", "").strip()
        flagged[lab] = f["verdict"]
    return {keys[lab][0]: flagged.get(lab, "GROUNDED") for lab in keys}


def run(corpus, limit, seed):
    rng = random.Random(seed)
    # counts[gate][class][kind][bucket] = [bad, n]
    counts = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0]))))
    n_turns = 0
    diag = defaultdict(lambda: defaultdict(int))
    examples = []
    with open(corpus, encoding="utf-8") as f:
        for line in f:
            if limit and n_turns >= limit:
                break
            t = json.loads(line)
            L = Ledger(t["ledger"])
            ndig = len(re.sub(r'\D', '', t["ledger"]))  # the L of the v1 argument: ALL digits
            b = bucket(ndig)
            n_turns += 1
            jobs = []  # (class, kind, token)
            for k in FAB_KINDS:
                for _ in range(50):
                    tok, gkind = fabricate(k, rng)
                    if L.absent(tok, gkind):
                        jobs.append(("fabricated", k, tok)); break
            # composed ratio: two ledger numbers never seen together as a ratio
            two = [x for x in L.nums if 2 <= len(x) <= 3 and not x.startswith("0")]
            if len(two) >= 2:
                a, c = rng.sample(sorted(two), 2)
                if not re.search(rf'(?<!\d){a}\s*(?:de|of|/)\s*{c}(?!\d)', t["ledger"]):
                    jobs.append(("composed", "ratio", f"{a} of {c}"))
            # sign flip: a real positive percent, asserted negative
            pos_pct = [m for m in re.findall(r'(?<![\w.,\-−])(\d{1,2})%', t["ledger"]) if m != "0"]
            if pos_pct:
                n = rng.choice(pos_pct)
                if not re.search(rf'[-−]\s?{n}%', t["ledger"]):
                    jobs.append(("signflip", "percent", f"-{n}%"))
            templates = {}
            # exempt-word probe: a fabricated token on a line with a former exempt word
            for _ in range(20):
                tok, gk = fabricate(rng.choice(["pct2", "currency", "ipv4"]), rng)
                if L.absent(tok, gk):
                    templates[len(jobs)] = rng.choice(EXEMPT_PROBE_LINES)
                    jobs.append(("exempt_probe", gk, tok))
                    break
            # derived arithmetic (R4): right sum passes, wrong sum and a
            # fabricated factor must block. Operands are real ledger numbers.
            # operands: STANDALONE integers only (not digits inside a time, date,
            # hash or identifier, which the gate rightly does not treat as quantities)
            ops = sorted(set(re.findall(r'(?<![\w.,:/\-])([1-9]\d{1,3})(?![\w.,:/\-%])', t["ledger"])))
            if len(ops) >= 2:
                x, y = (int(v) for v in rng.sample(ops, 2))
                right = f"R$ {x} + R$ {y} = R$ {x + y}"
                templates[len(jobs)] = "row {lab}: {tok}"
                jobs.append(("derived_right", "currency", right))
                wrong_v = x + y + rng.choice([1, 3, 7, 11, 90, 100])
                if not L.has_num(str(wrong_v)):
                    templates[len(jobs)] = "row {lab}: {tok}"
                    jobs.append(("derived_wrong", "currency", f"R$ {x} + R$ {y} = R$ {wrong_v}"))
                k = rng.randint(13, 97)
                if not L.has_num(str(k)) and not L.has_num(str(x * k)) and str(k) not in ("24", "30", "52", "60"):
                    templates[len(jobs)] = "row {lab}: {tok}"
                    jobs.append(("derived_launder", "currency", f"R$ {x} × {k} = R$ {x * k}"))
            for k, rx in POS_RX.items():
                ms = rx.findall(t["ledger"])
                if ms:
                    jobs.append(("grounded", k, rng.choice(ms).strip()))
            if not jobs:
                continue
            toks = [j[2] for j in jobs]
            for g, check in GATES.items():
                v = verdicts(check, toks, t["ledger"], templates)
                for idx, (cls, kind, tok) in enumerate(jobs):
                    passes = cls in ("grounded", "derived_right")
                    bad = (v[idx] != "GROUNDED") if passes else (v[idx] == "GROUNDED")
                    cell = counts[g][cls][kind][b]
                    cell[0] += bad; cell[1] += 1
                    if bad and g == f"v{v2.VERSION}" and len(examples) < 40:
                        examples.append({"class": cls, "kind": kind, "token": tok,
                                         "ctx": _ctx(tok, t["ledger"])})
                    if bad and g == f"v{v2.VERSION}":
                        c = cause(cls, kind, tok, t["ledger"])
                        diag[cls][c] += 1
                        if len(examples) < 40:
                            examples.append({"class": cls, "kind": kind, "token": tok, "cause": c,
                                             "ledger_digits": ndig})
    return n_turns, counts, diag, examples


def _ctx(tok, ledger):
    """a 60-char ledger window around the best match of the token's digits (debug only, private)."""
    d = re.sub(r'\D', '', tok)[:4]
    i = ledger.find(d) if d else -1
    return ledger[max(0, i - 30):i + 30] if i >= 0 else ""


def cause(cls, kind, tok, ledger):
    """coarse root cause of a v2 error, for RESULTS.md (no ledger content kept)."""
    core = re.sub(r'\D', '', tok)
    if cls == "grounded":
        if kind != "txhash" and len(core) < 2:
            return "single-digit value (digit_core requires >= 2 digits)"
        i = ledger.find(tok)
        if i > 0 and (ledger[i - 1] == "_" or ledger[i - 1].isalnum()):
            return "token glued to a word character (regex \\b fails)"
        return "other"
    if cls in ("fabricated", "composed"):
        flat = re.findall(r'\d[\d.,]*\d', ledger)
        if any(re.sub(r'\D', '', x) == core and not x.isdigit() for x in flat):
            return "collision: separator-flattened number (e.g. 2.0 -> 20)"
        return "other"
    return "other"


def summarize(n_turns, counts, diag, examples=()):
    out = {"turns": n_turns, "gates": {}, "current_error_causes": {c: dict(d) for c, d in diag.items()},
           "current_error_examples": list(examples)}
    for g, by_cls in counts.items():
        G = out["gates"][g] = {}
        for cls, by_kind in by_cls.items():
            tot_bad = tot_n = 0
            kinds, bks = {}, defaultdict(lambda: [0, 0])
            for kind, by_b in by_kind.items():
                kb = sum(c[0] for c in by_b.values()); kn = sum(c[1] for c in by_b.values())
                kinds[kind] = {"rate": round(kb / kn, 4), "bad": kb, "n": kn, "ci95": wilson(kb, kn),
                               "by_ledger_digits": {bk: {"rate": round(c[0] / c[1], 4), "bad": c[0], "n": c[1]}
                                                    for bk, c in by_b.items()}}
                tot_bad += kb; tot_n += kn
                for bk, c in by_b.items():
                    bks[bk][0] += c[0]; bks[bk][1] += c[1]
            G[cls] = {
                "rate": round(tot_bad / tot_n, 4) if tot_n else None, "bad": tot_bad, "n": tot_n,
                "ci95": wilson(tot_bad, tot_n), "by_kind": kinds,
                "by_ledger_digits": {k: {"rate": round(v[0] / v[1], 4), "bad": v[0], "n": v[1],
                                         "ci95": wilson(*v)} for k, v in sorted(bks.items(), key=lambda kv: int(kv[0].lstrip('>=').split('-')[0]))},
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.path.expanduser("~/.cache/hg-eval/turns.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out")
    ap.add_argument("--gates", default=",".join(GATES), help="comma list, e.g. v2.2")
    a = ap.parse_args()
    for g in list(GATES):
        if g not in a.gates.split(","):
            del GATES[g]
    res = summarize(*run(a.corpus, a.limit, a.seed))
    res["seed"] = a.seed
    s = json.dumps(res, indent=1)
    if a.out:
        open(a.out, "w").write(s + "\n")
    print(s)


if __name__ == "__main__":
    main()
