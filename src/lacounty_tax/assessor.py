"""Client for the (undocumented) LA County Assessor portal API.

Endpoints and field names come from the original analysis notebook:
``/api/parceldetail?ain=`` and ``/api/parcel_ownershiphistory?ain=``.

Failure policy:
* transient problems (timeouts, 429, 5xx) are retried with backoff;
* a bot-protection / HTML response raises :class:`BlockedError` immediately,
  so a run stops instead of hammering a site that is refusing us;
* an unknown parcel returns ``None``.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

import requests

BASE = "https://portal.assessor.lacounty.gov/api"
USER_AGENT = "lacounty-tax-research/0.1 (personal research tool)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class AssessorError(RuntimeError):
    pass


class BlockedError(AssessorError):
    """The site answered with a bot-protection page instead of JSON."""


def to_number(value: Any) -> float | None:
    """Assessor fields are strings, often empty; return a float or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(value: Any) -> str | None:
    """MM/DD/YYYY -> ISO date, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


class AssessorClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        min_interval: float = 0.5,
        retries: int = 3,
        backoff: float = 2.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.min_interval, self.retries, self.backoff = min_interval, retries, backoff
        self.timeout, self._sleep, self._clock = timeout, sleep, clock
        self._last = None

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _get_json(self, endpoint: str, ain: str) -> dict | None:
        url = f"{BASE}/{endpoint}"
        last_err = "no attempt made"
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(self.backoff * 2 ** (attempt - 1))
            self._throttle()
            try:
                resp = self.session.get(url, params={"ain": ain}, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            if resp.status_code in RETRY_STATUSES:
                last_err = f"HTTP {resp.status_code}"
                continue
            if resp.status_code == 404:
                return None
            ctype = resp.headers.get("content-type", "")
            body = resp.text or ""
            if "json" not in ctype.lower() and body.lstrip().startswith("<"):
                raise BlockedError(
                    f"{endpoint}: HTTP {resp.status_code} returned an HTML page (likely bot "
                    "protection). Run from a network the site accepts, e.g. your own machine."
                )
            if resp.status_code != 200:
                raise AssessorError(f"{endpoint}: HTTP {resp.status_code} for AIN {ain}")
            try:
                return resp.json()
            except ValueError as e:
                raise AssessorError(f"{endpoint}: invalid JSON for AIN {ain}: {e}") from e
        raise AssessorError(f"{endpoint}: gave up after {self.retries + 1} attempts ({last_err}) for AIN {ain}")

    def parcel_detail(self, ain: str) -> dict | None:
        return self._get_json("parceldetail", ain)

    def ownership_history(self, ain: str) -> dict | None:
        return self._get_json("parcel_ownershiphistory", ain)


PARCEL_FIELDS = {
    "situs_street": "SitusStreet",
    "situs_zip": "SitusZipCode",
    "tax_status": "TaxStatus",
    "tax_defaulted_year": "TaxDefaultedYear",
    "parcel_status": "ParcelStatus",
    "num_units": "NumOfUnits",
    "year_built": "YearBuilt",
    "sqft_main": "SqftMain",
    "sqft_lot": "SqftLot",
    "num_beds": "NumOfBeds",
    "num_baths": "NumOfBaths",
    "land_value": "RollPreparation_LandValue",
    "imp_value": "RollPreparation_ImpValue",
}
NUMERIC = {"num_units", "year_built", "sqft_main", "sqft_lot", "num_beds", "num_baths",
           "land_value", "imp_value"}


def summarize_parcel(detail: dict | None) -> dict:
    """Flatten a ``parceldetail`` response; every field is optional."""
    parcel = (detail or {}).get("Parcel") or {}
    out: dict[str, Any] = {}
    for col, key in PARCEL_FIELDS.items():
        raw = parcel.get(key)
        out[col] = to_number(raw) if col in NUMERIC else (str(raw).strip() if raw not in (None, "") else None)
    return out


def summarize_ownership(history: dict | None) -> dict:
    """Pick the most recent recorded transfer (don't trust list order)."""
    records = (history or {}).get("Parcel_OwnershipHistory") or []
    dated = [(parse_date(r.get("RecordingDate")), r) for r in records]
    dated = [(d, r) for d, r in dated if d]
    if not dated:
        return {"last_transfer_date": None, "last_transfer_doc": None, "last_transfer_reason": None}
    d, r = max(dated, key=lambda x: x[0])
    return {
        "last_transfer_date": d,
        "last_transfer_doc": (r.get("DocumentNumber") or None),
        "last_transfer_reason": (r.get("DocumentReasonCodeDesc") or None),
    }
