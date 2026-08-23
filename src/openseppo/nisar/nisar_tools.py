"""
openseppo.nisar.nisar_tools -- NISAR GCOV processing core
*********************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Core library for reading NISAR GCOV HDF5 files and converting them to
Cloud Optimized GeoTIFF (COG). Supports local and S3/HTTPS sources via
parallel streaming, with output modes: power (float32), amplitude (uint16),
dB (float32), and DN (uint8). Includes reprojection, spatial subsetting,
block downscaling, interior hole filling, VRT time-series stacking, and
dual-pol ratio computation.
"""

import sys
import os
import gc
import atexit
import tempfile
import shutil
import numpy as np
import h5py
import s3fs
import math
import re
import traceback
import warnings
from collections import defaultdict
import rasterio
from rasterio.warp import (transform, calculate_default_transform,
                            reproject, Resampling, transform_bounds)
from rasterio.crs import CRS
from rasterio.transform import from_origin
from rasterio.io import MemoryFile
import subprocess as sp
import shlex

# OPTIMIZATION: Add xarray with datatree for efficient HDF5 access
import xarray as xr

try:
    from datatree import open_datatree

    HAS_DATATREE = True
except ImportError:
    try:
        # Try newer xarray versions where datatree is integrated
        from xarray import open_datatree

        HAS_DATATREE = True
    except ImportError:
        HAS_DATATREE = False
        print("WARNING: datatree not available. Install with: pip install xarray-datatree or pip install 'xarray>=2024.2.0'")

# Optional: Earthdata Access
try:
    import earthaccess

    HAS_EARTHACCESS = True
except ImportError:
    HAS_EARTHACCESS = False


_EARTHACCESS_TOKEN_CACHE = os.path.expanduser("~/.cache/openseppo/earthaccess_token.json")

# How much oversampling to preserve for each resampling kernel when auto-computing
# a pre-downscale factor from -tr.  A divisor of 2 means the pre-downscale stops at
# half the target resolution, leaving a 2x "fine-tuning" step for the warp.
# nearest/average have no kernel-spread concern so they can go all the way.
_RESAMPLE_PREDOWNSCALE_DIVISOR = {
    "nearest":     1,
    "average":     1,
    "mode":        1,
    "bilinear":    2,
    "cubic":       2,
    "cubicspline": 2,
    "lanczos":     3,
}


def _earthaccess_login(verbose=False):
    """
    Login to NASA Earthdata, reusing a cached JWT token when still valid.

    The token (valid ~60 days) is stored in ~/.cache/openseppo/earthaccess_token.json.
    On a cache hit the URS OAuth handshake (~75 s) is bypassed by directly restoring
    Auth and Store state without calling earthaccess.login() at all.
    On a miss the full login runs and the fresh token is saved for next time.
    """
    import json
    import base64
    import datetime
    import threading

    def _jwt_expiry(access_token):
        """Decode JWT payload and return expiry as UTC datetime, or None."""
        try:
            parts = access_token.split(".")
            padding = "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            exp = payload.get("exp")
            return datetime.datetime.utcfromtimestamp(exp) if exp else None
        except Exception:
            return None

    def _restore_from_cache(access_token, exp):
        """
        Restore earthaccess Auth + Store state from a cached bearer token,
        bypassing Store.__init__ (which runs a ~75 s OAuth cookie handshake).

        earthaccess.open() for HTTPS uses Store.get_fsspec_session(), which only
        needs the bearer token -- no OAuth cookies required.
        """
        from earthaccess.store import Store

        auth = earthaccess._auth
        auth.token = {"access_token": access_token}
        auth.authenticated = True

        # Build a Store without calling __init__ so set_requests_session is never run.
        store = Store.__new__(Store)
        store.thread_locals = threading.local()
        store.auth = auth
        store._s3_credentials = {}
        store._requests_cookies = {}
        store.in_region = False
        # _http_session is created lazily by get_session() -- no network call.
        store._http_session = auth.get_session()
        earthaccess._store = store

        if verbose:
            print(
                f"    [t] earthaccess login (cached token, expires {exp.date()}): instant",
                flush=True,
            )
        return auth

    # --- Try cached token ---
    if os.path.exists(_EARTHACCESS_TOKEN_CACHE):
        try:
            with open(_EARTHACCESS_TOKEN_CACHE) as fh:
                cached = json.load(fh)
            access_token = cached.get("access_token", "")
            exp = _jwt_expiry(access_token)
            # Require at least 1 hour of remaining validity
            if exp and exp > datetime.datetime.utcnow() + datetime.timedelta(hours=1):
                return _restore_from_cache(access_token, exp)
        except Exception:
            pass  # Fall through to fresh login

    # --- Full login (first run or expired token) ---
    auth = earthaccess.login(strategy="netrc")

    # Cache the token for future runs
    if auth.authenticated and auth.token:
        try:
            os.makedirs(os.path.dirname(_EARTHACCESS_TOKEN_CACHE), exist_ok=True)
            with open(_EARTHACCESS_TOKEN_CACHE, "w") as fh:
                json.dump(auth.token, fh)
        except Exception:
            pass

    return auth


# =========================================================
# INTERNAL FILE HELPERS (replaces seppopy.tools.filehandling)
# =========================================================


def _unlink(path):
    """Remove a single file, ignoring errors."""
    try:
        os.unlink(path)
    except OSError:
        pass


# =========================================================
# 0. GRANULE NAME CONVENTION
# =========================================================

# NISAR granule names are underscore-delimited with the acquisition
# configuration in two fixed fields:
#   token 8 -- radar mode code, e.g. "4005", "0005"
#   token 9 -- polarization code, two characters per frequency ("<A><B>"),
#              with the literal "NA" where that frequency was not acquired:
#              "DHDH" = both frequencies, "NADV" = no frequency A,
#              "DHNA" = no frequency B.
_MODE_TOKEN = 8
_POL_TOKEN = 9


def granule_mode_pol(url):
    """The (mode, pol) acquisition codes carried in a NISAR granule name.

    Returns ``(None, None)`` for names that do not follow the convention --
    those are left to the reader rather than guessed at.
    """
    name = url.rstrip("/").split("/")[-1]
    if name.lower().endswith(".h5"):
        name = name[:-3]
    tokens = name.split("_")
    if len(tokens) <= _POL_TOKEN:
        return (None, None)
    return (tokens[_MODE_TOKEN], tokens[_POL_TOKEN])


def check_uniform_mode(urls):
    """Return a batch's shared (mode, pol) codes, refusing a mixed batch.

    A stack is only meaningful when every granule was acquired the same way:
    mode and polarization decide which frequencies exist and how the grids are
    posted, so mixing them in one ``-i`` list cannot yield a clean time series.

    Raises ValueError naming every mode found.  Granules whose names do not
    follow the convention are not counted either way.
    """
    seen = {}
    for url in urls:
        mode, pol = granule_mode_pol(url)
        if mode is None:
            continue
        seen.setdefault((mode, pol), []).append(url.split("/")[-1])
    if len(seen) > 1:
        lines = [f"  {mode}_{pol}  ({len(names)} file{'s' if len(names) > 1 else ''}), "
                 f"e.g. {names[0]}"
                 for (mode, pol), names in sorted(seen.items())]
        raise ValueError(
            "input list mixes acquisition modes:\n" + "\n".join(lines) +
            "\nStacks must be single-mode -- submit one mode per -i list.")
    return next(iter(seen), (None, None))


def frequency_from_pol_code(pol):
    """The frequency an unset ``-f`` means, read off the granule's pol code.

    Only decides when exactly one frequency was acquired; ``None`` means both
    are present and the caller's own default applies.
    """
    if not pol or len(pol) != 4:
        return None
    no_a, no_b = pol[:2] == "NA", pol[2:] == "NA"
    if no_a and not no_b:
        return "B"
    if no_b and not no_a:
        return "A"
    return None


# =========================================================
# 1. AUTHENTICATION & FILESYSTEM HELPER
# =========================================================


def create_s3_fs(auth_config=None):
    if auth_config is None:
        auth_config = {}

    if auth_config.get("use_earthdata"):
        if not HAS_EARTHACCESS:
            raise ImportError("Install 'earthaccess'.")
        # Login is always called before create_s3_fs from process_chunk_task.
        # Fall back to a fresh login if somehow called standalone.
        if earthaccess._store is None:
            _earthaccess_login()
        return earthaccess.get_s3_filesystem(endpoint="https://nisar.asf.earthdatacloud.nasa.gov/s3credentials")

    if "profile" in auth_config and auth_config["profile"]:
        return s3fs.S3FileSystem(profile=auth_config["profile"])

    if "key" in auth_config and "secret" in auth_config:
        return s3fs.S3FileSystem(key=auth_config["key"], secret=auth_config["secret"], token=auth_config.get("token"))

    if auth_config.get("anon"):
        return s3fs.S3FileSystem(anon=True)
    return s3fs.S3FileSystem(anon=False)


def check_s3_write_access(s3_path, auth_config=None):
    """
    Verify write permission on an S3 output location by initiating
    and immediately aborting a multipart upload via ``aws s3api``.
    No object is created or left behind.

    Requires ``s3:PutObject`` -- the same permission needed for writes.
    Silently skipped if the aws CLI is not installed.

    Raises PermissionError with a clear message on failure.
    """
    import json as _json

    parsed = s3_path.replace("s3://", "").split("/", 1)
    bucket = parsed[0]
    key = (parsed[1].rstrip("/") + "/.openseppo_write_check").lstrip("/")

    profile_args = []
    if auth_config and auth_config.get("profile"):
        profile_args = ["--profile", auth_config["profile"]]

    # Step 1: create multipart upload (tests s3:PutObject permission)
    cmd_create = ["aws", "s3api", "create-multipart-upload",
                   "--bucket", bucket, "--key", key,
                   "--output", "json"] + profile_args
    try:
        r = sp.run(cmd_create, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return  # aws CLI not installed -- skip
    except sp.TimeoutExpired:
        return  # network issue -- let actual write fail later

    if r.returncode != 0:
        err = r.stderr.strip()
        raise PermissionError(
            f"Cannot write to {s3_path}  --  check your AWS credentials, "
            f"profile, and bucket permissions.\n  {err}"
        )

    # Step 2: abort (cleanup, nothing left behind)
    try:
        upload_id = _json.loads(r.stdout)["UploadId"]
        sp.run(["aws", "s3api", "abort-multipart-upload",
                "--bucket", bucket, "--key", key,
                "--upload-id", upload_id] + profile_args,
               capture_output=True, timeout=15)
    except Exception:
        pass  # best-effort cleanup


# =========================================================
# 1b. REPROJECTION HELPERS
# =========================================================


def _ensure_utm_south(crs_str, h5_handle):
    """
    Add +south to a proj4 UTM string when the scene is in the southern hemisphere.

    NISAR files conforming to the SDS spec store projection as an integer EPSG
    code (e.g. 32755 for UTM 55S), which includes the correct false northing.
    Older or CalVal products may store a plain proj4 string such as
    "+proj=utm +zone=55 +datum=WGS84" that omits +south, causing a 10 000 000 m
    northing displacement.  We detect this by reading the scene bounding polygon
    (always in WGS84 lat/lon) from the NISAR identification group.
    """
    s = str(crs_str).strip()
    if "+proj=utm" not in s.lower():
        return s  # Not a UTM proj4 string -- nothing to fix
    if "+south" in s.lower():
        return s  # Already hemisphere-aware
    try:
        bp_path = "/science/LSAR/identification/boundingPolygon"
        if bp_path not in h5_handle:
            return s
        val = h5_handle[bp_path][()]
        if hasattr(val, "decode"):
            val = val.decode("utf-8")
        # WKT POLYGON: coordinate pairs are "lon lat" (space-separated)
        matches = re.findall(r"([-\d.]+)\s+([-\d.]+)", str(val))
        if matches:
            avg_lat = sum(float(lat) for _, lat in matches) / len(matches)
            if avg_lat < 0:
                s = s + " +south"
    except Exception:
        pass
    return s


def _parse_crs(crs_input):
    """Normalise EPSG string / int / WKT / CRS object -> rasterio CRS."""
    if crs_input is None:
        return None
    if isinstance(crs_input, CRS):
        return crs_input
    if isinstance(crs_input, int):
        return CRS.from_epsg(crs_input)
    s = str(crs_input).strip()
    if s.upper().startswith("EPSG:"):
        return CRS.from_epsg(int(s.split(":")[1]))
    # Bare numeric string -> treat as EPSG code (e.g. "32634" -> EPSG:32634)
    if s.isdigit():
        return CRS.from_epsg(int(s))
    try:
        return CRS.from_wkt(s)
    except Exception:
        return CRS.from_string(s)


def _get_resampling(method):
    """Return Resampling enum from string; defaults to cubic."""
    method = (method or "cubic").lower()
    mapping = {
        "nearest": Resampling.nearest,
        "bilinear": Resampling.bilinear,
        "cubic": Resampling.cubic,
        "cubicspline": Resampling.cubic_spline,
        "lanczos": Resampling.lanczos,
        "average": Resampling.average,
        "mode": Resampling.mode,
    }
    return mapping.get(method, Resampling.cubic)


def _fill_nodata_nn(data_2d):
    """
    Fill interior NaN / +/-inf pixels with the value of their nearest valid neighbour.

    Only pixels not connected to the image-frame edge are filled.  Edge-connected
    invalid regions (layover, shadow, swath gaps that reach the frame boundary)
    are left as-is so they are not projected into the destination as false data.
    """
    from scipy.ndimage import distance_transform_edt, binary_fill_holes

    valid = np.isfinite(data_2d)
    if valid.all():
        return data_2d
    if not valid.any():
        return data_2d  # nothing to fill from

    # Interior invalid pixels: invalid and not reachable from any array border
    interior_invalid = binary_fill_holes(valid) & ~valid
    if not interior_invalid.any():
        return data_2d

    row_idx, col_idx = distance_transform_edt(
        ~valid, return_distances=False, return_indices=True)
    result = data_2d.copy()
    result[interior_invalid] = data_2d[row_idx[interior_invalid], col_idx[interior_invalid]]
    return result


def _reproject_power_band(data_2d, src_transform, src_crs, dst_transform, dst_crs,
                           dst_width, dst_height, resampling, fill_holes=False,
                           num_threads=None):
    """
    Warp one 2-D float32 power array. NaN/+/-inf <-> NaN nodata. Returns warped array.

    Single-pass warp with src_nodata=NaN. GDAL 3.x+ correctly excludes NaN
    from cubic/lanczos kernels, preserving edge nodata without the memory-heavy
    scipy distance_transform + second mask warp that the old two-pass approach
    required.

    fill_holes -- if True, interior NaN/+/-inf pixels (those fully enclosed by
                 valid data, not connected to the image frame edge) are filled
                 with their nearest valid neighbour BEFORE warping so they
                 remain valid in the output. Uses scipy (higher memory); only
                 runs when the user explicitly requests --fill_holes.
                 Frame-boundary nodata is unaffected.
    """
    src = data_2d.astype(np.float32)
    src[~np.isfinite(src)] = np.nan

    if fill_holes:
        src = _fill_nodata_nn(src)

    n_threads = num_threads if num_threads is not None else os.cpu_count() or 1

    dst_data = np.full((dst_height, dst_width), np.nan, dtype=np.float32)

    reproject(
        source=src, destination=dst_data,
        src_transform=src_transform, src_crs=src_crs,
        dst_transform=dst_transform, dst_crs=dst_crs,
        src_nodata=np.nan, dst_nodata=np.nan,
        resampling=resampling,
        num_threads=n_threads,
    )

    del src
    gc.collect()

    # Clamp to physically meaningful power range (cubic/lanczos ringing).
    _PWR_FLOOR = 1e-4                        # -40 dB
    _PWR_CEIL = 10 ** (13.329 / 10.0)        # ~21.53
    finite = np.isfinite(dst_data)
    dst_data = np.where(finite, np.clip(dst_data, _PWR_FLOOR, _PWR_CEIL), dst_data)

    return dst_data


def calculate_source_window(target_bounds: tuple, target_crs: str, source_crs: str,
                             source_res: tuple, densify_step: float = 100.0,
                             buffer_pixels: int = 3) -> tuple:
    """
    Compute the bounding box in source_crs that fully covers target_bounds (in target_crs).

    Densifies the target polygon edges before reprojecting to handle curved edges
    (important for large areas or oblique projections). Adds a pixel buffer.

    Parameters
    ----------
    target_bounds : (ulx, lry, lrx, uly)  in target_crs
    target_crs    : CRS string for the target bounds  (e.g. 'EPSG:4326')
    source_crs    : CRS string of the source raster   (e.g. 'EPSG:32610')
    source_res    : (res_x, res_y) pixel size in source units (positive values)
    densify_step  : approx. linear segment length (map units of target_crs)
    buffer_pixels : extra pixels to add on each side

    Returns
    -------
    (final_minx, final_miny, final_maxx, final_maxy)  in source_crs
    """
    from pyproj import Transformer

    ulx_t, lry_t, lrx_t, uly_t = target_bounds

    # Build densified ring of points along target bbox edges
    def _linspace(a, b, step):
        n = max(2, int(abs(b - a) / step) + 1)
        return np.linspace(a, b, n)

    top_x = _linspace(ulx_t, lrx_t, densify_step)
    top_y = np.full_like(top_x, uly_t)
    right_x = np.full(max(2, int(abs(uly_t - lry_t) / densify_step) + 1), lrx_t)
    right_y = _linspace(uly_t, lry_t, densify_step)
    bot_x = _linspace(lrx_t, ulx_t, densify_step)
    bot_y = np.full_like(bot_x, lry_t)
    left_x = np.full(max(2, int(abs(lry_t - uly_t) / densify_step) + 1), ulx_t)
    left_y = _linspace(lry_t, uly_t, densify_step)

    xs = np.concatenate([top_x, right_x, bot_x, left_x])
    ys = np.concatenate([top_y, right_y, bot_y, left_y])

    transformer = Transformer.from_crs(target_crs, source_crs, always_xy=True)
    src_xs, src_ys = transformer.transform(xs, ys)

    minx, maxx = float(np.min(src_xs)), float(np.max(src_xs))
    miny, maxy = float(np.min(src_ys)), float(np.max(src_ys))

    res_x, res_y = source_res
    buf_x = buffer_pixels * res_x
    buf_y = buffer_pixels * res_y

    return (minx - buf_x, miny - buf_y, maxx + buf_x, maxy + buf_y)


# =========================================================
# 2. HELPER: INDICES & EC2 RECOMMENDATION
# =========================================================


def get_indices_from_extent(x_coords, y_coords, projwin):
    ulx, uly, lrx, lry = projwin
    x_start = (np.abs(x_coords - ulx)).argmin()
    x_end = (np.abs(x_coords - lrx)).argmin()
    y_start = (np.abs(y_coords - uly)).argmin()
    y_end = (np.abs(y_coords - lry)).argmin()

    col = min(x_start, x_end)
    row = min(y_start, y_end)
    w = abs(x_end - x_start)
    h = abs(y_end - y_start)
    if w == 0:
        w = 1
    if h == 0:
        h = 1
    return col, row, w, h


def reproject_projwin(projwin, src_srs, dst_srs, step=None, buffer_frac=0.002):
    """
    Convert a projwin bounding box from *src_srs* to *dst_srs*.

    Densifies each edge before reprojecting so curved projection boundaries
    are captured correctly.  The number of sample points per edge adapts to
    the edge length: one point every *step* map-units (in *src_srs*) with a
    minimum of 21 points.  The result bbox is then expanded by *buffer_frac*
    on each side to absorb floating-point edge effects.

    Parameters
    ----------
    projwin : (ulx, uly, lrx, lry) in src_srs
    src_srs, dst_srs : CRS string (EPSG:XXXX, WKT, or PROJ string)
    step : float or None
        Approx. sample spacing along each edge in src_srs units.
        None → 1/100 of the shorter edge length (min 21 pts per edge).
    buffer_frac : float
        Fractional expansion of the output bbox on each side.  Default 0.002.

    Returns
    -------
    (ulx, uly, lrx, lry) in dst_srs — guaranteed to cover the full input bbox.
    """
    from pyproj import Transformer

    ulx, uly, lrx, lry = projwin
    dx = abs(lrx - ulx)
    dy = abs(uly - lry)

    if step is None:
        step = min(dx, dy) / 100.0 if min(dx, dy) > 0 else 1.0

    def _pts(a, b):
        n = max(21, int(abs(b - a) / step) + 2)
        return np.linspace(a, b, n)

    top_x   = _pts(ulx, lrx);  top_y   = np.full_like(top_x,   uly)
    right_y = _pts(uly, lry);  right_x = np.full_like(right_y, lrx)
    bot_x   = _pts(lrx, ulx);  bot_y   = np.full_like(bot_x,   lry)
    left_y  = _pts(lry, uly);  left_x  = np.full_like(left_y,  ulx)

    xs = np.concatenate([top_x, right_x, bot_x, left_x])
    ys = np.concatenate([top_y, right_y, bot_y, left_y])

    transformer = Transformer.from_crs(src_srs, dst_srs, always_xy=True)
    dst_xs, dst_ys = transformer.transform(xs, ys)

    out_ulx, out_uly = float(np.min(dst_xs)), float(np.max(dst_ys))
    out_lrx, out_lry = float(np.max(dst_xs)), float(np.min(dst_ys))

    buf_x = abs(out_lrx - out_ulx) * buffer_frac
    buf_y = abs(out_uly - out_lry) * buffer_frac
    return (out_ulx - buf_x, out_uly + buf_y, out_lrx + buf_x, out_lry - buf_y)


def recommend_ec2_instance(width, height, num_bands=1, downscale_factor=1):
    raw_size_gb = (width * height * num_bands * 4) / (1024**3)
    peak_ram_gb = raw_size_gb * 3.5 + 1.0

    print("--- Resource Estimation ---")
    print(f"Input: {width}x{height} pixels x {num_bands} bands")
    print(f"Raw Data Size: {raw_size_gb:.2f} GB")
    print(f"Estimated Peak RAM: {peak_ram_gb:.2f} GB")

    instances = [("t3.medium", 4), ("m6i.large", 8), ("r6i.large", 16), ("m6i.xlarge", 16), ("r6i.xlarge", 32), ("m6i.2xlarge", 32), ("r6i.2xlarge", 64), ("m6i.4xlarge", 64), ("r6i.4xlarge", 128), ("r6i.8xlarge", 256)]
    recommendation = None
    for name, ram in instances:
        if ram > (peak_ram_gb * 1.1):
            recommendation = (name, ram)
            break

    if recommendation:
        print(f"Recommended Instance: {recommendation[0]} ({recommendation[1]} GB RAM)")
    else:
        print(f"Recommended Instance: r6i.8xlarge+ (Needs >{peak_ram_gb:.1f} GB)")
    return peak_ram_gb


# =========================================================
# 3. HELPER: DOWNSCALING & VRT
# =========================================================


# -- Ancillary grid handling ---------------------------------------------------
# Maps HDF5 variable name -> (suffix, downscale, warp_resampling, out_dtype, nodata).
# Variables not in this dict are treated as backscatter (power) data.
_ANCILLARY_GRIDS = {
    "mask":                    ("mask",        "mask_priority", "nearest", "uint8",   255),
    # Exception codes are a bitwise OR, so a block reduces by OR, not by mean,
    # and the product defines no fill value for it -- hence no nodata.
    "inputDataExceptionMask":  ("exceptionmask", "flag_or",     "nearest", "uint8",   None),
    "numberOfLooks":           ("nlooks",      "sum",           "sum",     "float32", np.nan),
    "rtcGammaToSigmaFactor":   ("gamma2sigma", "mean",          "average", "float32", np.nan),
}


def _is_ancillary(var):
    """Return True if *var* is a known ancillary grid (not backscatter)."""
    return var in _ANCILLARY_GRIDS


def _ancillary_suffix(var):
    """Return the short output filename token for an ancillary grid."""
    return _ANCILLARY_GRIDS[var][0]


def _ancillary_downscale_method(var):
    return _ANCILLARY_GRIDS[var][1]


def _ancillary_warp_resampling(var):
    return _ANCILLARY_GRIDS[var][2]


def _ancillary_out_dtype(var):
    return _ANCILLARY_GRIDS[var][3]


def _ancillary_nodata(var):
    return _ANCILLARY_GRIDS[var][4]


def _downscale_block(data_3d, factor, method="mean"):
    """Block-downscale a (1, H, W) array using the given aggregation.

    *factor* may be an integer (isotropic) or a ``(factor_y, factor_x)`` tuple
    for anisotropic downscaling.

    Methods:
        mean           -- nanmean  (power, gamma2sigma)
        sum            -- nansum   (numberOfLooks)
        mask_priority  -- NISAR mask: 255 (fill) > 0 (invalid) > subswath (valid)
        flag_or        -- bitwise OR of flag codes (inputDataExceptionMask)
    """
    if isinstance(factor, (tuple, list)):
        factor_y, factor_x = int(factor[0]), int(factor[1])
    else:
        factor_y = factor_x = int(factor)
    _, h, w = data_3d.shape
    new_h = h - (h % factor_y)
    new_w = w - (w % factor_x)
    if new_h == 0 or new_w == 0:
        raise ValueError(f"Downscale factor ({factor_y},{factor_x}) larger than image ({w}x{h}).")
    cropped = data_3d[:, :new_h, :new_w]
    reshaped = cropped.reshape(1, new_h // factor_y, factor_y, new_w // factor_x, factor_x)

    if method == "mask_priority":
        # Vectorised priority: 255 (fill) > 0 (invalid) > subswath value.
        # NaN (from float32 read) is treated as fill (255).
        # Two reductions (max + min) instead of boolean array creation.
        int_view = np.nan_to_num(reshaped, nan=255.0).astype(np.uint8)
        max_val = np.max(int_view, axis=(2, 4))
        min_val = np.min(int_view, axis=(2, 4))
        result = np.where(max_val == 255, np.uint8(255),
                 np.where(min_val == 0, np.uint8(0), max_val))
        return result.astype(np.float32)  # (1, new_h, new_w) -- preserves input band dim

    if method == "flag_or":
        # inputDataExceptionMask holds a bitwise OR of exception codes, so a
        # block carries every code any of its pixels raised.  Averaging would
        # invent codes that were never set; OR keeps the flags exact and is
        # the same reduction the product used to build the pixel value.
        int_view = np.nan_to_num(reshaped, nan=0.0).astype(np.uint8)
        result = np.bitwise_or.reduce(
            np.bitwise_or.reduce(int_view, axis=4), axis=2)
        return result.astype(np.float32)

    with np.errstate(invalid="ignore"):
        if method == "sum":
            result = np.nansum(reshaped, axis=(2, 4))
            # nansum returns 0 for all-NaN blocks; restore NaN for nodata regions
            all_nan = np.all(np.isnan(reshaped), axis=(2, 4))
            result[all_nan] = np.nan
            return result.astype(np.float32)
        return np.nanmean(reshaped, axis=(2, 4)).astype(np.float32)


def _reproject_ancillary_band(data_2d, src_transform, src_crs, dst_transform,
                               dst_crs, dst_width, dst_height, resample_name,
                               num_threads=None):
    """Warp one ancillary 2-D array with a single-pass reproject.

    Unlike the two-pass power warp, ancillary grids use a straightforward
    reproject: nearest for mask, sum for nlooks, average for gamma2sigma.
    """
    n_threads = num_threads if num_threads is not None else os.cpu_count() or 1
    resample_enum = getattr(Resampling, resample_name, Resampling.nearest)

    src = data_2d.astype(np.float32)
    dst = np.full((dst_height, dst_width), np.nan, dtype=np.float32)

    reproject(
        source=src, destination=dst,
        src_transform=src_transform, src_crs=src_crs,
        dst_transform=dst_transform, dst_crs=dst_crs,
        src_nodata=np.nan, dst_nodata=np.nan,
        resampling=resample_enum,
        num_threads=n_threads,
    )
    return dst


def perform_downscaling(data_stack, factor):
    """Block-average downscale a (B, H, W) stack.

    *factor* may be an integer (isotropic) or a ``(factor_y, factor_x)`` tuple
    for anisotropic downscaling.
    """
    if factor is None:
        return data_stack
    if isinstance(factor, (tuple, list)):
        factor_y, factor_x = int(factor[0]), int(factor[1])
    else:
        factor_y = factor_x = int(factor)
    if factor_y <= 1 and factor_x <= 1:
        return data_stack
    b, h, w = data_stack.shape
    new_h = h - (h % factor_y)
    new_w = w - (w % factor_x)
    if new_h == 0 or new_w == 0:
        raise ValueError(f"Downscale factor ({factor_y},{factor_x}) is larger than image size ({w}x{h}).")
    cropped = data_stack[:, :new_h, :new_w]
    reshaped = cropped.reshape(b, new_h // factor_y, factor_y, new_w // factor_x, factor_x)
    with np.errstate(invalid="ignore"):
        downscaled = np.nanmean(reshaped, axis=(2, 4))
    return downscaled


def get_gdal_dtype(numpy_dtype):
    """Translates Numpy/Rasterio dtype strings to GDAL VRT strings."""
    d = str(numpy_dtype).lower()
    if "uint8" in d:
        return "Byte"
    if "int8" in d:
        return "Byte"
    if "uint16" in d:
        return "UInt16"
    if "int16" in d:
        return "Int16"
    if "uint32" in d:
        return "UInt32"
    if "int32" in d:
        return "Int32"
    if "complex64" in d or "cfloat32" in d:
        return "CFloat32"
    if "complex128" in d or "cfloat64" in d:
        return "CFloat64"
    if "float32" in d:
        return "Float32"
    if "float64" in d:
        return "Float64"
    return "Float32"


def _vrt_metadata_xml(metadata, indent="  "):
    """Build a <Metadata> block from a dict of key/value pairs."""
    if not metadata:
        return ""
    lines = [f"{indent}<Metadata>"]
    for k, v in metadata.items():
        lines.append(f'{indent}  <MDI key="{k}">{v}</MDI>')
    lines.append(f"{indent}</Metadata>")
    return "\n".join(lines)


def generate_vrt_xml_single_step(width, height, transform, crs_wkt, band_files, band_names, date_str, dtype="UInt16", nodata=None, metadata=None):
    vrt_dtype = get_gdal_dtype(dtype)
    nodata_val = _format_nodata_val(nodata, dtype)

    geo_transform = f"{transform.c}, {transform.a}, {transform.b}, {transform.f}, {transform.d}, {transform.e}"
    xml = [f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">', f'  <SRS dataAxisToSRSAxisMapping="1,2">{crs_wkt}</SRS>', f"  <GeoTransform>{geo_transform}</GeoTransform>"]
    ds_meta = _vrt_metadata_xml(metadata)
    if ds_meta:
        xml.append(ds_meta)
    for i, (fpath, bname) in enumerate(zip(band_files, band_names)):
        if fpath.startswith("s3://"):
            bucket, key = fpath.replace("s3://", "").split("/", 1)
            vrt_path = f"/vsis3/{bucket}/{key}"
            rel_attr = "0"
        else:
            vrt_path = os.path.basename(fpath)
            rel_attr = "1"
        band_xml = f"""
  <VRTRasterBand dataType="{vrt_dtype}" band="{i+1}">
    <NoDataValue>{nodata_val}</NoDataValue>
    <Description>{bname}</Description>
    <Metadata><MDI key="Date">{date_str}</MDI></Metadata>
    <SimpleSource>
      <SourceFilename relativeToVRT="{rel_attr}">{vrt_path}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />
      <DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />
    </SimpleSource>
  </VRTRasterBand>"""  # noqa
        xml.append(band_xml)
    xml.append("</VRTDataset>")
    return "\n".join(xml)


def _format_nodata_val(nodata, dtype):
    """Format nodata for VRT XML: int when possible, 'nan' for NaN, heuristic fallback."""
    if nodata is not None:
        try:
            import math
            if math.isnan(nodata):
                return "nan"
        except (TypeError, ValueError):
            pass
        return str(int(nodata)) if nodata == int(nodata) else str(nodata)
    d_str = str(dtype).lower()
    return "0" if ("int" in d_str or "byte" in d_str) else "nan"


def generate_vrt_xml_timeseries(width, height, transform, crs_wkt, stack_items, dtype="UInt16", nodata=None, metadata=None):
    vrt_dtype = get_gdal_dtype(dtype)
    nodata_val = _format_nodata_val(nodata, dtype)

    geo_transform = f"{transform.c}, {transform.a}, {transform.b}, {transform.f}, {transform.d}, {transform.e}"

    xml = [f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">', f'  <SRS dataAxisToSRSAxisMapping="1,2">{crs_wkt}</SRS>', f"  <GeoTransform>{geo_transform}</GeoTransform>"]
    ds_meta = _vrt_metadata_xml(metadata)
    if ds_meta:
        xml.append(ds_meta)

    for i, item in enumerate(stack_items):
        fpath = item["path"]
        if fpath.startswith("s3://"):
            bucket, key = fpath.replace("s3://", "").split("/", 1)
            vrt_path = f"/vsis3/{bucket}/{key}"
            rel_attr = "0"
        else:
            vrt_path = os.path.basename(fpath)
            rel_attr = "1"

        band_xml = f"""
  <VRTRasterBand dataType="{vrt_dtype}" band="{i + 1}">
    <NoDataValue>{nodata_val}</NoDataValue>
    <Description>{item['date']}</Description>
    <Metadata><MDI key="Date">{item['date']}</MDI></Metadata>
    <SimpleSource>
      <SourceFilename relativeToVRT="{rel_attr}">{vrt_path}</SourceFilename>
      <SourceBand>{item['band_idx']}</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />
      <DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />
    </SimpleSource>
  </VRTRasterBand>"""
        xml.append(band_xml)

    xml.append("</VRTDataset>")
    return "\n".join(xml)


def generate_vrt_xml_timeseries_union(crs_wkt, stack_items, dtype="Float32", nodata=None):
    """
    Time-series VRT with union spatial extent (one band per timestep).
    stack_items: list of dicts with keys: path, band_idx, date, transform, w, h
    All items must share the same CRS and pixel size.
    """
    vrt_dtype = get_gdal_dtype(dtype)
    nodata_val = _format_nodata_val(nodata, dtype)

    res_x = abs(stack_items[0]["transform"].a)
    res_y = abs(stack_items[0]["transform"].e)
    union_ulx = min(item["transform"].c for item in stack_items)
    union_uly = max(item["transform"].f for item in stack_items)
    union_lrx = max(item["transform"].c + item["w"] * abs(item["transform"].a) for item in stack_items)
    union_lry = min(item["transform"].f - item["h"] * abs(item["transform"].e) for item in stack_items)

    union_w = int(round((union_lrx - union_ulx) / res_x))
    union_h = int(round((union_uly - union_lry) / res_y))
    union_transform = from_origin(union_ulx, union_uly, res_x, res_y)

    geo_transform = f"{union_transform.c}, {union_transform.a}, {union_transform.b}, {union_transform.f}, {union_transform.d}, {union_transform.e}"
    xml = [
        f'<VRTDataset rasterXSize="{union_w}" rasterYSize="{union_h}">',
        f'  <SRS dataAxisToSRSAxisMapping="1,2">{crs_wkt}</SRS>',
        f"  <GeoTransform>{geo_transform}</GeoTransform>",
    ]

    for i, item in enumerate(stack_items):
        fpath = item["path"]
        if fpath.startswith("s3://"):
            bucket, key = fpath.replace("s3://", "").split("/", 1)
            vrt_path = f"/vsis3/{bucket}/{key}"
            rel_attr = "0"
        else:
            vrt_path = os.path.basename(fpath)
            rel_attr = "1"

        dst_x_off = int(round((item["transform"].c - union_ulx) / res_x))
        dst_y_off = int(round((union_uly - item["transform"].f) / res_y))
        band_xml = f"""
  <VRTRasterBand dataType="{vrt_dtype}" band="{i + 1}">
    <NoDataValue>{nodata_val}</NoDataValue>
    <Description>{item['date']}</Description>
    <Metadata><MDI key="Date">{item['date']}</MDI></Metadata>
    <SimpleSource>
      <SourceFilename relativeToVRT="{rel_attr}">{vrt_path}</SourceFilename>
      <SourceBand>{item['band_idx']}</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{item['w']}" ySize="{item['h']}" />
      <DstRect xOff="{dst_x_off}" yOff="{dst_y_off}" xSize="{item['w']}" ySize="{item['h']}" />
    </SimpleSource>
  </VRTRasterBand>"""
        xml.append(band_xml)

    xml.append("</VRTDataset>")
    return "\n".join(xml)


def construct_timeseries_filename(sample_h5_url, min_date, max_date, frequency, pol, mode_str):
    basename = sample_h5_url.split("/")[-1]
    if basename.endswith(".h5"):
        basename = basename[:-3]
    elif basename.endswith(".tif"):
        # Strip suffix to find base ID
        # ..._001-EBD_A_hh_AMP.tif
        if "-EBD_" in basename:
            basename = basename.split("-EBD_")[0]
        else:
            basename = basename[:-4]

    pattern = r"(\d{8}T\d{6})_(\d{8}T\d{6})"
    match = re.search(pattern, basename)
    if match:
        new_start = min_date.replace("-", "") + "T000000"
        new_end = max_date.replace("-", "") + "T235959"
        new_time_str = f"{new_start}_{new_end}"
        new_base = basename.replace(match.group(0), new_time_str)
    else:
        new_base = f"NISAR_TS_{min_date.replace('-', '')}_{max_date.replace('-', '')}"
    if mode_str:
        return f"{new_base}-EBD_{frequency}_{pol}_{mode_str}.vrt"
    return f"{new_base}-EBD_{frequency}_{pol}.vrt"


# =========================================================
# 3b. HDF5 SUBSET WRITER
# =========================================================


def _write_h5_subset(src_f, grid_path, variable_names, col, row, w, h,
                     all_frequencies=True, ancillary_floats=True):
    """
    Return ``(bytes, written_freqs)`` for a NetCDF-4/HDF5 subset openable by
    GDAL's NETCDF: driver.  Uses the netCDF4 library so that named dimensions,
    _Netcdf4Dimid, _NCProperties and grid_mapping are all written correctly.
    Avoids h5py.copy() -- only targeted range reads are issued against src_f.

    With *all_frequencies* (the default) every frequency group present in the
    source is written, each windowed on its own axes; set it False to restrict
    the product to *grid_path*.  identification and the metadata groups are
    included so the result is a self-contained product rather than a bare
    raster container.

    *ancillary_floats* False drops the float ancillary grids (numberOfLooks,
    rtcGammaToSigmaFactor) that are picked up automatically -- they are
    float32 at full window size and roughly double a subset, while the uint8
    flag grids cost a few KB and are always kept.  A grid named explicitly
    through *variable_names* is always written.
    """
    try:
        import netCDF4 as nc_lib
    except ImportError:
        raise ImportError(
            "netCDF4 library required for -of h5 output. "
            "Install with: pip install netCDF4")

    def _v(val):
        """Decode bytes attrs to str for netCDF4."""
        if isinstance(val, (bytes, np.bytes_)):
            return val.decode("utf-8", errors="replace")
        if isinstance(val, np.ndarray) and val.dtype.kind == "S":
            return val.astype("U")
        return val

    def _cpattrs(src_obj, dst_obj, skip=()):
        for k, v in src_obj.attrs.items():
            if k in skip or k.startswith("_NC"):
                continue
            try:
                setattr(dst_obj, k, _v(v))
            except Exception:
                pass

    # --- product metadata, written as NetCDF-4 groups -----------------------
    # The raster variables above are what GDAL's NETCDF: driver reads; the
    # metadata below is added as sibling groups so the subset is a
    # self-contained product without giving up that readability.
    _ATTR_SKIP = ("_FillValue", "DIMENSION_LIST", "REFERENCE_LIST",
                  "CLASS", "NAME")
    _cmp_types = {}          # compound types are file-scoped; register once

    def _nc_write_ds(dst_grp, name, ds, sl=None, tail_dims=None,
                     keep_fill=False, complevel=4):
        """Copy one h5py dataset into *dst_grp* as a NetCDF-4 variable.

        NetCDF requires every dimension to be named, so leading dimensions get
        generated names.  *tail_dims* pins the trailing dimensions to the
        group's own coordinate dimensions -- passing them explicitly rather
        than matching on size, which would be ambiguous whenever a window comes
        out square (ny == nx).  *keep_fill* carries the source _FillValue onto
        the new variable, which netCDF can only take at creation time, and
        *complevel* lets a caller match the source's own deflate level rather
        than pay to recompress harder than the granule did.

        Returns the variable written.
        """
        data = np.asarray(ds[()] if sl is None else ds[sl])
        tail = tuple(tail_dims or ())
        dims = []
        for i, n in enumerate(data.shape[:data.ndim - len(tail)]):
            dn = f"{name}_d{i}"
            if dn not in dst_grp.dimensions:
                dst_grp.createDimension(dn, n)
            dims.append(dn)
        dims = tuple(dims) + tail

        if data.dtype.kind in "SOU":
            vals = data.astype("U") if data.dtype.kind == "S" else data
            var = dst_grp.createVariable(name, str, dims)
            if data.ndim == 0:
                var[...] = str(vals)
            else:
                var[:] = np.asarray(vals, dtype=object)
        elif data.dtype.kind == "c":
            # netCDF has no native complex type, and this build predates
            # auto_complex; use the same (r, i) compound representation
            # auto_complex would have written.  Complex calibration LUTs
            # would otherwise be dropped.
            base = "f4" if data.dtype.itemsize == 8 else "f8"
            cdt = np.dtype([("r", base), ("i", base)])
            tname = f"complex{data.dtype.itemsize * 8}"
            arr = np.empty(data.shape, dtype=cdt)
            arr["r"], arr["i"] = data.real, data.imag
            with warnings.catch_warnings():
                # netCDF4 notes that the native-endian compound is stored
                # big-endian; the values round-trip unchanged either way.
                warnings.filterwarnings("ignore", message=".*endian-ness.*")
                if tname not in _cmp_types:
                    _cmp_types[tname] = dst.createCompoundType(cdt, tname)
                var = dst_grp.createVariable(name, _cmp_types[tname], dims)
                var[...] = arr if data.ndim else arr.reshape(())
        elif data.ndim == 0:
            var = dst_grp.createVariable(name, data.dtype, ())
            var[...] = data
        else:
            kw = {}
            if keep_fill and "_FillValue" in ds.attrs:
                try:
                    kw["fill_value"] = np.asarray(
                        ds.attrs["_FillValue"]).astype(data.dtype).item()
                except Exception:
                    kw = {}
            var = dst_grp.createVariable(name, data.dtype, dims,
                                         zlib=True, complevel=complevel, **kw)
            var[:] = data
        _cpattrs(ds, var, skip=_ATTR_SKIP)
        return var

    def _deflate(ds, default=4):
        """The source's own gzip level, so a windowed copy of a grid costs the
        compression the granule already chose (gzip/1 for the float grids,
        gzip/9 for the near-uniform flag grids) rather than a flat level 4."""
        if ds.compression == "gzip" and ds.compression_opts:
            return int(ds.compression_opts)
        return default

    def _nc_copy_group(src_grp, dst_parent, name, extent=None, margin=2,
                       skip=()):
        """Recursively copy an HDF5 group, windowing grid-borne arrays.

        Mirrors the GSLC subsetter: any array whose trailing dims match the
        group's own (yCoordinates, xCoordinates) is sliced to *extent*, with a
        margin so interpolation at the window edge still has neighbours;
        everything else is copied verbatim.  Groups without coordinate axes
        (orbit, attitude) fall through to a verbatim copy.
        """
        g = dst_parent.createGroup(name)
        _cpattrs(src_grp, g)

        gx = src_grp["xCoordinates"][()] if "xCoordinates" in src_grp else None
        gy = src_grp["yCoordinates"][()] if "yCoordinates" in src_grp else None
        nx = ny = None
        c = r = ww = hh = 0
        if extent is not None and gx is not None and gy is not None:
            nx, ny = len(gx), len(gy)
            c, r, ww, hh = get_indices_from_extent(gx, gy, extent)
            c = max(0, c - margin); r = max(0, r - margin)
            ww = min(nx - c, ww + 2 * margin); hh = min(ny - r, hh + 2 * margin)
            g.createDimension("yCoordinates", hh)
            g.createDimension("xCoordinates", ww)

        for k in src_grp:
            if k in skip:
                continue
            child = src_grp[k]
            if isinstance(child, h5py.Group):
                _nc_copy_group(child, g, k, extent, margin)
                continue
            try:
                if nx and k == "xCoordinates":
                    _nc_write_ds(g, k, child, (slice(c, c + ww),),
                                 tail_dims=("xCoordinates",))
                elif ny and k == "yCoordinates":
                    _nc_write_ds(g, k, child, (slice(r, r + hh),),
                                 tail_dims=("yCoordinates",))
                elif ny and child.ndim >= 2 and child.shape[-2:] == (ny, nx):
                    _nc_write_ds(g, k, child,
                                 (Ellipsis, slice(r, r + hh), slice(c, c + ww)),
                                 tail_dims=("yCoordinates", "xCoordinates"))
                else:
                    _nc_write_ds(g, k, child)
            except Exception as exc:
                print(f"    [WARN] metadata {src_grp.name}/{k} not copied "
                      f"({type(exc).__name__}: {exc})", flush=True)

    fd, tmp_path = tempfile.mkstemp(suffix=".h5")
    os.close(fd)
    tmp_repacked = tmp_path + "_r.h5"
    try:
        with nc_lib.Dataset(tmp_path, "w", format="NETCDF4") as dst:

            # Global attributes (skip _NC* -- managed by netCDF4 library)
            _cpattrs(src_f, dst)

            def _write_freq(fq_path, var_names, c, r, ww, hh):
                """Write one frequency grid group, windowed on its own axes.

                Frequencies do not share a grid -- in a DHDH granule frequency
                B is posted 8x coarser than A -- so the caller's indices apply
                only to the requested frequency and the rest are recomputed.
                """
                g = dst
                cur = ""
                for part in fq_path.strip("/").split("/"):
                    cur += f"/{part}"
                    g = g.createGroup(part)
                    if cur in src_f:
                        _cpattrs(src_f[cur], g)

                # Named dimensions
                g.createDimension("yCoordinates", hh)
                g.createDimension("xCoordinates", ww)

                # Coordinate variables (two targeted range reads)
                xs = src_f[f"{fq_path}/xCoordinates"]
                ys = src_f[f"{fq_path}/yCoordinates"]
                xv = g.createVariable("xCoordinates", xs.dtype, ("xCoordinates",))
                yv = g.createVariable("yCoordinates", ys.dtype, ("yCoordinates",))
                xv[:] = xs[c:c + ww]
                yv[:] = ys[r:r + hh]
                _cpattrs(xs, xv)
                _cpattrs(ys, yv)

                # Grid mapping scalar (one tiny read)
                if f"{fq_path}/projection" in src_f:
                    proj_src = src_f[f"{fq_path}/projection"]
                    proj_var = g.createVariable("projection", "i4", ())
                    try:
                        proj_var[:] = int(proj_src[()])
                    except Exception:
                        pass
                    _cpattrs(proj_src, proj_var)

                # Data variables (one range read per variable)
                for var in var_names:
                    if f"{fq_path}/{var}" not in src_f:
                        continue
                    src_ds = src_f[f"{fq_path}/{var}"]
                    if src_ds.ndim != 2 or src_ds.shape != (len(ys), len(xs)):
                        # e.g. frequencyB/inputDataExceptionMask is stored at
                        # frequency A's shape and cannot be windowed here.
                        print(f"    [WARN] {fq_path}/{var} has shape "
                              f"{src_ds.shape}, which does not match this "
                              f"frequency's grid {(len(ys), len(xs))}; "
                              f"omitted.", flush=True)
                        continue
                    if src_ds.dtype.kind in "ui":
                        # Flag grids named explicitly through -vars (mask,
                        # inputDataExceptionMask) take the same integer path
                        # as the ones picked up below, so how a grid was
                        # asked for does not decide its dtype.
                        _nc_write_ds(g, var, src_ds,
                                     (slice(r, r + hh), slice(c, c + ww)),
                                     tail_dims=("yCoordinates", "xCoordinates"),
                                     keep_fill=True,
                                     complevel=_deflate(src_ds))
                        continue
                    data = src_ds[r:r + hh, c:c + ww].astype(np.float32)
                    raw_fill = src_ds.attrs.get("_FillValue", np.nan)
                    try:
                        fill_val = float(raw_fill)
                    except Exception:
                        fill_val = np.nan
                    var_out = g.createVariable(
                        var, "f4", ("yCoordinates", "xCoordinates"),
                        zlib=True, complevel=4, fill_value=fill_val)
                    var_out[:] = data
                    _cpattrs(src_ds, var_out, skip=("_FillValue",))

                # Everything else the frequency group holds.  The covariance
                # terms above are only the payload: the subswath `mask` and
                # `rtcGammaToSigmaFactor` are what let a subset be masked and
                # gamma->sigma converted the way the source can, and
                # `numberOfLooks` and `inputDataExceptionMask` complete the
                # per-pixel record.  Writing only the covariance terms left a
                # subset that could not be processed like the granule it came
                # from.  Each 2-D grid costs exactly one windowed read, the
                # same cost as a covariance term, and no array is ever read
                # whole.  Integer grids keep their own dtype -- casting a
                # uint8 mask to float32 would turn flag codes into values
                # something downstream may interpolate.
                _cov = {k for k in src_f[fq_path]
                        if len(k) == 4 and k.isupper()}
                for item in sorted(src_f[fq_path]):
                    if item in g.variables or item in _cov:
                        continue
                    ds = src_f[f"{fq_path}/{item}"]
                    if not isinstance(ds, h5py.Dataset):
                        continue
                    try:
                        if item in ("listOfCovarianceTerms",
                                    "listOfPolarizations"):
                            # These must name what was written, not what the
                            # granule held: a single-polarisation subset of a
                            # dual-pol granule would otherwise claim both.
                            _vals = [t.decode() if hasattr(t, "decode")
                                     else str(t) for t in ds[()]]
                            if item == "listOfCovarianceTerms":
                                _keep = [t for t in _vals if t in var_names]
                            else:
                                _pols = {t[:2] for t in var_names
                                         if len(t) == 4}
                                _pols |= {t[2:] for t in var_names
                                          if len(t) == 4}
                                _keep = [q for q in _vals if q in _pols]
                            _keep = _keep or _vals
                            _dim = f"{item}_d0"
                            if _dim not in g.dimensions:
                                g.createDimension(_dim, len(_keep))
                            _lv = g.createVariable(item, str, (_dim,))
                            _lv[:] = np.array(_keep, dtype=object)
                            _cpattrs(ds, _lv, skip=_ATTR_SKIP)
                        elif ds.ndim == 2 and ds.shape == (len(ys), len(xs)):
                            if not ancillary_floats and ds.dtype.kind == "f":
                                # -noanc: numberOfLooks and
                                # rtcGammaToSigmaFactor are the bulk of a
                                # subset; the uint8 flag grids are not.
                                continue
                            _nc_write_ds(
                                g, item, ds,
                                (slice(r, r + hh), slice(c, c + ww)),
                                tail_dims=("yCoordinates", "xCoordinates"),
                                keep_fill=True, complevel=_deflate(ds))
                        elif ds.ndim == 2:
                            # The source stores frequencyB's exception mask at
                            # frequency A's shape -- byte-for-byte the size of
                            # A's own array.  It cannot be windowed on this
                            # grid, and copying it verbatim would drag the
                            # full frame into the subset.
                            print(f"    [WARN] {fq_path}/{item} has shape "
                                  f"{ds.shape}, which does not match this "
                                  f"frequency's grid {(len(ys), len(xs))}; "
                                  f"omitted.", flush=True)
                        else:
                            # Scalars (spacings, bandwidths, numberOfSubSwaths)
                            # and short vectors, copied verbatim.
                            _nc_write_ds(g, item, ds)
                    except Exception as exc:
                        print(f"    [WARN] {fq_path}/{item} not copied "
                              f"({type(exc).__name__}: {exc})", flush=True)

            # The requested frequency, plus every other one present unless the
            # caller restricted the product to a single frequency.
            grids_base = grid_path.rsplit("/", 1)[0]
            written_freqs = [grid_path.rsplit("frequency", 1)[-1]]
            _write_freq(grid_path, variable_names, col, row, w, h)

            if all_frequencies:
                _wx = src_f[f"{grid_path}/xCoordinates"][col:col + w]
                _wy = src_f[f"{grid_path}/yCoordinates"][row:row + h]

                def _edge(centers, key):
                    p = f"{grid_path}/{key}"
                    sp = (float(src_f[p][()]) if p in src_f
                          else (float(centers[1] - centers[0])
                                if len(centers) > 1 else 0.0))
                    half = abs(sp) / 2.0
                    return (min(float(centers[0]), float(centers[-1])) - half,
                            max(float(centers[0]), float(centers[-1])) + half)

                x_lo, x_hi = _edge(_wx, "xCoordinateSpacing")
                y_lo, y_hi = _edge(_wy, "yCoordinateSpacing")

                for fk in sorted(src_f[grids_base].keys()):
                    fq_path = f"{grids_base}/{fk}"
                    if not fk.startswith("frequency") or fq_path == grid_path:
                        continue
                    if f"{fq_path}/xCoordinates" not in src_f:
                        continue
                    fx = src_f[f"{fq_path}/xCoordinates"][()]
                    fy = src_f[f"{fq_path}/yCoordinates"][()]
                    fdx = (float(src_f[f"{fq_path}/xCoordinateSpacing"][()])
                           if f"{fq_path}/xCoordinateSpacing" in src_f
                           else float(fx[1] - fx[0]))
                    fdy = (float(src_f[f"{fq_path}/yCoordinateSpacing"][()])
                           if f"{fq_path}/yCoordinateSpacing" in src_f
                           else float(fy[1] - fy[0]))
                    fc, fw = covering_indices(fx, fdx, x_lo, x_hi)
                    fr, fh = covering_indices(fy, fdy, y_lo, y_hi)
                    if fw == 0 or fh == 0:
                        print(f"    [WARN] {fq_path} does not overlap the "
                              f"requested window; skipped.", flush=True)
                        continue
                    # Covariance terms name this frequency's data variables.
                    lct = f"{fq_path}/listOfCovarianceTerms"
                    if lct in src_f:
                        fvars = [t.decode() if hasattr(t, "decode") else str(t)
                                 for t in src_f[lct][()]]
                    else:
                        fvars = [k for k in src_f[fq_path]
                                 if len(k) == 4 and k.isupper()]
                    _write_freq(fq_path, fvars, fc, fr, fw, fh)
                    written_freqs.append(fk.replace("frequency", ""))

            # ----------------------------------------------------------
            # identification + metadata, so the subset is self-contained
            # ----------------------------------------------------------
            # Window extent in the product CRS, from the subset pixel
            # centres (x ascending, y descending) -> [ulx, uly, lrx, lry].
            _xs = src_f[f"{grid_path}/xCoordinates"][col:col + w]
            _ys = src_f[f"{grid_path}/yCoordinates"][row:row + h]
            _extent = [float(_xs[0]), float(_ys[0]),
                       float(_xs[-1]), float(_ys[-1])]

            _prod = grid_path.strip("/").split("/")[2]   # e.g. GCOV
            _ident = "/science/LSAR/identification"
            if _ident in src_f:
                _lsar = dst.createGroup("science/LSAR")
                # listOfFrequencies is written fresh below: NetCDF variables
                # cannot be resized or deleted, so copying the source's list
                # first would fix the length at the granule's frequency count.
                _nc_copy_group(src_f[_ident], _lsar, "identification",
                               skip=("listOfFrequencies",))

                # Replace the inherited full-granule footprint with one
                # describing the window, per the isce3 geocoded-grid
                # convention (shared with the GSLC subsetter).
                try:
                    ig = _lsar.groups["identification"]
                    proj_val = src_f[f"{grid_path}/projection"][()]
                    crs_str = (proj_val.decode() if hasattr(proj_val, "decode")
                               else f"EPSG:{int(proj_val)}")

                    def _sp(key, arr):
                        p = f"{grid_path}/{key}"
                        if p in src_f:
                            return float(src_f[p][()])
                        return float(arr[1] - arr[0]) if len(arr) > 1 else 0.0

                    wkt = _geo_grid_bounding_polygon_wkt(
                        _xs, _ys, _sp("xCoordinateSpacing", _xs),
                        _sp("yCoordinateSpacing", _ys), crs_str)
                    if "boundingPolygon" in ig.variables:
                        ig.variables["boundingPolygon"][...] = wkt
                    else:
                        bp = ig.createVariable("boundingPolygon", str, ())
                        bp[...] = wkt
                except Exception as exc:
                    print(f"    [WARN] boundingPolygon not recomputed ({exc}); "
                          f"the subset may carry the full-granule footprint.",
                          flush=True)

                # listOfFrequencies must name what was actually written.
                try:
                    ig = _lsar.groups["identification"]
                    vals = np.asarray(sorted(written_freqs), dtype=object)
                    ig.createDimension("listOfFrequencies_d0", len(vals))
                    lf = ig.createVariable("listOfFrequencies", str,
                                           ("listOfFrequencies_d0",))
                    lf[:] = vals
                    if "listOfFrequencies" in src_f[_ident]:
                        _cpattrs(src_f[_ident]["listOfFrequencies"], lf,
                                 skip=_ATTR_SKIP)
                except Exception as exc:
                    print(f"    [WARN] listOfFrequencies not updated ({exc}).",
                          flush=True)

            _meta = f"/science/LSAR/{_prod}/metadata"
            if _meta in src_f:
                _pg = dst.createGroup(f"science/LSAR/{_prod}")
                _mg = _pg.createGroup("metadata")
                for _gname in src_f[_meta]:
                    _child = src_f[f"{_meta}/{_gname}"]
                    if not isinstance(_child, h5py.Group):
                        continue
                    # orbit/attitude have no map grid -> verbatim
                    _nc_copy_group(
                        _child, _mg, _gname,
                        extent=None if _gname in ("orbit", "attitude")
                        else _extent)

        # h5repack for compact sequential layout (cloud-friendly)
        try:
            sp.check_call(["h5repack", tmp_path, tmp_repacked],
                          stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            read_path = tmp_repacked
        except Exception:
            read_path = tmp_path  # h5repack unavailable -- use original

        with open(read_path, "rb") as fh:
            return fh.read(), sorted(written_freqs)

    finally:
        for p in [tmp_path, tmp_repacked]:
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass


# =========================================================
# 4. CORE I/O FUNCTIONS
# =========================================================


def _estimate_remote_size(url, fs=None):
    """Best-effort file size in bytes; returns 0 on failure."""
    try:
        if url.startswith("s3://") and fs is not None:
            return fs.info(url)["size"]
        if not url.startswith(("s3://", "https://")):
            return os.path.getsize(url)
    except Exception:
        pass
    return 0


def _shm_tmpdir(needed_bytes, multiplier=2):
    """
    Return /dev/shm if it exists, is writable, and has at least
    multiplier * needed_bytes free.  Otherwise return None.
    """
    shm = "/dev/shm"
    if not os.path.isdir(shm):
        return None
    try:
        if shutil.disk_usage(shm).free >= needed_bytes * multiplier:
            return shm
    except OSError:
        pass
    return None


def cache_to_local(url, localdir=None, keep=False, use_earthdata=False, fs=None):
    is_remote = url.startswith(("https://", "s3://"))

    # Local files: skip caching unless an explicit directory was given.
    # Creating a temp symlink to a local file wastes time without benefit.
    if not is_remote and localdir in [None, "yes", "y"]:
        return url

    if localdir in [None, "yes", "y"]:
        size = _estimate_remote_size(url, fs=fs)
        tmpdir = (_shm_tmpdir(size) if size > 0 else None) or tempfile.gettempdir()
        localdir = tempfile.mkdtemp(prefix="tmp_h5toCOG_", dir=tmpdir)
        if not keep:
            atexit.register(shutil.rmtree, localdir, True)

    localdir = localdir.rstrip("/") + "/"
    os.makedirs(localdir, exist_ok=True)

    local_path = os.path.join(localdir, os.path.basename(url))
    print(f"---> Caching {url} to {localdir} ... ", flush=True, end="")

    if url.startswith("https://"):
        _download_https(url, local_path, localdir, use_earthdata=use_earthdata)
    elif url.startswith("s3://"):
        # s3:// -- use aws cli (with optional earthdata S3 credentials)
        env = os.environ.copy()
        if use_earthdata:
            auth = earthaccess.login(strategy="netrc")
            creds = auth.get_s3_credentials(endpoint="https://nisar.asf.earthdatacloud.nasa.gov/s3credentials")
            env["AWS_ACCESS_KEY_ID"] = creds["accessKeyId"]
            env["AWS_SECRET_ACCESS_KEY"] = creds["secretAccessKey"]
            env["AWS_SESSION_TOKEN"] = creds["sessionToken"]
        cmd = f"aws s3 cp {url} {localdir}"
        sp.check_call(shlex.split(cmd), env=env)
    else:
        # local file into an explicit cache dir -- symlink
        os.symlink(os.path.abspath(url), local_path)

    print("done")

    if not keep:
        atexit.register(_unlink, local_path)

    return local_path


def _download_https(url, local_path, localdir, use_earthdata=False):
    """Download an HTTPS URL, preferring aria2c for parallel connections."""
    if shutil.which("aria2c"):
        download_url = url
        if use_earthdata:
            # earthaccess URLs redirect to a presigned S3 URL; aria2c cannot
            # follow the OAuth redirect chain, so resolve it first.
            session = earthaccess.get_requests_https_session()
            resp = session.get(url, allow_redirects=False)
            if resp.status_code in (302, 303):
                download_url = resp.headers["Location"]
            else:
                # Could not resolve presigned URL -- fall back to earthaccess
                earthaccess.login(strategy="netrc")
                earthaccess.download([url], localdir)
                return
        cmd = [
            "aria2c",
            "-x", "16", "-s", "16",
            "--auto-file-renaming=false",
            "-d", localdir,
            "-o", os.path.basename(local_path),
            download_url,
        ]
        sp.check_call(cmd)
        return

    # Fallback: earthaccess or plain urllib
    if use_earthdata:
        earthaccess.login(strategy="netrc")
        earthaccess.download([url], localdir)
    else:
        import urllib.request
        urllib.request.urlretrieve(url, local_path)


def _s3_creds_from_fs(fs):
    """Extract serializable S3 credentials from an s3fs.S3FileSystem object."""
    if fs is None:
        return {}
    try:
        key = fs.key or fs.storage_options.get("key")
        secret = fs.secret or fs.storage_options.get("secret")
        token = fs.token or fs.storage_options.get("token")
        return {"key": key, "secret": secret, "token": token}
    except Exception:
        return {}


def _s3_stripe_worker(task):
    """
    Top-level picklable worker executed in a subprocess.

    Each subprocess has its own HDF5 library state (its own 'phil' lock), so
    multiple subprocesses can issue concurrent S3 range requests without
    blocking each other.

    task: (file_url, s3_creds, ds_path, r_start, r_end, col, w)
    returns: (ds_path, r_start_offset, numpy_array)
    """
    file_url, s3_creds, ds_path, r_start, r_end, col, w, row = task
    import h5py
    import numpy as np
    import s3fs

    fs = s3fs.S3FileSystem(
        key=s3_creds.get("key"),
        secret=s3_creds.get("secret"),
        token=s3_creds.get("token"),
    )
    s3_file = fs.open(file_url, mode="rb", cache_type="bytes", block_size=16 * 1024 * 1024)
    fh = h5py.File(s3_file, driver="fileobj", mode="r", libver="latest", rdcc_nbytes=0)
    try:
        _raw = fh[ds_path][r_start:r_end, col : col + w]
        data = np.abs(_raw).astype(np.float32) if np.iscomplexobj(_raw) else _raw.astype(np.float32)
        return ds_path, r_start - row, data
    finally:
        fh.close()


def _read_bands_parallel(file_url, input_fs, grid_path, variable_names, row, h, col, w, n_workers=8):
    """
    Read HDF5 bands from a remote S3 file using subprocesses.

    h5py holds the global 'phil' RLock for the entire duration of each read,
    including blocking S3 network waits.  Threads all serialize on this lock,
    so ProcessPoolExecutor (one HDF5 state per process) is required for true
    parallelism.

    For local files, falls back to simple serial reads (no subprocess overhead).

    Stripe row-counts are aligned to the NISAR HDF5 chunk height (512).
    """
    import math

    CHUNK_H = 512  # NISAR default chunk height

    workers_per_band = max(1, n_workers // len(variable_names))
    stripe_h = max(CHUNK_H, math.ceil(h / workers_per_band / CHUNK_H) * CHUNK_H)

    tasks = []
    for var in variable_names:
        ds_path = f"{grid_path}/{var}"
        r = row
        while r < row + h:
            r_end = min(r + stripe_h, row + h)
            tasks.append((var, ds_path, r, r_end))
            r = r_end

    out_arrays = {var: np.empty((h, w), dtype=np.float32) for var in variable_names}

    if file_url.startswith("s3://"):
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing as mp

        s3_creds = _s3_creds_from_fs(input_fs)
        worker_tasks = [
            (file_url, s3_creds, ds_path, r_start, r_end, col, w, row)
            for var, ds_path, r_start, r_end in tasks
        ]
        var_order = [var for var, ds_path, r_start, r_end in tasks]

        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(len(tasks), n_workers), mp_context=ctx) as pool:
            for var, (_, offset, stripe_data) in zip(var_order, pool.map(_s3_stripe_worker, worker_tasks)):
                out_arrays[var][offset : offset + stripe_data.shape[0]] = stripe_data
    else:
        # Local or HTTPS: serial reads (subprocesses add overhead without benefit here)
        fh = open_h5_lazy(file_url, input_fs)
        try:
            for var in variable_names:
                ds_path = f"{grid_path}/{var}"
                _raw = fh[ds_path][row : row + h, col : col + w]
                out_arrays[var] = np.abs(_raw).astype(np.float32) if np.iscomplexobj(_raw) else _raw.astype(np.float32)
        finally:
            fh.close()

    return [out_arrays[var] for var in variable_names]


def open_h5_lazy_slow(path, s3_fs):
    if path.startswith("s3://"):
        return h5py.File(s3_fs.open(path, "rb"), "r")
    return h5py.File(path, "r")


# --- HDF5 paged-aggregation aware remote access -----------------------------
#
# NISAR L1/L2 products are written with HDF5 paged aggregation
# (H5F_FSPACE_STRATEGY_PAGE).  Reading with an LRU block cache whose blocks are
# aligned to the file's page size -- rather than a single-region byte cache --
# keeps every read page-aligned instead of re-fetching overlapping regions.
# Measured in-region (s3, us-west-2) on a 12.5 GB GSLC with a 4 MiB page:
#
#   workload                       bytes/16MiB (old)   blockcache/1x page
#   60 scattered metadata datasets      25-31 s              4.0 s
#   windowed imagery (2794x2285)         4.3 s               1.1 s
#   sequential full-width stripe        16.9 s               6.6 s
#
# Larger blocks were worse in all three regimes: a windowed read of a wide
# raster is scattered at the block level too, since consecutive needed chunks
# sit far apart in the file, so big blocks mostly over-fetch discarded
# neighbours.
#
# The page size is a property of the writer, so probe it rather than hardcode
# it: L0B, BETA-era files and non-JPL products (e.g. ISRO S-band) come from
# different writers and need not be paged at all.
H5_DEFAULT_BLOCK = 4 * 1024 * 1024      # used when the file is not paged
H5_PROBE_BLOCK = 1024 * 1024            # small block for the one-off probe
H5_BLOCK_PAGES = 1                      # fsspec block size, in pages
H5_PAGE_BUF_PAGES = 8                   # HDF5 page buffer, in pages
H5_PAGE_BUF_MAX = 64 * 1024 * 1024

# Probed file-space parameters keyed by path.  A granule is opened several
# times over a run (grid info, band reads, subset write); probe it once.
_H5_PAGE_PARAMS = {}

# Authenticated Earthdata HTTPS filesystem, built once per process.
_EARTHACCESS_HTTPS_FS = []


def _earthaccess_https_fs():
    """Authenticated fsspec filesystem for Earthdata HTTPS, or None.

    ``earthaccess.open()`` hands back a file already wrapped in fsspec's
    default cache, which cannot be reconfigured afterwards.  Going through the
    filesystem instead lets open_h5_lazy apply the same page-aligned blockcache
    the S3 branch uses -- see the note there for what the default costs.

    Returns None when earthaccess is absent or its session cannot be built, so
    callers keep their existing fallback.  Only successes are cached; a failure
    here means earthaccess is unusable anyway.
    """
    if _EARTHACCESS_HTTPS_FS:
        return _EARTHACCESS_HTTPS_FS[0]
    if not HAS_EARTHACCESS:
        return None
    try:
        if earthaccess._store is None:
            _earthaccess_login()
        fs = earthaccess.get_fsspec_https_session()
    except Exception:
        return None
    _EARTHACCESS_HTTPS_FS.append(fs)
    return fs


def probe_h5_page_params(path, s3_fs=None):
    """Return ``(block_size, page_buf_size)`` to open *path* with.

    Opens the file once with a small block, reads the file-creation property
    list, and derives the settings from the actual page size.  Returns
    ``page_buf_size=None`` for files that are not paged, so the caller opens
    without the HDF5 page buffer.  Any failure degrades to the defaults rather
    than propagating -- this is an optimisation, never a correctness gate.
    """
    if path in _H5_PAGE_PARAMS:
        return _H5_PAGE_PARAMS[path]

    params = (H5_DEFAULT_BLOCK, None)
    try:
        if path.startswith("s3://"):
            fobj = s3_fs.open(path, mode="rb", cache_type="blockcache",
                              block_size=H5_PROBE_BLOCK)
            fh = h5py.File(fobj, driver="fileobj", mode="r")
        elif path.startswith("https://"):
            # An extra remote open is expensive here (earthaccess re-auths and
            # re-opens), so skip the probe and let the caller's fallback pick
            # it up.
            _H5_PAGE_PARAMS[path] = params
            return params
        else:
            fh = h5py.File(path, "r")

        with fh:
            cp = fh.id.get_create_plist()
            strategy = cp.get_file_space_strategy()[0]
            page = int(cp.get_file_space_page_size())
        if strategy == h5py.h5f.FSPACE_STRATEGY_PAGE and page > 0:
            params = (H5_BLOCK_PAGES * page,
                      min(H5_PAGE_BUF_PAGES * page, H5_PAGE_BUF_MAX))
    except Exception:
        pass

    _H5_PAGE_PARAMS[path] = params
    return params


def covering_indices(nodes, spacing, lo, hi):
    """First index and count of every cell on *nodes* overlapping [lo, hi].

    get_indices_from_extent snaps to the nearest node, which truncates by up to
    half a cell on a coarse grid and can leave frequencies covering different
    ground.  Selecting every overlapping cell instead guarantees each frequency
    covers the requested window.  Works for ascending or descending axes, since
    the selected indices are contiguous either way.
    """
    half = abs(spacing) / 2.0
    inside = np.nonzero((nodes + half > lo) & (nodes - half < hi))[0]
    if inside.size == 0:
        return 0, 0
    return int(inside[0]), int(inside[-1] - inside[0] + 1)


def _geo_grid_bounding_polygon_wkt(x_centers, y_centers, dx, dy, crs_str,
                                   pts_per_edge=11, height=0.0):
    """
    WKT bounding polygon describing a geocoded grid window.

    Mirrors isce3.geometry.make_geo_grid_bounding_polygon -- the isce3 routine
    for turning a geocoded grid into a boundingPolygon -- and the conformance
    annotation in nisar_L2_GSLC.xml:

      * the perimeter is sampled with *pts_per_edge* points per edge, yielding
        4 * (pts_per_edge - 1) + 1 = 41 vertices by default, the same vertex
        count NISAR granules carry;
      * the ring starts at the upper-left corner and runs counter-clockwise on
        the map (left edge top->bottom, bottom left->right, right bottom->top,
        top right->left), and is explicitly closed;
      * vertices are 3-D "lon lat height" (height in metres above the WGS84
        ellipsoid), comma-separated as WKT requires;
      * the extent runs to the outer pixel *edges*: xCoordinates/yCoordinates
        hold pixel centres, so each side is pushed out by half a pixel.

    Sampling every edge rather than just the four corners matters because the
    projected -> geographic transform is not affine: straight edges in the
    product CRS are curved in lon/lat, so a 4-corner box understates the
    footprint.

    Two deliberate departures from the source granule's own polygon, which is
    inherited verbatim from the RSLC and so describes the radar swath:

      * this polygon describes the *subset window* (an axis-aligned rectangle
        on the geocoded grid), not the granule's swath perimeter;
      * the spec's "first point corresponds to the start-time, near-range radar
        coordinate" cannot be honoured without orbit/Doppler/DEM and rdr2geo,
        so the ring starts at the map upper-left, per the geocoded-grid routine.

    *height* is written for every vertex; 0.0 matches the zero-height DEM isce3
    falls back to when no DEM is supplied.  The source carries real terrain
    heights because its polygon was built against one; the GSLC stores no
    terrain elevation to sample.
    """
    import numpy as np
    from rasterio.warp import transform as _warp_transform

    x0 = float(x_centers[0]) - dx / 2.0
    x1 = float(x_centers[-1]) + dx / 2.0
    y0 = float(y_centers[0]) - dy / 2.0
    y1 = float(y_centers[-1]) + dy / 2.0

    xs = np.linspace(x0, x1, pts_per_edge)
    ys = np.linspace(y0, y1, pts_per_edge)

    ring = [(xs[0], y) for y in ys]                 # left edge,   top -> bottom
    ring += [(x, ys[-1]) for x in xs[1:]]           # bottom edge, left -> right
    ring += [(xs[-1], y) for y in ys[:-1][::-1]]    # right edge,  bottom -> top
    ring += [(x, ys[0]) for x in xs[1:-1][::-1]]    # top edge,    right -> left
    ring.append(ring[0])                            # close the ring

    lons, lats = _warp_transform(crs_str, "EPSG:4326",
                                 [p[0] for p in ring], [p[1] for p in ring])

    # Enforce counter-clockwise winding.  isce3 uses shapely's
    # LinearRing.is_ccw because the projection need not preserve orientation;
    # the shoelace sign is the same test without the extra dependency.
    area2 = sum(lons[i] * lats[i + 1] - lons[i + 1] * lats[i]
                for i in range(len(lons) - 1))
    if area2 < 0:
        lons, lats = lons[::-1], lats[::-1]

    return ("POLYGON ((" +
            ", ".join(f"{lon:.8f} {lat:.8f} {height:.4f}"
                      for lon, lat in zip(lons, lats)) +
            "))")


def _read_slices_worker(payload):
    """Read a list of ``(path, slice_tuple)`` from *url* in a fresh process.

    Module-level and self-contained so it can be pickled to a spawned worker:
    the worker re-authenticates and opens its own handle rather than inheriting
    any HDF5 or fsspec state, which is not fork-safe.
    """
    url, auth_config, items = payload

    from openseppo.nisar.nisar_tools import (
        create_s3_fs, _earthaccess_login, open_h5_lazy)

    fs = None
    if url.startswith(("s3://", "https://")):
        if (auth_config or {}).get("use_earthdata"):
            _earthaccess_login(verbose=False)
        if url.startswith("s3://"):
            fs = create_s3_fs(auth_config or {})

    fh = open_h5_lazy(url, fs)
    try:
        return {path: (fh[path][()] if sl is None else fh[path][sl])
                for path, sl in items}
    finally:
        fh.close()


def parallel_read_datasets(url, auth_config, worklist, workers=8,
                           verbose=False):
    """Read many dataset slices concurrently; return ``{path: ndarray}``.

    Returns None if the read could not be parallelised, so callers fall back to
    their serial path rather than failing.

    Concurrency has to be process-based: h5py serialises every HDF5 call behind
    a global lock, so threads would not overlap the network waits that dominate
    here.  Measured in-region on a 12.5 GB GSLC, reading three metadata groups:

        transport   serial    3 workers
        s3          4m02s     2m38s      (per-read time unchanged or better)
        https       50-53s    21-24s     (per-read time unchanged or better)

    The handoff recorded HTTPS concurrency as counterproductive from a laptop
    (each read 2.4-3.1x slower).  That reading does not survive: with N workers
    running at once, a per-read slowdown below Nx is still an aggregate speedup,
    and it is what sharing a link looks like.  Measured end to end on a laptop
    over the public internet, one GSLC h5 subset:

        8 workers  ~5 min        1 worker  ~9 min

    Concurrency wins off-region too, and by more than in-region arithmetic
    predicts, because a single TCP stream cannot fill a long-haul path: that run
    moved 851 MB at ~1.7 MB/s serially against 1070 MB at ~3.6 MB/s with 8
    workers.  The extra 26% of bytes (each worker re-opens the file, so the
    shared group-tree pages are re-read: 851 MB serial -> 986 MB at 3 workers ->
    1070 MB at 8) is paid for several times over by the throughput gain.

    So do not gate this on being in-region, and do not lower the default worker
    count for HTTPS.  Both were tried against the stale note above and both make
    the off-region case markedly slower.

    *worklist* entries are ``(path, slice_tuple_or_None)``.  Only bulk values
    are fetched -- attributes stay with the caller's handle, since object
    headers are consolidated by paged aggregation and are cheap to read, and
    HDF5 object references would not survive pickling anyway.
    """
    if workers <= 1 or len(worklist) <= 1:
        return None

    # Workers are spawned, and spawning re-imports __main__.  In Jupyter, or
    # under `python -`/`-c`, __main__ has no file, so every worker would die
    # noisily before the fallback caught it.  Detect that up front and stay
    # serial instead of spawning processes that cannot survive.
    # Jupyter leaves __file__ unset; `python -` sets it to the literal
    # "<stdin>", so the attribute existing is not enough -- it has to name a
    # real file that a worker can re-import.
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if not main_file or not os.path.isfile(main_file):
        if verbose:
            print("    [INFO] no importable __main__ (interactive session); "
                  "reading metadata serially.", flush=True)
        return None

    import concurrent.futures as cf
    import multiprocessing as mp

    # Round-robin the most expensive datasets first so each worker gets a
    # comparable share (approximates longest-processing-time scheduling).
    ordered = sorted(worklist, key=lambda it: -it[2] if len(it) > 2 else 0)
    ordered = [(p, s) for p, s, *_ in ordered]
    n = min(workers, len(ordered))
    chunks = [ordered[i::n] for i in range(n)]
    chunks = [c for c in chunks if c]

    results = {}
    try:
        ctx = mp.get_context("spawn")
        with cf.ProcessPoolExecutor(max_workers=len(chunks),
                                    mp_context=ctx) as ex:
            futures = [ex.submit(_read_slices_worker, (url, auth_config, c))
                       for c in chunks]
            for fut in cf.as_completed(futures):
                results.update(fut.result())
    except Exception as exc:
        if verbose:
            print(f"    [WARN] parallel metadata read unavailable ({exc}); "
                  f"falling back to serial.", flush=True)
        return None

    return results


def open_h5_lazy(path, s3_fs, block_size=None):
    """
    Lazily open a NISAR HDF5 file with S3-optimized metadata access.
    Caller is responsible for closing the file.

    *block_size* overrides the probed value when given.
    """

    h5_kwargs = {
        "mode": "r",
        "libver": "latest",
        # Disable or shrink chunk cache (often faster for remote reads)
        "rdcc_nbytes": 0,
    }

    probed_block, page_buf = probe_h5_page_params(path, s3_fs)
    if block_size is None:
        block_size = probed_block

    def _open(fobj):
        """Open with the HDF5 page buffer, falling back if it is rejected.

        page_buf_size must be a multiple of the file's page size and requires a
        paged file, so a mis-probe or an older h5py must not break the open.
        """
        if page_buf:
            try:
                return h5py.File(fobj, driver="fileobj",
                                 page_buf_size=page_buf, **h5_kwargs)
            except (TypeError, ValueError, OSError):
                try:
                    fobj.seek(0)
                except Exception:
                    pass
        return h5py.File(fobj, driver="fileobj", **h5_kwargs)

    if path.startswith("s3://"):
        s3_file = s3_fs.open(
            path,
            mode="rb",
            cache_type="blockcache",  # LRU of page-aligned blocks
            block_size=block_size,
        )
        return _open(s3_file)

    if path.startswith("https://"):
        # Same page-aligned blockcache as the S3 branch above.  earthaccess.open
        # returns a file wrapped in fsspec's default 16 MiB BackgroundBlockCache
        # instead, which over-fetches badly on the scattered reads an HDF5
        # subset issues: the ~200 metadata datasets of a NISAR granule sit
        # roughly one per 4 MiB page, so each costs a 16 MiB block plus a
        # speculative next-block prefetch that is then discarded.  Measured on
        # one GSLC h5 subset (206 metadata datasets + a 2772x2259 two-pol
        # window), bytes actually fetched over the wire:
        #
        #   default 16 MiB BackgroundBlockCache   228 requests   3825 MB   98 s
        #   blockcache at 1 page (4 MiB)          234 requests    981 MB   73 s
        #
        # 4x less data for the same work, and faster in-region too, so this is
        # not a bandwidth-constrained-client-only win.  Smaller blocks fetch
        # less still (410 MB at 1 MiB) but split each page read across several
        # requests, which loses on high-latency links and in-region alike.
        #
        # No HDF5 page buffer here: it measured 977 MB against 981 MB without,
        # i.e. nothing, and it would not repay the extra remote open that
        # probing the page size over HTTPS costs.
        fs = _earthaccess_https_fs()
        if fs is not None:
            try:
                return _open(fs.open(path, mode="rb",
                                     cache_type="blockcache",
                                     block_size=block_size))
            except Exception:
                # Auth or range-request behaviour that earthaccess.open handles
                # and a bare session does not: fall back rather than fail.
                pass
        if HAS_EARTHACCESS:
            # Login is expected to have been called once already at the
            # process_chunk_task level; earthaccess caches the session globally.
            file_obj = earthaccess.open([path])[0]
        else:
            import fsspec
            file_obj = fsspec.open(path, "rb").open()
        return h5py.File(file_obj, driver="fileobj", **h5_kwargs)

    return h5py.File(path, **h5_kwargs)


def open_datatree_lazy(path, s3_fs, verbose=False, block_size=16 * 1024 * 1024):
    """
    OPTIMIZED: Open NISAR HDF5 file using datatree for better performance.
    Falls back to h5py if datatree is not available.

    Only used for S3 and local files. HTTPS (earthaccess) is skipped because
    h5netcdf traverses the full HDF5 tree on open, which causes hundreds of
    small HTTP range requests and is much slower than direct h5py access.

    Returns:
        datatree object or None if unavailable
    """
    if not HAS_DATATREE:
        return None

    # Remote files (https://, s3://): tree traversal over HTTP/S3 is too slow even
    # with block caching, because h5netcdf visits every dataset on open and a
    # separate h5py open is still needed for band reading.  h5py with a single
    # lazy file object is faster end-to-end for all remote paths.
    if path.startswith("https://") or path.startswith("s3://"):
        return None

    # Local files only -- open directly by path
    try:
        try:
            dt = open_datatree(path, engine="h5netcdf", phony_dims="sort", decode_timedelta=False)
        except Exception:
            try:
                dt = open_datatree(path, phony_dims="sort", decode_timedelta=False)
            except Exception:
                dt = open_datatree(path, phony_dims="sort")
        return dt
    except Exception as e:
        if verbose:
            print(f"Warning: datatree opening failed, falling back to h5py: {e}")
        return None


def read_variables_datatree(dt, grid_path, variable_names, row_slice, col_slice):
    """
    OPTIMIZED: Read multiple variables efficiently using datatree.

    Args:
        dt: datatree object
        grid_path: Path to frequency grid (e.g., "/science/LSAR/GCOV/grids/frequencyA")
        variable_names: List of variables to read (e.g., ["HHHH", "HVHV"])
        row_slice: slice object for rows (y-dimension)
        col_slice: slice object for columns (x-dimension)

    Returns:
        list of numpy arrays (one per variable)
    """
    # Navigate to the frequency dataset in the datatree
    # Try multiple path formats as datatree structure can vary
    freq_path = grid_path.replace("/science/LSAR/GCOV/grids/", "science/LSAR/GCOV/grids/")

    # Try different path separators
    possible_paths = [
        freq_path,
        freq_path.replace("/", "."),  # Try dot notation
        grid_path,  # Try original path with leading slash
        grid_path.lstrip("/")  # Try without leading slash
    ]

    ds = None
    for path in possible_paths:
        try:
            if path in dt:
                ds = dt[path].ds
                break
            # Also try using subtree navigation
            parts = path.strip("/").split("/")
            node = dt
            for part in parts:
                if hasattr(node, 'children') and part in node.children:
                    node = node[part]
            if hasattr(node, 'ds') and node.ds is not None:
                ds = node.ds
                break
        except Exception:
            continue

    if ds is None:
        # Debug: show available paths
        available_paths = list(dt.subtree) if hasattr(dt, 'subtree') else str(dt)
        raise ValueError(f"Could not find dataset in datatree. Tried: {possible_paths[:2]}. Available: {available_paths}")

    # Read all variables efficiently (datatree can optimize this)
    bands_data = []
    for var in variable_names:
        if var not in ds:
            raise ValueError(f"Variable {var} not found in dataset. Available: {list(ds.data_vars.keys())[:10]}")

        # Use xarray's intelligent indexing - this is lazy until .values
        # The dimension names in NISAR are typically phony_dim_0 (y) and phony_dim_1 (x)
        data_var = ds[var]

        # Get dimension names (they vary depending on how h5netcdf reads the file)
        dims = data_var.dims
        if len(dims) >= 2:
            # Usually the last two dimensions are y, x
            y_dim = dims[-2]
            x_dim = dims[-1]

            # Apply spatial subsetting
            subset = data_var.isel({y_dim: row_slice, x_dim: col_slice})

            # Load into memory as float32
            bands_data.append(subset.values.astype(np.float32))
        else:
            raise ValueError(f"Variable {var} has unexpected dimensions: {dims}")

    return bands_data


def inspect_h5_structure(f):
    """
    Scans H5: returns frequency dict with CRS, Resolution, Variables, Exact Footprint, and Dimensions.
    """
    structure = {}
    base_path = "/science/LSAR/GCOV/grids"

    ident_path = "/science/LSAR/identification/boundingPolygon"
    poly_geo_display = "Not Found"
    cached_geo_corners = []

    if ident_path in f:
        try:
            val = f[ident_path][()]
            if hasattr(val, "decode"):
                val = val.decode("utf-8")
            raw_wkt = str(val).strip()

            matches = re.findall(r"([\d\.\-]+)\s+([\d\.\-]+)(?:\s+[\d\.\-]+)?", raw_wkt)

            if matches:
                points = [(float(m[0]), float(m[1])) for m in matches]

                # Extract 4 Corners using Sum/Diff Heuristic
                sums = [p[0] + p[1] for p in points]
                diffs = [p[0] - p[1] for p in points]

                idx_ne = np.argmax(sums)
                idx_sw = np.argmin(sums)
                idx_se = np.argmax(diffs)
                idx_nw = np.argmin(diffs)

                ordered_indices = [idx_nw, idx_ne, idx_se, idx_sw]
                cached_geo_corners = [points[i] for i in ordered_indices]

                poly_geo_display = ", ".join([f"({p[0]:.4f}, {p[1]:.4f})" for p in cached_geo_corners])
            else:
                poly_geo_display = "Could not parse WKT"

        except Exception as e:
            poly_geo_display = f"Metadata Error: {e}"

    if base_path not in f:
        return {"error": f"Path {base_path} not found in H5."}

    for freq_key in f[base_path].keys():
        if not freq_key.startswith("frequency"):
            continue
        freq_code = freq_key.replace("frequency", "")
        g_path = f"{base_path}/{freq_key}"

        try:
            proj_val = f[f"{g_path}/projection"][()]
            if hasattr(proj_val, "decode"):
                crs = _ensure_utm_south(proj_val.decode(), f)
            else:
                crs = f"EPSG:{proj_val}"

            x_coord_ds = f[f"{g_path}/xCoordinates"]
            y_coord_ds = f[f"{g_path}/yCoordinates"]
            x0, x1 = x_coord_ds[0], x_coord_ds[-1]
            y0, y1 = y_coord_ds[0], y_coord_ds[-1]
            res_x = x_coord_ds[1] - x_coord_ds[0]
            res_y = y_coord_ds[1] - y_coord_ds[0]
            ncols = len(x_coord_ds)
            nrows = len(y_coord_ds)

            min_x = min(x0, x1) - (abs(res_x) / 2.0)
            max_x = max(x0, x1) + (abs(res_x) / 2.0)
            min_y = min(y0, y1) - (abs(res_y) / 2.0)
            max_y = max(y0, y1) + (abs(res_y) / 2.0)

            poly_native_display = "No footprint found"
            dims_km = "N/A"

            if cached_geo_corners:
                try:
                    lons = [p[0] for p in cached_geo_corners]
                    lats = [p[1] for p in cached_geo_corners]

                    # Transform to native
                    xs, ys = transform("EPSG:4326", crs, lons, lats)

                    poly_native_display = ", ".join([f"({x:.2f}, {y:.2f})" for x, y in zip(xs, ys)])

                    # Calculate Dimensions (Euclidean distance in Native CRS meters)
                    # Ordered: 0=NW, 1=NE, 2=SE, 3=SW
                    # Width (Range) ~ Top edge (0-1) and Bottom edge (3-2)
                    w1 = math.sqrt((xs[1] - xs[0]) ** 2 + (ys[1] - ys[0]) ** 2)
                    w2 = math.sqrt((xs[2] - xs[3]) ** 2 + (ys[2] - ys[3]) ** 2)
                    avg_width_km = ((w1 + w2) / 2.0) / 1000.0

                    # Height (Azimuth) ~ Left edge (0-3) and Right edge (1-2)
                    h1 = math.sqrt((xs[3] - xs[0]) ** 2 + (ys[3] - ys[0]) ** 2)
                    h2 = math.sqrt((xs[2] - xs[1]) ** 2 + (ys[2] - ys[1]) ** 2)
                    avg_height_km = ((h1 + h2) / 2.0) / 1000.0

                    # Detect degenerate polygon (fewer than 4 unique corners)
                    unique_corners = len({(round(x, 1), round(y, 1)) for x, y in zip(xs, ys)})
                    if unique_corners < 4:
                        raster_w_km = (max_x - min_x) / 1000.0
                        raster_h_km = (max_y - min_y) / 1000.0
                        dims_km = (f"~{raster_w_km:.2f} km x ~{raster_h_km:.2f} km"
                                   f" (footprint polygon incomplete; showing raster extent)")
                    else:
                        dims_km = f"Width: {avg_width_km:.2f} km, Height: {avg_height_km:.2f} km"

                except Exception as e:
                    poly_native_display = f"Reprojection Failed: {e}"

            variables = []
            var_details = {}  # name -> {"dtype": str, "nodata": str}
            for item in f[g_path].keys():
                if item not in ["projection", "xCoordinates", "yCoordinates", "listOfPolarizations", "covarianceMatrixDiagonal", "covarianceMatrixOffDiagonal"]:
                    obj = f[f"{g_path}/{item}"]
                    if isinstance(obj, h5py.Dataset) and len(obj.shape) >= 2:
                        variables.append(item)
                        _dt = str(obj.dtype)
                        # Prefer explicit _FillValue attribute over h5py's
                        # .fillvalue (which returns the HDF5 dtype default).
                        if "_FillValue" in obj.attrs:
                            _fv = obj.attrs["_FillValue"]
                            _fv_str = str(_fv)
                        else:
                            _fv_str = "none"
                        var_details[item] = {"dtype": _dt, "nodata": _fv_str}

            structure[freq_code] = {"crs": crs, "ncols": ncols, "nrows": nrows, "res_x": float(res_x), "res_y": float(res_y), "bbox": (min_x, min_y, max_x, max_y), "vars": sorted(variables), "var_details": var_details, "poly_geo": poly_geo_display, "poly_native": poly_native_display, "dims": dims_km}
        except Exception as e:
            structure[freq_code] = {"error": str(e)}

    return structure


def get_grid_info(h5_handle, frequency="A"):
    grid_path = f"/science/LSAR/GCOV/grids/frequency{frequency}"
    try:
        x_ds = h5_handle[f"{grid_path}/xCoordinates"]
        y_ds = h5_handle[f"{grid_path}/yCoordinates"]
        proj_val = h5_handle[f"{grid_path}/projection"][()]
    except KeyError:
        raise KeyError(f"Grid path '{grid_path}' not found.")
    if hasattr(proj_val, "decode"):
        projection = _ensure_utm_south(proj_val.decode(), h5_handle)
    else:
        projection = f"EPSG:{proj_val}"
    # NISAR grids are uniformly spaced -- only read the first two values plus
    # dataset shape to get resolution and extent without fetching the full arrays.
    nx = x_ds.shape[0]
    ny = y_ds.shape[0]
    x01 = x_ds[0:2]
    y01 = y_ds[0:2]
    res_x = float(x01[1] - x01[0])
    res_y = float(y01[1] - y01[0])
    x0, y0 = float(x01[0]), float(y01[0])
    x_coords = np.arange(nx, dtype=np.float64) * res_x + x0
    y_coords = np.arange(ny, dtype=np.float64) * res_y + y0
    return {"x": x_coords, "y": y_coords, "res_x": res_x, "res_y": res_y, "crs": projection, "grid_path": grid_path, "freq": frequency}


def get_grid_info_from_datatree(dt, frequency="A"):
    """Read grid metadata from an open datatree, avoiding a second S3 file open."""
    grid_path = f"science/LSAR/GCOV/grids/frequency{frequency}"
    try:
        node = dt[grid_path]
        ds = node.ds
        x_coords = ds["xCoordinates"].values
        y_coords = ds["yCoordinates"].values
        proj_val = ds["projection"].values
        if hasattr(proj_val, "decode"):
            raw_proj = proj_val.decode()
            # Check bounding polygon from identification node for +south fix
            try:
                ident_ds = dt["science/LSAR/identification"].ds
                bp = str(ident_ds["boundingPolygon"].values)
                matches = re.findall(r"([-\d.]+)\s+([-\d.]+)", bp)
                avg_lat = sum(float(lat) for _, lat in matches) / len(matches) if matches else 0.0
            except Exception:
                avg_lat = 0.0
            if "+proj=utm" in raw_proj.lower() and "+south" not in raw_proj.lower() and avg_lat < 0:
                raw_proj = raw_proj + " +south"
            projection = raw_proj
        else:
            projection = f"EPSG:{int(proj_val)}"
        full_grid_path = f"/{grid_path}"
        return {"x": x_coords, "y": y_coords, "res_x": x_coords[1] - x_coords[0], "res_y": y_coords[1] - y_coords[0], "crs": projection, "grid_path": full_grid_path, "freq": frequency}
    except Exception:
        return None


def _decode_h5_scalar(val):
    """Decode an HDF5 / xarray scalar to a clean Python str.

    Handles np.bytes_, bytes, numpy 0-d arrays, and plain strings.
    """
    # Unwrap 0-d numpy arrays (from xarray .values on scalar datasets)
    if isinstance(val, np.ndarray) and val.ndim == 0:
        val = val.item()
    if isinstance(val, (bytes, np.bytes_)):
        return val.decode()
    return str(val)


def get_acquisition_metadata_from_datatree(dt):
    """Read acquisition metadata from an open datatree, avoiding a second S3 file open."""
    try:
        node = dt["science/LSAR/identification"]
        ds = node.ds
        t_start = _decode_h5_scalar(ds["zeroDopplerStartTime"].values)
        meta = {}
        if "T" in t_start:
            date_part, time_part = t_start.split("T")
            meta["ACQUISITION_DATE"] = date_part
            meta["ACQUISITION_TIME"] = time_part
        else:
            meta["ACQUISITION_DATETIME"] = t_start
        # CRID
        try:
            meta["CRID"] = _decode_h5_scalar(ds["compositeReleaseID"].values)
        except Exception:
            pass
        # ISCE3 / software version
        try:
            sw = dt["science/LSAR/GCOV/metadata/processingInformation/algorithms"].ds
            meta["ISCE3_VERSION"] = _decode_h5_scalar(sw["softwareVersion"].values)
        except Exception:
            pass
        return meta
    except Exception:
        return None


def get_acquisition_metadata(h5_handle):
    try:
        t_start = _decode_h5_scalar(
            h5_handle["/science/LSAR/identification/zeroDopplerStartTime"][()]
        )
        meta = {}
        if "T" in t_start:
            date_part, time_part = t_start.split("T")
            meta["ACQUISITION_DATE"] = date_part
            meta["ACQUISITION_TIME"] = time_part
        else:
            meta["ACQUISITION_DATETIME"] = t_start
        # CRID
        try:
            meta["CRID"] = _decode_h5_scalar(
                h5_handle["/science/LSAR/identification/compositeReleaseID"][()]
            )
        except Exception:
            pass
        # ISCE3 / software version
        try:
            meta["ISCE3_VERSION"] = _decode_h5_scalar(
                h5_handle["/science/LSAR/GCOV/metadata/processingInformation/algorithms/softwareVersion"][()]
            )
        except Exception:
            pass
        return meta
    except Exception:
        return {}


# =========================================================
# 5. MATH (Float32 and UInt8)
# =========================================================


def power_to_db_float32(data):
    valid_mask = np.isfinite(data) & (data > 0)
    out = np.full(data.shape, np.nan, dtype=np.float32)
    np.log10(data, out=out, where=valid_mask)
    out[valid_mask] = out[valid_mask] * 10.0

    # Edges
    out[np.isneginf(out)] = -40.0
    out[out < -40.0] = -40.0

    out[np.isposinf(out)] = 13.3
    out[out > 13.3] = 13.3

    return out


def power_to_dn_uint8(data, min_db=-31.0, max_db=7.1, min_dn=1, max_dn=255):
    """Convert linear power to uint8 DN with a linear dB scale.

    Default mapping:  dB = -31.15 + DN * 0.15
      (equivalent to min_db=-31.0, max_db=7.1, min_dn=1, max_dn=255)

    DN=0 is reserved for nodata.
    Values below min_db clamp to min_dn; above max_db clamp to max_dn.
    """
    # Pre-compute scalar coefficients: raw_dn = db * scale + offset
    scale = (max_dn - min_dn) / (max_db - min_db)
    offset = min_dn - min_db * scale

    is_neg_inf = np.isneginf(data)
    is_pos_inf = np.isposinf(data)
    valid_mask = np.isfinite(data) & (data > 0)

    db = np.full(data.shape, np.nan, dtype=np.float32)
    np.log10(data, out=db, where=valid_mask)
    db[valid_mask] *= 10.0

    dn = np.zeros(data.shape, dtype=np.uint8)  # 0 = nodata

    raw_dn = db * scale + offset

    mask_range = valid_mask & (raw_dn >= min_dn) & (raw_dn <= max_dn)
    dn[mask_range] = np.round(raw_dn[mask_range]).astype(np.uint8)

    mask_low = valid_mask & (raw_dn < min_dn)
    dn[mask_low] = min_dn
    dn[is_neg_inf] = min_dn

    mask_high = valid_mask & (raw_dn > max_dn)
    dn[mask_high] = max_dn
    dn[is_pos_inf] = max_dn

    return dn


def pwr_to_amp(pwr, scale_factor=10**8.3):
    is_neg_inf = np.isneginf(pwr)
    is_pos_inf = np.isposinf(pwr)
    valid_mask = np.isfinite(pwr) & (pwr > 0)
    dn = np.zeros(pwr.shape, dtype=np.float32)
    dn[valid_mask] = np.sqrt(pwr[valid_mask] * scale_factor)
    dn[valid_mask & (dn < 1.0)] = 1.0
    dn[is_neg_inf] = 1.0
    dn[is_pos_inf] = 65535.0
    dn[dn > 65535] = 65535
    return dn.astype(np.uint16)


# =========================================================
# 6. CORE PROCESSOR
# =========================================================


def _process_single_file(h5_url, variable_names, output_dir_or_file, srcwin, projwin, transform_mode, frequency, single_bands, vrt, downscale_factor, target_align_pixels, input_fs, output_fs, is_batch=False, cache=None, keep=False, use_earthdata=False, verbose=False, target_srs=None, target_res=None, resample="cubic", output_format="COG", fill_holes=False, num_threads=None, read_threads=8, dualpol_ratio=False, sigma0=False, projwin_srs=None, apply_mask=True, all_frequencies=False, h5_ancillary_floats=True):

    # One frequency unless the caller asked for all of them (-of h5 only, cf.
    # the RSLC subsetter's --all_freq).  The caller resolves an unset -f to the
    # frequency the granule actually carries; "A" is the last-resort default.
    frequency = frequency or "A"

    h5_basename = h5_url.split("/")[-1]
    base_name = h5_basename[:-3] if h5_basename.lower().endswith(".h5") else h5_basename

    # --- Mode Setup ---
    # mode_str is used for the FILENAME and matches exact user input (e.g. "Amp")
    # logic_mode is used for the IF statement and handles case-insensitivity
    mode_str = transform_mode if transform_mode else "pwr"
    logic_mode = mode_str.lower()

    # Detect quad-pol from the polarization code in the filename (token index 9).
    # For frequency A the pol-code occupies the first two characters (e.g. "QP" in "QPQP");
    # for frequency B it occupies the last two characters.
    _base_tokens = base_name.split("_")
    _pol_code = _base_tokens[9] if len(_base_tokens) > 9 else ""
    _is_qp = (_pol_code[:2] == "QP" if frequency == "A" else _pol_code[2:] == "QP")

    # Dual-pol ratio setup: validate pol mode and determine numerator/denominator variables.
    _ratio_pol_override = None  # EBD pol string to use in output filenames (e.g. "hhhvra")
    _ratio_band_name = None     # Band description written into TIF metadata (e.g. "HHHH/HVHV")
    _ratio_num_var = None
    _ratio_den_var = None
    if dualpol_ratio:
        _pol_freq_code = _pol_code[:2] if frequency == "A" else _pol_code[2:]
        if _pol_freq_code == "DH":
            _ratio_num_var, _ratio_den_var = "HHHH", "HVHV"
            _ratio_pol_override = "hhhvra"
            _ratio_band_name = "HHHH/HVHV"
        elif _pol_freq_code == "DV":
            _ratio_num_var, _ratio_den_var = "VVVV", "VHVH"
            _ratio_pol_override = "vvvhra"
            _ratio_band_name = "VVVV/VHVH"
        else:
            print(f"Warning: --dualpol_ratio requires DH or DV polarization mode, got '{_pol_code}'. Skipping {h5_basename}.", file=sys.stderr)
            return {"success": False, "h5_url": h5_url, "error": f"dualpol_ratio requires DH or DV mode, got {_pol_code}"}

    if verbose:
        print(f"--> Processing File: {h5_basename}", flush=True)

    if is_batch:
        final_path = f"{output_dir_or_file.rstrip('/')}/{base_name}.tif"
    else:
        if output_dir_or_file.endswith("/") or not output_dir_or_file.lower().endswith(".tif"):
            final_path = f"{output_dir_or_file.rstrip('/')}/{base_name}.tif"
        else:
            final_path = output_dir_or_file

    f = None
    dt = None
    cached_file_path = None  # Track cached file for immediate cleanup
    if verbose:
        import time as _time
        _t_file = _time.perf_counter()
    try:
        if cache is not None:
            file_url = cache_to_local(h5_url, localdir=cache, keep=keep, use_earthdata=use_earthdata, fs=input_fs)
            # Track the cached file so we can remove it immediately after processing
            if not keep:
                cached_file_path = file_url
        else:
            file_url = h5_url

        # Try datatree first; if it succeeds read metadata from it (one S3 open).
        # Only fall back to h5py when datatree is unavailable or metadata read fails.
        dt = open_datatree_lazy(file_url, input_fs, verbose=verbose)
        use_datatree = (dt is not None)

        info = None
        acq_meta = None
        if use_datatree:
            info = get_grid_info_from_datatree(dt, frequency=frequency)
            acq_meta = get_acquisition_metadata_from_datatree(dt)

        # Always open an h5py handle: needed for band reading regardless of
        # whether datatree succeeded for metadata (local files only use datatree).
        f = open_h5_lazy(file_url, input_fs)
        if info is None:
            info = get_grid_info(f, frequency=frequency)
        if acq_meta is None:
            acq_meta = get_acquisition_metadata(f)

        # Inject processing metadata into the tag dict.
        # RADIOMETRY and DB_FORMULA are added per-band (backscatter only).
        _radiometry = "sigma0" if sigma0 else "gamma0"
        _db_formulas = {
            "pwr": "dB = 10*log10(DN)",
            "db":  "dB = DN",
            "amp": "dB = 20*log10(DN) - 83",
            "dn":  "dB = -31.15 + DN*0.15",
        }
        _db_formula = _db_formulas.get(logic_mode, "")
        try:
            from importlib.metadata import version as _pkg_version
            acq_meta["OPENSEPPO_VERSION"] = _pkg_version("openseppo")
        except Exception:
            acq_meta["OPENSEPPO_VERSION"] = "unknown"

        if verbose:
            print(f"    [t] file open + metadata: {_time.perf_counter()-_t_file:.1f}s", flush=True)
            _t_file = _time.perf_counter()

        date_str = acq_meta.get("ACQUISITION_DATE", "Unknown")

        if verbose:
            mode_label = "datatree" if use_datatree else "h5py"
            print(f"    Date: {date_str} | Grid: {info['res_x']:.1f}m ({frequency}) | Mode: {mode_label}", flush=True)

        # --- REPROJECTION SETUP ---
        input_crs_obj = _parse_crs(info["crs"])
        dst_crs_obj = _parse_crs(target_srs) if target_srs else input_crs_obj
        crs_changes = (dst_crs_obj != input_crs_obj)

        # Convert projwin from projwin_srs to the CRS expected by downstream code.
        # With t_srs: convert to target_srs (existing calculate_source_window then expands to native).
        # Without t_srs: convert directly to native CRS.
        if projwin_srs and projwin:
            _dst_srs = target_srs if crs_changes else info["crs"]
            projwin = reproject_projwin(projwin, projwin_srs, _dst_srs)
            if verbose:
                print(f"    projwin_srs {projwin_srs} -> {_dst_srs}: {projwin}", flush=True)

        # --- AUTO PRE-DOWNSCALE from -tr (only when -d not explicitly set) ---
        # For same-CRS exact integer multiples: pure block averaging, no warp.
        # For same-CRS fractional multiples or reprojection: pick the largest
        # integer pre-downscale that still leaves enough oversampling for the
        # chosen resample kernel, then do a small warp step for the remainder.
        if target_res is not None and downscale_factor is None:
            _tr_x = abs(target_res[0])
            _tr_y = abs(target_res[1])
            _nat_x = abs(info["res_x"])
            _nat_y = abs(info["res_y"])
            _divisor = _RESAMPLE_PREDOWNSCALE_DIVISOR.get((resample or "cubic").lower(), 2)

            if not crs_changes:
                _ratio_x = _tr_x / _nat_x
                _ratio_y = _tr_y / _nat_y
                _ix = int(round(_ratio_x))
                _iy = int(round(_ratio_y))
                if (_ix == _iy and _ix > 1
                        and math.isclose(_ratio_x, _ix, rel_tol=1e-4)
                        and math.isclose(_ratio_y, _iy, rel_tol=1e-4)):
                    # Exact integer multiple: pure block average, no warp needed
                    downscale_factor = _ix
                    if verbose:
                        print(f"    Auto pre-downscale: {_ix}x block average "
                              f"({_nat_x:.4g} -> {_tr_x:.4g}, no warp needed)", flush=True)
                else:
                    _ratio = min(_ratio_x, _ratio_y)
                    _auto_d = max(1, int(math.floor(_ratio / _divisor)))
                    if _auto_d > 1:
                        downscale_factor = _auto_d
                        if verbose:
                            print(f"    Auto pre-downscale: {_auto_d}x before resampling "
                                  f"({_nat_x:.4g} -> {_nat_x * _auto_d:.4g}, "
                                  f"then warp to {_tr_x:.4g})", flush=True)
            else:
                # Reprojection case: estimate natural output pixel size via a
                # preliminary calculate_default_transform on the full raster extent.
                try:
                    _nx, _ny = len(info["x"]), len(info["y"])
                    _ulx = info["x"][0] - _nat_x / 2
                    _uly = info["y"][0] + _nat_y / 2
                    _pdt, _, _ = calculate_default_transform(
                        input_crs_obj, dst_crs_obj, _nx, _ny,
                        left=_ulx, bottom=_uly - _ny * _nat_y,
                        right=_ulx + _nx * _nat_x, top=_uly,
                    )
                    _ratio_x = _tr_x / abs(_pdt.a)
                    _ratio_y = _tr_y / abs(_pdt.e)
                    _ratio = min(_ratio_x, _ratio_y)
                    _auto_d = max(1, int(math.floor(_ratio / _divisor)))
                    if _auto_d > 1:
                        downscale_factor = _auto_d
                        if verbose:
                            print(f"    Auto pre-downscale: {_auto_d}x before reprojection "
                                  f"({_nat_x:.4g} -> {_nat_x * _auto_d:.4g} native, "
                                  f"target {_tr_x:.4g})", flush=True)
                except Exception:
                    pass  # fall back to no pre-downscale

        # Determine whether a warp is needed due to an explicit target resolution.
        # Skip if the effective resolution (after downscaling) already matches -tr.
        needs_resample = False
        if target_res is not None and not crs_changes:
            df = downscale_factor if (downscale_factor and downscale_factor > 1) else 1
            eff_res_x = abs(info["res_x"]) * df
            eff_res_y = abs(info["res_y"]) * df
            already_matches = (
                math.isclose(eff_res_x, abs(target_res[0]), rel_tol=1e-6) and
                math.isclose(eff_res_y, abs(target_res[1]), rel_tol=1e-6)
            )
            needs_resample = not already_matches

        needs_reproject = crs_changes or needs_resample
        resample_enum = _get_resampling(resample)
        if crs_changes and needs_reproject and verbose:
            src_epsg = input_crs_obj.to_epsg()
            src_label = f"EPSG:{src_epsg}" if src_epsg else info["crs"][:40]
            print(f"    Reprojecting: {src_label} -> {target_srs} (resample={resample})", flush=True)

        # When reprojecting with a projwin, the projwin coords are in target_srs.
        # Expand to native-CRS subset using calculate_source_window.
        # Only needed when the CRS actually changes; for same-CRS resampling the
        # projwin is already in native units.
        native_projwin = projwin
        if crs_changes and projwin:
            ulx_t, uly_t, lrx_t, lry_t = projwin
            n_left, n_bottom, n_right, n_top = calculate_source_window(
                target_bounds=(ulx_t, lry_t, lrx_t, uly_t),
                target_crs=target_srs,
                source_crs=info["crs"],
                source_res=(abs(info["res_x"]), abs(info["res_y"])),
            )
            native_projwin = [n_left, n_top, n_right, n_bottom]
            if verbose:
                print(f"    Reprojection: expanded native projwin {native_projwin}", flush=True)

        if srcwin:
            col, row, w, h = srcwin
            if verbose:
                print(f"    Slice (Pixels): col={col}, row={row}, w={w}, h={h}", flush=True)
        elif native_projwin and native_projwin is not projwin:
            col, row, w, h = get_indices_from_extent(info["x"], info["y"], native_projwin)
            if verbose:
                print(f"    Slice (Map native): {native_projwin} -> Pixels: {col},{row},{w},{h}", flush=True)
        elif projwin:
            col, row, w, h = get_indices_from_extent(info["x"], info["y"], projwin)
            if verbose:
                print(f"    Slice (Map): {projwin} -> Pixels: {col},{row},{w},{h}", flush=True)
        else:
            col, row, w, h = 0, 0, len(info["x"]), len(info["y"])
            if verbose:
                print(f"    Full extent (pixels): w={w}, h={h}", flush=True)

        if target_align_pixels and downscale_factor and downscale_factor > 1:
            curr_x_center = info["x"][col]
            curr_y_center = info["y"][row]
            dx, dy = info["res_x"], info["res_y"]
            curr_ulx = curr_x_center - (abs(dx) / 2.0)
            curr_uly = curr_y_center + (abs(dy) / 2.0)
            target_span_x = abs(dx) * downscale_factor
            target_span_y = abs(dy) * downscale_factor
            aligned_ulx = np.floor(curr_ulx / target_span_x) * target_span_x
            aligned_uly = np.ceil(curr_uly / target_span_y) * target_span_y
            diff_x = curr_ulx - aligned_ulx
            diff_y = aligned_uly - curr_uly
            shift_x = int(round(diff_x / abs(dx)))
            shift_y = int(round(diff_y / abs(dy)))
            new_col = col - shift_x
            new_row = row - shift_y
            if new_col < 0:
                offset_blocks = int(np.ceil(abs(new_col) / downscale_factor))
                new_col += offset_blocks * downscale_factor
            if new_row < 0:
                offset_blocks = int(np.ceil(abs(new_row) / downscale_factor))
                new_row += offset_blocks * downscale_factor
            if verbose:
                print(f"    Aligning Pixels: ({col},{row}) -> ({new_col},{new_row})", flush=True)
            col, row = new_col, new_row

        max_h, max_w = len(info["y"]), len(info["x"])
        if col < 0:
            col = 0
        if row < 0:
            row = 0
        if col + w > max_w:
            w = max_w - col
        if row + h > max_h:
            h = max_h - row
        if w <= 0 or h <= 0:
            raise ValueError("Resulting slice is empty/out of bounds.")

        # === H5 SUBSET OUTPUT: write raw subset and return early ===
        if output_format.lower() == "h5":
            x_c = info["x"][col]
            y_c = info["y"][row]
            _ulx = x_c - abs(info["res_x"]) / 2.0
            _uly = y_c + abs(info["res_y"]) / 2.0
            _tf = from_origin(_ulx, _uly, abs(info["res_x"]), abs(info["res_y"]))
            if verbose:
                print(f"    Writing H5 subset ({w}x{h}) ...", flush=True)
            h5_bytes, written_freqs = _write_h5_subset(
                f, info["grid_path"], variable_names, col, row, w, h,
                all_frequencies=all_frequencies,
                ancillary_floats=h5_ancillary_floats)

            # Name the file for the frequencies actually written.
            pol_list_str = "".join(v.lower() if _is_qp else v[:2].lower() for v in variable_names)
            suffix = f"-EBD_{''.join(written_freqs)}_{pol_list_str}.h5"
            h5_out_path = (final_path[:-4] if final_path.endswith(".tif") else final_path) + suffix

            def _wb(path, data):
                if path.startswith("s3://"):
                    with output_fs.open(path, "wb") as fout:
                        fout.write(data)
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as fout:
                        fout.write(data)

            _wb(h5_out_path, h5_bytes)
            if verbose:
                print(f"    [OK] H5 subset: {os.path.basename(h5_out_path)}", flush=True)
            files_map_h5 = {var: h5_out_path for var in variable_names}
            return {"success": True, "h5_url": h5_url, "date": date_str,
                    "files_map": files_map_h5,
                    "info": {"w": w, "h": h, "transform": _tf, "crs": info["crs"], "dtype": "float32"}}

        # MEMORY OPTIMIZATION: When using cache with single_bands, process one variable at a time
        # to reduce memory footprint (important for large files on smaller RAM).
        # dualpol_ratio requires both bands in memory simultaneously, so force normal mode.
        # When dualpol_ratio is active, strip ancillary vars to avoid holding
        # them in the full stack -- process ancillary in a separate run.
        if dualpol_ratio:
            _dropped_anc = [v for v in variable_names if _is_ancillary(v)]
            if _dropped_anc:
                variable_names = [v for v in variable_names if not _is_ancillary(v)]
                print(f"    Note: ancillary grids ({', '.join(_dropped_anc)}) skipped with -dpratio. "
                      f"Process them in a separate run without -dpratio.", flush=True)
        use_low_memory_mode = (cache is not None and single_bands) and not dualpol_ratio

        # Build the read list: the gamma->sigma factor (--sigma0) and the subswath
        # mask are appended so they ride the SAME parallel I/O pass (one HDF5 open)
        # as the covariance bands, then extracted by name. In low-memory mode bands
        # are read one at a time, so sigma/mask share a single auxiliary pass there
        # instead (the file is cached locally -- a cheap read).
        _sigma_var = "rtcGammaToSigmaFactor"
        _sigma_already_in_vars = _sigma_var in variable_names

        # --- Subswath mask (default ON for non-h5 output; -nomask disables) ---
        # The GCOV "mask" grid holds the subswath number of each valid sample:
        # 1..254 = valid subswath, 0 = invalid (averaging set not fully focused),
        # 255 = fill (outside acquisition extent). Fill/invalid pixels are nulled on
        # the backscatter at source resolution -- before downscale/warp -- so they
        # never contaminate block-averaging or the resampling kernel. The h5 output
        # path returns earlier, so this only affects COG/GTiff output.
        _mask_var = "mask"
        _do_mask = apply_mask and (f"{info['grid_path']}/{_mask_var}" in f)
        _mask_already_in_vars = _mask_var in variable_names
        if apply_mask and not _do_mask and verbose:
            print("    Note: no 'mask' grid found; backscatter masking skipped.", flush=True)

        _extras = []
        if sigma0 and not _sigma_already_in_vars:
            _extras.append(_sigma_var)
        if _do_mask and not _mask_already_in_vars:
            _extras.append(_mask_var)
        _read_vars = list(variable_names) + _extras

        _mask_invalid = None  # computed once below, then applied to backscatter

        def _invalid_from_mask(arr):
            # Valid only where mask is a subswath number (1..254); 0/255/NaN -> invalid
            inv = ~((arr >= 1) & (arr <= 254))
            if verbose:
                _n = int(inv.sum())
                print(f"    Masking: {_n} fill/invalid px ({100.0 * _n / inv.size:.1f}%) "
                      f"set to nodata on backscatter", flush=True)
            return inv

        if use_low_memory_mode:
            if verbose:
                print(f"    Low-memory mode: Processing {len(variable_names)} bands individually...", flush=True)
            # Process each band separately to minimize memory usage
            bands_data = None  # Don't load all bands at once
            data_stack = None  # Will process one at a time

            # Pre-read the sigma factor and mask once (each band reuses them) in a
            # single auxiliary pass.  The file is already cached locally in
            # low-memory mode, so this is a cheap local read.
            _aux_vars = ([_sigma_var] if sigma0 else []) + ([_mask_var] if _do_mask else [])
            _sigma_data = None
            if _aux_vars:
                _aux = dict(zip(_aux_vars, _read_bands_parallel(
                    file_url, input_fs, info["grid_path"],
                    _aux_vars, row, h, col, w, n_workers=read_threads,
                )))
                if sigma0:
                    _sigma_data = _aux[_sigma_var].astype(np.float32)
                    if verbose:
                        print(f"    Read rtcGammaToSigmaFactor for sigma0 conversion", flush=True)
                if _do_mask:
                    _mask_invalid = _invalid_from_mask(_aux[_mask_var])
        else:
            if verbose:
                print(f"    Extracting {len(_read_vars)} bands...", flush=True)
                _t_read = _time.perf_counter()

            # For local files use datatree if available; for remote paths use
            # parallel h5py reads (each bandxstripe gets its own file handle and
            # S3 connection, giving Nxk concurrent range requests).
            _is_remote = file_url.startswith("s3://") or file_url.startswith("https://")
            if use_datatree and not _is_remote:
                try:
                    row_slice = slice(row, row + h)
                    col_slice = slice(col, col + w)
                    bands_data = read_variables_datatree(dt, info["grid_path"], _read_vars, row_slice, col_slice)
                    if verbose:
                        print(f"    [OK] Used datatree for band extraction", flush=True)
                except Exception as e:
                    if verbose:
                        print(f"    [!] Datatree read failed, falling back to parallel h5py: {e}", flush=True)
                    bands_data = _read_bands_parallel(file_url, input_fs, info["grid_path"], _read_vars, row, h, col, w, n_workers=read_threads)
            else:
                bands_data = _read_bands_parallel(file_url, input_fs, info["grid_path"], _read_vars, row, h, col, w, n_workers=read_threads)

            # Take magnitude of off-diagonal (complex) GCOV elements before stacking
            # to avoid dtype promotion of the whole stack to complex.
            bands_data = [np.abs(b).astype(np.float32) if np.iscomplexobj(b) else b for b in bands_data]

            # Map each result to its variable name -- robust to ordering and to the
            # appended sigma/mask extras.
            _by_name = dict(zip(_read_vars, bands_data))
            del bands_data

            # Subswath mask read in the same pass; used to null backscatter below.
            if _do_mask:
                _mask_invalid = _invalid_from_mask(_by_name[_mask_var])

            # gamma0 -> sigma0: multiply each backscatter band by the factor.
            # rtcGammaToSigmaFactor is ancillary, so it is left untouched (and kept
            # as an output band only if the user requested it in --vars).
            if sigma0:
                _sigma_data = _by_name[_sigma_var]
                for _v in variable_names:
                    if not _is_ancillary(_v):
                        _by_name[_v] = _by_name[_v] * _sigma_data
                if verbose:
                    print(f"    Applied rtcGammaToSigmaFactor (gamma0 -> sigma0)", flush=True)

            # Stack only the requested output bands (appended extras are dropped).
            data_stack = np.stack([_by_name[_v] for _v in variable_names])
            del _by_name
            gc.collect()

            # Apply subswath mask to backscatter bands (source resolution).
            # data_stack rows align 1:1 with variable_names here.
            if _mask_invalid is not None:
                for _mi, _mv in enumerate(variable_names):
                    if not _is_ancillary(_mv):
                        data_stack[_mi][_mask_invalid] = np.nan

            if verbose:
                shape = data_stack.shape
                mb = data_stack.nbytes / 1e6
                print(f"    [t] data read ({shape[0]}x{shape[1]}x{shape[2]}, {mb:.1f} MB): {_time.perf_counter()-_t_read:.1f}s", flush=True)
                _t_file = _time.perf_counter()

        x_center = info["x"][col]
        y_center = info["y"][row]
        orig_res_x, orig_res_y = info["res_x"], info["res_y"]
        ulx = x_center - (abs(orig_res_x) / 2.0)
        uly = y_center + (abs(orig_res_y) / 2.0)

        # Calculate output resolution
        if downscale_factor and downscale_factor > 1:
            out_res_x = orig_res_x * downscale_factor
            out_res_y = orig_res_y * downscale_factor
            if verbose:
                print(f"    Effective resolution after downscale: {abs(out_res_x):.2f} x {abs(out_res_y):.2f}", flush=True)
        else:
            out_res_x = orig_res_x
            out_res_y = orig_res_y
        if needs_resample and verbose:
            print(f"    Resampling: {abs(out_res_x):.6g} x {abs(out_res_y):.6g} -> {abs(target_res[0]):.6g} x {abs(target_res[1]):.6g} (resample={resample})", flush=True)

        transform = from_origin(ulx, uly, out_res_x, abs(out_res_y))
        crs = info["crs"]

        # --- WARP PARAMETERS (computed once, used per-band or per-stack) ---
        if needs_reproject:
            # Effective post-downscale dimensions for default transform calculation
            df = downscale_factor if (downscale_factor and downscale_factor > 1) else 1
            w_eff = (w - w % df) // df
            h_eff = (h - h % df) // df

            _dt, _dw, _dh = calculate_default_transform(
                input_crs_obj, dst_crs_obj, w_eff, h_eff,
                left=ulx, bottom=uly - h_eff * abs(out_res_y),
                right=ulx + w_eff * out_res_x, top=uly,
            )
            if target_res:
                out_px, out_py = abs(target_res[0]), abs(target_res[1])
                if verbose:
                    print(f"    Using explicit target resolution: {out_px} x {out_py}", flush=True)
            else:
                out_px = abs(_dt.a)
                out_py = abs(_dt.e)

            if projwin:
                # projwin bounds are in target_srs; snap to target pixel grid
                ulx_t, uly_t, lrx_t, lry_t = projwin
                snap_ulx = math.floor(ulx_t / out_px) * out_px
                snap_uly = math.ceil(uly_t / out_py) * out_py
                snap_lrx = math.ceil(lrx_t / out_px) * out_px
                snap_lry = math.floor(lry_t / out_py) * out_py
                dst_w = max(1, int(round((snap_lrx - snap_ulx) / out_px)))
                dst_h = max(1, int(round((snap_uly - snap_lry) / out_py)))
                dst_transform = from_origin(snap_ulx, snap_uly, out_px, out_py)
            elif target_res or target_align_pixels:
                # srcwin path: derive output bounds from calculate_default_transform,
                # apply the requested pixel size, and (with -tap) snap origin to
                # integer multiples of the pixel size so no data gaps occur.
                left = _dt.c
                top = _dt.f
                right = _dt.c + _dw * _dt.a
                bottom = _dt.f + _dh * _dt.e
                if target_align_pixels:
                    snap_ulx = math.floor(left / out_px) * out_px
                    snap_uly = math.ceil(top / out_py) * out_py
                    snap_lrx = math.ceil(right / out_px) * out_px
                    snap_lry = math.floor(bottom / out_py) * out_py
                else:
                    snap_ulx, snap_uly = left, top
                    snap_lrx, snap_lry = right, bottom
                dst_w = max(1, int(round((snap_lrx - snap_ulx) / out_px)))
                dst_h = max(1, int(round((snap_uly - snap_lry) / out_py)))
                dst_transform = from_origin(snap_ulx, snap_uly, out_px, out_py)
            else:
                dst_transform, dst_w, dst_h = _dt, _dw, _dh

            warp_kw = {
                "src_transform": transform,
                "src_crs": input_crs_obj,
                "dst_transform": dst_transform,
                "dst_crs": dst_crs_obj,
                "dst_width": dst_w,
                "dst_height": dst_h,
                "resampling": resample_enum,
                "fill_holes": fill_holes,
                "num_threads": num_threads,
            }
            out_transform = dst_transform
            out_crs = dst_crs_obj.to_wkt()
        else:
            warp_kw = None
            out_transform = transform
            out_crs = crs

        def write_bytes(path, bytes_data):
            if path.startswith("s3://"):
                with output_fs.open(path, "wb") as f_out:
                    f_out.write(bytes_data)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f_out:
                    f_out.write(bytes_data)

        files_map = {}

        # Output driver and format-specific options
        _driver = "GTiff" if output_format.upper() == "GTIFF" else "COG"
        _gtiff_extra = {"bigtiff": "YES"} if _driver == "GTiff" else {}
        _n_th = str(num_threads) if num_threads is not None else "ALL_CPUS"
        # PREDICTOR: 3=floating-point, 2=integer -- improves deflate speed & ratio
        # pwr/dB output is float32; AMP is uint16; DN is uint8
        _predictor = 3 if transform_mode.lower() in ("pwr", "db") else 2
        _write_extra = {"num_threads": _n_th, "predictor": _predictor}
        if _driver == "COG":
            _write_extra["overview_resampling"] = "average"

        # === LOW MEMORY MODE: Process each band individually ===
        if use_low_memory_mode:
            if verbose:
                print(f"    Processing bands individually to save memory...", flush=True)

            generated_files = []
            for i, var in enumerate(variable_names):
                if verbose:
                    print(f"      [{i+1}/{len(variable_names)}] Processing {var}...", flush=True)

                # Read single band -- use striped parallel reads for remote files
                band_data = _read_bands_parallel(
                    file_url, input_fs, info["grid_path"], [var], row, h, col, w, n_workers=read_threads
                )[0]

                _var_is_anc = _is_ancillary(var)

                # Take magnitude of off-diagonal (complex) GCOV elements
                if np.iscomplexobj(band_data):
                    band_data = np.abs(band_data).astype(np.float32)

                # Apply gamma0-to-sigma0 conversion (backscatter only)
                if _sigma_data is not None and not _var_is_anc:
                    band_data = band_data * _sigma_data

                # Apply subswath mask to backscatter (source resolution)
                if _mask_invalid is not None and not _var_is_anc:
                    band_data[_mask_invalid] = np.nan

                # Reshape for downscaling (add band dimension)
                band_data = band_data[np.newaxis, :, :]

                # Downscale
                if downscale_factor and downscale_factor > 1:
                    if verbose:
                        print(f"        Downscaling by {downscale_factor} -> {abs(out_res_x):.2f} x {abs(out_res_y):.2f}", flush=True)
                    if _var_is_anc:
                        band_data = _downscale_block(band_data, downscale_factor, _ancillary_downscale_method(var))
                    else:
                        band_data = perform_downscaling(band_data, downscale_factor)

                # Reproject / resample
                if needs_reproject and warp_kw is not None:
                    if verbose:
                        print(f"        Reprojecting {var}...", flush=True)
                    if _var_is_anc:
                        band_data_2d = _reproject_ancillary_band(
                            band_data[0],
                            src_transform=warp_kw["src_transform"],
                            src_crs=warp_kw["src_crs"],
                            dst_transform=warp_kw["dst_transform"],
                            dst_crs=warp_kw["dst_crs"],
                            dst_width=warp_kw["dst_width"],
                            dst_height=warp_kw["dst_height"],
                            resample_name=_ancillary_warp_resampling(var),
                            num_threads=warp_kw.get("num_threads"),
                        )
                    else:
                        band_data_2d = _reproject_power_band(band_data[0], **warp_kw)
                    band_data = band_data_2d[np.newaxis, :, :]
                elif fill_holes and not _var_is_anc:
                    band_data[0] = _fill_nodata_nn(band_data[0])

                # Get output dimensions
                _, h_out, w_out = band_data.shape

                # Transform -- ancillary grids bypass power scaling
                if _var_is_anc:
                    output_dtype = _ancillary_out_dtype(var)
                    output_nodata = _ancillary_nodata(var)
                elif logic_mode == "amp":
                    band_data = pwr_to_amp(band_data)
                    output_dtype = "uint16"
                    output_nodata = 0
                elif logic_mode == "dn":
                    band_data = power_to_dn_uint8(band_data)
                    output_dtype = "uint8"
                    output_nodata = 0
                elif logic_mode == "db":
                    band_data = power_to_db_float32(band_data)
                    output_dtype = "float32"
                    output_nodata = np.nan
                else:
                    output_dtype = "float32"
                    output_nodata = np.nan

                # Remove band dimension for writing
                band_data = band_data[0, :, :]

                # Final dtype cast and nodata cleanup
                if output_dtype == "uint8":
                    band_data = np.nan_to_num(
                        band_data,
                        nan=0.0 if output_nodata is None else float(output_nodata)
                    ).astype(np.uint8)
                elif output_dtype == "float32":
                    band_data[~np.isfinite(band_data)] = np.nan

                # Write -- ancillary grids get their own suffix
                if _var_is_anc:
                    suffix = f"-EBD_{frequency}_{_ancillary_suffix(var)}.tif"
                else:
                    pol_str = var.lower() if _is_qp else var[:2].lower()
                    suffix = f"-EBD_{frequency}_{pol_str}_{mode_str}.tif"

                if final_path.endswith(".tif"):
                    band_path = final_path[:-4] + suffix
                else:
                    band_path = final_path + suffix

                # Per-band write options: predictor and overview resampling
                # depend on output dtype (int vs float) and variable type
                _band_predictor = 2 if output_dtype in ("uint8", "uint16") else 3
                _band_write = dict(_write_extra, predictor=_band_predictor)
                if _var_is_anc and output_dtype == "uint8" and _driver == "COG":
                    # Averaging overviews of a flag grid invents codes.
                    _band_write["overview_resampling"] = "nearest"

                profile = {"driver": _driver, "height": h_out, "width": w_out, "count": 1, "dtype": output_dtype, "crs": out_crs, "transform": out_transform, "compress": "deflate", "nodata": output_nodata, **_gtiff_extra, **_band_write}

                _band_tags = dict(acq_meta)
                if not _var_is_anc:
                    _band_tags["RADIOMETRY"] = _radiometry
                    if _db_formula:
                        _band_tags["DB_FORMULA"] = _db_formula

                with rasterio.Env(GDAL_NUM_THREADS=_n_th, GDAL_OVR_PROPAGATE_NODATA="NO"):
                    with MemoryFile() as memfile:
                        with memfile.open(**profile) as dst:
                            dst.write(band_data, 1)
                            dst.set_band_description(1, var)
                            dst.update_tags(**_band_tags)
                            dst.update_tags(1, Date=date_str)
                        memfile.seek(0)
                        write_bytes(band_path, memfile.read())
                        files_map[var] = band_path
                        generated_files.append(band_path)

                # Free memory immediately
                del band_data

            # VRT generation for low-memory mode -- backscatter bands only
            _bsc_files = [files_map[v] for v in variable_names if not _is_ancillary(v)]
            _bsc_vars = [v for v in variable_names if not _is_ancillary(v)]
            if vrt and _bsc_files:
                pol_list_str = "".join(v.lower() if _is_qp else v[:2].lower() for v in _bsc_vars)
                vrt_suffix = f"-EBD_{frequency}_{pol_list_str}_{mode_str}.vrt"

                if final_path.endswith(".tif"):
                    vrt_path = final_path[:-4] + vrt_suffix
                else:
                    vrt_path = final_path + vrt_suffix

                _vrt_meta = dict(acq_meta, RADIOMETRY=_radiometry)
                if _db_formula:
                    _vrt_meta["DB_FORMULA"] = _db_formula
                vrt_xml = generate_vrt_xml_single_step(w_out, h_out, out_transform, out_crs, _bsc_files, _bsc_vars, date_str, dtype=output_dtype, nodata=output_nodata, metadata=_vrt_meta)
                if verbose:
                    print(f"    Generated Snapshot VRT: {vrt_path}", flush=True)
                write_bytes(vrt_path, vrt_xml.encode("utf-8"))

        # === NORMAL MODE: Process all bands at once ===
        else:
            # Separate ancillary bands from backscatter so that
            # downscale/warp/transform can use the correct strategy per type.
            _anc_indices = [i for i, v in enumerate(variable_names) if _is_ancillary(v)]
            _bsc_indices = [i for i, v in enumerate(variable_names) if not _is_ancillary(v)]
            _anc_data = {variable_names[i]: data_stack[i] for i in _anc_indices}  # 2-D each
            if _bsc_indices:
                data_stack = data_stack[_bsc_indices]
            else:
                data_stack = np.empty((0,) + data_stack.shape[1:], dtype=np.float32)

            # -- Downscale backscatter (nanmean) --
            if downscale_factor and downscale_factor > 1:
                if verbose:
                    print(f"    Downscaling by {downscale_factor} -> {abs(out_res_x):.2f} x {abs(out_res_y):.2f}", flush=True)
                if data_stack.shape[0] > 0:
                    data_stack = perform_downscaling(data_stack, downscale_factor)
                for vname, arr in _anc_data.items():
                    _anc_data[vname] = _downscale_block(
                        arr[np.newaxis], downscale_factor, _ancillary_downscale_method(vname)
                    )[0]

            # -- Reproject / resample --
            if needs_reproject and warp_kw is not None:
                if verbose:
                    _n_bsc = data_stack.shape[0]
                    _n_anc = len(_anc_data)
                    _est_gb = (_n_bsc * 2 + _n_anc) * warp_kw["dst_height"] * warp_kw["dst_width"] * 4 / 1e9
                    print(f"    Reprojecting {_n_bsc} backscatter + {_n_anc} ancillary bands (~{_est_gb:.1f} GB)...", flush=True)
                if data_stack.shape[0] > 0:
                    n_bsc = data_stack.shape[0]
                    warped = np.full((n_bsc, warp_kw["dst_height"], warp_kw["dst_width"]), np.nan, dtype=np.float32)
                    for bi in range(n_bsc):
                        warped[bi] = _reproject_power_band(data_stack[bi], **warp_kw)
                    data_stack = warped
                for vname, arr in _anc_data.items():
                    _anc_data[vname] = _reproject_ancillary_band(
                        arr,
                        src_transform=warp_kw["src_transform"],
                        src_crs=warp_kw["src_crs"],
                        dst_transform=warp_kw["dst_transform"],
                        dst_crs=warp_kw["dst_crs"],
                        dst_width=warp_kw["dst_width"],
                        dst_height=warp_kw["dst_height"],
                        resample_name=_ancillary_warp_resampling(vname),
                        num_threads=warp_kw.get("num_threads"),
                    )
            elif fill_holes:
                for bi in range(data_stack.shape[0]):
                    data_stack[bi] = _fill_nodata_nn(data_stack[bi])
                # No hole-fill for ancillary grids

            if data_stack.shape[0] > 0:
                _, h_out, w_out = data_stack.shape
            elif _anc_data:
                _first_anc = next(iter(_anc_data.values()))
                h_out, w_out = _first_anc.shape
            else:
                raise ValueError("No bands to process.")

            # --- TRANSFORM LOGIC (backscatter only) ---
            if verbose:
                print(f"    Transforming: {mode_str} (Mode: {logic_mode})", flush=True)

            if data_stack.shape[0] == 0:
                processed_data = data_stack
                output_dtype = "float32"
                output_nodata = np.nan
            elif logic_mode == "amp":
                processed_data = pwr_to_amp(data_stack)
                output_dtype = "uint16"
                output_nodata = 0
            elif logic_mode == "dn":
                processed_data = power_to_dn_uint8(data_stack)
                output_dtype = "uint8"
                output_nodata = 0
            elif logic_mode == "db":
                processed_data = power_to_db_float32(data_stack)
                output_dtype = "float32"
                output_nodata = np.nan
            else:
                processed_data = data_stack
                output_dtype = "float32"
                output_nodata = np.nan

            # --- DUAL-POL RATIO: append as extra band after transform ---
            # With -dpratio, ancillary vars are already stripped from variable_names,
            # so _bsc_indices maps 1:1 onto processed_data bands.
            if dualpol_ratio and _ratio_pol_override:
                _bsc_var_list = [variable_names[i] for i in _bsc_indices]
                num_idx = _bsc_var_list.index(_ratio_num_var)
                den_idx = _bsc_var_list.index(_ratio_den_var)

                if logic_mode == "amp":
                    # (amp_likepol / amp_crosspol) * 1000 -> uint16, nodata=0, clamp [1, 65535]
                    with np.errstate(divide="ignore", invalid="ignore"):
                        ratio_data = (processed_data[num_idx].astype(np.float32) /
                                      processed_data[den_idx].astype(np.float32) * 1000.0)
                    nodata_mask = ~np.isfinite(ratio_data) | (processed_data[den_idx] == 0)
                    ratio_data = np.clip(ratio_data, 1, 65535)
                    ratio_data[nodata_mask] = 0
                    ratio_band = ratio_data.astype(np.uint16)

                elif logic_mode == "dn":
                    # dn_likepol - dn_crosspol + 127 -> uint8, nodata=0, clamp [1, 255]
                    ratio_data = (processed_data[num_idx].astype(np.float32) -
                                  processed_data[den_idx].astype(np.float32) + 127.0)
                    nodata_mask = (processed_data[num_idx] == 0) | (processed_data[den_idx] == 0)
                    ratio_data = np.clip(ratio_data, 1, 255)
                    ratio_data[nodata_mask] = 0
                    ratio_band = ratio_data.astype(np.uint8)

                elif logic_mode == "db":
                    # dB_likepol - dB_crosspol -> float32
                    ratio_band = (processed_data[num_idx] - processed_data[den_idx]).astype(np.float32)

                else:
                    # pwr: likepol / crosspol -> float32
                    with np.errstate(divide="ignore", invalid="ignore"):
                        ratio_band = np.where(
                            processed_data[den_idx] != 0,
                            processed_data[num_idx] / processed_data[den_idx],
                            np.nan,
                        ).astype(np.float32)

                processed_data = np.concatenate([processed_data, ratio_band[np.newaxis, :, :]], axis=0)
                variable_names = list(variable_names) + [_ratio_band_name]

            # Replace +/-inf with NaN so COG overview averaging excludes them
            if output_dtype == "float32" and processed_data.shape[0] > 0:
                processed_data[~np.isfinite(processed_data)] = np.nan

            # Build ordered write list: (var_name, 2d_array, dtype, nodata)
            # Backscatter bands (incl. ratio) come from processed_data;
            # ancillary from _anc_data.
            _write_list = []
            _bsc_idx = 0
            for var in variable_names:
                if _is_ancillary(var):
                    _write_list.append((var, _anc_data[var], _ancillary_out_dtype(var), _ancillary_nodata(var)))
                else:
                    _write_list.append((var, processed_data[_bsc_idx], output_dtype, output_nodata))
                    _bsc_idx += 1

            if single_bands:
                if verbose:
                    print("    Writing separate bands...", flush=True)
                    _t_write = _time.perf_counter()
                generated_files = []
                for var, band_data, _bd_dtype, _bd_nodata in _write_list:
                    _var_is_anc = _is_ancillary(var)

                    # Suffix
                    if _var_is_anc:
                        suffix = f"-EBD_{frequency}_{_ancillary_suffix(var)}.tif"
                    else:
                        pol_str = (_ratio_pol_override if var == _ratio_band_name
                                   else var[:2].lower() if dualpol_ratio
                                   else var.lower() if _is_qp
                                   else var[:2].lower())
                        suffix = f"-EBD_{frequency}_{pol_str}_{mode_str}.tif"

                    if final_path.endswith(".tif"):
                        band_path = final_path[:-4] + suffix
                    else:
                        band_path = final_path + suffix

                    # Final dtype cast
                    if _bd_dtype == "uint8":
                        band_data = np.nan_to_num(
                            band_data,
                            nan=0.0 if _bd_nodata is None else float(_bd_nodata)
                        ).astype(np.uint8)

                    # Per-band write options
                    _band_predictor = 2 if _bd_dtype in ("uint8", "uint16") else 3
                    _band_write = dict(_write_extra, predictor=_band_predictor)
                    if _var_is_anc and _bd_dtype == "uint8" and _driver == "COG":
                        _band_write["overview_resampling"] = "nearest"

                    profile = {"driver": _driver, "height": h_out, "width": w_out, "count": 1, "dtype": _bd_dtype, "crs": out_crs, "transform": out_transform, "compress": "deflate", "nodata": _bd_nodata, **_gtiff_extra, **_band_write}

                    _band_tags = dict(acq_meta)
                    if not _var_is_anc:
                        _band_tags["RADIOMETRY"] = _radiometry
                        if _db_formula:
                            _band_tags["DB_FORMULA"] = _db_formula

                    with rasterio.Env(GDAL_OVR_PROPAGATE_NODATA="NO"):
                        with MemoryFile() as memfile:
                            with memfile.open(**profile) as dst:
                                dst.write(band_data, 1)
                                dst.set_band_description(1, var)
                                dst.update_tags(**_band_tags)
                                dst.update_tags(1, Date=date_str)
                            memfile.seek(0)
                            write_bytes(band_path, memfile.read())
                            files_map[var] = band_path
                            generated_files.append(band_path)

                if verbose:
                    sz = sum(os.path.getsize(p) for p in generated_files if os.path.isfile(p))
                    print(f"    [t] COG write ({len(generated_files)} bands, {sz/1e6:.1f} MB): {_time.perf_counter()-_t_write:.1f}s", flush=True)

                # Snapshot VRT -- backscatter bands only (ancillary kept as separate TIFs)
                _bsc_files = [files_map[v] for v in variable_names if not _is_ancillary(v)]
                _bsc_vars = [v for v in variable_names if not _is_ancillary(v)]
                if vrt and _bsc_files:
                    if _ratio_pol_override:
                        _src_vars = [v for v in _bsc_vars if v != _ratio_band_name]
                        pol_list_str = "".join(v[:2].lower() for v in _src_vars) + _ratio_pol_override
                    else:
                        pol_list_str = "".join(v.lower() if _is_qp else v[:2].lower() for v in _bsc_vars)
                    vrt_suffix = f"-EBD_{frequency}_{pol_list_str}_{mode_str}.vrt"

                    if final_path.endswith(".tif"):
                        vrt_path = final_path[:-4] + vrt_suffix
                    else:
                        vrt_path = final_path + vrt_suffix

                    _vrt_meta = dict(acq_meta, RADIOMETRY=_radiometry)
                    if _db_formula:
                        _vrt_meta["DB_FORMULA"] = _db_formula
                    vrt_xml = generate_vrt_xml_single_step(w_out, h_out, out_transform, out_crs, _bsc_files, _bsc_vars, date_str, dtype=output_dtype, nodata=output_nodata, metadata=_vrt_meta)
                    if verbose:
                        print(f"    Generated Snapshot VRT: {vrt_path}", flush=True)
                    write_bytes(vrt_path, vrt_xml.encode("utf-8"))
            else:
                # Multi-band COG: backscatter bands only; ancillary written as separate TIFs
                if verbose:
                    print("    Writing Multi-band COG...", flush=True)
                    _t_write = _time.perf_counter()
                _bsc_vars = [v for v in variable_names if not _is_ancillary(v)]
                _mb_tags = dict(acq_meta, RADIOMETRY=_radiometry)
                if _db_formula:
                    _mb_tags["DB_FORMULA"] = _db_formula
                profile = {"driver": _driver, "height": h_out, "width": w_out, "count": processed_data.shape[0], "dtype": output_dtype, "crs": out_crs, "transform": out_transform, "compress": "deflate", "nodata": output_nodata, **_gtiff_extra, **_write_extra}
                with rasterio.Env(GDAL_NUM_THREADS=_n_th, GDAL_OVR_PROPAGATE_NODATA="NO"):
                    with MemoryFile() as memfile:
                        with memfile.open(**profile) as dst:
                            dst.write(processed_data)
                            for i, var in enumerate(_bsc_vars):
                                dst.set_band_description(i + 1, var)
                                dst.update_tags(i + 1, Date=date_str)
                            dst.update_tags(**_mb_tags)
                        memfile.seek(0)
                        write_bytes(final_path, memfile.read())

                for var in _bsc_vars:
                    files_map[var] = final_path

                # Write ancillary bands as separate TIFs
                for var, arr in _anc_data.items():
                    _anc_suf = f"-EBD_{frequency}_{_ancillary_suffix(var)}.tif"
                    band_path = (final_path[:-4] if final_path.endswith(".tif") else final_path) + _anc_suf
                    _ad = _ancillary_out_dtype(var)
                    _an = _ancillary_nodata(var)
                    _ap = 2 if _ad in ("uint8", "uint16") else 3
                    _aw = dict(_write_extra, predictor=_ap)
                    if _ad == "uint8" and _driver == "COG":
                        _aw["overview_resampling"] = "nearest"
                    band_out = (np.nan_to_num(
                        arr, nan=0.0 if _an is None else float(_an)
                    ).astype(np.uint8) if _ad == "uint8" else arr)
                    _profile = {"driver": _driver, "height": h_out, "width": w_out, "count": 1, "dtype": _ad, "crs": out_crs, "transform": out_transform, "compress": "deflate", "nodata": _an, **_gtiff_extra, **_aw}
                    with rasterio.Env(GDAL_OVR_PROPAGATE_NODATA="NO"):
                        with MemoryFile() as memfile:
                            with memfile.open(**_profile) as dst:
                                dst.write(band_out, 1)
                                dst.set_band_description(1, var)
                                dst.update_tags(**acq_meta)
                                dst.update_tags(1, Date=date_str)
                            memfile.seek(0)
                            write_bytes(band_path, memfile.read())
                    files_map[var] = band_path

                if verbose:
                    _all_files = [final_path] + [files_map[v] for v in _anc_data]
                    sz = sum(os.path.getsize(p) for p in _all_files if os.path.isfile(p))
                    print(f"    [t] COG write ({processed_data.shape[0]}+{len(_anc_data)} bands, {sz/1e6:.1f} MB): {_time.perf_counter()-_t_write:.1f}s", flush=True)

        if verbose:
            memory_mode = "low-memory (per-band)" if use_low_memory_mode else "standard (all-bands)"
            print(f"    [OK] Complete ({memory_mode} mode)", flush=True)

        return {"success": True, "h5_url": h5_url, "date": date_str, "files_map": files_map, "info": {"w": w_out, "h": h_out, "transform": out_transform, "crs": out_crs, "dtype": output_dtype}}

    except Exception as e:
        print(f"!!! Error processing {h5_basename}: {e}", file=sys.stderr, flush=True)
        if verbose:
            traceback.print_exc()
        return {"success": False, "error": str(e), "h5_url": h5_url}
    finally:
        # Clean up both h5py and datatree handles
        if f:
            f.close()
        if dt is not None:
            try:
                dt.close()
            except Exception:
                pass

        # OPTIMIZATION: Remove cached H5 file immediately after processing
        # instead of waiting for atexit (saves disk space during batch processing)
        if cached_file_path is not None:
            try:
                if os.path.exists(cached_file_path):
                    os.remove(cached_file_path)
                    if verbose:
                        print(f"    Removed cached file: {os.path.basename(cached_file_path)}", flush=True)
            except Exception as e:
                if verbose:
                    print(f"    Warning: Could not remove cached file {cached_file_path}: {e}", flush=True)


# =========================================================
# 7. WORKER TASK (Wrapper)
# =========================================================


def process_chunk_task(h5_url, variable_names, output_path, srcwin=None, projwin=None, transform_mode="db", frequency=None, single_bands=False, vrt=False, downscale_factor=None, target_align_pixels=False, input_auth=None, output_auth=None, time_series_vrt=True, list_grids=False, cache=None, keep=False, verbose=False, target_srs=None, target_res=None, resample="cubic", output_format="COG", fill_holes=False, num_threads=None, read_threads=8, dualpol_ratio=False, sigma0=False, projwin_srs=None, apply_mask=True, all_frequencies=False, h5_ancillary_floats=True):

    use_earthdata = False
    if input_auth is None:
        input_auth = {"use_earthdata": False}
    if "use_earthdata" in input_auth:
        use_earthdata = input_auth["use_earthdata"]

    if output_auth is None:
        output_auth = {}

    urls = h5_url

    # Authenticate once per process for any Earthdata URL (HTTPS or S3).
    # earthaccess caches the session globally; calling login() per file-open
    # causes a full URS round-trip each time (~60-90 s on cold start).
    if use_earthdata and HAS_EARTHACCESS and urls:
        if verbose:
            import time as _time
            _t0 = _time.perf_counter()
        _earthaccess_login(verbose=verbose)
        if verbose:
            # Only print timing when NOT instant (cache miss); cache hit prints its own line
            elapsed = _time.perf_counter() - _t0
            if elapsed > 1:
                print(f"    [t] earthaccess login:  {elapsed:.1f}s", flush=True)

    if not list_grids and len(urls) > 1:
        is_batch = True
        if output_path.lower().endswith(".tif") and not output_path.endswith("/"):
            raise ValueError("Batch mode requires output_path to be a directory prefix.")
    else:
        is_batch = False

    # Mode Setup for Batch VRT Naming
    mode_str = transform_mode if transform_mode else "pwr"
    if transform_mode:
        if transform_mode.lower() == "amp":
            mode_str = "AMP"
        elif transform_mode.lower() == "dn":
            mode_str = "DN"
        elif transform_mode.lower() == "db":
            mode_str = "dB"

    if verbose and is_batch:
        print(f"Batch Processing Started: {len(urls)} files.")

    results_meta = []

    try:
        # For HTTPS earthdata URLs, open_h5_lazy uses earthaccess.open() directly
        # and ignores input_fs -- skip the create_s3_fs call (and its login round-trip).
        _https_earthdata = use_earthdata and urls and urls[0].startswith("https://")
        input_fs = None if _https_earthdata else create_s3_fs(input_auth)

        # --- LIST GRIDS MODE ---
        if list_grids:
            print(f"Inspecting file: {urls[0]}")
            try:
                f = open_h5_lazy(urls[0], input_fs)
                struct = inspect_h5_structure(f)
                f.close()

                print("\nAvailable Grids in HDF5:")
                for freq, details in struct.items():
                    if "error" in details:
                        print(f"  Frequency {freq}: Error - {details['error']}")
                    else:
                        print(f"  Frequency {freq}:")
                        print(f"    CRS: {details['crs']}")
                        print(f"    Raster Size:  {details['ncols']} x {details['nrows']} pixels (cols/rows)")
                        print(f"    Resolution: X={details['res_x']:.2f}, Y={details['res_y']:.2f}")
                        # Bbox
                        bbox = details["bbox"]
                        print(f"    Extent (W,S,E,N): [{bbox[0]:.2f}, {bbox[1]:.2f}, {bbox[2]:.2f}, {bbox[3]:.2f}]")
                        # Exact Footprints
                        print(f"    Footprint (Lon/Lat): {details.get('poly_geo', 'N/A')}")
                        print(f"    Footprint (Native):  {details.get('poly_native', 'N/A')}")
                        print(f"    Frame Size:          {details.get('dims', 'N/A')}")
                        print(f"    Variables:")
                        vd = details.get("var_details", {})
                        for vname in details["vars"]:
                            info_v = vd.get(vname, {})
                            dt = info_v.get("dtype", "?")
                            nd = info_v.get("nodata", "?")
                            print(f"      {vname:30s}  dtype={dt}  nodata={nd}")
                return "Inspection Complete."
            except Exception as e:
                import traceback

                traceback.print_exc()
                return f"Error inspecting file: {e}"

        # --- AUTO-DETECT DEFAULTS ---
        # If variable_names is None, detect them from the first file using the given/default frequency
        target_freq = frequency if frequency else "A"

        if variable_names is None or len(variable_names) == 0:
            if verbose:
                print(f"No variables specified. Auto-detecting Covariance variables for Frequency {target_freq}...")
            try:
                f = open_h5_lazy(urls[0], input_fs)
                struct = inspect_h5_structure(f)
                f.close()

                if target_freq in struct and "vars" in struct[target_freq]:
                    all_vars = struct[target_freq]["vars"]
                    # FILTER: Only keep 4-letter UPPERCASE vars (e.g. HHHH, HVHV)
                    # This excludes 'incidenceAngle', 'layoverShadowMask', etc.
                    filtered_vars = [v for v in all_vars if len(v) == 4 and v.isupper()]

                    if not filtered_vars:
                        return f"Error: No Covariance variables (4-letter upper) found for Freq {target_freq}. Available: {all_vars}"

                    variable_names = filtered_vars
                    if verbose:
                        print(f"  -> Selected: {variable_names}")
                else:
                    return f"Error: Frequency {target_freq} not found in file or has no variables."
            except Exception as e:
                return f"Error detecting variables: {e}"

        output_fs = None
        if output_path.startswith("s3://"):
            output_fs = create_s3_fs(output_auth)

        for url in urls:
            res = _process_single_file(url, variable_names, output_path, srcwin, projwin, transform_mode, frequency, single_bands, vrt, downscale_factor, target_align_pixels, input_fs, output_fs, is_batch=is_batch, cache=cache, keep=keep, use_earthdata=use_earthdata, verbose=verbose, target_srs=target_srs, target_res=target_res, resample=resample, output_format=output_format, fill_holes=fill_holes, num_threads=num_threads, read_threads=read_threads, dualpol_ratio=dualpol_ratio, sigma0=sigma0, projwin_srs=projwin_srs, apply_mask=apply_mask, all_frequencies=all_frequencies, h5_ancillary_floats=h5_ancillary_floats)
            results_meta.append(res)

        if is_batch and time_series_vrt and output_format.lower() != "h5":
            valid_results = [r for r in results_meta if r["success"]]
            if not valid_results:
                return "Batch failed: No valid files processed."

            if verbose:
                print("Generating Time Series VRTs...")

            valid_results.sort(key=lambda x: x["date"])
            ref_info = valid_results[0]["info"]
            min_date = valid_results[0]["date"]
            max_date = valid_results[-1]["date"]

            # Detect if all outputs share the same spatial extent
            all_same_geom = all(
                r["info"]["w"] == ref_info["w"]
                and r["info"]["h"] == ref_info["h"]
                and r["info"]["transform"] == ref_info["transform"]
                for r in valid_results
            )

            def write_bytes(path, bytes_data):
                if path.startswith("s3://"):
                    with output_fs.open(path, "wb") as f_out:
                        f_out.write(bytes_data)
                else:
                    with open(path, "wb") as f_out:
                        f_out.write(bytes_data)

            # Separate backscatter and ancillary variables; track backscatter-only index
            _bsc_vars_ts = [v for v in variable_names if not _is_ancillary(v)]
            _bsc_idx_map = {v: i for i, v in enumerate(_bsc_vars_ts)}

            # Build per-variable time-series VRTs for backscatter only.
            # Ancillary TS VRTs are built by build_track_vrts (mosaic-aware).
            _bsc_count = 0
            for var_idx, var in enumerate(variable_names):
                if _is_ancillary(var):
                    continue

                pol_str = var[:2].lower()

                stack_items = []
                dates_list = []
                for r in valid_results:
                    fpath = r["files_map"][var]
                    b_idx = 1 if single_bands else (_bsc_idx_map[var] + 1)
                    item = {"path": fpath, "band_idx": b_idx, "date": r["date"]}
                    if not all_same_geom:
                        item["transform"] = r["info"]["transform"]
                        item["w"] = r["info"]["w"]
                        item["h"] = r["info"]["h"]
                    stack_items.append(item)
                    dates_list.append(r["date"].replace("-", ""))

                if all_same_geom:
                    vrt_xml = generate_vrt_xml_timeseries(ref_info["w"], ref_info["h"], ref_info["transform"], ref_info["crs"], stack_items, dtype=ref_info["dtype"])
                else:
                    vrt_xml = generate_vrt_xml_timeseries_union(ref_info["crs"], stack_items, dtype=ref_info["dtype"])

                vrt_name = construct_timeseries_filename(valid_results[0]["h5_url"], min_date, max_date, frequency, pol_str, mode_str)
                out_dir = output_path.rstrip("/")
                vrt_full_path = f"{out_dir}/{vrt_name}"
                if verbose:
                    print(f"  --> VRT: {vrt_name}")
                write_bytes(vrt_full_path, vrt_xml.encode("utf-8"))
                write_bytes(vrt_full_path.replace(".vrt", ".dates"), "\n".join(dates_list).encode("utf-8"))
                _bsc_count += 1

            return f"Batch Complete. Generated {_bsc_count} Time Series VRTs."

        if len(results_meta) == 1:
            return "Done" if results_meta[0]["success"] else results_meta[0]["error"]
        return f"Batch Processed {len(results_meta)} files."

    except Exception as e:
        import traceback

        traceback.print_exc()
        return f"Critical Error: {str(e)}"


# =========================================================
# 8. UTILS: REBUILD VRTS
# =========================================================


def rebuild_vrts(output_path, variable_names, transform_mode="AMP", frequency="A", auth_config=None, verbose=True, build_ts=True):
    """
    Scans output_path for TIFs matching frequency/mode.
    1. Rebuilds Multi-band Snapshot VRTs (grouped by Date).
    2. Rebuilds Time Series VRTs (grouped by Polarization).
    """
    if auth_config is None:
        auth_config = {}

    # 1. Create S3FS Session
    # We will use this for BOTH listing files AND opening them to read metadata.
    if output_path.startswith("s3://"):
        fs = create_s3_fs(auth_config)
    else:
        fs = None  # Local mode

    # Mode Handling
    mode_str = transform_mode if transform_mode else "pwr"
    if transform_mode:
        if transform_mode.lower() == "amp":
            mode_str = "AMP"
        elif transform_mode.lower() == "dn":
            mode_str = "DN"
        elif transform_mode.lower() == "db":
            mode_str = "dB"

    if verbose:
        print(f"Scanning {output_path} for *{frequency}*_{mode_str}.tif ...")

    # --- 1. LIST FILES ---
    if fs:
        bucket_path = output_path.replace("s3://", "")
        try:
            files = fs.ls(bucket_path)
            # Filter and ensure s3:// prefix
            tif_files = [f"s3://{f}" for f in files if f.endswith(".tif") and f"_{frequency}_" in f and f"_{mode_str}.tif" in f]
        except Exception as e:
            return f"Error listing S3: {e}"
    else:
        import glob

        pattern = os.path.join(output_path, f"*-EBD_{frequency}_*_{mode_str}.tif")
        tif_files = glob.glob(pattern)

    if not tif_files:
        return "No matching files found."

    # Detect quad-pol from the polarization code in the first TIF filename (token index 9).
    _tif_base = os.path.basename(tif_files[0]).split("-EBD_")[0]
    _tif_tokens = _tif_base.split("_")
    _pol_code = _tif_tokens[9] if len(_tif_tokens) > 9 else ""
    _is_qp = (_pol_code[:2] == "QP" if frequency == "A" else _pol_code[2:] == "QP")

    # Get the polarizations if variable_names is None:
    if variable_names is None:
        pols = {x.split("_")[-2].upper() for x in tif_files}
        variable_names = sorted(pols)

    # --- 2. PARSE METADATA & GROUP ---
    dates_map = defaultdict(lambda: defaultdict(dict))
    ref_info = None

    date_pattern = r"(\d{8})T"
    pol_pattern = f"-EBD_{frequency}_([a-zA-Z0-9]+)_{mode_str}.tif"

    for fpath in tif_files:
        basename = os.path.basename(fpath)

        d_match = re.search(date_pattern, basename)
        if not d_match:
            continue
        ymd = d_match.group(1)
        fmt_date = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"

        p_match = re.search(pol_pattern, basename)
        if not p_match:
            continue
        pol_found = p_match.group(1).upper()
        key_pol = pol_found if _is_qp else pol_found[:2]

        entry = dates_map[ymd][key_pol]
        entry["path"] = fpath
        entry["filename"] = basename
        entry["date_fmt"] = fmt_date

        # Read per-file geometry for union VRT support
        try:
            if fs:
                with fs.open(fpath, "rb") as fobj:
                    with rasterio.open(fobj) as ds:
                        file_geo = {"w": ds.width, "h": ds.height, "transform": ds.transform, "crs": ds.crs.to_wkt(), "dtype": ds.dtypes[0]}
            else:
                with rasterio.open(fpath) as ds:
                    file_geo = {"w": ds.width, "h": ds.height, "transform": ds.transform, "crs": ds.crs.to_wkt(), "dtype": ds.dtypes[0]}
            entry["geo"] = file_geo
            if ref_info is None:
                ref_info = file_geo
        except Exception as e:
            if verbose:
                print(f"Warning: Failed to read metadata from {basename}: {e}")
            entry["geo"] = None

    if not ref_info:
        return "Could not open any TIFs to get reference geometry. Check S3 permissions."

    # Define Write Helper
    def write_bytes(path, bytes_data):
        if path.startswith("s3://"):
            with fs.open(path, "wb") as f_out:
                f_out.write(bytes_data)
        else:
            with open(path, "wb") as f_out:
                f_out.write(bytes_data)

    # --- 3. REBUILD SNAPSHOTS ---
    count_snapshots = 0
    sorted_dates = sorted(dates_map.keys())

    for ymd in sorted_dates:
        day_files = dates_map[ymd]
        ordered_files = []
        ordered_vars = []
        base_filename = None

        for var in variable_names:
            k = var.upper() if _is_qp else var[:2].upper()
            if k in day_files:
                ordered_files.append(day_files[k]["path"])
                ordered_vars.append(var)
                if base_filename is None:
                    base_filename = day_files[k]["filename"]

        if not ordered_files:
            continue

        pol_list_str = "".join(v.lower() if _is_qp else v[:2].lower() for v in variable_names)
        new_suffix = f"-EBD_{frequency}_{pol_list_str}_{mode_str}.vrt"
        base_stripped = re.sub(f"-EBD_{frequency}_[a-zA-Z0-9]+_{mode_str}.tif", "", base_filename)
        vrt_full_path = os.path.join(output_path, base_stripped + new_suffix)

        xml = generate_vrt_xml_single_step(ref_info["w"], ref_info["h"], ref_info["transform"], ref_info["crs"], ordered_files, ordered_vars, day_files[list(day_files.keys())[0]]["date_fmt"], dtype=ref_info["dtype"])
        write_bytes(vrt_full_path, xml.encode("utf-8"))
        count_snapshots += 1

    if verbose:
        print(f"Updated {count_snapshots} Snapshot VRTs.")

    # --- 4. REBUILD TIME SERIES ---
    count_ts = 0
    if not build_ts:
        return f"Complete. Updated {count_snapshots} Snapshots."
    if not sorted_dates:
        return "No dates found."

    first_ymd = sorted_dates[0]
    last_ymd = sorted_dates[-1]

    first_day_pols = list(dates_map[first_ymd].keys())
    if not first_day_pols:
        return "Error parsing structure."

    sample_file = dates_map[first_ymd][first_day_pols[0]]["filename"]
    min_date_fmt = dates_map[first_ymd][first_day_pols[0]]["date_fmt"]

    last_day_pols = list(dates_map[last_ymd].keys())
    max_date_fmt = dates_map[last_ymd][last_day_pols[0]]["date_fmt"] if last_day_pols else min_date_fmt

    for var in variable_names:
        k = var[:2].upper()
        stack_items = []
        dates_list = []

        for ymd in sorted_dates:
            if k in dates_map[ymd]:
                f_info = dates_map[ymd][k]
                geo = f_info.get("geo") or ref_info
                item = {"path": f_info["path"], "band_idx": 1, "date": f_info["date_fmt"],
                        "transform": geo["transform"], "w": geo["w"], "h": geo["h"]}
                stack_items.append(item)
                dates_list.append(ymd)

        if not stack_items:
            continue

        all_same_geom = all(
            it["w"] == stack_items[0]["w"]
            and it["h"] == stack_items[0]["h"]
            and it["transform"] == stack_items[0]["transform"]
            for it in stack_items
        )
        if all_same_geom:
            xml = generate_vrt_xml_timeseries(ref_info["w"], ref_info["h"], ref_info["transform"], ref_info["crs"], stack_items, dtype=ref_info["dtype"])
        else:
            xml = generate_vrt_xml_timeseries_union(ref_info["crs"], stack_items, dtype=ref_info["dtype"])

        pol_str = var[:2].lower()
        vrt_name = construct_timeseries_filename(sample_file, min_date_fmt, max_date_fmt, frequency, pol_str, mode_str)
        vrt_full_path = os.path.join(output_path, vrt_name)

        write_bytes(vrt_full_path, xml.encode("utf-8"))
        write_bytes(vrt_full_path.replace(".vrt", ".dates"), "\n".join(dates_list).encode("utf-8"))

        if verbose:
            print(f"  --> TS VRT: {vrt_name}")
        count_ts += 1

    return f"Complete. Updated {count_snapshots} Snapshots and {count_ts} Time Series."


if __name__ == "__main__":
    print(__doc__)
    print("\n" + "=" * 70)
    if HAS_DATATREE:
        print("[OK] OPTIMIZED MODE: datatree available for fast HDF5 access")
        print("  - Lazy loading with spatial subsetting")
        print("  - Parallel band extraction")
        print("  - Reduced memory footprint")
    else:
        print("[!] FALLBACK MODE: Using h5py (slower)")
        print("  Install datatree for better performance:")
        print("    pip install xarray-datatree")
        print("    or: pip install 'xarray>=2024.2.0'")
