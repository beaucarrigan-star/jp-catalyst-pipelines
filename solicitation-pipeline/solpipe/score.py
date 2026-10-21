"""Re-score: deterministic scoring engine (scoring_engine.py), no AI.

Replaces the Sep 16 port of solicitation_score_scenario. What changed:
  * a blank criterion is Unknown, not a refusal: the score becomes a range (low = unknowns at 0,
    high = unknowns at 5) with a coverage %, and a range that straddles a band is Needs review
  * Answer_Status "Not applicable" drops a criterion and renormalises the other weights
  * the scorecard follows Lead_Organization (scorecards.py): JusticePoint keeps the calibrated
    workbook (Go 75 / Conditional 55, K1-K8); Sirona and TDP are Draft until approved
  * mandatory requirements of the parent Solicitation count: "Does not meet" with no cure plan is
    No-Go (Cure_Owner or Cure_Deadline set = curable); requirements whose Subject_Entity names a
    different org are ignored
  * writes Score_Range, Coverage_Pct and Rule_Fired, and a run key in the reason, so the same
    inputs always give the same, traceable answer

Workbook-v1 rules (JusticePoint): any gate Fail or uncurable unmet mandatory -> No-Go; range across
bands -> Needs review; >= Go with no Unclear gate -> Go; >= Go with Unclear -> Conditional Go;
>= Conditional -> Conditional Go; else No-Go. Blank gates count as Unclear.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import scorecards
from . import scoring_engine as eng
from .crm import Crm, CrmError

GATES = [
    ("K1", "K1_Eligibility"), ("K2", "K2_Legal_Terms"), ("K3", "K3_Budget_Floor"),
    ("K4", "K4_Timeline"), ("K5", "K5_Conflicts"), ("K6", "K6_Mission_Values"),
    ("K7", "K7_Staffing_Floor"), ("K8", "K8_Financial_Ceiling"),
]
CATEGORY_FIELD = {"A": "Cat_A_Points", "B": "Cat_B_Points", "C": "Cat_C_Points",
                  "D": "Cat_D_Points", "E": "Cat_E_Points", "F": "Cat_F_Points"}
FALLBACK_REC = "Not yet scored"  # written when the picklist lacks the engine's value (e.g. Needs review)


class Refused(Exception):
    """Scoring refused on purpose. The message is for the user."""


@dataclass
class Result:
    weighted: float | None
    score_high: float | None
    coverage: float | None
    categories: dict[str, float]
    fails: list[str]
    unclears: list[str]
    recommendation: str
    rule: str
    reason: str
    version: str
    thresholds: str = ""
    run: dict = field(default_factory=dict)

    @property
    def score_range(self) -> str:
        if self.weighted is None:
            return "No numeric score"
        if self.score_high is None or self.score_high == self.weighted:
            return f"{self.weighted:.1f}"
        return f"{self.weighted:.1f}-{self.score_high:.1f}"


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:.1f}"


def _gate(v):
    return None if v in (None, "", "-None-") else v


def _status(v):
    return None if v in (None, "", "-None-") else v


def requirement_inputs(reqs: list[dict], org: str) -> list[dict]:
    """Mandatory Solicitation_Requirements -> engine requirement dicts."""
    out = []
    for r in reqs:
        subj = _status(r.get("Subject_Entity"))
        if subj in scorecards.ORG_KEYS and subj != org:
            continue
        owner, due = r.get("Cure_Owner"), r.get("Cure_Deadline")
        out.append({"id": str(r.get("id")), "position": _status(r.get("Our_Position")),
                    "curable": bool(owner or due), "cure_owner": bool(owner), "cure_deadline": due or None})
    return out


def compute(scores: list[dict], gates: dict, scorecard: dict, scenario_id: str = "", name: str = "",
            requirements: list[dict] | None = None, matrix_line: str = "", evaluated_at: str = "") -> Result:
    """scores: Scenario_Scores rows {"Criterion_No", "Criterion_Score", "Category", "Answer_Status"}.
    gates: {"K1": "Pass"|"Fail"|"Unclear"|"N/A"|None, ...}. Pure - no CRM."""
    nos = {c["no"] for c in scorecard["criteria"]}
    by_no = {int(s["Criterion_No"]): s for s in scores if s.get("Criterion_No") is not None}
    missing = sorted(nos - set(by_no))
    if missing:
        raise Refused("No scorecard row for criterion " + ", ".join(map(str, missing))
                      + ". Press Seed Scorecard first.")
    assessments = {}
    for n in sorted(nos):
        s = by_no[n]
        v = s.get("Criterion_Score")
        assessments[n] = {"status": _status(s.get("Answer_Status")),
                          "rating": None if v in (None, "") else int(float(v))}
    g = {k: _gate(v) for k, v in gates.items()}
    try:
        run = eng.evaluate({"id": str(scenario_id), "name": name, "org": scorecard.get("org", ""),
                            "gates": g, "assessments": assessments, "requirements": requirements or []},
                           scorecard, evaluated_at)
    except eng.ScorecardError as e:
        raise Refused(str(e).capitalize() + ". Ratings must be 0-5.")
    cats = {k: 0.0 for k in CATEGORY_FIELD}
    for c in run["contributions"]:
        letter = (by_no[c["no"]].get("Category") or "?")[0]
        if letter in cats:
            cats[letter] += c["points"]
    cats = {k: round(v + 1e-9, 2) for k, v in cats.items()}
    reason = run["explanation"]
    if run["scorecard_errors"]:
        reason += " Scorecard problems: " + "; ".join(run["scorecard_errors"]) + "."
    if matrix_line:
        reason += " " + matrix_line
    reason += f" Run key {run['run_key']}."
    return Result(run["score_low"], run["score_high"], run["coverage_pct"], cats,
                  run["eligibility"]["gate_fail"], [k for k in run["eligibility"]["gate_unclear"]],
                  run["recommendation"], f"{run['rule_set']} rule {run['rule_fired']}", reason,
                  scorecard["version"],
                  thresholds=f"Go>={scorecard['go_threshold']:g} / CondGo>={scorecard['cond_threshold']:g}",
                  run=run)


def matrix_snapshot(sc: dict) -> str:
    total = int(float(sc.get("Spec_Lines_Total") or 0))
    if not total:
        return "Spec matrix at scoring: not generated."
    meets = int(float(sc.get("Spec_Meets") or 0))
    dnm = int(float(sc.get("Spec_DNM") or 0))
    core = int(float(sc.get("Unmet_Core_Count") or 0))
    pct = round(100.0 * meets / total)
    return (f"Spec matrix at scoring: {total:,} lines, {pct}% Meets, {dnm} Does-not-meet, "
            f"{core} Core lines unresolved.")


def _now_iso() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).replace(microsecond=0).isoformat()
    except Exception:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run(crm: Crm, scenario_id: str, dry_run: bool = False) -> str:
    sc = crm.get_record("Bid_Scenarios", scenario_id)
    org = _status(sc.get("Lead_Organization")) or scorecards.DEFAULT_ORG
    rows = crm.coql(
        "select Criterion_No, Criterion_Score, Category, Weight, Answer_Status from Scenario_Scores "
        f"where Bid_Scenario = {int(scenario_id)}")
    gates = {k: sc.get(f) for k, f in GATES}
    card = scorecards.load(crm, org)
    reqs: list[dict] = []
    lk = crm.lookup_field_to("Bid_Scenarios", "Solicitations")
    parent = (sc.get(lk) or {}).get("id")
    if parent:
        reqs = requirement_inputs(crm.coql(
            "select id, Our_Position, Cure_Owner, Cure_Deadline, Subject_Entity from Solicitation_Requirements "
            f"where Solicitation = {int(parent)} and Mandatory = true"), org)
    try:
        res = compute(rows, gates, card, scenario_id, sc.get("Name") or "", reqs, matrix_snapshot(sc), _now_iso())
    except Refused as e:
        return "Not scored. " + str(e)

    rec_value = res.recommendation
    try:
        f = crm.field("Bid_Scenarios", "Recommendation")
        allowed = {p.get("actual_value") or p.get("display_value") for p in (f or {}).get("pick_list_values") or []}
    except CrmError:
        allowed = set()
    note = ""
    if allowed and rec_value not in allowed:
        note = f" ('{rec_value}' is not yet a Recommendation picklist value, so the field shows {FALLBACK_REC}.)"
        rec_value = FALLBACK_REC

    msg = f"{res.recommendation} - {res.score_range} (coverage {_fmt(res.coverage)}%, {res.rule}, scorecard {res.version})"
    if dry_run:
        return "[dry run] " + msg + note + " | " + res.reason
    upd = {
        "Weighted_Score": res.weighted,
        "Score_Range": res.score_range[:30],
        "Coverage_Pct": res.coverage,
        "Rule_Fired": res.rule[:60],
        "Recommendation": rec_value,
        "Recommendation_Reason": (res.reason + note)[:32000],
        "Weight_Set_Version": res.version[:60],
        "Thresholds_Applied": res.thresholds[:60],
        "Knockout_Fails": len(res.fails),
        "Knockout_Unclears": len(res.unclears),
        "Last_Scored_On": _now_iso(),
    }
    upd.update({CATEGORY_FIELD[k]: v for k, v in res.categories.items()})
    crm.update("Bid_Scenarios", scenario_id, upd)
    old = sc.get("Recommendation")
    if old and old not in (FALLBACK_REC, "-None-") and old != rec_value and rec_value != FALLBACK_REC:
        # build pack §8.3: a changed recommendation notifies the owner, old -> new
        try:
            _change_task(crm, sc, old, res)
            msg += f" Recommendation changed from {old}; a review task went to the owner."
        except CrmError as e:
            msg += f" (change task not created: {str(e)[:200]})"
    try:
        _roll_up_parent(crm, sc)
    except CrmError as e:  # parent roll-up is a convenience; never fail the score on it
        msg += f" (parent Best_Scenario not updated: {str(e)[:200]})"
    return msg + note


def _change_task(crm: Crm, sc: dict, old: str, res: Result) -> None:
    from datetime import date, timedelta
    task = {
        "Subject": f"Recommendation changed: {old} -> {res.recommendation} ({res.score_range})"[:255],
        "Due_Date": (date.today() + timedelta(days=1)).isoformat(),
        "Status": "Not Started",
        "Priority": "High",
        "What_Id": {"id": sc["id"]},
        "$se_module": "Bid_Scenarios",
        "Description": res.reason[:32000],
    }
    if (sc.get("Owner") or {}).get("id"):
        task["Owner"] = {"id": sc["Owner"]["id"]}
    crm.create_many("Tasks", [task])


def _roll_up_parent(crm: Crm, sc: dict) -> None:
    lk = crm.lookup_field_to("Bid_Scenarios", "Solicitations")
    parent = sc.get(lk)
    if not parent:
        return
    pid = parent["id"]
    sibs = crm.coql(f"select id, Weighted_Score, Recommendation from Bid_Scenarios where {lk} = {int(pid)}")
    scored = [s for s in sibs if s.get("Weighted_Score") is not None
              and s.get("Recommendation") not in (None, "-None-", FALLBACK_REC)]
    if not scored:
        return
    rank = {"Go": 2, "Conditional Go": 1, "No-Go": 0}
    best = max(scored, key=lambda s: (rank.get(s["Recommendation"], -1), float(s["Weighted_Score"])))
    crm.update("Solicitations", pid, {"Best_Scenario": {"id": best["id"]},
                                      "Best_Score": float(best["Weighted_Score"]),
                                      "Best_Recommendation": best["Recommendation"]})
