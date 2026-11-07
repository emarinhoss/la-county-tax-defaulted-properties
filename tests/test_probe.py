import json
from pathlib import Path

import pandas as pd
import pytest

import lacounty_tax.cli as cli
from lacounty_tax.assessor import BlockedError
from lacounty_tax.cli import main
from lacounty_tax.geo import GisClient, GisError
from lacounty_tax.probe import (BASE, geocode_flags, markdown_report, probe, rank, sample_ains, to_json)
from test_assessor import Resp

AINS = [f"{2000000000 + i:010d}" for i in range(0, 400, 10)]            # 40 AINs
IN_LA = (34.05, -118.25)


def attrs(ain, with_coords=True, field="AIN"):
    a = {field: ain}
    if with_coords:
        a.update({"CENTER_LAT": IN_LA[0], "CENTER_LON": IN_LA[1]})
    return a


def square():
    return {"rings": [[[-118.3, 34.0], [-118.3, 34.1], [-118.2, 34.1], [-118.2, 34.0], [-118.3, 34.0]]]}


# service -> layers: {id: (name, geometryType, fields[(name,type)], count, rows_for_ains(ains)->features)}
def services():
    def parcels(ains):
        return [{"attributes": attrs(a)} for a in ains]

    def multi_year(ains):                                   # five roll years per AIN
        return [{"attributes": attrs(a)} for a in ains for _ in range(5)]

    def polygons(ains):
        return [{"attributes": {"AIN": a}, "geometry": square()} for a in ains]

    def numeric_ain(ains):
        return [{"attributes": {"AIN": int(a), "CENTER_LAT": IN_LA[0], "CENTER_LON": IN_LA[1]}} for a in ains]

    S = lambda n, t="esriFieldTypeString": (n, t)             # noqa: E731
    return {
        "Current_Parcels": {"layers": {0: ("Parcels", "esriGeometryPolygon", [S("AIN"), S("APN"), S("CENTER_LAT", "esriFieldTypeDouble"), S("CENTER_LON", "esriFieldTypeDouble")], 2_400_000, parcels)}},
        "Parcel_Data_2021_Table": {"tables": {0: ("Parcel_Data", None, [S("AIN"), S("CENTER_LAT", "esriFieldTypeDouble"), S("CENTER_LON", "esriFieldTypeDouble")], 12_000_000, multi_year)}},
        "Old_Roll_Polygons": {"layers": {0: ("Roll", "esriGeometryPolygon", [S("AIN")], 2_300_000, polygons)}},
        "Numeric_Parcel_Layer": {"layers": {0: ("N", "esriGeometryPoint", [S("AIN", "esriFieldTypeInteger"), S("CENTER_LAT", "esriFieldTypeDouble"), S("CENTER_LON", "esriFieldTypeDouble")], 100, numeric_ain)}},
        "Tax_Defaulted_2025A": {"layers": {0: ("Auction", "esriGeometryPolygon", [S("AIN"), S("MIN_BID", "esriFieldTypeDouble")], 1500, lambda ains: [])}},
        "Parcel_Notes": {"tables": {0: ("Notes", None, [S("Note")], 10, lambda ains: [])}},          # no AIN-like field
        "Unrelated_Bikes": {"layers": {0: ("Bikes", "esriGeometryPoint", [S("AIN")], 5, lambda ains: [])}},
    }


class FakeArcGis:
    def __init__(self, svcs=None, broken=(), blocked=False):
        self.svcs, self.broken, self.blocked = svcs or services(), set(broken), blocked
        self.calls, self.headers = [], {}

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        if self.blocked:
            return Resp(403, text="<html>bot protection</html>", ctype="text/html")
        rest = url[len(BASE):].strip("/")
        if rest == "":
            listing = [{"name": n, "type": "FeatureServer"} for n in self.svcs] + [{"name": "SomeMap", "type": "MapServer"}]
            return Resp(data={"services": listing})
        parts = [p.replace("%20", " ") for p in rest.split("/")]
        name = parts[0]
        if name in self.broken:
            return Resp(500, text="boom", ctype="text/plain")
        svc = self.svcs[name]
        if len(parts) == 2:                                                           # <name>/FeatureServer
            mk = lambda d: [{"id": i, "name": v[0]} for i, v in d.items()]            # noqa: E731
            return Resp(data={"layers": mk(svc.get("layers", {})), "tables": mk(svc.get("tables", {}))})
        lid = int(parts[2])
        lname, geom, fields, count, rows = (svc.get("layers", {}) | svc.get("tables", {}))[lid]
        if len(parts) == 3:
            return Resp(data={"name": lname, "geometryType": geom, "fields": [{"name": n, "type": t} for n, t in fields]})
        if params.get("returnCountOnly"):
            return Resp(data={"count": count})
        where = params["where"]
        field_name, rest_where = where.split(" IN (", 1)
        asked = [v.strip("'") for v in rest_where.rstrip(")").split(",")]
        feats = rows([a for a in asked if a in AINS])
        if params.get("returnGeometry") == "false":
            feats = [{"attributes": f["attributes"]} for f in feats]
        return Resp(data={"features": feats})


def client(session):
    return GisClient(BASE, session=session, lat_field="", lon_field="", min_interval=0, backoff=0, sleep=lambda s: None, retries=1)


def run(**kw):
    return probe(client(FakeArcGis(**{k: v for k, v in kw.items() if k in ("svcs", "broken", "blocked")})), AINS, base=BASE,
                 **{k: v for k, v in kw.items() if k not in ("svcs", "broken", "blocked")})


def test_sample_ains_spreads_and_normalizes():
    many = [f"{i:09d}" for i in range(1000)]                       # 9 digits: zero-padded to 10
    s = sample_ains(many, 40)
    assert len(s) == 40 and s[0] == "0000000000" and s[-1] > s[20] > s[0] and all(len(a) == 10 for a in s)
    assert sample_ains(["123"], 40) == ["0000000123"]


def test_scan_finds_candidates_and_ranks_the_clean_layer_first():
    reports = run()
    by = {r.service: r for r in reports}
    assert set(by) == {"Current_Parcels", "Parcel_Data_2021_Table", "Old_Roll_Polygons", "Numeric_Parcel_Layer", "Tax_Defaulted_2025A", "Parcel_Notes"}
    assert "Unrelated_Bikes" not in by and "SomeMap" not in by       # name filter / MapServer ignored
    cur = by["Current_Parcels"]
    assert (cur.found, cur.sampled, cur.coords, cur.ain_field, cur.rows_per_ain) == (40, 40, "attributes", "AIN", 1.0)
    assert by["Parcel_Data_2021_Table"].found == 40 and by["Parcel_Data_2021_Table"].rows_per_ain == 5.0   # distinct, not 200/40
    assert by["Old_Roll_Polygons"].coords == "geometry" and by["Old_Roll_Polygons"].usable
    assert by["Numeric_Parcel_Layer"].usable                                   # numeric AIN field: unquoted IN list
    assert by["Parcel_Notes"].ain_field is None and "no AIN-like field" in by["Parcel_Notes"].note
    assert not by["Tax_Defaulted_2025A"].usable and by["Tax_Defaulted_2025A"].found == 0
    order = [r.service for r in rank(reports) if r.usable]
    assert order[0] == "Current_Parcels"                       # 1 row/AIN, attribute coords, spatial layer
    assert order.index("Parcel_Data_2021_Table") == len(order) - 1             # multi-year table last among the usable


def test_known_only_and_explicit_names():
    assert {r.service for r in run(known_only=True)} == set()                  # none of the fake services is on the known list
    assert {r.service for r in run(names=["Current_Parcels"])} == {"Current_Parcels"}
    assert len(run(max_services=2)) == 2


def test_report_recommends_and_prints_geocode_flags():
    reports = run()
    md = markdown_report(reports, 40)
    assert "## Recommended" in md and "**Current_Parcels**" in md and "40/40" in md
    best = rank(reports)[0]
    flags = geocode_flags(best)
    assert f'--gis-url "{BASE}/Current_Parcels/FeatureServer/0/query"' in flags and "--lat-field CENTER_LAT --lon-field CENTER_LON" in flags
    poly = [r for r in reports if r.service == "Old_Roll_Polygons"][0]
    assert "--lat-field ''" in geocode_flags(poly)                              # polygon layers: no attribute coordinates
    assert "Per-auction layers" in md and "Tax_Defaulted_2025A" in md
    assert "Attribute tables keyed by AIN" in md and "Parcel_Data_2021_Table" in md
    j = to_json(reports)
    assert j[0]["service"] == "Current_Parcels" and j[0]["usable"] is True and j[0]["hit_rate"] == 1.0


def test_no_usable_layer_says_so():
    only_bad = {"Tax_Defaulted_2025A": services()["Tax_Defaulted_2025A"]}
    md = markdown_report(run(svcs=only_bad), 40)
    assert "No layer matched 90%" in md


def test_ain_field_priority_prefers_plain_ain():
    svc = {"Roll_Parcels": {"tables": {0: ("T", None, [("Assessment_AIN", "esriFieldTypeString"), ("Master_AIN", "esriFieldTypeString"),
                                                       ("AIN", "esriFieldTypeString")], 5, lambda ains: [])}}}
    assert run(svcs=svc)[0].ain_field == "AIN"
    svc2 = {"Roll_Parcels": {"tables": {0: ("T", None, [("Assessment_AIN", "esriFieldTypeString")], 5, lambda ains: [])}}}
    assert run(svcs=svc2)[0].ain_field == "Assessment_AIN"


def test_one_broken_service_does_not_stop_the_scan_but_a_block_does():
    reports = run(broken=["Old_Roll_Polygons"])
    bad = [r for r in reports if r.service == "Old_Roll_Polygons"][0]
    assert bad.note.startswith("error") and not bad.usable
    assert any(r.usable for r in reports)
    assert "## Errors (1)" in markdown_report(reports, 40)
    with pytest.raises(BlockedError):
        run(blocked=True)


def test_cli_probe_writes_reports_and_exit_codes(tmp_path, monkeypatch, capsys):
    d = tmp_path / "data" / "T1"
    d.mkdir(parents=True)
    pd.DataFrame({"auction": "T1", "ain": AINS}).to_csv(d / "items.csv", index=False)
    fake = FakeArcGis()
    monkeypatch.setattr(cli, "GisClient", lambda *a, **k: client(fake))
    out, js = tmp_path / "r.md", tmp_path / "r.json"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["probe-gis", "--auction", "T1", "--data-dir", str(tmp_path / "data"), "--out", str(out), "--json", str(js)]) == 0
    assert "Recommended" in capsys.readouterr().out and "Current_Parcels" in out.read_text() and "Parcel layer probe" in summary.read_text()
    assert json.loads(js.read_text())[0]["service"] == "Current_Parcels"
    assert main(["probe-gis", "--ains", ",".join(AINS[:5]), "--name", "Tax_Defaulted_2025A"]) == 1     # nothing usable -> exit 1
    assert main(["probe-gis", "--auction", "NOPE", "--data-dir", str(tmp_path / "data")]) == 2
    monkeypatch.setattr(cli, "GisClient", lambda *a, **k: client(FakeArcGis(blocked=True)))
    assert main(["probe-gis", "--auction", "T1", "--data-dir", str(tmp_path / "data")]) == 3
