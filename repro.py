#!/usr/bin/env python3
"""
repro.py — reproduction verifier for vulnerability claims.

One of a small set of composable gates that turn "verify before you claim"
into something executable, instead of relying on discipline on a good day:
hallucination has to be near-zero by ARCHITECTURE, not by habit. A companion
gate in this repository (`grounding.py`) checks numeric/id claims; this one
checks vulnerability-reproduction claims specifically. It defends against the
claim "this is a vuln" made without proof that runs.

THE DOCTRINE (one falsifiable sentence):
  A vuln does not exist because a scanner said so, or because the output
  "looks bad". It exists when a PoC RUNS and produces the observable you
  PREDICTED BEFORE running it, and that observable does NOT appear against a
  known-clean target. Without that, the finding is a hypothesis, and a
  hypothesis does not get reported.

THE TWO ANTI-HALLUCINATION GATES (the core of this tool):

  1. PRE-COMMIT OF THE PREDICTION. You register the expected signal
     (`register`), which writes a lock containing the sha256 of the
     prediction block plus a timestamp. Only then can you run (`run`). If the
     prediction in the case file changes after the lock, the tool REFUSES:
     you edited the prediction to match output you had already seen. This
     kills the most dangerous researcher error, the post-hoc one: run, look,
     then invent a prediction that fits. Running without a lock is refused
     too.

  2. NEGATIVE CONTROL. The same PoC also runs against a known-clean target
     (`control`). If the vuln signal shows up on BOTH, the signal is
     nonspecific and the finding is discarded (SIGNAL_NONSPECIFIC), even
     though the PoC "worked". This is what separates reproduction from "the
     scanner lights up for everything".

VERDICTS (the only axis this tool decides):
  REPRODUCED          -> every predicate matched AND the control stayed quiet.
                         This is the only verdict that authorizes reporting.
  NOT_REPRODUCED      -> the predicates did not match. The PoC ran, the vuln
                         did not show up.
  SIGNAL_NONSPECIFIC  -> matched on both the target and the control. False
                         positive, do not report.
  INCONCLUSIVE        -> the PoC did not execute (timeout, missing binary,
                         exec error). No trace, no verdict on the merits.
  UNSUBSTANTIATED     -> there is no PoC. Not "not yet verified", it is
                         "cannot be claimed". The difference is reputational.

HONEST CEILING (this tool does not claim past this):
  1. This tool proves REPRODUCTION, not real-world IMPACT or EXPLOITABILITY.
     A PoC that reproduces in a lab may not hold under the target's
     production configuration. REPRODUCED is necessary to report, not
     sufficient to set severity.
  2. The negative control is only as good as the "clean target" you picked.
     A badly chosen control (one that shares the same flaw) hides
     nonspecificity. That is why the control is explicit in the case file and
     lands in the ledger.
  3. The matcher checks signal PRESENCE (regex/substring/exit/absence), not
     SEMANTICS. A lazy predicted signal matches something that is not the
     vuln. Weak prediction = weak REPRODUCED. Pre-commit enforces discipline,
     it does not guess for you.
  4. This tool does NOT run the PoC anonymized or under a rules-of-engagement
     gate. Egress, OPSEC, and authorization scope are the caller's own
     responsibility; the PoC here is whatever you wrote, over whatever
     network you chose to run it on. This tool only judges the result.

Standard library only. `--selftest` exercises every verdict and every gate
offline, using local commands (echo/false/sleep), no network access.

Usage:
  python3 repro.py register <case.json>     # lock the prediction (pre-commit)
  python3 repro.py run      <case.json>     # run; requires a lock; emits a verdict
  python3 repro.py case     <case.json>     # register+run+control, writes repro_<id>.json
  python3 repro.py template                 # print an example case
  python3 repro.py --selftest               # offline gate
"""
import sys
import os
import json
import hashlib
import argparse
import subprocess
import re
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canon(obj):
    """Canonical, stable serialization for the pre-commit hash."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _prediction_hash(prediction):
    return hashlib.sha256(_canon(prediction).encode("utf-8")).hexdigest()


def _lock_path(case_id):
    return f".repro_lock_{case_id}.json"


# ---------------------------------------------------------------------------
# PoC execution
# ---------------------------------------------------------------------------

class ExecResult:
    def __init__(self, ran, exit_code=None, stdout="", stderr="", error=None):
        self.ran = ran            # True if the process actually executed (even with exit != 0)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error = error        # reason for non-execution (timeout, missing binary, ...)

    @property
    def combined(self):
        return (self.stdout or "") + "\n" + (self.stderr or "")


def run_poc(poc):
    """Run a poc block {cmd:[...], timeout:int, stdin?:str}. Never uses a shell."""
    if not poc or not poc.get("cmd"):
        return ExecResult(ran=False, error="no-cmd")
    cmd = poc["cmd"]
    if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
        return ExecResult(ran=False, error="cmd-must-be-list-of-str")
    timeout = poc.get("timeout", 30)
    try:
        proc = subprocess.run(
            cmd,
            input=poc.get("stdin"),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ExecResult(ran=True, exit_code=proc.returncode,
                          stdout=proc.stdout, stderr=proc.stderr)
    except subprocess.TimeoutExpired:
        return ExecResult(ran=False, error=f"timeout>{timeout}s")
    except FileNotFoundError:
        return ExecResult(ran=False, error=f"binary-not-found:{cmd[0]}")
    except Exception as e:  # exec failed for another reason; no verdict on the merits, INCONCLUSIVE
        return ExecResult(ran=False, error=f"exec-error:{type(e).__name__}")


# ---------------------------------------------------------------------------
# matcher: a single predicate matches or not, against an ExecResult
# ---------------------------------------------------------------------------

def eval_predicate(pred, res):
    """Return (matched: bool, detail: str). Predicate types: regex, substring, exit, absent."""
    t = pred.get("type")
    if t == "exit":
        want = pred.get("code")
        got = res.exit_code
        return (got == want, f"exit {got} vs want {want}")
    if t == "substring":
        needle = pred.get("value", "")
        found = needle in res.combined
        return (found, f"substring {'found' if found else 'absent'}: {needle!r}")
    if t == "regex":
        pat = pred.get("pattern", "")
        flags = re.IGNORECASE if pred.get("ignorecase") else 0
        found = re.search(pat, res.combined, flags) is not None
        return (found, f"regex {'match' if found else 'no-match'}: {pat!r}")
    if t == "absent":
        # the vuln is proven by the ABSENCE of a signal (e.g. no auth redirect)
        needle = pred.get("value", "")
        absent = needle not in res.combined
        return (absent, f"expected-absent {'ok' if absent else 'PRESENT'}: {needle!r}")
    return (False, f"unknown-predicate-type:{t}")


def eval_match_block(match, res):
    """Every predicate must match (AND). Returns (all_matched, details)."""
    if not match:
        return (False, ["no-predicates"])
    details = []
    allm = True
    for p in match:
        m, d = eval_predicate(p, res)
        details.append(("PASS" if m else "FAIL") + " " + d)
        allm = allm and m
    return (allm, details)


# ---------------------------------------------------------------------------
# register (pre-commit) and lock verification
# ---------------------------------------------------------------------------

def cmd_register(case, write=True):
    cid = case["id"]
    prediction = case.get("prediction")
    if not prediction or not prediction.get("match"):
        raise ValueError("case has no prediction.match: nothing to pre-commit")
    h = _prediction_hash(prediction)
    lock = {"id": cid, "prediction_sha256": h, "locked_at": _now()}
    if write:
        with open(_lock_path(cid), "w") as f:
            json.dump(lock, f, indent=2)
    return lock


def load_lock(case_id):
    p = _lock_path(case_id)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def check_lock(case):
    """Ensure the current prediction matches the lock. Returns (ok, reason)."""
    cid = case["id"]
    lock = load_lock(cid)
    if lock is None:
        return (False, "no-lock: run `register` first; without a pre-commit the "
                       "prediction could have been retrofitted to the output")
    cur = _prediction_hash(case.get("prediction", {}))
    if cur != lock["prediction_sha256"]:
        return (False, "prediction-changed-after-lock: the prediction was edited "
                       "after the pre-commit; this is a retrofit, REFUSED")
    return (True, f"locked@{lock['locked_at']}")


# ---------------------------------------------------------------------------
# run: requires a lock, runs poc + control, decides the verdict
# ---------------------------------------------------------------------------

def decide_verdict(case, target_res, control_res):
    """
    Return a dict with verdict + trace. Runs nothing; takes ExecResult objects.
    Decision order matters: UNSUBSTANTIATED and INCONCLUSIVE come before the
    verdict on the merits.
    """
    prediction = case.get("prediction", {})
    match = prediction.get("match")

    # 1. no real PoC -> cannot be claimed
    if not case.get("poc") or not case["poc"].get("cmd"):
        return {"verdict": "UNSUBSTANTIATED",
                "why": "no executable PoC; hypothesis, not a finding"}

    # 2. PoC did not execute -> no verdict on the merits
    if not target_res.ran:
        return {"verdict": "INCONCLUSIVE",
                "why": f"PoC did not execute: {target_res.error}"}

    matched, match_details = eval_match_block(match, target_res)

    # 3. no match -> not reproduced
    if not matched:
        return {"verdict": "NOT_REPRODUCED",
                "why": "the predicted signal did not appear",
                "match_details": match_details}

    # 4. matched on the target. Check the negative control, if any
    control = prediction.get("control")
    if control and control.get("cmd"):
        if not control_res or not control_res.ran:
            return {"verdict": "INCONCLUSIVE",
                    "why": f"negative control did not execute: "
                           f"{control_res.error if control_res else 'no-run'}; "
                           f"no control means no REPRODUCED verdict",
                    "match_details": match_details}
        # the control uses the SAME match predicates: if they match on the
        # clean target, the signal is nonspecific
        ctrl_matched, ctrl_details = eval_match_block(match, control_res)
        if ctrl_matched:
            return {"verdict": "SIGNAL_NONSPECIFIC",
                    "why": "the signal also appeared on the clean target; false positive",
                    "match_details": match_details,
                    "control_details": ctrl_details}
        return {"verdict": "REPRODUCED",
                "why": "predicted signal present on the target, absent on the control",
                "match_details": match_details,
                "control_details": ctrl_details}

    # 5. matched and there is no control. REPRODUCED, but flagged: a missing
    # control weakens the finding
    return {"verdict": "REPRODUCED",
            "why": "predicted signal present; NO negative control (weaker finding)",
            "match_details": match_details,
            "control_weak": True}


def cmd_run(case, write_report=False):
    ok, reason = check_lock(case)
    if not ok:
        return {"id": case["id"], "verdict": "REFUSED", "why": reason}

    target_res = run_poc(case.get("poc"))
    control = case.get("prediction", {}).get("control")
    control_res = run_poc(control) if control and control.get("cmd") else None

    verdict = decide_verdict(case, target_res, control_res)
    report = {
        "id": case["id"],
        "target": case.get("target"),
        "vuln_class": case.get("vuln_class"),
        "claim": case.get("claim"),
        "lock": reason,
        "run_at": _now(),
        "poc_cmd": (case.get("poc") or {}).get("cmd"),
        "poc_exit": target_res.exit_code,
        "poc_ran": target_res.ran,
        "poc_error": target_res.error,
        **verdict,
    }
    if write_report:
        with open(f"repro_{case['id']}.json", "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    return report


def cmd_case(case):
    """Full flow: register (pre-commit now), then run. Writes the report."""
    cmd_register(case, write=True)
    return cmd_run(case, write_report=True)


# ---------------------------------------------------------------------------
# template
# ---------------------------------------------------------------------------

TEMPLATE = {
    "id": "altside-bola-001",
    "target": "https://api.altside.example/v1/accounts/{id}",
    "vuln_class": "BOLA",
    "claim": "GET /accounts/{other_id} returns another user's data using my token",
    "poc": {
        "cmd": ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "-H", "Authorization: Bearer $MY_TOKEN",
                "https://api.altside.example/v1/accounts/OTHER_ID"],
        "timeout": 20
    },
    "prediction": {
        "match": [
            {"type": "regex", "pattern": "^200$"}
        ],
        "control": {
            "cmd": ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                    "https://api.altside.example/v1/accounts/OTHER_ID"],
            "timeout": 20
        }
    }
}


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def _selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # clean up locks left over from previous test cases
    for cid in ["st_repro", "st_notrepro", "st_nonspec", "st_incon",
                "st_unsub", "st_retrofit", "st_nolock", "st_absent",
                "st_substr", "st_exit"]:
        p = _lock_path(cid)
        if os.path.exists(p):
            os.remove(p)

    # -- canonical hash is stable (key order does not change the hash)
    a = {"match": [{"type": "regex", "pattern": "x"}], "control": {"cmd": ["true"]}}
    b = {"control": {"cmd": ["true"]}, "match": [{"pattern": "x", "type": "regex"}]}
    ck("canon-hash-stable-under-key-order", _prediction_hash(a) == _prediction_hash(b))

    # -- REPRODUCED: echo emits VULN, the control call stays quiet
    case_r = {
        "id": "st_repro", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {"cmd": ["echo", "VULN-CONFIRMED-42"], "timeout": 5},
        "prediction": {
            "match": [{"type": "substring", "value": "VULN-CONFIRMED-42"}],
            "control": {"cmd": ["echo", "clean-response"], "timeout": 5}
        }
    }
    r = cmd_case(case_r)
    ck("REPRODUCED verdict", r["verdict"] == "REPRODUCED")
    ck("REPRODUCED wrote report", os.path.exists("repro_st_repro.json"))
    ck("REPRODUCED not flagged control_weak", not r.get("control_weak"))

    # -- NOT_REPRODUCED: predicted signal absent
    case_nr = {
        "id": "st_notrepro", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {"cmd": ["echo", "all-good-here"], "timeout": 5},
        "prediction": {"match": [{"type": "substring", "value": "VULN"}]}
    }
    r = cmd_case(case_nr)
    ck("NOT_REPRODUCED verdict", r["verdict"] == "NOT_REPRODUCED")

    # -- SIGNAL_NONSPECIFIC: signal appears on the target AND the control
    case_ns = {
        "id": "st_nonspec", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {"cmd": ["echo", "error-500-generic"], "timeout": 5},
        "prediction": {
            "match": [{"type": "substring", "value": "error-500"}],
            "control": {"cmd": ["echo", "error-500-generic"], "timeout": 5}
        }
    }
    r = cmd_case(case_ns)
    ck("SIGNAL_NONSPECIFIC verdict", r["verdict"] == "SIGNAL_NONSPECIFIC")

    # -- INCONCLUSIVE: missing binary
    case_ic = {
        "id": "st_incon", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {"cmd": ["this-binary-does-not-exist-9x7", "arg"], "timeout": 5},
        "prediction": {"match": [{"type": "exit", "code": 0}]}
    }
    r = cmd_case(case_ic)
    ck("INCONCLUSIVE on missing binary", r["verdict"] == "INCONCLUSIVE")

    # -- INCONCLUSIVE via timeout
    case_to = {
        "id": "st_incon", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {"cmd": ["sleep", "5"], "timeout": 1},
        "prediction": {"match": [{"type": "exit", "code": 0}]}
    }
    # re-lock because the prediction changed (exit 0 is the same, but the poc
    # changed; the lock covers prediction)
    r = cmd_case(case_to)
    ck("INCONCLUSIVE on timeout", r["verdict"] == "INCONCLUSIVE"
       and "timeout" in (r.get("poc_error") or ""))

    # -- UNSUBSTANTIATED: no poc
    case_us = {
        "id": "st_unsub", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {},
        "prediction": {"match": [{"type": "exit", "code": 0}]}
    }
    # register requires prediction.match, fine; run should yield UNSUBSTANTIATED
    cmd_register(case_us, write=True)
    r = cmd_run(case_us)
    ck("UNSUBSTANTIATED without poc", r["verdict"] == "UNSUBSTANTIATED")

    # -- REFUSED: run without a lock
    case_nl = {
        "id": "st_nolock", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {"cmd": ["echo", "x"], "timeout": 5},
        "prediction": {"match": [{"type": "substring", "value": "x"}]}
    }
    # do not register
    r = cmd_run(case_nl)
    ck("REFUSED without lock", r["verdict"] == "REFUSED" and "no-lock" in r["why"])

    # -- REFUSED: retrofit (prediction changes after the lock)
    case_rf = {
        "id": "st_retrofit", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {"cmd": ["echo", "actual-output-777"], "timeout": 5},
        "prediction": {"match": [{"type": "substring", "value": "prediction-A"}]}
    }
    cmd_register(case_rf, write=True)  # lock prediction A
    # researcher sees the "777" output and edits the prediction to match it:
    case_rf["prediction"]["match"] = [{"type": "substring", "value": "777"}]
    r = cmd_run(case_rf)
    ck("REFUSED on retrofit", r["verdict"] == "REFUSED"
       and "prediction-changed" in r["why"])

    # -- matcher: absent (vuln proven by the absence of a signal)
    case_ab = {
        "id": "st_absent", "target": "t", "vuln_class": "missing-auth-redirect",
        "claim": "sensitive endpoint does not redirect to login",
        "poc": {"cmd": ["echo", "200 OK body-with-secret"], "timeout": 5},
        "prediction": {
            "match": [{"type": "absent", "value": "Location: /login"}],
            "control": {"cmd": ["echo", "302 Location: /login"], "timeout": 5}
        }
    }
    r = cmd_case(case_ab)
    # target: no redirect (absent ok=match). control: DOES contain the text ->
    # absent FAILS on the control, so the signal (absence) does NOT reproduce
    # on the control -> REPRODUCED
    ck("absent matcher -> REPRODUCED", r["verdict"] == "REPRODUCED")

    # -- matcher: exit code
    case_ex = {
        "id": "st_exit", "target": "t", "vuln_class": "test", "claim": "c",
        "poc": {"cmd": ["sh", "-c", "exit 3"], "timeout": 5},
        "prediction": {"match": [{"type": "exit", "code": 3}]}
    }
    r = cmd_case(case_ex)
    ck("exit matcher -> REPRODUCED (control_weak)", r["verdict"] == "REPRODUCED"
       and r.get("control_weak") is True)

    # -- matcher: negative substring inside an AND block (one passes, one
    # fails -> NOT)
    res_fake = ExecResult(ran=True, exit_code=0, stdout="foo", stderr="")
    allm, _ = eval_match_block(
        [{"type": "substring", "value": "foo"},
         {"type": "substring", "value": "bar"}], res_fake)
    ck("AND block fails if any predicate fails", allm is False)

    allm2, _ = eval_match_block(
        [{"type": "substring", "value": "foo"},
         {"type": "exit", "code": 0}], res_fake)
    ck("AND block passes if all predicates pass", allm2 is True)

    # -- regex ignorecase
    res_case = ExecResult(ran=True, exit_code=0, stdout="INTERNAL SERVER ERROR", stderr="")
    m, _ = eval_predicate({"type": "regex", "pattern": "internal server error",
                           "ignorecase": True}, res_case)
    ck("regex ignorecase", m is True)

    # -- unknown predicate type does not raise, returns False
    m2, _ = eval_predicate({"type": "nonsense"}, res_case)
    ck("unknown predicate -> no match, no crash", m2 is False)

    # -- the lock file was actually written with a hash
    lk = load_lock("st_repro")
    ck("lock file has sha256", lk is not None and len(lk.get("prediction_sha256", "")) == 64)

    # clean up artifacts
    for cid in ["st_repro", "st_notrepro", "st_nonspec", "st_incon",
                "st_unsub", "st_retrofit", "st_nolock", "st_absent", "st_exit"]:
        for p in [_lock_path(cid), f"repro_{cid}.json"]:
            if os.path.exists(p):
                os.remove(p)

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    for name, ok in checks:
        print(f"  [{'ok' if ok else 'XX'}] {name}")
    print(f"\nselftest: {passed}/{total}")
    return passed == total


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def _load_case(path):
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="reproduction verifier for vuln claims")
    sub = ap.add_subparsers(dest="cmd")

    p_reg = sub.add_parser("register", help="pre-commit the prediction (lock)")
    p_reg.add_argument("case")

    p_run = sub.add_parser("run", help="run the PoC; requires a lock; emits a verdict")
    p_run.add_argument("case")

    p_case = sub.add_parser("case", help="register+run+control; writes repro_<id>.json")
    p_case.add_argument("case")

    sub.add_parser("template", help="print an example case")
    ap.add_argument("--selftest", action="store_true")

    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if _selftest() else 1)

    if args.cmd == "template":
        print(json.dumps(TEMPLATE, indent=2, ensure_ascii=False))
        return

    if args.cmd == "register":
        case = _load_case(args.case)
        lock = cmd_register(case, write=True)
        print(json.dumps(lock, indent=2, ensure_ascii=False))
        return

    if args.cmd == "run":
        case = _load_case(args.case)
        print(json.dumps(cmd_run(case, write_report=True), indent=2, ensure_ascii=False))
        return

    if args.cmd == "case":
        case = _load_case(args.case)
        print(json.dumps(cmd_case(case), indent=2, ensure_ascii=False))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
