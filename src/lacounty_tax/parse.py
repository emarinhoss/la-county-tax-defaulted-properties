"""Parse an LA County "Resolution List" auction book PDF into a table.

The book is a borderless table whose rows wrap over several lines. Every cell
in a row is vertically centred on the line that carries the item number (the
"anchor" line), so a wrapped legal description or address extends equally
above and below it. We therefore assign each text line to a row by mirroring
around the anchor lines instead of using row rules (there are none).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pdfplumber

# x0 boundaries (PDF points) of the columns, measured on the 2026B book.
COLUMNS = {
    "item": (0, 60),
    "ain": (60, 125),
    "min_bid": (125, 185),
    "imp": (185, 225),
    "nsb": (225, 258),
    "legal": (258, 516),
    "location": (516, 640),
    "address": (640, 10_000),
}
MULTILINE = ("legal", "location", "address")
SINGLE = ("ain", "min_bid", "imp", "nsb")

LINE_TOL = 2.0  # words whose tops differ by less than this share a line
MIRROR_TOL = 3.0  # allowed error when matching mirrored line positions

ITEM_RE = re.compile(r"^\d{3,5}$")
AIN_RE = re.compile(r"^\d{4}-\d{3}-\d{3}$")
BID_RE = re.compile(r"^\$([\d,]+(?:\.\d\d)?)$")
ZIP_RE = re.compile(r"(\d{5})(?:[ -](\d{4}))?$")
AUCTION_RE = re.compile(r"(\d{4}[A-Z])\s+RESOLUTION\s+LIST")

COLUMNS_OUT = [
    "auction", "item", "ain", "min_bid", "improved", "nsb", "legal_description",
    "location", "property_address", "zip", "zip4", "vacant_lot", "page",
]


@dataclass
class ParseResult:
    table: pd.DataFrame
    auction: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Line:
    y: float
    words: list[dict]

    @property
    def text(self) -> str:
        return " ".join(w["text"] for w in sorted(self.words, key=lambda w: w["x0"]))


def _column_of(x0: float) -> str | None:
    for name, (lo, hi) in COLUMNS.items():
        if lo <= x0 < hi:
            return name
    return None


def _group_lines(words: list[dict]) -> list[_Line]:
    lines: list[_Line] = []
    for w in sorted(words, key=lambda w: w["top"]):
        if lines and abs(w["top"] - lines[-1].y) <= LINE_TOL:
            lines[-1].words.append(w)
        else:
            lines.append(_Line(w["top"], [w]))
    return lines


def assign_lines(lines: list[_Line], anchors: list[float]) -> tuple[list[list[_Line]], list[_Line]]:
    """Assign the lines of one column to rows centred on ``anchors``.

    Walking down the page: lines above an anchor that no earlier row claimed
    belong to this row; the row then claims the lines mirrored below the
    anchor. Returns (lines per row, unassigned lines).
    """
    rows: list[list[_Line]] = [[] for _ in anchors]
    p = 0
    for i, a in enumerate(anchors):
        above: list[_Line] = []
        while p < len(lines) and lines[p].y < a - LINE_TOL:
            above.append(lines[p])
            p += 1
        rows[i].extend(above)
        while p < len(lines) and abs(lines[p].y - a) <= LINE_TOL:
            rows[i].append(lines[p])
            p += 1
        for target in sorted(2 * a - ln.y for ln in above):
            if p < len(lines) and abs(lines[p].y - target) <= MIRROR_TOL:
                rows[i].append(lines[p])
                p += 1
    return rows, lines[p:]


def _join(lines: list[_Line]) -> str:
    return re.sub(r"\s+", " ", " ".join(ln.text for ln in lines)).strip()


def _page_body_words(page) -> list[dict]:
    words = page.extract_words()
    header = [w for w in words if w["text"] == "ITEM"]
    if not header:
        return []
    cutoff = min(w["top"] for w in header) + 6
    body = [w for w in words if w["top"] > cutoff]
    footer = [w for w in body if w["text"] == "Pg"]
    for f in footer:  # drop the "Pg N" footer line
        body = [w for w in body if abs(w["top"] - f["top"]) > LINE_TOL]
    return body


def parse_page(page, page_no: int, warnings: list[str]) -> list[dict]:
    by_col: dict[str, list[dict]] = {c: [] for c in COLUMNS}
    for w in _page_body_words(page):
        col = _column_of(w["x0"])
        if col:
            by_col[col].append(w)

    anchor_words = sorted(
        (w for w in by_col["item"] if ITEM_RE.match(w["text"])), key=lambda w: w["top"]
    )
    anchors = [w["top"] for w in anchor_words]
    if not anchors:
        return []

    cells: dict[str, list[list[_Line]]] = {}
    for col in (*MULTILINE, *SINGLE):
        rows, leftover = assign_lines(_group_lines(by_col[col]), anchors)
        cells[col] = rows
        if leftover:
            warnings.append(
                f"page {page_no}: {len(leftover)} unassigned '{col}' line(s), e.g. {leftover[0].text!r}"
            )

    out = []
    for i, aw in enumerate(anchor_words):
        out.append(
            {
                "item": int(aw["text"]),
                "ain_raw": _join(cells["ain"][i]),
                "bid_raw": _join(cells["min_bid"][i]),
                "imp_raw": _join(cells["imp"][i]),
                "nsb_raw": _join(cells["nsb"][i]),
                "legal_description": _join(cells["legal"][i]),
                "location": _join(cells["location"][i]),
                "property_address": _join(cells["address"][i]),
                "page": page_no,
            }
        )
    return out


def _finish(rows: list[dict], auction: str | None, warnings: list[str]) -> pd.DataFrame:
    records = []
    for r in rows:
        tag = f"item {r['item']} (page {r['page']})"
        ain = r["ain_raw"]
        if not AIN_RE.match(ain):
            warnings.append(f"{tag}: unexpected AIN {ain!r}")
        m = BID_RE.match(r["bid_raw"])
        if not m:
            warnings.append(f"{tag}: unexpected min bid {r['bid_raw']!r}")
        if r["imp_raw"] not in ("", "Y"):
            warnings.append(f"{tag}: unexpected IMP {r['imp_raw']!r}")
        if not r["nsb_raw"].isdigit():
            warnings.append(f"{tag}: unexpected NSB# {r['nsb_raw']!r}")

        addr = r["property_address"]
        # Same rule the original notebook used: addresses starting with "VAC"
        # ("VACANT LOT", "VAC/COR ...") are vacant land.
        vacant = addr.startswith("VAC")
        zm = ZIP_RE.search(addr)
        if r["imp_raw"] == "Y" and not zm and not vacant:
            warnings.append(f"{tag}: improved parcel without ZIP in address {addr!r}")
        if not r["legal_description"]:
            warnings.append(f"{tag}: empty legal description")
        if not r["location"]:
            warnings.append(f"{tag}: empty location")

        records.append(
            {
                "auction": auction,
                "item": r["item"],
                "ain": ain.replace("-", ""),
                "min_bid": float(m.group(1).replace(",", "")) if m else None,
                "improved": r["imp_raw"] == "Y",
                "nsb": int(r["nsb_raw"]) if r["nsb_raw"].isdigit() else None,
                "legal_description": r["legal_description"],
                "location": r["location"],
                "property_address": addr,
                "zip": zm.group(1) if zm else None,
                "zip4": zm.group(2) if zm else None,
                "vacant_lot": vacant,
                "page": r["page"],
            }
        )
    df = pd.DataFrame.from_records(records, columns=COLUMNS_OUT)
    if df["item"].duplicated().any():
        warnings.append(f"duplicate item numbers: {sorted(df.loc[df['item'].duplicated(), 'item'])[:10]}")
    if df["ain"].duplicated().any():
        warnings.append(f"duplicate AINs: {sorted(df.loc[df['ain'].duplicated(), 'ain'])[:10]}")
    return df


def parse_pdf(path: str | Path, auction: str | None = None) -> ParseResult:
    warnings: list[str] = []
    rows: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for n, page in enumerate(pdf.pages, start=1):
            if auction is None:
                m = AUCTION_RE.search(page.extract_text() or "")
                if m:
                    auction = m.group(1)
            rows.extend(parse_page(page, n, warnings))
    if not rows:
        warnings.append("no auction rows found; is this a Resolution List PDF?")
    return ParseResult(_finish(rows, auction, warnings), auction, warnings)
