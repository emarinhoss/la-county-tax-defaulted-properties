import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import lacounty_tax.cli as cli
from lacounty_tax.cli import main
from lacounty_tax.meta import AuctionMeta, is_active, load_meta, save_meta
from lacounty_tax.refresh import find_auctions, refresh_auction
from test_assessor import Resp, client, ok_handler        # reuse the fake Assessor API

ROOT = Path(__file__).parents[1]
FMR = ROOT / "data" / "reference" / "hud_safmr.csv"


def make_auction(base, name="T1", n=3, zip5="90001"):
    d = base / name
    d.mkdir(parents=True)
    pd.DataFrame([dict(auction=name, item=i, ain=f"00000000{i:02d}", min_bid=20_000 + i, improved=True,
                       nsb=500, legal_description="LOT 1", location="CITY-A", property_address=f"{i} A ST",
                       zip=zip5, zip4=None, vacant_lot=False, page=1) for i in range(n)]).to_csv(d / "items.csv", index=False)
    return d


def test_meta_roundtrip_and_activity(tmp_path):
    assert is_active(None, date(2030, 1, 1))
    save_meta(tmp_path, AuctionMeta("T1", "2026-12-05", "http://x"))
    m = load_meta(tmp_path)
    assert m.sale_date == "2026-12-05" and m.book_url == "http://x"
    assert is_active(m, date(2026, 12, 5)) and not is_active(m, date(2026, 12, 6))
    assert is_active(AuctionMeta("T1"), date(2099, 1, 1))
    with pytest.raises(ValueError):
        save_meta(tmp_path, AuctionMeta("T1", "not-a-date"))


def test_refresh_enriches_and_scores(tmp_path):
    d = make_auction(tmp_path)
    r = refresh_auction(d, client(ok_handler), cache_dir=tmp_path / "c", fmr_path=FMR, config_path=None,
                        today=date(2026, 11, 6))
    assert r.status == "refreshed" and "3 parcels pulled" in r.detail
    assert (d / "analysis" / "scored.csv").exists() and any((d / "enrichment").glob("*.csv"))


def test_refresh_without_fmr_still_enriches(tmp_path):
    d = make_auction(tmp_path)
    r = refresh_auction(d, client(ok_handler), cache_dir=tmp_path / "c", fmr_path=tmp_path / "missing.csv",
                        config_path=None, today=date(2026, 11, 6))
    assert r.status == "enriched_no_fmr" and not (d / "analysis").exists()


def test_frozen_after_sale_date_unless_forced(tmp_path):
    d = make_auction(tmp_path)
    save_meta(d, AuctionMeta("T1", "2026-11-01"))
    c = client(ok_handler)
    kw = dict(cache_dir=tmp_path / "c", fmr_path=FMR, config_path=None, today=date(2026, 11, 6))
    r = refresh_auction(d, c, **kw)
    assert r.status == "frozen" and c.session.calls == [] and not (d / "enrichment").exists()
    assert refresh_auction(d, c, force=True, **kw).status == "refreshed"


def test_blocked_writes_nothing(tmp_path):
    d = make_auction(tmp_path)
    c = client(lambda e, a, n: Resp(403, text="<html>", ctype="text/html"))
    r = refresh_auction(d, c, cache_dir=tmp_path / "c", fmr_path=FMR, config_path=None, today=date(2026, 11, 6))
    assert r.status == "blocked" and not (d / "enrichment").exists() and not (d / "analysis").exists()


def test_find_auctions_filters(tmp_path):
    make_auction(tmp_path, "A1"); make_auction(tmp_path, "B1")
    (tmp_path / "empty").mkdir()
    assert [d.name for d in find_auctions(tmp_path)] == ["A1", "B1"]
    assert [d.name for d in find_auctions(tmp_path, ["B1"])] == ["B1"]


def run_cli(tmp_path, monkeypatch, handler, *extra):
    monkeypatch.setattr(cli, "AssessorClient", lambda **k: client(handler))
    return main(["refresh", "--data-dir", str(tmp_path / "data"), "--cache-dir", str(tmp_path / "c"),
                 "--fmr", str(FMR), "--config", "", "--today", "2026-11-06", "--no-geocode", *extra])


def test_cli_refresh_ok_and_summary(tmp_path, monkeypatch, capsys):
    make_auction(tmp_path / "data")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert run_cli(tmp_path, monkeypatch, ok_handler) == 0
    assert "T1: refreshed" in capsys.readouterr().out
    assert "| T1 | refreshed |" in summary.read_text()


def test_cli_refresh_blocked_returns_3_with_annotation(tmp_path, monkeypatch, capsys):
    make_auction(tmp_path / "data", "A1"); make_auction(tmp_path / "data", "B1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc = run_cli(tmp_path, monkeypatch, lambda e, a, n: Resp(403, text="<html>", ctype="text/html"))
    out = capsys.readouterr().out
    assert rc == 3 and "::warning" in out
    assert "A1: blocked" in out and "B1" not in out          # stopped after the first block


def test_cli_refresh_nothing_to_do(tmp_path, monkeypatch, capsys):
    (tmp_path / "data").mkdir()
    assert run_cli(tmp_path, monkeypatch, ok_handler) == 0
    assert "nothing to refresh" in capsys.readouterr().out


def test_parse_records_meta(tmp_path):
    pdf = ROOT / "tests" / "fixtures" / "2026B-sample-pages.pdf"
    out = tmp_path / "2026B" / "items.csv"
    assert main(["parse", str(pdf), "--out", str(out), "--sale-date", "2026-12-05", "--book-url", "http://b"]) == 0
    assert json.loads((tmp_path / "2026B" / "meta.json").read_text()) == {
        "auction": "2026B", "sale_date": "2026-12-05", "book_url": "http://b"}
