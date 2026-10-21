"""Rule-based requirement extraction for solicitations. No AI, no network.

Replaces the earlier model-based ingest. Produces DRAFT Solicitation_Requirements rows that a person verifies.
  - text PDFs: pdfminer.six text layer per page (optional dependency; no OCR)
  - pre-extracted text: pass a list of page strings
  - tables (Form K-style XLSX rows): find_table() + map_table_rows()
Scanned PDFs yield little or no text -> result flagged needs_manual, never shown as complete.
"""
import io
import re

ENGINE = "rule-ingest-2026-09-25.2"

MANDATORY = re.compile(r"\b(shall|must|is required to|are required to|required|mandatory|at a minimum|minimum qualifications?)\b", re.I)
SOFT = re.compile(r"\b(should|preferred|desirable|may be considered)\b", re.I)
NUMBERED = re.compile(r"^\s*((?:\d+\.)+\d*|\d+\)|[A-Z]\.|\([a-z0-9]+\)|[a-z]\))\s+(.*)$")
SENT_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")

CATEGORY_RULES = [
    ("Insurance", r"\binsurance|liability coverage|additional insured|certificate of insurance"),
    ("Bonding", r"\bbond(ing)?\b|surety"),
    ("Licensure", r"\blicen[cs]e|licensure|certif(ied|ication)\b"),
    ("Staffing", r"\bstaff|personnel|key personnel|project manager|background check"),
    ("Experience / Refs", r"\breferences?\b|years of experience|past performance|similar contracts"),
    ("Technology / Device", r"\bdevice|gps|ankle|bracelet|monitoring equipment|battery|tamper|software|platform"),
    ("Security / Encryption", r"\bencrypt|cjis|security|hipaa|data breach"),
    ("Reporting", r"\breport(s|ing)?\b|dashboard"),
    ("Financial / Audit", r"\baudit|financial statements?|invoice|pricing"),
    ("Legal / Terms", r"\bindemnif|terminat|governing law|contract terms|warrant"),
]

# Table header recognition (Form K and similar requirement matrices)
HDR_TEXT = re.compile(r"requirement|specification|\bspec\b|description|criteri|scope of work|deliverable", re.I)
HDR_REF = re.compile(r"^\s*(item|line|ref|reference|req(uirement)?\.?\s*(no|#|id|number)|no\.?|#|id|number|section\s*(no|#)?)\s*\.?\s*$", re.I)
HDR_MAND = re.compile(r"mandatory|required\s*\?|^\s*m\s*/\s*[do]\s*$|requirement type|^\s*type\s*$|must\s*/\s*should", re.I)
HDR_SECTION = re.compile(r"^\s*(section|category|group|area|topic)\s*$", re.I)


def categorize(text):
    for cat, pat in CATEGORY_RULES:
        if re.search(pat, text, re.I):
            return cat
    return None


def _clean(s):
    return re.sub(r"\s+", " ", s).strip()


def extract_pdf_pages(src):
    """Text layer per page via pdfminer.six. src: a path or the PDF bytes.
    Returns None if pdfminer.six is not installed, [] if the PDF has no pages."""
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
    except ImportError:
        return None
    fp = io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src
    pages = []
    for layout in extract_pages(fp):
        pages.append("\n".join(el.get_text() for el in layout if isinstance(el, LTTextContainer)))
    return pages


def parse_pages(pages, min_chars_per_page=40, paged=True):
    """pages: list of page text (index 0 = PDF page 1). paged=False for DOCX/TXT, where there
    are no physical page numbers. Returns {"rows", "needs_manual", "notes"}."""
    total = sum(len(p.strip()) for p in pages)
    if not pages or total < min_chars_per_page * max(1, len(pages)) / 4:
        return {"rows": [], "needs_manual": True, "engine": ENGINE,
                "notes": "Little or no text layer (%d chars over %d pages). Likely scanned: enter requirements manually." % (total, len(pages))}
    rows, seen = [], set()
    section = None
    for pno, page in enumerate(pages, start=1):
        # join hard-wrapped lines into paragraphs, keeping numbered items as their own lines
        paras, buf = [], []
        for line in page.splitlines():
            if not line.strip():
                if buf:
                    paras.append(" ".join(buf)); buf = []
                continue
            if NUMBERED.match(line) and buf:
                paras.append(" ".join(buf)); buf = []
            buf.append(line.strip())
        if buf:
            paras.append(" ".join(buf))
        for para in paras:
            m = NUMBERED.match(para)
            ref = m.group(1) if m else None
            body = _clean(m.group(2) if m else para)
            if ref and len(body) < 80 and not body.rstrip().endswith((".", ";", ":")):
                section = "%s %s" % (ref, body)   # heading
                continue
            for sent in SENT_SPLIT.split(body):
                sent = _clean(sent)
                if len(sent) < 25:
                    continue
                hard = bool(MANDATORY.search(sent))
                if not hard and not SOFT.search(sent):
                    continue
                key = sent.lower()[:160]
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "Requirement_Text": sent[:2000],
                    "Mandatory": hard,
                    "Priority": "Unspecified",
                    "Category": categorize(sent),
                    "Section_Group": (section or "")[:255] or None,
                    "Source_Reference": ref,
                    "Page_PDF": str(pno) if paged else None,
                    "Our_Position": "Unverified",
                })
    return {"rows": rows, "needs_manual": len(rows) == 0, "engine": ENGINE,
            "notes": "%d draft rows from %d pages by rule (no AI). Verify each row; page numbers are PDF pages." % (len(rows), len(pages))}


def map_table_rows(rows, colmap):
    """rows: list of dicts from an XLSX/DOCX table reader. colmap: {"Requirement_Text": "Specification", ...}.
    Every mapped row is Mandatory unless a 'Mandatory' column says otherwise."""
    out = []
    for r in rows:
        text = _clean(str(r.get(colmap.get("Requirement_Text"), "") or ""))
        if len(text) < 3:
            continue
        mand_col = colmap.get("Mandatory")
        mand = True if not mand_col else str(r.get(mand_col, "")).strip().lower() in ("y", "yes", "m", "mandatory", "required", "true", "x")
        out.append({
            "Requirement_Text": text[:2000],
            "Mandatory": mand,
            "Priority": "Unspecified",
            "Category": categorize(text),
            "Section_Group": _clean(str(r.get(colmap.get("Section_Group"), "") or "")) or None,
            "Source_Reference": _clean(str(r.get(colmap.get("Source_Reference"), "") or "")) or None,
            "Our_Position": "Unverified",
        })
    return out


def find_table(grid, scan_rows=25):
    """grid: list of (row_number, [cell, ...]) for one sheet. Finds the header row of a
    requirements table. Returns (header_index, colmap-by-index) or None.
    colmap keys: Requirement_Text (required), Source_Reference, Mandatory, Section_Group."""
    for i, (_, cells) in enumerate(grid[:scan_rows]):
        if sum(1 for c in cells if (c or "").strip()) < 2:
            continue  # a title line, not a header row
        idx = {}
        for j, c in enumerate(cells):
            c = (c or "").strip()
            if not c or len(c) > 60:
                continue
            if "Requirement_Text" not in idx and HDR_TEXT.search(c) and not HDR_MAND.search(c):
                idx["Requirement_Text"] = j
            elif "Mandatory" not in idx and HDR_MAND.search(c):
                idx["Mandatory"] = j
            elif "Source_Reference" not in idx and HDR_REF.match(c):
                idx["Source_Reference"] = j
            elif "Section_Group" not in idx and HDR_SECTION.match(c):
                idx["Section_Group"] = j
        if "Requirement_Text" in idx:
            return i, idx
    return None


def parse_table(grid, sheet_name=""):
    """Rule-based read of a requirements matrix. Rows below the header whose only
    content is a short label (no text cell) become the running Section_Group.
    Returns list of row dicts, or None when no header is recognised."""
    found = find_table(grid)
    if not found:
        return None
    hi, idx = found
    t = idx["Requirement_Text"]
    header = grid[hi][1]
    names = {k: (header[j] if j < len(header) else k) for k, j in idx.items()}
    section, dict_rows, rownos, sections = None, [], [], []
    for rno, cells in grid[hi + 1:]:
        get = lambda j: (cells[j] if j is not None and j < len(cells) else "") or ""
        text = _clean(get(t))
        filled = [c for c in cells if (c or "").strip()]
        if len(text) < 3:
            if len(filled) == 1 and len(filled[0]) < 120:
                section = _clean(filled[0])
            continue
        dict_rows.append({names[k]: get(j) for k, j in idx.items()})
        rownos.append(rno)
        sections.append(section)
    out = map_table_rows(dict_rows, names)   # 1:1 with dict_rows: every text is >= 3 chars
    for row, rno, sec in zip(out, rownos, sections):
        if not row["Section_Group"] and sec:
            row["Section_Group"] = sec
        if not row["Source_Reference"]:
            row["Source_Reference"] = ("%s row %s" % (sheet_name, rno)).strip()
        row["Page_PDF"] = None
    return out
