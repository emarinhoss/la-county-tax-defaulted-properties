"""Load HUD Small Area Fair Market Rents (SAFMR) into a clean ZIP -> rent table.

Download the SAFMR spreadsheet for the current fiscal year by hand from
https://www.huduser.gov/portal/datasets/fmr.html (the site blocks scripts), then::

    lacounty-tax load-fmr fy2026_safmrs.xlsx --year 2026

Output (``data/reference/hud_safmr.csv``): zip, area_name, fmr_0br .. fmr_4br, fiscal_year.
HUD's FMR is roughly the 40th-percentile gross rent (utilities included), so it is a
conservative rent estimate.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

BEDROOMS = range(5)
FMR_COLUMNS = [f"fmr_{b}br" for b in BEDROOMS]
OUT_COLUMNS = ["zip", "area_name", *FMR_COLUMNS, "fiscal_year"]


def _norm(header: object) -> str:
    return re.sub(r"\s+", "", str(header)).lower()


def normalize_safmr(raw: pd.DataFrame, fiscal_year: int | None = None,
                    zip_range: tuple[str, str] = ("90001", "93599")) -> pd.DataFrame:
    """Pick the ZIP, area and plain (100%) SAFMR columns out of HUD's wide table."""
    cols = {_norm(c): c for c in raw.columns}
    zip_col = next((orig for n, orig in cols.items() if n.startswith("zip")), None)
    area_col = next((orig for n, orig in cols.items() if "areaname" in n), None)
    rent_cols = {}
    for n, orig in cols.items():
        m = re.fullmatch(r"safmr(\d)br", n)  # excludes the "90%/110% payment standard" variants
        if m:
            rent_cols[int(m.group(1))] = orig
    if zip_col is None or set(rent_cols) != set(BEDROOMS):
        raise ValueError(
            "Unrecognised SAFMR layout: need a ZIP column and 'SAFMR 0BR'..'SAFMR 4BR'. "
            f"Found columns: {list(raw.columns)}"
        )
    out = pd.DataFrame({
        "zip": raw[zip_col].astype(str).str.extract(r"(\d+)")[0].str.zfill(5),
        "area_name": raw[area_col] if area_col else None,
    })
    for b in BEDROOMS:
        out[f"fmr_{b}br"] = pd.to_numeric(
            raw[rent_cols[b]].astype(str).str.replace(r"[$,\s]", "", regex=True), errors="coerce"
        )
    out["fiscal_year"] = fiscal_year
    lo, hi = zip_range
    out = out[(out["zip"] >= lo) & (out["zip"] <= hi)].dropna(subset=FMR_COLUMNS)
    return out.drop_duplicates("zip").sort_values("zip").reset_index(drop=True)[OUT_COLUMNS]


def load_safmr_xlsx(path: str | Path, fiscal_year: int | None = None) -> pd.DataFrame:
    return normalize_safmr(pd.read_excel(path, dtype=str), fiscal_year)


def read_fmr_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"zip": str})
    missing = [c for c in ["zip", *FMR_COLUMNS] if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing columns {missing}")
    return df
