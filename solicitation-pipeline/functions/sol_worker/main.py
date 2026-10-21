"""Catalyst Job function: runs the long actions (ingest, matrix) submitted by
sol_api, within the 15-minute job ceiling, and posts the outcome as a Note on the
record so the person who pressed the button can see it.
"""
from __future__ import annotations

import logging

import solpipe
from solpipe.config import Config
from solpipe.crm import Crm

log = logging.getLogger()


def handler(job_request, context):
    action = job_request.get_job_param("action")
    rid = job_request.get_job_param("record_id")
    # leave 90 s for the final queue save and the note
    budget = max(60.0, context.get_remaining_execution_time_ms() / 1000.0 - 90)
    module = solpipe.ACTIONS.get(action, "Solicitations")
    title = {"ingest": "Ingest Requirements", "matrix": "Generate Spec Matrix"}.get(action, action)
    try:
        msg = solpipe.dispatch(action, rid, budget_s=budget)
        ok = True
    except Exception as e:  # noqa: BLE001
        log.exception("job failed")
        msg, ok = f"Failed: {str(e)[:1500]}", False
    try:
        Crm(Config.from_env()).add_note(module, rid, f"Pipeline - {title}", msg)
    except Exception:  # noqa: BLE001
        log.exception("could not post note")
    log.info("%s %s: %s", action, rid, msg)
    if ok:
        context.close_with_success()
    else:
        context.close_with_failure()
