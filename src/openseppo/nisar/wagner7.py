"""
openseppo.nisar.wagner7 -- Authalic Wagner VII equal-area projection
********************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Wagner VII applied to (longitude, authalic latitude) on the WGS84 authalic
sphere (R = 6,371,007.181 m). The result is equal-area with respect to the
WGS84 ellipsoid, on the same area reference as the EASE-Grid 2.0 polar grids
(EPSG:6931 / 6932). Selected with ``-t_srs wagner7``.

GDAL cannot warp to this projection: its warper maps every output pixel back
to the source, and PROJ's ``wag7`` has no inverse. This module provides:

  * forward()  -- WGS84 lon/lat to Wagner VII metres (one PROJ pipeline)
  * inverse()  -- Wagner VII metres to WGS84 lon/lat
  * default_transform() -- drop-in for rasterio's calculate_default_transform
  * warp()     -- 2-D array warp: average / sum by forward binning,
                  nearest by inverse mapping

References:
  - Snyder, J. P. (1987). Map Projections -- A Working Manual. USGS
    Professional Paper 1395 (authalic latitude eqs. 3-11, 3-12, 3-16, 3-18;
    Lambert azimuthal equal-area eqs. 24-16, 24-17).
  - Brodzik, M. J. et al. (2012). EASE-Grid 2.0. ISPRS Int. J. Geo-Inf. 1, 32-45.
"""

import numpy as np
from pyproj import CRS, Transformer
from rasterio.transform import from_origin


# =========================================================
# CONSTANTS
# =========================================================

WGS84_A = 6378137.0                          # semi-major axis [m]
WGS84_F = 1.0 / 298.257223563                # flattening
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)         # first eccentricity squared
WGS84_E = np.sqrt(WGS84_E2)


def _q(lat_deg):
    """Snyder eq. 3-12."""
    s = np.sin(np.radians(lat_deg))
    return (1 - WGS84_E2) * (s / (1 - WGS84_E2 * s * s)
                             - np.log((1 - WGS84_E * s) / (1 + WGS84_E * s)) / (2 * WGS84_E))


QP = _q(90.0)
AUTHALIC_RADIUS = float(WGS84_A * np.sqrt(QP / 2.0))  # 6,371,007.181 m

NAME = "wagner7"  # -t_srs value

# CRS written to output files. The x/y values are Wagner VII on this sphere; see TAGS.
PROJ4 = f"+proj=wag7 +lon_0=0 +R={AUTHALIC_RADIUS:.3f} +units=m +no_defs"

# Geodetic -> authalic latitude via ellipsoidal CEA (k0 = 1, y = a q / 2) and the
# inverse spherical CEA on the authalic sphere with k = a / R, then wag7.
FORWARD_PIPELINE = (
    "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
    "+step +proj=cea +ellps=WGS84 "
    f"+step +inv +proj=cea +R={AUTHALIC_RADIUS:.6f} +k_0={WGS84_A / AUTHALIC_RADIUS:.15f} "
    f"+step +proj=wag7 +R={AUTHALIC_RADIUS:.6f}"
)

TAGS = {
    "WAGNER7_AUTHALIC": (
        "Wagner VII applied to (longitude, authalic latitude) on the WGS84 authalic sphere "
        f"R = {AUTHALIC_RADIUS:.3f} m; equal-area on WGS84. y is not based on geodetic "
        "latitude: reprojecting with the CRS alone is off by up to 0.13 deg. "
        f"Forward PROJ pipeline: {FORWARD_PIPELINE}"
    )
}

# Forward binning needs several source samples per output cell, or cells are
# left empty: the auto pre-downscale stops at 1/PREDOWNSCALE_DIVISOR of -tr.
PREDOWNSCALE_DIVISOR = 4

# Wagner VII constants as in PROJ's wag7: sin(65 deg) and the x / y scale factors
_SIN65, _CX, _CY = 0.90630778703664996, 2.66723, 1.24104

_BLOCK_PIXELS = 4_000_000  # source or output pixels transformed per block

_FORWARD = Transformer.from_pipeline(FORWARD_PIPELINE)
_CRS = CRS.from_user_input(PROJ4)


def is_wagner7(crs):
    """True if *crs* (the -t_srs name, a CRS string or a CRS object) is authalic Wagner VII."""
    if crs is None:
        return False
    if str(crs).strip().lower() == NAME:
        return True
    try:
        return CRS.from_user_input(crs) == _CRS
    except Exception:
        return False


# =========================================================
# COORDINATE TRANSFORMS
# =========================================================


def geodetic_lat(beta_deg):
    """Authalic -> geodetic latitude [deg]. Snyder eq. 3-18 start, Newton on eq. 3-16."""
    beta = np.radians(np.asarray(beta_deg, dtype=np.float64))
    e4, e6 = WGS84_E2 ** 2, WGS84_E2 ** 3
    phi = (beta
           + (WGS84_E2 / 3 + 31 * e4 / 180 + 517 * e6 / 5040) * np.sin(2 * beta)
           + (23 * e4 / 360 + 251 * e6 / 3780) * np.sin(4 * beta)
           + 761 * e6 / 45360 * np.sin(6 * beta))
    pole = np.abs(np.sin(beta)) >= 1 - 1e-15
    target = QP * np.sin(beta)
    for _ in range(3):
        s = np.sin(phi)
        w = 1 - WGS84_E2 * s * s
        with np.errstate(divide="ignore", invalid="ignore"):
            step = w * w / (2 * np.cos(phi)) * (
                target / (1 - WGS84_E2) - s / w
                + np.log((1 - WGS84_E * s) / (1 + WGS84_E * s)) / (2 * WGS84_E))
        phi = np.where(pole, phi, phi + step)
    return np.where(pole, np.copysign(90.0, beta), np.degrees(phi))


def forward(lon, lat):
    """WGS84 lon/lat [deg] -> Wagner VII x, y [m]."""
    x, y = _FORWARD.transform(np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64))
    return np.asarray(x), np.asarray(y)


def inverse(x, y):
    """Wagner VII x, y [m] -> WGS84 lon/lat [deg]. NaN outside the map outline.

    Wagner VII is the equatorial Lambert azimuthal equal-area projection of the
    point (gamma, mu) with sin(gamma) = sin(65 deg) sin(beta) and mu = lon / 3,
    scaled by CX in x and CY in y. Undo the scaling, invert LAEA, then recover
    authalic latitude beta and convert it to geodetic latitude.
    """
    u = np.asarray(x, dtype=np.float64) / (_CX * AUTHALIC_RADIUS)
    v = np.asarray(y, dtype=np.float64) / (_CY * AUTHALIC_RADIUS)
    rho = np.hypot(u, v)
    with np.errstate(divide="ignore", invalid="ignore"):
        c = 2 * np.arcsin(rho / 2)
        sin_gamma = np.where(rho > 0, v * np.sin(c) / rho, 0.0)
        mu = np.arctan2(u * np.sin(c), rho * np.cos(c))
    sin_beta = sin_gamma / _SIN65
    # 1e-9: rounding pushes |sin(beta)| just past 1 within 0.0002 deg of the poles
    outside = (rho > 2) | (np.abs(sin_beta) > 1 + 1e-9) | (np.abs(mu) > np.pi / 3 + 1e-12)
    lon = np.where(outside, np.nan, np.degrees(3 * mu))
    lat = np.where(outside, np.nan, geodetic_lat(np.degrees(np.arcsin(np.clip(sin_beta, -1, 1)))))
    return lon, lat


# =========================================================
# OUTPUT GRID
# =========================================================


def default_transform(src_crs, dst_crs, width, height, left, bottom, right, top):
    """Drop-in for rasterio.warp.calculate_default_transform with a Wagner VII *dst_crs*.

    Returns (transform, width, height) covering the source extent at a pixel size
    of the same area as a source pixel. Source extents that cross the antimeridian
    are rejected: their Wagner VII bounding box would span the whole map.
    """
    n = 21
    t = np.linspace(0.0, 1.0, n)
    xs = np.concatenate([left + t * (right - left), np.full(n, right),
                         right - t * (right - left), np.full(n, left)])
    ys = np.concatenate([np.full(n, top), top - t * (top - bottom),
                         np.full(n, bottom), bottom + t * (top - bottom)])
    lon, lat = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True).transform(xs, ys)
    if np.ptp(lon) > 180:
        raise ValueError("Source extent crosses the antimeridian; -t_srs wagner7 does not "
                         "support such scenes yet.")
    x, y = forward(lon, lat)
    res = np.sqrt((right - left) * (top - bottom) / (width * height))
    w = max(1, int(np.ceil((x.max() - x.min()) / res)))
    h = max(1, int(np.ceil((y.max() - y.min()) / res)))
    return from_origin(x.min(), y.max(), res, res), w, h


# =========================================================
# WARP
# =========================================================


def warp(data_2d, src_transform, src_crs, dst_transform, dst_width, dst_height, method="average"):
    """Warp one 2-D float array to a Wagner VII grid. NaN/+/-inf in, NaN nodata out.

    average / sum -- forward binning: every valid source pixel centre is projected
                     and added to the output cell it falls in. Each cell is the mean
                     (or sum) of its source pixels. For output cells several times
                     larger than source pixels.
    nearest       -- inverse mapping: every output pixel centre is taken back to the
                     source pixel it lands in.
    """
    if method in ("average", "sum"):
        return _warp_binned(data_2d, src_transform, src_crs, dst_transform,
                            dst_width, dst_height, method)
    if method == "nearest":
        return _warp_nearest(data_2d, src_transform, src_crs, dst_transform,
                             dst_width, dst_height)
    raise ValueError(f"-t_srs wagner7 supports resampling average, sum and nearest, "
                     f"not '{method}'.")


def _warp_binned(data_2d, src_transform, src_crs, dst_transform, dst_width, dst_height, method):
    to_lonlat = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
    to_dst_px = ~dst_transform
    n_dst = dst_width * dst_height
    total = np.zeros(n_dst, dtype=np.float64)
    count = np.zeros(n_dst, dtype=np.float64)

    h, w = data_2d.shape
    cols = np.arange(w) + 0.5
    rows_per_block = max(1, _BLOCK_PIXELS // w)
    for r0 in range(0, h, rows_per_block):
        r1 = min(r0 + rows_per_block, h)
        values = data_2d[r0:r1].ravel()
        valid = np.isfinite(values)
        if not valid.any():
            continue
        cc, rr = np.meshgrid(cols, np.arange(r0, r1) + 0.5)
        sx, sy = src_transform * (cc.ravel()[valid], rr.ravel()[valid])
        x, y = forward(*to_lonlat.transform(sx, sy))
        col, row = to_dst_px * (x, y)
        col = np.floor(col).astype(np.int64)
        row = np.floor(row).astype(np.int64)
        inside = (col >= 0) & (col < dst_width) & (row >= 0) & (row < dst_height)
        idx = row[inside] * dst_width + col[inside]
        total += np.bincount(idx, weights=values[valid][inside], minlength=n_dst)
        count += np.bincount(idx, minlength=n_dst)

    with np.errstate(divide="ignore", invalid="ignore"):
        out = total / count if method == "average" else np.where(count > 0, total, np.nan)
    return out.reshape(dst_height, dst_width).astype(np.float32)


def _warp_nearest(data_2d, src_transform, src_crs, dst_transform, dst_width, dst_height):
    to_src = Transformer.from_crs("EPSG:4326", src_crs, always_xy=True)
    to_src_px = ~src_transform
    h, w = data_2d.shape
    out = np.full(dst_height * dst_width, np.nan, dtype=np.float32)

    cols = np.arange(dst_width) + 0.5
    rows_per_block = max(1, _BLOCK_PIXELS // dst_width)
    for r0 in range(0, dst_height, rows_per_block):
        r1 = min(r0 + rows_per_block, dst_height)
        cc, rr = np.meshgrid(cols, np.arange(r0, r1) + 0.5)
        lon, lat = inverse(*(dst_transform * (cc.ravel(), rr.ravel())))
        on_map = np.flatnonzero(np.isfinite(lon))
        sc, sr = to_src_px * to_src.transform(lon[on_map], lat[on_map])
        sc, sr = np.floor(sc), np.floor(sr)
        hit = (sc >= 0) & (sc < w) & (sr >= 0) & (sr < h)
        sr, sc = sr[hit].astype(np.int64), sc[hit].astype(np.int64)
        out[r0 * dst_width + on_map[hit]] = data_2d[sr, sc]
    return out.reshape(dst_height, dst_width)
