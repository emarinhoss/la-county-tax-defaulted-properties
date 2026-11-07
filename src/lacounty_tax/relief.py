"""Shaded-relief (hillshade) image of LA County for the map background.

Built once from public terrain tiles and committed (``data/reference/relief/``), so the dashboard
needs no map service at runtime:

    lacounty-tax build-relief          # -> data/reference/relief/la_relief.png + la_relief.json

Elevation comes from the open "terrarium" terrain tiles (Mapzen/Tilezen compilation of NASA SRTM,
USGS 3DEP/NED and other public sources) on AWS Open Data. The image is a transparent overlay
(black shadows and white highlights, clipped to the county outline), so it works over any land
colour in light and dark themes.
"""
from __future__ import annotations

import io
import json
import math
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
ATTRIBUTION = ("Elevation: Mapzen terrain tiles (NASA SRTM, USGS 3DEP and other public sources) "
               "via AWS Open Data")
TILE = 256
USER_AGENT = "lacounty-tax-research/0.1 (personal research tool)"


class ReliefError(RuntimeError):
    pass


# ---------- Web Mercator tile math ----------
def lonlat_to_pixel(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = TILE * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def pixel_to_lonlat(px: float, py: float, z: int) -> tuple[float, float]:
    n = TILE * 2 ** z
    lon = px / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / n))))
    return lon, lat


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    """Terrarium PNG -> elevation in metres: (R*256 + G + B/256) - 32768."""
    a = rgb[..., :3].astype(np.float64)
    return a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0


# ---------- hillshade ----------
def hillshade(elev: np.ndarray, cell_m: float, *, azimuth: float = 315.0, altitude: float = 45.0,
              z_factor: float = 2.0) -> np.ndarray:
    """Lambertian shade in [0, 1] for a north-up grid; a flat surface gives sin(altitude)."""
    gy, gx = np.gradient(elev.astype(np.float64), cell_m)          # rows run south, columns run east
    gx, gy = gx * z_factor, gy * z_factor
    slope = math.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)                                   # compass bearing the slope faces (downhill), from north
    az, alt = math.radians(azimuth), math.radians(altitude)
    shade = math.sin(alt) * np.sin(slope) + math.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return np.clip(shade, 0.0, 1.0)


def shade_to_rgba(shade: np.ndarray, *, altitude: float = 45.0, shadow_alpha: float = 0.55,
                  highlight_alpha: float = 0.30) -> np.ndarray:
    """Black where darker than a flat surface, white where lighter; alpha grows with the difference."""
    delta = shade - math.sin(math.radians(altitude))
    out = np.zeros(shade.shape + (4,), dtype=np.uint8)
    dark = delta < 0
    out[..., :3] = np.where(dark[..., None], 0, 255)
    alpha = np.where(dark, np.minimum(1.0, -delta * 2.2) * shadow_alpha, np.minimum(1.0, delta * 3.0) * highlight_alpha)
    out[..., 3] = np.round(alpha * 255).astype(np.uint8)
    return out


# ---------- county mask ----------
def county_rings(geojson: dict) -> list[list[list[float]]]:
    """Outer rings (lon, lat) of every polygon in a FeatureCollection's geometries."""
    rings = []
    for f in geojson["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        rings += [p[0] for p in polys]
    return rings


def bbox_of(rings, pad: float = 0.01) -> tuple[float, float, float, float]:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad      # west, south, east, north


def polygon_mask(rings, z: int, x0: int, y0: int, size: tuple[int, int]) -> np.ndarray:
    img = Image.new("L", size, 0)
    d = ImageDraw.Draw(img)
    for r in rings:
        d.polygon([(lonlat_to_pixel(lon, lat, z)[0] - x0, lonlat_to_pixel(lon, lat, z)[1] - y0) for lon, lat in r], fill=255)
    return np.asarray(img) > 0


# ---------- download and assemble ----------
def fetch_tile(session, z: int, x: int, y: int, *, retries: int = 3, sleep=None) -> np.ndarray:
    import time
    sleep = sleep or time.sleep
    url = TILE_URL.format(z=z, x=x, y=y)
    last = "no attempt made"
    for attempt in range(retries + 1):
        if attempt:
            sleep(2 ** attempt)
        try:
            r = session.get(url, timeout=60)
        except (requests.Timeout, requests.ConnectionError) as e:
            last = f"{type(e).__name__}: {e}"
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}"
            continue
        if r.status_code != 200:
            raise ReliefError(f"terrain tile {z}/{x}/{y}: HTTP {r.status_code} from {url}")
        try:
            return np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
        except Exception as e:                                         # not an image (e.g. an HTML error page)
            raise ReliefError(f"terrain tile {z}/{x}/{y}: response is not a PNG ({e})") from e
    raise ReliefError(f"terrain tile {z}/{x}/{y}: gave up after {retries + 1} attempts ({last})")


def build_relief(county_geojson: dict, *, zoom: int = 10, session=None, z_factor: float = 2.0,
                 progress=None) -> tuple[Image.Image, dict]:
    """Return (RGBA image, metadata) for the county; the image is cropped to the county's bounding box."""
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)
    rings = county_rings(county_geojson)
    west, south, east, north = bbox_of(rings)
    px0, py0 = lonlat_to_pixel(west, north, zoom)
    px1, py1 = lonlat_to_pixel(east, south, zoom)
    x0, y0, x1, y1 = int(math.floor(px0)), int(math.floor(py0)), int(math.ceil(px1)), int(math.ceil(py1))
    tx0, ty0, tx1, ty1 = x0 // TILE, y0 // TILE, (x1 - 1) // TILE, (y1 - 1) // TILE
    cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
    grid = np.zeros((rows * TILE, cols * TILE))
    total, done = cols * rows, 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            grid[(ty - ty0) * TILE:(ty - ty0 + 1) * TILE, (tx - tx0) * TILE:(tx - tx0 + 1) * TILE] = \
                decode_terrarium(fetch_tile(session, zoom, tx, ty))
            done += 1
            if progress:
                progress(done, total)
    ox, oy = x0 - tx0 * TILE, y0 - ty0 * TILE
    w, h = x1 - x0, y1 - y0
    elev = np.maximum(grid[oy:oy + h, ox:ox + w], 0.0)        # sea level floor: harbour/bathymetry spikes are not land
    lat_mid = (south + north) / 2
    cell_m = 156543.03392 * math.cos(math.radians(lat_mid)) / 2 ** zoom
    rgba = shade_to_rgba(hillshade(elev, cell_m, z_factor=z_factor))
    mask = polygon_mask(rings, zoom, x0, y0, (w, h))
    rgba[..., 3] = np.where(mask, rgba[..., 3], 0)
    (wlon, nlat), (elon, slat) = pixel_to_lonlat(x0, y0, zoom), pixel_to_lonlat(x1, y1, zoom)
    meta = {
        "bounds": [[round(slat, 6), round(wlon, 6)], [round(nlat, 6), round(elon, 6)]],   # [[south, west], [north, east]]
        "zoom": zoom, "width": w, "height": h, "cell_m": round(cell_m, 1),
        "elevation_m": {"min": float(elev[mask].min()), "max": float(elev[mask].max())} if mask.any() else None,
        "source": TILE_URL, "attribution": ATTRIBUTION,
    }
    return Image.fromarray(rgba, "RGBA"), meta


def write_relief(county_path: Path, out_dir: Path, **kw) -> dict:
    county = json.loads(Path(county_path).read_text())
    img, meta = build_relief(county, **kw)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    img.save(out_dir / "la_relief.png", optimize=True)
    meta["file"] = "la_relief.png"
    (out_dir / "la_relief.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


def load_relief(relief_dir: Path | None) -> dict | None:
    """Metadata for the page, or None if no relief image has been built."""
    if not relief_dir:
        return None
    d = Path(relief_dir)
    if not (d / "la_relief.json").exists() or not (d / "la_relief.png").exists():
        return None
    meta = json.loads((d / "la_relief.json").read_text())
    return {"url": "relief/la_relief.png", "bounds": meta["bounds"], "attribution": meta.get("attribution", "")}
