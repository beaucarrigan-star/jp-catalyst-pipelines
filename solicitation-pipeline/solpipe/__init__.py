"""Solicitation pipeline for Zoho CRM (org 882980956). No AI tokens anywhere (Sep 25, 2026).

Actions, each keyed by a record id:
  ingest  <Solicitation id>  attachments -> draft Solicitation_Requirements (rule-based)
  seed    <Bid Scenario id>  17 Scenario_Scores rows with anchors
  matrix  <Bid Scenario id>  requirement x device Spec_Assessments
  score   <Bid Scenario id>  deterministic engine: score range, coverage, gates, recommendation
  briefing <Solicitation id>  Word briefing document attached to the Solicitation (no AI)
  milestones <id|all>        deadline stage and next milestone (run daily)
"""
from __future__ import annotations

from . import briefdoc, ingest, matrix, milestones, score, seed
from .config import Config
from .crm import Crm

ACTIONS = {"briefing": "Solicitations", "ingest": "Solicitations", "milestones": "Solicitations", "seed": "Bid_Scenarios", "matrix": "Bid_Scenarios", "score": "Bid_Scenarios"}
__version__ = "2.0.0"


def dispatch(action: str, record_id: str, cfg: Config | None = None, budget_s: float = 840,
             dry_run: bool = False) -> str:
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}; expected one of {', '.join(ACTIONS)}")
    record_id = str(record_id).strip()
    if not record_id.isdigit() and not (action == "milestones" and record_id == "all"):
        raise ValueError(f"record id must be numeric, got {record_id!r} (is the button argument a merge field?)")
    cfg = cfg or Config.from_env()
    crm = Crm(cfg)
    if action == "ingest":
        return ingest.run(crm, record_id, budget_s=budget_s, dry_run=dry_run)
    if action == "briefing":
        return briefdoc.run(crm, record_id, dry_run=dry_run)
    if action == "milestones":
        return milestones.run(crm, record_id, dry_run=dry_run)
    mod = {"seed": seed, "matrix": matrix, "score": score}[action]
    return mod.run(crm, record_id, dry_run=dry_run)
