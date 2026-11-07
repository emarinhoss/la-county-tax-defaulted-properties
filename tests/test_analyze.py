from datetime import date

import pandas as pd
import pytest

from lacounty_tax.analyze import (
    Assumptions, estimate_monthly_rent, fmr_for_bedrooms, income_metrics, property_type,
    redemption_risk, rehab_estimate, rehab_risk, run_analysis, score,
)
from lacounty_tax.cli import main
from lacounty_tax.reference import normalize_safmr

A = Assumptions()
FMR = {"fmr_0br": 1500, "fmr_1br": 1800, "fmr_2br": 2300, "fmr_3br": 3000, "fmr_4br": 3500}
ASOF = date(2026, 11, 6)


def test_fmr_bedroom_scaling():
    assert fmr_for_bedrooms(FMR, 3) == 3000
    assert fmr_for_bedrooms(FMR, 5) == pytest.approx(3500 * 1.15)
    assert fmr_for_bedrooms(FMR, 6) == pytest.approx(3500 * 1.30)
    assert fmr_for_bedrooms(FMR, -1) == 1500


def test_rent_for_multi_unit_splits_bedrooms():
    assert estimate_monthly_rent(1, 3, FMR) == 3000
    assert estimate_monthly_rent(2, 4, FMR) == 2 * 2300       # 2 BR each
    assert estimate_monthly_rent(1, 3, FMR, factor=1.1) == pytest.approx(3300)
    assert estimate_monthly_rent(0, 3, FMR) is None
    assert estimate_monthly_rent(1, float("nan"), FMR) is None


def test_income_metrics_hand_computed():
    a = Assumptions(vacancy_rate=0.0, opex_pct=0.0, property_tax_rate=0.0, closing_cost_pct=0.0)
    m = income_metrics(a, 1000, bid=100_000, rehab=20_000)
    assert m["noi"] == 12_000 and m["all_in_cost"] == 120_000
    assert m["cap_rate"] == pytest.approx(0.10)
    a2 = Assumptions(target_cap_rate=0.08)
    m2 = income_metrics(a2, 2000, bid=50_000, rehab=15_000)
    # at the computed max bid the cap rate equals the target exactly
    m3 = income_metrics(a2, 2000, bid=m2["max_bid_for_target"], rehab=15_000)
    assert m3["cap_rate"] == pytest.approx(0.08)


def test_max_bid_never_negative():
    assert income_metrics(A, 100, 10_000, rehab=500_000)["max_bid_for_target"] == 0.0


def test_rehab_and_risk_proxies():
    assert rehab_estimate(A, 1000, 1990) == 15_000
    assert rehab_estimate(A, 1000, 1965) == 30_000
    assert rehab_estimate(A, 1000, 1920) == 45_000
    assert rehab_estimate(A, 100, 1990) == A.rehab_min
    assert rehab_estimate(A, None, None) == A.rehab_min
    assert rehab_risk(1990, 100, 300) == "low"
    assert rehab_risk(1920, 100, 40) == "high"
    assert rehab_risk(None, None, None) == "medium"


def test_redemption_risk_signals():
    old, recent = "2015-01-01", "2026-03-01"
    assert redemption_risk(A, ASOF, old, 50_000, 10_000, 1) == "low"
    assert redemption_risk(A, ASOF, recent, 50_000, 10_000, 1) == "medium"
    assert redemption_risk(A, ASOF, recent, 500_000, 10_000, 1) == "high"
    assert redemption_risk(A, ASOF, old, 50_000, 10_000, 3) == "medium"


def test_property_type():
    assert property_type(1, "TRACT 1 LOT 2") == "single_family"
    assert property_type(1, "TR=1 CONDOMINIUM UNIT 5") == "condo"
    assert property_type(3, "") == "multi_unit_2_4"
    assert property_type(8, "") == "multi_unit_5plus"
    assert property_type(0, "") == "no_residential_units"
    assert property_type(None, "") == "no_residential_units"


def make_inputs():
    items = pd.DataFrame([
        dict(auction="T", item=1, ain="1", min_bid=20_000, improved=True, vacant_lot=False, legal_description="LOT 1",
             location="CITY-A", property_address="1 A ST", zip="90001"),
        dict(auction="T", item=2, ain="2", min_bid=60_000, improved=True, vacant_lot=False, legal_description="LOT 2",
             location="CITY-A", property_address="2 A ST", zip="90001"),
        dict(auction="T", item=3, ain="3", min_bid=5_000, improved=False, vacant_lot=True, legal_description="LOT 3",
             location="CITY-A", property_address="VACANT LOT", zip=None),
        dict(auction="T", item=4, ain="4", min_bid=9_000, improved=True, vacant_lot=False, legal_description="LOT 4",
             location="CITY-A", property_address="4 A ST", zip="90001"),
        dict(auction="T", item=5, ain="5", min_bid=9_000, improved=True, vacant_lot=False, legal_description="LOT 5",
             location="CITY-A", property_address="5 A ST", zip="99999"),
        dict(auction="T", item=6, ain="6", min_bid=9_000, improved=True, vacant_lot=True, legal_description="LOT 6",
             location="CITY-A", property_address="VACANT LOT", zip=None),
        dict(auction="T", item=7, ain="7", min_bid=9_000, improved=True, vacant_lot=False, legal_description="LOT 7",
             location="CITY-A", property_address="7 A ST", zip="90001"),
    ])
    base = dict(tax_status="DEFAULTED", num_units=1, num_beds=3, num_baths=2, sqft_main=1200, sqft_lot=5000,
                year_built=1975, land_value=100_000, imp_value=120_000, fetch_error=None,
                last_transfer_date="2010-01-01")
    snap = pd.DataFrame([
        dict(ain="1", **base),
        dict(ain="2", **{**base, "num_beds": 2}),
        dict(ain="3", **{**base, "sqft_lot": 2500}),
        dict(ain="4", **{**base, "tax_status": "CURRENT"}),
        dict(ain="5", **base),
        dict(ain="6", **base),
        dict(ain="7", **{**base, "num_units": 0}),
    ])
    fmr = pd.DataFrame([{"zip": "90001", "area_name": "X", "fiscal_year": 2026,
                         **{k: v for k, v in FMR.items()}}])
    return items, snap, fmr


def test_score_segments_exclusions_and_ranking():
    items, snap, fmr = make_inputs()
    res = score(items, snap, fmr, A, ASOF).set_index("item")
    assert res.loc[1, "segment"] == "rental" and res.loc[3, "segment"] == "land"
    assert res.loc[6, "segment"] == "review" and res.loc[6, "excluded"] == "improved_flag_without_address"
    assert res.loc[4, "excluded"] == "tax_current"
    assert res.loc[5, "excluded"] == "no_fmr_for_zip"
    assert res.loc[7, "excluded"] == "no_residential_units"
    assert res.loc[3, "bid_per_lot_sqft"] == pytest.approx(2.0)
    # item 1: cheaper and 3BR rent -> best cap rate, rank 1; item 2 rank 2
    assert res.loc[1, "est_monthly_rent"] == 3000 and res.loc[2, "est_monthly_rent"] == 2300
    assert res.loc[1, "rank"] == 1 and res.loc[2, "rank"] == 2
    assert res.loc[1, "cap_rate"] > res.loc[2, "cap_rate"]
    assert res[res["rank"].notna()].shape[0] == 2
    assert res.loc[1, "bid_headroom"] == pytest.approx(res.loc[1, "max_bid_for_target"] / 20_000)


def test_bid_multiple_lowers_cap_rate():
    items, snap, fmr = make_inputs()
    base = score(items, snap, fmr, A, ASOF).set_index("item").loc[1, "cap_rate"]
    higher = score(items, snap, fmr, Assumptions(bid_multiple=2.0), ASOF).set_index("item").loc[1, "cap_rate"]
    assert higher < base


def test_unenriched_and_failed_rows_are_excluded():
    items, snap, fmr = make_inputs()
    snap = snap[snap.ain != "1"].copy()
    snap.loc[snap.ain == "2", "fetch_error"] = "HTTP 500"
    res = score(items, snap, fmr, A, ASOF).set_index("item")
    assert res.loc[1, "excluded"] == "not_enriched" and res.loc[2, "excluded"] == "enrichment_failed"


def test_assumptions_toml(tmp_path):
    f = tmp_path / "a.toml"
    f.write_text("target_cap_rate = 0.1\nvacancy_rate = 0.08\n")
    a = Assumptions.from_toml(f)
    assert a.target_cap_rate == 0.1 and a.closing_cost_pct == 0.03
    f.write_text("nonsense = 1\n")
    with pytest.raises(ValueError, match="unknown"):
        Assumptions.from_toml(f)


def test_normalize_safmr_handles_hud_style_headers():
    raw = pd.DataFrame({
        "ZIP\nCode": ["90001", "1001", "93550"],
        "HUD Metro Fair Market Rent Area Name": ["LA", "Springfield", "LA"],
        "SAFMR\n0BR": ["$1,500", "900", "1400"],
        "SAFMR\n0BR -\n90%\nPayment\nStandard": ["1", "1", "1"],
        "SAFMR\n1BR": ["1800", "1000", "1700"], "SAFMR\n2BR": ["2300", "1200", "2100"],
        "SAFMR\n3BR": ["3000", "1500", "2800"], "SAFMR\n4BR": ["3500", "1800", "3300"],
    })
    out = normalize_safmr(raw, 2026)
    assert list(out["zip"]) == ["90001", "93550"]            # non-LA-range zip dropped
    assert out.loc[0, "fmr_0br"] == 1500 and out.loc[0, "fiscal_year"] == 2026
    with pytest.raises(ValueError, match="Unrecognised"):
        normalize_safmr(pd.DataFrame({"a": [1]}))


def test_run_analysis_and_cli(tmp_path, capsys):
    items, snap, fmr = make_inputs()
    d = tmp_path / "data" / "T"
    (d / "enrichment").mkdir(parents=True)
    items.to_csv(d / "items.csv", index=False)
    snap.to_csv(d / "enrichment" / "2026-11-06.csv", index=False)
    fmr.to_csv(tmp_path / "fmr.csv", index=False)
    rc = main(["analyze", "T", "--data-dir", str(tmp_path / "data"), "--fmr", str(tmp_path / "fmr.csv")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "7 items scored, 2 ranked" in out and "1 A ST" in out
    scored = pd.read_csv(d / "analysis" / "scored.csv", dtype={"ain": str})
    assert len(scored) == 7 and (d / "analysis" / "summary.json").exists()
    assert main(["analyze", "T", "--data-dir", str(tmp_path / "nope"), "--fmr", str(tmp_path / "fmr.csv")]) == 2


TEXT = """Los Angeles-Long Beach-Glendale, CA HUD Metro FMR Area Small Area FMRs By Unit Bedrooms
ZIP Code	Efficiency	One-Bedroom	Two-Bedroom	Three-Bedroom	Four-Bedroom
90001	$1,690	$1,890	$2,330	$2,960	$3,310
90004	$2,320	$2,590	$3,190	$4,050	$4,530
""" + "".join(f"9{i:04d}\t$2,150\t$2,400\t$2,960\t$3,760\t$4,200\n" for i in range(1100, 1112)) + "10001\t$1\t$2\t$3\t$4\t$5\n"


def test_parse_safmr_text_and_metro_default_flag():
    from lacounty_tax.reference import parse_safmr_text
    t = parse_safmr_text(TEXT, 2027)
    assert len(t) == 14                                    # 10001 is outside the LA ZIP range
    r = t.set_index("zip")
    assert r.loc["90001", "fmr_2br"] == 2330 and r.loc["90001", "fiscal_year"] == 2027
    assert not r.loc["90001", "metro_default"] and r.loc["91100", "metro_default"]
    with pytest.raises(ValueError, match="no 'ZIP"):
        parse_safmr_text("nothing here")


def test_cli_load_fmr_text(tmp_path):
    src = tmp_path / "safmr.txt"
    src.write_text(TEXT)
    out = tmp_path / "ref" / "fmr.csv"
    assert main(["load-fmr", str(src), "--year", "2027", "--out", str(out)]) == 0
    assert pd.read_csv(out, dtype={"zip": str}).shape[0] == 14


# ---------------------------------------------------------------- tax roll signals and the risk-adjusted ranking
from lacounty_tax.analyze import age_bucket, odds_of_sale, paid_down_pct, redemption_rates, risk_from_odds


def roll_row(ain, years=15, amount=20_000.0, redeemed=False, on_roll=True, status="defaulted", rdate=None):
    return dict(ain=ain, on_roll=on_roll, roll_status=status if on_roll else None, default_count=1, defaulted_year=2026 - years if years is not None else None,
                years_in_default=years, redemption_amount=amount, compute_month="102026", redemption_date=rdate, redeemed=redeemed,
                unpaid_years=3, oldest_unpaid_year=2020, sale_ref=None, deeded_year=None)


def test_age_buckets():
    assert [age_bucket(y) for y in (0, 1, 4, 5, 9, 10, 19, 20, 55)] == ["0", "1-4", "1-4", "5-9", "5-9", "10-19", "10-19", "20+", "20+"]
    assert age_bucket(None) is None and age_bucket(float("nan")) is None and age_bucket(-3) == "0"


def test_redemption_rates_by_bucket_pool_small_groups_and_skip_removed_parcels():
    rows = [roll_row(f"a{i}", years=12, redeemed=i < 2) for i in range(40)]            # 10-19 yrs: 2 of 40 redeemed
    rows += [roll_row(f"b{i}", years=7, redeemed=i < 5) for i in range(20)]             # 5-9 yrs: 5 of 20
    rows += [roll_row(f"c{i}", years=25, redeemed=i == 0) for i in range(5)]            # 20+: only 5 parcels -> pooled
    rows += [roll_row("gone", years=None, on_roll=False, redeemed=True)]               # removed: no default year, not counted
    r = redemption_rates(pd.DataFrame(rows))
    assert r["all"]["n"] == 65 and r["all"]["redeemed"] == 8
    assert r["10-19"]["rate"] == pytest.approx(2 / 40) and r["5-9"]["rate"] == pytest.approx(5 / 20)
    assert r["20+"]["pooled"] and r["20+"]["rate"] == pytest.approx(8 / 65)              # too few parcels: falls back to the overall rate
    assert redemption_rates(None) == {} and redemption_rates(pd.DataFrame()) == {}


def test_paid_down_and_odds_of_sale_arithmetic():
    assert paid_down_pct(2_000, 20_000) == pytest.approx(0.9) and paid_down_pct(25_000, 20_000) == 0.0 and paid_down_pct(None, 5) is None
    rates = {"all": {"rate": 0.08}, "10-19": {"rate": 0.02}, "5-9": {"rate": 0.24}}
    assert odds_of_sale(A, rates, 12, 0.0) == pytest.approx(0.98)                         # old default, nothing paid
    assert odds_of_sale(A, rates, 7, 0.0) == pytest.approx(0.76)
    assert odds_of_sale(A, rates, 12, 0.9) == pytest.approx(0.98 * (1 - A.paid_down_leave_prob))   # mostly paid down
    assert odds_of_sale(A, rates, 12, 0.4) == pytest.approx(0.98)                         # below the threshold: no extra penalty
    assert odds_of_sale(A, rates, None, None) == pytest.approx(0.92)                      # unknown age: overall rate
    assert odds_of_sale(A, {}, 12, None) == 1.0
    assert [risk_from_odds(p) for p in (0.1, 0.49, 0.5, 0.84, 0.9)] == ["high", "high", "medium", "medium", "low"]


def roll_for(items, **over):
    rows = [roll_row(a, years=15, amount=float(m) * 1.0) for a, m in zip(items["ain"], items["min_bid"])]
    for r in rows:
        r.update(over.get(r["ain"], {}))
    return pd.DataFrame(rows)


def test_paid_down_parcel_drops_in_the_adjusted_ranking_but_keeps_its_raw_rank():
    items, snap, fmr = make_inputs()
    roll = roll_for(items, **{"1": {"redemption_amount": 1_000.0}})                      # item 1 (min bid 20,000) has paid 95% already
    res = score(items, snap, fmr, A, ASOF, roll=roll).set_index("item")
    assert res.loc[1, "paid_down_pct"] == pytest.approx(0.95) and res.loc[1, "p_stay"] < 0.3 and res.loc[1, "redemption_risk"] == "high"
    assert res.loc[2, "p_stay"] > 0.9 and res.loc[2, "redemption_risk"] == "low"
    assert res.loc[1, "rank_cap"] == 1 and res.loc[2, "rank_cap"] == 2                    # by raw cap rate nothing changed
    assert res.loc[1, "rank"] == 2 and res.loc[2, "rank"] == 1                            # but the risk-adjusted order flipped
    assert res.loc[1, "adj_cap_rate"] == pytest.approx(res.loc[1, "cap_rate"] * res.loc[1, "p_stay"])
    cap_only = score(items, snap, fmr, Assumptions(rank_by="cap"), ASOF, roll=roll).set_index("item")
    assert cap_only.loc[1, "rank"] == 1                                                   # rank_by = "cap" restores the old order


def test_roll_redemption_excludes_a_parcel_the_assessor_still_lists():
    items, snap, fmr = make_inputs()
    roll = roll_for(items, **{"2": {"redeemed": True, "redemption_date": "2026-10-24", "redemption_amount": 0.0}})
    res = score(items, snap, fmr, A, ASOF, roll=roll).set_index("item")
    assert res.loc[2, "excluded"] == "redeemed_per_roll" and res.loc[2, "p_stay"] == 0.0 and res.loc[2, "redemption_date"] == "2026-10-24"
    assert pd.isna(res.loc[2, "rank"]) and res.loc[1, "rank"] == 1


def test_offered_before_and_roll_fields_reach_the_scored_table():
    items, snap, fmr = make_inputs()
    hist = pd.DataFrame({"sale": ["2025B", "2025A", "T"], "ain": ["1", "1", "1"]})
    res = score(items, snap, fmr, A, ASOF, roll=roll_for(items, **{"1": {"sale_ref": "2027A #3347", "deeded_year": 2025}}), history=hist).set_index("item")
    assert res.loc[1, "offered_before"] == 2 and res.loc[1, "offered_sales"] == "2025A,2025B"   # the current auction ("T") is not counted
    assert res.loc[2, "offered_before"] == 0
    assert res.loc[1, "years_in_default"] == 15 and res.loc[1, "sale_ref"] == "2027A #3347" and res.loc[1, "deeded_year"] == 2025


def test_without_a_roll_nothing_changes():
    items, snap, fmr = make_inputs()
    res = score(items, snap, fmr, A, ASOF).set_index("item")
    assert res["p_stay"].isna().all() and (res.loc[[1, 2], "adj_cap_rate"] == res.loc[[1, 2], "cap_rate"]).all()
    assert res.loc[1, "rank"] == 1 and res.loc[1, "redemption_risk"] in ("low", "medium", "high")     # the old heuristic still applies


def test_run_analysis_reads_the_latest_roll_and_history(tmp_path):
    items, snap, fmr = make_inputs()
    d = tmp_path / "data" / "T"
    (d / "enrichment").mkdir(parents=True)
    (d / "roll").mkdir()
    items.to_csv(d / "items.csv", index=False)
    snap.to_csv(d / "enrichment" / "2026-11-06.csv", index=False)
    roll_for(items).to_csv(d / "roll" / "2026-11-07.csv", index=False)
    pd.DataFrame({"sale": ["2025B"], "ain": ["1"]}).to_csv(tmp_path / "hist.csv", index=False)
    _, res = run_analysis(d, fmr, A, history_path=tmp_path / "hist.csv")
    assert res["p_stay"].notna().any() and res.set_index("item").loc[1, "offered_before"] == 1
    import json as _json
    summary = _json.loads((d / "analysis" / "summary.json").read_text())
    assert summary["roll"]["snapshot"] == "2026-11-07.csv" and summary["roll"]["rates"]["all"]["n"] == 7 and summary["history_sales"] == ["2025B"]
