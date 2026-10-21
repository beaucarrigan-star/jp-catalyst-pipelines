"""Evaluation points, evaluation process and units-monitored facts, read by rule (no AI).

Used twice: the ingest (briefing.scan) reads whole documents with page numbers, and the
briefing document (briefdoc) re-reads the text already stored on the record, so a record
drafted before these rules existed still gets a points table and unit counts.
"""
from __future__ import annotations

import re

BULLET_LEAD = re.compile(r"^\s*(?:[-*•●▪]\s+)+")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _lines(text: str) -> list[str]:
    """Non-empty lines, bullets removed, runs of spaces collapsed; tabs are kept (they separate table cells)."""
    out = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"[ \u00a0]+", " ", BULLET_LEAD.sub("", ln)).strip(" ")
        if ln.strip():
            out.append(ln.strip())
    return out


# ---------------- points ----------------
NOISE = re.compile(r"^(no\.?|request for (proposals?|quotes?|bids?)\b.*|rfp\s*#?\s*[\w-]+$|title:.*|page \d+( of \d+)?|\d{1,3}|[\d.]+)$", re.I)
HEADER_WORDS = re.compile(r"^(scoring method|weight(\s*\(points?\))?|criteria?|evaluation criteria|points?)\b", re.I)
STOP_TITLE = re.compile(r"^(points[- ]based|reward\b|pass\s*/?\s*fail|see\b|attachment\b|as described|per\b)", re.I)
WEIGHT_TOTAL = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*\(\s*(\d{1,3}(?:\.\d+)?)\s*%\s*(?:of\s*(?:the\s*)?total)?\s*\)", re.I)
POINTS_RX = re.compile(r"(?P<crit>[A-Za-z][A-Za-z0-9 ,&/'()\-]{3,80}?)\s*[:\-–—(]*\s*(?P<n>\d{1,3}(?:\.\d+)?)\s*(?P<u>points?|pts\.?|%|percent)", re.I)
ROW_RX = re.compile(r"^(?P<crit>[A-Za-z][^\t|]{3,80}?)\s*(?:\t|\|| {2,})\s*(?P<n>\d{1,3}(?:\.\d+)?)\s*$")
PROSE = re.compile(r"\b(will|may|shall|should|must|up to|maximum|minimum|combined|earn|total|elevated|scoring at least)\b", re.I)
GROUP_RX = re.compile(r"^level\s*\d\b.*", re.I)
LEAD_NUM = re.compile(r"^\s*(?:\d+[.)]|[A-Z][.)])\s+")


def _title(parts: list[str]) -> str:
    t = _clean(" ".join(parts)).strip(" ,;:-")
    t = LEAD_NUM.sub("", t)
    return t[:120]


def _merge_weight_lines(lines: list[str]) -> list[str]:
    """'40' on one line and '(50% of Total)' on the next become one line."""
    out, k = [], 0
    while k < len(lines):
        ln = lines[k]
        nxt = lines[k + 1] if k + 1 < len(lines) else ""
        if re.fullmatch(r"\d{1,3}(?:\.\d+)?", ln) and re.fullmatch(r"\(\s*\d{1,3}(?:\.\d+)?\s*%\s*(?:of\s*(?:the\s*)?total)?\s*\)", nxt, re.I):
            out.append(f"{ln} {nxt}")
            k += 2
            continue
        out.append(ln)
        k += 1
    return out


def points_table(text: str) -> list[dict]:
    """[{crit, n, unit ('points'|'%'), share (percent of total or None), group}] in document order.
    group is the 'LEVEL n - ...' heading the criterion sits under, if any."""
    out, seen = [], set()
    group = ""

    def add(crit, n, unit, share=None, grp=None):
        g = group if grp is None else grp
        key = (crit.lower(), n, g)
        words = crit.split()
        if len(crit) < 3 or key in seen or len(words) > 10 or PROSE.search(crit):
            return
        seen.add(key)
        out.append({"crit": crit, "n": float(n), "unit": unit, "share": share, "group": g})

    title_parts: list[str] = []
    closed = False
    open_parens = 0
    pending = None          # a closed title whose weight has not appeared yet (pdf text order can put it later)
    for ln in _merge_weight_lines(_lines(text)):
        if ln.lower().startswith("published points"):
            continue    # our own summary line on a stored section; the real table is read below it
        cite = re.search(r"\.(pdf|docx?|xlsx?|zip|txt)\b", ln, re.I)
        if cite:            # "(file, p.N)" ends a heading line: a section starts here
            head = _clean(re.sub(r"\s*\([^)]*\.(pdf|docx?|xlsx?|zip|txt)[^)]*\)\s*$", "", ln))
            if GROUP_RX.match(head) and len(head) <= 90:
                if title_parts and closed:
                    pending = (_title(title_parts), group)
                group = head
            title_parts, closed, open_parens = [], False, 0
            continue
        if NOISE.match(ln):
            continue
        if GROUP_RX.match(ln) and len(ln) <= 90 and not re.search(r"[a-z]{3,} (will|may|shall)\b", ln):
            if title_parts and closed:
                pending = (_title(title_parts), group)
            group = _clean(ln)
            title_parts, closed, open_parens = [], False, 0
            continue
        if open_parens > 0:   # the "(§8 Proposal Format ... Question 3 & 4)" note that trails a weight
            open_parens += ln.count("(") - ln.count(")")
            continue
        if open_parens == 0 and ln.startswith("(") and ln.count("(") > ln.count(")") and not WEIGHT_TOTAL.search(ln):
            open_parens = ln.count("(") - ln.count(")")     # a note in parentheses that wraps over lines
            continue
        ln_h = re.sub(r"^(evaluation criteria|scoring method|weight\s*\(points?\)|weight|criteria)\s*", "", ln, flags=re.I).strip()
        m = WEIGHT_TOTAL.search(ln)
        if m:
            crit = _title(title_parts)
            grp = None
            if not crit and pending:
                crit, grp = pending
            pending = None if crit else pending
            if crit:
                add(crit, m.group(1), "points", float(m.group(2)), grp)
            title_parts, closed = [], False
            tail = ln[m.end():]
            open_parens = max(0, tail.count("(") - tail.count(")"))
            continue
        for pm in POINTS_RX.finditer(ln):
            crit = _clean(pm.group("crit")).strip("-:• ")
            unit = "%" if pm.group("u").lower() in ("%", "percent") else "points"
            add(crit, pm.group("n"), unit)
        rm = ROW_RX.match(ln)
        if rm and float(rm.group("n")) <= 100:
            add(_clean(rm.group("crit")), rm.group("n"), "points")
        if HEADER_WORDS.match(ln) and not ln_h:
            continue
        if STOP_TITLE.match(ln_h):
            closed = True
            continue
        if ln_h and not closed and len(ln_h) <= 80:
            title_parts.append(ln_h)
    return out


def points_strings(rows: list[dict]) -> list[str]:
    out = []
    for r in rows:
        n = int(r["n"]) if r["n"] == int(r["n"]) else r["n"]
        s = f"{r['crit']} - {n}{'%' if r['unit'] == '%' else ' points'}"
        if r.get("share") is not None:
            s += f" ({r['share']:g}% of total)"
        out.append(s)
    return out


# ---------------- evaluation process ----------------
PROCESS_STRONG = re.compile(
    r"evaluation committee|will be evaluated|evaluated (on|by|according)|evaluation process|selection levels?|\blevel \d\b|elevat"
    r"|\bup to \d+ points|\d+ points|scor(e|ed|es|ing)\b|minimum (score|points?|threshold)|at least \d+ points|threshold"
    r"|best evaluated|lowest and best|best and final|\bBAFO\b|negotiat|short-?list|oral presentation|presentation|interview"
    r"|cost will be calculated|disqualif|clarification|pass\s*/?\s*fail|relative importance|basis (of|for) award|recommend(ation)? (for|of) award", re.I)
PROCESS_WEAK = re.compile(r"evaluat|\bpanel\b|demonstrat|responsive|ranked?\b|ranking|site visit|reference check", re.I)
SENT_SPLIT = re.compile(r"(?<=[.;:])\s+(?=[A-Z\u2022])")


def _tocish(s: str) -> bool:
    words = s.split()
    caps = sum(1 for w in words if w.isupper() and len(w) > 2)
    return bool(re.search(r"table of contents|\.{4,}", s, re.I)) or (len(words) > 8 and caps / len(words) > 0.3)


def find_process(units: list[tuple[str | None, str]], label: str, limit: int = 22) -> str:
    """units: [(page|None, text)]. Bullets of sentences that describe how proposals are judged,
    strongest first picked, then shown in document order."""
    cands, seen = [], set()
    if re.search(r"(^|[^a-z])(w-?9|ciq|legal[ _-]?notice)", label or "", re.I):
        return ""      # tax / disclosure forms do not describe how proposals are judged
    for order, (pg, text) in enumerate(units):
        flat = _clean(" ".join(_lines(text)))
        for k, sent in enumerate(SENT_SPLIT.split(flat)):
            s = _clean(sent)
            s = re.sub(r"^[A-Z][A-Z /&-]{3,}\s+(?=[A-Z][a-z])", "", s)   # a glued all-caps heading
            s = re.sub(r"^(plus\s+)?install fees?[^.]*?install fee\s+", "", s, flags=re.I)   # a pricing-formula line glued on
            if re.search(r"following (criteria|sections?)\.?$", s, re.I):
                continue
            if not (60 <= len(s) <= 450) or _tocish(s) or re.match(r"^(january|february|march|april|may|june|july|august|september|october|november|december)\b", s, re.I):
                continue
            score = 3 if PROCESS_STRONG.search(s) else 1 if PROCESS_WEAK.search(s) else 0
            if score == 3 and re.search(r"\d+ points|level \d|best and final|references?|at least \d+", s, re.I):
                score = 4
            if not score:
                continue
            key = re.sub(r"\W+", "", s.lower())[:80]
            if key in seen:
                continue
            seen.add(key)
            cands.append((score, order, k, f"• {s} ({label}, p.{pg})" if pg else f"• {s} ({label})"))
    strong = [c for c in cands if c[0] >= 3]
    pool = strong if len(strong) >= 3 else cands
    pick = sorted(pool, key=lambda c: -c[0])[:limit]
    return "\n".join(c[3] for c in sorted(pick, key=lambda c: (c[1], c[2])))


# ---------------- units monitored ----------------
EM_CONTEXT = re.compile(r"monitor|supervis|\bGPS\b|electronic|offender|participant|defendant|device|\bunits?\b|\bADP\b|"
                        r"population|caseload|probation|parole|pre-?trial|alcohol|SCRAM|radio.?frequency|\bRF\b|ankle|bracelet|transmitter|\bEM\b", re.I)
UNIT_NOUN = (r"installations?|removals?|offenders?|participants?|defendants?|clients?|probationers?|parolees?|releasees?|supervisees?|individuals?|"
             r"persons?|juveniles?|youth|detainees?|residents?|cases|units?|devices?|monitors?|transmitters?|bracelets?|"
             r"trackers?|beacons?|licenses?|active (?:units?|devices?)|per day|daily|ADP")
NUM = r"(?P<n>\d{1,3}(?:,\d{3})+|\d{2,6})"
VOL_AFTER = re.compile(NUM + r"\s*\+?\s*(?:or more\s+)?(?:[A-Za-z/&-]+\s+){0,3}?(?P<noun>" + UNIT_NOUN + r")\b", re.I)
VOL_ADP = re.compile(r"(?:average daily population|\bADP\b|daily (?:population|caseload)|average caseload)[^0-9$]{0,40}" + NUM, re.I)
BAD_AFTER = re.compile(r"^\s*(?:pages?|days?|months?|years?|hours?|minutes?|copies|%|percent|points?|pts|mb|mbps|ft|feet|miles|lbs|pounds|dollars)\b", re.I)
KIND = [
    (r"average daily population|\bADP\b|daily (population|caseload)|average caseload", "average daily population"),
    (r"up to|maximum|no more than|not to exceed|capacity|at any (one )?time|concurrent", "maximum / capacity"),
    (r"annual|per year|each year|yearly|last year|prior year|fiscal", "annual"),
    (r"install fees?|installations?", "installations"),
    (r"current|currently|existing|presently|incumbent|historical|approximately|estimated|about|average", "current / estimated"),
]


def _kind(line: str) -> str:
    for rx, name in KIND:
        if re.search(rx, line, re.I):
            return name
    return "stated"


def find_volumes(lines: list[tuple[str | None, str]], label: str, limit: int = 12) -> list[dict]:
    """[{n, noun, kind, text, page, label}] unit and population counts stated beside monitoring words."""
    out, seen = [], set()
    ctx = ""
    for pg, raw in lines:
        ln = _clean(BULLET_LEAD.sub("", raw))
        if re.match(r"^[A-Z][A-Za-z/&,' -]{2,50}:$", ln):
            ctx = ln.rstrip(":")
            continue
        if len(ln) < 12 or len(ln) > 600 or "...." in ln or not EM_CONTEXT.search(ln):
            continue
        hits = []
        for m in VOL_ADP.finditer(ln):
            hits.append((m.group("n"), "ADP"))
        for m in VOL_AFTER.finditer(ln):
            tail = ln[m.end():]
            before = ln[max(0, m.start() - 2):m.start()]
            if BAD_AFTER.match(tail) or "$" in before:
                continue
            n = int(m.group("n").replace(",", ""))
            if 1900 <= n <= 2100 and "," not in m.group("n"):
                continue
            if m.start() == 0 and n < 1000:
                continue   # a list or spreadsheet row number, not a count
            if re.search(r"(FY|fiscal years?)\s*\d*\s*(&|and)?\s*$", ln[:m.start()], re.I):
                continue
            hits.append((m.group("n"), m.group("noun").lower()))
        nouns_by_n = {}
        for n_raw, noun in hits:
            nouns_by_n.setdefault(n_raw, set()).add(noun)
        hits = [(n_raw, noun) for n_raw, noun in hits if not (noun == "ADP" and len(nouns_by_n[n_raw]) > 1)]
        for n_raw, noun in hits:
            n = int(n_raw.replace(",", ""))
            if n < 10:
                continue
            key = (n, noun)
            if key in seen:
                continue
            seen.add(key)
            out.append({"n": n, "noun": noun, "kind": _kind(ln), "text": ln[:240], "page": pg, "label": label, "ctx": ctx})
            if len(out) >= limit:
                return out
    return out


def volume_lines(vols: list[dict]) -> str:
    out = []
    for v in vols:
        where = f"{v['label']}" + (f", p.{v['page']}" if v.get("page") else "")
        head = f"{v['ctx']}: " if v.get("ctx") else ""
        out.append(f"• {head}{v['n']:,} {v['noun']} ({v['kind']}): \"{v['text']}\" ({where})")
    return "\n".join(out)


# ---------------- stored "Published points" summary ----------------
def format_published(rows: list[dict]) -> str:
    """Grouped summary written to the top of Evaluation_Criteria; parse_published reads it back."""
    groups: list[str] = []
    for r in rows:
        if r.get("group", "") not in groups:
            groups.append(r.get("group", ""))
    lines = []
    for g in groups:
        part = [r for r in rows if r.get("group", "") == g]
        head = f"Published points [{g}]: " if g else "Published points: "
        lines.append(head + "; ".join(points_strings(part)))
    return "\n".join(lines)


PUB_LINE = re.compile(r"^Published points(?: \[(?P<g>.+?)\])?:\s*(?P<body>.+)$")
PUB_ITEM = re.compile(r"^(?P<crit>.+) - (?P<n>\d{1,3}(?:\.\d+)?)(?P<u> points|%)(?: \((?P<s>\d{1,3}(?:\.\d+)?)% of total\))?$")


def parse_published(text: str) -> list[dict]:
    rows = []
    for ln in (text or "").splitlines():
        m = PUB_LINE.match(ln.strip())
        if not m:
            continue
        for item in m.group("body").split("; "):
            im = PUB_ITEM.match(item.strip())
            if im:
                rows.append({"crit": im.group("crit"), "n": float(im.group("n")),
                             "unit": "%" if im.group("u") == "%" else "points",
                             "share": float(im.group("s")) if im.group("s") else None,
                             "group": m.group("g") or ""})
    return rows
