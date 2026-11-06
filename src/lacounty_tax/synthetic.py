"""Generate a clearly fake auction ("DEMO") for developing and testing the dashboard.

Nothing here resembles a real parcel: addresses are ``<n> DEMO <NAME> ST``, AINs start
with ``99`` (not a real LA County book), and the summary is stamped ``synthetic: true`` so
the dashboard shows a banner. The data goes through the real enrichment-status and scoring
code, so its schema is exactly what a real run produces.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .analyze import Assumptions, run_analysis
from .enrich import SNAPSHOT_COLUMNS, update_status_history

AUCTION_ID = "DEMO"
# (lat_min, lat_max, lon_min, lon_max) of a few populated areas; demo points are placed at random inside them
AREAS = [(33.95, 34.12, -118.45, -118.15), (34.15, 34.30, -118.60, -118.30), (33.76, 33.88, -118.25, -118.10),
         (34.10, 34.20, -118.20, -118.05), (34.00, 34.10, -118.00, -117.75), (34.55, 34.72, -118.25, -117.90)]
LOCATIONS = ["COUNTY OF LOS ANGELES"] * 5 + [
    "CITY-LOS ANGELES", "CITY-LOS ANGELES", "CITY-LANCASTER", "CITY-PALMDALE", "CITY-LONG BEACH",
    "CITY-POMONA", "CITY-COMPTON", "CITY-INGLEWOOD", "CITY-PASADENA", "CITY EMP PENSION",
]
STREETS = ["ALDER", "BIRCH", "CEDAR", "DALE", "ELM", "FERN", "GROVE", "HAZEL", "IRIS", "JUNIPER"]


def make_demo(out_dir: Path, fmr: pd.DataFrame, *, n_rental: int = 130, n_land: int = 230,
              seed: int = 7, asof: date = date(2026, 11, 6)) -> Path:
    """Write ``<out_dir>/DEMO/{items.csv,enrichment/,status_history.csv,analysis/}``; return its path."""
    rng = np.random.default_rng(seed)
    auction_dir = Path(out_dir) / AUCTION_ID
    (auction_dir / "enrichment").mkdir(parents=True, exist_ok=True)
    for stale in (auction_dir / "status_history.csv",):
        stale.unlink(missing_ok=True)

    zips = fmr[~fmr["metro_default"]]["zip"].to_numpy() if "metro_default" in fmr else fmr["zip"].to_numpy()
    rent2 = fmr.set_index("zip")["fmr_2br"]
    n = n_rental + n_land
    used_ain: set[str] = set()

    def new_ain() -> str:
        while True:
            a = "99" + "".join(map(str, rng.integers(0, 10, 8)))
            if a not in used_ain:
                used_ain.add(a)
                return a

    items, snap, coords = [], [], []
    kinds = ["rental"] * n_rental + ["land"] * n_land
    rng.shuffle(kinds)
    for i, kind in enumerate(kinds):
        ain = new_ain()
        zip5 = str(rng.choice(zips))
        zip4 = f"{rng.integers(0, 10000):04d}"
        loc = str(rng.choice(LOCATIONS))
        street = f"{rng.integers(100, 9999)} DEMO {rng.choice(STREETS)} ST"
        rent_scale = float(rent2.get(zip5, 2900)) / 2900
        a0, a1, o0, o1 = AREAS[int(rng.integers(len(AREAS)))]
        coords.append(dict(ain=ain, lat=round(float(rng.uniform(a0, a1)), 6), lon=round(float(rng.uniform(o0, o1)), 6),
                           source="synthetic", fetched_at=f"{asof.isoformat()}T12:00:00+00:00"))
        row_snap = dict(auction=AUCTION_ID, ain=ain, parcel_status="ACTIVE", fetch_error=None,
                        tax_status="DEFAULTED", tax_defaulted_year=str(rng.integers(2017, 2023)))
        if kind == "rental":
            units = int(rng.choice([1, 2, 3, 4, 0, 6], p=[0.72, 0.1, 0.05, 0.03, 0.06, 0.04]))
            beds_per = int(rng.choice([1, 2, 3, 4, 5], p=[0.08, 0.27, 0.42, 0.19, 0.04])) if units <= 1 \
                else int(rng.choice([1, 2, 3], p=[0.35, 0.5, 0.15]))
            beds = None if rng.random() < 0.04 else float(max(units, 1) * beds_per)
            sqft = float(round(max(units, 1) * (beds_per * 380 + rng.normal(250, 120)), -1)) if units else None
            year = float(rng.choice(np.arange(1915, 2008)))
            land = float(round(rng.lognormal(np.log(130_000 * rent_scale ** 2), 0.55), -2))
            imp = float(round(land * rng.uniform(0.2, 1.7) * (0.8 if year < 1950 else 1.0), -2))
            condo = units == 1 and rng.random() < 0.22
            legal = (f"TR={rng.integers(1000, 70000)} CONDOMINIUM*UNIT {rng.integers(1, 300)}" if condo
                     else f"TRACT NO {rng.integers(1000, 70000)} LOT {rng.integers(1, 200)}")
            bid = float(round(np.clip(rng.lognormal(np.log(115_000), 0.8), 12_000, 700_000)))
            items.append(dict(auction=AUCTION_ID, item=1001 + i, ain=ain, min_bid=bid, improved=True,
                              nsb=int(rng.integers(500, 620)), legal_description=legal, location=loc,
                              property_address=f"{street} DEMOVILLE CA {zip5} {zip4}", zip=zip5, zip4=zip4,
                              vacant_lot=False, page=1 + i // 20))
            row_snap.update(situs_street=street, situs_zip=f"{zip5}-{zip4}", num_units=float(units), year_built=year,
                            sqft_main=sqft, sqft_lot=float(round(rng.lognormal(np.log(6500), 0.5), -1)),
                            num_beds=beds, num_baths=None if beds is None else float(max(1, round(beds / 2 + rng.normal(0.3, 0.6)))),
                            land_value=land, imp_value=imp)
        else:
            lot = float(round(rng.lognormal(np.log(6000), 0.8), -1))
            with_zip = rng.random() < 0.25
            addr = f"VAC/{street.split(' ', 1)[1]} DEMOVILLE CA {zip5} {zip4}" if with_zip else "VACANT LOT"
            bid = float(round(np.clip(rng.lognormal(np.log(6_000), 0.8), 1_500, 90_000)))
            items.append(dict(auction=AUCTION_ID, item=1001 + i, ain=ain, min_bid=bid, improved=bool(rng.random() < 0.015),
                              nsb=int(rng.integers(500, 620)), legal_description=f"TRACT NO {rng.integers(1000, 70000)} LOT {rng.integers(1, 200)}",
                              location=loc, property_address=addr, zip=zip5 if with_zip else None,
                              zip4=zip4 if with_zip else None, vacant_lot=True, page=1 + i // 20))
            row_snap.update(situs_street=None, situs_zip=zip5 if with_zip else None, num_units=None, year_built=None,
                            sqft_main=None, sqft_lot=lot, num_beds=None, num_baths=None,
                            land_value=float(round(lot * rng.uniform(4, 45), -2)), imp_value=0.0)
        if rng.random() < 0.2:                      # recent transfer
            d = asof - timedelta(days=int(rng.integers(30, 3 * 365)))
        else:
            d = asof - timedelta(days=int(rng.integers(3 * 365, 40 * 365)))
        row_snap.update(last_transfer_date=d.isoformat(), last_transfer_doc=f"{rng.integers(100000, 999999):07d}",
                        last_transfer_reason="Demo transfer")
        snap.append(row_snap)

    pd.DataFrame(coords).sample(frac=0.97, random_state=seed).to_csv(auction_dir / "coordinates.csv", index=False)  # a few unmapped
    items_df = pd.DataFrame(items)
    items_df.to_csv(auction_dir / "items.csv", index=False)

    base = pd.DataFrame(snap)
    earlier = (asof - timedelta(days=14))
    first = base.assign(pulled_at=f"{earlier.isoformat()}T12:00:00+00:00")
    final = base.copy()
    # between the pulls ~7% of parcels paid up (CURRENT) and one failed to fetch
    paid = rng.choice(len(final), size=max(1, int(0.07 * len(final))), replace=False)
    final.loc[paid, "tax_status"] = "CURRENT"
    rest = np.setdiff1d(np.arange(len(final)), paid)
    final.loc[rng.choice(rest, size=max(1, int(0.05 * len(final))), replace=False), "tax_status"] = "DELINQUENT"
    final.loc[int(rng.integers(0, len(final))), "fetch_error"] = "HTTP 503"
    final["pulled_at"] = f"{asof.isoformat()}T12:00:00+00:00"
    for df, day in ((first, earlier), (final, asof)):
        df = df.reindex(columns=SNAPSHOT_COLUMNS)
        df.to_csv(auction_dir / "enrichment" / f"{day.isoformat()}.csv", index=False)
        update_status_history(auction_dir / "status_history.csv", df)

    run_analysis(auction_dir, fmr, Assumptions(), auction_dir / "enrichment" / f"{asof.isoformat()}.csv",
                 extra_summary={"synthetic": True,
                                "label": "Synthetic demo data: not real parcels, addresses or prices"})
    return auction_dir
