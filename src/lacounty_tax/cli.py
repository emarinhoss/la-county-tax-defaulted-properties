"""Command line interface: ``lacounty-tax <command>``."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .assessor import AssessorClient, BlockedError
from .analyze import Assumptions, run_analysis
from .enrich import run_enrichment
from .fetch import FetchError, download_pdf
from .meta import AuctionMeta, save_meta
from .parse import parse_pdf
from .refresh import find_auctions, refresh_auction
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
                                Path(args.snapshot) if args.snapshot else None)
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


def _cmd_make_demo(args: argparse.Namespace) -> int:
    fmr = read_fmr_csv(args.fmr)
    if "metro_default" not in fmr:
        fmr["metro_default"] = False
    path = make_demo(Path(args.out), fmr)
    print(f"synthetic demo auction -> {path}")
    return 0


def _cmd_build_site(args: argparse.Namespace) -> int:
    try:
        out = build_site([Path(d) for d in (args.data_dir or ["data/auctions"])], Path(args.out))
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
    today = date.fromisoformat(args.today) if args.today else date.today()
    results = []
    for d in dirs:
        r = refresh_auction(d, client, cache_dir=Path(args.cache_dir), fmr_path=Path(args.fmr),
                            config_path=Path(args.config) if args.config else None, today=today,
                            force=args.force, max_age_hours=args.cache_hours, limit=args.limit)
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
    z.add_argument("--top", type=int, default=15, help="rows to print (default 15)")
    z.set_defaults(func=_cmd_analyze)

    d = sub.add_parser("make-demo", help="write a clearly fake demo auction for trying the dashboard")
    d.add_argument("--out", default="demo/auctions")
    d.add_argument("--fmr", default="data/reference/hud_safmr.csv")
    d.set_defaults(func=_cmd_make_demo)

    w = sub.add_parser("build-site", help="build the static dashboard (docs/index.html)")
    w.add_argument("--data-dir", action="append", help="folder of auctions; repeatable (default data/auctions)")
    w.add_argument("--out", default="docs")
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
    r.set_defaults(func=_cmd_refresh)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
