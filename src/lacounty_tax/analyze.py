"""Score an auction's parcels as rent-and-hold candidates.

Inputs: ``items.csv`` (Phase 1), an enrichment snapshot (Phase 2) and a ZIP -> rent
table (HUD SAFMR). Output: one scored row per item, with the rental segment ranked.

Every number below that is not data is an *assumption* in :class:`Assumptions`, to be
tuned (override with a TOML file). Rents are HUD fair-market rents, i.e. conservative
and ZIP-level: the ranking is a screen for what deserves a closer look, not a valuation.

Money model (rental segment)::

    EGI   = 12 * rent * (1 - vacancy)
    NOI   = EGI * (1 - opex_pct) - property_tax_rate * bid
    cost  = bid * (1 + closing_cost_pct) + rehab
    cap   = NOI / cost
    max bid for a target cap rate t:
        bid <= (EGI * (1 - opex_pct) - t * rehab) / (property_tax_rate + t * (1 + closing_cost_pct))
"""
from __future__ import annotations

import json
import math
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

import pandas as pd

from .history import offered_before, read_history
from .roll import latest_roll, read_roll


@dataclass(frozen=True)
class Assumptions:
    bid_multiple: float = 1.0          # expected winning bid / minimum bid
    closing_cost_pct: float = 0.03     # fees, recording, etc. on top of the bid
    vacancy_rate: float = 0.06
    opex_pct: float = 0.25             # insurance, maintenance, management as % of collected rent
    property_tax_rate: float = 0.0125  # on purchase price (Prop 13 resets to price)
    target_cap_rate: float = 0.08
    rent_factor: float = 1.0           # scale HUD FMR (e.g. 1.1 if you believe market is above 40th pct)
    rehab_min: float = 10_000.0
    rehab_per_sqft_new: float = 15.0   # built 1980 or later
    rehab_per_sqft_mid: float = 30.0   # 1950-1979
    rehab_per_sqft_old: float = 45.0   # before 1950
    recent_transfer_years: float = 3.0
    equity_ratio_flag: float = 10.0    # assessed value / min bid considered "a lot of owner equity"
    paid_down_threshold: float = 0.5   # share of the book's minimum bid already paid off that marks an account "paid down"
    paid_down_leave_prob: float = 0.75 # chance a paid-down account is pulled before the sale (an assumption, not measured)
    rank_by: str = "adjusted"          # "adjusted" = cap rate x odds of staying in the sale; "cap" = raw cap rate

    @classmethod
    def from_toml(cls, path: str | Path) -> "Assumptions":
        data = tomllib.loads(Path(path).read_text())
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{path}: unknown assumption(s) {sorted(unknown)}; valid: {sorted(known)}")
        return cls(**data)


CHECKLIST = "verify liens (IRS/HOA/municipal) & title; occupancy unknown, no pre-sale access"


def _num(v) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def fmr_for_bedrooms(fmr: dict, beds: int) -> float:
    """HUD convention: 5BR = 4BR x 1.15, each further bedroom adds another 15%."""
    beds = max(int(beds), 0)
    if beds <= 4:
        return float(fmr[f"fmr_{beds}br"])
    return float(fmr["fmr_4br"]) * (1 + 0.15 * (beds - 4))


def estimate_monthly_rent(units: float, beds: float, fmr: dict, factor: float = 1.0) -> float | None:
    """Whole-property rent: units split the bedrooms evenly (rounded)."""
    if units < 1 or _num(beds) is None:
        return None
    per_unit = round(beds / units)
    return units * fmr_for_bedrooms(fmr, per_unit) * factor


def rehab_estimate(a: Assumptions, sqft: float | None, year_built: float | None) -> float:
    if sqft is None:
        return a.rehab_min
    if year_built is None or year_built < 1950:
        rate = a.rehab_per_sqft_old
    elif year_built < 1980:
        rate = a.rehab_per_sqft_mid
    else:
        rate = a.rehab_per_sqft_new
    return max(a.rehab_min, sqft * rate)


def income_metrics(a: Assumptions, monthly_rent: float, bid: float, rehab: float) -> dict:
    egi = 12 * monthly_rent * (1 - a.vacancy_rate)
    cost = bid * (1 + a.closing_cost_pct) + rehab
    noi = egi * (1 - a.opex_pct) - a.property_tax_rate * bid
    max_bid = (egi * (1 - a.opex_pct) - a.target_cap_rate * rehab) / (
        a.property_tax_rate + a.target_cap_rate * (1 + a.closing_cost_pct)
    )
    return {
        "all_in_cost": cost,
        "noi": noi,
        "cap_rate": noi / cost if cost else None,
        "gross_yield": 12 * monthly_rent / cost if cost else None,
        "max_bid_for_target": max(max_bid, 0.0),
    }


def rehab_risk(year_built: float | None, land: float | None, imp: float | None) -> str:
    """Condition proxy from age and how little of the assessed value is the structure."""
    pts = 0
    if year_built is None:
        pts += 1
    elif year_built < 1940:
        pts += 2
    elif year_built < 1960:
        pts += 1
    if land and imp is not None:
        ratio = imp / land
        pts += 2 if ratio < 0.5 else 1 if ratio < 1 else 0
    else:
        pts += 1
    return "high" if pts >= 4 else "medium" if pts >= 2 else "low"


def redemption_risk(a: Assumptions, asof: date, last_transfer: str | None, assessed: float | None,
                    bid: float, status_changes: int) -> str:
    """Heuristic: how likely the owner (or a lender) redeems before the sale."""
    signals = 0
    if last_transfer:
        years = (asof - date.fromisoformat(last_transfer)).days / 365.25
        signals += years <= a.recent_transfer_years
    if assessed and bid and assessed / bid >= a.equity_ratio_flag:
        signals += 1
    if status_changes > 1:  # status moved after first being recorded
        signals += 1
    return "high" if signals >= 2 else "medium" if signals == 1 else "low"


AGE_BUCKETS = [(0, 0, "0"), (1, 4, "1-4"), (5, 9, "5-9"), (10, 19, "10-19"), (20, 200, "20+")]
MIN_BUCKET_N = 15


def age_bucket(years: float | None) -> str | None:
    """Years in default as one of the buckets used for redemption rates."""
    if years is None or years != years:
        return None
    y = max(int(years), 0)
    return next((label for lo, hi, label in AGE_BUCKETS if lo <= y <= hi), None)


def redemption_rates(roll: pd.DataFrame | None) -> dict:
    """Share of parcels already redeemed, by years in default, among those still on the roll.

    Parcels removed from the roll lost their default year, so they are not in these counts: the rates are
    therefore lower bounds, and they describe the period up to the roll's date, not the final outcome.
    """
    if roll is None or not len(roll):
        return {}
    on = roll[roll["on_roll"].astype(bool) & roll["years_in_default"].notna()].copy()
    on["bucket"] = on["years_in_default"].map(age_bucket)
    overall = {"n": int(len(on)), "redeemed": int(on["redeemed"].astype(bool).sum())}
    overall["rate"] = overall["redeemed"] / overall["n"] if overall["n"] else 0.0
    out = {"all": overall}
    for label in [b[2] for b in AGE_BUCKETS]:
        g = on[on["bucket"] == label]
        n, k = int(len(g)), int(g["redeemed"].astype(bool).sum())
        out[label] = {"n": n, "redeemed": k, "rate": (k / n) if n >= MIN_BUCKET_N else overall["rate"], "pooled": n < MIN_BUCKET_N}
    return out


def paid_down_pct(redemption_amount: float | None, min_bid: float) -> float | None:
    """How much of the book's minimum bid has been paid off since the book was made (0..1)."""
    if redemption_amount is None or not min_bid:
        return None
    return float(min(1.0, max(0.0, 1.0 - redemption_amount / min_bid)))


def odds_of_sale(a: "Assumptions", rates: dict, years_in_default: float | None, paid_down: float | None) -> float:
    """Chance a still-listed parcel actually reaches the auction (a screening estimate, not a forecast)."""
    leave_age = rates.get(age_bucket(years_in_default) or "all", rates.get("all", {})).get("rate", 0.0) if rates else 0.0
    leave_paid = a.paid_down_leave_prob if (paid_down is not None and paid_down >= a.paid_down_threshold) else 0.0
    return float(max(0.0, min(1.0, (1 - leave_age) * (1 - leave_paid))))


def risk_from_odds(p_stay: float) -> str:
    return "high" if p_stay < 0.5 else "medium" if p_stay < 0.85 else "low"


def property_type(units: float | None, legal: str) -> str:
    if units is None or units < 1:
        return "no_residential_units"
    if units > 4:
        return "multi_unit_5plus"
    if units > 1:
        return "multi_unit_2_4"
    return "condo" if "CONDO" in (legal or "").upper() else "single_family"


def score(items: pd.DataFrame, snapshot: pd.DataFrame, fmr: pd.DataFrame, a: Assumptions,
          asof: date, status_history: pd.DataFrame | None = None, roll: pd.DataFrame | None = None,
          history: pd.DataFrame | None = None) -> pd.DataFrame:
    snap = snapshot.drop(columns=["auction"], errors="ignore").drop_duplicates("ain", keep="last")
    df = items.merge(snap, on="ain", how="left")
    rates = redemption_rates(roll)
    roll_by_ain = {} if roll is None else roll.drop_duplicates("ain").set_index("ain").to_dict("index")
    current_sale = str(items["auction"].iloc[0]) if len(items) else None
    hist = offered_before(history, list(df["ain"]), current_sale)
    hist_by_ain = hist.set_index("ain").to_dict("index") if history is not None else {}
    fmr_by_zip = fmr.set_index("zip").to_dict("index")
    changes = {}
    if status_history is not None and len(status_history):
        changes = status_history.groupby("ain").size().to_dict()

    out = []
    for r in df.to_dict("records"):
        bid = float(r["min_bid"])
        land, imp = _num(r.get("land_value")), _num(r.get("imp_value"))
        assessed = (land or 0) + (imp or 0) if (land is not None or imp is not None) else None
        units, beds = _num(r.get("num_units")), _num(r.get("num_beds"))
        sqft, year = _num(r.get("sqft_main")), _num(r.get("year_built"))
        zip5 = r.get("zip") if isinstance(r.get("zip"), str) else None
        if not zip5 and isinstance(r.get("situs_zip"), str):
            zip5 = r["situs_zip"][:5]

        if bool(r["improved"]) and not bool(r["vacant_lot"]):
            segment = "rental"
        elif bool(r["improved"]):
            segment = "review"
        else:
            segment = "land"

        row = {
            "auction": r["auction"], "item": r["item"], "ain": r["ain"], "segment": segment,
            "min_bid": bid, "property_address": r["property_address"], "zip": zip5,
            "location": r["location"], "tax_status": r.get("tax_status"),
            "year_built": year, "sqft_main": sqft, "sqft_lot": _num(r.get("sqft_lot")),
            "num_units": units, "num_beds": beds, "num_baths": _num(r.get("num_baths")),
            "land_value": land, "imp_value": imp, "assessed_value": assessed,
            "bid_to_assessed": bid / assessed if assessed else None,
            "excluded": None, "checklist": CHECKLIST,
        }
        rr = roll_by_ain.get(r["ain"])
        p_stay = None
        if rr is not None:
            on_roll, redeemed = bool(rr.get("on_roll")), bool(rr.get("redeemed"))
            yrs = _num(rr.get("years_in_default"))
            now = _num(rr.get("redemption_amount")) if (on_roll and not redeemed) else None
            pdn = paid_down_pct(now, bid)
            row.update({
                "roll_status": rr.get("roll_status") if isinstance(rr.get("roll_status"), str) else None,
                "years_in_default": yrs, "redemption_amount_now": now, "paid_down_pct": pdn,
                "redemption_date": rr.get("redemption_date") if isinstance(rr.get("redemption_date"), str) else None,
                "roll_redeemed": redeemed, "unpaid_years": _num(rr.get("unpaid_years")),
                "oldest_unpaid_year": _num(rr.get("oldest_unpaid_year")),
                "sale_ref": rr.get("sale_ref") if isinstance(rr.get("sale_ref"), str) else None,
                "deeded_year": _num(rr.get("deeded_year")),
            })
            p_stay = 0.0 if redeemed else odds_of_sale(a, rates, yrs, pdn)
            row["p_stay"] = p_stay
        hb = hist_by_ain.get(r["ain"])
        if hb is not None:
            row["offered_before"] = _num(hb.get("offered_before"))
            row["offered_sales"] = hb.get("offered_sales") if isinstance(hb.get("offered_sales"), str) else None
        if isinstance(r.get("fetch_error"), str):
            row["excluded"] = "enrichment_failed"
        elif r.get("tax_status") is None or (isinstance(r.get("tax_status"), float)):
            row["excluded"] = "not_enriched"
        elif str(r["tax_status"]).upper() == "CURRENT":
            row["excluded"] = "tax_current"
        elif rr is not None and row.get("roll_redeemed"):
            row["excluded"] = "redeemed_per_roll"
        elif segment == "review":
            row["excluded"] = "improved_flag_without_address"

        if segment == "land":
            lot = row["sqft_lot"]
            row["bid_per_lot_sqft"] = bid / lot if lot else None
        elif segment == "rental":
            row["property_type"] = property_type(units, r["legal_description"])
            if row["property_type"] in ("no_residential_units", "multi_unit_5plus") and not row["excluded"]:
                row["excluded"] = row["property_type"]
            fmr_row = fmr_by_zip.get(zip5)
            rent = None
            if not row["excluded"]:
                if fmr_row is None:
                    row["excluded"] = "no_fmr_for_zip"
                else:
                    rent = estimate_monthly_rent(units, beds, fmr_row, a.rent_factor)
                    if rent is None:
                        row["excluded"] = "no_bedroom_data"
            row["fmr_metro_default"] = bool(fmr_row.get("metro_default", False)) if fmr_row else None
            rehab = rehab_estimate(a, sqft, year)
            row["rehab_estimate"] = rehab
            row["rehab_risk"] = rehab_risk(year, land, imp)
            row["redemption_risk"] = risk_from_odds(p_stay) if p_stay is not None else redemption_risk(
                a, asof, r.get("last_transfer_date") if isinstance(r.get("last_transfer_date"), str) else None,
                assessed, bid, changes.get(r["ain"], 0),
            )
            if rent is not None:
                row["est_monthly_rent"] = rent
                row.update(income_metrics(a, rent, bid * a.bid_multiple, rehab))
                row["bid_headroom"] = row["max_bid_for_target"] / bid
        out.append(row)

    res = pd.DataFrame(out)
    for col in ("cap_rate", "est_monthly_rent"):
        if col not in res:
            res[col] = float("nan")
    if "p_stay" not in res:
        res["p_stay"] = float("nan")
    res["adj_cap_rate"] = res["cap_rate"] * res["p_stay"].where(res["p_stay"].notna(), 1.0)
    ranked = res[(res["segment"] == "rental") & res["excluded"].isna() & res["cap_rate"].notna()]
    res["rank_cap"] = ranked["cap_rate"].rank(ascending=False, method="first").astype("Int64")
    key = "adj_cap_rate" if a.rank_by == "adjusted" else "cap_rate"
    res["rank"] = ranked[key].rank(ascending=False, method="first").astype("Int64")
    return res.sort_values(["segment", "rank", "item"], na_position="last").reset_index(drop=True)


def latest_snapshot(auction_dir: Path) -> Path:
    files = sorted((auction_dir / "enrichment").glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"no snapshots in {auction_dir / 'enrichment'}; run `enrich` first")
    return files[-1]


def run_analysis(auction_dir: Path, fmr: pd.DataFrame, a: Assumptions,
                 snapshot_path: Path | None = None,
                 extra_summary: dict | None = None,
                 history_path: Path | None = None) -> tuple[Path, pd.DataFrame]:
    snapshot_path = snapshot_path or latest_snapshot(auction_dir)
    items = pd.read_csv(auction_dir / "items.csv", dtype={"ain": str, "zip": str, "zip4": str})
    snap = pd.read_csv(snapshot_path, dtype={"ain": str})
    hist_path = auction_dir / "status_history.csv"
    hist = pd.read_csv(hist_path, dtype=str) if hist_path.exists() else None
    asof = date.fromisoformat(snapshot_path.stem)
    roll_path = latest_roll(auction_dir)
    roll = read_roll(roll_path)
    history = read_history(history_path)
    res = score(items, snap, fmr, a, asof, hist, roll, history)

    out_dir = auction_dir / "analysis"
    out_dir.mkdir(exist_ok=True)
    res.to_csv(out_dir / "scored.csv", index=False)
    ranked = res[res["rank"].notna()]
    summary = {
        "auction": str(items["auction"].iloc[0]), "snapshot": snapshot_path.name,
        "assumptions": asdict(a), "items": len(res),
        "by_segment": res["segment"].value_counts().to_dict(),
        "excluded": res["excluded"].value_counts().to_dict(),
        "ranked": len(ranked),
        "median_cap_rate": None if ranked.empty else float(ranked["cap_rate"].median()),
        "above_target": int((ranked["cap_rate"] >= a.target_cap_rate).sum()),
    }
    if roll is not None:
        summary["roll"] = {"snapshot": roll_path.name, "rates": redemption_rates(roll),
                           "on_roll": int(roll["on_roll"].astype(bool).sum()),
                           "redeemed": int(roll["redeemed"].astype(bool).sum())}
    if history is not None:
        summary["history_sales"] = sorted(history["sale"].unique())
    summary.update(extra_summary or {})
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return out_dir / "scored.csv", res
