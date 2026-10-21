"""Catalyst Advanced I/O function (type "advancedio"): the endpoint the CRM buttons call.

POST /run   {"action": "ingest|briefing|seed|matrix|score", "record_id": "<id>"}
            header X-Pipeline-Token: <PIPELINE_TOKEN>
GET  /health

seed and score finish in a few seconds and run inline, so the button shows the
result. ingest and matrix can take minutes, which is past this function type's
30-second ceiling, so they are handed to the sol_worker job function (15-minute
ceiling) and the button says so; the worker writes its result as a Note on the
record when it finishes.
"""
from __future__ import annotations

import hmac
import logging
import os

from flask import Request, jsonify, make_response

import solpipe
from solpipe.config import Config

INLINE = {"seed", "score", "milestones", "briefing"}
log = logging.getLogger()


def _reply(ok: bool, message: str, code: int = 200):
    return make_response(jsonify({"status": "success" if ok else "error", "message": message}), code)


def handler(request: Request):
    # Catalyst may pass the path with or without the /server/sol_api prefix; match on the last segment.
    tail = request.path.rstrip("/").rsplit("/", 1)[-1]
    if tail in ("", "sol_api", "health"):
        return _reply(True, f"solicitation pipeline {solpipe.__version__}")
    if tail != "run" or request.method != "POST":
        return _reply(False, "Unknown path", 404)

    expected = os.environ.get("PIPELINE_TOKEN", "")
    got = request.headers.get("X-Pipeline-Token", "")
    if not expected or not hmac.compare_digest(expected, got):
        return _reply(False, "Unauthorised", 401)

    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).strip().lower()
    rid = str(body.get("record_id", "")).strip()
    if action not in solpipe.ACTIONS:
        return _reply(False, f"Unknown action {action!r}", 400)
    if not rid.isdigit() and not (action == "milestones" and rid == "all"):
        return _reply(False, f"record_id must be a numeric CRM id, got {rid!r}. "
                             "Check the button argument is the record Id merge field.", 400)

    try:
        if action in INLINE:
            return _reply(True, solpipe.dispatch(action, rid, budget_s=25))
        cfg = Config.from_env()
        import zcatalyst_sdk
        app = zcatalyst_sdk.initialize()
        job = app.job_scheduling().job.submit_job({
            "job_name": f"{action[:3]}_{rid[-14:]}",  # Catalyst caps job names at 20 characters
            "jobpool_name": cfg.jobpool_name,
            "target_type": "Function",
            "target_name": cfg.worker_name,
            "params": {"action": action, "record_id": rid},
        })
        log.info("submitted job %s", job)
        what = "Reading the attached documents" if action == "ingest" else "Building the spec matrix"
        return _reply(True, f"{what} in the background. This usually takes 1-10 minutes; the result "
                            "appears as a Note on this record. Refresh the page to see it.")
    except Exception as e:  # noqa: BLE001 - surface every failure to the button
        log.exception("run failed")
        return _reply(False, f"{action} failed: {str(e)[:600]}", 500)
