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
    assert not wagner7.is_wagner7("ESRI:54076")
    assert not wagner7.is_wagner7("EPSG:4326")
    assert not wagner7.is_wagner7(None)


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
