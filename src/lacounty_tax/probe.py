"""Find which ArcGIS layers can turn parcel numbers (AINs) into coordinates.

    lacounty-tax probe-gis --auction 2026B

scans the county's ArcGIS Online organization for parcel-like services, checks each layer's fields,
then tests it with real AINs from an auction book. It reports coverage (how many of the sample AINs
the layer knows) and how coordinates can be had (as plain attributes, or from the polygon), and prints
the ``geocode`` flags to use. Run it again whenever the county moves or renames its layers.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Callable

from .geo import GisClient, GisError, in_county, polygon_centroid

BASE = "https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services"
CANDIDATE_PATTERN = re.compile(r"parcel|assessor|assr|tax.?default|auction|roll", re.I)
# services we already know are good starting points, probed even in --known-only mode
KNOWN = ["ASSR_PARCELS_25_View", "Parcels_2022Roll", "Public_Parcels_Poly", "Assessor Parcel Data Roll 2026",
         "Secured Defaulted Tax Roll", "2025B_Tax_Defaulted_Properties", "2025A_Tax_Defaulted_Parcels"]
AIN_FIELD = re.compile(r"^(ain|apn|ain_?\w+|\w+_ain|assessment_ain|master_ain|apn_1|assessorid)$", re.I)
LAT_FIELDS = ("center_lat", "latitude", "lat", "y_coord", "point_y")
LON_FIELDS = ("center_lon", "longitude", "lon", "lng", "x_coord", "point_x")
NUMERIC = ("Integer", "SmallInteger", "Double", "Single", "OID")


@dataclass
class LayerReport:
    service: str
    layer_id: int
    name: str
    kind: str                       # "layer" (has shapes) or "table" (attributes only)
    geometry: str | None = None
    rows: int | None = None
    ain_field: str | None = None
    lat_field: str | None = None
    lon_field: str | None = None
    sampled: int = 0
    found: int = 0                  # distinct sample AINs the layer knows
    matched_rows: int = 0           # rows returned for them (more than `found` for multi-year tables)
    coords: str | None = None       # "attributes", "geometry" or None
    query_url: str = ""
    note: str = ""

    @property
    def hit_rate(self) -> float:
        return self.found / self.sampled if self.sampled else 0.0

    @property
    def rows_per_ain(self) -> float:
        return self.matched_rows / self.found if self.found else 0.0

    @property
    def usable(self) -> bool:
        return self.coords is not None and self.hit_rate >= 0.9


def sample_ains(ains: list[str], n: int = 40) -> list[str]:
    """Up to n AINs spread evenly through the list."""
    ains = list(dict.fromkeys(a.zfill(10) for a in ains))
    if len(ains) <= n:
        return ains
    step = len(ains) / n
    return [ains[int(i * step)] for i in range(n)]


def _pick(fields: list[str], names: tuple[str, ...]) -> str | None:
    lower = {f.lower(): f for f in fields}
    return next((lower[n] for n in names if n in lower), None)


def _where(field_name: str, ftype: str, ains: list[str]) -> str:
    numeric = any(t in ftype for t in NUMERIC)
    vals = ",".join(a if numeric else f"'{a}'" for a in ains)
    return f"{field_name} IN ({vals})"


def inspect_layer(client: GisClient, svc_url: str, service: str, item: dict, kind: str, ains: list[str]) -> LayerReport:
    rep = LayerReport(service=service, layer_id=item["id"], name=item.get("name", ""), kind=kind)
    layer_url = f"{svc_url}/{item['id']}"
    rep.query_url = f"{layer_url}/query"
    meta = client.request({"f": "json"}, url=layer_url)
    rep.geometry = meta.get("geometryType")
    fields = {f["name"]: f.get("type", "") for f in meta.get("fields", [])}
    candidates = [f for f in fields if AIN_FIELD.match(f)]
    # prefer the plain AIN column over e.g. Assessment_AIN or Master_AIN, then APN-style names
    rep.ain_field = next((f for f in candidates if f.lower() == "ain"), None) or next(
        (f for f in candidates if f.lower() == "apn"), None) or (candidates[0] if candidates else None)
    rep.lat_field, rep.lon_field = _pick(list(fields), LAT_FIELDS), _pick(list(fields), LON_FIELDS)
    if not rep.lat_field or not rep.lon_field:
        rep.lat_field = rep.lon_field = None
    try:
        rep.rows = client.request({"where": "1=1", "returnCountOnly": "true", "f": "json"}, url=rep.query_url).get("count")
    except GisError as e:
        rep.note = f"count failed: {e}"
    if not rep.ain_field:
        rep.note = (rep.note + " no AIN-like field").strip()
        return rep
    # look the sample AINs up in this layer
    rep.sampled = len(ains)
    params = {"where": _where(rep.ain_field, fields[rep.ain_field], ains), "f": "json"}
    if rep.lat_field:
        params.update({"outFields": f"{rep.ain_field},{rep.lat_field},{rep.lon_field}", "returnGeometry": "false"})
    elif rep.geometry == "esriGeometryPolygon":
        params.update({"outFields": rep.ain_field, "returnGeometry": "true", "outSR": "4326", "geometryPrecision": "6"})
    else:
        params.update({"outFields": rep.ain_field, "returnGeometry": "false"})
    feats = client.request(params, url=rep.query_url).get("features", [])
    ok_coords, seen = 0, set()
    for f in feats:
        a = f.get("attributes") or {}
        seen.add(re.sub(r"\D", "", str(a.get(rep.ain_field, ""))).zfill(10))
        try:
            if rep.lat_field:
                lat, lon = float(a[rep.lat_field]), float(a[rep.lon_field])
            else:
                c = polygon_centroid((f.get("geometry") or {}).get("rings") or [])
                lon, lat = c if c else (None, None)
            ok_coords += lat is not None and in_county(lat, lon)
        except (KeyError, TypeError, ValueError):
            pass
    rep.found = len(seen & set(ains))
    rep.matched_rows = len(feats)
    if rep.found and ok_coords >= 0.9 * len(feats):
        rep.coords = "attributes" if rep.lat_field else "geometry"
    elif feats:
        rep.note = (rep.note + " found rows but no usable coordinates").strip()
    return rep


def discover(client: GisClient, base: str = BASE) -> list[str]:
    """Names of FeatureServer services in the organization."""
    data = client.request({"f": "json"}, url=base)
    return [s["name"] for s in data.get("services", []) if s.get("type") == "FeatureServer"]


def probe(client: GisClient, ains: list[str], *, base: str = BASE, names: list[str] | None = None,
          known_only: bool = False, max_services: int | None = None,
          progress: Callable[[int, int, str], None] | None = None) -> list[LayerReport]:
    """Probe candidate services; per-service problems are recorded, a blocked site aborts."""
    if names:
        todo = names
    else:
        all_names = discover(client, base)
        known = [n for n in KNOWN if n in all_names]
        scanned = [n for n in all_names if CANDIDATE_PATTERN.search(n)]
        todo = known if known_only else list(dict.fromkeys(known + scanned))
    if max_services:
        todo = todo[:max_services]
    reports: list[LayerReport] = []
    for i, name in enumerate(todo, start=1):
        svc_url = f"{base}/{urllib.parse.quote(name)}/FeatureServer"
        try:
            info = client.request({"f": "json"}, url=svc_url)
            items = [(it, "layer") for it in info.get("layers", [])] + [(it, "table") for it in info.get("tables", [])]
            for it, kind in items:
                try:
                    reports.append(inspect_layer(client, svc_url, name, it, kind, ains))
                except GisError as e:
                    reports.append(LayerReport(name, it["id"], it.get("name", ""), kind, note=f"error: {str(e)[:80]}"))
        except GisError as e:
            reports.append(LayerReport(name, -1, "", "layer", note=f"error: {str(e)[:80]}"))
        if progress:
            progress(i, len(todo), name)
    return reports


def rank(reports: list[LayerReport]) -> list[LayerReport]:
    """Best first: usable; one row per parcel (not a multi-year table); attribute coordinates over polygons;
    a spatial layer over a table; higher hit rate; then the broader layer (more parcels generalise better than a
    small layer that merely contains the sample)."""
    return sorted(reports, key=lambda r: (not r.usable, r.rows_per_ain > 1.5, r.coords != "attributes", r.kind != "layer",
                                          -r.hit_rate, -(r.rows or 0)))


def geocode_flags(r: LayerReport) -> str:
    flags = [f'--gis-url "{r.query_url}"', f"--ain-field {r.ain_field}"]
    flags += [f"--lat-field {r.lat_field}", f"--lon-field {r.lon_field}"] if r.coords == "attributes" else ["--lat-field ''"]
    return " ".join(flags)


def markdown_report(reports: list[LayerReport], sampled: int, base: str = BASE) -> str:
    ranked = rank(reports)
    usable = [r for r in ranked if r.usable]
    hits = [r for r in ranked if r.found]
    lines = ["# Parcel layer probe", "", f"Organization: `{base}`", f"Sample: {sampled} AINs. Layers inspected: {len(reports)}. "
             f"Layers that returned any of the sample: {len(hits)}. Usable for coordinates (>= 90% of the sample): {len(usable)}.", ""]
    if usable:
        b = usable[0]
        lines += ["## Recommended", "", f"**{b.service}** / layer {b.layer_id} (`{b.name}`): {b.found}/{b.sampled} sample AINs found, "
                  f"{b.rows:,} rows, coordinates from {b.coords}." if b.rows else
                  f"**{b.service}** / layer {b.layer_id}: {b.found}/{b.sampled} found, coordinates from {b.coords}.",
                  "", "```bash", f"lacounty-tax geocode <auction> {geocode_flags(b)}", "```", ""]
    else:
        lines += ["## Recommended", "", "**No layer matched 90% of the sample with usable coordinates.** Check the sample AINs are "
                  "10 digits without dashes, and that the organization URL is right.", ""]
    lines += ["## Layers that know the sample AINs (rows/AIN above 1 means one row per year or assessment, not per parcel)", "", "| service | layer | kind | rows | AIN field | found | rows/AIN | coordinates |", "|---|---|---|---|---|---|---|---|"]
    for r in hits:
        rows = f"{r.rows:,}" if r.rows is not None else "?"
        lines.append(f"| {r.service} | {r.layer_id} {r.name} | {r.kind} | {rows} | {r.ain_field} | {r.found}/{r.sampled} | {r.rows_per_ain:.1f} | {r.coords or 'none'} |")
    others = [r for r in ranked if r.ain_field and not r.found and not r.note.startswith("error")]
    auctions = [r for r in others if re.search(r"tax.?def|auction", r.service, re.I)]
    if auctions:
        lines += ["", "## Per-auction layers (other sales; their AINs do not overlap this sample)", ""]
        lines += [f"- {r.service} / {r.layer_id}: {r.rows:,} rows" if r.rows else f"- {r.service} / {r.layer_id}" for r in auctions]
    tables = [r for r in ranked if r.kind == "table" and r.ain_field and r.rows]
    if tables:
        lines += ["", "## Attribute tables keyed by AIN (no shapes)", ""]
        lines += [f"- {r.service} / {r.layer_id} `{r.name}`: {r.rows:,} rows, found {r.found}/{r.sampled} sample AINs" for r in tables]
    errs = [r for r in reports if r.note.startswith("error")]
    if errs:
        lines += ["", f"## Errors ({len(errs)})", ""] + [f"- {r.service}: {r.note}" for r in errs[:15]]
    return "\n".join(lines) + "\n"


def to_json(reports: list[LayerReport]) -> list[dict]:
    return [asdict(r) | {"hit_rate": r.hit_rate, "usable": r.usable} for r in rank(reports)]
