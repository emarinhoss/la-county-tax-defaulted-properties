"""One scheduled refresh: re-pull Assessor data for active auctions, then re-score them."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .analyze import Assumptions, run_analysis
from .assessor import AssessorClient, BlockedError
from .enrich import run_enrichment
from .geo import GisClient, GisError, geocode_auction
from .meta import is_active, load_meta
from .reference import read_fmr_csv


@dataclass
class RefreshResult:
    auction: str
    status: str          # refreshed | frozen | blocked | enriched_no_fmr
    detail: str = ""


def find_auctions(data_dir: Path, only: list[str] | None = None) -> list[Path]:
    dirs = [d for d in sorted(Path(data_dir).glob("*")) if (d / "items.csv").exists()]
    if only:
        dirs = [d for d in dirs if d.name in only]
    return dirs


def refresh_auction(
    auction_dir: Path, client: AssessorClient, *, cache_dir: Path, fmr_path: Path | None,
    config_path: Path | None, today: date, force: bool = False,
    max_age_hours: float = 12.0, limit: int | None = None,
    gis_client: GisClient | None = None,
) -> RefreshResult:
    name = auction_dir.name
    meta = load_meta(auction_dir)
    if not force and not is_active(meta, today):
        return RefreshResult(name, "frozen", f"sale date {meta.sale_date} has passed; keeping the last snapshot")
    try:
        out, snap, changed = run_enrichment(auction_dir, client, cache_dir,
                                            max_age_hours=max_age_hours, limit=limit)
    except BlockedError as e:
        return RefreshResult(name, "blocked", str(e))
    failed = int(snap["fetch_error"].notna().sum())
    detail = f"{len(snap)} parcels pulled ({failed} failed), {changed} status change(s)"
    if gis_client is not None:
        detail += "; " + _geocode_note(auction_dir, gis_client)
    if not fmr_path or not Path(fmr_path).exists():
        return RefreshResult(name, "enriched_no_fmr", detail + "; no HUD rent file, so not re-scored")
    assumptions = Assumptions.from_toml(config_path) if config_path and Path(config_path).exists() else Assumptions()
    _, scored = run_analysis(auction_dir, read_fmr_csv(fmr_path), assumptions, out)
    return RefreshResult(name, "refreshed", detail + f"; {int(scored['rank'].notna().sum())} ranked rentals")


def _geocode_note(auction_dir: Path, gis_client: GisClient) -> str:
    """Fill in missing parcel coordinates; never fails the refresh (the map is optional)."""
    try:
        found, missing, total = geocode_auction(auction_dir, gis_client)
    except (BlockedError, GisError) as e:
        return f"coordinates skipped ({e})"
    return f"coordinates +{found} ({total - missing}/{total} mapped)"
