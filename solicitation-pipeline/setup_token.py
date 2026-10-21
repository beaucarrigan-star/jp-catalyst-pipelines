"""One-time: turn a Zoho Self Client grant code into a refresh token and write
%USERPROFILE%\\.solpipe.env.

1. https://api-console.zoho.com -> Add Client -> Self Client (once).
2. Self Client -> Generate Code, scope (paste exactly):
       ZohoCRM.modules.ALL,ZohoCRM.settings.fields.READ,ZohoCRM.settings.variables.READ,ZohoCRM.coql.READ
   time duration 10 minutes, description "solicitation pipeline".
3. python setup_token.py   and paste the client id, client secret and code.

The secret is typed visibly on purpose: in a Windows console, Ctrl+V into a hidden
prompt inserts a control character instead of pasting, which Zoho rejects.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

ENV = os.path.join(os.path.expanduser("~"), ".solpipe.env")

HINTS = {
    "invalid_code": "The grant code is expired, already used, or mistyped. Codes last the time you picked "
                    "(10 minutes) and work ONCE. Generate a new code and run this again straight away.",
    "invalid_client": "The Client ID or Client Secret is wrong, or belongs to a different Self Client than the "
                      "code. Copy both again from the Client Secret tab of the same Self Client.",
    "invalid_client_secret": "The Client Secret is wrong. Copy it again from the Client Secret tab.",
    "network": "Could not reach accounts.zoho.com. Check your internet or VPN connection and try again.",
}


def ask(prompt: str) -> str:
    # drop anything that is not printable (e.g. a stray ^V from Ctrl+V) and surrounding spaces/quotes
    v = "".join(ch for ch in input(prompt) if ch.isprintable()).strip().strip('"').strip("'")
    return v


def exchange(accounts: str, cid: str, csec: str, code: str) -> dict:
    data = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": cid,
                                   "client_secret": csec, "code": code}).encode()
    req = urllib.request.Request(accounts + "/oauth/v2/token", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": f"HTTP {e.code}", "detail": text[:500]}
    except urllib.error.URLError as e:
        return {"error": "network", "detail": str(e.reason)[:300]}


def main() -> None:
    print("Paste each value, then press Enter. (Right-click also pastes in PowerShell.)")
    cid = ask("Client ID: ")
    csec = ask("Client Secret: ")
    code = ask("Grant code: ")
    if not (cid.startswith("1000.") and code.startswith("1000.")):
        print("Note: Zoho Client IDs and grant codes normally start with '1000.' - check you pasted the right values.")
    accounts = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip("/")
    body = exchange(accounts, cid, csec, code)
    if "refresh_token" not in body:
        err = str(body.get("error", "unknown"))
        print(f"\nZoho refused the exchange: {json.dumps(body)}")
        print(HINTS.get(err, "Generate a new grant code and try again. If it fails the same way, send this message to Claude."))
        raise SystemExit(1)
    api = body.get("api_domain", "https://www.zohoapis.com")
    lines = [f"ZOHO_CLIENT_ID={cid}", f"ZOHO_CLIENT_SECRET={csec}", f"ZOHO_REFRESH_TOKEN={body['refresh_token']}",
             f"ZOHO_ACCOUNTS_URL={accounts}", f"ZOHO_API_DOMAIN={api}",
             f"PIPELINE_TOKEN={secrets.token_urlsafe(32)}"]
    with open(ENV, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.system("cls" if os.name == "nt" else "clear")  # take the secret off the screen
    print(f"Done. Wrote {ENV} (API domain {api}). Keep it out of OneDrive/SharePoint.")
    print("PIPELINE_TOKEN in that file is the value the Deluge helper needs.")


if __name__ == "__main__":
    main()
