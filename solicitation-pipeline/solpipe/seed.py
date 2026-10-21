"""Seed Scorecard: port of solicitation_seed_scorecard.

Creates the 17 Scenario_Scores rows for a Bid Scenario from the live weight set and
the two anchor variables. Refuses to seed twice.
"""
from __future__ import annotations

import json

from . import scorecards
from .crm import Crm

# Criterion names and categories from the go/no-go workbook's Scorecard tab.
CRITERIA = {
    1: ("Mandatory qualifications", "A Requirements"),
    2: ("Service model experience", "A Requirements"),
    3: ("Past performance & references", "A Requirements"),
    4: ("Budget adequacy", "B Financial"),
    5: ("Reimbursement & cash flow", "B Financial"),
    6: ("Match, indirect & financial terms", "B Financial"),
    7: ("Scale fit", "C Capacity"),
    8: ("Staffing feasibility", "C Capacity"),
    9: ("Infrastructure readiness", "C Capacity"),
    10: ("Funder relationship / history", "D Funder & Compete"),
    11: ("Competitive position", "D Funder & Compete"),
    12: ("Procurement dynamics", "D Funder & Compete"),
    13: ("Service area fit", "E Geography"),
    14: ("Local presence & partnerships", "E Geography"),
    15: ("Mission & service-line alignment (org overlay)", "F Strategic Fit"),
    16: ("Strategic growth priorities", "F Strategic Fit"),
    17: ("Portfolio effect", "F Strategic Fit"),
}


def anchor_text(a: dict | None) -> str | None:
    if not a:
        return None
    return "\n".join(f"{k} = {a[k]}" for k in ("5", "3", "0") if a.get(k))


def build_rows(scenario_id: str, scenario_name: str, weights_json: str, anchors: dict) -> tuple[list[dict], str]:
    cfg = json.loads(weights_json)
    weights = {}
    for k, v in cfg["weights"].items():
        if float(v) != int(float(v)):
            raise ValueError(f"Criterion {k} weight {v} is not a whole number; Scenario_Scores.Weight is an integer field.")
        weights[int(k)] = int(float(v))
    rows = []
    for n in sorted(weights):
        name, cat = CRITERIA[n]
        rows.append({
            "Name": f"{scenario_name} - C{n}"[:120],
            "Bid_Scenario": {"id": scenario_id},
            "Criterion_No": n,
            "Criterion": name,
            "Category": cat,
            "Weight": weights[n],
            "Anchors": anchor_text(anchors.get(str(n))),
        })
    return rows, cfg.get("version", "unversioned")


def run(crm: Crm, scenario_id: str, dry_run: bool = False) -> str:
    existing = crm.coql(f"select id from Scenario_Scores where Bid_Scenario = {int(scenario_id)}")
    if existing:
        return f"Already seeded - {len(existing)} rows exist. Nothing created."
    sc = crm.get_record("Bid_Scenarios", scenario_id)
    anchors: dict = {}
    for var in ("SOLICITATION_SCORE_ANCHORS", "SOLICITATION_SCORE_ANCHORS_2"):
        v = crm.variable(var)
        if v:
            anchors.update(json.loads(v))
    # Same scorecard Re-score will use (follows Lead_Organization), so the weights written on the rows
    # and the Weight_Set_Version stamped here match what scoring applies.
    org = sc.get("Lead_Organization")
    org = org if org in scorecards.ORG_KEYS else scorecards.DEFAULT_ORG
    card = scorecards.load(crm, org)
    weights_json = json.dumps({"version": card["version"],
                               "weights": {str(c["no"]): c["weight"] for c in card["criteria"]}})
    rows, version = build_rows(scenario_id, sc.get("Name") or scenario_id, weights_json, anchors)
    if dry_run:
        return f"[dry run] would create {len(rows)} rows (weight set {version})"
    crm.create_many("Scenario_Scores", rows)
    crm.update("Bid_Scenarios", scenario_id, {"Weight_Set_Version": version, "Recommendation": "Not yet scored"})
    note = "" if anchors else " Anchors blank - SOLICITATION_SCORE_ANCHORS is empty."
    return f"Seeded {len(rows)} criteria (weight set {version}). Score each 0-5, then press Re-score.{note}"
