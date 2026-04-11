"""
openseppo.nisar.radar_geometry -- SAR radar geometry routines
*************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Pure-Python/numpy implementations of core SAR geometry algorithms:

  * Hermite orbit interpolation (vectorised, window-cached)
  * ECEF <-> geodetic coordinate transforms
  * rdr2geo  -- radar (azimuth time, slant range) to geodetic (lon, lat, h)
  * geo2rdr  -- geodetic to radar coordinates

These are standard algorithms described in:
  - Cumming & Wong, "Digital Processing of Synthetic Aperture Radar Data"
  - Bamler & Hartl, "Synthetic aperture radar interferometry"

The implementation approach follows the isce3 framework (NASA/JPL,
Apache License 2.0), adapted as standalone pure-Python routines to
avoid a compiled-library dependency.
"""

import numpy as np


# =========================================================
# WGS-84 CONSTANTS
# =========================================================

WGS84_A = 6378137.0                          # semi-major axis [m]
WGS84_B = 6356752.314245179497                # semi-minor axis [m]
WGS84_E2 = 1.0 - (WGS84_B / WGS84_A) ** 2   # first eccentricity squared
WGS84_EP2 = (WGS84_A / WGS84_B) ** 2 - 1.0   # second eccentricity squared


# =========================================================
# COORDINATE TRANSFORMS
# =========================================================


def llh_to_xyz(lon_deg, lat_deg, h=0.0):
    """Geodetic (lon, lat, height) to ECEF (x, y, z).  All in metres."""
    lon = np.radians(lon_deg)
    lat = np.radians(lat_deg)
    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    N = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    return np.array([
        (N + h) * cos_lat * np.cos(lon),
        (N + h) * cos_lat * np.sin(lon),
        (N * (1.0 - WGS84_E2) + h) * sin_lat,
    ], dtype=np.float64)


def xyz_to_llh(xyz):
    """ECEF to geodetic.  Bowring iterative with early exit, sub-mm accuracy."""
    x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
    lon = np.degrees(np.arctan2(y, x))
    p = np.sqrt(x * x + y * y)
    theta = np.arctan2(z * WGS84_A, p * WGS84_B)
    lat = np.arctan2(
        z + WGS84_EP2 * WGS84_B * np.sin(theta) ** 3,
        p - WGS84_E2 * WGS84_A * np.cos(theta) ** 3,
    )
    for _ in range(5):
        sin_lat = np.sin(lat)
        N = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
        lat_new = np.arctan2(z + WGS84_E2 * N * sin_lat, p)
        if abs(lat_new - lat) < 1e-14:
            lat = lat_new
            break
        lat = lat_new
    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    N = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    h = p / cos_lat - N if abs(cos_lat) > 1e-10 else abs(z) - WGS84_B
    return lon, np.degrees(lat), h


def _reproject_to_ellipsoid(xyz, target_height=0.0):
    """Project ECEF point onto ellipsoid at *target_height*."""
    lon, lat, _ = xyz_to_llh(xyz)
    return llh_to_xyz(lon, lat, target_height)


# =========================================================
# HERMITE ORBIT INTERPOLATION  (vectorised, window-cached)
# =========================================================


class OrbitInterpolator:
    """
    Cached Hermite orbit interpolator.

    Pre-computes the denominator matrix per window and caches
    interpolated results at repeated azimuth times.

    The cache is bounded to the lifetime of the OrbitInterpolator
    instance, which is typically one subsetting operation (dozens
    of calls, not millions).

    Parameters
    ----------
    times : ndarray (N,)
        State-vector times (seconds since reference epoch).
    positions : ndarray (N, 3)
        ECEF positions [m].
    velocities : ndarray (N, 3)
        ECEF velocities [m/s].
    order : int
        Number of state vectors per interpolation (default 8).
    """

    def __init__(self, times, positions, velocities, order=8):
        self.times = np.asarray(times, dtype=np.float64)
        self.positions = np.asarray(positions, dtype=np.float64)
        self.velocities = np.asarray(velocities, dtype=np.float64)
        self.order = min(order, len(times))
        self._cache = {}          # t_key -> (pos, vel)
        self._window_cache = {}   # i0    -> precomputed arrays

    def __call__(self, t):
        """Interpolate position and velocity at time *t*."""
        t_key = round(t * 1e9)   # quantise to 1 ns for cache hits
        cached = self._cache.get(t_key)
        if cached is not None:
            return cached
        pos, vel = self._interpolate(t)
        self._cache[t_key] = (pos, vel)
        return pos, vel

    def clear_cache(self):
        self._cache.clear()
        self._window_cache.clear()

    def _get_window(self, i0):
        """Return precomputed per-window arrays, cached by start index."""
        cached = self._window_cache.get(i0)
        if cached is not None:
            return cached

        m = self.order
        tw = self.times[i0: i0 + m]
        pw = self.positions[i0: i0 + m]
        vw = self.velocities[i0: i0 + m]

        # denom[i,j] = tw[i] - tw[j], diagonal = 1
        denom = tw[:, None] - tw[None, :]
        np.fill_diagonal(denom, 1.0)
        denom_inv = 1.0 / denom
        np.fill_diagonal(denom_inv, 0.0)

        # L'_i at node tw[i] = sum_{j!=i} 1/(tw[i]-tw[j])
        Lp_node = np.sum(denom_inv, axis=1)

        # Off-diagonal mask (precomputed once per window)
        mask = 1.0 - np.eye(m)

        result = (denom, denom_inv, Lp_node, mask, tw, pw, vw)
        self._window_cache[i0] = result
        return result

    def _interpolate(self, t):
        n = len(self.times)
        m = self.order

        idx = np.searchsorted(self.times, t)
        i0 = max(0, min(idx - m // 2, n - m))

        denom, denom_inv, Lp_node, mask, tw, pw, vw = self._get_window(i0)

        dt = t - tw                                       # (m,)

        # Lagrange basis L_i(t)
        num = np.broadcast_to(dt, (m, m)).copy()
        np.fill_diagonal(num, 1.0)
        ratio = num * denom_inv
        np.fill_diagonal(ratio, 1.0)
        L = np.prod(ratio, axis=1)                        # (m,)

        # L'_i at query time t
        eps = 1e-15
        abs_dt = np.abs(dt)
        safe = abs_dt > eps

        if np.all(safe):
            inv_dt = 1.0 / dt
            dL = L * (inv_dt[None, :] * mask).sum(axis=1)
        else:
            # Rare: t near a node
            inv_dt_safe = np.where(safe, 1.0 / np.where(safe, dt, 1.0), 0.0)
            dL = np.zeros(m, dtype=np.float64)
            for i in range(m):
                if not safe[i]:
                    dL[i] = Lp_node[i]
                else:
                    dL[i] = L[i] * np.dot(inv_dt_safe, mask[i])

        L2 = L * L

        # Position
        h0 = (1.0 - 2.0 * dt * Lp_node) * L2
        h1 = dt * L2
        pos = h0 @ pw + h1 @ vw

        # Velocity (derivative of Hermite polynomial)
        two_L_dL = 2.0 * L * dL
        dh0 = -2.0 * Lp_node * L2 + (1.0 - 2.0 * dt * Lp_node) * two_L_dL
        dh1 = L2 + dt * two_L_dL
        vel = dh0 @ pw + dh1 @ vw

        return pos, vel


# =========================================================
# rdr2geo  --  radar coordinates to geodetic
# =========================================================


def rdr2geo(az_time, slant_range, orbit,
            look_side=1, target_height=0.0,
            threshold=1e-5, max_iter=50):
    """
    Radar coordinates to geodetic on WGS-84.

    Newton-Raphson iteration.  Converges on both range and Doppler
    residuals.  Uses a cross-track-offset initial guess for fast
    convergence (typically 5-10 iterations).

    Parameters
    ----------
    az_time : float
        Zero-Doppler azimuth time (seconds since orbit reference epoch).
    slant_range : float
        One-way slant range [m].
    orbit : OrbitInterpolator
    look_side : int
        +1 right-looking, -1 left-looking.
    target_height : float
        Height above the ellipsoid [m].
    threshold : float
        Convergence threshold for range residual [m].
    max_iter : int

    Returns
    -------
    lon, lat, height : float   (degrees, degrees, metres)
    """
    sat_pos, sat_vel = orbit(az_time)
    vel_mag = np.linalg.norm(sat_vel)
    vel_hat = sat_vel / vel_mag

    # --- Initial guess with cross-track offset ---
    target = _reproject_to_ellipsoid(sat_pos, target_height)
    sat_r = np.linalg.norm(sat_pos)
    tgt_r = np.linalg.norm(target)
    cos_inc_est = np.clip(
        (sat_r * sat_r + tgt_r * tgt_r - slant_range * slant_range)
        / (2.0 * sat_r * tgt_r), -1.0, 1.0,
    )
    inc_est = np.arccos(cos_inc_est)

    radial = sat_pos / sat_r
    cross = np.cross(vel_hat, radial)
    cross_norm = np.linalg.norm(cross)
    cross_hat = cross / cross_norm if cross_norm > 1e-10 else np.array([0., 0., 1.])

    target = (sat_pos
              - tgt_r * np.cos(inc_est) * radial
              + tgt_r * np.sin(inc_est) * look_side * cross_hat)
    target = _reproject_to_ellipsoid(target, target_height)

    # --- Newton iteration ---
    for _ in range(max_iter):
        dr = target - sat_pos
        r = np.linalg.norm(dr)
        dr_hat = dr / r

        range_err = r - slant_range
        doppler = np.dot(dr, sat_vel)

        if abs(range_err) < threshold and abs(doppler) < threshold * vel_mag:
            break

        # Correct range
        target -= range_err * dr_hat

        # Correct Doppler
        dr = target - sat_pos
        r = np.linalg.norm(dr)
        dr_hat = dr / r
        doppler = np.dot(dr, sat_vel)
        v_along_r = np.dot(sat_vel, dr_hat)
        v_perp = sat_vel - v_along_r * dr_hat
        v_perp_sq = np.dot(v_perp, v_perp)
        if v_perp_sq > 1e-20:
            target -= (doppler / v_perp_sq) * v_perp

        # Re-project onto ellipsoid
        target = _reproject_to_ellipsoid(target, target_height)

    # Enforce look side
    cross_check = np.cross(sat_vel, sat_pos)
    if np.dot(cross_check, target - sat_pos) * look_side < 0:
        normal = cross_check / np.linalg.norm(cross_check)
        d = np.dot(target - sat_pos, normal)
        target = target - 2.0 * d * normal
        target = _reproject_to_ellipsoid(target, target_height)

    return xyz_to_llh(target)


def rdr2geo_corners(orbit, zd_times, slant_ranges,
                    look_side=1, target_height=0.0):
    """
    Compute geodetic corners of an RSLC subset.

    Returns [(lon,lat), ...] for [near-early, far-early, far-late, near-late],
    or None on failure.  Orbit cache is shared across same-time evaluations.
    """
    t_early = float(zd_times[0])
    t_late = float(zd_times[-1])
    sr_near = float(slant_ranges[0])
    sr_far = float(slant_ranges[-1])

    corners = []
    for t, sr in [(t_early, sr_near), (t_early, sr_far),
                  (t_late, sr_far),   (t_late, sr_near)]:
        try:
            lon, lat, _ = rdr2geo(t, sr, orbit,
                                  look_side=look_side,
                                  target_height=target_height)
            corners.append((lon, lat))
        except Exception:
            return None
    return corners


# =========================================================
# geo2rdr  --  geodetic coordinates to radar
# =========================================================


def geo2rdr(lon_deg, lat_deg, height, orbit,
            threshold=1e-8, max_iter=50):
    """
    Geodetic to radar coordinates under zero-Doppler.

    Newton-Raphson on azimuth time.  Initial guess from coarse
    range search on cached orbit samples.

    Parameters
    ----------
    lon_deg, lat_deg : float   (degrees)
    height : float             [m]
    orbit : OrbitInterpolator
    threshold : float          convergence on dt [s]
    max_iter : int

    Returns
    -------
    az_time, slant_range : float   (s, m)
    """
    target_xyz = llh_to_xyz(lon_deg, lat_deg, height)

    # Coarse search over 16 orbit samples
    n_samples = min(len(orbit.times), 16)
    t_samples = np.linspace(orbit.times[0], orbit.times[-1], n_samples)
    r_min = np.inf
    t = 0.5 * (orbit.times[0] + orbit.times[-1])
    for ts in t_samples:
        ps, _ = orbit(ts)
        dr = target_xyz - ps
        rs = np.dot(dr, dr)
        if rs < r_min:
            r_min = rs
            t = ts

    # Newton iteration (clamped to orbit time span)
    # f(t) = dot(target - pos(t), vel(t)), f'(t) ≈ -|vel|^2
    # Newton step: dt = f/f' = doppler / (-vel_sq), so t_new = t - dt = t + doppler/vel_sq
    t_lo, t_hi = float(orbit.times[0]), float(orbit.times[-1])
    for _ in range(max_iter):
        pos, vel = orbit(t)
        doppler = np.dot(target_xyz - pos, vel)
        dt = doppler / np.dot(vel, vel)
        t += dt
        t = max(t_lo, min(t, t_hi))
        if abs(dt) < threshold:
            break

    pos, _ = orbit(t)
    slant_range = np.linalg.norm(target_xyz - pos)
    return t, slant_range


def geo2rdr_batch(coords, orbit, height=0.0):
    """
    Batch geo2rdr for multiple (lon, lat) points.

    Shares orbit cache and coarse time search across all points.

    Parameters
    ----------
    coords : list of (lon, lat) tuples  (degrees)
    orbit : OrbitInterpolator
    height : float  [m]

    Returns
    -------
    list of (az_time, slant_range) tuples
    """
    targets = [llh_to_xyz(lon, lat, height) for lon, lat in coords]

    # Shared coarse time grid
    n_samples = min(len(orbit.times), 16)
    t_samples = np.linspace(orbit.times[0], orbit.times[-1], n_samples)
    pos_samples = [orbit(ts)[0] for ts in t_samples]

    results = []
    for target_xyz in targets:
        r_min = np.inf
        t = 0.5 * (orbit.times[0] + orbit.times[-1])
        for i, ts in enumerate(t_samples):
            dr = target_xyz - pos_samples[i]
            rs = np.dot(dr, dr)
            if rs < r_min:
                r_min = rs
                t = ts

        t_lo, t_hi = float(orbit.times[0]), float(orbit.times[-1])
        for _ in range(50):
            pos, vel = orbit(t)
            doppler = np.dot(target_xyz - pos, vel)
            dt = doppler / np.dot(vel, vel)
            t += dt
            t = max(t_lo, min(t, t_hi))
            if abs(dt) < 1e-8:
                break

        pos, _ = orbit(t)
        sr = np.linalg.norm(target_xyz - pos)
        results.append((t, sr))

    return results


def geo2rdr_bbox(lon_min, lat_min, lon_max, lat_max,
                 orbit, height=0.0):
    """
    Geographic bounding box to radar coordinate ranges.

    Returns (az_time_min, az_time_max, sr_min, sr_max).
    """
    lon_mid = (lon_min + lon_max) / 2
    lat_mid = (lat_min + lat_max) / 2
    sample_pts = [
        (lon_min, lat_max), (lon_max, lat_max),
        (lon_max, lat_min), (lon_min, lat_min),
        (lon_mid, lat_max), (lon_mid, lat_min),
        (lon_min, lat_mid), (lon_max, lat_mid),
    ]

    results = geo2rdr_batch(sample_pts, orbit, height=height)
    if not results:
        raise ValueError("geo2rdr failed for all bbox sample points.")

    az_times = [r[0] for r in results]
    slant_ranges = [r[1] for r in results]
    return min(az_times), max(az_times), min(slant_ranges), max(slant_ranges)


# =========================================================
# BOUNDING POLYGON HELPER
# =========================================================


def corners_to_wkt(corners):
    """Convert list of (lon, lat) tuples to WKT POLYGON string."""
    pts = " ".join(f"{lon:.8f} {lat:.8f}" for lon, lat in corners)
    pts += f" {corners[0][0]:.8f} {corners[0][1]:.8f}"
    return f"POLYGON (({pts}))"
