"""Minimal Zoho CRM v8 client on the standard library only (no requests), so the
Catalyst bundle has no binary dependencies.

Every write checks the per-record status in the response and raises. Deluge's
zoho.crm.updateRecord silently swallowed a single bad field and dropped the
whole update (ingest-section-scoping-2026-09-16); this client never does that.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

from .config import Config


class CrmError(RuntimeError):
    pass


class Crm:
    def __init__(self, cfg: Config, timeout: int = 60):
        self.cfg = cfg
        self.timeout = timeout
        self._token: str | None = None
        self._token_exp = 0.0
        self._fields: dict[str, list[dict]] = {}

    # ---------- auth ----------
    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        data = urllib.parse.urlencode({
            "refresh_token": self.cfg.refresh_token,
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(self.cfg.accounts_url + "/oauth/v2/token", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = json.loads(r.read().decode())
        if "access_token" not in body:
            raise CrmError("Zoho token refresh failed: " + json.dumps(body))
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    # ---------- transport ----------
    def request(self, method: str, path: str, params: dict | None = None, body: Any = None,
                raw: bool = False, retries: int = 3):
        url = self.cfg.api_domain + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Authorization": "Zoho-oauthtoken " + self.token()}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    content = r.read()
                    if raw:
                        return content, dict(r.headers)
                    if r.status == 204 or not content:
                        return {}
                    return json.loads(content.decode())
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code == 204:
                    return {}
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                if e.code == 401 and attempt < retries - 1:
                    self._token = None
                    headers["Authorization"] = "Zoho-oauthtoken " + self.token()
                    continue
                raise CrmError(f"{method} {path} -> HTTP {e.code}: {text[:800]}")
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                # URLError only wraps send-side failures; a read timeout arrives as a bare
                # socket.timeout (an OSError, not TimeoutError, on 3.9).
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise CrmError(f"{method} {path} -> {e}")
        raise CrmError(f"{method} {path} failed after {retries} attempts")

    # ---------- metadata ----------
    def fields(self, module: str) -> list[dict]:
        if module not in self._fields:
            r = self.request("GET", "/crm/v8/settings/fields", {"module": module})
            self._fields[module] = r.get("fields", [])
        return self._fields[module]

    def field(self, module: str, api_name: str) -> dict | None:
        for f in self.fields(module):
            if f.get("api_name") == api_name:
                return f
        return None

    def lookup_field_to(self, module: str, target: str) -> str:
        """API name of the lookup on `module` that points at `target` (e.g. the
        Solicitation lookup on Solicitation_Requirements)."""
        for f in self.fields(module):
            if f.get("data_type") == "lookup" and ((f.get("lookup") or {}).get("module") or {}).get("api_name") == target:
                return f["api_name"]
        raise CrmError(
            f"No lookup from {module} to {target} is visible to this API user. If {target} is hidden "
            f"from your tab bar, unhide it - Zoho blocks API access to modules the user has hidden.")

    # ---------- records ----------
    def coql(self, query: str) -> list[dict]:
        """Run a COQL select, paging past the 2,000-row limit. `query` must not carry
        its own limit clause."""
        out: list[dict] = []
        offset = 0
        if " order by " not in query.lower():
            query += " order by id asc"  # stable order, or offset paging can skip/repeat rows
        while True:
            q = f"{query} limit {offset}, 2000"
            r = self.request("POST", "/crm/v8/coql", body={"select_query": q})
            rows = r.get("data", []) if r else []
            out.extend(rows)
            if not (r and r.get("info", {}).get("more_records")):
                return out
            offset += len(rows)

    def get_record(self, module: str, rid: str) -> dict:
        r = self.request("GET", f"/crm/v8/{module}/{rid}")
        data = r.get("data") or []
        if not data:
            raise CrmError(f"{module} record {rid} not found")
        return data[0]

    @staticmethod
    def _check(resp: dict, what: str) -> list[dict]:
        rows = resp.get("data", [])
        bad = [x for x in rows if x.get("status") != "success"]
        if bad:
            raise CrmError(f"{what}: {len(bad)} of {len(rows)} rejected - " + json.dumps(bad[:3])[:1200])
        return rows

    def update(self, module: str, rid: str, fields: dict, trigger: Iterable[str] = ("workflow",)) -> None:
        body = {"data": [dict(fields, id=rid)], "trigger": list(trigger)}
        self._check(self.request("PUT", f"/crm/v8/{module}", body=body), f"update {module} {rid}")

    def create_many(self, module: str, rows: list[dict], trigger: Iterable[str] = ("workflow",)) -> list[str]:
        ids: list[str] = []
        for i in range(0, len(rows), 100):
            chunk = rows[i:i + 100]
            resp = self.request("POST", f"/crm/v8/{module}", body={"data": chunk, "trigger": list(trigger)})
            ids.extend(x["details"]["id"] for x in self._check(resp, f"create {module} rows {i}-{i + len(chunk)}"))
        return ids

    def add_note(self, module: str, rid: str, title: str, content: str) -> None:
        body = {"data": [{"Note_Title": title[:120], "Note_Content": content[:30000],
                          "Parent_Id": {"module": {"api_name": module}, "id": rid}}]}
        self._check(self.request("POST", "/crm/v8/Notes", body=body), "add note")

    # ---------- attachments ----------
    def attachments(self, module: str, rid: str) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            r = self.request("GET", f"/crm/v8/{module}/{rid}/Attachments",
                             {"fields": "id,File_Name,Size,Created_Time", "page": page, "per_page": 200})
            out.extend(r.get("data", []) if r else [])
            if not (r and r.get("info", {}).get("more_records")):
                return out
            page += 1

    def download_attachment(self, module: str, rid: str, aid: str) -> bytes:
        content, _ = self.request("GET", f"/crm/v8/{module}/{rid}/Attachments/{aid}", raw=True)
        return content

    def upload_attachment(self, module: str, rid: str, filename: str, data: bytes) -> None:
        """multipart/form-data POST of one file to the record's Attachments."""
        boundary = "----solpipe" + str(int(time.time() * 1000))
        safe = filename.replace('"', "'")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        url = f"{self.cfg.api_domain}/crm/v8/{module}/{rid}/Attachments"
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Zoho-oauthtoken " + self.token(),
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 120)) as r:
                resp = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise CrmError(f"upload {filename} -> HTTP {e.code}: {e.read().decode(errors='replace')[:600]}")
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise CrmError(f"upload {filename} -> {e}")
        self._check(resp, f"upload {filename}")

    # ---------- variables ----------
    def variable(self, api_name: str) -> str | None:
        r = self.request("GET", f"/crm/v8/settings/variables/{api_name}")
        vs = r.get("variables") or []
        return vs[0].get("value") if vs else None
