"""Command line interface: ``lacounty-tax <command>``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .assessor import AssessorClient, BlockedError
from .enrich import run_enrichment
from .fetch import FetchError, download_pdf
from .parse import parse_pdf


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lacounty-tax")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="parse an auction book PDF into items.csv")
    p.add_argument("pdf")
    p.add_argument("--auction", help="auction id such as 2026B (detected from the PDF by default)")
    p.add_argument("--out", help="output CSV (default data/auctions/<auction>/items.csv)")
    p.add_argument("--strict", action="store_true", help="exit non-zero if any warning is raised")
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

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
