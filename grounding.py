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

MATCHER v2.3-v2.4 (2026-09-28 -- eval-driven, then research-verified):
  v2.3 normalizes FORMATS the ledger prints differently from drafts (ISO and
  yyyy/mm/dd dates, `ls` month names PT/EN, US month-first order, times after
  ':' in grep/log output, '9:00' vs '09:00'), reads magnitude suffixes
  ('R$296K' rounds from 'R$296,332'; 'R$5M' is extracted), orders ratios,
  treats zero-padded integers as identifiers, and drops parse artifacts
  (CNPJ, version lists, 4+-part slash lists). v2.4 closes three leaks an
  adversarial research pass reproduced against v2.3: a suffixed draft value
  grounded on its bare base ('R$ 412 mil' on 'rows 412'); the sign was
  ignored ('-5%' on 'growth 5%'); and line-level EXEMPT words ('convertido',
  'soma', 'range') waved through anything on the line. v2.4 also extracts
  bare '$' / USD / BRL / EUR amounts and RECOMPUTES shown arithmetic
  (DERIVED) instead of exempting it.

MATCHER v2.2 (2026-09-28 -- fixes found by measurement, see eval/RESULTS.md):
  An injection benchmark over 2,711 real ledgers and a replay over real
  drafts traced every v2.1 error to five matcher bugs, each now a selftest:
  1. separator flattening: digit_core('2.0') == '20', so a decimal in the
     ledger grounded a fabricated '20%' (every measured miss). Numeric kinds
     now match on locale-aware VALUES (pt-BR and en), not digit strings.
  2. single-digit values never grounded ('5%' blocked even when the ledger
     said '5%'). They now ground, but only against the SAME type: a raw '5'
     is in every ledger and must not ground a percent.
  3. ledger values glued to identifiers ('U_e2629...', '..._03:12') were
     lost because `\b` fails after '_'. Ledger-side boundaries are now
     glue-tolerant; draft-side boundaries are unchanged.
  4. receipts the gate could not SEE: 0x-prefixed hex (an invented EVM tx
     hash produced zero tokens), Stellar strkeys (G.../C...), base58
     keys/signatures. All are now structural danger tokens.
  5. parse artifacts: a full date '31/12/2026' also produced a ratio
     '31/12' (blocked as COMPOSED); PT 'Top 10 de 2025' was a ratio; the
     declarations 'não verificado' / 'derivado' never matched (a trailing
     `\b` after a word stem).

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
  3. A value DERIVED by arithmetic is accepted only when the line SHOWS the
     work ("R$ 72,19 + R$ 9,86 = R$ 82,05") over grounded operands (v2.4).
     A derived value stated without its arithmetic still flags as UNSOURCED,
     and a derivation word ("soma", "convertido") no longer exempts anything.
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

VERSION = "2.4"

# ---- danger tokens: what an operational hallucination usually asserts ----
# {L}/{R} are token boundaries. In the DRAFT they are plain `\b`. In the LEDGER
# they are glue-tolerant (v2.2): tool output glues values to identifiers
# ("U_e2629...", "2026-06-16_03:12") where `\b` fails and the fact was lost.
_DANGER_SPEC = [
    ("currency",   r'(?:(?<![0-9A-Za-z])[-−])?(?:R\$|US?\$|(?<![A-Za-z0-9{])\$|(?:USD|BRL|EUR)\s?|€\s?)'
                   r'\s?[-−]?\d(?:[\d.,]*\d)?(?:\s?' + r'{SUF}' + r')?(?![0-9A-Za-z])'),
    ("date",       r'{L}\d{1,2}/\d{1,2}(?:/\d{2,4})?{R}'),
    # operands may carry thousands separators (3.218 / 32.193) so they are not cut in half
    ("ratio",      r'{L}(?:\d{1,3}(?:[.,]\d{3})+|\d+)\s*(?:de|of|/)\s*(?:\d{1,3}(?:[.,]\d{3})+|\d+){R}'),  # "de" = PT "of"
    ("time",       r'{L}\d{1,2}:\d{2}{R}'),
    ("percent",    r'(?:(?<![0-9A-Za-z])[-−](?=\d))?{L}\d[\d.,]*\s*%'),  # signed (v2.3)
    ("ipv4",       r'{L}\d{1,3}(?:\.\d{1,3}){3}{R}'),
    ("strkey",     r'{L}[GC][A-Z2-7]{55}{R}'),                  # Stellar account / contract id
    ("txhash",     r'{L}(?:0x)?[0-9a-fA-F]{8,}{R}'),            # hex receipt, 0x optional (v2.2)
    ("base58",     r'{L}[1-9A-HJ-NP-Za-km-z]{32,88}{R}'),       # Solana-style pubkey / signature
]
# magnitude suffixes (v2.3): 'R$296K', 'R$ 20 mil', 'US$ 1,5 bi', 'R$5M'
_SUFFIX_MULT = [(r'milh[õo]es|milh[ãa]o|million|millions|mi|MM|M|m', 10**6),
                (r'bilh[õo]es|bilh[ãa]o|billion|billions|bi|bn|B|b', 10**9),
                (r'mil|k|K', 10**3)]
_SUF = '(?:' + '|'.join(p for p, _ in _SUFFIX_MULT) + ')'
_SUFFIX_AT_END = re.compile(r'\d\s?(' + _SUF + r')$')


def suffix_mult(tok):
    m = _SUFFIX_AT_END.search(tok)
    if not m:
        return None
    for p, mult in _SUFFIX_MULT:
        if re.fullmatch(p, m.group(1)):
            return mult
    return None


_B_DRAFT = (r'\b', r'\b')
_B_LEDGER = (r'(?<![0-9A-Za-z])', r'(?![0-9A-Za-z])')


# ledger-only overrides (v2.3): ISO 8601 glues a 'T' to the time (2026-09-28T10:55:00Z)
_LEDGER_OVERRIDE = {"time": r'(?<!\d)\d{1,2}:\d{2}(?!\d)'}


def _build(bounds, override=None):
    L, R = bounds
    override = override or {}
    return [(k, re.compile(override.get(k) or p.replace('{L}', L).replace('{R}', R).replace('{SUF}', _SUF)))
            for k, p in _DANGER_SPEC]


DANGER = _build(_B_DRAFT)
LEDGER_DANGER = _build(_B_LEDGER, _LEDGER_OVERRIDE)

# dates that tools print in OTHER formats than drafts do (v2.3): ISO 8601 and
# `ls`-style month names, English and Portuguese abbreviations.
_MONTHS = {"jan": 1, "feb": 2, "fev": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5, "mai": 5,
           "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10, "out": 10,
           "nov": 11, "dec": 12, "dez": 12}
_MON = (r'(jan(?:uary|eiro)?|feb(?:ruary)?|fev(?:ereiro)?|mar(?:ch|[çc]o)?|apr(?:il)?|abr(?:il)?|may|mai(?:o)?|'
        r'jun(?:e|ho)?|jul(?:y|ho)?|aug(?:ust)?|ago(?:sto)?|sep(?:t|tember)?|set(?:embro)?|oct(?:ober)?|'
        r'out(?:ubro)?|nov(?:ember|embro)?|dec(?:ember)?|dez(?:embro)?)\.?(?![A-Za-z])')
_ISO_DATE = re.compile(r'(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)')  # ISO and URL paths
_MON_DAY = re.compile(r'(?<![A-Za-z])' + _MON + r'\s+(\d{1,2})(?!\d)(?:,?\s+(\d{4})(?!\d))?', re.IGNORECASE)
_DAY_MON = re.compile(r'(?<!\d)(\d{1,2})\s+(?:de\s+)?' + _MON + r'(?:\s+(?:de\s+)?(\d{4})(?!\d))?', re.IGNORECASE)

# tags that DECLARE uncertainty -> the token is no longer asserted as a live fact.
# Matches both English and Portuguese hedge words by design (the guard accepts
# drafts in either language); do not prune the Portuguese alternatives below,
# that would silently weaken matching on non-English input. Stems end in \w*
# (v2.2): "verificad\b" could never match "verificado".
EXEMPT = re.compile(
    r'\b(INFERRED|INFERID\w*|RECALL|estimativa|estimado|estimate|estimated|'
    r'n[aã]o[- ]verificad\w*|unverified|\+/-)\b'
    r'|\bbanda\s+(?:de\s+)?[~≈]?\d',        # an explicit numeric band ('banda 3k-8k')
    re.IGNORECASE)
# v2.3: EXPLICIT declarations only. Derivation words (soma, derivado, convertido)
# and loose words (range, banda) exempted whole lines: an IP on a line with
# "range" passed as DECLARED. A derivation is a claim to check, not a doubt.
# Soft hedges (talvez, provável, approx, aproximado, acho que) never exempted in
# production either: the Stop-hook strips them before check().
EXEMPT_SYM = re.compile(r'±')

# tag that ASSERTS direct observation -> with no grounding this is the worst case
OBSERVED = re.compile(r'\bOBSERVED\b', re.IGNORECASE)

# types a RAW number (no unit in the ledger) is allowed to ground against
# (deliberate leniency, honest ceiling #2). time/ipv4/date/receipts require a
# real match. A single-digit VALUE never uses the leniency (see _match).
COMPAT = {
    "currency": {"currency", "number"},
    "percent":  {"percent", "number"},
    "txhash":   {"txhash", "number"},
    "time":     {"time"},
    "ipv4":     {"ipv4"},
    "date":     {"date"},
    "strkey":   {"strkey"},
    "base58":   {"base58"},
}
NUMERIC = {"currency", "percent", "number"}

_NUM = re.compile(r'\d[\d.,]*\d|\d')
# a real minus: not after a letter/digit/hyphen (CSS '--x--75', UUID '6a6-7aaa'),
# and the number is not glued to a letter afterwards
_SIGNED_NUM = re.compile(r'(?<![0-9A-Za-z\-−])[-−](?:\d[\d.,]*\d|\d)(?![0-9A-Za-z])|\d[\d.,]*\d|\d')
_RATIO_SPLIT = re.compile(r'\s*(?:de|of|/)\s*')


def digit_core(s):
    """Numeric signature of a token: digits only, len >= 2."""
    d = re.sub(r'\D', '', s)
    return d if len(d) >= 2 else ''


def _canon(d):
    """Decimal -> canonical string: no exponent, no trailing zeros ('2.0' -> '2')."""
    s = format(d.normalize(), 'f')
    return s.rstrip('0').rstrip('.') if '.' in s else s


def num_values(s, side="draft"):
    """
    VALUE signature(s) of a numeric string (v2.2), replacing digit concatenation,
    which made '2.0' == '20' and '12.5' == '125' (all measured misses).
    Locale rules, pt-BR and en:
      - both '.' and ',': the LAST one is the decimal separator (1.234,56 / 1,234.56)
      - one separator kind repeated with 3-digit groups: thousands (1.234.567)
      - one separator kind repeated otherwise: not a number (a version, 1.24.0) ->
        each part is its own integer
      - a single separator followed by exactly 3 digits is ambiguous (1.800):
        the DRAFT reads it as thousands only (a fabricated 'R$ 20.000' must not
        ground on a stray '20'); the LEDGER keeps both readings.
      - otherwise the single separator is decimal (82,05 / 2.0 / 12.5)
    """
    from decimal import Decimal
    base = _num_values_base(s, side)
    mult = suffix_mult(s.strip())
    if mult:
        scaled = {_canon(Decimal(v) * mult) for v in base if not _is_identifier(v)}
        # the DRAFT asserts the scaled value only: 'R$ 412 mil' must not ground
        # on a stray '412' (v2.3 leak). The LEDGER offers both readings.
        base = scaled if side == "draft" else base | scaled
    if side == "ledger":
        base = base | {v[1:] for v in base if v.startswith('-')}  # unsigned draft grounds on magnitude
    return base


def _is_identifier(v):
    v = v.lstrip('-')
    return len(v) > 1 and v[0] == '0' and '.' not in v


def _negative(s, m):
    """a minus sign right before the number, or before the currency symbol,
    and not a range hyphen (3-5%)."""
    pre = s[:m.start()].rstrip()
    lead = s.lstrip()
    return pre.endswith(('-', '−')) or lead.startswith(('-', '−'))


def _num_values_base(s, side="draft"):
    m = _NUM.search(s)
    if not m:
        return set()
    vals = _unsigned_values(m.group(0), side)
    if _negative(s, m):
        vals = {'-' + v if v != '0' else v for v in vals}
    return vals


def _unsigned_values(t, side):
    from decimal import Decimal, InvalidOperation
    try:
        if '.' in t and ',' in t:
            dec = '.' if t.rfind('.') > t.rfind(',') else ','
            other = ',' if dec == '.' else '.'
            return {_canon(Decimal(t.replace(other, '').replace(dec, '.')))}
        sep = '.' if '.' in t else (',' if ',' in t else None)
        if sep is None:
            # zero-padded ('0057', 'PR-0057') is an identifier, not the value 57 (v2.3)
            return {t} if (len(t) > 1 and t[0] == '0') else {_canon(Decimal(t))}
        parts = t.split(sep)
        if len(parts) > 2:
            if all(len(p) == 3 for p in parts[1:]):
                return {_canon(Decimal(''.join(parts)))}
            return {p if (len(p) > 1 and p[0] == '0') else _canon(Decimal(p)) for p in parts if p}
        head, tail = parts
        if len(tail) == 3 and head.lstrip('0'):
            thousands = _canon(Decimal(head + tail))
            if side == "draft":
                return {thousands}
            return {thousands, _canon(Decimal(head + '.' + tail))}
        return {_canon(Decimal(head + '.' + tail))}
    except InvalidOperation:
        return set()


# STRUCTURAL types: the non-digit characters ARE the identity -- the hex
# letters of a hash, the position of the dots in an IP. digit_core() would
# flatten these down to digits only and ground a WRONG receipt against a
# coincidentally matching number (a6281d89 vs. the byte-count 628189;
# 1.22.3.4 vs. the different IP 12.2.3.4). These match on the full
# normalized TOKEN, never on digit_core. False negative confirmed against
# a live case.
STRUCTURAL = {"txhash", "ipv4", "strkey", "base58"}


def norm_core(s, kind):
    """Identity signature of a NON-numeric-value token. Structural kinds
    normalize the whole token; time/date fall back to digit_core."""
    if kind == "txhash":
        h = s[2:] if s[:2].lower() == "0x" else s
        h = re.sub(r'[^0-9a-fA-F]', '', h).lower()
        return h if len(h) >= 2 else ''
    if kind == "ipv4":
        octs = re.findall(r'\d{1,3}', s)
        return '.'.join(str(int(o)) for o in octs) if len(octs) == 4 else ''
    if kind in ("strkey", "base58"):
        return s  # case IS identity in base58 and strkey
    return digit_core(s)


def _date_key(d, m, y=None):
    if y is not None and y < 100:
        y += 2000
    return f"{d}/{m}" if y is None else f"{d}/{m}/{y}"


def date_cores(parts, side):
    """Canonical calendar keys (v2.3). The draft asserts what it wrote (d/m or
    d/m/y). The ledger offers both day orders, with and without the year, so
    a dd/mm draft grounds on an ISO or US-ordered date of the same day."""
    p = [int(x) for x in parts]
    if len(p) < 2:
        return set()
    a, b = p[0], p[1]
    y = p[2] if len(p) > 2 else None
    if side == "draft":
        if 1 <= a <= 31 and 1 <= b <= 12:
            return {_date_key(a, b, y)}
        if 1 <= a <= 12 and 1 <= b <= 31:
            return {_date_key(b, a, y)}  # month-first (US) date
        return set()
    out = set()
    for d, m in ((a, b), (b, a)):
        if 1 <= d <= 31 and 1 <= m <= 12:
            out.add(_date_key(d, m))
            if y is not None:
                out.add(_date_key(d, m, y))
    return out


def cores(s, kind, side="draft"):
    """Set of signatures a token can match on."""
    if kind in NUMERIC:
        return num_values(s, side)
    if kind == "time":
        h, _, mm = s.partition(':')
        return {f"{int(h)}:{mm}"} if h.isdigit() and mm.isdigit() else set()
    if kind == "date":
        return date_cores(re.findall(r'\d+', s), side)
    c = norm_core(s, kind)
    return {c} if c else set()


def _ops(tok):
    """Operands of a ratio, canonical integers (a single digit is valid in a ratio)."""
    out = []
    for p in _RATIO_SPLIT.split(tok):
        d = re.sub(r'\D', '', p)
        if d:
            out.append(str(int(d)))
    return out


def ledger_digits(ledger):
    """DEPRECATED (v1). Flattened digit stream. Kept only for import compatibility."""
    return re.sub(r'\D', '', ledger or '')


def _valid_base58(tok):
    """base58 shape alone matches long identifiers; require the mix a real
    key/signature has (digit + upper + lower) so prose and code names do not."""
    return (any(c.isdigit() for c in tok) and any(c.isupper() for c in tok)
            and any(c.islower() for c in tok))


def ledger_facts(ledger):
    """
    Discrete, TYPED facts from the ledger. No concatenation: each typed fact
    is masked before scanning for raw numbers, so a '92%' does not turn into
    a phantom raw '92'.
    Returns (facts, ratio_pairs): facts={(core, kind)}, ratio_pairs=[(op, ...)].
    """
    text = ledger or ""
    facts = set()
    ratio_pairs = []
    masked = list(text)
    for kind, rx in LEDGER_DANGER:
        for m in rx.finditer(text):
            span = m.group(0)
            if kind == "base58" and not _valid_base58(span):
                continue
            if kind == "ratio":
                ops = _ops(span)
                if ops:
                    ratio_pairs.append(tuple(ops))
                    for raw in re.findall(r'\d+', span):
                        for c in num_values(raw, "ledger"):
                            facts.add((c, "number"))
            else:
                for c in cores(span, kind, side="ledger"):
                    facts.add((c, kind))
                # a purely-decimal txhash is also usable as a raw number
                if kind == "txhash" and span.isdigit():
                    for c in num_values(span, "ledger"):
                        facts.add((c, "number"))
            for i in range(m.start(), m.end()):
                masked[i] = ' '
    for m in _ISO_DATE.finditer(text):
        y, mo, d = m.groups()
        for c in date_cores([d, mo, y], "ledger"):
            facts.add((c, "date"))
    for rx, order in ((_MON_DAY, "md"), (_DAY_MON, "dm")):
        for m in rx.finditer(text):
            g = m.groups()
            mon, day, year = (g[0], g[1], g[2]) if order == "md" else (g[1], g[0], g[2])
            mo = _MONTHS.get(mon[:3].lower())
            if mo:
                for c in date_cores([day, mo] + ([year] if year else []), "ledger"):
                    facts.add((c, "date"))
    rest = ''.join(masked)
    for m in _SIGNED_NUM.finditer(rest):
        for c in num_values(m.group(0), "ledger"):
            facts.add((c, "number"))
    return facts, ratio_pairs


def _valid_date(tok):
    """a/b[/y] is a calendar date only if it can be dd/mm or mm/dd."""
    p = [int(x) for x in re.findall(r'\d+', tok)]
    if len(p) < 2:
        return False
    a, b = p[0], p[1]
    return (1 <= a <= 31 and 1 <= b <= 12) or (1 <= a <= 12 and 1 <= b <= 31)


_PT_YEAR = re.compile(r'\d+\s*de\s*(19|20)\d\d$')
_SLASH_RUN = re.compile(r'\d+(?:/\d+){3,}')     # 11/4/1/1: a list, not a date or ratio


def _inside_dotted(line, start, end):
    """the match is the tail/head of a longer dotted number: a CNPJ
    (61.020.726/0001-80) or a version (15.2.3 / 14.2.25), not a quantity."""
    before = line[max(0, start - 2):start]
    after = line[end:end + 2]
    return (len(before) == 2 and before[1] in '.,' and before[0].isdigit()) or \
           (len(after) == 2 and after[0] in '.,-' and after[1].isdigit())


def extract(draft):
    """
    (token, kind, line_text) per danger token, deduped by (token, line).
    v2.2 span rules (measured parse artifacts):
      - a full date with year ("31/12/2026") is one date token, never also a
        ratio "31/12"; a two-part dd/mm is a date that may ground as a ratio too
      - "10 de 2025" (PT month/ordinal + year) is not a ratio
      - a base58 match needs digit+upper+lower and must not overlap a receipt
    v2.3 span rules (orphan label pass):
      - a slash list with 4+ parts (11/4/1/1) holds no date or ratio
      - a ratio operand that is the head/tail of a dotted number is not a
        quantity: CNPJ 61.020.726/0001-80, versions 15.2.3 / 14.2.25
      - ratio operands keep their thousands separators (3.218 / 32.193)
    """
    out, seen = [], set()
    for line in draft.splitlines():
        runs = [m.span() for m in _SLASH_RUN.finditer(line)]  # 11/4/1/1 style lists
        taken = []  # spans claimed by dates with year and receipts
        for kind, rx in DANGER:
            for m in rx.finditer(line):
                tok = m.group(0).strip()
                span = (m.start(), m.end())
                if kind in ("date", "ratio") and any(s <= span[0] and span[1] <= e for s, e in runs):
                    continue
                if kind == "ratio" and _inside_dotted(line, *span):
                    continue
                if kind == "date":
                    if not _valid_date(tok):
                        continue
                    if tok.count('/') == 2:
                        taken.append(span)
                if kind == "ratio":
                    if _PT_YEAR.match(tok):
                        continue
                    if any(s <= span[0] and span[1] <= e for s, e in taken):
                        continue
                if kind == "base58":
                    if not _valid_base58(tok) or any(s < span[1] and span[0] < e for s, e in taken):
                        continue
                if kind == "currency" and re.fullmatch(r'\$\d', tok):
                    continue  # $1 in awk/shell, not a price
                if kind in ("strkey", "txhash"):
                    taken.append(span)
                key = (tok, line)
                if key in seen:
                    continue
                seen.add(key)
                out.append((tok, kind, line))
    return out


def _rounds_to(tok, mult, facts, compat):
    """'R$296K' grounds on a ledger value that ROUNDS to it at the precision
    shown (295,500 <= v < 296,500). The reverse is never allowed: a draft more
    precise than its source is fabricated precision."""
    from decimal import Decimal
    base = _num_values_base(tok, "draft")
    if len(base) != 1:
        return False
    b = Decimal(next(iter(base)))
    decimals = max(0, -b.as_tuple().exponent)
    target = b * mult
    half = Decimal(mult) * (Decimal(10) ** -decimals) / 2
    for fc, fk in facts:
        if fk not in compat or (len(fc) > 1 and fc[0] == '0' and '.' not in fc):
            continue
        try:
            v = Decimal(fc)
        except Exception:
            continue
        if target - half <= v < target + half:
            return True
    return False


def _match(tok, kind, facts, ratio_pairs):
    """(grounded, composed) against the ledger's discrete facts."""
    if kind == "ratio":
        ops = _ops(tok)
        if ops and tuple(ops) in {tuple(p) for p in ratio_pairs}:  # ordered (v2.3)
            return True, False
        values = {fc for fc, fk in facts if fk in NUMERIC}
        if ops and all(o in values for o in ops):
            return False, True
        return False, False
    cs = cores(tok, kind, side="draft")
    if not cs:
        return False, False
    compat = COMPAT.get(kind, {"number"})
    for c in cs:
        # a single-digit value is everywhere as a raw number: same TYPE only
        allowed = {kind} if (kind in NUMERIC and len(c.replace('.', '')) == 1) else compat
        if any((c, fk) in facts for fk in allowed):
            return True, False
    mult = suffix_mult(tok) if kind in NUMERIC else None
    if mult and _rounds_to(tok, mult, facts, compat):
        return True, False
    if kind == "date" and tok.count('/') == 1:
        # a bare dd/mm may be what the ledger printed as a ratio (e.g. "3/1")
        return _match(tok, "ratio", facts, ratio_pairs)[0], False
    return False, False


# ---- derived arithmetic (v2.4, roadmap R4): recompute shown work, never exempt it ----
_CUR_PREFIX = r'(?:R\$|US?\$|\$|USD\s?|BRL\s?|EUR\s?|€\s?)'
_OPND = (r'(?:' + _CUR_PREFIX + r'\s?\d(?:[\d.,]*\d)?(?:\s?' + _SUF + r'(?![A-Za-z]))?'
         r'|\d(?:[\d.,]*\d)?(?:\s?' + _SUF + r'(?![A-Za-z]))?%?)')
_OP = r'[+\-−×x*/÷]'
_CHAIN = re.compile(r'(' + _OPND + r'(?:\s*' + _OP + r'\s*' + _OPND + r')+)\s*[*~]*\s*(?:=|≈|→)\s*[*~]*\s*$')
_SPLIT_CHAIN = re.compile(r'(' + _OPND + r')|(' + _OP + r')')
# a bare constant in shown work must be grounded too, except unit conversions
UNIT_CONSTANTS = {"7", "12", "24", "30", "52", "60", "100", "365", "1000"}


def _operand_ok(op, facts, ratio_pairs):
    kind = "currency" if re.match(_CUR_PREFIX, op) else ("percent" if op.endswith('%') else "number")
    vals = num_values(op, "draft")
    if len(vals) != 1:
        return None
    v = next(iter(vals))
    if kind == "number":
        ok = v in UNIT_CONSTANTS or any((v, fk) in facts for fk in NUMERIC)
    else:
        ok = _match(op, kind, facts, ratio_pairs)[0]
    return v if ok else None


def _derived(tok, kind, line, facts, ratio_pairs):
    """tok is DERIVED if the line shows `operand op operand ... = tok`, every
    operand is grounded (or a unit constant), and the arithmetic holds at the
    precision tok is written with. Operator precedence: x/÷ before +/-."""
    from decimal import Decimal, InvalidOperation, localcontext
    i = line.find(tok)
    if i <= 0:
        return False
    m = _CHAIN.search(line[:i])
    if not m:
        return False
    vals, ops = [], []
    for om, pm in _SPLIT_CHAIN.findall(m.group(1)):
        if om:
            v = _operand_ok(om, facts, ratio_pairs)
            if v is None:
                return False
            vals.append(Decimal(v))
        else:
            ops.append(pm)
    if len(vals) != len(ops) + 1:
        return False
    try:
        with localcontext() as ctx:
            ctx.prec = 34
            terms, signs, acc = [], [], vals[0]
            for op, v in zip(ops, vals[1:]):
                if op in '×x*':
                    acc *= v
                elif op in '/÷':
                    acc /= v
                else:
                    terms.append(acc); signs.append(op); acc = v
            terms.append(acc)
            result = terms[0]
            for sg, t in zip(signs, terms[1:]):
                result = result + t if sg == '+' else result - t
    except (InvalidOperation, ZeroDivisionError):
        return False
    targets = num_values(tok, "draft")
    base = _num_values_base(tok, "draft")
    if len(targets) != 1 or len(base) != 1:
        return False
    target = Decimal(next(iter(targets)))
    b = Decimal(next(iter(base)))
    mult = suffix_mult(tok) or 1
    half = Decimal(mult) * (Decimal(10) ** -max(0, -b.as_tuple().exponent)) / 2
    candidates = [result] + ([result * 100] if kind == "percent" else [])
    return any(abs(c - target) <= half for c in candidates)


def _exempt(line):
    return bool(EXEMPT.search(line) or EXEMPT_SYM.search(line))


def classify(tok, kind, line, facts, ratio_pairs):
    grounded, composed = _match(tok, kind, facts, ratio_pairs)
    if grounded:
        return "GROUNDED"
    if kind in NUMERIC and _derived(tok, kind, line, facts, ratio_pairs):
        return "DERIVED"           # shown arithmetic over grounded operands, recomputed
    if _exempt(line):
        return "DECLARED"          # stated as uncertain, honest (covers composed too)
    if composed:
        return "COMPOSED"          # real operands, the relation is not in the source
    if OBSERVED.search(line):
        return "FALSE_OBSERVED"    # claimed to see what the ledger doesn't have
    return "UNSOURCED"             # the silent hallucination


SEVERITY = {"FALSE_OBSERVED": 3, "UNSOURCED": 2, "COMPOSED": 1,
            "DECLARED": 0, "DERIVED": 0, "GROUNDED": 0}


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
        "derived": sum(1 for f in findings if f["verdict"] == "DERIVED"),
        "flagged": flagged,
        "verdict": "HALLUCINATION_RISK" if flagged else "GROUNDED_OK",
        "block": bool(flagged),
    }


def render(r):
    lines = [
        f"grounding: {r['verdict']}",
        f"  danger-tokens: {r['tokens']}  |  grounded: {r['grounded']}  "
        f"|  declared-uncertain: {r['declared']}  |  derived: {r.get('derived', 0)}  "
        f"|  composed: {r.get('composed', 0)}  "
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

    # --- v2.2: the errors eval/synth.py and eval/realrun.py measured on real ledgers ---

    # (H) single-digit values: digit_core() drops anything under 2 digits, so a
    #     verbatim "5%" never grounded (63 of 121 measured false blocks).
    r = check("coverage is 5%", "coverage: 5% of lines")
    cases.append(("single_digit_verbatim_grounds", r["block"] is False))
    #     ...but a single digit must match its own TYPE: a stray raw "5" is
    #     everywhere in a real ledger, so it may not ground "5%".
    r = check("coverage is 5%", "processed 5 files, 50% done")
    cases.append(("single_digit_needs_same_type", r["block"] is True))

    # (I) ledger tokens glued to a word character: `\b` fails after "_" or a
    #     letter, so the fact was never extracted (54 of 121 false blocks).
    r = check("commit time 03:12", "701 2026-06-16_03:12 wave.service")
    cases.append(("glued_time_in_ledger_grounds", r["block"] is False))
    r = check("hacker id e2629087899c55", "https://dorahacks.io/hacker/U_e2629087899c55")
    cases.append(("glued_hash_in_ledger_grounds", r["block"] is False))

    # (I2) 0x-prefixed hashes were INVISIBLE (zero tokens): an invented EVM tx
    #      hash passed the gate untouched. Receipts are the highest-stakes class.
    r = check("tx 0x9f776be5c0a1 confirmed", "")
    cases.append(("hex0x_fabricated_blocks", r["block"] is True))
    r = check("tx 0x9f776be5c0a1 confirmed", "hash: 0x9f776be5c0a1 status SUCCESS")
    cases.append(("hex0x_real_grounds", r["block"] is False))

    # (I3) Stellar strkeys (G.../C... account and contract ids) and base58
    #      signatures/pubkeys (Solana) were not danger tokens at all.
    SK = "CB32KP47Q3ZJ5XLMVRDWFAN6YHTKE2GUOIPSC7B4QMVLXZ2RNDYWJ7HA"
    r = check(f"verifier deployed at {SK}", "")
    cases.append(("strkey_fabricated_blocks", r["block"] is True))
    r = check(f"verifier deployed at {SK}", f"contract id {SK} wasm hash ok")
    cases.append(("strkey_real_grounds", r["block"] is False))
    B58 = "5VERv8NMvzbJMEkV8xnrLkEaWRtSz9CosKDYjCJjBRnbJLgp8uirBgmQpjKhoR4tjF3ZpRzrFmBV6UjKdiSZkQUW"
    r = check(f"signature {B58}", "")
    cases.append(("base58_fabricated_blocks", r["block"] is True))
    r = check(f"signature {B58}", f'{{"signature": "{B58}", "slot": 1}}')
    cases.append(("base58_real_grounds", r["block"] is False))

    # (J) separator flattening: digit_core("2.0") == "20", so "2.0" in the
    #     ledger grounded a fabricated "20%" (all 24 measured misses).
    r = check("pass rate 20%", "version 2.0 released")
    cases.append(("decimal_does_not_ground_integer", r["block"] is True))
    r = check("latency 12.5%", "took 125 ms")
    cases.append(("integer_does_not_ground_decimal", r["block"] is True))
    #     value-level matching must keep the locale leniency that is correct:
    r = check("total R$ 1.800", "sum 1800")
    cases.append(("thousands_sep_value_grounds", r["block"] is False))
    r = check("price R$ 82,05", "82.05 BRL")
    cases.append(("decimal_locale_value_grounds", r["block"] is False))

    # (K) dates parsed as ratios: "31/12/2026" also yielded ratio "31/12",
    #     blocked as COMPOSED whenever 31 and 12 were in the ledger.
    r = check("deadline 31/12/2026", "31 items, 12 failed")
    cases.append(("date_not_also_a_ratio",
                  not any(f["kind"] == "ratio" for f in r["flagged"])))
    cases.append(("date_still_needs_grounding", r["block"] is True))
    r = check("Top 10 de 2025 list", "")
    cases.append(("pt_month_year_not_a_ratio", r["tokens"] == 0))
    r = check("found 8 de 12 hosts", "")
    cases.append(("pt_real_ratio_still_flagged", r["block"] is True))

    # (L) explicit declarations whose stems never matched because of the
    #     trailing `\b` ("verificad\b" cannot match "verificado").
    r = check("valor 42% (não verificado, fonte: memória)", "")
    cases.append(("pt_nao_verificado_exempts", r["block"] is False))
    # a DERIVATION word is a claim to be checked, not a declaration of doubt:
    # "derivado"/"convertido"/"soma"/"range" exempting the whole line was a leak
    # lane (an IP on a line with "range" passed as DECLARED)
    r = check("R$ 7.720 derivado da cotação", "")
    cases.append(("pt_derivado_does_not_exempt", r["block"] is True))
    r = check("Valor convertido: R$ 9.731,00", "")
    cases.append(("pt_convertido_does_not_exempt", r["block"] is True))
    r = check("IP range 10.0.0.1 do atacante", "")
    cases.append(("range_word_does_not_exempt", r["block"] is True))

    # --- v2.3: residuals of the v2.2 measurement run ---

    # (M) zero-padded identifiers are not quantities: v2.2's value canonicalization
    #     turned 'PR-0057' into 57 and grounded a fabricated '57%'.
    r = check("pass rate 57%", "ticket PR-0057 merged")
    cases.append(("zero_padded_id_does_not_ground", r["block"] is True))

    # (N) the same date/time in another FORMAT is the same fact. Tool output is
    #     ISO 8601 or `ls` month names; drafts are dd/mm and h:mm.
    ISO = '"merged_at": "2026-09-28T10:55:00Z"'
    r = check("merged at 10:55", ISO)
    cases.append(("iso_timestamp_time_grounds", r["block"] is False))
    r = check("merged on 28/09", ISO)
    cases.append(("iso_date_grounds_ddmm", r["block"] is False))
    r = check("merged on 28/09/2026", ISO)
    cases.append(("iso_date_grounds_full_date", r["block"] is False))
    r = check("merged on 29/09", ISO)
    cases.append(("iso_date_other_day_blocks", r["block"] is True))
    r = check("merged on 28/09/2025", ISO)
    cases.append(("iso_date_other_year_blocks", r["block"] is True))
    r = check("file from 21/09", "-rw-rw-r-- 1 galmanus galmanus 701 set 21 17:22 wave_jev.py")
    cases.append(("pt_ls_month_name_grounds", r["block"] is False))
    r = check("file from 21/09", "-rw-r--r-- 1 root root 701 Sep 21 17:22 wave_jev.py")
    cases.append(("en_ls_month_name_grounds", r["block"] is False))
    r = check("starts at 9:00", "cron: starts 09:00 UTC")
    cases.append(("time_zero_pad_grounds", r["block"] is False))
    #     grep -n and access logs put the time right after a colon
    r = check("error at 06:09", "3375:06:09:18 | sovereign traceback")
    cases.append(("time_after_colon_prefix_grounds", r["block"] is False))
    r = check("hit at 16:28", "[25/Aug/2026:16:28:42 /demo.html 200")
    cases.append(("apache_log_time_grounds", r["block"] is False))
    #     US month-first dates and yyyy/mm/dd paths (news URLs)
    r = check("published 03/18", "https://x.com/news/2026/03/18/story")
    cases.append(("us_order_date_grounds", r["block"] is False))
    r = check("published 18/03/2026", "https://x.com/news/2026/03/18/story")
    cases.append(("url_ymd_date_grounds", r["block"] is False))

    # (O) magnitude suffixes: 'R$296K' is the ledger's 'R$296,332' at the precision
    #     shown; 'R$5M' (one digit + suffix) was not extracted at all.
    r = check("median R$296K", "Median Total Compensation: R$296,332")
    cases.append(("suffix_rounding_grounds", r["block"] is False))
    r = check("median R$20k", "average is R$20,775/month")
    cases.append(("suffix_wrong_rounding_blocks", r["block"] is True))
    r = check("budget R$5M", "")
    cases.append(("single_digit_suffix_extracted", r["block"] is True))
    r = check("budget R$5M", "approved budget: 5,000,000 BRL")
    cases.append(("single_digit_suffix_grounds_by_value", r["block"] is False))
    r = check("budget R$ 20.000", "orçamento de R$ 20 mil aprovado")
    cases.append(("ledger_suffix_scales", r["block"] is False))

    # (Q) precision residuals from the v2.3 orphan label pass
    r = check("receita R$ 1,5M (INFERIDO, base: 2 lojas)", "")
    cases.append(("pt_inferido_exempts", r["block"] is False))
    r = check("CNPJ 61.020.726/0001-80 ativo", "")
    cases.append(("cnpj_not_a_ratio", r["tokens"] == 0))
    r = check("corrigido nas versões 15.2.3 / 14.2.25", "")
    cases.append(("version_list_not_a_ratio", r["tokens"] == 0))
    r = check("contagem batendo (11/4/1/1)", "")
    cases.append(("long_slash_run_not_date", r["tokens"] == 0))
    r = check("vivos 3.218 / 32.193 hosts", "hosts vivos 3.218 / 32.193")
    cases.append(("thousands_ratio_grounds_whole", r["block"] is False))
    r = check("vivos 3.218 / 32.193 hosts", "")
    cases.append(("thousands_ratio_is_one_token",
                  [f["token"] for f in r["flagged"] if f["kind"] == "ratio"] == ["3.218 / 32.193"]))

    # (R) research-verified leaks in v2.3 (roadmap R1)
    r = check("custa R$ 412 mil", "rows 412")
    cases.append(("suffix_base_does_not_ground", r["block"] is True))
    r = check("custa R$ 412K", "rows 412")
    cases.append(("suffix_k_base_does_not_ground", r["block"] is True))
    r = check("custa R$ 412K", "total R$ 412K no contrato")
    cases.append(("suffix_same_form_grounds", r["block"] is False))
    r = check("Queda de -5% no mês", "growth 5% this month")
    cases.append(("sign_flip_blocks", r["block"] is True))
    r = check("Queda de -5% no mês", "change: -5% this month")
    cases.append(("negative_matches_negative", r["block"] is False))
    r = check("variação de −5%", "change: -5% this month")
    cases.append(("unicode_minus_is_minus", r["block"] is False))
    r = check("queda de 5% no mês", "change: -5% this month")
    cases.append(("unsigned_draft_grounds_on_magnitude", r["block"] is False))
    r = check("faixa de 3-5% ao ano", "yield 5% a.a.")
    cases.append(("range_hyphen_is_not_minus", r["block"] is False))
    r = check("revenue was $181,674,817 last year", "")
    cases.append(("bare_dollar_extracted", r["block"] is True))
    r = check("revenue was $181,674,817 last year", "Revenue: 181,674,817 USD")
    cases.append(("bare_dollar_grounds", r["block"] is False))
    r = check("run awk '{print $1}' on the file", "")
    cases.append(("shell_positional_not_currency", r["tokens"] == 0))
    r = check("TVL of USD 1.19m", "")
    cases.append(("usd_prefix_extracted", r["block"] is True))

    # (S) shown arithmetic is RECOMPUTED, not exempted (roadmap R4). The result
    #     is DERIVED only if every operand is grounded (or a unit constant) and
    #     the arithmetic holds at the precision shown.
    L2 = "fare R$ 72,19 fee R$ 9,86"
    r = check("total R$ 72,19 + R$ 9,86 = R$ 82,05", L2)
    cases.append(("derived_sum_recomputed", r["block"] is False))
    r = check("total R$ 72,19 + R$ 9,86 = R$ 90,00", L2)
    cases.append(("derived_wrong_sum_blocks", r["block"] is True))
    r = check("total R$ 72,19 + R$ 9,86 = R$ 82,05", "fare R$ 72,19")
    cases.append(("derived_ungrounded_operand_blocks", r["block"] is True))
    r = check("por mês: R$ 330k / 12 = R$ 27,5k", "receita anual R$ 330k")
    cases.append(("derived_unit_constant_ok", r["block"] is False))
    r = check("R$ 100 × 50 = R$ 5.000", "unit price R$ 100")
    cases.append(("derived_fabricated_factor_blocks", r["block"] is True))
    r = check("R$ 602k − R$ 392k = R$ 210k", "bruto R$ 602k, opex R$ 392k")
    cases.append(("derived_unicode_minus_ok", r["block"] is False))

    # (U) a UUID fragment left after masking must not backtrack into a
    #     shorter signed number ('19c68f0e-579c' produced '-57', grounding 57%)
    r = check("taxa 57%", "sessionId: 19c68f0e-579c-411a-9e2b")
    cases.append(("uuid_fragment_no_backtrack", r["block"] is True))

    # (T) a hyphen in CSS custom properties is not a minus sign
    r = check("queda de -75%", "--wp--preset--dimension--75: 75%;")
    cases.append(("css_double_hyphen_not_minus", r["block"] is True))
    #     an explicit numeric band IS a declaration; the bare word "banda" is not
    r = check("lucro ~R$ 5000 (banda 3k-8k)", "")
    cases.append(("numeric_band_declares", r["block"] is False))
    r = check("plano banda larga 300 Mbps por R$ 99", "")
    cases.append(("banda_larga_not_a_declaration", r["block"] is True))

    # (P) a ratio is ORDERED: "226 of 100" does not ground on "100 of 226"
    r = check("passed 226 of 100", "passed 100 of 226")
    cases.append(("ratio_order_matters", r["block"] is True))

    #     ...but only real month names: "market 25" / "decimal 3" are not dates
    r = check("deadline 25/03", "market 25 closed, decimal 3 places")
    cases.append(("month_prefix_words_not_dates", r["block"] is True))

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
