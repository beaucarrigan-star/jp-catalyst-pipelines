"""Offline tests: no network, no CRM. Run with  python -m pytest tests  (or python tests/test_pipeline.py).

The scoring acceptance tests are the ones that matter: Re-score (now the deterministic
engine) must reproduce the calibrated workbook (Charlotte PRIME 42.6 No-Go, PRIME+ 52.4
No-Go, SUB 61.0 Conditional Go) and the Sep 16 Deluge fixture (all fives -> 100.0 Go).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solpipe import ingest, matrix, milestones, score, scorecards, seed  # noqa: E402

WEIGHTS = json.dumps({"version": "v1-2026-09", "weights": {
    "1": 5, "2": 5, "3": 5, "4": 7, "5": 7, "6": 6, "7": 5, "8": 5, "9": 5,
    "10": 7, "11": 7, "12": 6, "13": 5, "14": 5, "15": 7, "16": 7, "17": 6}})
CAT = {n: c for n, (_, c) in seed.CRITERIA.items()}


class VarCrm:
    """Only what scorecards.load needs."""
    def __init__(self, extra=None):
        self.vars = {"SOLICITATION_SCORING_WEIGHTS": WEIGHTS, "SOLICITATION_GO_THRESHOLD": "75",
                     "SOLICITATION_COND_GO_THRESHOLD": "55", **(extra or {})}

    def variable(self, name):
        if name not in self.vars:
            raise score.CrmError("HTTP 404 variable not found")
        return self.vars[name]


JP = scorecards.load(VarCrm(), "JusticePoint")


def card(go=75, cond=55):
    return dict(JP, go_threshold=go, cond_threshold=cond)


# Criterion scores 1..17 exactly as in RFP GoNoGo - Charlotte CCPA EM - 3 Scenarios - 2026-09.xlsx
CHARLOTTE = {
    "PRIME": ([3, 3, 3, 2, 2, 1, 4, 3, 3, 0, 1, 2, 0, 1, 3, 3, 3],
              ["Pass", "Unclear", "Unclear", "Pass", "Unclear", "Pass", "Pass", "Unclear"], 42.6, "No-Go"),
    "PRIME+": ([4, 3, 3, 3, 2, 2, 4, 3, 3, 1, 2, 2, 1, 2, 3, 4, 3],
               ["Pass", "Unclear", "Unclear", "Pass", "Pass", "Pass", "Pass", "Unclear"], 52.4, "No-Go"),
    "SUB": ([5, 4, 4, 4, 3, 3, 3, 4, 4, 1, 4, 3, 0, 1, 3, 3, 3], ["Pass"] * 8, 61.0, "Conditional Go"),
}


def rows(scores):
    return [{"Criterion_No": i + 1, "Criterion_Score": s, "Category": CAT[i + 1]} for i, s in enumerate(scores)]


def gates(vals):
    return {f"K{i + 1}": v for i, v in enumerate(vals)}


def test_charlotte_reconciles():
    for name, (sc, g, want, rec) in CHARLOTTE.items():
        r = score.compute(rows(sc), gates(g), JP, name)
        assert r.weighted == want, (name, r.weighted)
        assert r.recommendation == rec, (name, r.recommendation)
        assert r.coverage == 100.0 and r.score_range == f"{want:.1f}"
        assert abs(sum(r.categories.values()) - want) < 0.05


def test_prime_plus_categories_match_workbook():
    r = score.compute(rows(CHARLOTTE["PRIME+"][0]), gates(CHARLOTTE["PRIME+"][1]), JP)
    assert r.categories == {"A": 10.0, "B": 9.4, "C": 10.0, "D": 6.6, "E": 3.0, "F": 13.4}


def test_all_fives_is_100_go():
    r = score.compute(rows([5] * 17), gates(["Pass"] * 8), JP)
    assert (r.weighted, r.recommendation) == (100.0, "Go")
    assert r.categories == {"A": 15, "B": 20, "C": 15, "D": 20, "E": 10, "F": 20}
    assert r.thresholds == "Go>=75 / CondGo>=55" and r.version == "JP-v1-2026-09"
    assert r.rule == "Workbook-v1 rule 4" and "Run key" in r.reason


def test_fail_gate_overrides_score():
    r = score.compute(rows([5] * 17), gates(["Pass"] * 7 + ["Fail"]), JP)
    assert r.recommendation == "No-Go" and "K8" in r.reason


def test_unclear_caps_go():
    r = score.compute(rows([5] * 17), gates(["Unclear"] + ["Pass"] * 7), JP)
    assert r.recommendation == "Conditional Go"


def test_boundaries():
    r = score.compute(rows([3] * 17), gates(["Pass"] * 8), JP)
    assert r.weighted == 60.0 and r.recommendation == "Conditional Go"
    r = score.compute(rows([5] * 17), gates(["N/A"] + ["Pass"] * 7), JP)
    assert r.recommendation == "Go"
    r = score.compute(rows([5] * 17), gates(["Pass"] * 8), card(go=100))
    assert r.recommendation == "Go"


def test_blank_criterion_is_a_range_not_a_zero():
    sc = list(CHARLOTTE["SUB"][0]); sc[16] = None          # criterion 17 (weight 6) unknown
    r = score.compute(rows(sc), gates(["Pass"] * 8), JP)
    assert (r.weighted, r.score_high, r.coverage) == (57.4, 63.4, 94.0)
    assert r.recommendation == "Conditional Go" and r.score_range == "57.4-63.4"
    sc[0] = None                                              # range now straddles 55
    r = score.compute(rows(sc), gates(["Pass"] * 8), JP)
    assert r.recommendation == "Needs review" and "Unrated criteria: 1, 17" in r.reason


def test_blank_gate_counts_as_unclear():
    r = score.compute(rows([5] * 17), gates(["Pass"] * 7 + [None]), JP)
    assert r.recommendation == "Conditional Go" and r.unclears == ["K8"]


def test_not_applicable_renormalises():
    rs = rows([5] * 16 + [0])
    rs[16]["Answer_Status"] = "Not applicable"
    r = score.compute(rs, gates(["Pass"] * 8), JP)
    assert r.weighted == 100.0 and r.run["excluded"] == [17]


def test_mandatory_requirement_rules():
    reqs = score.requirement_inputs([
        {"id": 1, "Our_Position": "Does not meet", "Subject_Entity": "Sirona Recovery"},  # other org: ignored
        {"id": 2, "Our_Position": "Unverified"}], "JusticePoint")
    assert [r["id"] for r in reqs] == ["2"]
    assert score.compute(rows([5] * 17), gates(["Pass"] * 8), JP, requirements=reqs).recommendation == "Go"
    dnm = score.requirement_inputs([{"id": 3, "Our_Position": "Does not meet"}], "JusticePoint")
    assert score.compute(rows([5] * 17), gates(["Pass"] * 8), JP, requirements=dnm).recommendation == "No-Go"
    cure = score.requirement_inputs([{"id": 3, "Our_Position": "Does not meet", "Cure_Owner": {"id": "9"},
                                      "Cure_Deadline": "2026-10-10"}], "JusticePoint")
    assert score.compute(rows([5] * 17), gates(["Pass"] * 8), JP, requirements=cure).recommendation == "Go"


def test_refuses_missing_rows_and_bad_ratings():
    for bad in (rows([5] * 16), rows([5] * 16 + [7])):
        try:
            score.compute(bad, gates(["Pass"] * 8), JP)
        except score.Refused:
            continue
        raise AssertionError("should refuse")


def test_scorecards_by_org():
    assert JP["rule_set"] == "Workbook-v1" and JP["approval_state"] == "Approved"
    assert scorecards.load(VarCrm(), None)["org"] == "JusticePoint"
    sir = scorecards.load(VarCrm(), "Sirona Recovery")
    assert (sir["rule_set"], sir["approval_state"], sir["go_threshold"], sir["min_coverage"]) == ("Handoff-v1", "Draft", 80, 90)
    r = score.compute(rows([5] * 17), gates(["Pass"] * 8), sir)
    assert r.recommendation == "Needs review" and r.rule == "Handoff-v1 rule 1" and r.weighted == 100.0
    ov = json.dumps(dict(sir, approval_state="Approved", version="SIRONA-v1"))
    sir2 = scorecards.load(VarCrm({"SOLICITATION_SCORECARD_SIRONA": ov}), "Sirona Recovery")
    assert score.compute(rows([5] * 17), gates(["Pass"] * 8), sir2).recommendation == "Go"
    assert score.compute(rows(CHARLOTTE["SUB"][0]), gates(["Pass"] * 8), JP).weighted == 61.0


class ModCrm(VarCrm):
    """VarCrm plus coql over fake Scorecard_Versions / Scorecard_Criteria rows (module mode)."""
    def __init__(self, versions, criteria, source="module"):
        super().__init__({"SOLICITATION_SCORECARD_SOURCE": source} if source else None)
        self.versions, self.criteria = versions, criteria

    def coql(self, q):
        if "from Scorecard_Versions" in q:
            org = q.split("Lead_Organization = '")[1].split("'")[0]
            return [v for v in self.versions if v["Lead_Organization"] == org and v["Approval_State"] in ("Approved", "Draft")]
        vid = q.split("Scorecard_Version = ")[1].strip()
        return [c for c in self.criteria if str(c["_v"]) == vid]


def _crit(vid, weights, critical=()):
    return [{"_v": vid, "Criterion_No": n, "Weight": w, "Critical": n in critical} for n, w in weights.items()]


def test_scorecard_from_module():
    w = {int(k): v for k, v in json.loads(WEIGHTS)["weights"].items()}
    w2 = {**w, 1: 4, 2: 6}  # still totals 100
    vers = [
        {"id": 1, "Lead_Organization": "Sirona Recovery", "Version": "SIR-v1", "Rule_Set": "Handoff-v1", "Approval_State": "Draft",
         "Go_Threshold": 80, "Cond_Threshold": 65, "Min_Coverage": 90, "Gates_Enabled": None, "Effective_From": "2026-09-01"},
        {"id": 2, "Lead_Organization": "Sirona Recovery", "Version": "SIR-v2", "Rule_Set": "Handoff-v1", "Approval_State": "Approved",
         "Go_Threshold": 78, "Cond_Threshold": 60, "Min_Coverage": 85, "Gates_Enabled": "K1, K2,K3", "Effective_From": "2026-10-01"},
        {"id": 3, "Lead_Organization": "Sirona Recovery", "Version": "SIR-v3", "Rule_Set": "Handoff-v1", "Approval_State": "Retired",
         "Go_Threshold": 1, "Cond_Threshold": 1, "Min_Coverage": 1, "Gates_Enabled": None, "Effective_From": "2026-12-01"},
        {"id": 4, "Lead_Organization": "JusticePoint", "Version": "JP-v2", "Rule_Set": "Workbook-v1", "Approval_State": "Approved",
         "Go_Threshold": 75, "Cond_Threshold": 55, "Min_Coverage": None, "Gates_Enabled": None, "Effective_From": "2026-10-01"},
    ]
    crit = _crit(1, w) + _crit(2, w2, critical=(1,)) + _crit(4, w)
    crm = ModCrm(vers, crit)
    sir = scorecards.load(crm, "Sirona Recovery")
    # newest Approved wins over an older Draft and over a Retired row
    assert sir["version"] == "SIR-v2" and sir["approval_state"] == "Approved"
    assert (sir["go_threshold"], sir["cond_threshold"], sir["min_coverage"]) == (78.0, 60.0, 85.0)
    assert sir["gates_enabled"] == ["K1", "K2", "K3"]
    assert [c["weight"] for c in sir["criteria"][:2]] == [4.0, 6.0] and sir["criteria"][0].get("critical") is True
    r = score.compute(rows([5] * 17), gates(["Pass"] * 8), sir)
    assert r.version == "SIR-v2" and r.weighted == 100.0 and r.recommendation == "Go"
    # JusticePoint from the module reproduces the calibrated workbook
    jp = scorecards.load(crm, "JusticePoint")
    assert score.compute(rows(CHARLOTTE["SUB"][0]), gates(["Pass"] * 8), jp).weighted == 61.0
    # only a Draft exists -> Draft is used and the engine will not recommend
    crm_d = ModCrm(vers[:1], crit)
    d = scorecards.load(crm_d, "Sirona Recovery")
    assert d["approval_state"] == "Draft"
    assert score.compute(rows([5] * 17), gates(["Pass"] * 8), d).recommendation == "Needs review"
    # no row for the org, or no criteria: stop loudly, no silent fallback to the variables
    for bad in (ModCrm([], crit), ModCrm(vers[:1], [])):
        try:
            scorecards.load(bad, "Sirona Recovery")
        except score.CrmError:
            continue
        raise AssertionError("should stop")
    # weights that do not total 100: the engine says Needs review and names the problem
    crm_bad = ModCrm(vers, _crit(4, {**w, 1: 6}) + _crit(2, w2))
    bad_jp = scorecards.load(crm_bad, "JusticePoint")
    rb = score.compute(rows([5] * 17), gates(["Pass"] * 8), bad_jp)
    assert rb.recommendation == "Needs review" and "total 101" in rb.reason
    # switch off: variables behave exactly as before
    assert scorecards.load(ModCrm(vers, crit, source=None), "JusticePoint")["version"] == "JP-v1-2026-09"
    assert scorecards.load(ModCrm(vers, crit, source="variables"), "Sirona Recovery")["version"] == "SIRONA-draft-2026-09"


def test_seed_non_integer_weight_refused():
    bad = json.dumps({"version": "x", "weights": {"1": 7.5}})
    try:
        seed.build_rows("1", "S", bad, {})
    except ValueError:
        return
    raise AssertionError("should refuse")


def test_seed_rows():
    anchors = {"1": {"0": "a", "3": "b", "5": "c"}}
    rs, ver = seed.build_rows("99", "Scn", WEIGHTS, anchors)
    assert ver == "v1-2026-09" and len(rs) == 17 and sum(r["Weight"] for r in rs) == 100
    assert rs[0]["Anchors"] == "5 = c\n3 = b\n0 = a" and rs[1]["Anchors"] is None
    assert rs[14]["Category"] == "F Strategic Fit" and rs[16]["Name"] == "Scn - C17"


def test_matrix_rows():
    reqs = [{"id": "1", "Name": "R1", "Category": "Technology / Device", "Mandatory": True, "Priority": "Core"},
            {"id": "2", "Name": "R2", "Category": "Insurance", "Mandatory": False, "Priority": None}]
    devs = matrix.devices_from("Buddi Smart Tag\n- Allied VeriTracks\n\nbuddi smart tag\n")
    assert devs == ["Buddi Smart Tag", "Allied VeriTracks"]
    rs = matrix.build_rows("9", reqs, devs)
    assert len(rs) == 4
    assert rs[0]["Assessment_Status"] == "U Unverified" and rs[1]["Assessment_Status"] == "N/A Not device-specific"
    assert rs[1]["Req_Priority"] == "Unspecified" and rs[0]["Req_Mandatory"] is True


def test_queue_roundtrip_matches_deluge_format():
    txt = "123|1 to 1|SKIP|CITY OF CHARLOTTE\n123|1 to 2|PENDING|§4.3 Battery and Continuity"
    q = ingest.parse_queue(txt)
    assert q[1] == ["123", "1 to 2", "PENDING", "§4.3 Battery and Continuity"]
    assert ingest.render_queue(q) == txt



def test_dedupe_and_names():
    texts, names = {ingest._norm("The device shall be waterproof.")}, {"r1-gn-1"}
    reqs = [{"Requirement_Text": "The device SHALL be waterproof", "Source_Reference": "R1-GN-1"},
            {"Requirement_Text": "Battery lasts 30 hours", "Source_Reference": "R1-GN-1", "Category": "Nope",
             "Mandatory": True, "Page_PDF": "14", "Section_Group": "4.3 Battery " + "x" * 200},
            {"Requirement_Text": "Battery lasts 30 hours."}]
    rows_, d = ingest.to_rows(reqs, "7", "Solicitation", "rfp.pdf", texts, names)
    assert d == 2 and len(rows_) == 1
    r = rows_[0]
    assert r["Name"] == "R1-GN-1 (2)" and r["Category"] == "Other" and r["Mandatory"] is True
    assert r["Solicitation"] == {"id": "7"} and r["Our_Position"] == "Unverified"
    assert r["Source_Reference"] == "rfp.pdf, R1-GN-1, p.14" and r["Page_PDF"] == "14"
    assert len(r["Section_Group"]) == 120, "Section_Group is 120 chars in CRM"
    rows2, _ = ingest.to_rows([{"Requirement_Text": "Offerors must be licensed in NC."}], "7", "Solicitation",
                              "rfp.pdf", set(), set(), allowed={"Name", "Requirement_Text"})
    assert set(rows2[0]) == {"Name", "Requirement_Text", "Solicitation"}


def test_no_ai_anywhere():
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = re.compile(r"gemini|openai|anthropic|cohere|generativelanguage|LLM_API_KEY|zia\.|\bllm\b", re.I)
    hits = []
    for d, _, files in os.walk(root):
        if "tests" in d.split(os.sep) or "__pycache__" in d:
            continue
        for f in files:
            if f.endswith((".py", ".dg", ".ps1", ".json", ".txt")):
                p = os.path.join(d, f)
                for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                    if bad.search(line) and "no AI" not in line and "No AI" not in line:
                        hits.append(f"{p}:{i}: {line.strip()[:80]}")
    assert not hits, "\n".join(hits)


def test_milestones():
    from datetime import date
    rec = {"Status": "Reviewing", "Proposal_Due": "2026-10-14T13:00:00-05:00", "Questions_Round_2_Due": "2026-09-23T13:00:00-05:00",
           "Teaming_LOI_Due": "2026-09-26", "Anticipated_Award": "2027-02-01"}
    out = milestones.compute(rec, date(2026, 9, 24))
    assert out == {"Days_To_Deadline": 20, "Deadline_Alert_Stage": "30 day", "Next_Milestone": "Teaming LOI due",
                   "Next_Milestone_Date": "2026-09-26"}
    assert milestones.compute({"Status": "Identified"}, date(2026, 9, 24))["Deadline_Alert_Stage"] == "No deadline"
    assert milestones.stage(-2, "Reviewing") == "Past due" and milestones.stage(-2, "Submitted") == "OK"



if __name__ == "__main__":
    fails = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", k)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", k, repr(e))
    sys.exit(1 if fails else 0)
