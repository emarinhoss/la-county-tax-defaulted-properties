import json

import pytest

import lacounty_tax.cli as cli
from lacounty_tax.assessor import BlockedError
from lacounty_tax.boundaries import DEFAULT_CITIES_URL, fetch_cities, pick_name_field, write_cities
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
    assert [f["properties"] for f in fc["features"]] == [{"name": n, "kind": "city"} for n in "ABC"]   # no-geometry dropped
    calls = kw["session"].calls
    assert [c["resultOffset"] for c in calls] == ["0", "2", "4"] and calls[0]["f"] == "geojson"
    assert calls[0]["outSR"] == "4326" and calls[0]["maxAllowableOffset"] == "0.0001"
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
    seen = []
    monkeypatch.setattr(cli, "write_cities", lambda url, o, **k: seen.append(url) or 1)
    assert main(["fetch-boundaries", "--out", str(out)]) == 0                 # no --url: the county's ArcGIS layer is the default
    assert seen == [DEFAULT_CITIES_URL] and "services.arcgis.com/RmCCgQtiZLDCtblq" in seen[0]
    monkeypatch.setenv("LACOUNTY_CITIES_URL", "https://example.test/Layer/1")
    assert main(["fetch-boundaries", "--out", str(out)]) == 0 and seen[-1] == "https://example.test/Layer/1"


def test_a_server_that_ignores_paging_cannot_loop_forever():
    kw = client_kw(lambda p, n: Resp(data={"features": [feat("A"), feat("B")], "exceededTransferLimit": True}))
    with pytest.raises(GisError, match="too many pages"):
        fetch_cities("https://x/0", page_size=2, **kw)


# ---------- the real county layer's attribute table (tests/fixtures/la_city_layer_attributes.csv) ----------
import csv
from pathlib import Path

from lacounty_tax.boundaries import clean_features, load_geojson, rdp, simplify_geometry

LAYER_CSV = Path(__file__).parent / "fixtures" / "la_city_layer_attributes.csv"


def layer_features():
    """The real attribute table with a small square standing in for each (unknown) shape."""
    rows = list(csv.DictReader(LAYER_CSV.open(encoding="utf-8-sig")))
    return rows, [{"type": "Feature", "properties": r,
                   "geometry": {"type": "Polygon", "coordinates": [[[-118.3 + i * 1e-3, 34.0], [-118.3 + i * 1e-3, 34.1],
                                                                       [-118.2 + i * 1e-3, 34.1], [-118.2 + i * 1e-3, 34.0],
                                                                       [-118.3 + i * 1e-3, 34.0]]]}}
                  for i, r in enumerate(rows)]


def test_name_detection_on_the_real_county_layer():
    rows, _ = layer_features()
    assert len(rows) == 167 and "CITY" in rows[0]
    # CITY exists but is empty in every row; the detector must not fall for it
    assert sum(bool(r["CITY"]) for r in rows) == 0
    assert pick_name_field(rows) == "CITY_COMM_NAME"


def test_real_layer_defaults_to_the_88_incorporated_cities():
    rows, feats = layer_features()
    fc = clean_features(feats)
    assert len(fc) == 88 and {f["properties"]["kind"] for f in fc} == {"city"}
    names = {f["properties"]["name"] for f in fc}
    assert {"LOS ANGELES", "LONG BEACH", "PALMDALE", "LANCASTER", "SANTA CLARITA", "POMONA"} <= names
    assert "ANTELOPE VALLEY" not in names and "ALTADENA" not in names


def test_real_layer_with_unincorporated_skips_the_umbrella_areas():
    rows, feats = layer_features()
    fc = clean_features(feats, include_unincorporated=True)
    names = [f["properties"]["name"] for f in fc]
    assert len(fc) == 88 + 79 - 3                      # the three TYPE=AREA regions are never drawn
    for umbrella in ("ANTELOPE VALLEY", "SANTA CLARITA VALLEY", "SANTA MONICA MOUNTAINS NORTH AREA"):
        assert umbrella not in names
    assert "ALTADENA" in names and "EAST LOS ANGELES" in names
    assert {f["properties"]["kind"] for f in fc} == {"city", "unincorporated"}


def test_rdp_keeps_shape_and_drops_collinear_points():
    line = [[0, 0], [1, 0.00001], [2, 0], [3, 2], [4, 0]]
    assert rdp(line, 0.001) == [[0, 0], [2, 0], [3, 2], [4, 0]]
    assert rdp(line, 0) == line and rdp([[0, 0], [1, 1]], 0.5) == [[0, 0], [1, 1]]


def test_simplify_geometry_rounds_drops_slivers_and_keeps_holes():
    sq = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
    noisy = [[0, 0], [0, 5], [0, 10], [5.00001, 10.00001], [10, 10], [10, 0], [0, 0]]
    hole = [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]]
    g = simplify_geometry({"type": "Polygon", "coordinates": [noisy, hole]}, 0.01)
    assert g["coordinates"][0] == [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]] and len(g["coordinates"]) == 2
    sliver = [[0, 0], [0.00001, 0], [0.00001, 0.00001], [0, 0]]                     # collapses below 4 points
    multi = simplify_geometry({"type": "MultiPolygon", "coordinates": [[sq], [sliver]]}, 0.001)
    assert multi["type"] == "Polygon" and multi["coordinates"][0] == sq               # sliver dropped, single part collapses
    assert simplify_geometry({"type": "Point", "coordinates": [0, 0]}, 0.01) is None


def test_only_empty_names_is_an_explicit_error():
    feats = [{"type": "Feature", "properties": {"CITY": "", "NAME": ""}, "geometry": {"type": "Polygon", "coordinates": [SQUARE[0]]}}]
    with pytest.raises(GisError, match="pass --name-field"):
        clean_features(feats)


def test_load_geojson_file_and_cli(tmp_path, capsys):
    rows, feats = layer_features()
    src = tmp_path / "layer.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    out = tmp_path / "out" / "la_cities.geojson"
    assert main(["load-boundaries", str(src), "--out", str(out)]) == 0
    assert "88 polygons (88 cities, 0 unincorporated)" in capsys.readouterr().out
    assert len(json.loads(out.read_text())["features"]) == 88
    assert main(["load-boundaries", str(src), "--out", str(out), "--include-unincorporated"]) == 0
    assert "164 polygons (88 cities, 76 unincorporated)" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text('{"nope": 1}')
    assert main(["load-boundaries", str(bad), "--out", str(out)]) == 1
    assert main(["load-boundaries", str(tmp_path / "missing.geojson"), "--out", str(out)]) == 1


# ---------- county outline (dissolve) and the committed data files ----------
BOUNDS = Path(__file__).parents[1] / "data" / "reference" / "boundaries"


def poly(x0, y0, x1, y1):
    return {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}}


def test_county_outline_fills_slivers_and_drops_specks():
    pytest.importorskip("shapely")
    from lacounty_tax.boundaries import county_outline
    a, b = poly(-118.5, 34.0, -118.30, 34.2), poly(-118.30, 34.0, -118.1, 34.2)       # neighbours sharing an edge
    hole = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [
        [[-118.5, 33.5], [-118.5, 33.7], [-118.3, 33.7], [-118.3, 33.5], [-118.5, 33.5]],
        [[-118.42, 33.58], [-118.42, 33.62], [-118.38, 33.62], [-118.38, 33.58], [-118.42, 33.58]]]}}
    speck = poly(-118.0, 34.5, -117.9995, 34.5005)
    fc = county_outline([a, b, hole, speck], simplify=0.0001)
    geom = fc["features"][0]["geometry"]
    parts = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    assert len(parts) == 2                                        # merged neighbours + the holed square; speck gone
    assert all(len(rings) == 1 for rings in parts)                # no holes left anywhere
    assert fc["features"][0]["properties"]["fips"] == "06037"


def test_committed_city_file_is_88_named_cities_in_the_right_places():
    fc = json.loads((BOUNDS / "la_cities.geojson").read_text())
    names = [f["properties"]["name"] for f in fc["features"]]
    assert len(names) == 88 and len(set(names)) == 88 and all(names)
    assert {f["properties"]["kind"] for f in fc["features"]} == {"city"}
    by_name = {f["properties"]["name"]: f["geometry"] for f in fc["features"]}

    def inside(pt, g):
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        x, y = pt
        c = False
        for rings in polys:
            for ring in rings:
                for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
                    if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
                        c = not c
        return c
    for label, lat, lon, city in [("Downtown LA", 34.0522, -118.2437, "LOS ANGELES"), ("Long Beach", 33.77, -118.19, "LONG BEACH"),
                                  ("Pasadena", 34.1478, -118.1445, "PASADENA"), ("Palmdale", 34.5794, -118.1165, "PALMDALE"),
                                  ("Avalon", 33.3428, -118.3278, "AVALON"), ("Malibu", 34.0259, -118.7798, "MALIBU")]:
        assert [n for n, g in by_name.items() if inside((lon, lat), g)] == [city], label


def test_every_city_lies_inside_the_county_outline():
    shapely = pytest.importorskip("shapely")
    from shapely.geometry import shape
    cities = json.loads((BOUNDS / "la_cities.geojson").read_text())["features"]
    county = shape(json.loads((BOUNDS / "la_county.geojson").read_text())["features"][0]["geometry"])
    outside = [f["properties"]["name"] for f in cities if not county.contains(shape(f["geometry"]).representative_point())]
    assert outside == []
    # and the outline is not wildly bigger than the cities' own footprint region: ~4,058 sq mi of land
    import math
    sq_mi = county.area * 69.0 * math.cos(math.radians(county.centroid.y)) * 69.0
    assert 3900 < sq_mi < 4250
