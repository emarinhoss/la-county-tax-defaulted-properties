"""Parcel coordinates from LA County's public parcel GIS service (ArcGIS REST).

Each parcel (AIN) is looked up in a parcel polygon layer and reduced to its centroid, so
vacant lots without a street address can be mapped too. Coordinates never change, so they
are stored per auction in ``coordinates.csv`` and only missing AINs are looked up again.

The service URL and AIN field name are settings (``--gis-url``, ``--ain-field``) because
the county can move its layers; defaults are best known values. Failure policy mirrors the
Assessor client: transient errors are retried, a bot-protection/HTML page raises
:class:`~lacounty_tax.assessor.BlockedError` at once, and service-level errors raise
:class:`GisError`.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import requests

from .assessor import BlockedError

# The county's 2025 assessor parcel layer on its ArcGIS Online organization: 2.4 million parcels with the AIN and
# precomputed centre coordinates as plain attributes, so no polygons need downloading. Found with `probe-gis`; it
# matched 1,252 of 1,252 AINs of the 2026B auction book and, unlike the county's own web servers, that host also
# answers GitHub Actions runners.
DEFAULT_URL = (
    "https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/"
    "ASSR_PARCELS_25_View/FeatureServer/0/query"
)
DEFAULT_AIN_FIELD = "AIN"
DEFAULT_LAT_FIELD = "CENTER_LAT"
DEFAULT_LON_FIELD = "CENTER_LON"
USER_AGENT = "lacounty-tax-research/0.1 (personal research tool)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Rough LA County bounds, used to reject results in the wrong coordinate system or order.
LAT_RANGE, LON_RANGE = (32.7, 34.9), (-119.0, -117.5)
COORD_COLUMNS = ["ain", "lat", "lon", "source", "fetched_at"]


class GisError(RuntimeError):
    pass


def ring_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    """(x, y, signed area) of one polygon ring; falls back to the vertex mean if degenerate."""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a /= 2
    if abs(a) < 1e-18:
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys), 0.0
    return cx / (6 * a), cy / (6 * a), a


def polygon_centroid(rings: list[list[list[float]]]) -> tuple[float, float] | None:
    """Area-weighted centroid of an Esri polygon (holes have opposite orientation and subtract)."""
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    parts = [ring_centroid(r) for r in rings]
    total = sum(a for _, _, a in parts)
    if abs(total) < 1e-18:
        x = sum(p[0] for p in parts) / len(parts)
        y = sum(p[1] for p in parts) / len(parts)
        return x, y
    return sum(x * a for x, _, a in parts) / total, sum(y * a for _, y, a in parts) / total


def in_county(lat: float, lon: float) -> bool:
    return LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]


class GisClient:
    def __init__(
        self,
        url: str | None = None,
        ain_field: str | None = None,
        session: requests.Session | None = None,
        *,
        lat_field: str | None = None,
        lon_field: str | None = None,
        batch_size: int = 50,
        min_interval: float = 0.5,
        retries: int = 3,
        backoff: float = 2.0,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        default_layer = not (url or os.environ.get("LACOUNTY_GIS_URL"))
        self.url = url or os.environ.get("LACOUNTY_GIS_URL") or DEFAULT_URL
        self.ain_field = ain_field or os.environ.get("LACOUNTY_GIS_AIN_FIELD") or DEFAULT_AIN_FIELD
        # With lat/lon fields the centre comes straight from attributes (no geometry); without them it is the
        # centroid of the parcel polygon. The defaults only apply to the default layer.
        lat = lat_field if lat_field is not None else os.environ.get("LACOUNTY_GIS_LAT_FIELD", DEFAULT_LAT_FIELD if default_layer else "")
        lon = lon_field if lon_field is not None else os.environ.get("LACOUNTY_GIS_LON_FIELD", DEFAULT_LON_FIELD if default_layer else "")
        self.lat_field, self.lon_field = (lat, lon) if lat and lon else (None, None)
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.batch_size, self.min_interval = batch_size, min_interval
        self.retries, self.backoff, self.timeout = retries, backoff, timeout
        self._sleep, self._clock, self._last = sleep, clock, None

    @property
    def source_label(self) -> str:
        """How coordinates from this client were derived, recorded in coordinates.csv."""
        return "county_gis_center" if self.lat_field else "county_gis"

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _query(self, ains: list[str]) -> dict:
        quoted = ",".join(f"'{a}'" for a in ains)
        params = {"where": f"{self.ain_field} IN ({quoted})", "f": "json"}
        if self.lat_field:
            params.update({"outFields": f"{self.ain_field},{self.lat_field},{self.lon_field}", "returnGeometry": "false"})
        else:
            params.update({"outFields": self.ain_field, "returnGeometry": "true", "outSR": "4326", "geometryPrecision": "6"})
        return self.request(params)

    def request(self, params: dict, url: str | None = None) -> dict:
        """GET the service with retries; raises BlockedError / GisError, returns the parsed JSON."""
        last_err = "no attempt made"
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(self.backoff * 2 ** (attempt - 1))
            self._throttle()
            try:
                resp = self.session.get(url or self.url, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            if resp.status_code in RETRY_STATUSES:
                last_err = f"HTTP {resp.status_code}"
                continue
            body = resp.text or ""
            if "json" not in resp.headers.get("content-type", "").lower() and body.lstrip().startswith("<"):
                raise BlockedError(
                    f"GIS service: HTTP {resp.status_code} returned an HTML page (likely bot protection "
                    f"or a wrong URL: {self.url})."
                )
            if resp.status_code != 200:
                raise GisError(f"GIS service: HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as e:
                raise GisError(f"GIS service: invalid JSON: {e}") from e
            if "error" in data:   # ArcGIS reports problems (bad field, too many records) in a 200
                err = data["error"]
                msg = err.get("message", err) if isinstance(err, dict) else err
                raise GisError(f"GIS service error: {msg}; details: {json.dumps(err)[:200]}")
            return data
        raise GisError(f"GIS service: gave up after {self.retries + 1} attempts ({last_err})")

    def centroids(self, ains: Iterable[str]) -> dict[str, tuple[float, float]]:
        """{ain: (lat, lon)} for the AINs the service knows; unknown or implausible ones are omitted."""
        out: dict[str, tuple[float, float]] = {}
        ains = [a for a in dict.fromkeys(ains)]
        for i in range(0, len(ains), self.batch_size):
            batch = ains[i:i + self.batch_size]
            for feat in self._query(batch).get("features", []):
                attrs = feat.get("attributes") or {}
                ain = re.sub(r"\D", "", str(attrs.get(self.ain_field, "")))
                if ain not in batch:
                    continue
                if self.lat_field:                                  # centre coordinates stored as attributes
                    try:
                        lat, lon = float(attrs[self.lat_field]), float(attrs[self.lon_field])
                    except (KeyError, TypeError, ValueError):
                        continue
                else:                                               # centroid of the parcel polygon
                    c = polygon_centroid((feat.get("geometry") or {}).get("rings") or [])
                    if not c:
                        continue
                    lon, lat = c
                if in_county(lat, lon):
                    out.setdefault(ain, (round(lat, 6), round(lon, 6)))
        return out


def read_coordinates(auction_dir: Path) -> pd.DataFrame:
    p = Path(auction_dir) / "coordinates.csv"
    if not p.exists():
        return pd.DataFrame(columns=COORD_COLUMNS)
    return pd.read_csv(p, dtype={"ain": str})


def geocode_auction(
    auction_dir: Path, client: GisClient, *, now: datetime | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, int]:
    """Look up coordinates for AINs in ``items.csv`` not yet in ``coordinates.csv``.

    Returns (newly found, still unmapped, total). Progress is saved after every batch, so an
    interrupted or blocked run keeps what it found.
    """
    auction_dir = Path(auction_dir)
    items = pd.read_csv(auction_dir / "items.csv", dtype={"ain": str})
    have = read_coordinates(auction_dir)
    known = set(have["ain"])
    todo = [a for a in dict.fromkeys(items["ain"]) if a not in known]
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    found = 0
    for i in range(0, len(todo), client.batch_size):
        batch = todo[i:i + client.batch_size]
        got = client.centroids(batch)
        if got:
            rows = pd.DataFrame(
                [{"ain": a, "lat": lat, "lon": lon, "source": getattr(client, "source_label", "county_gis"), "fetched_at": now.isoformat()}
                 for a, (lat, lon) in got.items()], columns=COORD_COLUMNS)
            have = pd.concat([have, rows], ignore_index=True)
            have.to_csv(auction_dir / "coordinates.csv", index=False)
            found += len(got)
        if progress:
            progress(min(i + client.batch_size, len(todo)), len(todo))
    total = items["ain"].nunique()
    return found, total - len(set(have["ain"])), total
