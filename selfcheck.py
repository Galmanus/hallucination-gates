#!/usr/bin/env python3
"""
selfcheck.py — semantic self-consistency gate for black-box LLM hallucination detection.

This is one of five composable hallucination gates in this repo (see also
`grounding.py`, `corroborate.py`, `claim.py`, and `repro.py`). The other four are
SINGLE-SAMPLE provenance checks: each looks at ONE generated answer and asks a
narrow question — does this number trace back to a tool result this turn, does
this claim have independent corroboration, does this reproduction actually hold
up.

The state of the art on black-box hallucination (arXiv:2609.02679, 2026-09-02,
"From Tokens to Semantics") names the blind spot every single-sample check
shares: the CONFIDENT CONSISTENT ERROR. Asked once, a fabrication looks
identical to a fact — full confidence, clean prose, no visible seam. A
hallucinated transit detail is a canonical instance: a route name, a departure
time, and a seat count, stated once, with total confidence and zero backing
("Northline Express, 8:00 PM, seat 27 of 46" — invented whole, nothing wrong
with the prose). A single-pass check cannot catch it, because from one sample a
fabrication is indistinguishable from a fact.

The move single-sample checking cannot make: RESAMPLE. Ask the same question
multiple times and measure whether the *meanings* agree (semantic entropy;
Farquhar, Kossen, Kuhn, Gal, Nature 2024). Disagreement across samples means the
model does not actually know. The method's own declared failure mode: semantic
entropy goes uninformative when every sample lands in ONE meaning cluster — the
confident-consistent error again, one level up. So the source paper's own
routing rule applies here too: a convergent (single-cluster) result needs a
COMPLEMENTARY signal and must never be read as confidence by itself.

This module implements that routing for the case where the model under test is
an API black box: there is no access to its residual stream the way a
white-box probe (e.g. Latent Critic, arXiv:2608.10430, a LoRA trained on an
open model's internals) would have. Disanalogy stated explicitly, not hidden.
What black-box resampling CAN do is sample ACROSS INDEPENDENT LLM BACKENDS —
distinct vendors/model families, not just a different random seed on the same
model — and cluster the answers by meaning. Cross-family disagreement is a
stronger signal than same-model temperature resampling, because independent
pretraining distributions tend to fail independently (a noisy-OR assumption),
the same independence-class reasoning `corroborate.py` applies to sources.
That cross-family independence is the actual edge this module adds over
resampling one model at temperature.

THE 2x2 (the core contribution here). Two axes: does the resample CONVERGE (low
semantic entropy), and is the claim GROUNDED (backed by some external
ledger/tool result this turn)?

                 | grounded (ledger)      | ungrounded (no ledger)
  -----------------------------------------------------------------------
  convergent      | ASSERT                 | DANGER  <- confident fabrication lives here
  (agree w/ self) | (fact + models agree)  | confident + unbacked -> FORCE_GROUND
  -----------------------------------------------------------------------
  divergent       | TRUST_TOOL             | ABSTAIN
  (disagree)      | (ledger wins, flag)    | honest "I don't know yet"

The bottom-left / top-right distinction is the whole point. A single-sample
check sees top-left and top-right as identical (both confident). Only
resampling reveals that the top-right agreement is stubbornness, not
knowledge — and when that cell is ALSO ungrounded, it is the single most
dangerous output class a system like this can produce, so it gets the hardest
verdict (FORCE_GROUND: never assert, go get a source).

HONEST CEILING (do not pretend past it):
 - Self-consistency catches VARIABLE hallucination, not DETERMINISTIC confident
   error. If every backend makes the SAME wrong claim (shared training-data
   error, shared prompt injection), they converge on a falsehood, and this gate
   reports ASSERT if that falsehood also happens to look grounded. That
   residual is exactly why an independent corroboration check (`corroborate.py`)
   and a provenance/grounding guard (`grounding.py`) still need to run
   alongside this one. This gate is a COMPLEMENT to those, not a replacement
   for them.
 - Clustering here is a proxy for semantic equivalence, not the real thing:
   offline, it is text normalization plus numeric canonicalization
   (deterministic, no network). True semantic equivalence needs an NLI/judge
   model; the `live` subcommand can route sample pairs through an external
   judge for that, at the cost of a network call and a judge that can itself
   be wrong.
 - Thresholds (`agree_frac`, `min_families`) are chosen knobs, not measured
   constants — labeled as such throughout. The source paper reports results at
   FPR budgets of 1-15%; this module exposes the knob rather than asserting a
   pre-calibrated operating point.

Stdlib only for import and the offline selftest. Live cross-backend sampling is
opt-in, requires wiring an external provider integration, and is
network-gated — see `live_sample` below.
"""

import sys, os, json, math, re, argparse, importlib
from collections import defaultdict

# -----------------------------------------------------------------------------
# semantic clustering (offline proxy: normalize text + canonicalize numbers)
# -----------------------------------------------------------------------------

_NUM_RE = re.compile(r'(?<![\w.])(\d[\d.,]*)\s*(k|mil|m|mi|kk)?', re.IGNORECASE)

def _canon_number(raw, suffix):
    """Canonicalize a number token to a float. Handles pt/en thousand/decimal seps
    and k/m/mil suffixes so '38k' == '38.000' == '38000' == 'R$38 mil'."""
    s = raw.strip()
    suf = (suffix or '').lower()
    # decide separator convention:
    if ',' in s and '.' in s:
        # last separator is the decimal one
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')   # pt: 1.234,56
        else:
            s = s.replace(',', '')                      # en: 1,234.56
    elif ',' in s:
        # comma alone: treat as decimal if 1-2 trailing digits, else thousands
        frac = s.split(',')[-1]
        if len(frac) in (1, 2) and s.count(',') == 1:
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
    elif s.count('.') > 1:
        s = s.replace('.', '')                          # 1.234.567 thousands
    elif '.' in s:
        # dot alone: thousands if exactly 3 trailing digits and integer-looking
        frac = s.split('.')[-1]
        if len(frac) == 3 and s.count('.') == 1 and len(s.replace('.', '')) > 3:
            s = s.replace('.', '')
    try:
        val = float(s)
    except ValueError:
        return None
    mult = {'k': 1e3, 'mil': 1e3, 'm': 1e6, 'mi': 1e6, 'kk': 1e6, '': 1.0}.get(suf, 1.0)
    return val * mult

def _extract_numbers(text):
    out = []
    for m in _NUM_RE.finditer(text):
        v = _canon_number(m.group(1), m.group(2))
        if v is not None:
            out.append(round(v, 6))
    return tuple(sorted(out))

_WS = re.compile(r'\s+')
_PUNCT = re.compile(r'[^\w\s]')

def _norm_text(text):
    t = text.strip().lower()
    # canonicalize numbers inline so "38k" and "38000" normalize together
    def repl(m):
        v = _canon_number(m.group(1), m.group(2))
        return f' <num:{v:.6g}> ' if v is not None else m.group(0)
    t = _NUM_RE.sub(repl, t)
    t = _PUNCT.sub(' ', t)
    t = _WS.sub(' ', t).strip()
    return t

def cluster_key(text, numeric_mode=False):
    """Return the meaning-cluster key for one sample.
    numeric_mode=True: cluster purely by the set of numbers present (for numeric claims
    where wording differs but the number is the fact)."""
    if numeric_mode:
        nums = _extract_numbers(text)
        return ('num', nums)
    return ('txt', _norm_text(text))

# -----------------------------------------------------------------------------
# semantic entropy over meaning clusters
# -----------------------------------------------------------------------------

def semantic_entropy(samples, numeric_mode=False):
    """samples: list of {'text':..., 'family':...}. Returns clustering + entropy stats."""
    clusters = defaultdict(list)
    for s in samples:
        clusters[cluster_key(s['text'], numeric_mode)].append(s)
    n = len(samples)
    sizes = [len(v) for v in clusters.values()]
    # Shannon entropy over cluster probabilities, natural log
    se = 0.0
    for c in sizes:
        p = c / n
        se -= p * math.log(p)
    k = len(clusters)
    se_norm = se / math.log(n) if n > 1 else 0.0        # in [0,1]
    consensus = max(sizes) / n                            # largest cluster fraction
    # which cluster is the plurality, and how many DISTINCT families back it
    top = max(clusters.values(), key=len)
    top_families = {s.get('family', '?') for s in top}
    all_families = {s.get('family', '?') for s in samples}
    return {
        'n_samples': n,
        'n_clusters': k,
        'entropy_nat': round(se, 4),
        'entropy_norm': round(se_norm, 4),      # 0 = perfect agreement, 1 = max spread
        'consensus': round(consensus, 4),        # fraction in the largest meaning cluster
        'top_cluster_families': sorted(top_families),
        'n_top_families': len(top_families),
        'all_families': sorted(all_families),
        'n_families': len(all_families),
        'clusters': {str(k_): len(v) for k_, v in clusters.items()},
    }

# -----------------------------------------------------------------------------
# the 2x2 verdict
# -----------------------------------------------------------------------------

VERDICTS = {
    'ASSERT':       'models converge AND claim is grounded in the tool ledger. Safe to state as fact.',
    'ASSERT_WEAK':  'converge + grounded, but consensus rests on a single model family. State, but weaker independence.',
    'TRUST_TOOL':   'models disagree but a tool ledger backs the claim. Ledger wins; flag the model instability.',
    'FORCE_GROUND': 'DANGER: models agree with high confidence but NOTHING backs it (the confident-fabrication cell). Do not assert. Get a tool source or abstain.',
    'ABSTAIN':      'models disagree AND nothing backs it. Honest "I do not know yet, I need X."',
    'INCONCLUSIVE': 'too few samples to measure self-consistency.',
    'REFUSED':      'malformed case (no claim / no samples).',
}

def decide(stats, grounded, agree_frac=0.75, min_families=2):
    """Route the 2x2. agree_frac and min_families are CHOSEN knobs, not measured."""
    if stats['n_samples'] < 3:
        return 'INCONCLUSIVE', ['need >=3 samples to measure semantic entropy']
    convergent = stats['consensus'] >= agree_frac
    reasons = []
    reasons.append(f"consensus={stats['consensus']:.2f} (threshold {agree_frac}) -> "
                   f"{'convergent' if convergent else 'divergent'}")
    reasons.append(f"entropy_norm={stats['entropy_norm']:.2f}, {stats['n_clusters']} meaning cluster(s)")
    reasons.append(f"grounded={grounded}")
    if convergent and grounded:
        # independence check: convergence across >= min_families is the strong form
        if stats['n_top_families'] >= min_families:
            reasons.append(f"top cluster spans {stats['n_top_families']} independent families -> strong")
            return 'ASSERT', reasons
        reasons.append(f"top cluster spans only {stats['n_top_families']} family -> weak independence")
        return 'ASSERT_WEAK', reasons
    if convergent and not grounded:
        reasons.append("confident but UNBACKED = the confident-consistent-error cell (arXiv:2609.02679)")
        return 'FORCE_GROUND', reasons
    if (not convergent) and grounded:
        reasons.append("models waver but ledger backs it")
        return 'TRUST_TOOL', reasons
    reasons.append("models waver and nothing backs it")
    return 'ABSTAIN', reasons

# -----------------------------------------------------------------------------
# live cross-model sampling (opt-in, network, optional provider integration)
# -----------------------------------------------------------------------------

def _load_provider_backend():
    """Lazily load an optional multi-backend LLM provider module.

    This module ships no live LLM client of its own. The live path needs a way
    to call several INDEPENDENT LLM backends (distinct vendors/model families)
    so the 2x2 grid above gets genuine cross-distribution spread, not just
    temperature noise on one model. Wire your own by dropping a module on the
    Python path (default name `llm_providers`, override with the
    SELFCHECK_PROVIDER_MODULE env var) that exposes:

      available() -> list[str]
          names of backends that are currently usable (have credentials, etc).
      chat(prompt, system=None, max_tokens=..., temperature=..., only=<family>) -> obj
          obj.ok (bool) and obj.text (str); `only` pins the call to one backend
          family so each sample can be attributed to it.

    Raises RuntimeError with a friendly explanation if no such module is
    importable. This is never imported at module load time, only when
    `live_sample` actually runs, so `import selfcheck` and `--selftest` never
    need it.
    """
    mod_name = os.environ.get('SELFCHECK_PROVIDER_MODULE', 'llm_providers')
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        return importlib.import_module(mod_name)
    except ImportError as exc:
        raise RuntimeError(
            f"live sampling needs a multi-backend provider module (looked for "
            f"'{mod_name}'; override the name via the SELFCHECK_PROVIDER_MODULE "
            f"env var) exposing available() and chat(...). None was importable "
            f"({exc}). This repo ships no such backend by design — wire your own "
            f"providers, or use the `case` subcommand with pre-collected samples "
            f"instead of `live`."
        ) from exc

def live_sample(question, rounds=1):
    """Resample a question across INDEPENDENT model families for real
    cross-distribution spread (opt-in, network, needs an external provider
    module — see `_load_provider_backend`). Pins each available backend via
    chat(only=<family>) so the answers come from distinct pretraining
    distributions (noisy-OR independence), not the same model at temperature.
    `rounds` repeats the sweep (temperature spread within family on top of the
    cross-family spread)."""
    wp = _load_provider_backend()
    fams = wp.available()
    system = ("Answer in one short line. State only the fact asked. "
              "If you are not sure, say 'I don't know'.")
    samples = []
    for _ in range(max(1, rounds)):
        for fam in fams:
            for attempt in range(2):   # 1 retry: free backends transiently time out
                try:
                    r = wp.chat(question, system=system, max_tokens=120,
                                temperature=0.7, only=fam)
                    if r.ok and r.text.strip():
                        samples.append({'text': r.text.strip(), 'family': fam})
                        break
                except Exception:
                    continue
    return samples

# -----------------------------------------------------------------------------
# case runner
# -----------------------------------------------------------------------------

def run_case(case):
    claim = case.get('claim', '').strip()
    samples = case.get('samples', [])
    if not claim or not samples:
        return {'verdict': 'REFUSED', 'reasons': ['missing claim or samples'],
                'gloss': VERDICTS['REFUSED']}
    numeric = bool(case.get('numeric_mode', False))
    grounded = bool(case.get('grounded', False))
    agree_frac = float(case.get('agree_frac', 0.75))
    min_families = int(case.get('min_families', 2))
    stats = semantic_entropy(samples, numeric_mode=numeric)
    verdict, reasons = decide(stats, grounded, agree_frac, min_families)
    return {
        'claim': claim,
        'grounded': grounded,
        'numeric_mode': numeric,
        'stats': stats,
        'verdict': verdict,
        'gloss': VERDICTS[verdict],
        'reasons': reasons,
        'can_assert_as_fact': verdict in ('ASSERT', 'ASSERT_WEAK'),
    }

def print_report(r):
    if r['verdict'] in ('REFUSED',):
        print(f"verdict: {r['verdict']} — {r['gloss']}")
        return
    print(f"claim   : {r['claim']}")
    s = r['stats']
    print(f"samples : {s['n_samples']} across families {s['all_families']}")
    print(f"clusters: {s['n_clusters']}  consensus={s['consensus']:.2f}  "
          f"entropy_norm={s['entropy_norm']:.2f}")
    print(f"grounded: {r['grounded']}")
    print(f"VERDICT : {r['verdict']} — {r['gloss']}")
    for reason in r['reasons']:
        print(f"   · {reason}")
    print(f"assert-as-fact allowed: {r['can_assert_as_fact']}")

# -----------------------------------------------------------------------------
# selftest (offline)
# -----------------------------------------------------------------------------

def selftest():
    passed = 0; failed = 0
    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {name}")

    # number canonicalization
    check("38k == 38000", _canon_number('38', 'k') == 38000.0)
    check("38.000 == 38000", _canon_number('38.000', '') == 38000.0)
    check("38000 == 38000", _canon_number('38000', '') == 38000.0)
    check("1.234,56 pt", _canon_number('1.234,56', '') == 1234.56)
    check("1,234.56 en", _canon_number('1,234.56', '') == 1234.56)
    check("15 mil == 15000", _canon_number('15', 'mil') == 15000.0)
    check("1.5m == 1500000", _canon_number('1.5', 'm') == 1500000.0)

    # numeric clustering groups equivalents, splits conflicts
    s_equiv = [{'text':'R$38k','family':'a'},{'text':'38.000','family':'b'},{'text':'38000 dollars','family':'c'}]
    st = semantic_entropy(s_equiv, numeric_mode=True)
    check("38k/38.000/38000 -> 1 cluster", st['n_clusters'] == 1)
    check("equiv consensus==1.0", st['consensus'] == 1.0)
    check("equiv entropy==0", st['entropy_norm'] == 0.0)

    s_conf = [{'text':'port 443','family':'a'},{'text':'port 8443','family':'b'},{'text':'port 443','family':'c'}]
    st2 = semantic_entropy(s_conf, numeric_mode=True)
    check("443 vs 8443 -> 2 clusters", st2['n_clusters'] == 2)
    check("conflict consensus 2/3", abs(st2['consensus'] - 2/3) < 1e-3)

    # entropy math: 2 equal clusters -> normalized entropy == 1
    s_split = [{'text':'A','family':'a'},{'text':'B','family':'b'}]
    st3 = semantic_entropy(s_split)
    check("50/50 split -> entropy_norm==1", abs(st3['entropy_norm'] - 1.0) < 1e-9)
    # all identical -> entropy 0
    s_same = [{'text':'A','family':'a'},{'text':'A','family':'b'},{'text':'A','family':'c'}]
    st4 = semantic_entropy(s_same)
    check("all same -> entropy_norm==0", st4['entropy_norm'] == 0.0)
    check("entropy_norm in [0,1]", 0.0 <= st3['entropy_norm'] <= 1.0)

    # 2x2 verdicts
    # 1. convergent + grounded + 2 families -> ASSERT
    r1 = run_case({'claim':'x','grounded':True,'samples':[
        {'text':'yes','family':'groq'},{'text':'yes','family':'google'},{'text':'yes','family':'cerebras'}]})
    check("convergent+grounded+multifamily -> ASSERT", r1['verdict']=='ASSERT')

    # 2. THE CONFIDENT FABRICATION: convergent + ungrounded -> FORCE_GROUND
    # normalize-identical on purpose: this test isolates the 2x2 routing, not the NLP proxy
    bus = [{'text':'Northline Express 20:00','family':'groq'},
           {'text':'NORTHLINE EXPRESS 20:00','family':'google'},
           {'text':'northline express 20:00','family':'cerebras'}]
    r2 = run_case({'claim':'bus departure time','grounded':False,'samples':bus})
    check("convergent+ungrounded -> FORCE_GROUND (confident fabrication)", r2['verdict']=='FORCE_GROUND')
    check("confident fabrication cannot be asserted", r2['can_assert_as_fact'] is False)

    # 3. divergent + ungrounded -> ABSTAIN
    r3 = run_case({'claim':'y','grounded':False,'samples':[
        {'text':'alpha','family':'a'},{'text':'beta','family':'b'},{'text':'gamma','family':'c'}]})
    check("divergent+ungrounded -> ABSTAIN", r3['verdict']=='ABSTAIN')

    # 4. divergent + grounded -> TRUST_TOOL
    r4 = run_case({'claim':'z','grounded':True,'samples':[
        {'text':'alpha','family':'a'},{'text':'beta','family':'b'},{'text':'gamma','family':'c'}]})
    check("divergent+grounded -> TRUST_TOOL", r4['verdict']=='TRUST_TOOL')

    # 5. convergent + grounded but single family -> ASSERT_WEAK
    r5 = run_case({'claim':'w','grounded':True,'samples':[
        {'text':'yes','family':'groq'},{'text':'yes','family':'groq'},{'text':'yes','family':'groq'}]})
    check("single-family convergence -> ASSERT_WEAK", r5['verdict']=='ASSERT_WEAK')

    # 6. too few samples -> INCONCLUSIVE
    r6 = run_case({'claim':'q','grounded':True,'samples':[{'text':'a','family':'x'}]})
    check("2-few samples -> INCONCLUSIVE", r6['verdict']=='INCONCLUSIVE')

    # 7. malformed -> REFUSED
    r7 = run_case({'claim':'','samples':[]})
    check("empty -> REFUSED", r7['verdict']=='REFUSED')

    # 8. numeric mode: 38k vs 38.000 grounded -> ASSERT (equivalent numbers, multifamily)
    r8 = run_case({'claim':'price','grounded':True,'numeric_mode':True,'samples':[
        {'text':'R$38k','family':'groq'},{'text':'38.000','family':'google'},{'text':'38000','family':'cerebras'}]})
    check("numeric-equiv multifamily grounded -> ASSERT", r8['verdict']=='ASSERT')

    # 9. numeric conflict ungrounded -> ABSTAIN (they disagree on the number, nothing backs)
    r9 = run_case({'claim':'port','grounded':False,'numeric_mode':True,'samples':[
        {'text':'443','family':'a'},{'text':'8443','family':'b'},{'text':'22','family':'c'}]})
    check("numeric conflict ungrounded -> ABSTAIN", r9['verdict']=='ABSTAIN')

    # 10. consensus threshold boundary: 2/3 agree with default 0.75 -> divergent
    r10 = run_case({'claim':'b','grounded':False,'samples':[
        {'text':'yes','family':'a'},{'text':'yes','family':'b'},{'text':'no','family':'c'}]})
    check("2/3 < 0.75 -> divergent -> ABSTAIN", r10['verdict']=='ABSTAIN')
    # same but relaxed threshold 0.6 -> convergent -> FORCE_GROUND
    r10b = run_case({'claim':'b','grounded':False,'agree_frac':0.6,'samples':[
        {'text':'yes','family':'a'},{'text':'yes','family':'b'},{'text':'no','family':'c'}]})
    check("2/3 >= 0.6 -> convergent -> FORCE_GROUND", r10b['verdict']=='FORCE_GROUND')

    # 11. determinism: same input twice -> same verdict
    check("deterministic verdict", run_case({'claim':'x','grounded':True,'samples':bus})['verdict']
          == run_case({'claim':'x','grounded':True,'samples':bus})['verdict'])

    total = passed + failed
    print(f"\nselftest: {passed}/{total} passed")
    return failed == 0

# -----------------------------------------------------------------------------
# cli
# -----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="semantic self-consistency gate for black-box LLM hallucination detection")
    sub = ap.add_subparsers(dest='cmd')

    p_case = sub.add_parser('case', help='run a case file (json with claim/samples/grounded)')
    p_case.add_argument('path')
    p_case.add_argument('--json', action='store_true')

    p_live = sub.add_parser('live', help='resample a question across model families, then judge')
    p_live.add_argument('question')
    p_live.add_argument('--rounds', type=int, default=4)
    p_live.add_argument('--grounded', action='store_true', help='mark claim as tool-backed')
    p_live.add_argument('--numeric', action='store_true')
    p_live.add_argument('--json', action='store_true')

    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    if args.cmd == 'case':
        with open(args.path) as f:
            case = json.load(f)
        r = run_case(case)
        print(json.dumps(r, indent=2, ensure_ascii=False) if args.json else None) if args.json else print_report(r)
        sys.exit(0 if r['can_assert_as_fact'] else 1)

    if args.cmd == 'live':
        samples = live_sample(args.question, rounds=args.rounds)
        case = {'claim': args.question, 'samples': samples,
                'grounded': args.grounded, 'numeric_mode': args.numeric}
        r = run_case(case)
        if args.json:
            print(json.dumps(r, indent=2, ensure_ascii=False))
        else:
            print_report(r)
        sys.exit(0 if r['can_assert_as_fact'] else 1)

    ap.print_help()

if __name__ == '__main__':
    main()
