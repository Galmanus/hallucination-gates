#!/usr/bin/env python3
"""
claim.py — claim-provenance / egress gate for outgoing findings.

The THIRD and LAST of three walls in a "near-zero" hallucination defense, and the
hardest one, because it does not judge a signal or a source: it judges the TEXT
that is about to leave the machine under the author's name. It is the
`verify-before-claim` discipline turned executable at the point where it matters
most: the egress boundary (client report, public disclosure, GitHub issue/PR,
post). The two prior walls produce artifacts; this wall refuses to let ANY
statement out unless it is BOUND to an accepted artifact, at the STRENGTH that
artifact actually authorizes.

The gap this wall closes (the one the first two do not catch):
  the repro gate proves a finding REPRODUCES. The corroborate gate proves a fact
  is CORROBORATED. But nothing stops a researcher from, at write-up time,
  stamping "critical RCE" on top of an artifact that only proves "endpoint
  returns 200", or stating as fact a sentence no artifact backs, or inheriting
  lab severity as if it were production impact. The error is not in the signal
  or the source: it is in the JUMP between evidence and sentence. This is that
  jump.

THE DOCTRINE (one falsifiable sentence):
  No claim of EXISTENCE, FACT, or SEVERITY leaves under the author's name unless
  it is bound to an artifact whose VERDICT authorizes it, in the right CLASS
  (existence <- repro REPRODUCED; fact <- corroborate CORROBORATED) and at the
  right STRENGTH (lab severity does not become "critical" without production
  impact). Analysis and opinion may go out, but only LABELED as derivation,
  never as fact.

THE FOUR GATES (the heart of the tool):

  1. BIND OR DO NOT CLAIM. A claim of kind existence/fact/severity/impact
     WITHOUT a binding to an artifact falls as UNGROUNDED. This is not "not yet
     verified", it is "cannot go out as fact". Analysis goes out as
     `inference`, recommendation as `recommendation`: allowed, but LABELED,
     never dressed up as fact.

  2. RIGHT ARTIFACT CLASS. Existence is only authorized by a repro artifact
     with verdict REPRODUCED. Fact only by a corroborate artifact with
     CORROBORATED. Binding existence to a corroborate artifact (or vice versa)
     is WRONG_WALL. Binding to a verdict that does not authorize it
     (NOT_REPRODUCED, CONFLICTED, SINGLE_SOURCE...) is CONTRADICTED: the
     artifact exists and says the OPPOSITE of what the claim states.

  3. SEVERITY CEILING (the part the first two walls explicitly left open).
     REPRODUCED in lab is a condition to CLAIM EXISTENCE, not to stamp
     SEVERITY. Without confirmed production impact, the ceiling is `high`;
     with a weak negative control (control_weak), the ceiling drops to
     `medium`; and for a `public` audience without prod, the ceiling drops to
     `medium`. Declaring above the ceiling is OVERCLAIM. Severity without a
     derivation block (`severity_factors`) is UNJUSTIFIED: a severity number
     with no stated origin.

  4. FRESHNESS. An artifact has a date. If the claim declares `max_age_days`
     and the artifact is older than that, it falls as STALE: reproduction
     ages, the target changes, and asserting today on proof from three months
     ago is hallucination with a stamp on it. Without a declared
     `max_age_days`, freshness is not checked (opt-in).

THE VERDICTS (the only axis the tool decides, at REPORT level):
  CLEARED  -> every claim is either AUTHORIZED (bound and authorized) or TAGGED
              (inference/opinion/recommendation, labeled). Only this clears
              egress.
  BLOCKED  -> at least one claim failed. The report lists each blocker and the
              reason. Nothing goes out until every blocker clears or the claim
              is withdrawn.

Per-claim status: AUTHORIZED, TAGGED, UNGROUNDED, ARTIFACT_MISSING, WRONG_WALL,
CONTRADICTED, OVERCLAIM, UNJUSTIFIED, STALE.

HONEST CEILING (no claim beyond this; this is the hardest gap left):
  1. The wall checks that the claim is BOUND to an artifact that AUTHORIZES it
     by CLASS and STRENGTH. It does NOT check that the claim's free text
     semantically matches the artifact. "endpoint leaks ALL PII data" bound to
     a repro artifact that only proves "returns 200" passes the class check
     and lies in the text. Text<->artifact entailment is `needs_judge`: a
     human or an LLM judge, with its own error rate. This is the hardest
     residual, and it is named here, not hidden.
  2. The wall trusts the artifact's VERDICT. If the repro gate returned
     REPRODUCED on a lazy prediction, or the corroborate gate returned
     CORROBORATED on top of two sources that made the same mistake, this wall
     inherits the error. It walls the COHERENCE between claim and artifact,
     not the artifact's truth. The chain is only as strong as the weakest of
     the three walls.
  3. It judges the claims you WROTE as claims. A statement buried in report
     prose that was never declared as a claim is not gated. It defends
     declared claims, not undeclared prose (the same ceiling as the
     `derives_from` gap in the second wall: hidden provenance/assertion is the
     next gap to close).
  4. The severity ceiling is a heuristic map (lab -> non-critical), not a
     computed CVSS score. It blocks gross overclaim by rule; it does not
     compute real production impact. `prod_confirmed` is declared by whoever
     assembles the case and should itself have supporting evidence; the wall
     trusts the declaration, not proof of it.

Standard library only. `--selftest` exercises every verdict and every gate
offline (inline verdicts, no file or network access).

Usage:
  python3 claim.py gate <report.json>     # emit the report's verdict + per-claim status
  python3 claim.py template               # print a sample report
  python3 claim.py --selftest             # offline gate
"""
import sys
import os
import json
import argparse
from datetime import datetime, timezone, timedelta


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ===========================================================================
# claim taxonomy: what each kind requires
# ===========================================================================
# claims that ASSERT reality: need an artifact that authorizes them.
FACTUAL_KINDS = {"existence", "fact", "severity", "impact"}
# claims that are reading/recommendation: go out, but LABELED, never as fact.
LABELED_KINDS = {"inference", "opinion", "recommendation"}
ALL_KINDS = FACTUAL_KINDS | LABELED_KINDS

# which artifact class authorizes which kind, and which verdict is the only one that passes
KIND_TO_WALL = {
    "existence": ("repro", "REPRODUCED"),
    "severity":  ("repro", "REPRODUCED"),
    "impact":    ("repro", "REPRODUCED"),
    "fact":      ("corroborate", "CORROBORATED"),
}

SEV_ORDER = ["info", "low", "medium", "high", "critical"]


def _sev_idx(s):
    try:
        return SEV_ORDER.index((s or "").lower())
    except ValueError:
        return -1


# ===========================================================================
# artifact resolution: alias -> {kind, verdict, ...meta}
# accepts an inline verdict (for tests/direct use) OR a repro_/corrob_ file on disk
# ===========================================================================

def resolve_artifact(spec):
    """
    spec: {"kind": "repro"|"corroborate", "file": "..."} OR with an inline "verdict".
    Returns (art_dict, error). art_dict has at minimum: kind, verdict.
    """
    if not isinstance(spec, dict):
        return None, "artifact-spec-must-be-object"
    kind = spec.get("kind")
    if kind not in ("repro", "corroborate"):
        return None, f"unknown-artifact-kind:{kind}"

    # inline verdict wins (direct use, offline selftest)
    if "verdict" in spec:
        art = {k: v for k, v in spec.items() if k != "file"}
        return art, None

    f = spec.get("file")
    if not f:
        return None, "artifact-without-file-or-verdict"
    if not os.path.exists(f):
        return None, f"artifact-file-missing:{f}"
    try:
        with open(f) as fh:
            data = json.load(fh)
    except Exception as e:
        return None, f"artifact-unreadable:{type(e).__name__}"
    if "verdict" not in data:
        return None, f"artifact-has-no-verdict:{f}"
    art = {
        "kind": kind,
        "verdict": data.get("verdict"),
        "control_weak": data.get("control_weak", False),
        "produced_at": data.get("run_at") or data.get("checked_at"),
        "source_file": f,
    }
    return art, None


# ===========================================================================
# severity ceiling: what the lab artifact actually authorizes
# ===========================================================================

def severity_cap(art, factors, audience):
    """
    Returns (cap_level_str, reason). Cap = highest severity the evidence
    authorizes. REPRODUCED in lab != production impact.
    """
    prod = bool((factors or {}).get("prod_confirmed"))
    weak = bool(art.get("control_weak"))

    if prod:
        cap = "critical"
        reason = "prod_confirmed=true; ceiling released"
    elif weak:
        cap = "medium"
        reason = "weak negative control (control_weak); ceiling=medium"
    else:
        cap = "high"
        reason = "lab REPRODUCED without prod_confirmed; ceiling=high"

    # public audience without prod: tighten the ceiling (do not publish high
    # severity based on lab alone)
    if audience == "public" and not prod:
        if _sev_idx(cap) > _sev_idx("medium"):
            cap = "medium"
            reason += "; audience=public without prod tightens to medium"

    return cap, reason


# ===========================================================================
# evaluating a single claim
# ===========================================================================

def eval_claim(claim, artifacts, audience):
    """
    artifacts: dict alias -> (art_dict|None, err). Returns a status dict.
    """
    cid = claim.get("id", "?")
    kind = claim.get("kind")
    base = {"id": cid, "kind": kind, "text": claim.get("text")}

    if kind not in ALL_KINDS:
        return {**base, "status": "UNGROUNDED",
                "why": f"unknown kind:{kind}; no rule authorizes it"}

    # labeled claims: go out, but marked as non-fact
    if kind in LABELED_KINDS:
        note = "labeled as derivation/opinion; NOT asserted as fact"
        # even if an inference/opinion claim carries a binding, that's fine, it's just not required
        return {**base, "status": "TAGGED", "why": note}

    # from here down: FACTUAL_KINDS, binding required
    binds = claim.get("binds") or []
    if not binds:
        return {**base, "status": "UNGROUNDED",
                "why": "factual claim without a binding to an artifact; cannot go out as fact"}

    wall, need_verdict = KIND_TO_WALL[kind]

    # resolve the first binding (a claim binds to a primary artifact;
    # multiple bindings: requires that ALL authorize it, the weakest one decides)
    resolved = []
    for alias in binds:
        pair = artifacts.get(alias)
        if pair is None:
            return {**base, "status": "ARTIFACT_MISSING",
                    "why": f"binding '{alias}' is not in artifacts"}
        art, err = pair
        if err:
            return {**base, "status": "ARTIFACT_MISSING",
                    "why": f"artifact '{alias}' failed to resolve: {err}"}
        resolved.append((alias, art))

    for alias, art in resolved:
        # right class?
        if art.get("kind") != wall:
            return {**base, "status": "WRONG_WALL",
                    "why": f"kind '{kind}' requires a {wall} artifact, but "
                           f"'{alias}' is {art.get('kind')}"}
        # verdict authorizes it?
        if art.get("verdict") != need_verdict:
            return {**base, "status": "CONTRADICTED",
                    "why": f"'{alias}' has verdict {art.get('verdict')}, "
                           f"and '{kind}' requires {need_verdict}"}

    # freshness (opt-in)
    max_age = claim.get("max_age_days")
    if max_age is not None:
        for alias, art in resolved:
            ts = _parse_ts(art.get("produced_at"))
            if ts is None:
                return {**base, "status": "STALE",
                        "why": f"'{alias}' has no timestamp; cannot prove freshness "
                               f"with max_age_days={max_age}"}
            age = datetime.now(timezone.utc) - ts
            if age > timedelta(days=max_age):
                return {**base, "status": "STALE",
                        "why": f"'{alias}' is {age.days}d old, over "
                               f"max_age_days={max_age}"}

    # severity/impact: ceiling
    if kind in ("severity", "impact"):
        declared = claim.get("severity")
        if declared is None:
            return {**base, "status": "UNJUSTIFIED",
                    "why": "severity claim without a `severity` field"}
        if _sev_idx(declared) < 0:
            return {**base, "status": "UNJUSTIFIED",
                    "why": f"severity '{declared}' is not in {SEV_ORDER}"}
        factors = claim.get("severity_factors")
        if not factors:
            return {**base, "status": "UNJUSTIFIED",
                    "why": "severity without `severity_factors`: a number with no derivation"}
        # use the primary (first) artifact for the ceiling
        _, art0 = resolved[0]
        cap, reason = severity_cap(art0, factors, audience)
        if _sev_idx(declared) > _sev_idx(cap):
            return {**base, "status": "OVERCLAIM", "severity": declared,
                    "cap": cap,
                    "why": f"severity '{declared}' above ceiling '{cap}' ({reason})"}
        return {**base, "status": "AUTHORIZED", "severity": declared, "cap": cap,
                "why": f"severity '{declared}' <= ceiling '{cap}' ({reason})",
                "semantic_unverified": True}

    # existence / fact authorized
    return {**base, "status": "AUTHORIZED",
            "why": f"bound to {wall} {need_verdict} via {[a for a, _ in resolved]}",
            "semantic_unverified": True}


# ===========================================================================
# gating the whole report
# ===========================================================================

PASS_STATUSES = {"AUTHORIZED", "TAGGED"}


def gate_report(report):
    audience = report.get("audience", "client")
    # resolve every artifact once
    artifacts = {}
    for alias, spec in (report.get("artifacts") or {}).items():
        artifacts[alias] = resolve_artifact(spec)

    claims_out = []
    for claim in report.get("claims") or []:
        claims_out.append(eval_claim(claim, artifacts, audience))

    blockers = [c for c in claims_out if c["status"] not in PASS_STATUSES]
    verdict = "CLEARED" if not blockers else "BLOCKED"

    n_factual = sum(1 for c in claims_out if c["kind"] in FACTUAL_KINDS)
    n_semantic_unverified = sum(1 for c in claims_out if c.get("semantic_unverified"))

    result = {
        "id": report.get("id"),
        "audience": audience,
        "gated_at": _now(),
        "verdict": verdict,
        "n_claims": len(claims_out),
        "n_factual": n_factual,
        "n_blockers": len(blockers),
        "claims": claims_out,
    }
    if blockers:
        result["blockers"] = [{"id": c["id"], "status": c["status"], "why": c["why"]}
                              for c in blockers]
    if n_semantic_unverified:
        result["note_semantic"] = (
            f"{n_semantic_unverified} claim(s) AUTHORIZED by class+strength, but "
            "text<->artifact entailment was NOT verified (needs_judge). Honest "
            "ceiling: this wall proves the binding, not semantic correspondence."
        )
    return result


# ===========================================================================
# template
# ===========================================================================

TEMPLATE = {
    "id": "example-report-001",
    "audience": "client",
    "artifacts": {
        "bola-repro": {"kind": "repro", "file": "repro_example-bola-001.json"},
        "subs-corr":  {"kind": "corroborate", "file": "corrob_ab12cd34.json"}
    },
    "claims": [
        {"id": "c1", "kind": "existence",
         "text": "GET /accounts/{other_id} returns another user's data (BOLA)",
         "binds": ["bola-repro"], "max_age_days": 30},
        {"id": "c2", "kind": "fact",
         "text": "the subdomain admin.example.com exists and resolves",
         "binds": ["subs-corr"]},
        {"id": "c3", "kind": "severity",
         "text": "severity of the BOLA finding",
         "binds": ["bola-repro"], "severity": "high",
         "severity_factors": {"prod_confirmed": False, "auth_bypass": True,
                              "data_class": "PII", "note": "reproduced in lab"}},
        {"id": "c4", "kind": "inference",
         "text": "the pattern suggests a shared authorization layer across services"},
        {"id": "c5", "kind": "recommendation",
         "text": "implement per-object ownership checks at the gateway"}
    ]
}


# ===========================================================================
# selftest
# ===========================================================================

def _selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    A_REPRO_OK = {"kind": "repro", "verdict": "REPRODUCED"}
    A_REPRO_WEAK = {"kind": "repro", "verdict": "REPRODUCED", "control_weak": True}
    A_REPRO_BAD = {"kind": "repro", "verdict": "NOT_REPRODUCED"}
    A_CORR_OK = {"kind": "corroborate", "verdict": "CORROBORATED"}
    A_CORR_BAD = {"kind": "corroborate", "verdict": "CONFLICTED"}

    # -- CLEARED: existence bound to repro REPRODUCED
    r = gate_report({
        "id": "t1", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["e"]}]
    })
    ck("existence+REPRODUCED -> CLEARED", r["verdict"] == "CLEARED")
    ck("claim AUTHORIZED", r["claims"][0]["status"] == "AUTHORIZED")
    ck("emits note_semantic (honest ceiling)", "note_semantic" in r)

    # -- fact bound to corroborate CORROBORATED
    r = gate_report({
        "id": "t2", "artifacts": {"f": A_CORR_OK},
        "claims": [{"id": "c1", "kind": "fact", "text": "x", "binds": ["f"]}]
    })
    ck("fact+CORROBORATED -> CLEARED", r["verdict"] == "CLEARED")

    # -- UNGROUNDED: factual without binding
    r = gate_report({
        "id": "t3", "artifacts": {},
        "claims": [{"id": "c1", "kind": "existence", "text": "x"}]
    })
    ck("existence without binding -> BLOCKED", r["verdict"] == "BLOCKED")
    ck("status UNGROUNDED", r["claims"][0]["status"] == "UNGROUNDED")

    # -- ARTIFACT_MISSING: binding points to a nonexistent alias
    r = gate_report({
        "id": "t4", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["ZZZ"]}]
    })
    ck("nonexistent binding -> ARTIFACT_MISSING",
       r["claims"][0]["status"] == "ARTIFACT_MISSING")

    # -- WRONG_WALL: existence bound to corroborate
    r = gate_report({
        "id": "t5", "artifacts": {"f": A_CORR_OK},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["f"]}]
    })
    ck("existence on corroborate -> WRONG_WALL",
       r["claims"][0]["status"] == "WRONG_WALL")

    # -- WRONG_WALL: fact bound to repro
    r = gate_report({
        "id": "t5b", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "fact", "text": "x", "binds": ["e"]}]
    })
    ck("fact on repro -> WRONG_WALL", r["claims"][0]["status"] == "WRONG_WALL")

    # -- CONTRADICTED: existence bound to repro NOT_REPRODUCED
    r = gate_report({
        "id": "t6", "artifacts": {"e": A_REPRO_BAD},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["e"]}]
    })
    ck("existence on NOT_REPRODUCED -> CONTRADICTED",
       r["claims"][0]["status"] == "CONTRADICTED")

    # -- CONTRADICTED: fact bound to corroborate CONFLICTED
    r = gate_report({
        "id": "t6b", "artifacts": {"f": A_CORR_BAD},
        "claims": [{"id": "c1", "kind": "fact", "text": "x", "binds": ["f"]}]
    })
    ck("fact on CONFLICTED -> CONTRADICTED",
       r["claims"][0]["status"] == "CONTRADICTED")

    # -- severity within ceiling (high, lab, no prod) -> AUTHORIZED
    r = gate_report({
        "id": "t7", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "severity", "text": "x", "binds": ["e"],
                    "severity": "high",
                    "severity_factors": {"prod_confirmed": False}}]
    })
    ck("severity high in lab -> AUTHORIZED", r["claims"][0]["status"] == "AUTHORIZED")
    ck("reported ceiling = high", r["claims"][0].get("cap") == "high")

    # -- OVERCLAIM: critical without prod
    r = gate_report({
        "id": "t8", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "severity", "text": "x", "binds": ["e"],
                    "severity": "critical",
                    "severity_factors": {"prod_confirmed": False}}]
    })
    ck("critical without prod -> OVERCLAIM", r["claims"][0]["status"] == "OVERCLAIM")

    # -- critical WITH prod confirmed -> AUTHORIZED
    r = gate_report({
        "id": "t9", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "severity", "text": "x", "binds": ["e"],
                    "severity": "critical",
                    "severity_factors": {"prod_confirmed": True}}]
    })
    ck("critical with prod -> AUTHORIZED", r["claims"][0]["status"] == "AUTHORIZED")

    # -- control_weak tightens ceiling to medium: high becomes OVERCLAIM
    r = gate_report({
        "id": "t10", "artifacts": {"e": A_REPRO_WEAK},
        "claims": [{"id": "c1", "kind": "severity", "text": "x", "binds": ["e"],
                    "severity": "high",
                    "severity_factors": {"prod_confirmed": False}}]
    })
    ck("high with control_weak -> OVERCLAIM", r["claims"][0]["status"] == "OVERCLAIM")
    ck("ceiling with control_weak = medium", r["claims"][0].get("cap") == "medium")

    # -- public audience tightens to medium: high without prod becomes OVERCLAIM
    r = gate_report({
        "id": "t11", "audience": "public", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "severity", "text": "x", "binds": ["e"],
                    "severity": "high",
                    "severity_factors": {"prod_confirmed": False}}]
    })
    ck("public+high without prod -> OVERCLAIM", r["claims"][0]["status"] == "OVERCLAIM")

    # -- UNJUSTIFIED: severity without severity_factors
    r = gate_report({
        "id": "t12", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "severity", "text": "x", "binds": ["e"],
                    "severity": "high"}]
    })
    ck("severity without factors -> UNJUSTIFIED", r["claims"][0]["status"] == "UNJUSTIFIED")

    # -- UNJUSTIFIED: severity without a severity field
    r = gate_report({
        "id": "t12b", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "severity", "text": "x", "binds": ["e"],
                    "severity_factors": {"prod_confirmed": True}}]
    })
    ck("severity without severity field -> UNJUSTIFIED",
       r["claims"][0]["status"] == "UNJUSTIFIED")

    # -- TAGGED: inference and recommendation go out labeled, without binding
    r = gate_report({
        "id": "t13", "artifacts": {},
        "claims": [
            {"id": "c1", "kind": "inference", "text": "suggests shared auth"},
            {"id": "c2", "kind": "recommendation", "text": "rotate the keys"},
            {"id": "c3", "kind": "opinion", "text": "the gateway looks like the weak link"}
        ]
    })
    ck("inference/opinion/recommendation -> CLEARED", r["verdict"] == "CLEARED")
    ck("all TAGGED", all(c["status"] == "TAGGED" for c in r["claims"]))

    # -- STALE: old artifact with max_age_days
    old = {"kind": "repro", "verdict": "REPRODUCED",
           "produced_at": (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()}
    r = gate_report({
        "id": "t14", "artifacts": {"e": old},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["e"],
                    "max_age_days": 30}]
    })
    ck("90d-old artifact with max_age 30 -> STALE", r["claims"][0]["status"] == "STALE")

    # -- freshness OK: recent artifact passes
    fresh = {"kind": "repro", "verdict": "REPRODUCED",
             "produced_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()}
    r = gate_report({
        "id": "t15", "artifacts": {"e": fresh},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["e"],
                    "max_age_days": 30}]
    })
    ck("5d-old artifact with max_age 30 -> AUTHORIZED",
       r["claims"][0]["status"] == "AUTHORIZED")

    # -- STALE due to missing timestamp when max_age is required
    r = gate_report({
        "id": "t15b", "artifacts": {"e": A_REPRO_OK},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["e"],
                    "max_age_days": 30}]
    })
    ck("no timestamp + max_age -> STALE", r["claims"][0]["status"] == "STALE")

    # -- multiple bindings: the weakest one decides (one REPRODUCED, one NOT) -> CONTRADICTED
    r = gate_report({
        "id": "t16", "artifacts": {"a": A_REPRO_OK, "b": A_REPRO_BAD},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["a", "b"]}]
    })
    ck("multi-bind, one NOT_REPRODUCED -> CONTRADICTED",
       r["claims"][0]["status"] == "CONTRADICTED")

    # -- mixed report: one good, one bad -> BLOCKED and lists only the bad one
    r = gate_report({
        "id": "t17", "artifacts": {"e": A_REPRO_OK},
        "claims": [
            {"id": "ok", "kind": "existence", "text": "x", "binds": ["e"]},
            {"id": "bad", "kind": "fact", "text": "y"}
        ]
    })
    ck("mixed report -> BLOCKED", r["verdict"] == "BLOCKED")
    ck("blockers list only the bad one",
       len(r.get("blockers", [])) == 1 and r["blockers"][0]["id"] == "bad")

    # -- unknown kind -> UNGROUNDED, does not crash
    r = gate_report({
        "id": "t18", "artifacts": {},
        "claims": [{"id": "c1", "kind": "vibe", "text": "x"}]
    })
    ck("unknown kind -> UNGROUNDED", r["claims"][0]["status"] == "UNGROUNDED")

    # -- artifact resolution via a real repro file on disk
    tmpf = "repro_st_claim_tmp.json"
    with open(tmpf, "w") as fh:
        json.dump({"id": "x", "verdict": "REPRODUCED", "run_at": _now()}, fh)
    r = gate_report({
        "id": "t19", "artifacts": {"e": {"kind": "repro", "file": tmpf}},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["e"]}]
    })
    ck("artifact read from file -> AUTHORIZED",
       r["claims"][0]["status"] == "AUTHORIZED")
    os.remove(tmpf)

    # -- nonexistent file -> ARTIFACT_MISSING
    r = gate_report({
        "id": "t20", "artifacts": {"e": {"kind": "repro", "file": "nope_9x7.json"}},
        "claims": [{"id": "c1", "kind": "existence", "text": "x", "binds": ["e"]}]
    })
    ck("nonexistent file -> ARTIFACT_MISSING",
       r["claims"][0]["status"] == "ARTIFACT_MISSING")

    # -- template passes through the gate without crashing (with missing files it
    #    becomes BLOCKED, but does not explode)
    r = gate_report(TEMPLATE)
    ck("template does not crash", r["verdict"] in ("CLEARED", "BLOCKED"))

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    for name, ok in checks:
        print(f"  [{'ok' if ok else 'XX'}] {name}")
    print(f"\nselftest: {passed}/{total}")
    return passed == total


# ===========================================================================
# cli
# ===========================================================================

def _load(path):
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="claim-provenance / egress gate")
    sub = ap.add_subparsers(dest="cmd")

    p_gate = sub.add_parser("gate", help="gate a report; emits verdict + per-claim status")
    p_gate.add_argument("report")
    p_gate.add_argument("--write", action="store_true", help="write claimgate_<id>.json")

    sub.add_parser("template", help="print a sample report")
    ap.add_argument("--selftest", action="store_true")

    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if _selftest() else 1)

    if args.cmd == "template":
        print(json.dumps(TEMPLATE, indent=2, ensure_ascii=False))
        return

    if args.cmd == "gate":
        report = _load(args.report)
        result = gate_report(report)
        if args.write:
            out = f"claimgate_{report.get('id', 'report')}.json"
            with open(out, "w") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        # exit code encodes the verdict: 0 = CLEARED (may ship), 1 = BLOCKED
        sys.exit(0 if result["verdict"] == "CLEARED" else 1)

    ap.print_help()


if __name__ == "__main__":
    main()
