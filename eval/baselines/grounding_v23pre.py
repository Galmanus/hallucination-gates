# grounding v2.3-pre: the v2.3 matcher BEFORE the research-verified leak fixes (suffix base, sign, EXEMPT words),
# as transplanted into the staged Wave module on 2026-09-28. Kept only as an eval baseline.
#!/usr/bin/env python3
"""
wave_grounding.py — anti-hallucination grounding guard.

Nasceu do caso um site de passagens (2026-09-05): dado operacional de alta
especificidade (empresa, horário, preço, poltronas) foi emitido como se
fosse OBSERVED, sem nenhum artefato de tool que o contivesse. Foi
alucinação. Este guard torna essa classe de erro detectável, não pela
minha disciplina, mas por um teste executável.

DOUTRINA (uma frase falsificável):
  Todo TOKEN-PERIGO na resposta (preço, hora-do-dia, contagem N de M,
  percentual, tx-hash hex, IPv4, data) DEVE rastrear pra um FATO que
  alguma tool de fato retornou neste turno (o "ledger"). Sem lastro e
  sem tag de incerteza declarada -> FLAG como provável alucinação.

MATCHER (v2, 2026-09-09 — conserta a aritmética quebrada do v1):
  O v1 achatava TODO output de tool num único fluxo de dígitos e fazia
  substring. Isso tinha uma falha aritmética: um número inventado de d
  dígitos casa por acaso com prob ~ (L-d) * 10^-d num fluxo de L dígitos.
  Medido contra um ledger real de 857 dígitos (turno PEQUENO): 2 dígitos
  = 51.5% falso-lastreado, 3 dígitos = 16%. E piora com L. O guard ficava
  mais fraco no turno pesado de tool, que é quando mais se alucina número.

  v2 casa contra FATOS DISCRETOS e TIPADOS, nunca substring do fluxo:
  1. o ledger é tokenizado nos MESMOS tipos que o draft (currency/percent/
     time/ipv4/txhash/date/ratio) + números crus do que sobra; o span de
     cada fato tipado é MASCARADO antes de varrer crus, então "92%" NÃO
     gera um "92" cru fantasma (mata a confusão de unidade).
  2. lastro = existe um fato no ledger com o MESMO core de dígitos E de
     tipo COMPATÍVEL. "R$ 92" não lastreia em "92%". "20:00" precisa de um
     time real no ledger, não dos dígitos 2000 dentro de 59320.
     Leniência deliberada (mantém o viés anti-falso-positivo): currency/
     percent/txhash TAMBÉM lastreiam num número CRU de mesmo valor, porque
     tool costuma devolver número sem unidade (JSON "port": 8443).
  3. razão (N de M) é RELACIONAL: só é GROUNDED se a razão inteira co-
     ocorre no ledger. Se os dois operandos existem soltos mas a razão
     não, o veredito é COMPOSED (operandos reais, relação sua) — a lição
     exata do caso 72/76.

MATCHER v2.1 (2026-09-10 — tipos ESTRUTURAIS):
  digit_core() achatava txhash/ipv4 em só dígitos, então um hash alucinado
  lastreava num número coincidente e um IP alucinado num IP DIFERENTE de
  dígitos concatenados iguais (a6281d89 vs o byte-count 628189; 1.22.3.4 vs
  12.2.3.4). Correção de PRECISÃO, não de sensibilidade (o pré-mortem: subir
  sensibilidade mata o guard por fadiga de alarme): estes casam pelo TOKEN
  normalizado inteiro (hex minúsculo / quad pontilhado canônico), o resto
  mantém a leniência de valor. Fecha um falso-NEGATIVO na classe de maior
  aposta reputacional (um receipt num disclosure) a ~zero custo de falso-+.

REGRA FINA (verdito por token):
  - fato tipado com core+tipo compatível no ledger  -> GROUNDED (ok)
  - razão cujos operandos existem mas a razão não    -> COMPOSED (sev 1,
                                                        bloqueia: relação
                                                        composta por mim)
  - sem lastro, mas a linha declara INFERRED/RECALL/estimativa/unverified
    /banda                                           -> DECLARED (ok)
  - sem lastro e a linha diz OBSERVED                -> FALSE_OBSERVED (sev
                                                        3: afirmou ver o que
                                                        não viu)
  - sem lastro e sem tag                             -> UNSOURCED (sev 2, a
                                                        alucinação silenciosa)

TETO HONESTO (não finjo além disto):
  1. Pega FATO-DURO (números/receipts), não alucinação mole: uma causal
     errada sem token numérico ("o site exige login" quando não exige)
     passa batido. Detector de fumaça pra a classe que nos queimou, não
     prova de correção semântica.
  2. Leniência do número cru (regra 2) é um falso-NEGATIVO deliberado:
     "R$ 8.443" lastreia num ledger que só tem o cru "8443" (ex: uma
     porta). Escolha consciente pra nunca bloquear indevido (o guard que
     grita lobo é desligado). A confusão de unidade EXPLÍCITA (unidade
     conflitante no ledger, ex: 92%) é pega; a implícita (cru) não.
  3. Valor DERIVADO por aritmética (somas, conversões de fuso) não é fato
     do ledger e ainda não é modelado: "total R$ 82,05" a partir de dois
     preços somados vai flagar como UNSOURCED se o total não estiver no
     ledger. Tag a linha como estimativa/soma pra escapar (vira DECLARED).
  4. Roda pela MINHA mão (dose 1). A parede real é wire como Stop-hook
     (dose 2) que intercepta o output antes de sair. Disanalogia soft-vs-
     hard do Whonix: declarativo, não compilado.

Stdlib only. `--selftest` reproduz o caso do ônibus, o caso 72/76, a
confusão de unidade e o falso-lastro por concatenação (todos têm que FLAG),
mais casos lastreados que têm que passar.
"""
import argparse
import json
import re
import sys

# (matcher v2.3 transplantado de hallucination-gates/grounding.py, 2026-09-28; ver eval/RESULTS.md)
VERSION = "2.3"
# ---- danger tokens: what an operational hallucination usually asserts ----
# {L}/{R} are token boundaries. In the DRAFT they are plain `\b`. In the LEDGER
# they are glue-tolerant (v2.2): tool output glues values to identifiers
# ("U_e2629...", "2026-06-16_03:12") where `\b` fails and the fact was lost.
_DANGER_SPEC = [
    ("currency",   r'(?:R\$|US?\$)\s?\d(?:[\d.,]*\d)?(?:\s?' + r'{SUF}' + r')?(?![0-9A-Za-z])'),
    ("date",       r'{L}\d{1,2}/\d{1,2}(?:/\d{2,4})?{R}'),
    # operands may carry thousands separators (3.218 / 32.193) so they are not cut in half
    ("ratio",      r'{L}(?:\d{1,3}(?:[.,]\d{3})+|\d+)\s*(?:de|of|/)\s*(?:\d{1,3}(?:[.,]\d{3})+|\d+){R}'),  # "de" = PT "of"
    ("time",       r'{L}\d{1,2}:\d{2}{R}'),
    ("percent",    r'{L}\d[\d.,]*\s*%'),
    ("ipv4",       r'{L}\d{1,3}(?:\.\d{1,3}){3}{R}'),
    ("strkey",     r'{L}[GC][A-Z2-7]{55}{R}'),                  # Stellar account / contract id
    ("txhash",     r'{L}(?:0x)?[0-9a-fA-F]{8,}{R}'),            # hex receipt, 0x optional (v2.2)
    ("base58",     r'{L}[1-9A-HJ-NP-Za-km-z]{32,88}{R}'),       # Solana-style pubkey / signature
]
# magnitude suffixes (v2.3): 'R$296K', 'R$ 20 mil', 'US$ 1,5 bi', 'R$5M'
_SUFFIX_MULT = [(r'milh[õo]es|milh[ãa]o|million|millions|mi|MM|M', 10**6),
                (r'bilh[õo]es|bilh[ãa]o|billion|billions|bi|bn|B', 10**9),
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
    r'n[aã]o[- ]verificad\w*|unverified|prov[aá]vel|banda|range|'
    r'\+/-|soma|somado|derivad\w*|convertid\w*|approx|aproximad|talvez|acho que)\b',
    re.IGNORECASE)
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
        base = base | {_canon(Decimal(v) * mult) for v in base if not (len(v) > 1 and v[0] == '0' and '.' not in v)}
    return base


def _num_values_base(s, side="draft"):
    from decimal import Decimal, InvalidOperation
    m = _NUM.search(s)
    if not m:
        return set()
    t = m.group(0)
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
    for m in _NUM.finditer(rest):
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


def _exempt(line):
    return bool(EXEMPT.search(line) or EXEMPT_SYM.search(line))


def classify(tok, kind, line, facts, ratio_pairs):
    grounded, composed = _match(tok, kind, facts, ratio_pairs)
    if grounded:
        return "GROUNDED"
    if _exempt(line):
        return "DECLARED"          # dito incerto, honesto (cobre até composed)
    if composed:
        return "COMPOSED"          # operandos reais, relação não está na fonte
    if OBSERVED.search(line):
        return "FALSE_OBSERVED"    # afirmou ver o que o ledger não tem
    return "UNSOURCED"             # a alucinação silenciosa


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
        f"  tokens-perigo: {r['tokens']}  |  lastreados: {r['grounded']}  "
        f"|  declarados-incertos: {r['declared']}  |  compostos: {r.get('composed', 0)}  "
        f"|  flagados: {len(r['flagged'])}",
    ]
    if r["flagged"]:
        lines.append("  --- FLAGADOS (sem lastro discreto no ledger) ---")
        label = {3: "FALSE_OBSERVED!", 2: "UNSOURCED", 1: "COMPOSED"}
        for f in r["flagged"]:
            tag = label[f["severity"]]
            lines.append(f"  [{tag}] {f['kind']:8} {f['token']!r}")
            lines.append(f"           linha: {f['line']!r}")
    else:
        lines.append("  todo token-perigo lastreia num fato discreto do ledger ou foi declarado incerto.")
    return "\n".join(lines)


# ------------------------------- selftest -------------------------------
BUS_DRAFT = """
empresa Novo Horizonte, servico Executivo
saida 20:00 (Cuiaba), chegada 23:40 (Rondonopolis)
tarifa R$ 72,19 + taxa R$ 9,86 = total R$ 82,05
poltronas livres 27 de 46
"""
BUS_LEDGER = "site-de-passagens home pagina 59320 bytes csrf token laravel sessao"

GROUNDED_DRAFT = """
operador LogTrans, poltrona Convencional
saida 06:00 chegada 10:08, preco R$ 79,62
"""
GROUNDED_LEDGER = ("clickbus rota cuiaba rondonopolis LogTrans Convencional "
                   "06:00 10:08 R$ 79,62 outras saidas 09:00 12:30")

DECLARED_DRAFT = "lucro mensal ~R$ 5000 (INFERRED, estimativa, banda 3k-8k)"


def selftest():
    cases = []

    # --- casos herdados do v1 (não podem regredir) ---
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

    r = check("exit Tor OBSERVED 192.42.116.102", "ledger vazio sem esse ip")
    cases.append(("false_observed_flagged",
                  any(f["verdict"] == "FALSE_OBSERVED" for f in r["flagged"])))

    r = check("exit Tor OBSERVED 192.42.116.102", "verify ip 192.42.116.102 IsTor true")
    cases.append(("observed_with_ledger_ok", r["block"] is False))

    r = check("a estrategia certa aqui e mapear a agenda antes de agir", "")
    cases.append(("negative_control", r["tokens"] == 0 and r["block"] is False))

    # --- v2: o que o v1 deixava passar por concatenação/unidade ---

    # (A) falso-lastro por concatenação: '2746' existe no fluxo de '8027'+'46',
    #     mas 27 NAO e um fato discreto. v1 -> GROUNDED (falso). v2 -> flag.
    r = check("achei 27 de 46 hosts vivos",
              "scan porta 8027 e 46 hosts responderam ao ping")
    cases.append(("concat_false_ground_caught", r["block"] is True))

    # (B) relacao composta (o caso 72/76): operandos reais como percentuais
    #     separados, a razao nao esta na fonte -> COMPOSED, bloqueia.
    r = check("o SAFE acerta 72/76",
              "SAFE bate humano em 72% e ganha 76% das discordancias")
    cases.append(("composed_relation_verdict",
                  any(f["verdict"] == "COMPOSED" for f in r["flagged"])))
    cases.append(("composed_relation_blocks", r["block"] is True))

    # (B2) mesma razao, mas declarada incerta -> DECLARED, nao bloqueia
    r = check("o SAFE acerta ~72/76 (estimativa)",
              "SAFE bate humano em 72% e ganha 76% das discordancias")
    cases.append(("composed_declared_ok", r["block"] is False))

    # (B3) razao que REALMENTE co-ocorre no ledger -> GROUNDED
    r = check("livres 27 de 46",
              "poltronas: 27 de 46 disponiveis no onibus")
    cases.append(("ratio_cooccurs_grounded", r["block"] is False))

    # (C) confusao de unidade explicita: R$ 92 contra ledger que so tem 92%
    r = check("cobra R$ 92 pela hora", "a cobertura foi de 92% no teste")
    cases.append(("unit_mismatch_caught", r["block"] is True))

    # (D) leniencia deliberada: numero CRU sem unidade lastreia currency
    #     (teto honesto #2 — nao introduzir falso-positivo)
    r = check("o retainer fecha em R$ 20.000/mes",
              "estimador devolveu central 20000 por mes")
    cases.append(("bare_number_lenient_pass", r["block"] is False))

    # (E) time exige forma real: 20:00 NAO lastreia nos digitos 2000 crus
    r = check("saida as 20:00", "resultado 2000 pontos no teste")
    cases.append(("time_needs_real_time", r["block"] is True))

    # --- v2.1 (2026-09-10): tipos ESTRUTURAIS — o nao-digito E a identidade ---
    # digit_core() achata hash/IP/data em so digitos; as letras hex, os pontos e
    # as barras SAO a identidade. Sem isso o guard lastreia um receipt ERRADO num
    # numero coincidente — a classe de maior aposta reputacional (tx/IP/data num
    # disclosure). Falso-NEGATIVO provado ao vivo 2026-09-10.

    # (F) txhash: hash alucinado NAO lastreia num numero cru de digitos coincidentes
    r = check("restore tx OBSERVED a6281d89", "log da corrida linha 628189 bytes")
    cases.append(("txhash_hex_letters_matter", r["block"] is True))

    # (F2) hash REAL que esta no ledger continua lastreando (guarda anti-overshoot)
    r = check("restore tx OBSERVED a6281d89", "tx confirmada a6281d89 no explorer")
    cases.append(("txhash_real_still_grounds", r["block"] is False))

    # (G) ipv4: IP alucinado NAO lastreia num IP DIFERENTE de digitos concatenados iguais
    r = check("exit Tor OBSERVED 1.22.3.4", "verify ip 12.2.3.4 IsTor true")
    cases.append(("ipv4_dot_positions_matter", r["block"] is True))

    # (G2) IP REAL que esta no ledger continua lastreando (guarda anti-overshoot)
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

    c = sub.add_parser("check", help="lastreia um draft contra o ledger de tools do turno")
    c.add_argument("--draft", required=True, help="arquivo com a resposta rascunho")
    c.add_argument("--ledger", help="arquivo com os outputs de tool deste turno (vazio=nada saiu)")
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
