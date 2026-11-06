"""Enrich an auction's parcels with Assessor data and track status changes.

Layout::

    data/auctions/<id>/items.csv              (Phase 1)
    data/auctions/<id>/enrichment/<date>.csv  one snapshot per pull
    data/auctions/<id>/status_history.csv     a row whenever a parcel's status changes
    .cache/assessor/<ain>.json                raw responses, git-ignored, makes runs resumable
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from .assessor import (
    PARCEL_FIELDS,
    AssessorClient,
    AssessorError,
    BlockedError,
    summarize_ownership,
    summarize_parcel,
)

SNAPSHOT_COLUMNS = [
    "auction", "ain", "pulled_at", *PARCEL_FIELDS,
    "last_transfer_date", "last_transfer_doc", "last_transfer_reason", "fetch_error",
]
STATUS_COLUMNS = ["auction", "ain", "pulled_at", "tax_status", "parcel_status", "tax_defaulted_year"]
STATUS_FIELDS = ["tax_status", "parcel_status", "tax_defaulted_year"]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _read_cache(path: Path, max_age_hours: float, now: datetime) -> dict | None:
    try:
        entry = json.loads(path.read_text())
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None
    if (now - fetched).total_seconds() <= max_age_hours * 3600:
        return entry
    return None


def fetch_parcel(
    client: AssessorClient, ain: str, cache_dir: Path, max_age_hours: float, now: datetime
) -> dict:
    """Return {'fetched_at', 'detail', 'ownership'} for one parcel, using the cache when fresh."""
    path = cache_dir / f"{ain}.json"
    cached = _read_cache(path, max_age_hours, now)
    if cached:
        return cached
    entry = {
        "fetched_at": now.isoformat(),
        "detail": client.parcel_detail(ain),
        "ownership": client.ownership_history(ain),
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry))
    tmp.replace(path)
    return entry


def build_snapshot(
    items: pd.DataFrame,
    client: AssessorClient,
    cache_dir: Path,
    *,
    max_age_hours: float = 12.0,
    now: datetime | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> pd.DataFrame:
    """Enrich every AIN in ``items``. Raises BlockedError if the site blocks us."""
    now = now or _now()
    rows = []
    total = len(items)
    for n, (auction, ain) in enumerate(zip(items["auction"], items["ain"]), start=1):
        row = {"auction": auction, "ain": ain, "pulled_at": now.isoformat(), "fetch_error": None}
        try:
            entry = fetch_parcel(client, ain, cache_dir, max_age_hours, now)
            if entry["detail"] is None:
                row["fetch_error"] = "parcel not found"
            row.update(summarize_parcel(entry["detail"]))
            row.update(summarize_ownership(entry["ownership"]))
        except BlockedError:
            raise
        except AssessorError as e:
            row["fetch_error"] = str(e)
        rows.append(row)
        if progress:
            progress(n, total, ain)
    return pd.DataFrame(rows).reindex(columns=SNAPSHOT_COLUMNS)


def update_status_history(history_path: Path, snapshot: pd.DataFrame) -> int:
    """Append a row for each parcel whose status differs from its latest recorded one.

    Parcels that failed to fetch are skipped. Returns the number of rows added.
    """
    str_cols = {c: str for c in STATUS_COLUMNS}
    if history_path.exists():
        hist = pd.read_csv(history_path, dtype=str_cols, keep_default_na=False)
    else:
        hist = pd.DataFrame(columns=STATUS_COLUMNS)
    latest = (
        hist.sort_values("pulled_at").groupby("ain").tail(1).set_index("ain")[STATUS_FIELDS].to_dict("index")
        if len(hist) else {}
    )
    new = []
    for rec in snapshot[snapshot["fetch_error"].isna()].to_dict("records"):
        current = {f: ("" if pd.isna(rec[f]) else str(rec[f])) for f in STATUS_FIELDS}
        if latest.get(rec["ain"]) != current:
            new.append({"auction": rec["auction"], "ain": rec["ain"], "pulled_at": rec["pulled_at"], **current})
    if new:
        out = pd.concat([hist, pd.DataFrame(new)], ignore_index=True)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(history_path, index=False)
    return len(new)


def run_enrichment(
    auction_dir: Path,
    client: AssessorClient,
    cache_dir: Path,
    *,
    max_age_hours: float = 12.0,
    limit: int | None = None,
    now: datetime | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[Path, pd.DataFrame, int]:
    """Enrich an auction directory; returns (snapshot path, snapshot, status rows added)."""
    items = pd.read_csv(auction_dir / "items.csv", dtype={"ain": str})
    if limit:
        items = items.head(limit)
    now = now or _now()
    snap = build_snapshot(items, client, cache_dir, max_age_hours=max_age_hours, now=now, progress=progress)
    out = auction_dir / "enrichment" / f"{now.date().isoformat()}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    snap.to_csv(out, index=False)
    changed = update_status_history(auction_dir / "status_history.csv", snap)
    return out, snap, changed
