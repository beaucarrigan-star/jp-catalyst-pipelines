"""Generate Spec Matrix: port of solicitation_build_spec_matrix.

One Spec_Assessments row per requirement x device in the scenario's Device_Lineup.
Idempotent per requirement x device pair (existing pairs are skipped, so a
partial run is completed on the next press). Mandatory and
Priority are denormalised onto the row because Zoho rollups cannot filter through
the Requirement lookup. Lines whose category is not Technology / Device are
pre-marked "N/A Not device-specific". No row cap: Python pages COQL properly, so the
Deluge 600-row ceiling is gone.
"""
from __future__ import annotations

from .crm import Crm

DEVICE_CATEGORY = "Technology / Device"


def devices_from(lineup: str | None) -> list[str]:
    seen, out = set(), []
    for line in (lineup or "").replace("\r", "").split("\n"):
        d = line.strip().lstrip("-*• ").strip()
        if d and d.lower() not in seen:
            seen.add(d.lower())
            out.append(d)
    return out


def build_rows(scenario_id: str, reqs: list[dict], devices: list[str], req_name_field: str = "Name") -> list[dict]:
    rows = []
    for dev in devices:
        for r in reqs:
            rname = r.get(req_name_field) or r["id"]
            device_specific = r.get("Category") == DEVICE_CATEGORY
            rows.append({
                "Name": f"{rname} - {dev}"[:120],
                "Requirement": {"id": r["id"]},
                "Device_Name": dev[:255],
                "Bid_Scenario": {"id": scenario_id},
                "Req_Mandatory": bool(r.get("Mandatory")),
                "Req_Priority": r.get("Priority") or "Unspecified",
                "Assessment_Status": "U Unverified" if device_specific else "N/A Not device-specific",
            })
    return rows


def run(crm: Crm, scenario_id: str, dry_run: bool = False) -> str:
    sc = crm.get_record("Bid_Scenarios", scenario_id)
    devices = devices_from(sc.get("Device_Lineup"))
    if not devices:
        return "Device Lineup is empty. Put one device per line in Device Lineup, then press Generate Spec Matrix."
    sol_lk = crm.lookup_field_to("Bid_Scenarios", "Solicitations")
    parent = sc.get(sol_lk)
    if not parent:
        return "This scenario is not linked to a Solicitation, so there are no requirements to assess."
    req_lk = crm.lookup_field_to("Solicitation_Requirements", "Solicitations")
    reqs = crm.coql(f"select id, Name, Category, Mandatory, Priority from Solicitation_Requirements "
                    f"where {req_lk} = {int(parent['id'])}")
    if not reqs:
        return "The Solicitation has no requirements yet. Run Ingest Requirements first."
    have = crm.coql(f"select Requirement, Device_Name from Spec_Assessments where Bid_Scenario = {int(scenario_id)}")
    # idempotent per (requirement, device) pair, so a run that died part-way is completed, not skipped
    have_pairs = {((h.get("Requirement") or {}).get("id"), (h.get("Device_Name") or "").strip().lower()) for h in have}
    rows = [r for r in build_rows(scenario_id, reqs, devices)
            if (r["Requirement"]["id"], r["Device_Name"].strip().lower()) not in have_pairs]
    todo = sorted({r["Device_Name"] for r in rows}, key=devices.index)
    skipped = [d for d in devices if d not in todo]
    if dry_run:
        return f"[dry run] would create {len(rows)} rows for {len(todo)} device(s); skip {len(skipped)}"
    if rows:
        crm.create_many("Spec_Assessments", rows, trigger=())
    crm.update("Bid_Scenarios", scenario_id, {"Matrix_Generated": True})
    n_dev = sum(1 for r in reqs if r.get("Category") == DEVICE_CATEGORY)
    msg = (f"Created {len(rows):,} assessment row(s): {len(reqs)} requirements x {len(todo)} device(s); "
           f"{n_dev} requirement(s) are device-specific, the rest pre-marked N/A.")
    if skipped:
        msg += f" Skipped {len(skipped)} device(s) already in the matrix: {', '.join(skipped)}."
    return msg
