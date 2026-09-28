# grounding v1 (substring matcher), from the original wave_grounding.py at commit 37e759f, 2026-09-07,
# verbatim except that one ticket-vendor name is anonymized. Kept only as the eval baseline. Do not use: see grounding.py.
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
  percentual, tx-hash hex, IPv4, data) DEVE rastrear pra um byte que
  alguma tool de fato retornou neste turno (o "ledger"). Sem lastro e
  sem tag de incerteza declarada -> FLAG como provável alucinação.

REGRA FINA (a lição exata do caso):
  - token com lastro no ledger              -> GROUNDED (ok)
  - sem lastro, mas a linha declara INFERRED/RECALL/estimativa/unverified
    /banda                                  -> DECLARED (ok, dito incerto)
  - sem lastro e a linha diz OBSERVED       -> FALSE_OBSERVED (severidade
                                               máxima: afirmou ver o que
                                               não viu)
  - sem lastro e sem tag                    -> UNSOURCED (a alucinação
                                               silenciosa do caso do ônibus)

TETO HONESTO (não finjo além disto):
  1. Pega FATO-DURO (números/receipts), não alucinação mole: uma causal
     errada sem token numérico ("o site exige login" quando não exige)
     passa batido. É detector de fumaça pra a classe que nos queimou,
     não prova de correção semântica.
  2. Match de dígito é por substring do fluxo de dígitos do ledger:
     pode dar falso-GROUNDED se a sequência aparecer por acaso dentro de
     outro número. Falso-negativo, não falso-positivo: erra pro lado de
     deixar passar, nunca de bloquear indevido.
  3. Roda pela MINHA mão (dose 1). A parede real é wire como Stop-hook
     (dose 2) que intercepta o output antes de sair. Mesma disanalogia
     soft-vs-hard do Whonix.

Stdlib only. `--selftest` reproduz o caso do ônibus (tem que FLAG) e um
caso lastreado (tem que passar).
"""
import argparse
import json
import re
import sys

# ---- token-perigo: o que uma alucinação operacional costuma cravar ----
DANGER = [
    ("currency",   re.compile(r'(?:R\$|US?\$)\s?\d[\d.,]*\d|\bR\$\s?\d\b')),
    ("ratio",      re.compile(r'\b\d+\s*(?:de|of|/)\s*\d+\b')),
    ("time",       re.compile(r'\b\d{1,2}:\d{2}\b')),
    ("percent",    re.compile(r'\b\d[\d.,]*\s*%')),
    ("ipv4",       re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')),
    ("txhash",     re.compile(r'\b[0-9a-fA-F]{8,}\b')),
    ("date",       re.compile(r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b')),
]

# tags que DECLARAM incerteza -> o token deixa de ser afirmado como fato vivo
EXEMPT = re.compile(
    r'\b(INFERRED|RECALL|estimativa|estimado|estimate|estimated|'
    r'n[aã]o[- ]verificad|unverified|prov[aá]vel|banda|range|'
    r'\+/-|±|aproximad|approx|talvez|acho que)\b', re.IGNORECASE)

# tag que AFIRMA observação direta -> sem lastro isto é o pior caso
OBSERVED = re.compile(r'\bOBSERVED\b', re.IGNORECASE)


def digit_core(s):
    """assinatura numérica de um token: só os dígitos, len>=2."""
    d = re.sub(r'\D', '', s)
    return d if len(d) >= 2 else ''


def ledger_digits(ledger):
    """fluxo de todos os dígitos que as tools retornaram neste turno."""
    return re.sub(r'\D', '', ledger or '')


def extract(draft):
    """(token, kind, line_text) por token-perigo, dedup por (token,linha)."""
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


def classify(tok, line, led_digits):
    core = digit_core(tok)
    grounded = bool(core) and core in led_digits
    if grounded:
        return "GROUNDED"
    if OBSERVED.search(line):
        return "FALSE_OBSERVED"   # afirmou ver o que o ledger não tem
    if EXEMPT.search(line):
        return "DECLARED"         # dito incerto, honesto
    return "UNSOURCED"            # a alucinação silenciosa


SEVERITY = {"FALSE_OBSERVED": 3, "UNSOURCED": 2, "DECLARED": 0, "GROUNDED": 0}


def check(draft, ledger):
    led = ledger_digits(ledger)
    findings = []
    for tok, kind, line in extract(draft):
        verdict = classify(tok, line, led)
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
        "flagged": flagged,
        "verdict": "HALLUCINATION_RISK" if flagged else "GROUNDED_OK",
        "block": bool(flagged),
    }


def render(r):
    lines = [
        f"grounding: {r['verdict']}",
        f"  tokens-perigo: {r['tokens']}  |  lastreados: {r['grounded']}  "
        f"|  declarados-incertos: {r['declared']}  |  flagados: {len(r['flagged'])}",
    ]
    if r["flagged"]:
        lines.append("  --- FLAGADOS (provável alucinação, sem lastro) ---")
        for f in r["flagged"]:
            tag = {3: "FALSE_OBSERVED!", 2: "UNSOURCED"}[f["severity"]]
            lines.append(f"  [{tag}] {f['kind']:8} {f['token']!r}")
            lines.append(f"           linha: {f['line']!r}")
    else:
        lines.append("  todo token-perigo tem lastro no ledger ou foi declarado incerto.")
    return "\n".join(lines)


# ------------------------------- selftest -------------------------------
BUS_DRAFT = """
empresa Novo Horizonte, servico Executivo
saida 20:00 (Cuiaba), chegada 23:40 (Rondonopolis)
tarifa R$ 72,19 + taxa R$ 9,86 = total R$ 82,05
poltronas livres 27 de 46
"""
# o que a tool DE FATO voltou naquele turno: casca da home, sem os dados.
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

    r = check(BUS_DRAFT, BUS_LEDGER)
    # tem que flagar: 20:00, 23:40, 72,19, 9,86, 82,05, 27 de 46
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

    # OBSERVED sem lastro = pior caso
    r = check("exit Tor OBSERVED 192.42.116.102", "ledger vazio sem esse ip")
    cases.append(("false_observed_flagged",
                  any(f["verdict"] == "FALSE_OBSERVED" for f in r["flagged"])))

    # OBSERVED COM lastro = ok
    r = check("exit Tor OBSERVED 192.42.116.102", "verify ip 192.42.116.102 IsTor true")
    cases.append(("observed_with_ledger_ok", r["block"] is False))

    # controle negativo: texto sem token-perigo nunca bloqueia
    r = check("a estrategia certa aqui e mapear a agenda antes de agir", "")
    cases.append(("negative_control", r["tokens"] == 0 and r["block"] is False))

    passed = sum(1 for _, ok in cases if ok)
    for name, ok in cases:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\nselftest: {passed}/{len(cases)}")
    return passed == len(cases)


def main():
    ap = argparse.ArgumentParser(description="anti-hallucination grounding guard")
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
