"""Command line interface: ``lacounty-tax <command>``."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from .assessor import AssessorClient, BlockedError
from .analyze import Assumptions, run_analysis
from .enrich import run_enrichment
from .fetch import FetchError, download_pdf
from .boundaries import DEFAULT_CITIES_URL, load_county_outline, load_geojson, write_cities, write_geojson
from .geo import GisClient, GisError, geocode_auction
from .meta import AuctionMeta, save_meta
from .parse import parse_pdf
from .probe import BASE as PROBE_BASE, markdown_report, probe, sample_ains, to_json
from .history import pull_history, write_history
from .refresh import find_auctions, refresh_auction
from .roll import pull_roll
from .relief import ReliefError, write_relief
from .site import build_site
from .synthetic import make_demo
from .reference import load_safmr_xlsx, parse_safmr_text, read_fmr_csv


def _cmd_parse(args: argparse.Namespace) -> int:
    result = parse_pdf(args.pdf, auction=args.auction)
    if not result.auction:
        print("Could not detect the auction id; pass --auction (e.g. 2026B).", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else Path("data/auctions") / result.auction / "items.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    result.table.to_csv(out, index=False)
    t = result.table
    print(f"{result.auction}: {len(t)} items, {int(t['improved'].sum())} improved, "
          f"{int(t['vacant_lot'].sum())} vacant -> {out}")
    if args.sale_date or args.book_url:
        save_meta(out.parent, AuctionMeta(result.auction, args.sale_date, args.book_url))
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 1 if (result.warnings and args.strict) else 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    try:
        dest = download_pdf(args.url, Path(args.dest))
    except FetchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"saved {dest}")
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    auction_dir = Path(args.data_dir) / args.auction
    if not (auction_dir / "items.csv").exists():
        print(f"error: {auction_dir / 'items.csv'} not found; run `parse` first.", file=sys.stderr)
        return 2
    client = AssessorClient(min_interval=args.delay)

    def progress(n: int, total: int, ain: str) -> None:
        if n % 25 == 0 or n == total:
            print(f"  {n}/{total} (last AIN {ain})", file=sys.stderr)

    try:
        out, snap, changed = run_enrichment(
            auction_dir, client, Path(args.cache_dir),
            max_age_hours=args.cache_hours, limit=args.limit, progress=progress,
        )
    except BlockedError as e:
        print(f"error: {e}\nProgress so far is cached in {args.cache_dir}; re-run to resume.", file=sys.stderr)
        return 3
    failed = int(snap["fetch_error"].notna().sum())
    print(f"{args.auction}: {len(snap)} parcels -> {out} ({failed} failed, {changed} status rows added)")
    return 1 if failed else 0


def _cmd_load_fmr(args: argparse.Namespace) -> int:
    try:
        if Path(args.xlsx).suffix.lower() in (".xlsx", ".xls"):
            table = load_safmr_xlsx(args.xlsx, args.year)
        else:  # text copied from the HUD web page
            table = parse_safmr_text(Path(args.xlsx).read_text(), args.year)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)
    print(f"{len(table)} ZIP codes -> {out}")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    auction_dir = Path(args.data_dir) / args.auction
    try:
        fmr = read_fmr_csv(args.fmr)
        assumptions = Assumptions.from_toml(args.config) if args.config else Assumptions()
        out, res = run_analysis(auction_dir, fmr, assumptions,
                                Path(args.snapshot) if args.snapshot else None,
                                history_path=Path(args.history) if args.history else None)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    ranked = res[res["rank"].notna()]
    print(f"{args.auction}: {len(res)} items scored, {len(ranked)} ranked -> {out}")
    cols = ["rank", "item", "property_address", "min_bid", "est_monthly_rent", "cap_rate",
            "max_bid_for_target", "rehab_risk", "redemption_risk"]
    if len(ranked):
        print(ranked.sort_values("rank").head(args.top)[cols].to_string(index=False))
    return 0


def _cmd_geocode(args: argparse.Namespace) -> int:
    auction_dir = Path(args.data_dir) / args.auction
    if not (auction_dir / "items.csv").exists():
        print(f"error: {auction_dir / 'items.csv'} not found; run `parse` first.", file=sys.stderr)
        return 2
    client = GisClient(args.gis_url, args.ain_field, lat_field=args.lat_field, lon_field=args.lon_field,
                       min_interval=args.delay, batch_size=args.batch_size)

    def progress(done: int, total: int) -> None:
        print(f"  looked up {done}/{total} parcels", file=sys.stderr)

    try:
        found, missing, total = geocode_auction(auction_dir, client, progress=progress)
    except BlockedError as e:
        print(f"error: {e}\nCoordinates found so far are saved; re-run to continue.", file=sys.stderr)
        return 3
    except GisError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{args.auction}: {found} new, {total - missing}/{total} parcels have coordinates "
          f"({missing} not found in the GIS layer) -> {auction_dir / 'coordinates.csv'}")
    if total and total - missing == 0:
        print("hint: the layer answered but matched nothing. Check --gis-url (a parcel layer's .../query URL) and "
              "--ain-field, and whether that layer stores AINs as 10 digits without dashes.", file=sys.stderr)
        return 1
    return 0


def _cmd_fetch_boundaries(args: argparse.Namespace) -> int:
    url = args.url or os.environ.get("LACOUNTY_CITIES_URL") or DEFAULT_CITIES_URL
    try:
        n = write_cities(url, Path(args.out), name_field=args.name_field, simplify=args.simplify,
                         include_unincorporated=args.include_unincorporated)
    except BlockedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except GisError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{n} city polygons -> {args.out}")
    return 0


def _cmd_load_boundaries(args: argparse.Namespace) -> int:
    try:
        fc = load_geojson(Path(args.geojson), name_field=args.name_field, simplify=args.simplify,
                          include_unincorporated=args.include_unincorporated)
    except (GisError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    n = write_geojson(fc, Path(args.out))
    if args.county_out:
        try:
            county = load_county_outline(Path(args.geojson))
        except (GisError, OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        write_geojson(county, Path(args.county_out))
        print(f"county outline (all polygons dissolved) -> {args.county_out}")
    kinds = {k: sum(f["properties"]["kind"] == k for f in fc["features"]) for k in ("city", "unincorporated")}
    print(f"{n} polygons ({kinds['city']} cities, {kinds['unincorporated']} unincorporated) -> {args.out}")
    return 0


def _cmd_build_relief(args: argparse.Namespace) -> int:
    def progress(done: int, total: int) -> None:
        print(f"  terrain tiles {done}/{total}", file=sys.stderr)

    try:
        meta = write_relief(Path(args.county), Path(args.out_dir), zoom=args.zoom, z_factor=args.z_factor, progress=progress)
    except (ReliefError, OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    e = meta["elevation_m"] or {}
    print(f"relief {meta['width']}x{meta['height']} px (z{meta['zoom']}, ~{meta['cell_m']} m/px), "
          f"elevation {e.get('min', 0):.0f}..{e.get('max', 0):.0f} m -> {Path(args.out_dir) / 'la_relief.png'}")
    return 0


def _cmd_probe_gis(args: argparse.Namespace) -> int:
    import pandas as pd
    if args.ains:
        ains = [a.strip() for a in args.ains.split(",") if a.strip()]
    else:
        items = Path(args.data_dir) / args.auction / "items.csv"
        if not items.exists():
            print(f"error: {items} not found; run `parse` first or pass --ains.", file=sys.stderr)
            return 2
        ains = list(pd.read_csv(items, dtype={"ain": str})["ain"])
    sample = sample_ains(ains, args.sample)
    client = GisClient(args.base, lat_field="", lon_field="", min_interval=args.delay, retries=2)

    def progress(done: int, total: int, name: str) -> None:
        print(f"  [{done}/{total}] {name}", file=sys.stderr)

    try:
        reports = probe(client, sample, base=args.base, names=args.name, known_only=args.known_only,
                        max_services=args.max_services, progress=progress)
    except BlockedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except GisError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    md = markdown_report(reports, len(sample), args.base)
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md)
    if args.json:
        Path(args.json).write_text(json.dumps(to_json(reports), indent=1) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(md)
    return 0 if any(r.usable for r in reports) else 1


def _cmd_pull_roll(args: argparse.Namespace) -> int:
    auction_dir = Path(args.data_dir) / args.auction
    if not (auction_dir / "items.csv").exists():
        print(f"error: {auction_dir / 'items.csv'} not found; run `parse` first.", file=sys.stderr)
        return 2
    client = GisClient(None, lat_field="", lon_field="", min_interval=args.delay)

    def progress(done: int, total: int) -> None:
        print(f"  roll lookups {done}/{total}", file=sys.stderr)

    try:
        out, s = pull_roll(auction_dir, client, progress=progress)
    except BlockedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except GisError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    live = s[s["on_roll"] & ~s["redeemed"]]
    print(f"{args.auction}: {int(s['on_roll'].sum())}/{len(s)} parcels on the roll ({len(live)} live, "
          f"{int(s['redeemed'].sum())} redeemed or removed) -> {out}")
    return 0


def _cmd_pull_history(args: argparse.Namespace) -> int:
    client = GisClient(None, lat_field="", lon_field="", min_interval=args.delay)
    try:
        df = pull_history(client, only=args.sale)
    except BlockedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except GisError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = write_history(df, Path(args.out))
    per = df.groupby("sale").size().to_dict()
    print(f"{len(df)} rows from {len(per)} earlier books ({', '.join(f'{k}: {v}' for k, v in per.items())}) -> {out}")
    return 0


def _cmd_make_demo(args: argparse.Namespace) -> int:
    fmr = read_fmr_csv(args.fmr)
    if "metro_default" not in fmr:
        fmr["metro_default"] = False
    path = make_demo(Path(args.out), fmr)
    print(f"synthetic demo auction -> {path}")
    return 0


def _cmd_build_site(args: argparse.Namespace) -> int:
    try:
        out = build_site([Path(d) for d in (args.data_dir or ["data/auctions"])], Path(args.out),
                         Path(args.boundaries_dir), Path(args.relief_dir) if args.relief_dir else None)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"dashboard -> {out}")
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    dirs = find_auctions(Path(args.data_dir), args.auction)
    if not dirs:
        print("no auctions with an items.csv; nothing to refresh")
        return 0
    client = AssessorClient(min_interval=args.delay)
    gis = None if args.no_geocode else GisClient(args.gis_url, args.ain_field, lat_field=args.lat_field,
                                                 lon_field=args.lon_field, min_interval=args.delay)
    today = date.fromisoformat(args.today) if args.today else date.today()
    results = []
    for d in dirs:
        r = refresh_auction(d, client, cache_dir=Path(args.cache_dir), fmr_path=Path(args.fmr),
                            config_path=Path(args.config) if args.config else None, today=today,
                            force=args.force, max_age_hours=args.cache_hours, limit=args.limit,
                            gis_client=gis, history_path=Path(args.history) if args.history else None)
        results.append(r)
        print(f"{r.auction}: {r.status}: {r.detail}")
        if r.status == "blocked":                     # same site for every auction: stop hammering it
            break
    blocked = any(r.status == "blocked" for r in results)
    if blocked and os.environ.get("GITHUB_ACTIONS"):
        print("::warning title=Assessor site blocked this runner::Data was not refreshed; "
              "run `lacounty-tax enrich` from your own machine and commit the result.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("### Refresh\n\n| Auction | Status | Detail |\n|---|---|---|\n")
            fh.writelines(f"| {r.auction} | {r.status} | {r.detail} |\n" for r in results)
    return 3 if blocked else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lacounty-tax")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="parse an auction book PDF into items.csv")
    p.add_argument("pdf")
    p.add_argument("--auction", help="auction id such as 2026B (detected from the PDF by default)")
    p.add_argument("--out", help="output CSV (default data/auctions/<auction>/items.csv)")
    p.add_argument("--strict", action="store_true", help="exit non-zero if any warning is raised")
    p.add_argument("--sale-date", help="last day of the sale (YYYY-MM-DD); scheduled refreshes stop after it")
    p.add_argument("--book-url", help="where the book came from, recorded in meta.json")
    p.set_defaults(func=_cmd_parse)

    f = sub.add_parser("fetch", help="download an auction book PDF")
    f.add_argument("url")
    f.add_argument("--dest", default="data/inbox/book.pdf")
    f.set_defaults(func=_cmd_fetch)

    e = sub.add_parser("enrich", help="pull Assessor data for an auction's parcels")
    e.add_argument("auction", help="auction id such as 2026B")
    e.add_argument("--data-dir", default="data/auctions")
    e.add_argument("--cache-dir", default=".cache/assessor")
    e.add_argument("--cache-hours", type=float, default=12.0,
                   help="reuse cached responses younger than this (default 12; 0 forces a refresh)")
    e.add_argument("--delay", type=float, default=0.5, help="seconds between requests (default 0.5)")
    e.add_argument("--limit", type=int, help="only the first N parcels (for trying it out)")
    e.set_defaults(func=_cmd_enrich)

    m = sub.add_parser("load-fmr", help="convert a HUD SAFMR .xlsx, or the table copied from HUD's web page as text, into data/reference/hud_safmr.csv")
    m.add_argument("xlsx")
    m.add_argument("--year", type=int, help="HUD fiscal year, recorded in the output")
    m.add_argument("--out", default="data/reference/hud_safmr.csv")
    m.set_defaults(func=_cmd_load_fmr)

    z = sub.add_parser("analyze", help="score an auction's parcels as rent-and-hold candidates")
    z.add_argument("auction")
    z.add_argument("--data-dir", default="data/auctions")
    z.add_argument("--fmr", default="data/reference/hud_safmr.csv")
    z.add_argument("--config", help="TOML file overriding the default assumptions")
    z.add_argument("--snapshot", help="enrichment snapshot CSV (default: the latest)")
    z.add_argument("--history", default="data/reference/auction_history.csv",
                   help="earlier-auction history from `pull-history` (used for the 'offered before' count)")
    z.add_argument("--top", type=int, default=15, help="rows to print (default 15)")
    z.set_defaults(func=_cmd_analyze)

    g = sub.add_parser("geocode", help="look up parcel centroids from the county GIS (for the map)")
    g.add_argument("auction")
    g.add_argument("--data-dir", default="data/auctions")
    g.add_argument("--gis-url", help="parcel layer query URL (default: LA County public parcel service)")
    g.add_argument("--ain-field", help="AIN field name in that layer (default AIN)")
    g.add_argument("--lat-field", help="attribute holding the parcel centre latitude (default CENTER_LAT for the default layer; "
                   "pass '' to use polygon centroids instead)")
    g.add_argument("--lon-field", help="attribute holding the parcel centre longitude (default CENTER_LON for the default layer)")
    g.add_argument("--delay", type=float, default=0.5)
    g.add_argument("--batch-size", type=int, default=50)
    g.set_defaults(func=_cmd_geocode)

    b = sub.add_parser("fetch-boundaries", help="download city boundaries for the map from the county's GIS layer")
    b.add_argument("--url", help="city boundary layer URL (default: the county's City and Community Boundaries layer on "
                   "ArcGIS Online; or set LACOUNTY_CITIES_URL)")
    b.add_argument("--name-field", help="attribute holding the city name (detected by default)")
    b.add_argument("--simplify", type=float, default=0.0002, help="simplification tolerance in degrees (default ~20 m)")
    b.add_argument("--out", default="data/reference/boundaries/la_cities.geojson")
    b.add_argument("--include-unincorporated", action="store_true",
                   help="also keep unincorporated communities and islands (never the large AREA regions)")
    b.set_defaults(func=_cmd_fetch_boundaries)

    lb = sub.add_parser("load-boundaries", help="build the map's city boundaries from a downloaded GeoJSON file")
    lb.add_argument("geojson", help="GeoJSON downloaded from LA County GeoHub (City and Community Boundaries)")
    lb.add_argument("--name-field", help="attribute holding the place name (detected by default)")
    lb.add_argument("--simplify", type=float, default=0.0002, help="simplification tolerance in degrees (default ~20 m)")
    lb.add_argument("--include-unincorporated", action="store_true",
                    help="also keep unincorporated communities and islands (never the large AREA regions)")
    lb.add_argument("--county-out", help="also dissolve all polygons into the county outline (needs shapely: pip install -e '.[geo]')")
    lb.add_argument("--out", default="data/reference/boundaries/la_cities.geojson")
    lb.set_defaults(func=_cmd_load_boundaries)

    rl = sub.add_parser("build-relief", help="build the shaded-relief (elevation) background image for the map")
    rl.add_argument("--county", default="data/reference/boundaries/la_county.geojson", help="county outline to clip to")
    rl.add_argument("--out-dir", default="data/reference/relief")
    rl.add_argument("--zoom", type=int, default=10, help="terrain tile zoom (10 is ~110 m/pixel; 9 is smaller and coarser)")
    rl.add_argument("--z-factor", type=float, default=2.0, help="vertical exaggeration of the shading (default 2)")
    rl.set_defaults(func=_cmd_build_relief)

    pr = sub.add_parser("probe-gis", help="find which county ArcGIS layers can turn AINs into coordinates")
    pr.add_argument("--auction", default="2026B", help="auction whose AINs form the sample (default 2026B)")
    pr.add_argument("--data-dir", default="data/auctions")
    pr.add_argument("--ains", help="comma-separated AINs instead of an auction's items.csv")
    pr.add_argument("--sample", type=int, default=40, help="how many AINs to test (default 40)")
    pr.add_argument("--base", default=PROBE_BASE, help="ArcGIS services root of the organization to scan")
    pr.add_argument("--name", action="append", help="probe only this service; repeatable")
    pr.add_argument("--known-only", action="store_true", help="only the short list of known candidates (fast)")
    pr.add_argument("--max-services", type=int, help="stop after N services")
    pr.add_argument("--delay", type=float, default=0.2, help="seconds between requests (default 0.2)")
    pr.add_argument("--out", help="write the Markdown report here")
    pr.add_argument("--json", help="write the full results as JSON here")
    pr.set_defaults(func=_cmd_probe_gis)

    pl = sub.add_parser("pull-roll", help="pull the Tax Collector's defaulted tax roll for an auction's parcels")
    pl.add_argument("auction")
    pl.add_argument("--data-dir", default="data/auctions")
    pl.add_argument("--delay", type=float, default=0.2)
    pl.set_defaults(func=_cmd_pull_roll)

    ph = sub.add_parser("pull-history", help="list which parcels appeared in earlier auction books")
    ph.add_argument("--out", default="data/reference/auction_history.csv")
    ph.add_argument("--sale", action="append", help="only this sale id (e.g. 2025B); repeatable")
    ph.add_argument("--delay", type=float, default=0.2)
    ph.set_defaults(func=_cmd_pull_history)

    d = sub.add_parser("make-demo", help="write a clearly fake demo auction for trying the dashboard")
    d.add_argument("--out", default="demo/auctions")
    d.add_argument("--fmr", default="data/reference/hud_safmr.csv")
    d.set_defaults(func=_cmd_make_demo)

    w = sub.add_parser("build-site", help="build the static dashboard (docs/index.html)")
    w.add_argument("--data-dir", action="append", help="folder of auctions; repeatable (default data/auctions)")
    w.add_argument("--out", default="docs")
    w.add_argument("--relief-dir", default="data/reference/relief",
                   help="folder with la_relief.png/.json from `build-relief` (elevation shading for the map background)")
    w.add_argument("--boundaries-dir", default="data/reference/boundaries",
                   help="folder with la_county.geojson (and optionally la_cities.geojson) for the map")
    w.set_defaults(func=_cmd_build_site)

    r = sub.add_parser("refresh", help="re-pull Assessor data for active auctions and re-score them (used by CI)")
    r.add_argument("--auction", action="append", help="only this auction id; repeatable (default: all)")
    r.add_argument("--data-dir", default="data/auctions")
    r.add_argument("--cache-dir", default=".cache/assessor")
    r.add_argument("--cache-hours", type=float, default=12.0)
    r.add_argument("--delay", type=float, default=0.5)
    r.add_argument("--fmr", default="data/reference/hud_safmr.csv")
    r.add_argument("--config", default="config/assumptions.toml")
    r.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    r.add_argument("--force", action="store_true", help="refresh even if the sale date has passed")
    r.add_argument("--limit", type=int, help="only the first N parcels per auction")
    r.add_argument("--no-geocode", action="store_true", help="skip the parcel coordinate, tax roll and history lookups")
    r.add_argument("--history", default="data/reference/auction_history.csv")
    r.add_argument("--gis-url", help="parcel layer query URL (default: LA County public parcel service)")
    r.add_argument("--ain-field", help="AIN field name in that layer (default AIN)")
    r.add_argument("--lat-field", help="attribute holding the parcel centre latitude (see `geocode --help`)")
    r.add_argument("--lon-field", help="attribute holding the parcel centre longitude")
    r.set_defaults(func=_cmd_refresh)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
