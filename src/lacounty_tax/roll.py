"""The Treasurer-Tax Collector's secured defaulted tax roll, for the parcels of one auction.

Source: the county's ArcGIS Online table ``Secured Defaulted Tax Roll`` (about 1.7 million rows, recomputed
monthly). Rows come in four kinds, all keyed by ``Group_Number`` (the defaulted account, normally the parcel's AIN):

* ``01``  account header: tax status (1 = delinquent, 2 = defaulted), the year it first defaulted, the amount
  needed to redeem as of ``Compute_MonthYear``, and a ``Redemption_Date`` once it has been paid off;
* ``02``  one line per assessment year with the tax and penalty still owed;
* ``03``  direct assessments (not used here);
* ``04``  a sale/item reference with a ``Deeded_Year``.

A parcel that has been redeemed either carries a redemption date (still listed, amounts zeroed) or is removed
from the roll altogether. The county's minimum bid for a parcel is, to within a few percent, its redemption
amount at the time the book was made, so today's redemption amount against the book's minimum bid shows how much
of the default the owner has already paid.

    lacounty-tax pull-roll 2026B        # -> data/auctions/2026B/roll/<date>.csv
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from .geo import GisClient

ROLL_URL = ("https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/"
            "Secured%20Defaulted%20Tax%20Roll/FeatureServer/0/query")
OUT_FIELDS = ["OBJECTID", "Record_Type", "Group_Number", "Default_Count", "Tax_Status", "Defaulted_Year",
              "Redemption_Amount", "Redemption_Date", "Compute_MonthYear", "Last_Payment_Date", "Assessment_AIN",
              "Assessment_Year", "Tax_Balance", "Penalty_Balance", "AIN", "Sale_Number", "Item_Number", "Deeded_Year"]
KEEP_TYPES = ("01", "02", "04")
SUMMARY_COLUMNS = [
    "ain", "on_roll", "roll_status", "default_count", "defaulted_year", "years_in_default", "redemption_amount",
    "compute_month", "redemption_date", "redeemed", "unpaid_years", "oldest_unpaid_year", "sale_ref", "deeded_year",
]
STATUS_NAMES = {"1": "delinquent", "2": "defaulted"}
PAGE = 2000


def _where_in(field: str, values: list[str]) -> str:
    return f"{field} IN ({','.join(repr(v) for v in values)})"


def _query_all(client: GisClient, where: str) -> list[dict]:
    """All rows for a where clause, following the service's paging."""
    rows, offset = [], 0
    while True:
        data = client.request({
            "where": where, "outFields": ",".join(OUT_FIELDS), "returnGeometry": "false", "f": "json",
            "orderByFields": "OBJECTID", "resultOffset": str(offset), "resultRecordCount": str(PAGE),
        }, url=ROLL_URL)
        feats = [f.get("attributes") or {} for f in data.get("features", [])]
        rows += feats
        if len(feats) < PAGE and not data.get("exceededTransferLimit"):
            return rows
        offset += len(feats)


def fetch_roll_rows(client: GisClient, ains: list[str], *, batch: int = 60,
                    progress: Callable[[int, int], None] | None = None) -> pd.DataFrame:
    """Raw roll rows (types 01, 02, 04) for the given AINs, as one frame de-duplicated on OBJECTID.

    Pass 1 asks for accounts whose group number is one of the AINs. Parcels that are not the head of their
    own account (part of a larger group) are found in pass 2 through their assessment lines.
    """
    ains = list(dict.fromkeys(a.zfill(10) for a in ains))
    types = "Record_Type IN ('01','02','04')"
    seen: dict[int, dict] = {}
    batches = [ains[i:i + batch] for i in range(0, len(ains), batch)]
    for n, b in enumerate(batches, start=1):
        for r in _query_all(client, f"{_where_in('Group_Number', b)} AND {types}"):
            seen[r["OBJECTID"]] = r
        if progress:
            progress(n, len(batches) * 2)
    have = {r["Group_Number"] for r in seen.values() if r.get("Record_Type") == "01"}
    missing = [a for a in ains if a not in have]
    groups: set[str] = set()
    for n, i in enumerate(range(0, len(missing), batch), start=1):
        for r in _query_all(client, f"{_where_in('Assessment_AIN', missing[i:i + batch])} AND Record_Type = '02'"):
            seen[r["OBJECTID"]] = r
            if r.get("Group_Number"):
                groups.add(r["Group_Number"])
    todo = sorted(groups - have)
    for i in range(0, len(todo), batch):
        for r in _query_all(client, f"{_where_in('Group_Number', todo[i:i + batch])} AND {types}"):
            seen[r["OBJECTID"]] = r
    if progress:
        progress(len(batches) * 2, len(batches) * 2)
    return pd.DataFrame(list(seen.values()))


def _num(v) -> float | None:
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _date(v) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def summarize_roll(rows: pd.DataFrame, ains: list[str], asof: date) -> pd.DataFrame:
    """One row per AIN: is it on the roll, since when, what does it owe now, was it redeemed."""
    ains = list(dict.fromkeys(a.zfill(10) for a in ains))
    if rows is None or not len(rows):
        rows = pd.DataFrame(columns=OUT_FIELDS)
    rows = rows.reindex(columns=OUT_FIELDS)          # tolerate batches that happened to lack a kind of row
    rows = rows.astype({c: "object" for c in rows.columns})
    headers = rows[rows["Record_Type"] == "01"]
    lines = rows[rows["Record_Type"] == "02"]
    sales = rows[rows["Record_Type"] == "04"]
    group_of: dict[str, str] = {}
    for g in headers["Group_Number"].dropna().unique():
        group_of[g] = g
    for _, ln in lines.iterrows():
        a, g = ln.get("Assessment_AIN"), ln.get("Group_Number")
        if isinstance(a, str) and isinstance(g, str):
            group_of.setdefault(a, g)

    out = []
    for ain in ains:
        g = group_of.get(ain)
        acct = headers[headers["Group_Number"] == g] if g else headers.iloc[0:0]
        rec: dict = {c: None for c in SUMMARY_COLUMNS}
        rec.update(ain=ain, on_roll=len(acct) > 0, redeemed=False)
        if len(acct):
            # one account normally; if several, prefer one still unredeemed, then the oldest default
            acct = acct.assign(_live=acct["Redemption_Date"].isna(), _dy=pd.to_numeric(acct["Defaulted_Year"], errors="coerce"))
            h = acct.sort_values(["_live", "_dy"], ascending=[False, True]).iloc[0]
            rdate = _date(h.get("Redemption_Date"))
            dy = _num(h.get("Defaulted_Year"))
            rec.update(
                roll_status=STATUS_NAMES.get(str(h.get("Tax_Status"))), default_count=_num(h.get("Default_Count")),
                defaulted_year=dy, years_in_default=(asof.year - dy) if dy else None,
                redemption_amount=_num(h.get("Redemption_Amount")), compute_month=h.get("Compute_MonthYear"),
                redemption_date=rdate, redeemed=rdate is not None,
            )
            mine = lines[(lines["Group_Number"] == g) & ((lines["Assessment_AIN"] == ain) | (lines["Assessment_AIN"].isna()))]
            owed = mine[(pd.to_numeric(mine["Tax_Balance"], errors="coerce").fillna(0) > 0)
                        | (pd.to_numeric(mine["Penalty_Balance"], errors="coerce").fillna(0) > 0)]
            yrs = pd.to_numeric(owed["Assessment_Year"], errors="coerce").dropna()
            rec.update(unpaid_years=int(yrs.nunique()) if len(yrs) else 0, oldest_unpaid_year=int(yrs.min()) if len(yrs) else None)
            s = sales[sales["Group_Number"] == g]
            if len(s):
                rec.update(sale_ref=f"{s.iloc[0].get('Sale_Number')} #{s.iloc[0].get('Item_Number')}",
                           deeded_year=_num(s.iloc[0].get("Deeded_Year")))
        else:
            rec["redeemed"] = True       # off the roll altogether: paid off and removed
        out.append(rec)
    return pd.DataFrame(out, columns=SUMMARY_COLUMNS)


def roll_dir(auction_dir: Path) -> Path:
    return Path(auction_dir) / "roll"


def latest_roll(auction_dir: Path) -> Path | None:
    files = sorted(roll_dir(auction_dir).glob("*.csv"))
    return files[-1] if files else None


def read_roll(path: Path | None) -> pd.DataFrame | None:
    if not path or not Path(path).exists():
        return None
    return pd.read_csv(path, dtype={"ain": str})


def pull_roll(auction_dir: Path, client: GisClient, *, today: date | None = None,
              progress: Callable[[int, int], None] | None = None) -> tuple[Path, pd.DataFrame]:
    """Fetch the roll for an auction's parcels and write ``roll/<date>.csv``."""
    today = today or date.today()
    items = pd.read_csv(Path(auction_dir) / "items.csv", dtype={"ain": str})
    ains = list(items["ain"])
    rows = fetch_roll_rows(client, ains, progress=progress)
    summary = summarize_roll(rows, ains, today)
    out = roll_dir(auction_dir) / f"{today.isoformat()}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out, index=False)
    return out, summary


def sale_digits(v) -> str:
    """AINs appear as 10 digits or as 'NNNN-NNN-NNN'; normalise to 10 digits."""
    return re.sub(r"\D", "", str(v))[:10].zfill(10)
