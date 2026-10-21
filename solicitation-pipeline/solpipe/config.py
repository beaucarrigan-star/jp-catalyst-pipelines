"""Runtime configuration, read from environment variables.

Locally, put these in a file named `.env` OUTSIDE any synced folder and point
SOLPIPE_ENV_FILE at it (or set them in the shell). In Catalyst, set them as the
function's environment variables (catalyst-config.json -> env_variables, or the
console). Nothing secret lives in the code or in CRM Variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _load_env_file() -> None:
    path = os.environ.get("SOLPIPE_ENV_FILE")
    if not path:
        home = os.path.expanduser("~")
        cand = os.path.join(home, ".solpipe.env")
        path = cand if os.path.exists(cand) else None
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    client_id: str
    client_secret: str
    refresh_token: str
    accounts_url: str
    api_domain: str
    pipeline_token: str | None
    jobpool_name: str
    worker_name: str

    @classmethod
    def from_env(cls) -> "Config":
        _load_env_file()
        missing = [k for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN") if not os.environ.get(k)]
        if missing:
            raise RuntimeError("Missing environment variable(s): " + ", ".join(missing))
        return cls(
            client_id=os.environ["ZOHO_CLIENT_ID"],
            client_secret=os.environ["ZOHO_CLIENT_SECRET"],
            refresh_token=os.environ["ZOHO_REFRESH_TOKEN"],
            accounts_url=os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip("/"),
            api_domain=os.environ.get("ZOHO_API_DOMAIN", "https://www.zohoapis.com").rstrip("/"),
            pipeline_token=os.environ.get("PIPELINE_TOKEN") or None,
            jobpool_name=os.environ.get("JOBPOOL_NAME", "soljobs"),
            worker_name=os.environ.get("WORKER_FUNCTION", "sol_worker"),
        )
