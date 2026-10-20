"""
Tests for openseppo.nisar.wagner7 -- authalic Wagner VII projection and warp.

No network or NISAR data needed: projections are checked against EPSG:6933 and
geodesic areas, warps against synthetic UTM rasters with one bright pixel.

    pytest tests/test_wagner7.py
"""

import numpy as np
import pytest
from pyproj import Geod, Transformer
from rasterio.transform import from_origin

import openseppo.nisar.wagner7 as wagner7

M_PER_DEG = np.pi * wagner7.AUTHALIC_RADIUS / 180


def _authalic_lat(lat_deg):
    """Geodetic -> authalic latitude (Snyder eq. 3-11), written out independently."""
    return np.degrees(np.arcsin(wagner7._q(lat_deg) / wagner7.QP))


def test_authalic_sphere_reproduces_ease_grid_2():
    """Authalic latitude + radius + spherical CEA must equal EPSG:6933 (ellipsoidal CEA)."""
    rng = np.random.default_rng(0)
    a, e2, r = wagner7.WGS84_A, wagner7.WGS84_E2, wagner7.AUTHALIC_RADIUS
    k0 = np.cos(np.radians(30)) / np.sqrt(1 - e2 * np.sin(np.radians(30)) ** 2)
    sphere_cea = Transformer.from_crs(
        f"+proj=longlat +R={r:.6f}", f"+proj=cea +k_0={a * k0 / r:.15f} +R={r:.6f} +units=m",
        always_xy=True)
    ease2 = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)
    lon, lat = rng.uniform(-180, 180, 100_000), rng.uniform(-85, 85, 100_000)
    x1, y1 = ease2.transform(lon, lat)
    x2, y2 = sphere_cea.transform(lon, _authalic_lat(lat))
    assert np.abs(x1 - x2).max() < 1e-3 and np.abs(y1 - y2).max() < 1e-3


def test_forward_pipeline_matches_two_step_form():
    rng = np.random.default_rng(0)
    two_step = Transformer.from_crs(f"+proj=longlat +R={wagner7.AUTHALIC_RADIUS:.6f}",
                                    wagner7.PROJ4, always_xy=True)
    lon, lat = rng.uniform(-180, 180, 100_000), rng.uniform(-90, 90, 100_000)
    x1, y1 = wagner7.forward(lon, lat)
    x2, y2 = two_step.transform(lon, _authalic_lat(lat))
    assert np.abs(x1 - x2).max() < 1e-3 and np.abs(y1 - y2).max() < 1e-3


def test_inverse_round_trip():
    rng = np.random.default_rng(0)
    lon, lat = rng.uniform(-180, 180, 200_000), rng.uniform(-90, 90, 200_000)
    x, y = wagner7.forward(lon, lat)
    lon2, lat2 = wagner7.inverse(x, y)
    x2, y2 = wagner7.forward(lon2, lat2)
    assert np.abs(x2 - x).max() < 1e-3 and np.abs(y2 - y).max() < 1e-3
    # ground error; near the poles latitude barely moves y (each pole is a line)
    m = np.abs(lat) < 80
    assert np.abs(lat2 - lat)[m].max() * M_PER_DEG < 1e-3
    assert (np.abs(lon2 - lon) * np.cos(np.radians(lat)))[m].max() * M_PER_DEG < 1e-3


def test_inverse_outside_outline_is_nan():
    x_edge, _ = wagner7.forward(180.0, 0.0)
    _, y_top = wagner7.forward(0.0, 90.0)
    lon, lat = wagner7.inverse([x_edge * 1.001, 0.0], [0.0, y_top * 1.001])
    assert np.isnan(lon).all() and np.isnan(lat).all()


def _band_ring(lat0, lat1, n=3601):
    """Closed lon/lat ring around the latitude band lat0..lat1."""
    lons, lats = np.linspace(-180, 180, n), np.linspace(lat0, lat1, n)
    lon = np.concatenate([lons, np.full(n, 180), lons[::-1], np.full(n, -180)])
    lat = np.concatenate([np.full(n, lat0), lats, np.full(n, lat1), lats[::-1]])
    return lon, lat


@pytest.mark.parametrize("lat0, lat1", [(0, 30), (30, 60), (60, 90), (-60, 60)])
def test_equal_area_on_wgs84(lat0, lat1):
    """Latitude-band areas equal geodesic WGS84 areas (PROJ's wag7 itself adds +1.6 ppm)."""
    geod = Geod(ellps="WGS84")
    true_area = 0.0
    for a in range(lat0, lat1, 10):  # 10 deg bands keep each geodesic polygon small
        lon, lat = _band_ring(a, min(a + 10, 89.99999))
        true_area += abs(geod.polygon_area_perimeter(lon, lat)[0])
    x, y = wagner7.forward(*_band_ring(lat0, lat1))
    w7_area = abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2
    assert abs(w7_area / true_area - 1) < 3e-6


def test_is_wagner7():
    assert wagner7.is_wagner7("wagner7") and wagner7.is_wagner7("Wagner7")
    assert wagner7.is_wagner7(wagner7.PROJ4)
    assert wagner7.is_wagner7(wagner7.WKT)
    assert not wagner7.is_wagner7("ESRI:54076")
    assert not wagner7.is_wagner7("+proj=wag7 +lon_0=10 +R=6371007.181")
    assert not wagner7.is_wagner7("EPSG:4326")
    assert not wagner7.is_wagner7(None)


def test_wkt_names_survive_geotiff(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    path = tmp_path / "w7.tif"
    with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=1,
                       dtype="float32", crs=wagner7.WKT,
                       transform=from_origin(0, 0, 1000, 1000)) as dst:
        dst.write(np.zeros((1, 2, 2), dtype=np.float32))
    with rasterio.open(path) as src:
        crs = src.crs
    assert "unknown" not in crs.to_wkt().lower()
    assert "Wagner_VII" in crs.to_wkt()
    assert wagner7.is_wagner7(crs)  # ESRI-renamed on read-back, matched by content


def _point_scene(lon0, lat0, epsg, res=10.0, size=1000):
    """UTM raster of ones with a single bright pixel at (lon0, lat0)."""
    ex, ny = Transformer.from_crs("EPSG:4326", epsg, always_xy=True).transform(lon0, lat0)
    half = size * res / 2
    tr = from_origin(np.floor(ex / res) * res - half, np.floor(ny / res) * res + half, res, res)
    data = np.ones((size, size), dtype=np.float32)
    col, row = ~tr * (ex, ny)
    data[int(row), int(col)] = 1000.0
    return data, tr


def _snapped_grid(src_crs, tr, shape, res):
    """Wagner VII grid of pixel size res covering the scene, aligned to multiples of res."""
    h, w = shape
    dt, dw, dh = wagner7.default_transform(src_crs, None, w, h,
                                           tr.c, tr.f + h * tr.e, tr.c + w * tr.a, tr.f)
    x0, y1 = np.floor(dt.c / res) * res, np.ceil(dt.f / res) * res
    x1, y0 = dt.c + dw * dt.a, dt.f + dh * dt.e
    return (from_origin(x0, y1, res, res), int(np.ceil((x1 - x0) / res)),
            int(np.ceil((y1 - y0) / res)))


@pytest.mark.parametrize("lon0, lat0, epsg", [(-77.0365, 38.8977, "EPSG:32618"),
                                               (-45.5, 72.6, "EPSG:32623"),
                                               (147.3, -42.9, "EPSG:32755")])
@pytest.mark.parametrize("method, res", [("average", 1000.0), ("nearest", 10.0)])
def test_warp_puts_point_in_its_cell(lon0, lat0, epsg, method, res):
    data, tr = _point_scene(lon0, lat0, epsg)
    dst_tr, dst_w, dst_h = _snapped_grid(epsg, tr, data.shape, res)
    out = wagner7.warp(data, tr, epsg, dst_tr, dst_w, dst_h, method=method)
    row, col = np.unravel_index(np.nanargmax(out), out.shape)
    xt, yt = wagner7.forward(lon0, lat0)
    tc, trow = ~dst_tr * (float(xt), float(yt))
    assert (int(np.floor(tc)), int(np.floor(trow))) == (col, row)


def test_warp_average_of_constant_is_constant():
    data, tr = _point_scene(10.0, 45.0, "EPSG:32632")
    data[:] = 3.0
    dst_tr, dst_w, dst_h = _snapped_grid("EPSG:32632", tr, data.shape, 500.0)
    out = wagner7.warp(data, tr, "EPSG:32632", dst_tr, dst_w, dst_h, method="average")
    assert np.nanmax(np.abs(out - 3.0)) < 1e-6
    assert np.isfinite(out).sum() > 0.5 * 10_000 * 10_000 / 500 ** 2


def test_warp_sum_counts_pixels():
    data, tr = _point_scene(10.0, 45.0, "EPSG:32632")
    data[:] = 1.0
    dst_tr, dst_w, dst_h = _snapped_grid("EPSG:32632", tr, data.shape, 1000.0)
    out = wagner7.warp(data, tr, "EPSG:32632", dst_tr, dst_w, dst_h, method="sum")
    assert np.nansum(out) == data.size


def test_default_transform_rejects_antimeridian():
    _, tr = _point_scene(179.95, 65.0, "EPSG:32660")  # 10 km scene across 180 deg
    with pytest.raises(ValueError, match="antimeridian"):
        wagner7.default_transform("EPSG:32660", None, 1000, 1000,
                                  tr.c, tr.f - 10_000, tr.c + 10_000, tr.f)


@pytest.mark.parametrize("lat0, divisor_min", [(60.0, 4.0), (80.0, 5.0), (85.0, 10.0)])
def test_predownscale_divisor_grows_toward_pole(lat0, divisor_min):
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:3413", always_xy=True).transform(-45.0, lat0)
    d = wagner7.predownscale_divisor("EPSG:3413", x - 20_000, y - 20_000, x + 20_000, y + 20_000)
    assert divisor_min <= d < 2 * divisor_min


def _empty_inner_cells(lat0, src_res, res=1000.0, half=20_000.0, margin=3_000.0):
    """Average-warp a constant polar stereographic scene at lat0; count empty cells
    whose centre lies at least *margin* inside the scene."""
    to_ps = Transformer.from_crs("EPSG:4326", "EPSG:3413", always_xy=True)
    cx, cy = to_ps.transform(-45.0, lat0)
    n = int(2 * half / src_res)
    tr = from_origin(cx - half, cy + half, src_res, src_res)
    data = np.ones((n, n), dtype=np.float32)
    dst_tr, dst_w, dst_h = _snapped_grid("EPSG:3413", tr, data.shape, res)
    out = wagner7.warp(data, tr, "EPSG:3413", dst_tr, dst_w, dst_h, method="average")
    cc, rr = np.meshgrid(np.arange(dst_w) + 0.5, np.arange(dst_h) + 0.5)
    px, py = to_ps.transform(*wagner7.inverse(*(dst_tr * (cc.ravel(), rr.ravel()))))
    inner = ((np.abs(px - cx) < half - margin) & (np.abs(py - cy) < half - margin)).reshape(out.shape)
    return int((inner & ~np.isfinite(out)).sum()), int(inner.sum())


def test_predownscale_divisor_fills_polar_cells():
    to_ps = Transformer.from_crs("EPSG:4326", "EPSG:3413", always_xy=True)
    x, y = to_ps.transform(-45.0, 85.0)
    d = wagner7.predownscale_divisor("EPSG:3413", x - 20_000, y - 20_000, x + 20_000, y + 20_000)
    empty_fixed, inner = _empty_inner_cells(85.0, 1000.0 / wagner7.PREDOWNSCALE_DIVISOR)
    empty_scaled, _ = _empty_inner_cells(85.0, 1000.0 / d)
    assert inner > 100
    assert empty_fixed > 0     # a quarter of -tr leaves holes at 85 deg
    assert empty_scaled == 0


def test_vector_cli_matches_forward(tmp_path):
    pytest.importorskip("osgeo")
    import json
    from openseppo.cli import wagner7_vector
    src = tmp_path / "pts.geojson"
    pts = [(10.0, 45.0), (-170.0, -72.5), (147.3, -42.9)]
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Point", "coordinates": list(p)}} for p in pts]}))
    dst = tmp_path / "pts_w7.geojson"
    wagner7_vector.project_vector(str(src), str(dst))
    feats = json.loads(dst.read_text())["features"]
    assert {f["geometry"]["type"] for f in feats} == {"MultiPoint"}  # -wrapdateline promotes
    got = np.array([f["geometry"]["coordinates"] for f in feats]).reshape(-1, 2)
    x, y = wagner7.forward(*zip(*pts))
    assert np.allclose(got[:, 0], x, atol=1e-3) and np.allclose(got[:, 1], y, atol=1e-3)


def test_vector_cli_graticule(tmp_path):
    pytest.importorskip("osgeo")
    import json
    from openseppo.cli import wagner7_vector
    dst = tmp_path / "grat.geojson"
    wagner7_vector.graticule(str(dst), step=30)
    feats = json.loads(dst.read_text())["features"]
    kinds = [f["properties"]["kind"] for f in feats]
    assert kinds.count("meridian") == 13 and kinds.count("pole_line") == 2
    assert kinds.count("parallel") == 5
    x_edge, _ = wagner7.forward(180.0, 0.0)
    xs = np.concatenate([np.array(f["geometry"]["coordinates"])[:, 0] for f in feats])
    assert abs(np.abs(xs).max() - float(x_edge)) < 1.0


def test_vector_cli_splits_antimeridian(tmp_path):
    pytest.importorskip("osgeo")
    import json
    from openseppo.cli import wagner7_vector
    src = tmp_path / "dl.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [
            [[179, 60], [181, 60], [181, 61], [179, 61], [179, 60]]]}}]}))
    dst = tmp_path / "dl_w7.geojson"
    wagner7_vector.project_vector(str(src), str(dst))
    geom = json.loads(dst.read_text())["features"][0]["geometry"]
    assert geom["type"] == "MultiPolygon" and len(geom["coordinates"]) == 2
    x_edge, _ = wagner7.forward(180.0, 60.0)
    for part in geom["coordinates"]:  # one part at each map edge, none across the map
        xs = np.array(part[0])[:, 0]
        assert np.ptp(xs) < 0.05 * float(x_edge)


def test_doc_wkt_matches_cli():
    """The WKT block in doc/wagner7.md is what seppo_wagner7_vector --wkt prints."""
    import re
    from pathlib import Path
    from openseppo.cli import wagner7_vector
    doc = (Path(__file__).parents[1] / "doc" / "wagner7.md").read_text()
    block = re.search(r"```\n(PROJCRS.*?)\n```", doc, re.S).group(1)
    assert block == wagner7_vector.qgis_wkt()
    assert wagner7.is_wagner7(block)
