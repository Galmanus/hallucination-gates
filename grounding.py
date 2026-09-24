#!/usr/bin/env python3
"""
grounding.py -- anti-hallucination grounding guard.

Motivated by a real incident (the "bus case", see selftest below): a draft
answer stated operationally specific data (carrier, time, price, seat count)
as if it had been directly OBSERVED, with no tool artifact backing any of
it. That was a hallucination. This gate makes that error class detectable
by an executable test, not by model self-discipline.

DOCTRINE (one falsifiable sentence):
  Every DANGER-TOKEN in a response (price, time-of-day, an N-of-M count,
  percentage, hex tx-hash, IPv4, date) MUST trace to a FACT that some tool
  actually returned this turn (the "ledger"). No grounding and no declared
  uncertainty tag -> FLAG as probable hallucination.

MATCHER (v2, 2026-09-09 -- fixes v1's broken arithmetic):
  v1 flattened ALL tool output into a single digit stream and matched by
  substring. That has an arithmetic flaw: a fabricated number of d digits
  matches by chance with probability ~ (L-d) * 10^-d in a stream of length
  L. Measured against a real 857-digit ledger (a SMALL turn): 2 digits =
  51.5% falsely grounded, 3 digits = 16%. It gets worse as L grows -- the
  guard got weaker on tool-heavy turns, exactly when number hallucination
  is most likely.

  v2 matches against DISCRETE, TYPED facts, never a stream substring:
  1. The ledger is tokenized into the SAME types as the draft (currency/
     percent/time/ipv4/txhash/date/ratio) plus raw numbers from whatever is
     left over; each typed fact's span is MASKED before scanning for raw
     numbers, so "92%" does not also leave behind a phantom raw "92" (kills
     unit confusion).
  2. Grounded = a fact exists in the ledger with the SAME digit core AND a
     COMPATIBLE type. "R$ 92" does not ground against "92%". "20:00" needs
     a real time in the ledger, not the digits 2000 buried inside 59320.
     Deliberate leniency (keeps the anti-false-positive bias): currency/
     percent/txhash ALSO ground against a raw number of the same value,
     because tools often return a number with no unit (JSON `"port": 8443`).
  3. A ratio (N of M) is RELATIONAL: it is only GROUNDED if the whole ratio
     co-occurs in the ledger. If both operands exist separately but the
     ratio itself does not, the verdict is COMPOSED (real operands, a
     relation the model constructed) -- the exact lesson of the 72/76 case.

MATCHER v2.1 (2026-09-10 -- structural types):
  digit_core() flattened txhash/ipv4 down to digits only, so a hallucinated
  hash would ground against a coincidentally matching number, and a
  hallucinated IP would ground against a DIFFERENT IP whose digits happen
  to concatenate the same way (a6281d89 vs. the byte-count 628189; 1.22.3.4
  vs. 12.2.3.4). This is a PRECISION fix, not a sensitivity increase (the
  pre-mortem: raising sensitivity kills the guard through alarm fatigue) --
  these types now match on the full normalized token (lowercase hex /
  canonical dotted quad), everything else keeps the value-level leniency.
  Closes a false negative in the class with the highest reputational stake
  (a receipt in a disclosure) at ~zero false-positive cost.

VERDICT RULE (per token):
  - typed fact with a compatible core+type in the ledger -> GROUNDED (ok)
  - ratio whose operands exist but the ratio itself does not -> COMPOSED
    (sev 1, blocks: a relation the model constructed)
  - no grounding, but the line declares INFERRED/RECALL/estimate/unverified
    /band -> DECLARED (ok)
  - no grounding and the line says OBSERVED -> FALSE_OBSERVED
    (sev 3: claimed to see what it did not)
  - no grounding and no tag -> UNSOURCED
    (sev 2, the silent hallucination)

HONEST CEILING (no claim beyond this):
  1. Catches HARD facts (numbers/receipts), not soft hallucination: a wrong
     causal claim with no numeric token ("the site requires login" when it
     does not) passes through untouched. A smoke detector for the class
     that caused the original incident, not proof of semantic correctness.
  2. The raw-number leniency (rule 2) is a deliberate false negative: "R$
     8.443" grounds against a ledger that only has the raw "8443" (e.g. a
     port number). A conscious choice to never block wrongly (a guard that
     cries wolf gets disabled). EXPLICIT unit confusion (a conflicting unit
     present in the ledger, e.g. 92%) is caught; IMPLICIT confusion (a bare
     raw number) is not.
  3. A value DERIVED by arithmetic (sums, timezone conversions) is not a
     ledger fact and is not modeled yet: "total R$ 82,05" computed from two
     summed prices will flag as UNSOURCED if the total itself is not in the
     ledger. Tag the line as an estimate/sum to escape it (becomes DECLARED).
  4. Runs on manual invocation only (layer 1). The real backstop is wiring
     this as a Stop-hook (layer 2) that intercepts output before it leaves.
     Disanalogy with Whonix's soft-vs-hard split: this is declarative, not
     compiled.

Stdlib only. `--selftest` reproduces the bus case, the 72/76 case, unit
confusion, and false grounding by digit concatenation (all of these must
FLAG), plus grounded cases that must pass.
"""
import argparse
import json
import re
import sys

# ---- danger tokens: what an operational hallucination usually asserts ----
DANGER = [
    ("currency",   re.compile(r'(?:R\$|US?\$)\s?\d[\d.,]*\d|\bR\$\s?\d\b')),
    ("ratio",      re.compile(r'\b\d+\s*(?:de|of|/)\s*\d+\b')),  # "de" = PT "of", bilingual by design
    ("time",       re.compile(r'\b\d{1,2}:\d{2}\b')),
    ("percent",    re.compile(r'\b\d[\d.,]*\s*%')),
    ("ipv4",       re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')),
    ("txhash",     re.compile(r'\b[0-9a-fA-F]{8,}\b')),
    ("date",       re.compile(r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b')),
]

# tags that DECLARE uncertainty -> the token is no longer asserted as a live fact.
# Matches both English and Portuguese hedge words by design (the guard accepts
# drafts in either language); do not prune the Portuguese alternatives below,
# that would silently weaken matching on non-English input.
EXEMPT = re.compile(
    r'\b(INFERRED|RECALL|estimativa|estimado|estimate|estimated|'
    r'n[aã]o[- ]verificad|unverified|prov[aá]vel|banda|range|'
    r'\+/-|soma|somado|derivad|convertid|approx|aproximad|talvez|acho que)\b',
    re.IGNORECASE)
EXEMPT_SYM = re.compile(r'±')

# tag that ASSERTS direct observation -> with no grounding this is the worst case
OBSERVED = re.compile(r'\bOBSERVED\b', re.IGNORECASE)

# types a RAW number (no unit in the ledger) is allowed to ground against
# (deliberate leniency, honest ceiling #2). time/ipv4/date require a real match.
COMPAT = {
    "currency": {"currency", "number"},
    "percent":  {"percent", "number"},
    "txhash":   {"txhash", "number"},
    "time":     {"time"},
    "ipv4":     {"ipv4"},
    "date":     {"date"},
}

_NUM = re.compile(r'\d[\d.,]*\d|\d')
_RATIO_SPLIT = re.compile(r'\s*(?:de|of|/)\s*')


def digit_core(s):
    """Numeric signature of a token: digits only, len >= 2."""
    d = re.sub(r'\D', '', s)
    return d if len(d) >= 2 else ''


# STRUCTURAL types: the non-digit characters ARE the identity -- the hex
# letters of a hash, the position of the dots in an IP. digit_core() would
# flatten these down to digits only and ground a WRONG receipt against a
# coincidentally matching number (a6281d89 vs. the byte-count 628189;
# 1.22.3.4 vs. the different IP 12.2.3.4). These match on the full
# normalized TOKEN, never on digit_core. False negative confirmed against
# a live case.
STRUCTURAL = {"txhash", "ipv4"}


def norm_core(s, kind):
    """Identity signature of a token. Structural kinds (txhash/ipv4) normalize
    the whole token; everything else falls back to digit_core (digits only,
    the value-level leniency)."""
    if kind == "txhash":
        h = re.sub(r'[^0-9a-fA-F]', '', s).lower()
        return h if len(h) >= 2 else ''
    if kind == "ipv4":
        octs = re.findall(r'\d{1,3}', s)
        return '.'.join(str(int(o)) for o in octs) if len(octs) == 4 else ''
    return digit_core(s)


def _ops(tok):
    """Operands of a ratio, digits only, len >= 1 (a single digit is valid
    in a ratio)."""
    out = []
    for p in _RATIO_SPLIT.split(tok):
        d = re.sub(r'\D', '', p)
        if d:
            out.append(d)
    return out


def ledger_digits(ledger):
    """DEPRECATED (v1). Flattened digit stream. Kept only for import compatibility."""
    return re.sub(r'\D', '', ledger or '')


def ledger_facts(ledger):
    """
    Discrete, TYPED numeric facts from the ledger (v2). No concatenation:
    each typed fact is masked before scanning for raw numbers, so a '92%'
    does not turn into a phantom raw '92'.
    Returns (facts, ratio_pairs): facts=[(core, kind)], ratio_pairs=[(op, ...)].
    """
    text = ledger or ""
    facts = []
    ratio_pairs = []
    masked = list(text)
    for kind, rx in DANGER:
        for m in rx.finditer(text):
            span = m.group(0)
            if kind == "ratio":
                ops = _ops(span)
                if ops:
                    ratio_pairs.append(tuple(ops))
                    for o in ops:
                        if len(o) >= 2:
                            facts.append((o, "number"))
            else:
                core = norm_core(span, kind)
                if core:
                    facts.append((core, kind))
                    # a purely-decimal txhash is also usable as a raw number
                    if kind == "txhash" and span.isdigit():
                        facts.append((digit_core(span), "number"))
            for i in range(m.start(), m.end()):
                masked[i] = ' '
    rest = ''.join(masked)
    for m in _NUM.finditer(rest):
        core = digit_core(m.group(0))
        if core:
            facts.append((core, "number"))
    return facts, ratio_pairs


def extract(draft):
    """(token, kind, line_text) per danger token, deduped by (token, line)."""
    out, seen = [], set()
    for line in draft.splitlines():
        for kind, rx in DANGER:
            for m in rx.finditer(line):
                tok = m.group(0).strip()
                key = (tok, line)
                if key in seen:
                    continue
                seen.add(key)
                out.append((tok, kind, line))
    return out


def _match(tok, kind, facts, ratio_pairs):
    """(grounded, composed) against the ledger's discrete facts."""
    if kind == "ratio":
        ops = _ops(tok)
        if ops and any(set(ops) <= set(p) for p in ratio_pairs):
            return True, False
        cores = {fc for fc, _ in facts}
        if ops and all(o in cores for o in ops):
            return False, True
        return False, False
    core = norm_core(tok, kind)
    if not core:
        return False, False
    compat = COMPAT.get(kind, {"number"})
    if any(fc == core and fk in compat for fc, fk in facts):
        return True, False
    return False, False


def _exempt(line):
    return bool(EXEMPT.search(line) or EXEMPT_SYM.search(line))


def classify(tok, kind, line, facts, ratio_pairs):
    grounded, composed = _match(tok, kind, facts, ratio_pairs)
    if grounded:
        return "GROUNDED"
    if _exempt(line):
        return "DECLARED"          # stated as uncertain, honest (covers composed too)
    if composed:
        return "COMPOSED"          # real operands, the relation is not in the source
    if OBSERVED.search(line):
        return "FALSE_OBSERVED"    # claimed to see what the ledger doesn't have
    return "UNSOURCED"             # the silent hallucination


SEVERITY = {"FALSE_OBSERVED": 3, "UNSOURCED": 2, "COMPOSED": 1,
            "DECLARED": 0, "GROUNDED": 0}


def check(draft, ledger):
    facts, ratio_pairs = ledger_facts(ledger)
    findings = []
    for tok, kind, line in extract(draft):
        verdict = classify(tok, kind, line, facts, ratio_pairs)
        findings.append({
            "token": tok, "kind": kind, "verdict": verdict,
            "severity": SEVERITY[verdict], "line": line.strip()[:120],
        })
    flagged = [f for f in findings if f["severity"] > 0]
    flagged.sort(key=lambda f: -f["severity"])
    return {
        "tokens": len(findings),
        "grounded": sum(1 for f in findings if f["verdict"] == "GROUNDED"),
        "declared": sum(1 for f in findings if f["verdict"] == "DECLARED"),
        "composed": sum(1 for f in findings if f["verdict"] == "COMPOSED"),
        "flagged": flagged,
        "verdict": "HALLUCINATION_RISK" if flagged else "GROUNDED_OK",
        "block": bool(flagged),
    }


def render(r):
    lines = [
        f"grounding: {r['verdict']}",
        f"  danger-tokens: {r['tokens']}  |  grounded: {r['grounded']}  "
        f"|  declared-uncertain: {r['declared']}  |  composed: {r.get('composed', 0)}  "
        f"|  flagged: {len(r['flagged'])}",
    ]
    if r["flagged"]:
        lines.append("  --- FLAGGED (no discrete grounding in ledger) ---")
        label = {3: "FALSE_OBSERVED!", 2: "UNSOURCED", 1: "COMPOSED"}
        for f in r["flagged"]:
            tag = label[f["severity"]]
            lines.append(f"  [{tag}] {f['kind']:8} {f['token']!r}")
            lines.append(f"           line: {f['line']!r}")
    else:
        lines.append("  every danger-token grounds against a discrete ledger fact or was declared uncertain.")
    return "\n".join(lines)


# ------------------------------- selftest -------------------------------
BUS_DRAFT = """
carrier New Horizon, Executive service
departure 20:00 (Cuiaba), arrival 23:40 (Rondonopolis)
fare R$ 72,19 + fee R$ 9,86 = total R$ 82,05
seats free 27 of 46
"""
BUS_LEDGER = "bus-ticket vendor homepage, 59320 bytes, csrf token, laravel session"

GROUNDED_DRAFT = """
carrier LogTrans, seat Standard
departure 06:00 arrival 10:08, price R$ 79,62
"""
GROUNDED_LEDGER = ("bus marketplace route cuiaba-rondonopolis LogTrans Standard "
                   "06:00 10:08 R$ 79,62 other departures 09:00 12:30")

DECLARED_DRAFT = "monthly profit ~R$ 5000 (INFERRED, estimate, band 3k-8k)"


def selftest():
    cases = []

    # --- cases inherited from v1 (must not regress) ---
    r = check(BUS_DRAFT, BUS_LEDGER)
    cases.append(("bus_flag", r["block"] is True and len(r["flagged"]) >= 5))
    toks = {f["token"] for f in r["flagged"]}
    cases.append(("bus_price_flagged", any("82,05" in t for t in toks)))
    cases.append(("bus_ratio_flagged", any("27" in t and "46" in t for t in toks)))
    cases.append(("bus_time_flagged", "20:00" in toks or "23:40" in toks))

    r = check(GROUNDED_DRAFT, GROUNDED_LEDGER)
    cases.append(("grounded_pass", r["block"] is False))
    cases.append(("grounded_verdict", r["verdict"] == "GROUNDED_OK"))

    r = check(DECLARED_DRAFT, "")
    cases.append(("declared_exempt", r["block"] is False))

    r = check("exit Tor OBSERVED 192.42.116.102", "empty ledger, no matching ip")
    cases.append(("false_observed_flagged",
                  any(f["verdict"] == "FALSE_OBSERVED" for f in r["flagged"])))

    r = check("exit Tor OBSERVED 192.42.116.102", "verify ip 192.42.116.102 IsTor true")
    cases.append(("observed_with_ledger_ok", r["block"] is False))

    r = check("the right strategy here is to map the agenda before acting", "")
    cases.append(("negative_control", r["tokens"] == 0 and r["block"] is False))

    # --- v2: what v1 let through via concatenation/unit confusion ---

    # (A) false grounding via concatenation: '2746' exists in the '8027'+'46'
    #     stream, but 27 is NOT a discrete fact. v1 -> GROUNDED (wrong). v2 -> flags it.
    r = check("found 27 of 46 hosts alive",
              "scan port 8027 and 46 hosts responded to ping")
    cases.append(("concat_false_ground_caught", r["block"] is True))

    # (B) composed relation (the 72/76 case): real operands exist as separate
    #     percentages, but the ratio itself is not in the source -> COMPOSED, blocks.
    r = check("SAFE scores 72/76",
              "SAFE beats the human baseline: 72% accuracy, 76% win rate on disagreements")
    cases.append(("composed_relation_verdict",
                  any(f["verdict"] == "COMPOSED" for f in r["flagged"])))
    cases.append(("composed_relation_blocks", r["block"] is True))

    # (B2) same ratio, but declared uncertain -> DECLARED, does not block
    r = check("SAFE scores ~72/76 (estimate)",
              "SAFE beats the human baseline: 72% accuracy, 76% win rate on disagreements")
    cases.append(("composed_declared_ok", r["block"] is False))

    # (B3) ratio that ACTUALLY co-occurs in the ledger -> GROUNDED
    r = check("seats free 27 of 46",
              "seats: 27 of 46 available on the bus")
    cases.append(("ratio_cooccurs_grounded", r["block"] is False))

    # (C) explicit unit confusion: R$ 92 against a ledger that only has 92%
    r = check("charges R$ 92 per hour", "coverage was 92% in the test")
    cases.append(("unit_mismatch_caught", r["block"] is True))

    # (D) deliberate leniency: a RAW number with no unit grounds a currency
    #     claim (honest ceiling #2 -- never introduce a false positive)
    r = check("the retainer closes at R$ 20.000/month",
              "estimator returned a central value of 20000 per month")
    cases.append(("bare_number_lenient_pass", r["block"] is False))

    # (E) time requires a real match: 20:00 does NOT ground against the raw digits 2000
    r = check("departure at 20:00", "result 2000 points in the test")
    cases.append(("time_needs_real_time", r["block"] is True))

    # --- v2.1: STRUCTURAL types -- the non-digit characters ARE the identity ---
    # digit_core() flattens hash/IP/date down to digits only; the hex letters, the
    # dots, and the slashes ARE the identity. Without this the guard would ground a
    # WRONG receipt against a coincidentally matching number -- the class with the
    # highest reputational stake (a tx/IP/date in a disclosure). False negative
    # confirmed against a live case.

    # (F) txhash: a hallucinated hash does NOT ground against a raw number of coincident digits
    r = check("restore tx OBSERVED a6281d89", "run log line 628189 bytes")
    cases.append(("txhash_hex_letters_matter", r["block"] is True))

    # (F2) a REAL hash that is in the ledger still grounds (anti-overshoot guard)
    r = check("restore tx OBSERVED a6281d89", "tx confirmed a6281d89 on the explorer")
    cases.append(("txhash_real_still_grounds", r["block"] is False))

    # (G) ipv4: a hallucinated IP does NOT ground against a DIFFERENT IP with matching concatenated digits
    r = check("exit Tor OBSERVED 1.22.3.4", "verify ip 12.2.3.4 IsTor true")
    cases.append(("ipv4_dot_positions_matter", r["block"] is True))

    # (G2) a REAL IP that is in the ledger still grounds (anti-overshoot guard)
    r = check("exit Tor OBSERVED 12.2.3.4", "verify ip 12.2.3.4 IsTor true")
    cases.append(("ipv4_real_still_grounds", r["block"] is False))

    passed = sum(1 for _, ok in cases if ok)
    for name, ok in cases:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\nselftest: {passed}/{len(cases)}")
    return passed == len(cases)


def main():
    ap = argparse.ArgumentParser(description="anti-hallucination grounding guard (v2, discrete-fact matcher)")
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="ground a draft against this turn's tool-output ledger")
    c.add_argument("--draft", required=True, help="file containing the draft response")
    c.add_argument("--ledger", help="file containing this turn's tool outputs (omit if none)")
    c.add_argument("--json", action="store_true")

    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    if args.cmd == "check":
        with open(args.draft, encoding="utf-8") as f:
            draft = f.read()
        ledger = ""
        if args.ledger:
            with open(args.ledger, encoding="utf-8") as f:
                ledger = f.read()
        r = check(draft, ledger)
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(render(r))
        sys.exit(1 if r["block"] else 0)

    ap.print_help()


if __name__ == "__main__":
    main()
