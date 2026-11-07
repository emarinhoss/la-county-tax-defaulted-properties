import io
import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import lacounty_tax.cli as cli
from lacounty_tax.cli import main
from lacounty_tax.relief import (ReliefError, build_relief, decode_terrarium, fetch_tile, hillshade, load_relief,
                                 lonlat_to_pixel, pixel_to_lonlat, polygon_mask, shade_to_rgba, write_relief)
from lacounty_tax.site import build_site
from test_assessor import Resp

ROOT = Path(__file__).parents[1]


def encode_terrarium(elev: np.ndarray) -> bytes:
    v = elev + 32768.0
    r, g = np.floor(v / 256), np.floor(v % 256)
    b = np.floor((v - np.floor(v)) * 256)
    img = Image.fromarray(np.stack([r, g, b], -1).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class PngResp(Resp):
    def __init__(self, status=200, body=b""):
        super().__init__(status, text="")
        self.content = body


class FakeTerrain:
    """Serves terrain tiles whose elevation rises towards the south-east (so the shading has something to show)."""
    def __init__(self, handler=None):
        self.calls, self.headers, self.handler = [], {}, handler

    def get(self, url, timeout=None):
        self.calls.append(url)
        if self.handler:
            return self.handler(url, len(self.calls))
        rows, cols = np.mgrid[0:256, 0:256].astype(float)
        return PngResp(200, encode_terrarium((rows + cols) * 4 + 100))


def test_tile_math_roundtrip_and_los_angeles_tile():
    for lon, lat in [(-118.25, 34.05), (-117.64, 32.8), (-118.95, 34.83)]:
        px, py = lonlat_to_pixel(lon, lat, 10)
        assert pixel_to_lonlat(px, py, 10) == pytest.approx((lon, lat), abs=1e-9)
    px, py = lonlat_to_pixel(-118.25, 34.05, 10)
    assert (int(px // 256), int(py // 256)) == (175, 408)           # downtown LA at zoom 10


def test_terrarium_decoding():
    px = np.array([[[128, 0, 0], [128, 100, 128], [127, 255, 0]]], dtype=np.uint8)
    assert decode_terrarium(px)[0].tolist() == [0.0, 100.5, -1.0]


def test_hillshade_lights_northwest_slopes_and_darkens_southeast_ones():
    n = np.arange(20, dtype=float)
    rows, cols = np.meshgrid(n, n, indexing="ij")
    shade = lambda e: hillshade(e * 20, 100.0, z_factor=1)[10, 10]       # noqa: E731
    flat = math.sin(math.radians(45))
    assert shade(rows * 0) == pytest.approx(flat)
    nw, se = shade(rows + cols), shade(-(rows + cols))                    # rising to the SE faces NW, toward the light
    assert se < shade(cols - rows) < nw and nw > flat > se
    assert shade(cols) == pytest.approx(shade(rows)) and shade(cols) > flat        # faces W and faces N: equally lit by a NW sun
    assert shade(-cols) == pytest.approx(shade(-rows)) and shade(-cols) < flat     # faces E and faces S: equally dark


def test_shade_to_rgba_black_shadows_white_highlights_transparent_flat():
    flat = math.sin(math.radians(45))
    out = shade_to_rgba(np.array([[flat, flat - 0.3, flat + 0.2]]))
    assert out[0, 0, 3] == 0                                              # flat: fully transparent
    assert tuple(out[0, 1, :3]) == (0, 0, 0) and out[0, 1, 3] > 0         # shadow
    assert tuple(out[0, 2, :3]) == (255, 255, 255) and out[0, 2, 3] > 0   # highlight
    assert out[0, 1, 3] > out[0, 2, 3]                                    # shadows are stronger than highlights


def test_polygon_mask_matches_the_polygon():
    z = 10
    (x0, y0), (x1, y1) = lonlat_to_pixel(-118.3, 34.1, z), lonlat_to_pixel(-118.2, 34.0, z)
    ring = [[-118.3, 34.1], [-118.3, 34.0], [-118.2, 34.0], [-118.2, 34.1], [-118.3, 34.1]]
    size = (int(x1 - x0) + 40, int(y1 - y0) + 40)
    m = polygon_mask([ring], z, int(x0) - 20, int(y0) - 20, size)
    assert m[20 + 5, 20 + 5] and not m[2, 2] and not m[-2, -2]
    assert abs(m.sum() - (x1 - x0) * (y1 - y0)) / ((x1 - x0) * (y1 - y0)) < 0.03


def test_fetch_tile_retries_and_errors():
    good = PngResp(200, encode_terrarium(np.zeros((256, 256))))
    seq = [PngResp(503), PngResp(429), good]
    s = FakeTerrain(lambda u, n: seq[n - 1])
    assert fetch_tile(s, 10, 1, 2, sleep=lambda t: None).shape == (256, 256, 3) and len(s.calls) == 3
    with pytest.raises(ReliefError, match="HTTP 404"):
        fetch_tile(FakeTerrain(lambda u, n: PngResp(404)), 10, 1, 2, sleep=lambda t: None)
    with pytest.raises(ReliefError, match="not a PNG"):
        fetch_tile(FakeTerrain(lambda u, n: PngResp(200, b"<html>blocked</html>")), 10, 1, 2, sleep=lambda t: None)
    with pytest.raises(ReliefError, match="gave up"):
        fetch_tile(FakeTerrain(lambda u, n: PngResp(500)), 10, 1, 2, retries=1, sleep=lambda t: None)


COUNTY = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {
    "type": "Polygon", "coordinates": [[[-118.30, 34.00], [-118.30, 34.10], [-118.20, 34.10], [-118.20, 34.00], [-118.30, 34.00]]]}}]}


def test_build_relief_crops_clips_and_reports_bounds():
    prog = []
    img, meta = build_relief(COUNTY, zoom=9, session=FakeTerrain(), progress=lambda d, t: prog.append((d, t)))
    assert img.mode == "RGBA" and img.size == (meta["width"], meta["height"]) and prog[-1][0] == prog[-1][1]
    (s, w), (n, e) = meta["bounds"]
    assert s < 34.0 - 0.005 and n > 34.1 + 0.005 and w < -118.3 and e > -118.2          # padded around the county
    a = np.asarray(img)[..., 3]
    assert a[0, 0] == 0 and a[-1, -1] == 0                                              # transparent outside the polygon
    assert a[a.shape[0] // 2, a.shape[1] // 2] > 0                                      # shading inside it
    assert meta["elevation_m"]["min"] >= 0 and "Mapzen" in meta["attribution"]


def test_negative_elevations_are_clamped_to_sea_level():
    def deep(url, n):
        return PngResp(200, encode_terrarium(np.full((256, 256), -1249.0)))
    _, meta = build_relief(COUNTY, zoom=9, session=FakeTerrain(deep))
    assert meta["elevation_m"] == {"min": 0.0, "max": 0.0}


def test_write_load_and_site_embed(tmp_path):
    cfile = tmp_path / "county.geojson"
    cfile.write_text(json.dumps(COUNTY))
    out = tmp_path / "relief"
    write_relief(cfile, out, zoom=9, session=FakeTerrain())
    assert (out / "la_relief.png").exists() and json.loads((out / "la_relief.json").read_text())["file"] == "la_relief.png"
    r = load_relief(out)
    assert r["url"] == "relief/la_relief.png" and len(r["bounds"]) == 2 and r["attribution"]
    assert load_relief(None) is None and load_relief(tmp_path / "nope") is None
    # the built page embeds it and ships the image next to index.html
    from lacounty_tax.reference import read_fmr_csv
    from lacounty_tax.synthetic import make_demo
    demo = make_demo(tmp_path / "demo", read_fmr_csv(ROOT / "data/reference/hud_safmr.csv"))
    page = build_site([demo.parent], tmp_path / "site", None, out)
    data = json.loads(page.read_text().split('id="data">', 1)[1].split("</script>", 1)[0])
    assert data["relief"]["url"] == "relief/la_relief.png" and (tmp_path / "site" / "relief" / "la_relief.png").exists()
    page2 = build_site([demo.parent], tmp_path / "site2", None, None)                    # no relief: page still builds
    assert json.loads(page2.read_text().split('id="data">', 1)[1].split("</script>", 1)[0])["relief"] is None
    assert not (tmp_path / "site2" / "relief").exists()


def test_committed_relief_matches_its_metadata_and_covers_the_county():
    d = ROOT / "data" / "reference" / "relief"
    meta = json.loads((d / "la_relief.json").read_text())
    img = Image.open(d / "la_relief.png")
    assert img.mode == "RGBA" and img.size == (meta["width"], meta["height"])
    (s, w), (n, e) = meta["bounds"]
    assert s < 32.8 and n > 34.8 and w < -118.9 and e > -117.65                          # the whole county
    a = np.asarray(img)[..., 3]
    assert 0.05 < (a > 0).mean() < 0.6                                                   # shaded land only, not the whole frame
    assert 2800 < meta["elevation_m"]["max"] < 3200 and meta["elevation_m"]["min"] == 0   # Mt. San Antonio ~3,068 m


def test_cli_build_relief(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "write_relief", lambda *a, **k: {"width": 10, "height": 20, "zoom": 10, "cell_m": 127.0,
                                                              "elevation_m": {"min": 0.0, "max": 3050.0}})
    assert main(["build-relief", "--out-dir", str(tmp_path / "r")]) == 0
    assert "10x20 px" in capsys.readouterr().out
    def boom(*a, **k):
        raise ReliefError("terrain tile 10/1/2: HTTP 403")
    monkeypatch.setattr(cli, "write_relief", boom)
    assert main(["build-relief", "--out-dir", str(tmp_path / "r")]) == 1
    assert "HTTP 403" in capsys.readouterr().err
