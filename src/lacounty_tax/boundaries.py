"""City boundaries for the map.

Two ways to get ``data/reference/boundaries/la_cities.geojson`` (commit it; boundaries rarely change):

* ``lacounty-tax load-boundaries city_layer.geojson``: from a GeoJSON file downloaded from LA County
  GeoHub ("City and Community Boundaries" dataset -> Download -> GeoJSON). No network needed.
* ``lacounty-tax fetch-boundaries --url <layer URL>``: straight from the county's ArcGIS layer.

The county layer mixes the 88 incorporated cities with unincorporated areas. By default only the
cities are kept; ``--include-unincorporated`` also keeps communities and islands (but never the huge
"AREA" umbrella regions such as Antelope Valley, which overlap the cities).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .geo import GisClient, GisError, in_county

MAX_PAGES = 100
# The county's "City and Community Boundaries" layer in its ArcGIS Online organization (service name taken from the
# organization's service directory recorded in the la-county-elections repo; layer 0 is assumed). That host answered
# HTTP 200 to GitHub Actions runners there, unlike the county's own web servers.
DEFAULT_CITIES_URL = ("https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/"
                      "admin_dist_SDE_DIST_DRP_CITY_COMM_BDY/FeatureServer/0")
NAME_CANDIDATES = ["city_comm_name", "city_name", "cityname", "cityname_alf", "city", "name", "label",
                   "city_label", "city_nm", "comm_name"]


# ---------- simplification ----------
def rdp(points: list[list[float]], tol: float) -> list[list[float]]:
    """Ramer-Douglas-Peucker on a coordinate list (tolerance in the coordinates' units, here degrees)."""
    n = len(points)
    if n < 3 or tol <= 0:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        (x0, y0), (x1, y1) = points[i][:2], points[j][:2]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        best, far = -1.0, -1
        for k in range(i + 1, j):
            x, y = points[k][:2]
            if seg2:
                t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / seg2))
                d = math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))
            else:
                d = math.hypot(x - x0, y - y0)
            if d > best:
                best, far = d, k
        if best > tol:
            keep[far] = True
            stack += [(i, far), (far, j)]
    return [p for p, k in zip(points, keep) if k]


def _ring(ring: list[list[float]], tol: float) -> list[list[float]] | None:
    out = [[round(x, 4), round(y, 4)] for x, y in (p[:2] for p in rdp(ring, tol))]
    return out if len(out) >= 4 else None             # a closed ring needs 4+ points


def simplify_geometry(geom: dict, tol: float) -> dict | None:
    """Simplify and round a Polygon/MultiPolygon; tiny parts that collapse are dropped."""
    if geom["type"] == "Polygon":
        polys = [geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        return None
    out = []
    for rings in polys:
        outer = _ring(rings[0], tol)
        if outer is None:
            continue
        holes = [h for h in (_ring(r, tol) for r in rings[1:]) if h]
        out.append([outer, *holes])
    if not out:
        return None
    return {"type": "Polygon", "coordinates": out[0]} if len(out) == 1 else {"type": "MultiPolygon", "coordinates": out}


# ---------- cleaning ----------
def _filled(v) -> bool:
    return v is not None and str(v).strip() != ""


def pick_name_field(sample, wanted: str | None = None) -> str:
    """The attribute holding the place name: a known candidate that is actually filled in for most rows."""
    props = [sample] if isinstance(sample, dict) else list(sample)
    keys = {k for p in props for k in p}
    if wanted:
        if wanted not in keys:
            raise GisError(f"name field {wanted!r} not in the layer; fields: {sorted(keys)}")
        return wanted
    lower = {k.lower().lstrip("﻿"): k for k in keys}
    best, best_rate = None, 0.0
    for cand in NAME_CANDIDATES:
        if cand in lower:
            k = lower[cand]
            rate = sum(_filled(p.get(k)) for p in props) / max(1, len(props))
            if rate > best_rate + 1e-9:                # earlier candidates win ties
                best, best_rate = k, rate
    if best is None:
        raise GisError("could not tell which field holds the place name (no known name field is filled in); "
                       f"pass --name-field. Fields: {sorted(keys)}")
    return best


def clean_features(features: list[dict], *, name_field: str | None = None, include_unincorporated: bool = False,
                   simplify: float = 0.0002) -> list[dict]:
    props = [f.get("properties") or {} for f in features]
    field = pick_name_field(props, name_field) if props else None
    out = []
    for f, p in zip(features, props):
        geom = f.get("geometry")
        name = str(p.get(field) or "").strip() if field else ""
        if not geom or not geom.get("coordinates") or not name:
            continue
        lower = {k.lower(): v for k, v in p.items()}
        juris = str(lower.get("jurisdiction") or "INCORPORATED CITY").strip().upper()
        kind = "city" if juris == "INCORPORATED CITY" else "unincorporated"
        if kind == "unincorporated" and (not include_unincorporated or str(lower.get("type") or "").strip().upper() == "AREA"):
            continue
        g = simplify_geometry(geom, simplify)
        if g:
            out.append({"type": "Feature", "properties": {"name": name, "kind": kind}, "geometry": g})
    if not out:
        raise GisError("no polygons left after cleaning; check the file/layer is the county's city boundaries "
                       "(and that its jurisdiction/name attributes are filled in)")
    first = out[0]["geometry"]["coordinates"]
    while isinstance(first[0], list):
        first = first[0]
    if not in_county(first[1], first[0]):             # guards against a wrong layer or projected coordinates
        raise GisError(f"coordinates look wrong (first vertex lon/lat {first}); is this the LA County city layer in WGS84?")
    return out


# ---------- sources ----------
def load_geojson(path: Path, **kw) -> dict:
    data = json.loads(Path(path).read_text())
    feats = data.get("features") if isinstance(data, dict) else None
    if not feats:
        raise GisError(f"{path}: not a GeoJSON FeatureCollection with features")
    return {"type": "FeatureCollection", "features": clean_features(feats, **kw)}


def fetch_cities(url: str, *, name_field: str | None = None, include_unincorporated: bool = False, session=None,
                 page_size: int = 500, simplify: float = 0.0002, **client_kw) -> dict:
    """Download the layer as GeoJSON (paged), then clean and simplify it like a local file."""
    url = url.rstrip("/")
    if not url.endswith("/query"):
        url += "/query"
    client = GisClient(url, session=session, **client_kw)
    raw, offset = [], 0
    for _ in range(MAX_PAGES + 1):
        data = client.request({
            "where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326",
            "geometryPrecision": "5", "maxAllowableOffset": str(simplify / 2), "f": "geojson",
            "resultOffset": str(offset), "resultRecordCount": str(page_size),
        })
        page = data.get("features", [])
        raw += page
        if not page or (len(page) < page_size and not data.get("exceededTransferLimit")):
            break
        offset += len(page)
    else:
        raise GisError(f"too many pages (more than {MAX_PAGES}); does the service ignore resultOffset?")
    if not raw:
        raise GisError("the layer returned no polygons; check the URL points at the city boundary layer")
    return {"type": "FeatureCollection",
            "features": clean_features(raw, name_field=name_field, include_unincorporated=include_unincorporated, simplify=simplify)}


def county_outline(features: list[dict], *, simplify: float = 0.0003, min_sq_mi: float = 0.5) -> dict:
    """Dissolve every polygon of the county layer (cities and unincorporated areas) into the county outline.

    The layer's shapes don't quite touch, which leaves thousands of sliver holes, so holes are filled
    (LA County has no enclaves) and specks below ``min_sq_mi`` are dropped; the islands survive.
    Needs shapely (``pip install -e '.[geo]'``); it is used only for this one-off step.
    """
    try:
        from shapely.geometry import MultiPolygon, Polygon, mapping, shape
        from shapely.ops import unary_union
    except ImportError as e:                                   # pragma: no cover - depends on the environment
        raise GisError("--county-out needs shapely: pip install -e '.[geo]'") from e
    geoms = [shape(f["geometry"]).buffer(0) for f in features if f.get("geometry")]
    if not geoms:
        raise GisError("no geometries to dissolve")
    union = unary_union(geoms)
    parts = list(union.geoms) if hasattr(union, "geoms") else [union]
    keep = []
    for g in parts:
        k = 69.0 * math.cos(math.radians(g.centroid.y)) * 69.0     # square miles per square degree
        if g.area * k >= min_sq_mi:
            keep.append(Polygon(g.exterior).simplify(simplify, preserve_topology=True))   # exterior only: fills holes
    if not keep:
        raise GisError("the dissolved outline is empty")
    geom = mapping(MultiPolygon(keep) if len(keep) > 1 else keep[0])
    geom = {"type": geom["type"], "coordinates": json.loads(json.dumps(geom["coordinates"]))}
    geom = simplify_geometry(geom, 0)                          # round to 4 decimals, drop degenerate rings
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"name": "Los Angeles County", "fips": "06037",
                       "source": "LA County GeoHub City and Community Boundaries, all polygons dissolved"},
        "geometry": geom}]}


def load_county_outline(path: Path, **kw) -> dict:
    data = json.loads(Path(path).read_text())
    feats = data.get("features") if isinstance(data, dict) else None
    if not feats:
        raise GisError(f"{path}: not a GeoJSON FeatureCollection with features")
    return county_outline(feats, **kw)


def write_geojson(fc: dict, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc, separators=(",", ":")))
    return len(fc["features"])


def write_cities(url: str, out: Path, **kw) -> int:
    return write_geojson(fetch_cities(url, **kw), out)
