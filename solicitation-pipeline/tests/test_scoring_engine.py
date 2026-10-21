"""Acceptance tests for scoring_engine (spec section 'Acceptance tests', Sep 25 2026).
Charlotte fixtures pulled live from CRM Scenario_Scores / Bid_Scenarios on Sep 25, 2026."""
import copy
import os
import pathlib
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solpipe.scoring_engine import (evaluate, apply_tracking, validate_scorecard, GO, COND, NOGO, REVIEW, TRACK)  # noqa: E402

T = "2026-09-25T09:00:00-05:00"
JP_WEIGHTS = {1: 5, 2: 5, 3: 5, 4: 7, 5: 7, 6: 6, 7: 5, 8: 5, 9: 5, 10: 7, 11: 7, 12: 6, 13: 5, 14: 5, 15: 7, 16: 7, 17: 6}
JP_V1 = {"version": "JP-v1-2026-09", "rule_set": "Workbook-v1", "approval_state": "Approved",
         "go_threshold": 75, "cond_threshold": 55, "min_coverage": None,
         "criteria": [{"no": n, "weight": w} for n, w in JP_WEIGHTS.items()]}

# Criterion_Score 1..17, as stored in CRM
CHARLOTTE = {
    "PRIME":  ([3, 3, 3, 2, 2, 1, 4, 3, 3, 0, 1, 2, 0, 1, 3, 3, 3],
               {"K1": "Pass", "K2": "Unclear", "K3": "Unclear", "K4": "Pass", "K5": "Unclear", "K6": "Pass", "K7": "Pass", "K8": "Unclear"}),
    "PRIME+": ([4, 3, 3, 3, 2, 2, 4, 3, 3, 1, 2, 2, 1, 2, 3, 4, 3],
               {"K1": "Pass", "K2": "Unclear", "K3": "Unclear", "K4": "Pass", "K5": "Pass", "K6": "Pass", "K7": "Pass", "K8": "Unclear"}),
    "SUB":    ([5, 4, 4, 4, 3, 3, 3, 4, 4, 1, 4, 3, 0, 1, 3, 3, 3],
               {k: "Pass" for k in ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8"]}),
}


def charlotte(name):
    scores, gates = CHARLOTTE[name]
    # status left blank on purpose: the live rows predate Answer_Status
    return {"id": name, "name": name, "org": "JusticePoint", "gates": gates,
            "assessments": {i + 1: {"rating": s} for i, s in enumerate(scores)}}


DEMO = {"version": "ZZ-Demo-1", "rule_set": "Handoff-v1", "approval_state": "Draft", "demo": True,
        "go_threshold": 80, "cond_threshold": 65, "min_coverage": 90,
        "gates_enabled": ["K1"],
        "criteria": [{"no": 1, "weight": 25}, {"no": 2, "weight": 25}, {"no": 3, "weight": 20},
                     {"no": 4, "weight": 20}, {"no": 5, "weight": 10}]}


def demo(ratings, gates=None, reqs=None):
    a = {}
    for i, r in enumerate(ratings):
        a[i + 1] = {"status": "Unknown"} if r is None else ({"status": "Not applicable"} if r == "NA" else {"status": "Rated", "rating": r})
    return {"id": "ZZ", "name": "ZZ scenario", "org": "ZZ Test Org", "gates": gates or {"K1": "Pass"},
            "assessments": a, "requirements": reqs or []}


class Charlotte(unittest.TestCase):
    def test_1_prime(self):
        r = evaluate(charlotte("PRIME"), JP_V1, T)
        self.assertEqual((r["score_low"], r["score_high"], r["recommendation"]), (42.6, 42.6, NOGO))

    def test_2_prime_plus(self):
        r = evaluate(charlotte("PRIME+"), JP_V1, T)
        self.assertEqual((r["score_low"], r["recommendation"]), (52.4, NOGO))

    def test_3_sub(self):
        r = evaluate(charlotte("SUB"), JP_V1, T)
        self.assertEqual((r["score_low"], r["coverage_pct"], r["recommendation"]), (61.0, 100.0, COND))

    def test_4_all_fives_go(self):
        s = charlotte("SUB")
        s["assessments"] = {n: {"rating": 5} for n in JP_WEIGHTS}
        r = evaluate(s, JP_V1, T)
        self.assertEqual((r["score_low"], r["recommendation"]), (100.0, GO))

    def test_4b_high_score_unclear_gate_caps_at_conditional(self):
        s = charlotte("PRIME")
        s["assessments"] = {n: {"rating": 5} for n in JP_WEIGHTS}
        self.assertEqual(evaluate(s, JP_V1, T)["recommendation"], COND)

    def test_4c_workbook_gate_fail(self):
        s = charlotte("SUB")
        s["gates"] = dict(s["gates"], K2="Fail")
        self.assertEqual(evaluate(s, JP_V1, T)["recommendation"], NOGO)


class Handoff(unittest.TestCase):
    def test_5_complete(self):
        r = evaluate(demo([4, 3, 5, 4, 2]), DEMO, T)
        self.assertEqual((r["score_low"], r["score_high"], r["coverage_pct"]), (75.0, 75.0, 100.0))
        self.assertEqual(r["recommendation"], COND)  # 75 >= 65, no open conditions

    def test_6_unknown(self):
        r = evaluate(demo([4, 3, 5, None, 2]), DEMO, T)
        self.assertEqual((r["score_low"], r["score_high"], r["coverage_pct"]), (59.0, 79.0, 80.0))
        self.assertEqual(r["recommendation"], REVIEW)

    def test_7_mandatory_fail_and_tracking(self):
        reqs = [{"id": "R1", "position": "Does not meet", "curable": False}]
        r = evaluate(demo([5, 5, 5, 5, 3], reqs=reqs), DEMO, T)
        self.assertEqual(r["score_low"], 96.0)
        self.assertEqual(r["recommendation"], NOGO)
        with self.assertRaises(ValueError):
            apply_tracking(r, "recompete 2028", None, "2028-01-01")
        self.assertEqual(apply_tracking(r, "recompete 2028", "Beau", "2028-01-01")["disposition"], TRACK)

    def test_8_unknown_mandatory(self):
        reqs = [{"id": "R1", "position": "Unverified"}]
        self.assertEqual(evaluate(demo([5, 5, 5, 5, 3], reqs=reqs), DEMO, T)["recommendation"], REVIEW)

    def test_9_go_boundary(self):
        r = evaluate(demo([4, 4, 4, 4, 4]), DEMO, T)
        self.assertEqual((r["score_low"], r["recommendation"]), (80.0, GO))

    def test_10_conditional_boundary(self):
        # exact 65: 4,3,3,2,5 -> 20+15+12+8+10 = 65
        planned = [{"id": "R2", "position": "Conditional", "cure_owner": "Beau", "cure_deadline": "2026-10-10"}]
        unplanned = [{"id": "R2", "position": "Conditional"}]
        r1 = evaluate(demo([4, 3, 3, 2, 5], reqs=planned), DEMO, T)
        r2 = evaluate(demo([4, 3, 3, 2, 5], reqs=unplanned), DEMO, T)
        self.assertEqual(r1["score_low"], 65.0)
        self.assertEqual((r1["recommendation"], r2["recommendation"]), (COND, REVIEW))

    def test_10b_below(self):
        self.assertEqual(evaluate(demo([2, 2, 2, 2, 2]), DEMO, T)["recommendation"], NOGO)

    def test_11_not_applicable(self):
        r = evaluate(demo([4, 3, 5, "NA", 2]), DEMO, T)
        # 25/80,25/80,20/80,10/80 -> 25+18.75+25+5 = 73.75
        self.assertEqual(r["score_low"], 73.8)
        self.assertEqual(r["excluded"], [4])
        r = evaluate(demo(["NA"] * 5), DEMO, T)
        self.assertEqual((r["score_low"], r["recommendation"]), (None, REVIEW))

    def test_12_isolation(self):
        before = copy.deepcopy(JP_V1)
        sirona = copy.deepcopy(DEMO)
        sirona["criteria"][0]["weight"] = 30
        sirona["criteria"][1]["weight"] = 20
        evaluate(demo([4, 3, 5, 4, 2]), sirona, T)
        self.assertEqual(JP_V1, before)
        self.assertEqual(evaluate(charlotte("SUB"), JP_V1, T)["score_low"], 61.0)

    def test_unapproved_production_scorecard(self):
        sc = dict(DEMO, demo=False)
        self.assertEqual(evaluate(demo([5, 5, 5, 5, 5]), sc, T)["rule_fired"], 1)

    def test_invalid_weights_rejected(self):
        sc = copy.deepcopy(DEMO)
        sc["criteria"][0]["weight"] = 30
        self.assertTrue(validate_scorecard(sc))
        self.assertEqual(evaluate(demo([5, 5, 5, 5, 5]), sc, T)["recommendation"], REVIEW)

    def test_16_repeatable(self):
        a = evaluate(charlotte("SUB"), JP_V1, T)
        b = evaluate(charlotte("SUB"), JP_V1, T)
        self.assertEqual(a, b)
        c = evaluate(charlotte("PRIME"), JP_V1, T)
        self.assertNotEqual(a["run_key"], c["run_key"])

    def test_rubric_endpoints_and_range_check(self):
        self.assertEqual(evaluate(demo([0, 0, 0, 0, 0]), DEMO, T)["score_low"], 0.0)
        with self.assertRaises(ValueError):
            evaluate(demo([6, 0, 0, 0, 0]), DEMO, T)


class NoAI(unittest.TestCase):
    def test_15_no_network_or_ai(self):
        src = (pathlib.Path(__file__).parent.parent / "solpipe" / "scoring_engine.py").read_text()
        for bad in ["gemini", "openai", "anthropic", "cohere", "urllib", "requests", "http", "socket", "LLM_API_KEY"]:
            self.assertIsNone(re.search(bad, src, re.I), bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
