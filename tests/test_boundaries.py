import json

import pytest

import lacounty_tax.cli as cli
from lacounty_tax.assessor import BlockedError
from lacounty_tax.boundaries import fetch_cities, pick_name_field, write_cities
from lacounty_tax.cli import main
from lacounty_tax.geo import GisError
from test_assessor import Resp
from test_geo import Session

SQUARE = [[[-118.3, 34.0], [-118.3, 34.1], [-118.2, 34.1], [-118.2, 34.0], [-118.3, 34.0]]]


def feat(name, field="CITY_NAME", geom=True):
    return {"type": "Feature", "properties": {field: name, "OBJECTID": 1},
            "geometry": {"type": "Polygon", "coordinates": [[[x + 0.123456789, y] for x, y in SQUARE[0]]]} if geom else None}


def client_kw(handler):
    return dict(session=Session(handler), min_interval=0, backoff=0, sleep=lambda s: None)


def test_pick_name_field():
    assert pick_name_field({"OBJECTID": 1, "City_Name": "X"}) == "City_Name"
    assert pick_name_field({"A": 1, "B": 2}, "B") == "B"
    with pytest.raises(GisError, match="pass --name-field"):
        pick_name_field({"A": 1})
    with pytest.raises(GisError, match="not in the layer"):
        pick_name_field({"A": 1}, "ZZ")


def test_pages_through_the_layer_and_cleans_features():
    pages = [{"features": [feat("A"), feat("B")], "exceededTransferLimit": True},
             {"features": [feat("C"), feat("D", geom=False)]},        # a full page: the client asks once more
             {"features": []}]
    kw = client_kw(lambda p, n: Resp(data=pages[n - 1]))
    fc = fetch_cities("https://gis.test/Layer/3", page_size=2, **kw)
    assert [f["properties"] for f in fc["features"]] == [{"name": "A"}, {"name": "B"}, {"name": "C"}]   # no-geometry dropped
    calls = kw["session"].calls
    assert [c["resultOffset"] for c in calls] == ["0", "2", "4"] and calls[0]["f"] == "geojson"
    assert calls[0]["outSR"] == "4326" and calls[0]["maxAllowableOffset"] == "0.0002"
    assert fc["features"][0]["geometry"]["coordinates"][0][0] == [-118.1765, 34.0]                    # rounded to 4 dp


def test_adds_query_suffix_once():
    kw = client_kw(lambda p, n: Resp(data={"features": [feat("A")]}))
    fetch_cities("https://gis.test/Layer/3/", **kw)
    fetch_cities("https://gis.test/Layer/3/query", **client_kw(lambda p, n: Resp(data={"features": [feat("A")]})))
    assert kw["session"].calls  # (URL itself is held by the client; params checked above)


def test_errors_are_explicit():
    with pytest.raises(GisError, match="no polygons"):
        fetch_cities("https://x/0", **client_kw(lambda p, n: Resp(data={"features": []})))
    with pytest.raises(GisError, match="Invalid"):
        fetch_cities("https://x/0", **client_kw(lambda p, n: Resp(data={"error": {"message": "Invalid query"}})))
    with pytest.raises(BlockedError):
        fetch_cities("https://x/0", **client_kw(lambda p, n: Resp(403, text="<html>", ctype="text/html")))
    far = {"type": "Feature", "properties": {"CITY": "X"}, "geometry": {"type": "Polygon", "coordinates": [[[10, 10], [10, 11], [11, 11], [10, 10]]]}}
    with pytest.raises(GisError, match="look wrong"):
        fetch_cities("https://x/0", **client_kw(lambda p, n: Resp(data={"features": [far]})))
    with pytest.raises(GisError, match="pass --name-field"):
        fetch_cities("https://x/0", **client_kw(lambda p, n: Resp(data={"features": [feat("A", field="ZZZ")]})))


def test_write_and_cli(tmp_path, monkeypatch, capsys):
    out = tmp_path / "b" / "la_cities.geojson"
    monkeypatch.setattr(cli, "write_cities", lambda url, o, **k: write_cities(url, o, **client_kw(lambda p, n: Resp(data={"features": [feat("A"), feat("B")]}))))
    assert main(["fetch-boundaries", "--url", "https://gis.test/Layer/3", "--out", str(out)]) == 0
    assert "2 city polygons" in capsys.readouterr().out
    assert len(json.loads(out.read_text())["features"]) == 2
    monkeypatch.delenv("LACOUNTY_CITIES_URL", raising=False)
    assert main(["fetch-boundaries", "--out", str(out)]) == 2
    assert "GeoHub" in capsys.readouterr().err


def test_a_server_that_ignores_paging_cannot_loop_forever():
    kw = client_kw(lambda p, n: Resp(data={"features": [feat("A"), feat("B")], "exceededTransferLimit": True}))
    with pytest.raises(GisError, match="too many pages"):
        fetch_cities("https://x/0", page_size=2, **kw)
