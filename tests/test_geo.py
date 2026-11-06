import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import requests

import lacounty_tax.cli as cli
from lacounty_tax.assessor import BlockedError
from lacounty_tax.cli import main
from lacounty_tax.geo import (GisClient, GisError, geocode_auction, polygon_centroid, read_coordinates,
                              ring_centroid)
from lacounty_tax.refresh import refresh_auction
from lacounty_tax.site import load_auction
from test_assessor import Resp, client as assessor_client, ok_handler
from test_refresh import FMR, make_auction

CW_SQUARE = [[0, 0], [0, 4], [4, 4], [4, 0], [0, 0]]            # clockwise: Esri outer ring
CCW_HOLE = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]             # counter-clockwise: a hole


def test_ring_and_polygon_centroids():
    x, y, a = ring_centroid(CW_SQUARE[:-1])
    assert (x, y) == pytest.approx((2, 2)) and a < 0           # clockwise has negative signed area
    cx, cy = polygon_centroid([CW_SQUARE[:-1], CCW_HOLE[:-1]])  # hole pulls the centroid away
    assert (cx, cy) == pytest.approx((28 / 12, 28 / 12))
    assert polygon_centroid([]) is None and polygon_centroid([[[0, 0], [1, 1]]]) is None
    assert polygon_centroid([[[1, 1], [2, 2], [3, 3]]]) == pytest.approx((2, 2))   # degenerate: vertex mean


def feature(ain, lon, lat, size=0.0001, field="AIN"):
    ring = [[lon - size, lat - size], [lon - size, lat + size], [lon + size, lat + size], [lon + size, lat - size],
            [lon - size, lat - size]]
    return {"attributes": {field: ain}, "geometry": {"rings": [ring]}}


class Session:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self.handler(params, len(self.calls))


def gis(handler, **kw):
    kw.setdefault("batch_size", 50)
    return GisClient("http://gis.test/query", session=Session(handler), min_interval=0, backoff=0,
                     sleep=lambda s: None, **kw)


def ok(params, n):
    ains = [a.strip("'") for a in params["where"].split("(", 1)[1].rstrip(")").split(",")]
    feats = [feature(a, -118.3 + int(a[-2:]) * 0.001, 34.0) for a in ains if not a.endswith("99")]
    return Resp(data={"features": feats})


def test_centroids_batches_and_omits_unknown():
    c = gis(ok, batch_size=2)
    got = c.centroids(["0000000001", "0000000002", "0000000099", "0000000003", "0000000004"])
    assert len(c.session.calls) == 3                                      # 5 AINs in batches of 2
    assert set(got) == {"0000000001", "0000000002", "0000000003", "0000000004"}   # ...99 not in the layer
    lat, lon = got["0000000001"]
    assert lat == pytest.approx(34.0) and lon == pytest.approx(-118.299)
    p = c.session.calls[0]
    assert p["where"] == "AIN IN ('0000000001','0000000002')" and p["outSR"] == "4326" and p["f"] == "json"


def test_implausible_or_unrequested_features_ignored():
    feats = [feature("0000000001", 118.3, 34.0),         # wrong sign: outside LA County
             feature("0000000002", -118.3, 34.0),
             feature("0000000777", -118.3, 34.0)]        # not requested
    got = gis(lambda p, n: Resp(data={"features": feats})).centroids(["0000000001", "0000000002"])
    assert set(got) == {"0000000002"}


def test_custom_ain_field():
    c = gis(lambda p, n: Resp(data={"features": [feature("0000000001", -118.3, 34.0, field="APN")]}), ain_field="APN")
    assert set(c.centroids(["0000000001"])) == {"0000000001"}
    assert c.session.calls[0]["where"].startswith("APN IN")


def test_service_errors_and_blocks():
    with pytest.raises(GisError, match="Invalid field"):
        gis(lambda p, n: Resp(data={"error": {"code": 400, "message": "Invalid field: AIN"}})).centroids(["1"])
    with pytest.raises(BlockedError):
        gis(lambda p, n: Resp(403, text="<html>", ctype="text/html")).centroids(["1"])
    with pytest.raises(GisError, match="HTTP 404"):
        gis(lambda p, n: Resp(404, text="nope", ctype="text/plain")).centroids(["1"])


def test_retries_transient_failures():
    seq = [Resp(503, text="x"), Resp(data={"features": []})]
    c = gis(lambda p, n: seq[n - 1])
    assert c.centroids(["1"]) == {} and len(c.session.calls) == 2

    def flaky(p, n):
        if n == 1:
            raise requests.Timeout("slow")
        return Resp(data={"features": []})
    assert gis(flaky).centroids(["1"]) == {}
    with pytest.raises(GisError, match="gave up"):
        gis(lambda p, n: Resp(500, text="x"), retries=1).centroids(["1"])


def test_geocode_auction_is_incremental(tmp_path):
    d = make_auction(tmp_path, n=4)
    ains = list(pd.read_csv(d / "items.csv", dtype={"ain": str})["ain"])
    c = gis(ok)
    found, missing, total = geocode_auction(d, c)
    assert (found, missing, total) == (4, 0, 4)
    assert set(read_coordinates(d)["ain"]) == set(ains) and set(read_coordinates(d)["source"]) == {"county_gis"}
    c2 = gis(ok)
    assert geocode_auction(d, c2) == (0, 0, 4) and c2.session.calls == []       # nothing left to look up


def test_geocode_auction_keeps_progress_when_blocked(tmp_path):
    d = make_auction(tmp_path, n=4)
    def h(p, n):
        return ok(p, n) if n == 1 else Resp(403, text="<html>", ctype="text/html")
    with pytest.raises(BlockedError):
        geocode_auction(d, gis(h, batch_size=2))
    assert len(read_coordinates(d)) == 2                                         # first batch was saved


def test_unmapped_parcels_are_counted(tmp_path):
    d = make_auction(tmp_path, n=3)                    # AINs ...00, ...01, ...02
    c = gis(lambda p, n: Resp(data={"features": [feature("0000000001", -118.3, 34.0)]}))
    assert geocode_auction(d, c) == (1, 2, 3)


def test_refresh_maps_but_never_fails_on_gis_problems(tmp_path):
    d = make_auction(tmp_path)
    kw = dict(cache_dir=tmp_path / "c", fmr_path=FMR, config_path=None, today=date(2026, 11, 6))
    r = refresh_auction(d, assessor_client(ok_handler), gis_client=gis(ok), **kw)
    assert r.status == "refreshed" and "coordinates +3 (3/3 mapped)" in r.detail
    d2 = make_auction(tmp_path, "T2")
    r2 = refresh_auction(d2, assessor_client(ok_handler), gis_client=gis(lambda p, n: Resp(403, text="<html>", ctype="text/html")), **kw)
    assert r2.status == "refreshed" and "coordinates skipped" in r2.detail


def test_site_rows_carry_coordinates(tmp_path):
    from lacounty_tax.synthetic import make_demo
    from lacounty_tax.reference import read_fmr_csv
    d = make_demo(tmp_path, read_fmr_csv(FMR))
    rows = load_auction(d)["rows"]
    mapped = [r for r in rows if r["lat"] is not None]
    assert 0.9 * len(rows) < len(mapped) < len(rows)                 # ~97% mapped, the rest null
    assert all(33.5 < r["lat"] < 34.9 and -119 < r["lon"] < -117.5 for r in mapped)


def test_cli_geocode(tmp_path, monkeypatch, capsys):
    d = make_auction(tmp_path / "data", "T1", n=3)
    monkeypatch.setattr(cli, "GisClient", lambda *a, **k: gis(ok))
    assert main(["geocode", "T1", "--data-dir", str(tmp_path / "data")]) == 0
    assert "3/3 parcels have coordinates" in capsys.readouterr().out
    assert (d / "coordinates.csv").exists()
    monkeypatch.setattr(cli, "GisClient", lambda *a, **k: gis(lambda p, n: Resp(403, text="<html>", ctype="text/html")))
    (d / "coordinates.csv").unlink()
    assert main(["geocode", "T1", "--data-dir", str(tmp_path / "data")]) == 3
    assert main(["geocode", "NOPE", "--data-dir", str(tmp_path / "data")]) == 2


def test_returned_ains_are_normalized_to_digits():
    got = gis(lambda p, n: Resp(data={"features": [feature("2024-002-096", -118.3, 34.0)]})).centroids(["2024002096"])
    assert set(got) == {"2024002096"}


def test_cli_geocode_hints_when_nothing_matches(tmp_path, monkeypatch, capsys):
    make_auction(tmp_path / "data", "T1", n=2)
    monkeypatch.setattr(cli, "GisClient", lambda *a, **k: gis(lambda p, n: Resp(data={"features": []})))
    assert main(["geocode", "T1", "--data-dir", str(tmp_path / "data")]) == 1
    assert "matched nothing" in capsys.readouterr().err
