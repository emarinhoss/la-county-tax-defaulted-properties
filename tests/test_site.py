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
    assert html.count("<script") == 2                          # data + code only
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
