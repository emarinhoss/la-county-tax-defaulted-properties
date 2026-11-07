"""Which parcels were offered in earlier auction books.

The county publishes one ArcGIS layer per past sale (``2025A_Tax_Defaulted_Parcels``,
``TAX_DEFAULT_PROPERTIES_2024A``, ...) with each item's AIN, minimum bid and address. A parcel that shows up
again in a later book was offered before without leaving the roll, so a count of earlier appearances is a
simple "stale" signal. The layers are found by scanning the organization's service directory, so new sales
appear automatically.

    lacounty-tax pull-history      # -> data/reference/auction_history.csv  (sale, ain, item, min_bid)
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

import pandas as pd

from .geo import GisClient, GisError
from .roll import sale_digits

BASE = "https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services"
SALE_RE = re.compile(r"(20\d\d[AB])", re.I)
SERVICE_RE = re.compile(r"tax.?default", re.I)
KEY_FIELDS = ("ain", "apn", "apn_1", "ain_mb_pg_pcl")
ITEM_FIELDS = ("item", "item_no", "item_number")
BID_FIELDS = ("min_bid", "minbid")
COLUMNS = ["sale", "ain", "item", "min_bid"]
PAGE = 2000


def _find(fields: list[str], names: tuple[str, ...]) -> str | None:
    lower = {f.lower(): f for f in fields}
    return next((lower[n] for n in names if n in lower), None)


def discover_sales(client: GisClient, base: str = BASE) -> dict[str, tuple[str, int]]:
    """{sale id: (service name, layer id)} for every per-auction layer in the organization."""
    services = client.request({"f": "json"}, url=base).get("services", [])
    found: dict[str, tuple[str, int]] = {}
    for s in services:
        name = s.get("name", "")
        m = SALE_RE.search(name)
        if s.get("type") != "FeatureServer" or not SERVICE_RE.search(name) or not m:
            continue
        svc_url = f"{base}/{urllib.parse.quote(name)}/FeatureServer"
        try:
            best, best_rows = None, -1
            for lyr in client.request({"f": "json"}, url=svc_url).get("layers", []):
                meta = client.request({"f": "json"}, url=f"{svc_url}/{lyr['id']}")
                if not _find([f["name"] for f in meta.get("fields", [])], KEY_FIELDS):
                    continue
                n = client.request({"where": "1=1", "returnCountOnly": "true", "f": "json"},
                                   url=f"{svc_url}/{lyr['id']}/query").get("count") or 0
                if n > best_rows:
                    best, best_rows = lyr["id"], n
            if best is not None:
                found[m.group(1).upper()] = (name, best)
        except GisError:
            continue                                       # one odd service must not hide the others
    return dict(sorted(found.items()))


def fetch_sale(client: GisClient, sale: str, service: str, layer: int, base: str = BASE) -> pd.DataFrame:
    url = f"{base}/{urllib.parse.quote(service)}/FeatureServer/{layer}"
    meta = client.request({"f": "json"}, url=url)
    fields = [f["name"] for f in meta.get("fields", [])]
    oid = next((f["name"] for f in meta.get("fields", []) if f.get("type") == "esriFieldTypeOID"), None)   # OBJECTID, OBJECTID_1, ...
    key, item, bid = _find(fields, KEY_FIELDS), _find(fields, ITEM_FIELDS), _find(fields, BID_FIELDS)
    if not key:
        raise GisError(f"{service}: no AIN-like field")
    out_fields = ",".join(f for f in (key, item, bid) if f)
    rows, offset = [], 0
    while True:
        params = {"where": "1=1", "outFields": out_fields, "returnGeometry": "false", "f": "json",
                  "resultOffset": str(offset), "resultRecordCount": str(PAGE)}
        if oid:
            params["orderByFields"] = oid                  # stable paging
        data = client.request(params, url=url + "/query")
        feats = [f.get("attributes") or {} for f in data.get("features", [])]
        for a in feats:
            rows.append({"sale": sale, "ain": sale_digits(a.get(key)), "item": a.get(item) if item else None,
                         "min_bid": a.get(bid) if bid else None})
        if len(feats) < PAGE and not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
    return pd.DataFrame(rows, columns=COLUMNS)


def pull_history(client: GisClient, base: str = BASE, *, only: list[str] | None = None) -> pd.DataFrame:
    sales = discover_sales(client, base)
    frames = [fetch_sale(client, sale, svc, lyr, base) for sale, (svc, lyr) in sales.items() if not only or sale in only]
    if not frames:
        raise GisError("no per-auction layers found in the organization's service directory")
    return pd.concat(frames, ignore_index=True)


def write_history(df: pd.DataFrame, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values(["sale", "ain"]).to_csv(path, index=False)
    return path


def read_history(path: Path | None) -> pd.DataFrame | None:
    if not path or not Path(path).exists():
        return None
    return pd.read_csv(path, dtype={"ain": str, "sale": str, "item": str})


def offered_before(history: pd.DataFrame | None, ains: list[str], current_sale: str | None = None) -> pd.DataFrame:
    """Per AIN: how many earlier books listed it, and which ones."""
    cols = ["ain", "offered_before", "offered_sales"]
    if history is None or not len(history):
        return pd.DataFrame({"ain": ains, "offered_before": None, "offered_sales": None}, columns=cols)
    h = history[history["sale"].str.upper() != (current_sale or "").upper()]
    g = h.groupby("ain")["sale"].agg(lambda s: sorted(set(s)))
    rows = [{"ain": a, "offered_before": len(g.get(a, [])), "offered_sales": ",".join(g.get(a, [])) or None} for a in ains]
    return pd.DataFrame(rows, columns=cols)
