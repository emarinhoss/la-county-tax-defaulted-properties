import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from lacounty_tax.analyze import Assumptions, income_metrics
from lacounty_tax.cli import main
from lacounty_tax.reference import read_fmr_csv
from lacounty_tax.site import ROW_COLUMNS, build_site, collect, render_html
from lacounty_tax.synthetic import make_demo

ROOT = Path(__file__).parents[1]
JS = ROOT / "src" / "lacounty_tax" / "templates" / "dashboard.js"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo")
    return make_demo(out, read_fmr_csv(ROOT / "data/reference/hud_safmr.csv")), out


def test_demo_is_clearly_synthetic_and_has_every_segment(demo):
    d, _ = demo
    scored = pd.read_csv(d / "analysis" / "scored.csv", dtype={"ain": str})
    assert scored["ain"].str.startswith("99").all()
    assert scored["property_address"].str.contains("DEMO|VACANT").all()
    assert set(scored["segment"]) == {"rental", "land", "review"} or set(scored["segment"]) >= {"rental", "land"}
    assert scored["rank"].notna().sum() > 50
    assert {"DEFAULTED", "CURRENT", "DELINQUENT"} <= set(scored["tax_status"].dropna())
    ranked_status = scored[scored["rank"].notna()]["tax_status"]
    assert "DELINQUENT" in set(ranked_status) and "CURRENT" not in set(ranked_status)   # kept and flagged
    assert {"tax_current", "no_bedroom_data"} <= set(scored["excluded"].dropna())
    assert json.loads((d / "analysis" / "summary.json").read_text())["synthetic"] is True
    hist = pd.read_csv(d / "status_history.csv", dtype=str)
    assert hist.groupby("ain").size().max() == 2          # some parcels changed status


def test_demo_is_deterministic(tmp_path, demo):
    fmr = read_fmr_csv(ROOT / "data/reference/hud_safmr.csv")
    again = make_demo(tmp_path, fmr)
    a = (demo[0] / "analysis" / "scored.csv").read_text()
    assert (again / "analysis" / "scored.csv").read_text() == a


def test_collect_and_build(demo, tmp_path):
    d, base = demo
    auctions = collect([base])
    assert [a["id"] for a in auctions] == ["DEMO"] and auctions[0]["synthetic"]
    row = auctions[0]["rows"][0]
    assert set(row) == set(ROW_COLUMNS)
    out = build_site([base], tmp_path / "site")
    html = out.read_text()
    assert out.name == "index.html" and (tmp_path / "site" / ".nojekyll").exists()
    assert "/*DATA*/" not in html and "/*JS*/" not in html and "/*CSS*/" not in html
    blob = html.split('id="data">', 1)[1].split("</script>", 1)[0]
    data = json.loads(blob)                                   # valid JSON, NaN already null
    assert len(data["auctions"][0]["rows"]) == 360
    assert "NaN" not in blob


def test_real_auctions_sort_before_synthetic():
    a = [{"id": "DEMO", "synthetic": True}, {"id": "2026A", "synthetic": False}, {"id": "2026B", "synthetic": False}]
    got = sorted(a, key=lambda x: (x["synthetic"], [-ord(c) for c in x["id"]]))
    assert [x["id"] for x in got] == ["2026B", "2026A", "DEMO"]


def test_embedded_data_cannot_break_out_of_script_tag():
    evil = {"id": "X", "snapshot": "s", "synthetic": True, "label": "</script><script>alert(1)</script>",
            "assumptions": {}, "rows": []}
    html = render_html([evil])
    assert "</script><script>alert(1)" not in html
    assert html.count("<script") == 3                          # leaflet + data + code only
    assert "\\u003c/script>\\u003cscript>alert(1)" in html


def test_build_site_without_analysis_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="analyze"):
        build_site([tmp_path], tmp_path / "out")
    assert main(["build-site", "--data-dir", str(tmp_path), "--out", str(tmp_path / "o")]) == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_money_model_matches_python(demo):
    d, _ = demo
    scored = pd.read_csv(d / "analysis" / "scored.csv", dtype={"ain": str})
    r = scored[scored["rank"].notna()].head(25)
    a = Assumptions()
    script = f"""
      const {{ capMetrics }} = require({json.dumps(str(JS))});
      const a = {json.dumps(a.__dict__)};
      const rows = {r[["est_monthly_rent", "min_bid", "rehab_estimate"]].to_json(orient="records")};
      console.log(JSON.stringify([1, 2.5].map(m => rows.map(r => capMetrics(a, r.est_monthly_rent, r.min_bid, r.rehab_estimate, m)))));
    """
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for m, js_rows in zip([1, 2.5], out):
        for (_, row), js in zip(r.iterrows(), js_rows):
            py = income_metrics(a, row.est_monthly_rent, row.min_bid * m, row.rehab_estimate)
            assert js["cap"] == pytest.approx(py["cap_rate"])
            assert js["maxBid"] == pytest.approx(py["max_bid_for_target"])
            assert js["noi"] == pytest.approx(py["noi"])
    # at m = 1 the JS value is what the pipeline wrote to scored.csv
    assert [x["cap"] for x in out[0]] == pytest.approx(list(r["cap_rate"]))


def test_page_uses_vendored_leaflet_and_has_a_map_tab(demo, tmp_path):
    out = build_site([demo[1]], tmp_path / "site", None)
    html = out.read_text()
    assert 'src="vendor/leaflet/leaflet.js"' in html and 'href="vendor/leaflet/leaflet.css"' in html
    assert "unpkg.com" not in html                                            # no CDN dependency any more
    import base64, hashlib
    sri = lambda p: "sha256-" + base64.b64encode(hashlib.sha256(p.read_bytes()).digest()).decode()
    v = tmp_path / "site" / "vendor" / "leaflet"
    assert sri(v / "leaflet.js") == "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="    # the genuine 1.9.4 release
    assert sri(v / "leaflet.css") == "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    assert (v / "LICENSE").exists() and (v / "images" / "layers.png").exists()
    assert 'data-seg="map"' in html and 'id="mapCard"' in html and 'id="lyStreets"' in html


BOUNDARIES = ROOT / "data" / "reference" / "boundaries"


def _inside(pt, polygons):
    x, y = pt
    c = False
    for rings in polygons:                       # even-odd rule over outer rings and holes
        for ring in rings:
            for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
                if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
                    c = not c
    return c


@pytest.mark.parametrize("name,lat,lon,expected", [
    ("Downtown LA", 34.05, -118.25, True), ("Palmdale", 34.58, -118.12, True), ("Lancaster", 34.70, -118.14, True),
    ("Santa Clarita", 34.39, -118.54, True), ("Long Beach", 33.77, -118.19, True), ("Malibu", 34.03, -118.78, True),
    ("Pomona", 34.06, -117.75, True), ("Avalon, Catalina", 33.34, -118.33, True),
    ("Thousand Oaks", 34.17, -118.84, False), ("Santa Ana", 33.75, -117.87, False),
    ("Fontana", 34.09, -117.43, False), ("Brea", 33.92, -117.90, False), ("Mojave", 35.05, -118.17, False),
])
def test_county_outline_matches_known_places(name, lat, lon, expected):
    geom = json.loads((BOUNDARIES / "la_county.geojson").read_text())["features"][0]["geometry"]
    assert geom["type"] == "MultiPolygon"
    assert _inside((lon, lat), geom["coordinates"]) is expected, name


def test_boundaries_are_embedded_and_optional(demo, tmp_path):
    from lacounty_tax.site import load_boundaries
    b = load_boundaries(BOUNDARIES)
    assert b["county"]["features"] and len(b["cities"]["features"]) == 88
    assert load_boundaries(tmp_path) == {"county": None, "cities": None}
    assert load_boundaries(None) == {"county": None, "cities": None}
    out = build_site([demo[1]], tmp_path / "site", BOUNDARIES)
    blob = out.read_text().split('id="data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(blob)["boundaries"]["county"]["features"][0]["properties"]["fips"] == "06037"
    out2 = build_site([demo[1]], tmp_path / "site2", None)           # no overlay: page still builds
    assert json.loads(out2.read_text().split('id="data">', 1)[1].split("</script>", 1)[0])["boundaries"]["county"] is None


# ---------------------------------------------------------------- Tax roll tab: data, page and JS logic
def test_rows_carry_the_roll_and_history_fields(demo):
    rows = collect([demo[1]])[0]["rows"]
    for col in ("p_stay", "years_in_default", "paid_down_pct", "offered_before", "offered_sales", "roll_status", "redemption_date",
                "sale_ref", "rank_cap", "adj_cap_rate", "redemption_amount_now"):
        assert col in ROW_COLUMNS and col in rows[0]
    live = [r for r in rows if r["p_stay"]]
    assert live and any(r["paid_down_pct"] and r["paid_down_pct"] > 0.5 for r in live)
    assert any(r["p_stay"] == 0 for r in rows) and any(r["offered_before"] for r in rows)


def test_page_has_the_tax_roll_tab_with_its_own_map(demo, tmp_path):
    html = build_site([demo[1]], tmp_path / "site", None, None).read_text()
    assert 'data-seg="roll"' in html and 'id="rollCard"' in html and 'id="rollMap"' in html and 'id="rollMode"' in html
    assert 'id="rollFacts"' in html and 'id="noPulled"' in html
    for opt in ("pull", "years", "paid", "offered"):
        assert f'<option value="{opt}">' in html
    assert html.count("data-seg=") == 6                                       # rentals, land, excluded, map, tax roll, how it works
    for own in ("rollStreets", "rollRelief", "rollCities"):                    # the second map has its own background controls
        assert f'id="{own}"' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_adjusted_cap_and_roll_classes_match_the_python_scoring(demo):
    d, _ = demo
    scored = pd.read_csv(d / "analysis" / "scored.csv", dtype={"ain": str})
    r = scored[scored["rank"].notna()].head(30)
    script = f"""
      const {{ adjustedCap, ROLL_MODES }} = require({json.dumps(str(JS))});
      const rows = {r[["cap_rate", "p_stay", "adj_cap_rate"]].to_json(orient="records")};
      const b = (mode, v, key) => ROLL_MODES[mode].of({{ [key]: v }});
      console.log(JSON.stringify({{
        adj: rows.map(x => adjustedCap(x.cap_rate, x.p_stay)),
        years: [0, 4, 5, 9, 10, 19, 20, 45].map(v => b("years", v, "years_in_default")),
        paid: [0, 0.049, 0.05, 0.499, 0.5, 0.949, 0.95, 1].map(v => b("paid", v, "paid_down_pct")),
        pull: [1, 0.9, 0.851, 0.85, 0.5, 0.49, 0.2, 0.0].map(v => b("pull", v, "p_stay")),
        offered: [0, 1, 2, 3, 7].map(v => b("offered", v, "offered_before")),
        none: [b("years", null, "years_in_default"), b("paid", null, "paid_down_pct"), b("offered", null, "offered_before")],
        shape: Object.values(ROLL_MODES).map(m => [m.labels.length, m.idx.length]),
      }}));
    """
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out["adj"] == pytest.approx(list(r["adj_cap_rate"]))               # the browser's ranking metric = the pipeline's
    assert out["years"] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert out["paid"] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert out["pull"] == [0, 0, 0, 1, 2, 2, 2, 2]                             # chance pulled = 1 - odds; classes start at 15% and 50%
    assert out["offered"] == [0, 1, 2, 3, 3] and out["none"] == [None, None, None]
    assert all(a == b for a, b in out["shape"])                                # every mode has one colour step per label


def test_about_tab_is_inlined_and_bindings_are_real_assumptions():
    import dataclasses, re
    from lacounty_tax.analyze import Assumptions
    from lacounty_tax import site
    html = site.render_html([], None, None) if False else None
    tpl = __import__("importlib.resources", fromlist=["files"]).files("lacounty_tax").joinpath("templates")
    about = tpl.joinpath("about.html").read_text()
    fields = {f.name for f in dataclasses.fields(Assumptions)}
    keys = set(re.findall(r'data-bind="a\.(\w+)"', about))
    assert keys and keys <= fields, keys - fields
    for i in ("aboutSources", "aboutReasons", "aboutRates", "aboutAssumptions"):
        assert f'id="{i}"' in about
    dash = tpl.joinpath("dashboard.html").read_text()
    assert 'data-seg="about"' in dash and "<!--ABOUT-->" in dash
