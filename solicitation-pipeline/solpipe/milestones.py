"""Milestone alerts: port of the planned solicitation_milestone_alerts (build pack §8.1).

For every Solicitation that is not closed (tracking records included) compute
Days_To_Deadline and Deadline_Alert_Stage from Proposal_Due, and Next_Milestone /
Next_Milestone_Date from the twelve milestone dates. Writes only records whose values
changed. Meant to run daily (Catalyst cron, or `python cli.py milestones all`).
"""
from __future__ import annotations

from datetime import date, datetime

from .crm import Crm

MILESTONES = [
    ("Questions_Round_1_Due", "Questions round 1 due"),
    ("Pre_Proposal_Conference", "Pre-proposal conference"),
    ("Questions_Round_2_Due", "Questions round 2 due"),
    ("Partner_Commitment_Due", "Partner commitment due"),
    ("Teaming_LOI_Due", "Teaming LOI due"),
    ("Internal_Upload_Target", "Internal upload target"),
    ("Proposal_Due", "Proposal due"),
    ("Field_Test_Start", "Field test starts"),
    ("Field_Test_End", "Field test ends"),
    ("Shortlist_Window_Start", "Shortlist window opens"),
    ("Anticipated_Award", "Anticipated award"),
    ("Anticipated_Start", "Anticipated start"),
]
CLOSED = {"Won", "Lost", "Cancelled by Agency", "Withdrawn"}


def _d(v) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)).date() if "T" in str(v) else date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def stage(days: int | None, status: str | None) -> str:
    if days is None:
        return "No deadline"
    if status in ("Submitted", "Shortlisted / Orals") and days < 0:
        return "OK"
    if days < 0:
        return "Past due"
    if days == 0:
        return "Due today"
    for lim, name in ((3, "3 day"), (7, "7 day"), (14, "14 day"), (30, "30 day")):
        if days <= lim:
            return name
    return "OK"


def compute(rec: dict, today: date) -> dict:
    due = _d(rec.get("Proposal_Due"))
    days = (due - today).days if due else None
    upcoming = sorted((d, label) for f, label in MILESTONES if (d := _d(rec.get(f))) and d >= today)
    nxt = upcoming[0] if upcoming else None
    return {
        "Days_To_Deadline": days,
        "Deadline_Alert_Stage": stage(days, rec.get("Status")),
        "Next_Milestone": nxt[1] if nxt else None,
        "Next_Milestone_Date": nxt[0].isoformat() if nxt else None,
    }


def run(crm: Crm, record_id: str = "all", dry_run: bool = False, today: date | None = None) -> str:
    today = today or date.today()
    cols = ", ".join(["id", "Name", "Status", "Days_To_Deadline", "Deadline_Alert_Stage", "Next_Milestone",
                      "Next_Milestone_Date"] + [f for f, _ in MILESTONES])
    where = f"id = {int(record_id)}" if record_id.isdigit() else "id is not null"
    recs = [r for r in crm.coql(f"select {cols} from Solicitations where {where}") if r.get("Status") not in CLOSED]
    changed = []
    for r in recs:
        new = compute(r, today)
        if any(str(r.get(k)) != str(v) for k, v in new.items()):
            changed.append((r, new))
    if not dry_run:
        for r, new in changed:
            crm.update("Solicitations", r["id"], new)
    return (("[dry run] " if dry_run else "") + f"{len(recs)} open solicitation(s) checked, {len(changed)} updated.")
