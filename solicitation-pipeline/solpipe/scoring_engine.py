"""Deterministic go/no-go scoring engine for the Solicitations module.

No AI, no network. Pure function: identical inputs -> identical outputs.
Spec: Claude Doc "Solicitations - Multi-Org Scorecard Extension Spec" (Sep 25, 2026).

Two rule sets:
  Workbook-v1  JusticePoint's calibrated workbook (Go >= 75, Conditional 55-74.9,
               K1-K8 gates: any Fail = No-Go, any Unclear caps at Conditional Go).
  Handoff-v1   Ordered rules from the Sep 24 handoff (demo defaults 80 / 65 / 90%).
"""
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

ENGINE_VERSION = "engine-2026-09-25.1"

RATED, UNKNOWN, NA = "Rated", "Unknown", "Not applicable"
PASS, FAIL, UNCLEAR, GATE_NA = "Pass", "Fail", "Unclear", "N/A"

GO, COND, NOGO, REVIEW = "Go", "Conditional Go", "No-Go", "Needs review"
TRACK = "No go but track"


class ScorecardError(ValueError):
    pass


def _d(x):
    return Decimal(str(x))


def answer_status(c):
    """Blank status with a score present = Rated (the 51 Charlotte rows predate the field)."""
    s = c.get("status")
    if s in (RATED, UNKNOWN, NA):
        if s == RATED and c.get("rating") is None:
            return UNKNOWN
        return s
    return RATED if c.get("rating") is not None else UNKNOWN


def validate_scorecard(sc):
    errs = []
    total = sum(_d(c["weight"]) for c in sc["criteria"])
    if total != 100:
        errs.append("criterion weights total %s, not 100" % total)
    if any(_d(c["weight"]) <= 0 for c in sc["criteria"]):
        errs.append("every criterion weight must be positive")
    go, cond = sc.get("go_threshold"), sc.get("cond_threshold")
    if go is None or cond is None or not (0 <= _d(cond) <= _d(go) <= 100):
        errs.append("thresholds must satisfy 0 <= conditional <= go <= 100")
    if sc.get("rule_set") not in ("Workbook-v1", "Handoff-v1"):
        errs.append("unknown rule set %r" % sc.get("rule_set"))
    nos = [c["no"] for c in sc["criteria"]]
    if len(nos) != len(set(nos)):
        errs.append("duplicate criterion numbers")
    return errs


def calculate_fit(scorecard, assessments):
    """assessments: {criterion_no: {"status", "rating"}}. Returns bounds, coverage, contributions."""
    by_no = {c["no"]: c for c in scorecard["criteria"]}
    applicable, excluded = [], []
    for no, c in by_no.items():
        a = assessments.get(no, {})
        st = answer_status(a)
        if st == NA:
            excluded.append(no)
        else:
            r = a.get("rating")
            if st == RATED and not (0 <= int(r) <= 5):
                raise ScorecardError("criterion %s rating %s outside 0-5" % (no, r))
            applicable.append((no, st, r, _d(c["weight"])))
    denom = sum(w for _, _, _, w in applicable)
    if not applicable or denom == 0:
        return {"numeric": False, "excluded": sorted(excluded)}
    low = high_extra = known = Decimal(0)
    contrib, unknown = [], []
    for no, st, r, w in applicable:
        nw = w / denom
        if st == RATED:
            pts = 100 * nw * _d(r) / 5
            low += pts
            known += nw
            contrib.append({"no": no, "rating": int(r), "norm_weight": float(nw), "points": float(pts)})
        else:
            high_extra += 100 * nw
            unknown.append(no)
    critical_unknown = [n for n in unknown if by_no[n].get("critical")]
    return {
        "numeric": True,
        "low": low, "high": low + high_extra, "coverage": 100 * known,
        "contributions": contrib, "unknown": sorted(unknown),
        "critical_unknown": sorted(critical_unknown), "excluded": sorted(excluded),
    }


def check_eligibility(scorecard, gates, requirements):
    """gates: {"K1": "Pass", ...}; requirements: list of mandatory-requirement dicts
    {"id", "position": Meets/Partial/Unverified/Does not meet/Not applicable/Conditional,
     "curable": bool, "cure_owner", "cure_deadline"}."""
    enabled = scorecard.get("gates_enabled") or ["K%d" % i for i in range(1, 9)]
    fails = [g for g in enabled if gates.get(g) == FAIL]
    unclear = [g for g in enabled if gates.get(g) in (UNCLEAR, None, "")]
    req_fail = [r["id"] for r in requirements if r.get("position") == "Does not meet" and not r.get("curable")]
    req_unknown = [r["id"] for r in requirements if r.get("position") in ("Unverified", None, "")]
    conditions = [r for r in requirements if r.get("position") in ("Conditional", "Partial")
                  or (r.get("position") == "Does not meet" and r.get("curable"))]
    open_cond = [r["id"] for r in conditions]
    unplanned = [r["id"] for r in conditions if not (r.get("cure_owner") and r.get("cure_deadline"))]
    return {"gate_fail": fails, "gate_unclear": unclear, "req_fail": req_fail,
            "req_unknown": req_unknown, "open_conditions": open_cond, "unplanned_conditions": unplanned}


def recommend(scorecard, fit, elig, stale=False):
    rs = scorecard["rule_set"]
    go, cond = _d(scorecard["go_threshold"]), _d(scorecard["cond_threshold"])
    if validate_scorecard(scorecard):
        return REVIEW, 1
    if not fit["numeric"]:
        return REVIEW, 1
    if rs == "Handoff-v1" and scorecard.get("approval_state") != "Approved" and not scorecard.get("demo"):
        return REVIEW, 1
    L, H = fit["low"], fit["high"]
    if rs == "Workbook-v1":
        if elig["gate_fail"] or elig["req_fail"]:
            return NOGO, 2
        if fit["unknown"]:
            band = lambda s: GO if s >= go else COND if s >= cond else NOGO
            if band(L) != band(H):
                return REVIEW, 3
        if L >= go:
            return (COND, 5) if elig["gate_unclear"] else (GO, 4)
        if L >= cond:
            return COND, 6
        return NOGO, 7
    # Handoff-v1
    if elig["gate_fail"] or elig["req_fail"]:
        return NOGO, 2
    if elig["gate_unclear"] or elig["req_unknown"] or fit["critical_unknown"] or stale:
        return REVIEW, 3
    min_cov = scorecard.get("min_coverage")
    if min_cov is not None and fit["coverage"] < _d(min_cov):
        return REVIEW, 4
    if L >= go and not elig["open_conditions"]:
        return GO, 5
    if L >= cond:
        return (COND, 6) if not elig["unplanned_conditions"] else (REVIEW, 6)
    if H >= cond and fit["unknown"]:
        return REVIEW, 7
    return NOGO, 8


def _r1(x):
    return float(_d(x).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def explain(org, scenario, scorecard, fit, elig, rec, rule, evaluated_at):
    if not fit["numeric"]:
        fit_txt = "no applicable criteria, so no numeric score"
    elif fit["unknown"]:
        fit_txt = "provisional fit range %.1f-%.1f; coverage %.1f%%" % (_r1(fit["low"]), _r1(fit["high"]), _r1(fit["coverage"]))
    else:
        fit_txt = "fit %.1f; coverage 100%%" % _r1(fit["low"])
    parts = ["%s for %s under %s. %s." % (rec, org, scenario, fit_txt[0].upper() + fit_txt[1:])]
    if elig["gate_fail"]:
        parts.append("Failed gates: %s." % ", ".join(elig["gate_fail"]))
    if elig["req_fail"]:
        parts.append("Unmet mandatory requirements: %d." % len(elig["req_fail"]))
    if elig["gate_unclear"]:
        parts.append("Unclear gates: %s." % ", ".join(elig["gate_unclear"]))
    if fit.get("unknown"):
        parts.append("Unrated criteria: %s." % ", ".join(str(n) for n in fit["unknown"]))
    if fit.get("excluded"):
        parts.append("Excluded as not applicable: %s." % ", ".join(str(n) for n in fit["excluded"]))
    if fit["numeric"] and fit["contributions"]:
        top = sorted(fit["contributions"], key=lambda c: (-c["rating"], c["no"]))
        strong = [str(c["no"]) for c in top if c["rating"] >= 4][:3]
        weak = [str(c["no"]) for c in sorted(fit["contributions"], key=lambda c: (c["rating"], c["no"])) if c["rating"] <= 2][:3]
        if strong:
            parts.append("Main strengths: criteria %s." % ", ".join(strong))
        if weak:
            parts.append("Main constraints: criteria %s." % ", ".join(weak))
    parts.append("Rule %s of %s. Scorecard %s; %s; evaluated %s." % (
        rule, scorecard["rule_set"], scorecard["version"], ENGINE_VERSION, evaluated_at))
    return " ".join(parts)


def run_key(scenario_id, scorecard, inputs):
    blob = json.dumps({"s": scenario_id, "v": scorecard["version"], "e": ENGINE_VERSION, "i": inputs},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def evaluate(scenario, scorecard, evaluated_at, stale=False):
    """scenario: {"id","name","org","gates":{..},"assessments":{no:{status,rating}},"requirements":[..]}.
    Returns a complete run dict or raises; never returns a partial result."""
    errs = validate_scorecard(scorecard)
    fit = calculate_fit(scorecard, scenario.get("assessments", {})) if not errs else {"numeric": False, "excluded": []}
    elig = check_eligibility(scorecard, scenario.get("gates", {}), scenario.get("requirements", []))
    rec, rule = (REVIEW, 1) if errs else recommend(scorecard, fit, elig, stale)
    inputs = {"gates": scenario.get("gates", {}), "assessments": scenario.get("assessments", {}),
              "requirements": scenario.get("requirements", []), "stale": stale}
    out = {
        "run_key": run_key(scenario["id"], scorecard, inputs),
        "engine_version": ENGINE_VERSION,
        "scorecard_version": scorecard["version"],
        "rule_set": scorecard["rule_set"],
        "scorecard_errors": errs,
        "recommendation": rec, "rule_fired": rule,
        "eligibility": elig,
        "score_low": _r1(fit["low"]) if fit["numeric"] else None,
        "score_high": _r1(fit["high"]) if fit["numeric"] else None,
        "coverage_pct": _r1(fit["coverage"]) if fit["numeric"] else None,
        "contributions": fit.get("contributions", []),
        "unknown": fit.get("unknown", []), "excluded": fit.get("excluded", []),
        "explanation": explain(scenario.get("org", ""), scenario.get("name", ""), scorecard, fit, elig, rec, rule, evaluated_at),
        "input_snapshot": inputs,
        "evaluated_at": evaluated_at,
    }
    return out


def apply_tracking(run, rationale, owner, trigger_or_date):
    """Human step: turn No-Go into No go but track. Never changes score or eligibility."""
    if run["recommendation"] != NOGO:
        raise ValueError("only a No-Go can become No go but track")
    if not (rationale and owner and trigger_or_date):
        raise ValueError("No go but track needs rationale, owner and a trigger or date")
    return {"disposition": TRACK, "rationale": rationale, "owner": owner, "trigger": trigger_or_date,
            "run_key": run["run_key"]}
