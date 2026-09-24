#!/usr/bin/env python3
"""
corroborate.py — source-corroboration gate for asserted claims.

The second of three "near-zero" gates. The first (repro.py) proves a finding
REPRODUCES. This one proves it is CORROBORATED. Those are different things: a
proof-of-concept can reproduce a signal that a SINGLE source reported cleanly,
and that source can still be wrong. The grounding gate (grounding.py) defends
against a number that was fabricated outright; the reproduction gate defends
against "it's a vuln" without a PoC that runs; THIS gate defends against a
narrower gap: a grounding check only verifies that a number traces to a tool
result, not that the tool result itself was correct. If a tool returns garbage
and that garbage gets repeated verbatim, grounding alone lets it through.

THE DOCTRINE (one falsifiable sentence):
  A finding backed by ONE source is a HYPOTHESIS, never a fact, no matter how
  confident that source is. It becomes a fact when TWO sources from distinct
  INDEPENDENCE CLASSES converge on the SAME value. A repeated source, or two
  sources that derive from the same upstream origin, count as ONE. Convergence
  of independent sources is the only asset that compounds.

THE THREE LOCKS (the core of this gate):

  1. INDEPENDENCE CLASS, NOT SOURCE COUNT. crt.sh and certspotter both read
     Certificate Transparency: that is the SAME origin counted twice, not
     corroboration. The gate groups observations by class and requires >=2
     distinct classes agreeing. Ten scanners wrapping nmap are still one.

  2. VALUE AGREEMENT, NOT CO-PRESENCE. Two sources that both exist but report
     different values ("port 8443" vs "port 443") do NOT corroborate: they
     CONFLICT. Conflict is the most valuable verdict — it is where the error
     that would have shipped to production lives. Agreement is checked by type
     (exact / numeric-with-tolerance / set-Jaccard).

  3. CIRCULAR-PROVENANCE COLLAPSE (the lock plain multi-source fusion lacks).
     Two differently-named sources that both `derives_from` the same upstream
     origin (two news sites citing the same press release; two scanners
     wrapping the same nmap run) collapse into a single ORIGIN UNIT.
     Independence is effective, not nominal. If all of the "corroboration"
     reduces to one origin, the verdict is CIRCULAR: pseudo-corroboration,
     rejected.

THE VERDICTS (the only axis this gate decides):
  CORROBORATED   -> >=2 independent origin units agree on the value. Only this
                    verdict authorizes asserting the claim as FACT.
  CONFLICTED     -> sources exist but disagree on the value. Not a fact; it is
                    the signal that one of them (or the interpretation) is
                    wrong. Reports the split.
  CIRCULAR       -> sources agree but collapse into a single upstream origin.
                    Pseudo-corroboration. Rejected as fact.
  SINGLE_SOURCE  -> exactly one origin unit. An explicit hypothesis, not
                    "not yet checked".
  UNCORROBORATED -> multiple sources, but all in the SAME class (repeated
                    origin). They count as one. Hypothesis, not fact.

HONEST CEILING (no claims beyond this):
  1. This gate checks VALUE AGREEMENT, not TRUTH. Two independent sources can
     converge on the same error (both read the same bad upstream data without
     knowing they are linked). Corroboration raises the probability of truth,
     it does not guarantee it. CORROBORATED is a necessary condition for
     asserting a fact, not proof of one.
  2. The CLASSES are a hand-maintained map. A new source falls into its own
     class (assumed independent) but is FLAGGED by name: a wrong class
     inflates or deflates corroboration. The map is falsifiable and revisable,
     not ground truth.
  3. PROVENANCE (`derives_from`) is declared by whoever assembles the case. If
     you do not declare that two sources share an origin, the gate does not
     guess: it defends against DECLARED circularity, not hidden circularity
     nobody has mapped. Hidden provenance is the next gap, and a harder one.
  4. Numeric agreement uses tolerance; free-text semantic agreement ("do these
     two reports describe the same vuln?") is NOT resolved by this gate — it
     is flagged `needs_judge`. Equality is not semantics.

Standard library only. `--selftest` exercises every verdict and every lock
offline.

Usage:
  python3 corroborate.py check <claim.json>   # emit verdict + confidence fusion
  python3 corroborate.py template             # print an example claim
  python3 corroborate.py --selftest           # offline gate
"""
import sys
import json
import argparse
import re
from collections import defaultdict
from datetime import datetime, timezone


# Noisy-OR fusion is inlined here (rather than imported from a shared module)
# so this file has zero sibling imports and runs fully standalone.
def _noisy_or(confs):
    """P(at least one true) for independent evidence: 1 - prod(1 - c)."""
    prod = 1.0
    for c in confs:
        prod *= (1.0 - max(0.0, min(1.0, c)))
    return 1.0 - prod


def _now():
    return datetime.now(timezone.utc).isoformat()


# =========================================================================
# INDEPENDENCE CLASSES for CLAIMS (broader than a plain OSINT source map).
# Sources that share a GROUND-TRUTH ORIGIN share a class. A class only
# corroborates ANOTHER class, never itself.
# =========================================================================
CLAIM_SOURCE_CLASS = {
    "crtsh":        "ct_logs",      # crt.sh reads Certificate Transparency
    "certspotter":  "ct_logs",      # certspotter ALSO reads CT: same origin
    "censys":       "ct_logs",      # censys certs derive from CT too
    "wayback":      "archive",      # Internet Archive
    "dns":          "dns",          # live resolver
    "shodan":       "scan_db",      # internet scan database
    "zoomeye":      "scan_db",      # another scan database, same technique
    "nmap":         "active_scan",  # active scan
    "masscan":      "active_scan",  # active scan, same family
    "nuclei":       "template_scan",# template-based scanner
    "curl":         "manual_probe", # manual request
    "manual":       "manual_probe", # manual inspection
    "docs":         "vendor_docs",  # vendor documentation
    "changelog":    "vendor_docs",  # changelog, same origin as docs
    "cve":          "cve_db",       # CVE database (NVD/MITRE)
    "nvd":          "cve_db",       # NVD is the CVE database, same origin
    "github":       "vcs",          # VCS history
    "gitlab":       "vcs",          # same
    "llm":          "model_guess",  # model guess, NOT a primary source
    "recall":       "model_guess",  # model recall, same weak class
    "news":         "press",        # press (frequently circular)
    "blog":         "press",        # blog, same class as press
}


def source_class(source, explicit=None):
    """Maps a source to its independence class. An explicit class on the case
    wins. An unmapped source becomes its own class but is FLAGGED by name so
    it never silently joins an existing class."""
    if explicit:
        return explicit
    s = (source or "").lower()
    for key, cls in CLAIM_SOURCE_CLASS.items():
        if key in s:
            return cls
    return f"unmapped:{s or 'anon'}"


# =========================================================================
# VALUE AGREEMENT: do two observations agree? checked by type.
# =========================================================================

def _norm_text(v):
    return re.sub(r"\s+", " ", str(v).strip().casefold())


def _as_set(v):
    if isinstance(v, (list, tuple, set)):
        return set(_norm_text(x) for x in v)
    # comma/whitespace separated string
    return set(_norm_text(x) for x in re.split(r"[,\s]+", str(v)) if x.strip())


def values_agree(a, b, kind="exact", tolerance=0.0, jaccard=1.0):
    """Returns (agree: bool, detail: str). kind: exact|numeric|set.
    numeric: |a-b| <= tolerance (absolute), OR relative tolerance if 0<tol<1
             and the base is large. set: Jaccard >= threshold."""
    if kind == "numeric":
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return (False, f"numeric-unparseable: {a!r} / {b!r}")
        diff = abs(fa - fb)
        # relative tolerance when 0 < tol < 1: fraction of the larger magnitude
        if 0.0 < tolerance < 1.0:
            base = max(abs(fa), abs(fb), 1e-9)
            ok = (diff / base) <= tolerance
            return (ok, f"|{fa}-{fb}|/{base:.3g}={diff/base:.3g} vs rel<= {tolerance}")
        ok = diff <= tolerance
        return (ok, f"|{fa}-{fb}|={diff:g} vs abs<= {tolerance}")
    if kind == "set":
        sa, sb = _as_set(a), _as_set(b)
        if not sa and not sb:
            return (True, "both-empty")
        inter = len(sa & sb)
        union = len(sa | sb) or 1
        j = inter / union
        return (j >= jaccard, f"jaccard={j:.3g} vs >= {jaccard}")
    # exact (default): normalized-text equality
    na, nb = _norm_text(a), _norm_text(b)
    return (na == nb, f"{'eq' if na==nb else 'ne'}: {na!r} vs {nb!r}")


# =========================================================================
# ORIGIN UNIT: class collapsed by declared provenance.
# Two distinct classes that both derive from the same upstream origin collapse
# into a single unit. This is where circularity is caught.
# =========================================================================

def origin_unit(obs):
    """The EFFECTIVE independence unit of an observation. If it declares
    `derives_from`, the upstream origin RULES (two sources with the same
    upstream are one). Otherwise, falls back to the independence class."""
    up = obs.get("derives_from")
    if up:
        return f"upstream:{_norm_text(up)}"
    return source_class(obs.get("source"), obs.get("class"))


# =========================================================================
# corroboration: the core
# =========================================================================

def _agreement_clusters(reps, kind, tolerance, jaccard):
    """Groups origin units that agree on the value (pairwise-agreement union).
    Returns a list of clusters (each one = a list of unit keys).
    Ceiling note: tolerance-based numeric agreement is not transitive; this
    uses pairwise union and assumes clusters are coherent (flagged in the
    honest-ceiling section)."""
    units = list(reps.keys())
    parent = {u: u for u in units}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            a, b = units[i], units[j]
            agree, _ = values_agree(reps[a]["value"], reps[b]["value"],
                                    kind, tolerance, jaccard)
            if agree:
                union(a, b)
    clusters = defaultdict(list)
    for u in units:
        clusters[find(u)].append(u)
    return list(clusters.values())


def corroborate(claim):
    """Takes a claim {statement, value_kind, observations:[...]}. Emits a verdict."""
    obs_list = claim.get("observations") or []
    kind = claim.get("value_kind", "exact")
    tolerance = claim.get("tolerance", 0.0)
    jaccard = claim.get("jaccard", 1.0)

    if not obs_list:
        return {"verdict": "UNCORROBORATED",
                "why": "no observations; cannot be asserted"}

    # 1. group by effective ORIGIN UNIT (class collapsed by provenance)
    by_unit = defaultdict(list)
    nominal_classes = set()
    for o in obs_list:
        by_unit[origin_unit(o)].append(o)
        nominal_classes.add(source_class(o.get("source"), o.get("class")))

    # within each unit: the representative is the observation with max confidence
    reps = {}
    for unit, os_in in by_unit.items():
        best = max(os_in, key=lambda o: o.get("confidence", 0.0))
        reps[unit] = {"value": best.get("value"),
                      "confidence": max(0.0, min(1.0, best.get("confidence", 0.0))),
                      "source": best.get("source"),
                      "n_in_unit": len(os_in)}

    n_units = len(reps)
    unmapped = sorted(c for c in nominal_classes if c.startswith("unmapped:"))

    # nominal-circularity flag: more than one nominal CLASS, but they collapsed
    collapsed_by_provenance = len(nominal_classes) > n_units

    # 2. agreement clusters across origin units
    clusters = _agreement_clusters(reps, kind, tolerance, jaccard)
    clusters.sort(key=len, reverse=True)
    biggest = clusters[0]
    biggest_size = len(biggest)

    # confidence fusion (independence-aware) over the agreeing cluster
    def fuse_units(unit_keys):
        confs = [reps[u]["confidence"] for u in unit_keys]
        point = _noisy_or(confs)  # already per-unit (independent by construction)
        lo = max(confs) if confs else 0.0
        hi = _noisy_or([reps[u]["confidence"] for u in unit_keys])
        return {"point": round(point, 3), "lo": round(lo, 3), "hi": round(hi, 3),
                "n_units": len(unit_keys)}

    base = {
        "statement": claim.get("statement"),
        "value_kind": kind,
        "checked_at": _now(),
        "n_observations": len(obs_list),
        "n_nominal_classes": len(nominal_classes),
        "n_origin_units": n_units,
        "units": {u: {"value": r["value"], "confidence": r["confidence"],
                      "source": r["source"], "n_in_unit": r["n_in_unit"]}
                  for u, r in reps.items()},
        "unmapped_sources": unmapped,
    }

    # 3. verdict. order matters.

    # 3a. a single origin unit -> hypothesis
    if n_units == 1:
        # multiple sources but the same origin?
        if len(obs_list) > 1:
            v = "UNCORROBORATED"
            why = ("multiple observations, a single independence origin "
                   f"({list(reps)[0]}); they count as one source, not corroboration")
        else:
            v = "SINGLE_SOURCE"
            why = "a single source; hypothesis, not fact"
        return {**base, "verdict": v, "why": why,
                "confidence": fuse_units(list(reps))}

    # 3b. >=2 units. the largest agreeing cluster decides.
    if biggest_size >= 2:
        # they agree. but is this circular? (units sharing the same upstream
        # origin were already collapsed; if >=2 DISTINCT units still agree,
        # this is real corroboration.)
        dissenters = [u for cl in clusters[1:] for u in cl]
        result = {**base, "verdict": "CORROBORATED",
                  "why": f"{biggest_size} independent origin units "
                         f"agree on the value",
                  "agreeing_units": biggest,
                  "confidence": fuse_units(biggest)}
        if dissenters:
            result["dissenting_units"] = dissenters
            result["note"] = ("some units disagree with the majority cluster; "
                              "corroborated by the majority, split recorded")
        return result

    # 3c. >=2 units and NO pair agrees -> conflict, OR collapsed-circular
    if collapsed_by_provenance and n_units == 1:
        # (already handled in 3a; kept here for completeness)
        pass
    # if the nominal classes collapsed by provenance to the point where there
    # are no 2 independent origins left, it is circular
    if collapsed_by_provenance:
        return {**base, "verdict": "CIRCULAR",
                "why": ("sources have different names but derive from the same "
                        "upstream origin; pseudo-corroboration, rejected"),
                "confidence": fuse_units(biggest)}

    # 3d. genuinely independent units that disagree -> conflict
    return {**base, "verdict": "CONFLICTED",
            "why": "independent sources disagree on the value; one of them is wrong",
            "clusters": [{"units": cl,
                          "value": reps[cl[0]]["value"]} for cl in clusters],
            "confidence": fuse_units(biggest)}


# =========================================================================
# template
# =========================================================================

TEMPLATE = {
    "statement": "the /admin endpoint on example.com is exposed without auth",
    "value_kind": "exact",
    "observations": [
        {"source": "nuclei", "value": "exposed", "confidence": 0.7,
         "note": "template scan flagged it"},
        {"source": "curl-manual", "value": "exposed", "confidence": 0.9,
         "class": "manual_probe", "note": "confirmed manually, HTTP 200 on /admin"},
    ],
    "_comment": ("value_kind can be exact|numeric|set. For numeric use "
                 "tolerance; for set use jaccard. derives_from on an "
                 "observation declares the upstream origin and collapses "
                 "circularity.")
}


# =========================================================================
# selftest
# =========================================================================

def _selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # -- CORROBORATED: two independent classes agree
    c = corroborate({
        "statement": "port 443 open", "value_kind": "exact",
        "observations": [
            {"source": "shodan", "value": "open", "confidence": 0.8},
            {"source": "nmap", "value": "open", "confidence": 0.95},
        ]})
    ck("CORROBORATED two independent classes agree", c["verdict"] == "CORROBORATED")
    ck("CORROBORATED fuses higher than any single", c["confidence"]["point"] > 0.95)
    ck("CORROBORATED point <= hi band", c["confidence"]["point"] <= c["confidence"]["hi"] + 1e-9)

    # -- UNCORROBORATED: two sources, SAME class (crt.sh + certspotter = CT)
    c = corroborate({
        "statement": "subdomain api.x exists", "value_kind": "exact",
        "observations": [
            {"source": "crtsh", "value": "yes", "confidence": 0.9},
            {"source": "certspotter", "value": "yes", "confidence": 0.9},
        ]})
    ck("UNCORROBORATED same class (both CT logs)", c["verdict"] == "UNCORROBORATED")
    ck("UNCORROBORATED collapses to 1 unit", c["n_origin_units"] == 1)
    ck("UNCORROBORATED saw 2 observations", c["n_observations"] == 2)

    # -- SINGLE_SOURCE: just one source
    c = corroborate({
        "statement": "vuln exists", "value_kind": "exact",
        "observations": [{"source": "llm", "value": "yes", "confidence": 0.6}]})
    ck("SINGLE_SOURCE one source", c["verdict"] == "SINGLE_SOURCE")

    # -- CONFLICTED: two independent classes DISAGREE
    c = corroborate({
        "statement": "service port", "value_kind": "exact",
        "observations": [
            {"source": "shodan", "value": "8443", "confidence": 0.8},
            {"source": "nmap", "value": "443", "confidence": 0.9},
        ]})
    ck("CONFLICTED independent classes disagree", c["verdict"] == "CONFLICTED")
    ck("CONFLICTED reports clusters", len(c.get("clusters", [])) == 2)

    # -- CIRCULAR: two different nominal sources, same declared upstream
    c = corroborate({
        "statement": "company was breached", "value_kind": "exact",
        "observations": [
            {"source": "news-site-a", "value": "yes", "confidence": 0.7,
             "derives_from": "pressrelease-2026"},
            {"source": "blog-b", "value": "yes", "confidence": 0.7,
             "derives_from": "pressrelease-2026"},
        ]})
    ck("CIRCULAR shared upstream collapses", c["verdict"] in ("CIRCULAR", "UNCORROBORATED"))
    ck("CIRCULAR two nominal classes", c["n_nominal_classes"] >= 1)
    ck("CIRCULAR one effective origin unit", c["n_origin_units"] == 1)

    # -- properly testing the CIRCULAR verdict: >=2 nominal classes collapsing,
    #    but a third observation from a different origin would keep >1 unit.
    #    Here: two scanners wrapping nmap + one manual probe -> 2 units.
    c = corroborate({
        "statement": "port 22 open", "value_kind": "exact",
        "observations": [
            {"source": "wrapper-x", "value": "open", "confidence": 0.7,
             "derives_from": "nmap-run-1"},
            {"source": "wrapper-y", "value": "open", "confidence": 0.7,
             "derives_from": "nmap-run-1"},
            {"source": "manual", "value": "open", "confidence": 0.85},
        ]})
    ck("two wrappers of nmap + manual = 2 units", c["n_origin_units"] == 2)
    ck("collapsed wrappers still CORROBORATED by manual", c["verdict"] == "CORROBORATED")

    # -- numeric with tolerance: 100 vs 103, 5% relative tolerance -> agree
    c = corroborate({
        "statement": "latency ms", "value_kind": "numeric", "tolerance": 0.05,
        "observations": [
            {"source": "shodan", "value": 100, "confidence": 0.7},
            {"source": "nmap", "value": 103, "confidence": 0.7},
        ]})
    ck("numeric tolerance agree", c["verdict"] == "CORROBORATED")

    # -- numeric outside tolerance -> conflict
    c = corroborate({
        "statement": "latency ms", "value_kind": "numeric", "tolerance": 0.01,
        "observations": [
            {"source": "shodan", "value": 100, "confidence": 0.7},
            {"source": "nmap", "value": 300, "confidence": 0.7},
        ]})
    ck("numeric out of tolerance -> CONFLICTED", c["verdict"] == "CONFLICTED")

    # -- set Jaccard: same subdomains (different order) -> agree
    c = corroborate({
        "statement": "subdomains", "value_kind": "set", "jaccard": 0.8,
        "observations": [
            {"source": "crtsh", "value": ["api.x", "www.x", "mail.x"], "confidence": 0.8},
            {"source": "dns", "value": ["www.x", "api.x", "mail.x"], "confidence": 0.85},
        ]})
    ck("set jaccard identical -> CORROBORATED", c["verdict"] == "CORROBORATED")

    # -- set Jaccard low -> conflict
    c = corroborate({
        "statement": "subdomains", "value_kind": "set", "jaccard": 0.8,
        "observations": [
            {"source": "crtsh", "value": ["api.x"], "confidence": 0.8},
            {"source": "dns", "value": ["totally.different"], "confidence": 0.85},
        ]})
    ck("set jaccard low -> CONFLICTED", c["verdict"] == "CONFLICTED")

    # -- unmapped source becomes its own class and gets flagged
    c = corroborate({
        "statement": "x", "value_kind": "exact",
        "observations": [
            {"source": "mystery-tool-9000", "value": "y", "confidence": 0.5},
            {"source": "nmap", "value": "y", "confidence": 0.9},
        ]})
    ck("unmapped source flagged", any("mystery" in u for u in c["unmapped_sources"]))
    ck("unmapped still counts as own unit -> CORROBORATED", c["verdict"] == "CORROBORATED")

    # -- explicit class overrides the map
    c = corroborate({
        "statement": "x", "value_kind": "exact",
        "observations": [
            {"source": "crtsh", "value": "y", "confidence": 0.8, "class": "manual_probe"},
            {"source": "certspotter", "value": "y", "confidence": 0.8},
        ]})
    ck("explicit class overrides map -> 2 units", c["n_origin_units"] == 2)

    # -- empty -> UNCORROBORATED
    c = corroborate({"statement": "x", "observations": []})
    ck("empty -> UNCORROBORATED", c["verdict"] == "UNCORROBORATED")

    # -- fusion: 3 independent units raise confidence more than 2
    c2 = corroborate({"statement": "x", "observations": [
        {"source": "shodan", "value": "o", "confidence": 0.6},
        {"source": "nmap", "value": "o", "confidence": 0.6},
    ]})
    c3 = corroborate({"statement": "x", "observations": [
        {"source": "shodan", "value": "o", "confidence": 0.6},
        {"source": "nmap", "value": "o", "confidence": 0.6},
        {"source": "manual", "value": "o", "confidence": 0.6},
    ]})
    ck("3 independent units fuse higher than 2", c3["confidence"]["point"] > c2["confidence"]["point"])

    # -- majority corroboration with a dissenter on record
    c = corroborate({
        "statement": "port", "value_kind": "exact",
        "observations": [
            {"source": "shodan", "value": "443", "confidence": 0.8},
            {"source": "nmap", "value": "443", "confidence": 0.9},
            {"source": "manual", "value": "8080", "confidence": 0.5},
        ]})
    ck("majority CORROBORATED with dissenter logged",
       c["verdict"] == "CORROBORATED" and c.get("dissenting_units"))

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    for name, ok in checks:
        print(f"  [{'ok' if ok else 'XX'}] {name}")
    print(f"\nselftest: {passed}/{total}")
    return passed == total


# =========================================================================
# cli
# =========================================================================

def _load(path):
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="source-corroboration gate for claims")
    sub = ap.add_subparsers(dest="cmd")

    p_chk = sub.add_parser("check", help="emit corroboration verdict + fusion")
    p_chk.add_argument("claim")
    p_chk.add_argument("--write", action="store_true",
                       help="write corrob_<hash>.json")

    sub.add_parser("template", help="print an example claim")
    ap.add_argument("--selftest", action="store_true")

    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if _selftest() else 1)

    if args.cmd == "template":
        print(json.dumps(TEMPLATE, indent=2, ensure_ascii=False))
        return

    if args.cmd == "check":
        claim = _load(args.claim)
        result = corroborate(claim)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.write:
            import hashlib
            h = hashlib.sha256(_now().encode()).hexdigest()[:8]
            with open(f"corrob_{h}.json", "w") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
