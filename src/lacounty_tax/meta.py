"""Per-auction metadata (``data/auctions/<id>/meta.json``).

The only field that changes behaviour is ``sale_date``: until that day the auction is
"active" and scheduled runs re-pull Assessor data; afterwards its last snapshot is frozen.
No ``sale_date`` means "still active".
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path


@dataclass
class AuctionMeta:
    auction: str
    sale_date: str | None = None      # ISO date, last day of the sale
    book_url: str | None = None


def meta_path(auction_dir: Path) -> Path:
    return Path(auction_dir) / "meta.json"


def load_meta(auction_dir: Path) -> AuctionMeta | None:
    p = meta_path(auction_dir)
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return AuctionMeta(**{k: raw.get(k) for k in ("auction", "sale_date", "book_url")})


def save_meta(auction_dir: Path, meta: AuctionMeta) -> Path:
    if meta.sale_date:
        date.fromisoformat(meta.sale_date)          # validate early
    p = meta_path(auction_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(meta), indent=2) + "\n")
    return p


def is_active(meta: AuctionMeta | None, today: date) -> bool:
    if meta is None or not meta.sale_date:
        return True
    return today <= date.fromisoformat(meta.sale_date)
