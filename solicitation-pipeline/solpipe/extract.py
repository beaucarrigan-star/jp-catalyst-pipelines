"""Turn an attachment into parser input for rule_ingest. No AI, no network.

PDF          -> text layer per page (pdfminer.six; scanned PDFs have none and go to manual entry)
XLSX / XLSM  -> cell grid per sheet (Form K requirement matrices are xlsx)
DOCX         -> paragraph and table text
TXT/CSV/MD   -> as is
ZIP          -> opened; every supported file inside is read as if it were attached on its own
Anything else is skipped with a reason.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile

from . import rule_ingest

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def kind_of(filename: str) -> str | None:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    return {"pdf": "pdf", "xlsx": "xlsx", "xlsm": "xlsx", "docx": "docx",
            "txt": "text", "csv": "text", "md": "text", "zip": "zip"}.get(ext)


# Limits so one bad or hostile archive cannot exhaust the function's memory.
ZIP_MAX_FILES = 200
ZIP_MAX_FILE_BYTES = 60 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 250 * 1024 * 1024


def expand_zip(data: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Returns ([(member name, bytes) for readable members], [skip notes]).
    Folders, macOS metadata, hidden files and nested zips are not read."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("not a valid .zip file")
    members, skipped, total = [], [], 0
    for info in z.infolist():
        name = info.filename.replace("\\", "/")
        base = name.rsplit("/", 1)[-1]
        if info.is_dir() or not base or name.startswith("__MACOSX/") or base.startswith((".", "~$")):
            continue
        if info.flag_bits & 0x1:
            skipped.append(f"{base} (password-protected)")
            continue
        k = kind_of(base)
        if k is None:
            skipped.append(f"{base} (unsupported file type)")
            continue
        if k == "zip":
            skipped.append(f"{base} (zip inside a zip - unzip it first)")
            continue
        if len(members) >= ZIP_MAX_FILES:
            skipped.append(f"{base} (more than {ZIP_MAX_FILES} files in the zip)")
            continue
        if info.file_size > ZIP_MAX_FILE_BYTES or total + info.file_size > ZIP_MAX_TOTAL_BYTES:
            skipped.append(f"{base} (too large once unzipped)")
            continue
        total += info.file_size
        members.append((name, z.read(info)))
    return members, skipped


def _col_index(ref: str | None, fallback: int) -> int:
    m = re.match(r"[A-Z]+", ref or "")
    if not m:
        return fallback  # the r attribute is optional in xlsx
    letters = m.group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_grids(data: bytes, max_rows: int = 5000) -> list[tuple[str, list[tuple[str, list[str]]]]]:
    """[(sheet name, [(row number, [cell text by column]), ...]), ...]"""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    out = []
    for sh in wb.find("m:sheets", NS):
        name = sh.get("name")
        rid = sh.get(f"{{{NS['r']}}}id")
        path = target[rid].lstrip("/")
        path = path if path.startswith("xl/") else "xl/" + path
        root = ET.fromstring(z.read(path))
        grid = []
        for row in root.iter(f"{{{NS['m']}}}row"):
            cells: dict[int, str] = {}
            for c in row.findall("m:c", NS):
                t = c.get("t")
                v = c.find("m:v", NS)
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
                else:
                    val = v.text if v is not None else ""
                val = (val or "").replace("\n", " ").strip()
                if val:
                    cells[_col_index(c.get("r"), max(cells) + 1 if cells else 0)] = val
            if cells:
                grid.append((row.get("r") or str(len(grid) + 1), [cells.get(i, "") for i in range(max(cells) + 1)]))
            if len(grid) >= max_rows:
                break
        if grid:
            out.append((name, grid))
    return out


def grid_text(grid: list[tuple[str, list[str]]]) -> str:
    return "\n".join("\t".join(cells) for _, cells in grid)


def docx_text(data: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(data))
    root = ET.fromstring(z.read("word/document.xml"))
    body = root.find("w:body", NS)
    out = []
    for el in body:
        tag = el.tag.split("}")[1]
        if tag == "p":
            t = "".join(x.text or "" for x in el.iter(f"{{{NS['w']}}}t")).strip()
            if t:
                out.append(t)
        elif tag == "tbl":
            for tr in el.iter(f"{{{NS['w']}}}tr"):
                cells = ["".join(x.text or "" for x in tc.iter(f"{{{NS['w']}}}t")).strip()
                         for tc in tr.findall("w:tc", NS)]
                if any(cells):
                    out.append(" | ".join(cells))
    return "\n".join(out)


def to_document(filename: str, data: bytes) -> dict:
    """Returns {"kind": "pdf", "pages": [...]} | {"kind": "xlsx", "sheets": [...]} |
    {"kind": "docx"|"text", "text": str} | {"skip": reason}."""
    k = kind_of(filename)
    if k is None:
        return {"skip": "unsupported file type"}
    try:
        if k == "pdf":
            if not data.startswith(b"%PDF"):
                return {"skip": "named .pdf but is not a PDF"}
            pages = rule_ingest.extract_pdf_pages(data)
            if pages is None:
                return {"skip": "PDF reader not installed (pip install pdfminer.six)"}
            return {"kind": "pdf", "pages": pages}
        if k == "xlsx":
            sheets = xlsx_grids(data)
            if not sheets:
                return {"skip": "no readable cells"}
            return {"kind": "xlsx", "sheets": sheets}
        text = docx_text(data) if k == "docx" else data.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - any unreadable file is skipped, never fatal
        return {"skip": f"could not read file ({e.__class__.__name__})"}
    if len(text.strip()) < 40:
        return {"skip": "no readable text"}
    return {"kind": k, "text": text}


def parse_document(doc: dict) -> dict:
    """Rule-based requirement rows for one document: {"rows", "needs_manual", "notes"}."""
    if doc["kind"] == "pdf":
        return rule_ingest.parse_pages(doc["pages"])
    if doc["kind"] == "xlsx":
        rows, notes = [], []
        for name, grid in doc["sheets"]:
            t = rule_ingest.parse_table(grid, name)
            if t is None:  # no recognisable header: read the cells as prose
                t = rule_ingest.parse_pages([grid_text(grid)], paged=False)["rows"]
                notes.append(f"{name}: no requirement column found, read as text ({len(t)} rows)")
            else:
                notes.append(f"{name}: table, {len(t)} rows")
            rows.extend(t)
        return {"rows": rows, "needs_manual": not rows, "notes": "; ".join(notes)}
    return rule_ingest.parse_pages([doc["text"]], paged=False)
