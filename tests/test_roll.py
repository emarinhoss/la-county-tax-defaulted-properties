import re
from datetime import date

import pandas as pd
import pytest

import lacounty_tax.cli as cli
import lacounty_tax.roll as roll_mod
from lacounty_tax.assessor import BlockedError
from lacounty_tax.cli import main
from lacounty_tax.geo import GisClient, GisError
from lacounty_tax.history import BASE, discover_sales, fetch_sale, offered_before, pull_history, read_history, write_history
from lacounty_tax.roll import (ROLL_URL, fetch_roll_rows, latest_roll, pull_roll, read_roll, sale_digits, summarize_roll)
from test_assessor import Resp
from test_refresh import make_auction

A, B, C, D, E, G = "0000000001", "0000000002", "0000000003", "0000000004", "0000000005", "0000000099"
ASOF = date(2026, 11, 7)


def hdr(oid, group, status="2", year="2010", amount=5000.0, rdate=None, count="001"):
    return dict(OBJECTID=oid, Record_Type="01", Group_Number=group, Default_Count=count, Tax_Status=status, Defaulted_Year=year,
                Redemption_Amount=amount, Redemption_Date=rdate, Compute_MonthYear="000000" if rdate else "102026")


def line(oid, group, ain, year, tax=100.0, pen=0.0):
    return dict(OBJECTID=oid, Record_Type="02", Group_Number=group, Assessment_AIN=ain, Assessment_Year=str(year), Tax_Balance=tax, Penalty_Balance=pen)


ROWS = [
    hdr(1, A), line(2, A, A, 2010), line(3, A, A, 2011), line(4, A, A, 2012, tax=0.0),               # A: 2 unpaid years
    dict(OBJECTID=5, Record_Type="04", Group_Number=A, AIN=A, Sale_Number="2027A", Item_Number="3347", Deeded_Year="2025"),
    hdr(6, B, rdate="09/25/2026", amount=0.0),                                                       # B: redeemed, still listed
    hdr(7, G, year="1999"), line(8, G, D, 1999), line(9, G, G, 2000),                                # D: part of group G
    hdr(10, E, status="1", year="2026", amount=1574.49, count="003"), line(11, E, E, 2025),         # E: delinquent
    dict(OBJECTID=12, Record_Type="03", Group_Number=A, Assessment_AIN=A, DA_Account_Number="x"),   # type 03 must never be requested
]


class FakeRoll:
    def __init__(self, rows=ROWS, blocked=False):
        self.rows, self.blocked, self.calls, self.headers = rows, blocked, [], {}

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append(params)
        if self.blocked:
            return Resp(403, text="<html>bot</html>", ctype="text/html")
        assert url == ROLL_URL
        where = params["where"]
        m = re.match(r"(\w+) IN \(([^)]*)\)(?: AND (.*))?", where)
        field, vals, rest = m.group(1), {v.strip("'") for v in m.group(2).split(",")}, m.group(3) or ""
        types = set(re.findall(r"'(\d\d)'", rest)) if "IN" in rest else {re.search(r"= '(\d\d)'", rest).group(1)}
        hit = [r for r in self.rows if str(r.get(field)) in vals and r["Record_Type"] in types]
        off, n = int(params.get("resultOffset", 0)), int(params.get("resultRecordCount", 2000))
        page = hit[off:off + n]
        return Resp(data={"features": [{"attributes": r} for r in page], "exceededTransferLimit": off + n < len(hit)})


def client(fake):
    return GisClient(None, session=fake, lat_field="", lon_field="", min_interval=0, backoff=0, sleep=lambda s: None, retries=1)


def test_fetch_follows_paging_finds_grouped_parcels_and_never_asks_for_type_03(monkeypatch):
    monkeypatch.setattr(roll_mod, "PAGE", 2)
    fake = FakeRoll()
    rows = fetch_roll_rows(client(fake), [A, B, C, D, E])
    assert set(rows["OBJECTID"]) == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}          # everything except the type-03 row
    assert not (rows["Record_Type"] == "03").any()
    assert any(c["resultOffset"] != "0" for c in fake.calls)                        # paged
    assert any("Assessment_AIN IN" in c["where"] for c in fake.calls)               # pass 2 looked for D by its assessment line


def test_summary_fields_for_each_kind_of_parcel():
    s = summarize_roll(fetch_roll_rows(client(FakeRoll()), [A, B, C, D, E]), [A, B, C, D, E], ASOF).set_index("ain")
    a = s.loc[A]
    assert (a.on_roll, a.redeemed, a.roll_status, a.defaulted_year, a.years_in_default) == (True, False, "defaulted", 2010, 16)
    assert a.redemption_amount == 5000 and a.unpaid_years == 2 and a.oldest_unpaid_year == 2010 and a.sale_ref == "2027A #3347" and a.deeded_year == 2025
    b = s.loc[B]
    assert b.on_roll and b.redeemed and b.redemption_date == "2026-09-25"
    c = s.loc[C]
    assert (c.on_roll, c.redeemed) == (False, True) and c.years_in_default is None or pd.isna(c.years_in_default)   # removed from the roll
    d = s.loc[D]
    assert d.on_roll and not d.redeemed and d.years_in_default == 27 and d.unpaid_years == 1          # via group G, only D's own lines
    e = s.loc[E]
    assert e.roll_status == "delinquent" and e.years_in_default == 0 and e.default_count == 3


def test_summary_of_an_empty_roll_marks_everything_removed():
    s = summarize_roll(pd.DataFrame(), [A, B], ASOF)
    assert list(s["on_roll"]) == [False, False] and list(s["redeemed"]) == [True, True]


def test_pull_roll_writes_a_dated_snapshot_and_latest_finds_it(tmp_path):
    d = make_auction(tmp_path, n=3)
    ains = list(pd.read_csv(d / "items.csv", dtype={"ain": str})["ain"])
    rows = [hdr(i + 1, a) for i, a in enumerate(ains[:2])]                          # two of three on the roll
    out, s = pull_roll(d, client(FakeRoll(rows)), today=ASOF)
    assert out == d / "roll" / "2026-11-07.csv" and latest_roll(d) == out
    back = read_roll(out)
    assert list(back["ain"]) == ains and list(back["on_roll"]) == [True, True, False]
    assert read_roll(None) is None and read_roll(tmp_path / "nope.csv") is None


def test_sale_digits_normalises_ain_formats():
    assert sale_digits("2006-013-023") == "2006013023" and sale_digits(2006013023) == "2006013023" and sale_digits("123") == "0000000123"


def test_cli_pull_roll_exit_codes(tmp_path, monkeypatch, capsys):
    make_auction(tmp_path / "data", "T1", n=3)
    monkeypatch.setattr(cli, "GisClient", lambda *a, **k: client(FakeRoll([hdr(1, "0000000000")])))
    assert main(["pull-roll", "T1", "--data-dir", str(tmp_path / "data")]) == 0
    assert "parcels on the roll" in capsys.readouterr().out
    monkeypatch.setattr(cli, "GisClient", lambda *a, **k: client(FakeRoll(blocked=True)))
    assert main(["pull-roll", "T1", "--data-dir", str(tmp_path / "data")]) == 3
    assert main(["pull-roll", "NOPE", "--data-dir", str(tmp_path / "data")]) == 2


# ---------------------------------------------------------------- history of earlier books
class FakeDirectory:
    """An org with three per-sale services (one with two layers) plus noise."""
    def __init__(self):
        self.calls, self.headers = [], {}
        self.layers = {
            "2025B_Tax_Defaulted_Properties": {0: ([("OBJECTID", "esriFieldTypeOID"), ("APN_1", "esriFieldTypeString"), ("ITEM", "esriFieldTypeString"), ("MIN_BID", "esriFieldTypeDouble")],
                                                   [{"OBJECTID": 1, "APN_1": "0000000001", "ITEM": "1", "MIN_BID": 500.0}, {"OBJECTID": 2, "APN_1": "0000000002", "ITEM": "2", "MIN_BID": 900.0}])},
            "TAX_DEFAULT_PROPERTIES_2024A": {0: ([("OBJECTID", "esriFieldTypeOID"), ("Note", "esriFieldTypeString")], []),            # no AIN: skipped
                                             2: ([("OBJECTID_1", "esriFieldTypeOID"), ("AIN", "esriFieldTypeString"), ("ITEM", "esriFieldTypeString")],
                                                 [{"OBJECTID_1": 1, "AIN": "0000000001", "ITEM": "9"}, {"OBJECTID_1": 2, "AIN": "0000000003", "ITEM": "10"},
                                                  {"OBJECTID_1": 3, "AIN": "0000000004", "ITEM": "11"}])},
            "2022B_Tax_Defaulted_Properties": {0: ([("OBJECTID", "esriFieldTypeOID"), ("AIN_MB_PG_PCL", "esriFieldTypeString")],
                                                   [{"OBJECTID": 1, "AIN_MB_PG_PCL": "0000-000-002"}])},
            "Parcel_Roll_2025A": {0: ([("AIN", "esriFieldTypeString")], [])},                                                       # not a tax-default service
        }

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        rest = url[len(BASE):].strip("/")
        if rest == "":
            return Resp(data={"services": [{"name": n, "type": "FeatureServer"} for n in self.layers] + [{"name": "X_2025A_Tax_Default", "type": "MapServer"}]})
        parts = rest.split("/")
        svc = self.layers[parts[0]]
        if len(parts) == 2:
            return Resp(data={"layers": [{"id": i, "name": f"L{i}"} for i in svc]})
        fields, rows = svc[int(parts[2])]
        if len(parts) == 3:
            return Resp(data={"fields": [{"name": n, "type": t} for n, t in fields]})
        if params.get("returnCountOnly"):
            return Resp(data={"count": len(rows)})
        assert params.get("orderByFields") in {f for f, t in fields if t == "esriFieldTypeOID"}      # uses the layer's own OID field name
        off, n = int(params["resultOffset"]), int(params["resultRecordCount"])
        return Resp(data={"features": [{"attributes": r} for r in rows[off:off + n]]})


def test_discover_picks_the_ain_layer_of_each_tax_default_service():
    found = discover_sales(client(FakeDirectory()))
    assert found == {"2022B": ("2022B_Tax_Defaulted_Properties", 0), "2024A": ("TAX_DEFAULT_PROPERTIES_2024A", 2),
                     "2025B": ("2025B_Tax_Defaulted_Properties", 0)}


def test_pull_history_normalises_ains_and_keeps_item_and_bid(tmp_path):
    h = pull_history(client(FakeDirectory()))
    assert sorted(h["sale"].unique()) == ["2022B", "2024A", "2025B"] and len(h) == 6
    assert set(h[h.sale == "2022B"]["ain"]) == {"0000000002"}                      # '0000-000-002' -> digits
    assert h[(h.sale == "2025B") & (h.ain == "0000000002")].iloc[0].min_bid == 900.0
    only = pull_history(client(FakeDirectory()), only=["2024A"])
    assert set(only["sale"]) == {"2024A"}
    path = write_history(h, tmp_path / "ref" / "h.csv")
    back = read_history(path)
    assert len(back) == 6 and back["ain"].str.len().eq(10).all() and read_history(None) is None


def test_offered_before_counts_earlier_books_but_not_the_current_one():
    h = pd.DataFrame({"sale": ["2025B", "2024A", "2024A", "2026B"], "ain": ["0000000001", "0000000001", "0000000002", "0000000001"]})
    o = offered_before(h, ["0000000001", "0000000002", "0000000009"], "2026B").set_index("ain")
    assert o.loc["0000000001", "offered_before"] == 2 and o.loc["0000000001", "offered_sales"] == "2024A,2025B"   # 2026B itself ignored
    assert o.loc["0000000002", "offered_before"] == 1 and o.loc["0000000009", "offered_before"] == 0
    assert offered_before(None, ["0000000001"]).iloc[0].offered_before is None


def test_empty_directory_is_an_explicit_error():
    d = FakeDirectory()
    d.layers = {"Parcel_Roll_2025A": d.layers["Parcel_Roll_2025A"]}
    with pytest.raises(GisError, match="no per-auction layers"):
        pull_history(client(d))
