"""Which scorecard a Bid Scenario is scored against, chosen by its Lead_Organization.

JusticePoint     Workbook-v1, Approved. Weights from SOLICITATION_SCORING_WEIGHTS, thresholds
                 from SOLICITATION_GO_THRESHOLD / SOLICITATION_COND_GO_THRESHOLD (75 / 55).
Sirona Recovery  Handoff-v1, Draft (80 / 65 / 90% coverage). A Draft scorecard never gives a
The Difference   recommendation: the engine returns Needs review (rule 1) until it is approved.
  Principle      Starting criteria = the 17 workbook criteria and weights, as a placeholder.

Override per org without a code change: create a CRM Variable SOLICITATION_SCORECARD_SIRONA or
SOLICITATION_SCORECARD_TDP (or _JP) holding JSON in the scorecard shape below.

Module source (preferred): create the Scorecard_Versions and Scorecard_Criteria modules, then set the
CRM Variable SOLICITATION_SCORECARD_SOURCE to "module". For the scenario's Lead_Organization the loader
then uses the Approved Scorecard_Versions row with the newest Effective_From (else the newest Draft, which
the engine will score as a range and recommend Needs review). In module mode nothing falls back silently:
no row for the org, or a Zoho error, stops the run with a message, because scoring on the wrong weights
is worse than not scoring. Any other value of the variable (or no variable) keeps the behaviour above.

Shape: {"version", "rule_set", "approval_state", "go_threshold", "cond_threshold",
        "min_coverage", "gates_enabled"?, "criteria": [{"no", "weight", "critical"?}]}
"""
from __future__ import annotations

import json

from .crm import CrmError

JP, SIRONA, TDP = "JusticePoint", "Sirona Recovery", "The Difference Principle"
ORG_KEYS = {JP: "JP", SIRONA: "SIRONA", TDP: "TDP"}
DEFAULT_ORG = JP  # blank Lead_Organization: the module was JP-only until Sep 25


def _var(crm, name: str) -> str | None:
    try:
        return crm.variable(name)
    except CrmError:
        return None  # variable not created


def _workbook_weights(crm) -> tuple[dict[int, float], str]:
    raw = crm.variable("SOLICITATION_SCORING_WEIGHTS")
    if not raw:
        raise CrmError("SOLICITATION_SCORING_WEIGHTS is empty.")
    cfg = json.loads(raw)
    return {int(k): float(v) for k, v in cfg["weights"].items()}, cfg.get("version", "unversioned")


VERSIONS_MODULE = "Scorecard_Versions"
CRITERIA_MODULE = "Scorecard_Criteria"
CRITERIA_LOOKUP = "Scorecard_Version"  # lookup on Scorecard_Criteria to Scorecard_Versions


def _num(v, default=None):
    return default if v in (None, "", "-None-") else float(v)


def from_module(crm, org: str) -> dict:
    """The scorecard for `org` from Scorecard_Versions / Scorecard_Criteria. Raises CrmError if none."""
    rows = crm.coql(
        "select id, Version, Rule_Set, Approval_State, Go_Threshold, Cond_Threshold, Min_Coverage, "
        "Gates_Enabled, Effective_From from Scorecard_Versions "
        f"where Lead_Organization = '{org}' and Approval_State in ('Approved', 'Draft')")
    if not rows:
        raise CrmError(f"No Approved or Draft scorecard version for {org} in {VERSIONS_MODULE}. "
                       "Add one, or clear SOLICITATION_SCORECARD_SOURCE to use the variables.")

    def newest(r):
        return (r.get("Effective_From") or "", str(r.get("id")))
    approved = sorted((r for r in rows if r.get("Approval_State") == "Approved"), key=newest)
    v = approved[-1] if approved else sorted(rows, key=newest)[-1]
    crit = crm.coql(f"select Criterion_No, Weight, Critical from {CRITERIA_MODULE} "
                    f"where {CRITERIA_LOOKUP} = {int(v['id'])}")
    if not crit:
        raise CrmError(f"Scorecard version {v.get('Version')} for {org} has no criteria rows in {CRITERIA_MODULE}.")
    card = {
        "org": org, "version": str(v.get("Version") or v["id"]), "rule_set": v.get("Rule_Set") or "Workbook-v1",
        "approval_state": v.get("Approval_State"),
        "go_threshold": _num(v.get("Go_Threshold")), "cond_threshold": _num(v.get("Cond_Threshold")),
        "min_coverage": _num(v.get("Min_Coverage")),
        "criteria": [{"no": int(c["Criterion_No"]), "weight": float(c["Weight"]),
                      **({"critical": True} if c.get("Critical") else {})}
                     for c in sorted(crit, key=lambda c: int(c["Criterion_No"]))],
    }
    if card["go_threshold"] is None or card["cond_threshold"] is None:
        raise CrmError(f"Scorecard version {card['version']} for {org} needs Go and Conditional thresholds.")
    gates = (v.get("Gates_Enabled") or "").replace(";", ",").split(",")
    gates = [g.strip() for g in gates if g.strip()]
    if gates:
        card["gates_enabled"] = gates
    return card


def load(crm, org: str | None) -> dict:
    org = org if org in ORG_KEYS else DEFAULT_ORG
    if (_var(crm, "SOLICITATION_SCORECARD_SOURCE") or "").strip().lower() == "module":
        return from_module(crm, org)
    override = _var(crm, "SOLICITATION_SCORECARD_" + ORG_KEYS[org])
    if override:
        sc = json.loads(override)
        sc.setdefault("org", org)
        return sc
    weights, version = _workbook_weights(crm)
    criteria = [{"no": n, "weight": w} for n, w in sorted(weights.items())]
    if org == JP:
        return {"org": JP, "version": "JP-" + version, "rule_set": "Workbook-v1", "approval_state": "Approved",
                "go_threshold": float(_var(crm, "SOLICITATION_GO_THRESHOLD") or 75),
                "cond_threshold": float(_var(crm, "SOLICITATION_COND_GO_THRESHOLD") or 55),
                "min_coverage": None, "criteria": criteria}
    return {"org": org, "version": ORG_KEYS[org] + "-draft-2026-09", "rule_set": "Handoff-v1",
            "approval_state": "Draft", "go_threshold": 80, "cond_threshold": 65, "min_coverage": 90,
            "criteria": criteria}
