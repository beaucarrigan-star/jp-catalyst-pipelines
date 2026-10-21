"""Rule-based solicitation briefing. No AI, no network (Beau, Oct 15 2026).

Reads the same documents the requirement ingest reads and drafts the briefing that the
RFP team and executives look at on the Solicitation record:

  Scope_of_Work            sections headed scope of work / statement of work / services requested
  Submission_Requirements  proposal format, instructions to offerors, how and where to submit
  Evaluation_Criteria      evaluation / selection criteria sections, plus "<criterion> - 30 points" lines
  Key Requirements Summary mandatory counts by category and the first lines of the key categories,
                           built from the Solicitation_Requirements rows (not from the documents)
  Key dates                release, questions, pre-proposal conference, proposal due, award, start
  Stated_Budget            only when the packet names exactly one dollar amount beside a budget word
  Service_Lines            which service lines the packet asks for, with Service_Type_Summary quoting the evidence
                           and the duties the contractor is asked to perform (solpipe/services.py)
  Solicitation_Type        "Renewal Notice" when the document is a contract renewal / option / extension notice;
                           its contract number, renewal term, current term end, pricing and changes go in a
                           RENEWAL DETAILS block at the top of Scope_of_Work

Rules that keep people's work safe:
  * a field that already has a value is never overwritten; only empty fields are drafted
  * every drafted line cites its file and page; dates and budget cite the matched text
  * a date with no time of day is filled at 12:00 and flagged; no time zone stated means Central, flagged
  * Key_Dates_Verified stays False until a person ticks it
"""
from __future__ import annotations

import re
from datetime import datetime

from . import evalfacts, services

ENGINE = "briefing-2026-10-16.1"

SECTION_FIELDS = {
    "Scope_of_Work": (
        r"scope of (work|services?)|statement of work|services? (to be provided|requested|required)"
        r"|project (description|overview)|description of (services|work)|background and scope", 6000),
    "Submission_Requirements": (
        r"(proposal|bid|response|quote)s? (submission|format|content|instructions|preparation|requirements)"
        r"|submission (requirements|instructions|of (proposals?|bids?|responses?))"
        r"|instructions to (offerors?|proposers?|bidders?|vendors?)|how to submit"
        r"|delivery of (proposals?|bids?)|proposal requirements", 6000),
    "Evaluation_Criteria": (
        r"evaluation (criteria|factors|process|of (proposals?|bids?|responses?))|selection criteria|^\W*(\d+\.?\s*)?evaluation\W*$|^level\s*\d\b.*(assessment|evaluation|scoring|proposal)|detailed proposal assessment"
        r"|basis (of|for) award|award criteria|scoring (criteria|matrix)", 6000),
}

NUMBERED = re.compile(r"^\s*((?:\d+\.)+\d*|\d+\)|Section\s+\d+(?:\.\d+)*|ARTICLE\s+[IVX\d]+|[A-Z]\.)\s+(\S.*)$", re.I)
BULLET = re.compile(r"^\s*([-*•●▪]|\(?[a-z0-9]{1,3}[.)])\s+")
LEADER = re.compile(r"\.{4,}|…{2,}")  # table-of-contents lines

MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?"
         r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
DATE_RE = re.compile(r"(?P<mon>%s)\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})|"
                     r"(?P<m2>\d{1,2})/(?P<d2>\d{1,2})/(?P<y2>\d{4}|\d{2})" % MONTH, re.I)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?", re.I)
NOON_RE = re.compile(r"\bnoon\b", re.I)
TZ_RE = re.compile(r"\b(E[SD]?T|C[SD]?T|M[SD]?T|P[SD]?T|Eastern|Central|Mountain|Pacific)\b")
TZ_NAMES = {"e": "America/New_York", "c": "America/Chicago", "m": "America/Denver", "p": "America/Los_Angeles"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# first match wins per line, in this order, so a questions deadline is not read as the proposal deadline
DATE_RULES = [
    ("Questions_Round_1_Due", r"(deadline|last day|due date)( for| to submit)? (written )?(questions|inquiries)"
                              r"|questions? (are |is )?due|written (questions|inquiries)|submit(ting)? questions"),
    ("Pre_Proposal_Conference", r"pre[- ]?(proposal|bid|submittal) (conference|meeting)|mandatory (pre|site)"
                                r"|bidders?'? conference|vendor conference|site visit"),
    ("Release_Date", r"(issue|issued|release|released|publication|posting) date|date (issued|released)|rfp issued"),
    ("Anticipated_Award", r"(anticipated|estimated|expected|intended|projected) (contract )?award"
                          r"|award (date|notification)|notice of (intent to )?award"),
    ("Anticipated_Start", r"(anticipated|estimated|expected|projected) (contract |service )?(start|commencement|effective|go[- ]live)"
                          r"|contract (start|begin)|services? (to )?(begin|commence)"),
    ("Proposal_Due", r"(proposal|bid|response|submission|quote)s?( are| is)? due|due date|submission deadline|closing (date|time)"
                     r"|deadline (for|to) (submit|submission|receipt)|(proposals?|bids?) must be (received|submitted)|opening date"),
    # contract renewal notices: the date the agency needs our answer is the response deadline
    ("Proposal_Due", r"(renew(al)?|option|extension)[^.]{0,80}(deadline|due|no later than|must be (received|returned|exercised|accepted|submitted))"
                     r"|(accept(ance)?|response|reply|return)[^.]{0,40}(deadline|no later than|due)|please (respond|reply|return)[^.]{0,40}by"),
    ("Anticipated_Start", r"renewal (term|period) (will )?(begin|commence|start)s?|(renewal|extension) (is )?effective|renewed term (begin|start)s?"),
]
DATE_ONLY = {"Release_Date", "Anticipated_Award", "Anticipated_Start"}
DATETIME_FIELDS = {"Questions_Round_1_Due", "Pre_Proposal_Conference", "Proposal_Due"}

BUDGET_LABEL = re.compile(r"budget|not[- ]to[- ]exceed|funding (available|amount)|available funds|"
                          r"estimated (annual |total |contract )?(value|cost|amount|budget)|maximum (contract|award|amount)|ceiling", re.I)
AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?\s*(million|thousand|[MK]\b)?", re.I)
POINTS = re.compile(r"(?P<crit>[A-Za-z][A-Za-z0-9 ,&/'()\-]{3,80}?)\s*[:\-–—(]*\s*(?P<n>\d{1,3})\s*(?P<u>points?|pts\.?|%|percent)", re.I)


# ---------------- helpers ----------------

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def units_of(doc: dict) -> list[tuple[str | None, str]]:
    """[(page number or None, text)] for a document from extract.to_document."""
    if doc.get("kind") == "pdf":
        return [(str(i), p) for i, p in enumerate(doc["pages"], 1)]
    if doc.get("kind") == "xlsx":
        return [(None, "\n".join("\t".join(c for c in cells if c) for _, cells in grid)) for _, grid in doc["sheets"]]
    return [(None, doc.get("text", ""))]


def _depth(line: str) -> int | None:
    m = NUMBERED.match(line)
    if not m:
        return None
    ref = m.group(1)
    return len([p for p in re.split(r"\.", ref.rstrip(".)")) if p.isdigit()]) or 1


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 100 or LEADER.search(s):
        return False
    m = NUMBERED.match(s)
    body = m.group(2) if m else s
    if len(body) < 3:
        return False
    if body.endswith((".", ",", ";")) and not body.isupper():
        return False
    words = body.split()
    if m:
        return len(words) <= 12
    if body.isupper() and len(words) <= 12:
        return True
    caps = sum(1 for w in words if w[:1].isupper())
    return len(words) <= 8 and caps >= max(1, int(len(words) * 0.7)) and not BULLET.match(s)


def _paragraphs(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        starts_new = bool(NUMBERED.match(s) or BULLET.match(s))
        if out and not starts_new and not out[-1].rstrip().endswith((".", ":", ";", "?")) and not s[:1].isupper():
            out[-1] += " " + s
        else:
            out.append(s)
    return out


def _bullets(paras: list[str], budget: int) -> str:
    out, used = [], 0
    for p in paras:
        p = _clean(p)
        p = p if len(p) <= 500 else p[:497] + "..."
        if used + len(p) + 3 > budget:
            out.append("• (more in the document)")
            break
        out.append("• " + p)
        used += len(p) + 3
    return "\n".join(out)


def _lines_with_pages(doc: dict) -> list[tuple[str | None, str]]:
    return [(pg, ln) for pg, text in units_of(doc) for ln in text.splitlines()]


# ---------------- sections ----------------

MULTI = {"Evaluation_Criteria"}   # evaluation text often spans several headed sections (levels, tables)


def find_sections(lines: list[tuple[str | None, str]], label: str) -> dict[str, str]:
    """{field: text} for the first section in the document that matches each rule
    (every matching section for the fields in MULTI)."""
    found: dict[str, str] = {}
    compiled = {f: (re.compile(p, re.I), cap) for f, (p, cap) in SECTION_FIELDS.items()}
    i, n = 0, len(lines)
    while i < n:
        pg, ln = lines[i]
        jump = i + 1
        if _is_heading(ln):
            for field, (rx, cap) in compiled.items():
                if (field in found and field not in MULTI) or not rx.search(ln):
                    continue
                d0 = _depth(ln)
                body: list[str] = []
                j = i + 1
                while j < n:
                    pj, lj = lines[j]
                    if _is_heading(lj):
                        dj = _depth(lj)
                        if d0 is None:
                            if dj is None and lj.strip().isupper():
                                break
                            if dj is not None and dj <= 1:
                                break
                        elif dj is not None and dj <= d0:
                            break
                        elif dj is None and lj.strip().isupper():
                            break
                    if not LEADER.search(lj):
                        body.append(lj)
                    if sum(len(b) for b in body) > cap * 3:
                        break
                    j += 1
                paras = _paragraphs(body)
                if sum(len(p) for p in paras) >= 40:   # skips table-of-contents and empty headings
                    where = f"{label}, p.{pg}" if pg else label
                    text = f"{_clean(ln)} ({where})\n" + _bullets(paras, cap)
                    found[field] = (found[field] + "\n\n" + text)[:30000] if field in found else text
                    if field in MULTI:
                        jump = max(jump, j)
        i = jump
    return found


def find_points(section_text: str) -> list[str]:
    return evalfacts.points_strings(evalfacts.points_table(section_text))


# ---------------- dates and budget ----------------

def _parse_date(m: re.Match) -> datetime | None:
    try:
        if m.group("mon"):
            mo, d, y = MONTHS[m.group("mon")[:3].lower()], int(m.group("d")), int(m.group("y"))
        else:
            mo, d, y = int(m.group("m2")), int(m.group("d2")), int(m.group("y2"))
            y += 2000 if y < 100 else 0
        if not 2000 <= y <= 2100:
            return None
        return datetime(y, mo, d)
    except (ValueError, KeyError):
        return None


def _time_in(text: str) -> tuple[int, int] | None:
    if NOON_RE.search(text):
        return 12, 0
    m = TIME_RE.search(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (1 <= h <= 12 and mi < 60):
        return None
    if m.group(3).lower() == "p" and h != 12:
        h += 12
    if m.group(3).lower() == "a" and h == 12:
        h = 0
    return h, mi


def find_dates(lines: list[tuple[str | None, str]], label: str) -> dict[str, dict]:
    """First date found beside each label, looking at the label's line and the next line."""
    rules = [(f, re.compile(p, re.I)) for f, p in DATE_RULES]
    found: dict[str, dict] = {}
    for i, (pg, ln) in enumerate(lines):
        if LEADER.search(ln) or not ln.strip():
            continue
        for field, rx in rules:
            if field in found or not rx.search(ln):
                continue
            window = ln + " " + (lines[i + 1][1] if i + 1 < len(lines) else "")
            dm = next((x for x in DATE_RE.finditer(window) if _parse_date(x)), None)
            if not dm:
                continue
            tm = _time_in(window[dm.end():dm.end() + 60]) or _time_in(window)
            tz = TZ_RE.search(window[dm.end():dm.end() + 60])
            found[field] = {"date": _parse_date(dm), "time": tm, "tz": tz.group(1) if tz else None,
                            "text": _clean(window)[:200], "label": label, "page": pg}
            break  # a line is one kind of date
    return found


def _tz_offset(when: datetime, tzword: str | None) -> tuple[str, str] | None:
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None
    key = (tzword or "c")[0].lower()
    try:
        off = ZoneInfo(TZ_NAMES[key]).utcoffset(when)
    except Exception:  # noqa: BLE001 - zone data missing on this host
        return None
    secs = int(off.total_seconds())
    sign = "+" if secs >= 0 else "-"
    secs = abs(secs)
    return f"{sign}{secs // 3600:02d}:{secs % 3600 // 60:02d}", (tzword or "Central")


def find_budget(lines: list[tuple[str | None, str]], label: str) -> list[dict]:
    out = []
    for pg, ln in lines:
        if not BUDGET_LABEL.search(ln) or LEADER.search(ln):
            continue
        for m in AMOUNT.finditer(ln):
            val = float(m.group(1).replace(",", "") + (m.group(2) or ""))
            unit = (m.group(3) or "").lower()
            val *= 1_000_000 if unit in ("million", "m") else 1000 if unit in ("thousand", "k") else 1
            out.append({"amount": val, "text": _clean(ln)[:200], "label": label, "page": pg})
    return out


# ---------------- per document, merge, apply ----------------

def _raw_points(doc: dict) -> list[dict]:
    """Points tables read from the document's own lines (the stored section text loses line breaks):
    from the first page that has an evaluation heading to four pages later."""
    rx = re.compile(SECTION_FIELDS["Evaluation_Criteria"][0], re.I)
    units = units_of(doc)
    for k, (pg, text) in enumerate(units):
        if not any(_is_heading(ln) and rx.search(ln) and not LEADER.search(ln) for ln in text.splitlines()):
            continue
        rows = evalfacts.points_table("\n".join(t for _, t in units[k:k + 5]))
        if rows:      # the first heading page that actually leads to a points table (skips the contents page)
            return rows
    return []


def scan(label: str, doc: dict) -> dict:
    lines = _lines_with_pages(doc)
    secs = find_sections(lines, label)
    return {"sections": secs, "dates": find_dates(lines, label), "budget": find_budget(lines, label),
            "points": _raw_points(doc),
            "volumes": evalfacts.find_volumes(lines, label),
            "process": evalfacts.find_process(units_of(doc), label),
            "services": services.find_services(lines, label),
            "renewal": services.scan_renewal(lines, label)}


def new_findings() -> dict:
    return {"sections": {}, "dates": {}, "budget": [], "volumes": [], "process": [], "points": [],
            "services": services.new_services(), "renewal": services.new_renewal()}


def merge(acc: dict, one: dict) -> dict:
    for f, t in one["sections"].items():
        acc["sections"].setdefault(f, []).append(t)
    for f, d in one["dates"].items():
        acc["dates"].setdefault(f, d)          # first document and first hit win
    acc["budget"].extend(one["budget"])
    acc["volumes"].extend(one.get("volumes", []))
    acc["points"].extend(one.get("points", []))
    if one.get("process"):
        acc["process"].append(one["process"])
    if one.get("services"):
        services.merge_services(acc["services"], one["services"])
    if one.get("renewal"):
        services.merge_renewal(acc["renewal"], one["renewal"])
    return acc


def key_requirements_summary(rows: list[dict]) -> str:
    """rows: Solicitation_Requirements dicts (Requirement_Text, Mandatory, Category, Source_Reference, Page_PDF)."""
    if not rows:
        return ""
    mand = [r for r in rows if r.get("Mandatory")]
    cats: dict[str, int] = {}
    for r in mand:
        cats[r.get("Category") or "Other"] = cats.get(r.get("Category") or "Other", 0) + 1
    head = f"{len(mand)} mandatory of {len(rows)} requirements read. "
    head += "Mandatory by category: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])) + "."
    focus = ["Insurance", "Bonding", "Licensure", "Staffing", "Experience / Refs", "Security / Encryption", "Legal / Terms"]
    out, used = [head], len(head)
    for cat in focus:
        picks = [r for r in mand if r.get("Category") == cat][:3]
        for r in picks:
            ref = ", ".join(x for x in (r.get("Source_Reference"), f"p.{r['Page_PDF']}" if r.get("Page_PDF") else None) if x)
            line = f"• [{cat}] {_clean(r.get('Requirement_Text'))[:300]}" + (f" ({ref})" if ref else "")
            if used + len(line) > 6000:
                return "\n".join(out + ["• (more in the Requirements list)"])
            out.append(line)
            used += len(line)
    return "\n".join(out)


def _empty(v) -> bool:
    return v in (None, "", [], "-None-")


def fields_for(sol: dict, acc: dict, req_rows: list[dict]) -> tuple[dict, list[str]]:
    """(updates for Solicitations, notes). Only empty fields are drafted."""
    upd: dict = {}
    notes: list[str] = []
    for field, parts in acc["sections"].items():
        if _empty(sol.get(field)):
            upd[field] = ("\n\n".join(parts))[:31000]
    if "Evaluation_Criteria" in upd:
        rows = acc.get("points") or []
        if len(rows) < 2:
            rows = evalfacts.points_table(upd["Evaluation_Criteria"])
        if len(rows) >= 2:
            upd["Evaluation_Criteria"] = (evalfacts.format_published(rows) + "\n\n" + upd["Evaluation_Criteria"])[:49000]
            if not sol.get("Eval_Weights_Published"):
                upd["Eval_Weights_Published"] = True
    if _empty(sol.get("Evaluation_Process")) and acc.get("process"):
        upd["Evaluation_Process"] = ("\n".join(acc["process"]))[:31000]
    if _empty(sol.get("Units_Monitored")) and acc.get("volumes"):
        seen, vols = set(), []
        for v in acc["volumes"]:
            if (v["n"], v["noun"]) not in seen:
                seen.add((v["n"], v["noun"]))
                vols.append(v)
        upd["Units_Monitored"] = evalfacts.volume_lines(vols[:12])[:1990]
    if _empty(sol.get("Key_Requirements_Summary")):
        s = key_requirements_summary(req_rows)
        if s:
            upd["Key_Requirements_Summary"] = s
    svc = acc.get("services") or services.new_services()
    if _empty(sol.get("Service_Lines")) and services.service_line_values(svc):
        upd["Service_Lines"] = services.service_line_values(svc)
    if _empty(sol.get("Service_Type_Summary")):
        s = services.services_summary(svc)
        if s:
            upd["Service_Type_Summary"] = s
    ren = acc.get("renewal") or services.new_renewal()
    if ren["notice"]:
        cur = sol.get("Solicitation_Type")
        if _empty(cur):
            upd["Solicitation_Type"] = "Renewal Notice"
        elif cur != "Renewal Notice":
            notes.append(f"This looks like a contract renewal notice but Solicitation Type is {cur}; not changed.")
        block = services.renewal_block(ren)
        if _empty(sol.get("Scope_of_Work")):
            upd["Scope_of_Work"] = (block + ("\n\n" + upd["Scope_of_Work"] if "Scope_of_Work" in upd else ""))[:31000]
        else:
            notes.append("Renewal details found but Scope of Work already has text, so they were not written: "
                         + block.replace("\n", " | ")[:700])
    sources: list[str] = []
    for field, d in acc["dates"].items():
        if not _empty(sol.get(field)):
            continue
        when, where = d["date"], f"{d['label']}" + (f", p.{d['page']}" if d["page"] else "")
        if field in DATE_ONLY:
            upd[field] = when.strftime("%Y-%m-%d")
            sources.append(f"{field}: \"{d['text']}\" ({where})")
            continue
        t = d["time"] or (12, 0)
        tz = _tz_offset(when, d["tz"])
        if tz is None:
            notes.append(f"{field}: found \"{d['text']}\" ({where}) but no time zone data on this host; enter it by hand")
            continue
        upd[field] = f"{when.strftime('%Y-%m-%d')}T{t[0]:02d}:{t[1]:02d}:00{tz[0]}"
        flags = []
        if not d["time"]:
            flags.append("no time of day found, 12:00 PM placeholder")
        if not d["tz"]:
            flags.append("no time zone stated, read as Central")
        sources.append(f"{field}: \"{d['text']}\" ({where})" + (f" [{'; '.join(flags)}]" if flags else ""))
        if field == "Proposal_Due" and _empty(sol.get("Deadline_Timezone_Note")) and d["tz"]:
            upd["Deadline_Timezone_Note"] = f"Packet says {d['tz']}"
    if _empty(sol.get("Stated_Budget")) and acc["budget"]:
        amounts = {b["amount"] for b in acc["budget"]}
        b0 = acc["budget"][0]
        where = f"{b0['label']}" + (f", p.{b0['page']}" if b0["page"] else "")
        if len(amounts) == 1:
            upd["Stated_Budget"] = b0["amount"]
            upd["Budget_Disclosed"] = True
            sources.append(f"Stated_Budget: \"{b0['text']}\" ({where})")
        else:
            cand = "; ".join(f"${a:,.0f}" for a in sorted(amounts)[:6])
            notes.append(f"Several dollar amounts beside budget words ({cand}); first: \"{b0['text']}\" ({where}). "
                         "Stated Budget left blank for a person to choose.")
    if sources and _empty(sol.get("Header_Sources")):
        upd["Header_Sources"] = ("\n".join(sources))[:1990]
    elif sources:
        notes.append("Header Sources already filled, not changed. Drafted: " + " | ".join(sources))
    if upd and sol.get("Briefing_Status") in (None, "", "-None-", "Not started"):
        upd["Briefing_Status"] = "Drafted from packet"
    return upd, notes
