#!/usr/bin/env python3
"""Write function environment variables into the Catalyst configs of a build folder.

    python ci/inject_env.py solicitation <build_dir>
    python ci/inject_env.py contract <build_dir>

Values come from environment variables (GitHub secrets). Mirrors what deploy.ps1 and
deploy-contracts.ps1 do from .solpipe.env / .conpipe.env. Written without a BOM.
"""
import json, os, sys
from pathlib import Path

def need(name, default=None):
    v = os.environ.get(name, "").strip() or default
    if not v:
        raise SystemExit(f"Missing secret/variable {name}")
    return v

ZOHO3 = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")

def spec(kind):
    if kind == "solicitation":
        env = {k: need(k) for k in ZOHO3}
        for k in ("ZOHO_ACCOUNTS_URL", "ZOHO_API_DOMAIN"):
            if os.environ.get(k, "").strip():
                env[k] = os.environ[k].strip()
        env["PIPELINE_TOKEN"] = need("PIPELINE_TOKEN")
        return {"sol_api": dict(env), "sol_worker": dict(env)}
    if kind == "contract":
        return {
            "con_api": {"CON_PIPELINE_TOKEN": need("CON_PIPELINE_TOKEN")},
            "con_worker": {
                **{k: need(k) for k in ZOHO3},
                "ZOHO_ACCOUNTS_DOMAIN": need("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com"),
                "ZOHO_CRM_DOMAIN": need("ZOHO_API_DOMAIN", "https://www.zohoapis.com"),
            },
        }
    raise SystemExit("kind must be solicitation or contract")

def main():
    kind, build = sys.argv[1], Path(sys.argv[2])
    for fn, env in spec(kind).items():
        p = build / "functions" / fn / "catalyst-config.json"
        cfg = json.loads(p.read_text(encoding="utf-8-sig"))
        cfg["deployment"]["env_variables"] = env
        p.write_bytes(json.dumps(cfg, indent=2).encode("utf-8"))   # no BOM
        print(f"{fn}: {len(env)} environment variables set")

if __name__ == "__main__":
    main()
