"""Build the static dashboard (one self-contained HTML file) from scored auctions."""
from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path

import pandas as pd

from .geo import read_coordinates
from .relief import load_relief

ROW_COLUMNS = [
    "item", "ain", "segment", "property_type", "min_bid", "property_address", "zip", "location",
    "tax_status", "year_built", "sqft_main", "sqft_lot", "num_units", "num_beds", "num_baths",
    "land_value", "imp_value", "assessed_value", "bid_to_assessed", "bid_per_lot_sqft",
    "est_monthly_rent", "rehab_estimate", "rehab_risk", "redemption_risk", "fmr_metro_default",
    "excluded", "checklist", "lat", "lon",
    "roll_status", "years_in_default", "redemption_amount_now", "paid_down_pct", "redemption_date", "roll_redeemed",
    "unpaid_years", "oldest_unpaid_year", "sale_ref", "deeded_year", "p_stay", "adj_cap_rate", "rank_cap",
    "offered_before", "offered_sales",
]


def load_auction(auction_dir: Path) -> dict | None:
    """Read one auction's scored output, or None if it hasn't been analyzed yet."""
    scored, summary = auction_dir / "analysis" / "scored.csv", auction_dir / "analysis" / "summary.json"
    if not (scored.exists() and summary.exists()):
        return None
    meta = json.loads(summary.read_text())
    df = pd.read_csv(scored, dtype={"ain": str, "zip": str})
    coords = read_coordinates(auction_dir)
    if len(coords):                       # parcels without a coordinate simply get null lat/lon
        df = df.drop(columns=["lat", "lon"], errors="ignore").merge(
            coords[["ain", "lat", "lon"]].drop_duplicates("ain"), on="ain", how="left")
    df = df.reindex(columns=ROW_COLUMNS)
    return {
        "id": meta["auction"],
        "snapshot": meta["snapshot"].removesuffix(".csv"),
        "synthetic": bool(meta.get("synthetic", False)),
        "label": meta.get("label"),
        "assumptions": meta["assumptions"],
        "rows": json.loads(df.to_json(orient="records")),   # NaN -> null
    }


def collect(data_dirs: list[Path]) -> list[dict]:
    found = []
    for base in data_dirs:
        for d in sorted(Path(base).glob("*")):
            a = load_auction(d) if d.is_dir() else None
            if a:
                found.append(a)
    # real auctions first (newest id first), synthetic demos last
    return sorted(found, key=lambda a: (a["synthetic"], [-ord(c) for c in a["id"]]))


DEFAULT_BOUNDARIES_DIR = Path("data/reference/boundaries")
DEFAULT_RELIEF_DIR = Path("data/reference/relief")


def load_boundaries(boundaries_dir: Path | None) -> dict:
    """GeoJSON overlays for the map: la_county.geojson, and la_cities.geojson if present."""
    out: dict = {"county": None, "cities": None}
    d = Path(boundaries_dir) if boundaries_dir else None
    for key, name in (("county", "la_county.geojson"), ("cities", "la_cities.geojson")):
        if d and (d / name).exists():
            out[key] = json.loads((d / name).read_text())
    return out


def render_html(auctions: list[dict], boundaries: dict | None = None, relief: dict | None = None) -> str:
    tpl = resources.files("lacounty_tax").joinpath("templates")
    html = tpl.joinpath("dashboard.html").read_text()
    css = tpl.joinpath("dashboard.css").read_text()
    js = tpl.joinpath("dashboard.js").read_text()
    data = json.dumps({"auctions": auctions, "boundaries": boundaries or {"county": None, "cities": None}, "relief": relief},
                      separators=(",", ":"), allow_nan=False)
    # Escape every "<" (valid JSON): stops "</script>" and "<!--<script" from confusing the HTML parser.
    data = data.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    # data last, so user-visible text can never be re-expanded by a later replace
    return html.replace("/*CSS*/", css).replace("/*JS*/", js).replace("/*DATA*/", data)


def build_site(data_dirs: list[Path], out_dir: Path, boundaries_dir: Path | None = DEFAULT_BOUNDARIES_DIR,
               relief_dir: Path | None = DEFAULT_RELIEF_DIR) -> Path:
    auctions = collect(data_dirs)
    if not auctions:
        raise FileNotFoundError(
            f"no analyzed auctions found under {[str(d) for d in data_dirs]} "
            "(each needs analysis/scored.csv and summary.json; run `analyze` first)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("")
    vendor = Path(str(resources.files("lacounty_tax").joinpath("templates", "vendor")))
    shutil.copytree(vendor, out_dir / "vendor", dirs_exist_ok=True)      # Leaflet, so the page needs no CDN
    relief = load_relief(relief_dir)
    if relief:                                                            # the shaded-relief image sits beside index.html
        (out_dir / "relief").mkdir(exist_ok=True)
        shutil.copyfile(Path(relief_dir) / "la_relief.png", out_dir / "relief" / "la_relief.png")
    out = out_dir / "index.html"
    out.write_text(render_html(auctions, load_boundaries(boundaries_dir), relief))
    return out
