"""Build the static dashboard (one self-contained HTML file) from scored auctions."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pandas as pd

ROW_COLUMNS = [
    "item", "ain", "segment", "property_type", "min_bid", "property_address", "zip", "location",
    "tax_status", "year_built", "sqft_main", "sqft_lot", "num_units", "num_beds", "num_baths",
    "land_value", "imp_value", "assessed_value", "bid_to_assessed", "bid_per_lot_sqft",
    "est_monthly_rent", "rehab_estimate", "rehab_risk", "redemption_risk", "fmr_metro_default",
    "excluded", "checklist",
]


def load_auction(auction_dir: Path) -> dict | None:
    """Read one auction's scored output, or None if it hasn't been analyzed yet."""
    scored, summary = auction_dir / "analysis" / "scored.csv", auction_dir / "analysis" / "summary.json"
    if not (scored.exists() and summary.exists()):
        return None
    meta = json.loads(summary.read_text())
    df = pd.read_csv(scored, dtype={"ain": str, "zip": str})
    df = df.reindex(columns=ROW_COLUMNS)
    return {
        "id": meta["auction"],
        "snapshot": meta["snapshot"].removesuffix(".csv"),
        "synthetic": bool(meta.get("synthetic", False)),
        "label": meta.get("label"),
        "assumptions": meta["assumptions"],
        "rows": json.loads(df.to_json(orient="records")),   # NaN -> null
    }


def collect(data_dirs: list[Path]) -> list[dict]:
    found = []
    for base in data_dirs:
        for d in sorted(Path(base).glob("*")):
            a = load_auction(d) if d.is_dir() else None
            if a:
                found.append(a)
    # real auctions first (newest id first), synthetic demos last
    return sorted(found, key=lambda a: (a["synthetic"], [-ord(c) for c in a["id"]]))


def render_html(auctions: list[dict]) -> str:
    tpl = resources.files("lacounty_tax").joinpath("templates")
    html = tpl.joinpath("dashboard.html").read_text()
    css = tpl.joinpath("dashboard.css").read_text()
    js = tpl.joinpath("dashboard.js").read_text()
    data = json.dumps({"auctions": auctions}, separators=(",", ":"), allow_nan=False)
    # Escape every "<" (valid JSON): stops "</script>" and "<!--<script" from confusing the HTML parser.
    data = data.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    # data last, so user-visible text can never be re-expanded by a later replace
    return html.replace("/*CSS*/", css).replace("/*JS*/", js).replace("/*DATA*/", data)


def build_site(data_dirs: list[Path], out_dir: Path) -> Path:
    auctions = collect(data_dirs)
    if not auctions:
        raise FileNotFoundError(
            f"no analyzed auctions found under {[str(d) for d in data_dirs]} "
            "(each needs analysis/scored.csv and summary.json; run `analyze` first)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("")
    out = out_dir / "index.html"
    out.write_text(render_html(auctions))
    return out
