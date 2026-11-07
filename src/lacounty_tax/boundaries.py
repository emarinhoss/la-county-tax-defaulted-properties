"""City boundaries for the map, downloaded from an ArcGIS layer (e.g. LA County's city layer).

Run once (the file is committed; boundaries rarely change)::

    lacounty-tax fetch-boundaries --url https://.../MapServer/<n>

Find the URL on LA County's GeoHub: open the "City Boundaries" dataset, choose "I want to use
this" -> "API resources" and copy the feature/map service layer URL (it ends in ``/<number>``;
``/query`` is added for you). The name field is detected, or pass ``--name-field``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .geo import GisClient, GisError, in_county

MAX_PAGES = 100
NAME_CANDIDATES = ["city_name", "cityname", "city", "name", "label", "city_label", "city_nm", "comm_name"]


def _round(c, nd=4):
    return [round(v, nd) for v in c] if c and isinstance(c[0], (int, float)) else [_round(x, nd) for x in c]


def pick_name_field(props: dict, wanted: str | None = None) -> str:
    if wanted:
        if wanted not in props:
            raise GisError(f"name field {wanted!r} not in the layer; fields: {sorted(props)}")
        return wanted
    lower = {k.lower(): k for k in props}
    for cand in NAME_CANDIDATES:
        if cand in lower:
            return lower[cand]
    raise GisError(f"could not tell which field holds the city name; pass --name-field. Fields: {sorted(props)}")


def fetch_cities(url: str, *, name_field: str | None = None, session=None, page_size: int = 500,
                 simplify: float = 0.0002, **client_kw) -> dict:
    """FeatureCollection of {name} polygons, simplified (``simplify`` is in degrees, ~0.0002 = 20 m)."""
    url = url.rstrip("/")
    if not url.endswith("/query"):
        url += "/query"
    client = GisClient(url, session=session, **client_kw)
    features, offset, field = [], 0, name_field
    for _ in range(MAX_PAGES + 1):
        data = client.request({
            "where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326",
            "geometryPrecision": "4", "maxAllowableOffset": str(simplify), "f": "geojson",
            "resultOffset": str(offset), "resultRecordCount": str(page_size),
        })
        page = data.get("features", [])
        for f in page:
            props = f.get("properties") or {}
            field = field or pick_name_field(props)
            geom = f.get("geometry")
            if not geom or not geom.get("coordinates") or field not in props:
                continue
            features.append({"type": "Feature", "properties": {"name": str(props[field]).strip()},
                             "geometry": {"type": geom["type"], "coordinates": _round(geom["coordinates"])}})
        if len(page) < page_size and not data.get("exceededTransferLimit"):
            break
        if not page:
            break
        offset += len(page)
    else:
        raise GisError(f"too many pages (more than {MAX_PAGES}); does the service ignore resultOffset?")
    if not features:
        raise GisError("the layer returned no polygons; check the URL points at the city boundary layer")
    # sanity: the layer should be in LA County (guards against a wrong layer or coordinate system)
    first = features[0]["geometry"]["coordinates"]
    while isinstance(first[0], list):
        first = first[0]
    if not in_county(first[1], first[0]):
        raise GisError(f"coordinates look wrong (first vertex lon/lat {first}); is this the LA County city layer?")
    return {"type": "FeatureCollection", "features": features}


def write_cities(url: str, out: Path, **kw) -> int:
    fc = fetch_cities(url, **kw)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc, separators=(",", ":")))
    return len(fc["features"])
