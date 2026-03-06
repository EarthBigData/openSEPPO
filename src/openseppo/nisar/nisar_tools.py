"""SEPPO NISAR Tools - OPTIMIZED with datatree

PERFORMANCE IMPROVEMENTS over h5py version:
- Uses xarray datatree for hierarchical HDF5 access with lazy loading
- Parallel band reading instead of sequential loops
- Spatial subsetting before loading into memory
- Better chunking and memory efficiency
- All original function names and signatures preserved for compatibility

# Bash prepare list
$ smddb -A -t -c "select url from nisar_pub.gcov where track=172 and frame=65 order by start_time" -o  urls.txt'

# ipython:
with open("urls.txt","r") as f:
    h5_urls = [x.strip() for x in f.readlines()]


h5_url_ops = "s3://nisar-ops-rs-fwd/products/L2_L_GCOV/2026/01/21/NISAR_L2_PR_GCOV_015_172_D_065_4005_DHDH_A_20260121T031851_20260121T031926_P05006_N_F_J_001/NISAR_L2_PR_GCOV_015_172_D_065_4005_DHDH_A_20260121T031851_20260121T031926_P05006_N_F_J_001.h5"

variable_names=["HHHH", "HVHV"]

srcwin = [10000,10000,2000,2000]
projwin=[500590,5183650,585620,5092470]

output_auth={'profile':'josefk'}

set_credentials("a")

my_input_creds = {
'key': os.environ.get('AWS_ACCESS_KEY_ID'),
'secret': os.environ.get('AWS_SECRET_ACCESS_KEY'),
'token': os.environ.get('AWS_SESSION_TOKEN')
}

output_path="s3://ebd-clients-w/TEST/NISAR/"

process_chunk_task(h5_urls,variable_names,output_path,srcwin=None, projwin=projwin,transform_mode="amp", single_bands=True,vrt=True,output_auth=output_auth, input_auth=my_input_creds, frequency='B', downscale_factor=2, verbose=True, target_align_pixels=True)

set_credentials("u")

"""

import sys
import os
import atexit
import tempfile
import shutil
import numpy as np
import h5py
import s3fs
import math
import re
import traceback
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
        needs the bearer token — no OAuth cookies required.
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
        # _http_session is created lazily by get_session() — no network call.
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
        return s  # Not a UTM proj4 string — nothing to fix
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
    """Normalise EPSG string / int / WKT / CRS object → rasterio CRS."""
    if crs_input is None:
        return None
    if isinstance(crs_input, CRS):
        return crs_input
    if isinstance(crs_input, int):
        return CRS.from_epsg(crs_input)
    s = str(crs_input).strip()
    if s.upper().startswith("EPSG:"):
        return CRS.from_epsg(int(s.split(":")[1]))
    # Bare numeric string → treat as EPSG code (e.g. "32634" → EPSG:32634)
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
    Fill ALL NaN / ±inf pixels with the value of their nearest valid neighbour.

    The image-frame boundary is handled naturally by the mask pass in
    _reproject_power_band: destination pixels that fall outside the source
    extent get mask=0 and are set to NaN regardless of this fill.
    NaN regions connected to the image edge (layover, shadow, swath gaps)
    are therefore filled here but restored to NaN in the output wherever
    the destination pixel truly has no source coverage.
    """
    from scipy.ndimage import distance_transform_edt

    valid = np.isfinite(data_2d)
    if valid.all():
        return data_2d
    if not valid.any():
        return data_2d  # nothing to fill from

    row_idx, col_idx = distance_transform_edt(
        ~valid, return_distances=False, return_indices=True)
    result = data_2d.copy()
    inv = ~valid
    result[inv] = data_2d[row_idx[inv], col_idx[inv]]
    return result


def _reproject_power_band(data_2d, src_transform, src_crs, dst_transform, dst_crs,
                           dst_width, dst_height, resampling, fill_holes=False,
                           num_threads=None):
    """
    Warp one 2-D float32 power array. NaN/±inf ↔ NaN nodata. Returns warped array.

    Two-pass approach to avoid introducing extra NaN pixels:

    Pass 1 — warp the data with invalid pixels (NaN/±inf) replaced by 0 and
              src_nodata=None.  With no declared source nodata GDAL clamps
              out-of-bounds kernel taps to the nearest edge pixel rather than
              treating them as NaN, preventing the 1-2 pixel NaN erosion that
              higher-order resampling (cubic, lanczos) otherwise adds around
              every NaN boundary.

    Pass 2 — warp a binary valid-pixel mask using nearest-neighbour resampling
              so each output pixel inherits the validity of its nearest source
              pixel with no kernel spreading.  Only pixels with no source
              coverage at all are set to NaN in the output.

    fill_holes — if True, interior NaN/±inf pixels (those fully enclosed by
                 valid data, not connected to the image frame edge) are filled
                 with their nearest valid neighbour BEFORE warping.  This
                 prevents the cubic kernel from seeing isolated invalid pixels
                 inside the valid image area.  Frame-boundary nodata is
                 unaffected and still propagates to NaN in the output.
    """
    src = data_2d.astype(np.float32)

    if fill_holes:
        src = _fill_nodata_nn(src)

    invalid = ~np.isfinite(src)          # NaN and ±inf
    filled = np.where(invalid, 0.0, src)
    valid = (~invalid).astype(np.float32)

    n_threads = num_threads if num_threads is not None else os.cpu_count() or 1

    kw = dict(src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs=dst_crs,
              src_nodata=None, num_threads=n_threads)

    dst_data = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
    dst_mask = np.zeros((dst_height, dst_width), dtype=np.float32)

    reproject(source=filled, destination=dst_data,
              resampling=resampling, dst_nodata=np.nan, **kw)
    reproject(source=valid, destination=dst_mask,
              resampling=Resampling.nearest, dst_nodata=0.0, **kw)

    dst_data[dst_mask == 0] = np.nan

    # Cubic/lanczos ringing near high-contrast boundaries can produce negative
    # or ±inf power values.  Clamp to a physically meaningful power range:
    #   floor : -40 dB  → 10^(-4)
    #   ceil  : 13.329 dB → 10^(13.329/10)
    # np.where propagates NaN, so image-edge NaN pixels are unaffected.
    _PWR_FLOOR = 10 ** (-40.0 / 10.0)        # 1e-4
    _PWR_CEIL  = 10 ** (13.329 / 10.0)       # ~21.53
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


def perform_downscaling(data_stack, factor):
    if factor is None or factor <= 1:
        return data_stack
    b, h, w = data_stack.shape
    new_h = h - (h % factor)
    new_w = w - (w % factor)
    if new_h == 0 or new_w == 0:
        raise ValueError(f"Downscale factor {factor} is larger than image size ({w}x{h}).")
    cropped = data_stack[:, :new_h, :new_w]
    reshaped = cropped.reshape(b, new_h // factor, factor, new_w // factor, factor)
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
    if "float32" in d:
        return "Float32"
    if "float64" in d:
        return "Float64"
    return "Float32"


def generate_vrt_xml_single_step(width, height, transform, crs_wkt, band_files, band_names, date_str, dtype="UInt16", nodata=None):
    vrt_dtype = get_gdal_dtype(dtype)

    # --- 1. Determine NoData Value ---
    if nodata is not None:
        nodata_val = str(nodata)
    else:
        # Heuristic: Integers use 0, Floats use nan
        # Check against the input dtype string (e.g., 'uint16', 'float32', 'Byte')
        d_str = str(dtype).lower()
        if "int" in d_str or "byte" in d_str:
            nodata_val = "0"
        else:
            nodata_val = "nan"

    geo_transform = f"{transform.c}, {transform.a}, {transform.b}, {transform.f}, {transform.d}, {transform.e}"
    xml = [f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">', f'  <SRS dataAxisToSRSAxisMapping="2,1">{crs_wkt}</SRS>', f"  <GeoTransform>{geo_transform}</GeoTransform>"]
    for i, (fpath, bname) in enumerate(zip(band_files, band_names)):
        if fpath.startswith("s3://"):
            bucket, key = fpath.replace("s3://", "").split("/", 1)
            vrt_path = f"/vsis3/{bucket}/{key}"
        else:
            vrt_path = fpath
        band_xml = f"""
  <VRTRasterBand dataType="{vrt_dtype}" band="{i+1}">
    <NoDataValue>{nodata_val}</NoDataValue>
    <Description>{bname}</Description>
    <Metadata><MDI key="Date">{date_str}</MDI></Metadata>
    <SimpleSource>
      <SourceFilename relativeToVRT="0">{vrt_path}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />
      <DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />
    </SimpleSource>
  </VRTRasterBand>"""  # noqa
        xml.append(band_xml)
    xml.append("</VRTDataset>")
    return "\n".join(xml)


def generate_vrt_xml_timeseries(width, height, transform, crs_wkt, stack_items, dtype="UInt16", nodata=None):
    vrt_dtype = get_gdal_dtype(dtype)

    # --- 1. Determine NoData Value ---
    if nodata is not None:
        nodata_val = str(nodata)
    else:
        # Heuristic: Integers use 0, Floats use nan
        # Check against the input dtype string (e.g., 'uint16', 'float32', 'Byte')
        d_str = str(dtype).lower()
        if "int" in d_str or "byte" in d_str:
            nodata_val = "0"
        else:
            nodata_val = "nan"

    geo_transform = f"{transform.c}, {transform.a}, {transform.b}, {transform.f}, {transform.d}, {transform.e}"

    xml = [f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">', f'  <SRS dataAxisToSRSAxisMapping="2,1">{crs_wkt}</SRS>', f"  <GeoTransform>{geo_transform}</GeoTransform>"]

    for i, item in enumerate(stack_items):
        fpath = item["path"]
        if fpath.startswith("s3://"):
            bucket, key = fpath.replace("s3://", "").split("/", 1)
            vrt_path = f"/vsis3/{bucket}/{key}"
        else:
            vrt_path = fpath

        band_xml = f"""
  <VRTRasterBand dataType="{vrt_dtype}" band="{i + 1}">
    <NoDataValue>{nodata_val}</NoDataValue>
    <Description>{item['date']}</Description>
    <Metadata><MDI key="Date">{item['date']}</MDI></Metadata>
    <SimpleSource>
      <SourceFilename relativeToVRT="0">{vrt_path}</SourceFilename>
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
    if nodata is not None:
        nodata_val = str(nodata)
    else:
        d_str = str(dtype).lower()
        nodata_val = "0" if ("int" in d_str or "byte" in d_str) else "nan"

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
        f'  <SRS dataAxisToSRSAxisMapping="2,1">{crs_wkt}</SRS>',
        f"  <GeoTransform>{geo_transform}</GeoTransform>",
    ]

    for i, item in enumerate(stack_items):
        fpath = item["path"]
        if fpath.startswith("s3://"):
            bucket, key = fpath.replace("s3://", "").split("/", 1)
            vrt_path = f"/vsis3/{bucket}/{key}"
        else:
            vrt_path = fpath

        dst_x_off = int(round((item["transform"].c - union_ulx) / res_x))
        dst_y_off = int(round((union_uly - item["transform"].f) / res_y))
        band_xml = f"""
  <VRTRasterBand dataType="{vrt_dtype}" band="{i + 1}">
    <NoDataValue>{nodata_val}</NoDataValue>
    <Description>{item['date']}</Description>
    <Metadata><MDI key="Date">{item['date']}</MDI></Metadata>
    <SimpleSource>
      <SourceFilename relativeToVRT="0">{vrt_path}</SourceFilename>
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
    return f"{new_base}-EBD_{frequency}_{pol}_{mode_str}.vrt"


# =========================================================
# 3b. HDF5 SUBSET WRITER
# =========================================================


def _write_h5_subset(src_f, grid_path, variable_names, col, row, w, h):
    """
    Return bytes of a proper NetCDF-4/HDF5 subset file openable by GDAL's
    NETCDF: driver.  Uses the netCDF4 library so that named dimensions,
    _Netcdf4Dimid, _NCProperties and grid_mapping are all written correctly.
    Avoids h5py.copy() — only targeted range reads are issued against src_f.
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

    fd, tmp_path = tempfile.mkstemp(suffix=".h5")
    os.close(fd)
    tmp_repacked = tmp_path + "_r.h5"
    try:
        with nc_lib.Dataset(tmp_path, "w", format="NETCDF4") as dst:

            # Global attributes (skip _NC* — managed by netCDF4 library)
            _cpattrs(src_f, dst)

            # Build group hierarchy and copy per-group attributes
            grp = dst
            current_path = ""
            for part in grid_path.strip("/").split("/"):
                current_path += f"/{part}"
                grp = grp.createGroup(part)
                if current_path in src_f:
                    _cpattrs(src_f[current_path], grp)

            # Named dimensions
            grp.createDimension("yCoordinates", h)
            grp.createDimension("xCoordinates", w)

            # Coordinate variables (two targeted range reads)
            x_src = src_f[f"{grid_path}/xCoordinates"]
            y_src = src_f[f"{grid_path}/yCoordinates"]
            x_var = grp.createVariable("xCoordinates", x_src.dtype, ("xCoordinates",))
            y_var = grp.createVariable("yCoordinates", y_src.dtype, ("yCoordinates",))
            x_var[:] = x_src[col:col + w]
            y_var[:] = y_src[row:row + h]
            _cpattrs(x_src, x_var)
            _cpattrs(y_src, y_var)

            # Grid mapping scalar (one tiny read)
            proj_full = f"{grid_path}/projection"
            if proj_full in src_f:
                proj_src = src_f[proj_full]
                proj_var = grp.createVariable("projection", "i4", ())
                try:
                    proj_var[:] = int(proj_src[()])
                except Exception:
                    pass
                _cpattrs(proj_src, proj_var)

            # Data variables (one range read per variable)
            for var in variable_names:
                src_ds = src_f[f"{grid_path}/{var}"]
                data = src_ds[row:row + h, col:col + w].astype(np.float32)
                raw_fill = src_ds.attrs.get("_FillValue", np.nan)
                try:
                    fill_val = float(raw_fill)
                except Exception:
                    fill_val = np.nan
                var_out = grp.createVariable(
                    var, "f4", ("yCoordinates", "xCoordinates"),
                    zlib=True, complevel=4, fill_value=fill_val)
                var_out[:] = data
                _cpattrs(src_ds, var_out, skip=("_FillValue",))

        # h5repack for compact sequential layout (cloud-friendly)
        try:
            sp.check_call(["h5repack", tmp_path, tmp_repacked],
                          stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            read_path = tmp_repacked
        except Exception:
            read_path = tmp_path  # h5repack unavailable — use original

        with open(read_path, "rb") as fh:
            return fh.read()

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


def cache_to_local(url, localdir=None, keep=False, use_earthdata=False):

    if localdir in [None, "yes", "y"]:
        localdir = tempfile.mkdtemp(prefix="tmp_h5toCOG_")
        if not keep:
            atexit.register(shutil.rmtree, localdir, True)

    localdir = localdir.rstrip("/") + "/"
    os.makedirs(localdir, exist_ok=True)

    local_path = os.path.join(localdir, os.path.basename(url))
    print(f"---> Caching {url} to {localdir} ... ", flush=True, end="")

    if url.startswith("https://"):
        if use_earthdata:
            earthaccess.login(strategy="netrc")
            earthaccess.download([url], localdir)
        else:
            import urllib.request
            urllib.request.urlretrieve(url, local_path)
    elif url.startswith("s3://"):
        # s3:// — use aws cli (with optional earthdata S3 credentials)
        env = os.environ.copy()
        if use_earthdata:
            auth = earthaccess.login(strategy="netrc")
            creds = auth.get_s3_credentials(endpoint="https://nisar.asf.earthdatacloud.nasa.gov/s3credentials")
            env["AWS_ACCESS_KEY_ID"] = creds["accessKeyId"]
            env["AWS_SECRET_ACCESS_KEY"] = creds["secretAccessKey"]
            env["AWS_SESSION_TOKEN"] = creds["sessionToken"]
            print("GOT CREDS FROM EARTHACCESS", flush=True)
        cmd = f"aws s3 cp {url} {localdir}"
        sp.check_call(shlex.split(cmd), env=env)
    else:
        # local file — symlink into cache dir
        os.symlink(os.path.abspath(url), local_path)

    print("done")

    if not keep:
        atexit.register(_unlink, local_path)

    return local_path


def open_h5_lazy_slow(path, s3_fs):
    if path.startswith("s3://"):
        return h5py.File(s3_fs.open(path, "rb"), "r")
    return h5py.File(path, "r")


def open_h5_lazy(path, s3_fs, block_size=16 * 1024 * 1024):
    """
    Lazily open a NISAR HDF5 file with S3-optimized metadata access.
    Caller is responsible for closing the file.
    """

    h5_kwargs = {
        "mode": "r",
        "libver": "latest",
        # Disable or shrink chunk cache (often faster for remote reads)
        "rdcc_nbytes": 0,
    }

    if path.startswith("s3://"):
        s3_file = s3_fs.open(
            path,
            mode="rb",
            cache_type="bytes",  # often better than readahead for HDF5
            block_size=block_size,
        )
        return h5py.File(s3_file, driver="fileobj", **h5_kwargs)

    if path.startswith("https://"):
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

    # Local files only — open directly by path
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

                    dims_km = f"Width: {avg_width_km:.2f} km, Height: {avg_height_km:.2f} km"

                except Exception as e:
                    poly_native_display = f"Reprojection Failed: {e}"

            variables = []
            for item in f[g_path].keys():
                if item not in ["projection", "xCoordinates", "yCoordinates", "listOfPolarizations", "covarianceMatrixDiagonal", "covarianceMatrixOffDiagonal"]:
                    obj = f[f"{g_path}/{item}"]
                    if isinstance(obj, h5py.Dataset) and len(obj.shape) >= 2:
                        variables.append(item)

            structure[freq_code] = {"crs": crs, "res_x": float(res_x), "res_y": float(res_y), "bbox": (min_x, min_y, max_x, max_y), "vars": sorted(variables), "poly_geo": poly_geo_display, "poly_native": poly_native_display, "dims": dims_km}
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
    # NISAR grids are uniformly spaced — only read the first two values plus
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


def get_acquisition_metadata_from_datatree(dt):
    """Read acquisition metadata from an open datatree, avoiding a second S3 file open."""
    try:
        node = dt["science/LSAR/identification"]
        ds = node.ds
        t_start = ds["zeroDopplerStartTime"].values
        t_start = t_start.decode() if hasattr(t_start, "decode") else str(t_start)
        if "T" in t_start:
            date_part, time_part = t_start.split("T")
            return {"ACQUISITION_DATE": date_part, "ACQUISITION_TIME": time_part}
        return {"ACQUISITION_DATETIME": t_start}
    except Exception:
        return None


def get_acquisition_metadata(h5_handle):
    try:
        t_start = h5_handle["/science/LSAR/identification/zeroDopplerStartTime"][()]
        t_start = t_start.decode() if hasattr(t_start, "decode") else str(t_start)
        if "T" in t_start:
            date_part, time_part = t_start.split("T")
            return {"ACQUISITION_DATE": date_part, "ACQUISITION_TIME": time_part}
        return {"ACQUISITION_DATETIME": t_start}
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


def power_to_dn_uint8(data, min_db=-40.0, max_db=20.0):
    is_neg_inf = np.isneginf(data)
    is_pos_inf = np.isposinf(data)
    valid_mask = np.isfinite(data) & (data > 0)
    db = np.full(data.shape, -9999.0, dtype=np.float32)
    np.log10(data, out=db, where=valid_mask)
    db[valid_mask] *= 10.0

    dn = np.zeros(data.shape, dtype=np.uint8)
    scale_factor = 254.0 / (max_db - min_db)

    # Scale valid range
    scaled = (db - min_db) * scale_factor + 1.0
    mask_range = valid_mask & (db >= min_db) & (db <= max_db)
    dn[mask_range] = scaled[mask_range].astype(np.uint8)

    # Clamp
    mask_low = valid_mask & (db < min_db)
    dn[mask_low] = 1
    dn[is_neg_inf] = 1

    mask_high = valid_mask & (db > max_db)
    dn[mask_high] = 255
    dn[is_pos_inf] = 255

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


def _process_single_file(h5_url, variable_names, output_dir_or_file, srcwin, projwin, transform_mode, frequency, single_bands, vrt, downscale_factor, target_align_pixels, input_fs, output_fs, is_batch=False, cache=None, keep=False, use_earthdata=False, verbose=False, target_srs=None, target_res=None, resample="cubic", output_format="COG", fill_holes=False, num_threads=None):

    h5_basename = h5_url.split("/")[-1]
    base_name = h5_basename[:-3] if h5_basename.lower().endswith(".h5") else h5_basename

    # --- Mode Setup ---
    # mode_str is used for the FILENAME and matches exact user input (e.g. "Amp")
    # logic_mode is used for the IF statement and handles case-insensitivity
    mode_str = transform_mode if transform_mode else "pwr"
    logic_mode = mode_str.lower()

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
            file_url = cache_to_local(h5_url, localdir=cache, keep=keep, use_earthdata=use_earthdata)
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

        if verbose:
            print(f"    [t] file open + metadata: {_time.perf_counter()-_t_file:.1f}s", flush=True)
            _t_file = _time.perf_counter()

        date_str = acq_meta.get("ACQUISITION_DATE", "Unknown")

        if verbose:
            mode_label = "datatree" if use_datatree else "h5py"
            print(f"    Date: {date_str} | Grid: {info['res_x']:.1f}m ({frequency}) | Mode: {mode_label}", flush=True)

        # --- REPROJECTION SETUP ---
        input_crs_obj = _parse_crs(info["crs"])
        dst_crs_obj = _parse_crs(target_srs) if target_srs else None
        needs_reproject = (dst_crs_obj is not None and dst_crs_obj != input_crs_obj)
        resample_enum = _get_resampling(resample)
        if needs_reproject and verbose:
            src_epsg = input_crs_obj.to_epsg()
            src_label = f"EPSG:{src_epsg}" if src_epsg else info["crs"][:40]
            print(f"    Reprojecting: {src_label} -> {target_srs} (resample={resample})", flush=True)

        # When reprojecting with a projwin, the projwin coords are in target_srs.
        # Expand to native-CRS subset using calculate_source_window.
        native_projwin = projwin
        if needs_reproject and projwin:
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
            pol_list_str = "".join(v[:2].lower() for v in variable_names)
            suffix = f"-EBD_{frequency}_{pol_list_str}.h5"
            h5_out_path = (final_path[:-4] if final_path.endswith(".tif") else final_path) + suffix

            if verbose:
                print(f"    Writing H5 subset ({w}x{h}) ...", flush=True)
            h5_bytes = _write_h5_subset(f, info["grid_path"], variable_names, col, row, w, h)

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
                print(f"    ✓ H5 subset: {os.path.basename(h5_out_path)}", flush=True)
            files_map_h5 = {var: h5_out_path for var in variable_names}
            return {"success": True, "h5_url": h5_url, "date": date_str,
                    "files_map": files_map_h5,
                    "info": {"w": w, "h": h, "transform": _tf, "crs": info["crs"], "dtype": "float32"}}

        # MEMORY OPTIMIZATION: When using cache with single_bands, process one variable at a time
        # to reduce memory footprint (important for large files on smaller RAM)
        use_low_memory_mode = (cache is not None and single_bands)

        if use_low_memory_mode:
            if verbose:
                print(f"    Low-memory mode: Processing {len(variable_names)} bands individually...", flush=True)
            # Process each band separately to minimize memory usage
            bands_data = None  # Don't load all bands at once
            data_stack = None  # Will process one at a time
        else:
            if verbose:
                print(f"    Extracting {len(variable_names)} bands...", flush=True)
                _t_read = _time.perf_counter()

            # OPTIMIZATION: Use datatree for faster band reading if available
            if use_datatree:
                try:
                    row_slice = slice(row, row + h)
                    col_slice = slice(col, col + w)
                    bands_data = read_variables_datatree(dt, info["grid_path"], variable_names, row_slice, col_slice)
                    if verbose:
                        print(f"    ✓ Used datatree for optimized band extraction", flush=True)
                except Exception as e:
                    if verbose:
                        print(f"    ⚠ Datatree read failed, falling back to h5py: {e}", flush=True)
                    # Fallback to h5py
                    bands_data = []
                    for var in variable_names:
                        ds_path = f"{info['grid_path']}/{var}"
                        bands_data.append(f[ds_path][row : row + h, col : col + w].astype(np.float32))  # noqa
            else:
                # Use h5py (original method)
                bands_data = []
                for var in variable_names:
                    ds_path = f"{info['grid_path']}/{var}"
                    bands_data.append(f[ds_path][row : row + h, col : col + w].astype(np.float32))  # noqa

            data_stack = np.stack(bands_data)
            if verbose:
                shape = data_stack.shape
                mb = data_stack.nbytes / 1e6
                print(f"    [t] data read ({shape[0]}×{shape[1]}×{shape[2]}, {mb:.1f} MB): {_time.perf_counter()-_t_read:.1f}s", flush=True)
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
        else:
            out_res_x = orig_res_x
            out_res_y = orig_res_y

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
        # PREDICTOR: 3=floating-point, 2=integer — improves deflate speed & ratio
        # pwr/dB output is float32; AMP is uint16; DN is uint8
        _predictor = 3 if transform_mode.lower() in ("pwr", "db") else 2
        _write_extra = {"num_threads": _n_th, "predictor": _predictor}

        # === LOW MEMORY MODE: Process each band individually ===
        if use_low_memory_mode:
            if verbose:
                print(f"    Processing bands individually to save memory...", flush=True)

            generated_files = []
            for i, var in enumerate(variable_names):
                if verbose:
                    print(f"      [{i+1}/{len(variable_names)}] Processing {var}...", flush=True)

                # Read single band
                if use_datatree:
                    try:
                        row_slice = slice(row, row + h)
                        col_slice = slice(col, col + w)
                        band_data = read_variables_datatree(dt, info["grid_path"], [var], row_slice, col_slice)[0]
                    except Exception as e:
                        if verbose and i == 0:  # Only print warning once
                            print(f"        ⚠ Datatree read failed, using h5py: {e}", flush=True)
                        ds_path = f"{info['grid_path']}/{var}"
                        band_data = f[ds_path][row : row + h, col : col + w].astype(np.float32)  # noqa
                else:
                    ds_path = f"{info['grid_path']}/{var}"
                    band_data = f[ds_path][row : row + h, col : col + w].astype(np.float32)  # noqa

                # Reshape for downscaling (add band dimension)
                band_data = band_data[np.newaxis, :, :]

                # Downscale
                if downscale_factor and downscale_factor > 1:
                    band_data = perform_downscaling(band_data, downscale_factor)

                # Reproject (on raw power data, before dtype conversion)
                if needs_reproject and warp_kw is not None:
                    if verbose:
                        print(f"        Reprojecting {var}...", flush=True)
                    band_data_2d = _reproject_power_band(band_data[0], **warp_kw)
                    band_data = band_data_2d[np.newaxis, :, :]

                # Get output dimensions
                _, h_out, w_out = band_data.shape

                # Transform
                if logic_mode == "amp":
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

                # Write
                pol_str = var[:2].lower()
                suffix = f"-EBD_{frequency}_{pol_str}_{mode_str}.tif"

                if final_path.endswith(".tif"):
                    band_path = final_path[:-4] + suffix
                else:
                    band_path = final_path + suffix

                profile = {"driver": _driver, "height": h_out, "width": w_out, "count": 1, "dtype": output_dtype, "crs": out_crs, "transform": out_transform, "compress": "deflate", "nodata": output_nodata, **_gtiff_extra, **_write_extra}

                with rasterio.Env(GDAL_NUM_THREADS=_n_th):
                    with MemoryFile() as memfile:
                        with memfile.open(**profile) as dst:
                            dst.write(band_data, 1)
                            dst.set_band_description(1, var)
                            dst.update_tags(**acq_meta)
                            dst.update_tags(1, Date=date_str)
                        memfile.seek(0)
                        write_bytes(band_path, memfile.read())
                        files_map[var] = band_path
                        generated_files.append(band_path)

                # Free memory immediately
                del band_data

            # VRT generation for low-memory mode
            if vrt:
                pol_list_str = "".join([v[:2].lower() for v in variable_names])
                vrt_suffix = f"-EBD_{frequency}_{pol_list_str}_{mode_str}.vrt"

                if final_path.endswith(".tif"):
                    vrt_path = final_path[:-4] + vrt_suffix
                else:
                    vrt_path = final_path + vrt_suffix

                # Note: Pass output_dtype to ensure correct VRT Mapping (e.g. uint8 -> Byte)
                vrt_xml = generate_vrt_xml_single_step(w_out, h_out, out_transform, out_crs, generated_files, variable_names, date_str, dtype=output_dtype)
                if verbose:
                    print(f"    Generated Snapshot VRT: {vrt_path}", flush=True)
                write_bytes(vrt_path, vrt_xml.encode("utf-8"))

        # === NORMAL MODE: Process all bands at once ===
        else:
            if downscale_factor and downscale_factor > 1:
                if verbose:
                    print(f"    Downscaling by {downscale_factor}...", flush=True)
                data_stack = perform_downscaling(data_stack, downscale_factor)

            # Reproject (on raw power data, before dtype conversion)
            if needs_reproject and warp_kw is not None:
                if verbose:
                    print(f"    Reprojecting {len(variable_names)} bands...", flush=True)
                n_bands = data_stack.shape[0]
                warped = np.full((n_bands, warp_kw["dst_height"], warp_kw["dst_width"]), np.nan, dtype=np.float32)
                for bi in range(n_bands):
                    warped[bi] = _reproject_power_band(data_stack[bi], **warp_kw)
                data_stack = warped

            _, h_out, w_out = data_stack.shape

            # --- TRANSFORM LOGIC ---
            if verbose:
                print(f"    Transforming: {mode_str} (Mode: {logic_mode})", flush=True)

            if logic_mode == "amp":
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
                # No Transform ("pwr" or None)
                processed_data = data_stack
                output_dtype = "float32"
                output_nodata = np.nan

            if single_bands:
                if verbose:
                    print("    Writing separate bands...", flush=True)
                    _t_write = _time.perf_counter()
                generated_files = []
                for i, var in enumerate(variable_names):
                    pol_str = var[:2].lower()
                    suffix = f"-EBD_{frequency}_{pol_str}_{mode_str}.tif"

                    if final_path.endswith(".tif"):
                        band_path = final_path[:-4] + suffix
                    else:
                        band_path = final_path + suffix

                    band_data = processed_data[i, :, :]

                    profile = {"driver": _driver, "height": h_out, "width": w_out, "count": 1, "dtype": output_dtype, "crs": out_crs, "transform": out_transform, "compress": "deflate", "nodata": output_nodata, **_gtiff_extra}

                    with MemoryFile() as memfile:
                        with memfile.open(**profile) as dst:
                            dst.write(band_data, 1)
                            dst.set_band_description(1, var)
                            dst.update_tags(**acq_meta)
                            dst.update_tags(1, Date=date_str)
                        memfile.seek(0)
                        write_bytes(band_path, memfile.read())
                        files_map[var] = band_path
                        generated_files.append(band_path)

                if verbose:
                    sz = sum(os.path.getsize(p) for p in generated_files if os.path.isfile(p))
                    print(f"    [t] COG write ({len(generated_files)} bands, {sz/1e6:.1f} MB): {_time.perf_counter()-_t_write:.1f}s", flush=True)

                if vrt:
                    pol_list_str = "".join([v[:2].lower() for v in variable_names])
                    vrt_suffix = f"-EBD_{frequency}_{pol_list_str}_{mode_str}.vrt"

                    if final_path.endswith(".tif"):
                        vrt_path = final_path[:-4] + vrt_suffix
                    else:
                        vrt_path = final_path + vrt_suffix

                    # Note: Pass output_dtype to ensure correct VRT Mapping (e.g. uint8 -> Byte)
                    vrt_xml = generate_vrt_xml_single_step(w_out, h_out, out_transform, out_crs, generated_files, variable_names, date_str, dtype=output_dtype)
                    if verbose:
                        print(f"    Generated Snapshot VRT: {vrt_path}", flush=True)
                    write_bytes(vrt_path, vrt_xml.encode("utf-8"))
            else:
                if verbose:
                    print("    Writing Multi-band COG...", flush=True)
                    _t_write = _time.perf_counter()
                profile = {"driver": _driver, "height": h_out, "width": w_out, "count": len(variable_names), "dtype": output_dtype, "crs": out_crs, "transform": out_transform, "compress": "deflate", "nodata": output_nodata, **_gtiff_extra, **_write_extra}
                with rasterio.Env(GDAL_NUM_THREADS=_n_th):
                    with MemoryFile() as memfile:
                        with memfile.open(**profile) as dst:
                            dst.write(processed_data)
                            for i, var in enumerate(variable_names):
                                dst.set_band_description(i + 1, var)
                                dst.update_tags(i + 1, Date=date_str)
                            dst.update_tags(**acq_meta)
                        memfile.seek(0)
                        write_bytes(final_path, memfile.read())

                for var in variable_names:
                    files_map[var] = final_path
                if verbose:
                    sz = os.path.getsize(final_path) if os.path.isfile(final_path) else 0
                    print(f"    [t] COG write ({len(variable_names)} bands, {sz/1e6:.1f} MB): {_time.perf_counter()-_t_write:.1f}s", flush=True)

        if verbose:
            memory_mode = "low-memory (per-band)" if use_low_memory_mode else "standard (all-bands)"
            print(f"    ✓ Complete ({memory_mode} mode)", flush=True)

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


def process_chunk_task(h5_url, variable_names, output_path, srcwin=None, projwin=None, transform_mode="db", frequency="A", single_bands=False, vrt=False, downscale_factor=None, target_align_pixels=False, input_auth=None, output_auth=None, time_series_vrt=True, list_grids=False, cache=None, keep=False, verbose=False, target_srs=None, target_res=None, resample="cubic", output_format="COG", fill_holes=False, num_threads=None):

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
        # and ignores input_fs — skip the create_s3_fs call (and its login round-trip).
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
                        print(f"    Resolution: X={details['res_x']:.2f}, Y={details['res_y']:.2f}")
                        # Bbox
                        bbox = details["bbox"]
                        print(f"    Extent (W,S,E,N): [{bbox[0]:.2f}, {bbox[1]:.2f}, {bbox[2]:.2f}, {bbox[3]:.2f}]")
                        # Exact Footprints
                        print(f"    Footprint (Lon/Lat): {details.get('poly_geo', 'N/A')}")
                        print(f"    Footprint (Native):  {details.get('poly_native', 'N/A')}")
                        print(f"    Frame Size:          {details.get('dims', 'N/A')}")
                        print(f"    Variables: {', '.join(details['vars'])}")
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
            res = _process_single_file(url, variable_names, output_path, srcwin, projwin, transform_mode, frequency, single_bands, vrt, downscale_factor, target_align_pixels, input_fs, output_fs, is_batch=is_batch, cache=cache, keep=keep, use_earthdata=use_earthdata, verbose=verbose, target_srs=target_srs, target_res=target_res, resample=resample, output_format=output_format, fill_holes=fill_holes, num_threads=num_threads)
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

            for var_idx, var in enumerate(variable_names):
                pol_str = var[:2].lower()
                stack_items = []
                dates_list = []
                for r in valid_results:
                    fpath = r["files_map"][var]
                    b_idx = 1 if single_bands else (var_idx + 1)
                    item = {"path": fpath, "band_idx": b_idx, "date": r["date"]}
                    if not all_same_geom:
                        item["transform"] = r["info"]["transform"]
                        item["w"] = r["info"]["w"]
                        item["h"] = r["info"]["h"]
                    stack_items.append(item)
                    dates_list.append(r["date"].replace("-", ""))

                # Note: Pass output_dtype to ensure correct VRT Mapping (e.g. uint8 -> Byte)
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

            return f"Batch Complete. Generated {len(variable_names)} Time Series VRTs."

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


def rebuild_vrts(output_path, variable_names, transform_mode="AMP", frequency="A", auth_config=None, verbose=True):
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

    # Get the polarizations if variable_names is None:
    if variable_names is None:
        pols = {x.split("_")[-2][:2].upper() for x in tif_files}
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
        key_pol = pol_found[:2]

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
            k = var[:2].upper()
            if k in day_files:
                ordered_files.append(day_files[k]["path"])
                ordered_vars.append(var)
                if base_filename is None:
                    base_filename = day_files[k]["filename"]

        if not ordered_files:
            continue

        pol_list_str = "".join([v[:2].lower() for v in variable_names])
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
        print("✓ OPTIMIZED MODE: datatree available for fast HDF5 access")
        print("  - Lazy loading with spatial subsetting")
        print("  - Parallel band extraction")
        print("  - Reduced memory footprint")
    else:
        print("⚠ FALLBACK MODE: Using h5py (slower)")
        print("  Install datatree for better performance:")
        print("    pip install xarray-datatree")
        print("    or: pip install 'xarray>=2024.2.0'")
