"""
openseppo.nisar.nisar_tools_gslc -- NISAR GSLC processing core
***************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Core library for reading NISAR GSLC (Geocoded Single-Look Complex) HDF5 files
and converting them to Cloud Optimized GeoTIFF (COG).  Supports:

  * Power intensity (|z|^2, float32)  -- default
  * Amplitude       (|z|,   float32)
  * Wrapped phase   (angle(z), float32, radians)

Spatial subsetting via srcwin or projwin, optional downscaling, reprojection,
interior hole filling, and VRT time-series stacking are all supported.
Local and remote (S3/HTTPS) sources are handled via earthaccess / s3fs.

Shared authentication, filesystem, reprojection, downscaling, and VRT helpers
are imported directly from nisar_tools to avoid duplication.
"""

import sys
import os
import gc
import math
import re
import tempfile
import numpy as np
import h5py
import rasterio
from rasterio.warp import calculate_default_transform
from rasterio.transform import from_origin
from rasterio.io import MemoryFile

# ---------------------------------------------------------------------------
# Import shared utilities from the GCOV tools.  Only GSLC-specific logic
# lives in this module; everything else is delegated to nisar_tools.
# ---------------------------------------------------------------------------
import openseppo.nisar.nisar_tools as nisar_tools
from openseppo.nisar.nisar_tools import (
    # Authentication / filesystem
    create_s3_fs,
    _earthaccess_login,
    HAS_EARTHACCESS,
    # HDF5 access
    open_h5_lazy,
    _geo_grid_bounding_polygon_wkt,
    open_datatree_lazy,
    _decode_h5_scalar,
    _ensure_utm_south,
    # Reprojection helpers
    _parse_crs,
    _get_resampling,
    _fill_nodata_nn,
    _reproject_power_band,
    calculate_source_window,
    get_indices_from_extent,
    # Downscaling
    _downscale_block,
    perform_downscaling,
    # VRT helpers
    get_gdal_dtype,
    generate_vrt_xml_single_step,
    generate_vrt_xml_timeseries,
    generate_vrt_xml_timeseries_union,
    # Scaling helpers shared with GCOV
    pwr_to_amp,
    # Caching / download
    cache_to_local,
    construct_timeseries_filename,
    # Misc
    _RESAMPLE_PREDOWNSCALE_DIVISOR,
)


# ---------------------------------------------------------------------------
# GSLC-specific constants
# ---------------------------------------------------------------------------

GSLC_GRID_BASE = "/science/LSAR/GSLC/grids"
GSLC_PRODUCT   = "GSLC"


# =========================================================
# 1. HDF5 INSPECTION & GRID INFO
# =========================================================


def inspect_h5_structure_gslc(f):
    """
    Scan a GSLC HDF5 file and return a dict keyed by frequency code.

    Each value contains CRS, resolution, bbox, a list of complex polarisation
    variable names, per-variable dtype/nodata, and a geographic footprint.
    """
    structure = {}
    base_path = GSLC_GRID_BASE

    # --- bounding polygon from identification group (same as GCOV) ---
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
                sums  = [p[0] + p[1] for p in points]
                diffs = [p[0] - p[1] for p in points]
                ordered_indices = [np.argmin(diffs), np.argmax(sums), np.argmax(diffs), np.argmin(sums)]
                cached_geo_corners = [points[i] for i in ordered_indices]
                poly_geo_display = ", ".join([f"({p[0]:.4f}, {p[1]:.4f})" for p in cached_geo_corners])
        except Exception as e:
            poly_geo_display = f"Metadata Error: {e}"

    if base_path not in f:
        return {"error": f"Path {base_path} not found in H5 (not a GSLC file?)."}

    from rasterio.warp import transform as _warp_transform

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

            x_ds = f[f"{g_path}/xCoordinates"]
            y_ds = f[f"{g_path}/yCoordinates"]
            x0, x1 = x_ds[0], x_ds[-1]
            y0, y1 = y_ds[0], y_ds[-1]
            res_x = x_ds[1] - x_ds[0]
            res_y = y_ds[1] - y_ds[0]
            ncols = len(x_ds)
            nrows = len(y_ds)

            min_x = min(x0, x1) - abs(res_x) / 2.0
            max_x = max(x0, x1) + abs(res_x) / 2.0
            min_y = min(y0, y1) - abs(res_y) / 2.0
            max_y = max(y0, y1) + abs(res_y) / 2.0

            poly_native_display = "No footprint found"
            dims_km = "N/A"

            if cached_geo_corners:
                try:
                    lons = [p[0] for p in cached_geo_corners]
                    lats = [p[1] for p in cached_geo_corners]
                    xs, ys = _warp_transform("EPSG:4326", crs, lons, lats)
                    poly_native_display = ", ".join([f"({x:.2f}, {y:.2f})" for x, y in zip(xs, ys)])
                    w1 = math.sqrt((xs[1]-xs[0])**2 + (ys[1]-ys[0])**2)
                    w2 = math.sqrt((xs[2]-xs[3])**2 + (ys[2]-ys[3])**2)
                    h1 = math.sqrt((xs[3]-xs[0])**2 + (ys[3]-ys[0])**2)
                    h2 = math.sqrt((xs[2]-xs[1])**2 + (ys[2]-ys[1])**2)
                    unique_corners = len({(round(x, 1), round(y, 1)) for x, y in zip(xs, ys)})
                    if unique_corners < 4:
                        dims_km = (f"~{(max_x-min_x)/1000:.2f} km x ~{(max_y-min_y)/1000:.2f} km"
                                   f" (footprint polygon incomplete; showing raster extent)")
                    else:
                        dims_km = (f"Width: {(w1+w2)/2/1000:.2f} km, "
                                   f"Height: {(h1+h2)/2/1000:.2f} km")
                except Exception as e:
                    poly_native_display = f"Reprojection Failed: {e}"

            # --- Collect complex polarisation variables ---
            _skip = {"projection", "xCoordinates", "yCoordinates",
                     "listOfPolarizations", "validSamplesSubSwath"}
            variables = []
            var_details = {}
            for item in f[g_path].keys():
                if item in _skip:
                    continue
                obj = f[f"{g_path}/{item}"]
                if isinstance(obj, h5py.Dataset) and len(obj.shape) >= 2:
                    variables.append(item)
                    _dt = str(obj.dtype)
                    if "_FillValue" in obj.attrs:
                        _fv_str = str(obj.attrs["_FillValue"])
                    else:
                        _fv_str = "none"
                    var_details[item] = {"dtype": _dt, "nodata": _fv_str}

            structure[freq_code] = {
                "crs": crs, "ncols": ncols, "nrows": nrows,
                "res_x": float(res_x), "res_y": float(res_y),
                "bbox": (min_x, min_y, max_x, max_y),
                "vars": sorted(variables), "var_details": var_details,
                "poly_geo": poly_geo_display, "poly_native": poly_native_display,
                "dims": dims_km,
            }
        except Exception as e:
            structure[freq_code] = {"error": str(e)}

    return structure


def get_grid_info_gslc(h5_handle, frequency="A"):
    """Read grid coordinates and projection from a GSLC HDF5 handle."""
    grid_path = f"{GSLC_GRID_BASE}/frequency{frequency}"
    try:
        x_ds = h5_handle[f"{grid_path}/xCoordinates"]
        y_ds = h5_handle[f"{grid_path}/yCoordinates"]
        proj_val = h5_handle[f"{grid_path}/projection"][()]
    except KeyError:
        raise KeyError(f"Grid path '{grid_path}' not found in GSLC file.")

    if hasattr(proj_val, "decode"):
        projection = _ensure_utm_south(proj_val.decode(), h5_handle)
    else:
        projection = f"EPSG:{proj_val}"

    nx = x_ds.shape[0]
    ny = y_ds.shape[0]
    x01 = x_ds[0:2]
    y01 = y_ds[0:2]
    res_x = float(x01[1] - x01[0])
    res_y = float(y01[1] - y01[0])
    x0, y0 = float(x01[0]), float(y01[0])
    x_coords = np.arange(nx, dtype=np.float64) * res_x + x0
    y_coords = np.arange(ny, dtype=np.float64) * res_y + y0

    return {
        "x": x_coords, "y": y_coords,
        "res_x": res_x, "res_y": res_y,
        "crs": projection, "grid_path": grid_path, "freq": frequency,
    }


def get_grid_info_from_datatree_gslc(dt, frequency="A"):
    """Read grid metadata from an open datatree for GSLC."""
    grid_path = f"science/LSAR/GSLC/grids/frequency{frequency}"
    try:
        node = dt[grid_path]
        ds = node.ds
        x_coords = ds["xCoordinates"].values
        y_coords = ds["yCoordinates"].values
        proj_val = ds["projection"].values
        if hasattr(proj_val, "decode"):
            raw_proj = proj_val.decode()
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
        return {
            "x": x_coords, "y": y_coords,
            "res_x": x_coords[1] - x_coords[0], "res_y": y_coords[1] - y_coords[0],
            "crs": projection, "grid_path": full_grid_path, "freq": frequency,
        }
    except Exception:
        return None


def get_acquisition_metadata_gslc(h5_handle):
    """Read acquisition date/time/CRID from a GSLC HDF5 handle."""
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
        try:
            meta["CRID"] = _decode_h5_scalar(
                h5_handle["/science/LSAR/identification/compositeReleaseID"][()]
            )
        except Exception:
            pass
        try:
            meta["ISCE3_VERSION"] = _decode_h5_scalar(
                h5_handle[f"/science/LSAR/{GSLC_PRODUCT}/metadata/processingInformation/"
                          "algorithms/softwareVersion"][()]
            )
        except Exception:
            pass
        return meta
    except Exception:
        return {}


def get_acquisition_metadata_from_datatree_gslc(dt):
    """Read acquisition metadata from a datatree for GSLC."""
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
        try:
            meta["CRID"] = _decode_h5_scalar(ds["compositeReleaseID"].values)
        except Exception:
            pass
        try:
            sw = dt[f"science/LSAR/{GSLC_PRODUCT}/metadata/processingInformation/algorithms"].ds
            meta["ISCE3_VERSION"] = _decode_h5_scalar(sw["softwareVersion"].values)
        except Exception:
            pass
        return meta
    except Exception:
        return None


# =========================================================
# 2. COMPLEX TRANSFORM FUNCTIONS
# =========================================================


def complex_to_power(z):
    """Power intensity: |z|^2 as float32.  Zero (GSLC nodata) and non-finite -> NaN."""
    nodata_mask = (z.real == 0) & (z.imag == 0)
    mag = np.abs(z.astype(np.complex64)).astype(np.float32)
    out = mag * mag
    out[nodata_mask] = np.nan
    out[~np.isfinite(out)] = np.nan
    return out


def complex_to_magnitude(z):
    """Raw magnitude: |z| as float32.  Zero (GSLC nodata) and non-finite ->NaN."""
    nodata_mask = (z.real == 0) & (z.imag == 0)
    out = np.abs(z.astype(np.complex64)).astype(np.float32)
    out[nodata_mask] = np.nan
    out[~np.isfinite(out)] = np.nan
    return out


def complex_to_amp_uint16(z):
    """Scaled amplitude: same formula as GCOV -amp (uint16, nodata=0).

    Pipeline: complex -> power (|z|^2) -> pwr_to_amp(power) -> uint16.
    Matches the GCOV amplitude scaling exactly so GSLC and GCOV outputs
    are directly comparable.
    """
    pwr = complex_to_power(z)   # float32, nodata=NaN
    return pwr_to_amp(pwr)      # uint16, nodata=0


def complex_to_phase(z):
    """Wrapped phase in radians (-pi ... pi) as float32.  Zero-valued samples -> NaN."""
    # Pixels where both real and imag are 0 are nodata
    nodata_mask = (z.real == 0) & (z.imag == 0)
    out = np.angle(z.astype(np.complex64)).astype(np.float32)
    out[nodata_mask] = np.nan
    return out


# =========================================================
# 3. FULL-TREE VARIABLE LISTING
# =========================================================


def list_h5_variables(f):
    """Return a sorted flat list of all paths in an open HDF5 file."""
    paths = []
    f.visit(paths.append)
    return sorted(paths)


# =========================================================
# 3. COMPLEX BAND READER
# =========================================================


def _read_gslc_bands(file_url, input_fs, grid_path, variable_names, row, h, col, w,
                     with_mask=False):
    """
    Read GSLC complex bands from an open HDF5 file, preserving the native
    complex dtype (usually complex64).

    Returns ``(arrays, mask)`` where *arrays* is the list of 2-D complex64 bands
    in the order of *variable_names*, and *mask* is the 2-D subswath ``mask`` grid
    (uint8) for the same window when *with_mask* is True and the grid is present,
    else None.  Reading the mask here reuses the same open file handle as the
    bands -- no extra HDF5 open.

    For S3/HTTPS sources the caller should cache the file locally first
    (cache=True) to avoid repeated small range-requests.
    """
    fh = open_h5_lazy(file_url, input_fs)
    try:
        arrays = []
        for var in variable_names:
            ds_path = f"{grid_path}/{var}"
            data = fh[ds_path][row: row + h, col: col + w]
            # Keep complex; cast to complex64 to normalise memory
            arrays.append(data.astype(np.complex64))
        mask_arr = None
        if with_mask:
            mp = f"{grid_path}/mask"
            if mp in fh and fh[mp].ndim == 2:
                mask_arr = fh[mp][row: row + h, col: col + w]
    finally:
        fh.close()
    return arrays, mask_arr


def _retarget_swath_extents(dst, meta_base, verbose=False):
    """Narrow sourceData/swaths and the identification zero-Doppler times from
    the full source granule to the window actually written.

    The metadata copy brings ``metadata/sourceData/swaths`` across verbatim
    because, by the NISAR spec, that group describes the *input* L1 RSLC rather
    than the geocoded product -- which is why even a pristine granule ships a
    52648 x 30400 swath alongside a 67824 x 66384 grid.  Readers that build
    radar-geometry parameters from those fields nonetheless take
    ``numberOfRangeSamples`` / ``numberOfAzimuthLines`` as the product's sample
    counts, so a subset that inherits them advertises dimensions contradicting
    its own geocoded grid.

    The already-subsetted ``metadata/radarGrid`` cube carries slantRange,
    zeroDopplerAzimuthTime and incidenceAngle over the window, so every field
    here is derived from that one source and stays mutually consistent -- the
    reason the earlier pass left them alone was that changing one would desync
    it from ``slantRangeStart`` in the same group.

    The window is a map-projected rectangle whose radar footprint is a skewed
    quadrilateral, so what this can offer is the *bounding* radar window; the
    ``description`` attribute of each rewritten field is amended to say so
    rather than letting the narrowed value read as exact.

    Leaves the fields untouched (and warns) if the cube is missing or all-NaN.
    """
    rg_path = f"{meta_base}/radarGrid"
    sw_path = f"{meta_base}/sourceData/swaths"
    if rg_path not in dst or sw_path not in dst:
        return
    rg, sw = dst[rg_path], dst[sw_path]

    def _extent(name):
        """Finite (min, max) of a radarGrid cube, or None if unusable."""
        if name not in rg:
            return None
        a = np.asarray(rg[name][()], dtype="float64")
        a = a[np.isfinite(a)]
        return (float(a.min()), float(a.max())) if a.size else None

    sr, az, inc = _extent("slantRange"), _extent("zeroDopplerAzimuthTime"), \
        _extent("incidenceAngle")
    if sr is None or az is None:
        print("    [WARN] radarGrid carries no finite slantRange / azimuth "
              "time; sourceData/swaths left at the full-granule extent.",
              flush=True)
        return

    # zeroDopplerAzimuthTime is 'seconds since <ISO epoch>'; without the epoch
    # the cube's times cannot be turned back into the UTC strings the swath
    # and identification fields hold.
    units = rg["zeroDopplerAzimuthTime"].attrs.get("units", b"")
    if hasattr(units, "decode"):
        units = units.decode()
    if "since" not in units:
        print("    [WARN] radarGrid/zeroDopplerAzimuthTime has no 'seconds "
              "since <epoch>' units; sourceData/swaths left at the "
              "full-granule extent.", flush=True)
        return
    epoch = np.datetime64(units.split("since", 1)[1].strip(), "ns")

    def _iso(seconds):
        """UTC string in the products' YYYY-mm-ddTHH:MM:SS.sssssssss form."""
        return str(epoch + np.timedelta64(int(round(seconds * 1e9)), "ns"))

    note = "narrowed to the geocoded subset window (bounding radar extent)"

    def _set(grp, name, value):
        """Overwrite one field, preserving dtype and attrs, and record in the
        description that the value now describes the window."""
        if name not in grp:
            return
        old = grp[name]
        attrs, dtype = dict(old.attrs), old.dtype
        del grp[name]
        data = np.bytes_(value) if dtype.kind == "S" \
            else np.asarray(value, dtype=dtype)
        d = grp.create_dataset(name, data=data)
        for k, v in attrs.items():
            d.attrs[k] = v
        desc = attrs.get("description")
        if desc is not None:
            if hasattr(desc, "decode"):
                desc = desc.decode()
            d.attrs["description"] = np.bytes_(f"{desc} [{note}]")

    # The cube is written with a margin so it fully brackets the window, which
    # means its extremes reach a little past the granule itself -- left alone,
    # the subset would claim an azimuth end after the source's own.  A subset
    # spans no more than its source, so clamp every extent to the inherited
    # value before it is overwritten.
    def _sec(iso):
        if hasattr(iso, "decode"):
            iso = iso.decode()
        return float((np.datetime64(iso, "ns") - epoch)
                     / np.timedelta64(1, "s"))

    if "zeroDopplerStartTime" in sw and "zeroDopplerEndTime" in sw:
        az = (max(az[0], _sec(sw["zeroDopplerStartTime"][()])),
              min(az[1], _sec(sw["zeroDopplerEndTime"][()])))

    if "zeroDopplerTimeSpacing" in sw:
        ts = float(sw["zeroDopplerTimeSpacing"][()])
        if ts > 0:
            _set(sw, "numberOfAzimuthLines",
                 int(round((az[1] - az[0]) / ts)) + 1)
    _set(sw, "zeroDopplerStartTime", _iso(az[0]))
    _set(sw, "zeroDopplerEndTime", _iso(az[1]))

    # Slant range geometry is frequency-independent, but the sample count is
    # not: each frequency counts the same range span at its own spacing.
    for fq in [k for k in sw if k.startswith("frequency")]:
        fg = sw[fq]
        sp = float(fg["slantRangeSpacing"][()]) \
            if "slantRangeSpacing" in fg else 0.0
        sr_f = sr
        if sp > 0 and "slantRangeStart" in fg and "numberOfRangeSamples" in fg:
            s0 = float(fg["slantRangeStart"][()])
            s1 = s0 + (int(fg["numberOfRangeSamples"][()]) - 1) * sp
            sr_f = (max(sr[0], s0), min(sr[1], s1))
        _set(fg, "slantRangeStart", sr_f[0])
        if sp > 0:
            _set(fg, "numberOfRangeSamples",
                 int(round((sr_f[1] - sr_f[0]) / sp)) + 1)
        if inc is not None:
            inc_f = inc
            if "nearRangeIncidenceAngle" in fg \
                    and "farRangeIncidenceAngle" in fg:
                inc_f = (max(inc[0], float(fg["nearRangeIncidenceAngle"][()])),
                         min(inc[1], float(fg["farRangeIncidenceAngle"][()])))
            _set(fg, "nearRangeIncidenceAngle", inc_f[0])
            _set(fg, "farRangeIncidenceAngle", inc_f[1])

    # identification's times describe "the product" (this subset), not the
    # source, so narrowing them needs no caveat beyond the same derivation.
    ident = "/science/LSAR/identification"
    if ident in dst:
        _set(dst[ident], "zeroDopplerStartTime", _iso(az[0]))
        _set(dst[ident], "zeroDopplerEndTime", _iso(az[1]))

    if verbose:
        print(f"    Swath extents retargeted to window: "
              f"slant range {sr[0]:.1f}-{sr[1]:.1f} m, "
              f"{_iso(az[0])[11:]}-{_iso(az[1])[11:]}", flush=True)


def _subset_gslc(src_f, grid_path, variable_names, col, row, w, h,
                 all_frequencies=True, src_url=None, auth_config=None,
                 read_workers=1, verbose=False):
    """
    Return bytes of a fully self-contained GSLC HDF5 subset with all
    metadata required by isce3, GAMMA Remote Sensing, and SEPPO.

    Curated + subsetted analogue of _subset_rslc (radar-coord RSLC): small
    groups are copied verbatim, and every grid-borne metadata array is sliced
    to the window on its own geocoded x/y axes rather than deep-copied.

    Preserves complex64 dtype and all attributes exactly as they appear
    in the source file.  Uses explicit construction (not deep-copy).

    Returns ``(bytes, written_freqs)``.

    With *all_frequencies* (the default) every frequency group present in the
    source is written, each windowed on its own x/y axes -- frequencies do not
    share a grid, so the caller's (col, row, w, h) applies only to *grid_path*
    and the rest are recomputed from the same map extent.  Set it False to
    restrict the product to *grid_path* alone.

    The output contains:
      * Root-level attributes
      * /science/LSAR/identification/ (all fields; boundingPolygon recomputed
        for the window, listOfFrequencies set to what was written)
      * /science/LSAR/GSLC/metadata/orbit/ (verbatim)
      * /science/LSAR/GSLC/metadata/attitude/ (verbatim, if present)
      * /science/LSAR/GSLC/metadata/processingInformation/ (verbatim)
      * /science/LSAR/GSLC/metadata/calibrationInformation/ (verbatim, if present)
      * /science/LSAR/GSLC/grids/frequency{A|B}/:
        - xCoordinates / yCoordinates (subsetted)
        - projection
        - listOfPolarizations (updated)
        - validSamplesSubSwath (subsetted, if present)
        - every 2-D array on the frequency's own grid -- SLC variables, mask,
          inputDataExceptionMask -- subsetted, source compression preserved
        - scalars (spacing, bandwidth, ...) verbatim

    zeroDopplerStartTime/EndTime and metadata/sourceData/swaths are narrowed
    from the granule values to the window by _retarget_swath_extents, which
    reads the azimuth time and slant range off the subsetted radarGrid cube
    rather than trying to infer them from the northing axis.
    """
    import numpy as np

    fd, tmp_path = tempfile.mkstemp(suffix=".h5")
    os.close(fd)
    try:
        with h5py.File(tmp_path, "w") as dst:

            def _cpattr(src, dst_obj):
                for k, v in src.attrs.items():
                    try:
                        dst_obj.attrs[k] = v
                    except Exception:
                        pass

            def _cpds(path, dst_grp, name=None):
                """Copy one dataset verbatim."""
                ds = src_f[path]
                d = dst_grp.create_dataset(name or path.split("/")[-1],
                                            data=ds[()])
                _cpattr(ds, d)

            def _cpgrp(path, dst_parent, name=None):
                """Recursively copy a group with all datasets and attrs."""
                src_grp = src_f[path]
                g = dst_parent.require_group(name or path.split("/")[-1])
                _cpattr(src_grp, g)
                for item in src_grp:
                    child = src_grp[item]
                    if isinstance(child, h5py.Group):
                        _cpgrp(f"{path}/{item}", g, item)
                    elif isinstance(child, h5py.Dataset):
                        _cpds(f"{path}/{item}", g)

            def _subset_meta_grid(src_grp, dst_parent, name, extent, margin=2,
                                  collect=None, prefetch=None):
                """Copy a metadata group, subsetting any dataset that lives on
                the group's own (yCoordinates, xCoordinates) grid to *extent* =
                [ulx, uly, lrx, lry] (product map CRS).

                radarGrid holds full-frame geolocation cubes (~470 MB, shape
                (height, y, x)); copying them verbatim over HTTPS was the source
                of the h5-write timeout.  Here each grid-borne array is sliced
                to the window with the same get_indices_from_extent used for the
                SLC data, so descending-Y orientation is handled.  The height
                axis, scalars and non-grid arrays are copied verbatim.  Groups
                without x/y axes fall through to a verbatim copy.

                Two modes share this traversal so the plan can never drift from
                what is written:

                *collect*   append ``(path, slice, nbytes)`` for every dataset
                            and write nothing -- the plan for a parallel read.
                *prefetch*  ``{path: ndarray}`` of values already fetched;
                            anything missing is read from this handle.
                """
                planning = collect is not None
                g = None if planning else dst_parent.require_group(name)
                if not planning:
                    _cpattr(src_grp, g)

                gx = src_grp["xCoordinates"][()] if "xCoordinates" in src_grp else None
                gy = src_grp["yCoordinates"][()] if "yCoordinates" in src_grp else None
                if gx is not None and gy is not None:
                    nx, ny = len(gx), len(gy)
                    c, r, ww, hh = get_indices_from_extent(gx, gy, extent)
                    # get_indices_from_extent snaps to the nearest node; pad so
                    # the coarse grid still brackets the window edges.
                    c = max(0, c - margin); r = max(0, r - margin)
                    ww = min(nx - c, ww + 2 * margin); hh = min(ny - r, hh + 2 * margin)
                else:
                    nx = ny = None

                for k in src_grp:
                    child = src_grp[k]
                    if isinstance(child, h5py.Group):
                        _subset_meta_grid(child, g, k, extent, margin,
                                          collect=collect, prefetch=prefetch)
                        continue
                    ds = child

                    # The slice this dataset needs, as a picklable spec.
                    if nx and k == "xCoordinates":
                        sl = (slice(c, c + ww),)
                    elif ny and k == "yCoordinates":
                        sl = (slice(r, r + hh),)
                    elif ny and ds.ndim >= 2 and ds.shape[-2:] == (ny, nx):
                        sl = (Ellipsis, slice(r, r + hh), slice(c, c + ww))
                    else:
                        sl = None

                    if planning:
                        nbytes = int(np.prod(ds.shape or (1,))) * ds.dtype.itemsize
                        collect.append((ds.name, sl, nbytes))
                        continue

                    if prefetch is not None and ds.name in prefetch:
                        data = prefetch[ds.name]
                    else:
                        data = ds[()] if sl is None else ds[sl]
                    _cpattr(ds, g.create_dataset(k, data=data))

            # --- Root attributes ---
            _cpattr(src_f, dst)

            # ============================================================
            # /science/LSAR/identification/
            # ============================================================
            ident_src = "/science/LSAR/identification"
            if ident_src in src_f:
                _cpgrp(ident_src, dst, ident_src.lstrip("/"))

                # Replace the inherited full-granule boundingPolygon with one
                # describing the subset window, following the isce3 geocoded-grid
                # convention (see _geo_grid_bounding_polygon_wkt).
                id_grp = dst[ident_src.lstrip("/")]
                bp_path = f"{ident_src}/boundingPolygon"
                bp_src = src_f[bp_path] if bp_path in src_f else None
                try:
                    x_sub = src_f[f"{grid_path}/xCoordinates"][col: col + w]
                    y_sub = src_f[f"{grid_path}/yCoordinates"][row: row + h]
                    proj_val = src_f[f"{grid_path}/projection"][()]
                    if hasattr(proj_val, "decode"):
                        crs_str = proj_val.decode()
                    else:
                        crs_str = f"EPSG:{proj_val}"

                    def _spacing(key, arr):
                        p = f"{grid_path}/{key}"
                        if p in src_f:
                            return float(src_f[p][()])
                        return float(arr[1] - arr[0]) if len(arr) > 1 else 0.0

                    wkt = _geo_grid_bounding_polygon_wkt(
                        x_sub, y_sub,
                        _spacing("xCoordinateSpacing", x_sub),
                        _spacing("yCoordinateSpacing", y_sub),
                        crs_str)

                    if "boundingPolygon" in id_grp:
                        del id_grp["boundingPolygon"]
                    d = id_grp.create_dataset("boundingPolygon",
                                              data=np.bytes_(wkt))
                    # Restore epsg / ogr_geometry / description, which
                    # conformance readers key on.
                    if bp_src is not None:
                        _cpattr(bp_src, d)
                except Exception as exc:
                    # _cpgrp has already copied the source polygon, which covers
                    # the whole granule.  Leaving it would make the subset
                    # silently claim a footprint it does not have -- worse than
                    # the field being absent -- so drop it and say so.
                    if "boundingPolygon" in id_grp:
                        del id_grp["boundingPolygon"]
                    print(f"    [WARN] boundingPolygon could not be recomputed "
                          f"({exc}); omitting rather than keeping the "
                          f"full-granule footprint.", flush=True)

            # ============================================================
            # /science/LSAR/GSLC/metadata/  (curated + subsetted, cf. _subset_rslc)
            # ============================================================
            meta_base = "/science/LSAR/GSLC/metadata"
            meta_grp = dst.require_group(meta_base.lstrip("/"))

            # Window extent from the SLC subset coordinates (x ascending,
            # y descending) -> [ulx, uly, lrx, lry].  Used to subset every
            # grid-borne metadata array (radarGrid geolocation cubes,
            # calibration/processing grids) to the requested window instead of
            # copying the full frame verbatim.
            _xs = src_f[f"{grid_path}/xCoordinates"][col:col + w]
            _ys = src_f[f"{grid_path}/yCoordinates"][row:row + h]
            _extent = [float(_xs[0]), float(_ys[0]), float(_xs[-1]), float(_ys[-1])]

            # orbit / attitude have no map grid -> copy verbatim (small); every
            # other group (processingInformation, calibrationInformation,
            # radarGrid, sourceData, ceosAnalysisReadyData) is subsetted on its
            # own x/y axes where present, verbatim otherwise.
            _subsettable = [gname for gname in src_f[meta_base].keys()
                            if gname not in ("orbit", "attitude")]

            # The metadata copy is ~200 scattered datasets and dominates the
            # remote runtime, so plan every read first and fetch them
            # concurrently.  Falls back to reading through this handle if the
            # pool cannot start or the file is local (where there is nothing to
            # overlap).
            _prefetch = None
            if read_workers > 1 and src_url and src_url.startswith(
                    ("s3://", "https://")):
                _plan = []
                for gname in _subsettable:
                    _subset_meta_grid(src_f[f"{meta_base}/{gname}"],
                                      meta_grp, gname, _extent, collect=_plan)
                if verbose:
                    print(f"    Prefetching {len(_plan)} metadata datasets "
                          f"with {read_workers} workers ...", flush=True)
                _prefetch = nisar_tools.parallel_read_datasets(
                    src_url, auth_config, _plan, workers=read_workers,
                    verbose=verbose, src_f=src_f)

            for gname in src_f[meta_base].keys():
                if gname in ("orbit", "attitude"):
                    _cpgrp(f"{meta_base}/{gname}", meta_grp, gname)
                else:
                    _subset_meta_grid(src_f[f"{meta_base}/{gname}"],
                                      meta_grp, gname, _extent,
                                      prefetch=_prefetch)

            # sourceData/swaths came across verbatim above and still describes
            # the whole source RSLC; retarget it (and the identification times)
            # to the window from the radarGrid cube just written.
            _retarget_swath_extents(dst, meta_base, verbose=verbose)

            # ============================================================
            # /science/LSAR/GSLC/grids/frequency{X}/  (the grid data)
            # ============================================================
            def _write_freq_grid(fq_path, var_names, c, r, ww, hh):
                """Write one frequency grid group, windowed to (c, r, ww, hh)
                on that frequency's *own* x/y axes.

                Frequencies do not share a grid: in a DHDH granule frequency B
                is posted 8x coarser in x than frequency A, so the window has
                to be recomputed per frequency rather than reusing A's indices.
                """
                # Build group hierarchy to fq_path with attributes
                g = dst
                cur = ""
                for part in fq_path.strip("/").split("/"):
                    cur += f"/{part}"
                    g = g.require_group(part)
                    if cur in src_f:
                        _cpattr(src_f[cur], g)

                # Coordinate datasets (subsetted)
                x_src = src_f[f"{fq_path}/xCoordinates"]
                y_src = src_f[f"{fq_path}/yCoordinates"]
                _cpattr(x_src, g.create_dataset(
                    "xCoordinates", data=x_src[c: c + ww]))
                _cpattr(y_src, g.create_dataset(
                    "yCoordinates", data=y_src[r: r + hh]))
                ny_f, nx_f = len(y_src), len(x_src)

                # Projection
                if f"{fq_path}/projection" in src_f:
                    _cpds(f"{fq_path}/projection", g)

                # listOfPolarizations (update to match the vars written)
                lop_path = f"{fq_path}/listOfPolarizations"
                if lop_path in src_f:
                    pols = sorted(v for v in var_names
                                  if len(v) == 2 and v.isupper())
                    if pols:
                        d = g.create_dataset(
                            "listOfPolarizations",
                            data=np.array(pols,
                                          dtype=f"S{max(len(p) for p in pols)}"))
                        _cpattr(src_f[lop_path], d)

                # validSamplesSubSwath (subset rows if present)
                vs_path = f"{fq_path}/validSamplesSubSwath"
                if vs_path in src_f and len(src_f[vs_path].shape) == 2:
                    vs_src = src_f[vs_path]
                    d = g.create_dataset(
                        "validSamplesSubSwath", data=vs_src[r: r + hh, :],
                        compression="gzip", compression_opts=4)
                    _cpattr(vs_src, d)

                # Chunk shape shared by every 2-D array of this frequency.
                chunk_y = min(512, hh)
                chunk_x = min(512, ww)

                _skip = {"projection", "xCoordinates", "yCoordinates",
                         "listOfPolarizations", "validSamplesSubSwath"}
                for item in src_f[fq_path].keys():
                    if item in _skip or item in g:
                        continue
                    ds = src_f[f"{fq_path}/{item}"]
                    if not isinstance(ds, h5py.Dataset):
                        continue

                    # Polarisation layers the caller did not ask for.
                    if len(item) == 2 and item.isupper() and item not in var_names:
                        continue

                    if ds.shape == ():
                        # Scalars (spacing, bandwidth, ...) copied verbatim.
                        _cpds(f"{fq_path}/{item}", g)
                    elif ds.ndim == 2 and ds.shape == (ny_f, nx_f):
                        # Everything on this frequency's own grid -- the complex
                        # polarisations, mask, inputDataExceptionMask -- is
                        # windowed, preserving the source compression settings.
                        d = g.create_dataset(
                            item, data=ds[r: r + hh, c: c + ww],
                            chunks=(chunk_y, chunk_x),
                            compression=ds.compression or "gzip",
                            compression_opts=ds.compression_opts or 1,
                            shuffle=ds.shuffle)
                        _cpattr(ds, d)
                    elif ds.ndim == 1:
                        _cpds(f"{fq_path}/{item}", g)
                    else:
                        # A 2-D array whose shape does not match this
                        # frequency's grid cannot be windowed on it (the source
                        # stores frequencyB/inputDataExceptionMask at frequency
                        # A's shape).  Copying it verbatim would drag the full
                        # frame into the subset, so omit it and say so.
                        print(f"    [WARN] {fq_path}/{item} has shape "
                              f"{ds.shape}, which does not match this "
                              f"frequency's grid {(ny_f, nx_f)}; omitted from "
                              f"the subset.", flush=True)

            # The requested frequency, windowed with the indices the caller
            # already computed, plus every other frequency present unless the
            # caller restricted the product to one.
            grids_base = grid_path.rsplit("/", 1)[0]
            written_freqs = [grid_path.rsplit("frequency", 1)[-1]]
            _write_freq_grid(grid_path, variable_names, col, row, w, h)

            if all_frequencies:
                # Map extent of the requested window, at pixel *edges*.
                def _edge_span(centers, key):
                    p = f"{grid_path}/{key}"
                    if p in src_f:
                        sp = float(src_f[p][()])
                    else:
                        sp = (float(centers[1] - centers[0])
                              if len(centers) > 1 else 0.0)
                    half = abs(sp) / 2.0
                    lo = min(float(centers[0]), float(centers[-1])) - half
                    hi = max(float(centers[0]), float(centers[-1])) + half
                    return lo, hi

                x_lo, x_hi = _edge_span(_xs, "xCoordinateSpacing")
                y_lo, y_hi = _edge_span(_ys, "yCoordinateSpacing")

                def _covering_indices(nodes, spacing, lo, hi):
                    """First index and count of every cell overlapping [lo, hi].

                    get_indices_from_extent snaps to the nearest node, which
                    truncates by up to half a cell on a coarser grid and can
                    leave frequencies covering different ground.  Selecting
                    every overlapping cell instead guarantees each frequency
                    covers the requested window.  Works for ascending or
                    descending axes since the selected indices are contiguous
                    either way.
                    """
                    half = abs(spacing) / 2.0
                    inside = np.nonzero((nodes + half > lo)
                                        & (nodes - half < hi))[0]
                    if inside.size == 0:
                        return 0, 0
                    return int(inside[0]), int(inside[-1] - inside[0] + 1)

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
                    fc, fw = _covering_indices(fx, fdx, x_lo, x_hi)
                    fr, fh = _covering_indices(fy, fdy, y_lo, y_hi)
                    if fw == 0 or fh == 0:
                        print(f"    [WARN] {fq_path} does not overlap the "
                              f"requested window; skipped.", flush=True)
                        continue
                    lop_path = f"{fq_path}/listOfPolarizations"
                    if lop_path in src_f:
                        fvars = [p.decode() if hasattr(p, "decode") else str(p)
                                 for p in src_f[lop_path][()]]
                    else:
                        fvars = [k for k in src_f[fq_path].keys()
                                 if len(k) == 2 and k.isupper()]
                    _write_freq_grid(fq_path, fvars, fc, fr, fw, fh)
                    written_freqs.append(fk.replace("frequency", ""))

            # identification/listOfFrequencies must name what was actually
            # written, not what the source happened to contain.
            lof_path = f"{ident_src}/listOfFrequencies"
            if lof_path in src_f and ident_src.lstrip("/") in dst:
                id_grp = dst[ident_src.lstrip("/")]
                if "listOfFrequencies" in id_grp:
                    del id_grp["listOfFrequencies"]
                d = id_grp.create_dataset(
                    "listOfFrequencies",
                    data=np.array(sorted(written_freqs), dtype="S1"))
                _cpattr(src_f[lof_path], d)

        with open(tmp_path, "rb") as fh:
            return fh.read(), sorted(written_freqs)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# =========================================================
# 4. CORE PROCESSOR
# =========================================================


def _process_single_file_gslc(
    h5_url, variable_names, output_dir_or_file,
    srcwin, projwin, transform_mode, frequency,
    single_bands, vrt, downscale_factor, target_align_pixels,
    input_fs, output_fs,
    is_batch=False, cache=None, keep=False, use_earthdata=False,
    verbose=False, target_srs=None, target_res=None, resample="cubic",
    output_format="COG", fill_holes=False, num_threads=None, read_threads=8,
    square_pixels=False, projwin_srs=None, apply_mask=True, input_auth=None,
    all_frequencies=False,
):
    """
    Convert one GSLC HDF5 file to COG/GTiff/H5/complex-GTiff.

    transform_mode choices
    ----------------------
    ``pwr``   -- Power intensity (|z|^2), float32,   nodata=NaN  (default)
    ``AMP``   -- Scaled amplitude,        uint16,    nodata=0    (GCOV-compatible)
    ``mag``   -- Raw magnitude |z|,        float32,   nodata=NaN
    ``phase`` -- Wrapped phase angle(z),   float32,   nodata=NaN, radians
    ``cslc``  -- Raw complex SLC,          complex64, tiled GTiff, nodata=0+0j convention
    """

    # One frequency unless the caller asked for all of them (-of h5 only, cf.
    # the RSLC subsetter's --all_freq).  The caller resolves an unset -f to the
    # frequency the granule actually carries; "A" is the last-resort default.
    frequency = frequency or "A"

    h5_basename = h5_url.split("/")[-1]
    base_name = h5_basename[:-3] if h5_basename.lower().endswith(".h5") else h5_basename

    mode_str   = transform_mode if transform_mode else "pwr"
    logic_mode = mode_str.lower()
    # Normalise display / filename strings
    if logic_mode == "amp":
        mode_str = "AMP"

    if verbose:
        print(f"--> Processing File (GSLC): {h5_basename}", flush=True)

    if is_batch:
        final_path = f"{output_dir_or_file.rstrip('/')}/{base_name}.tif"
    else:
        if output_dir_or_file.endswith("/") or not output_dir_or_file.lower().endswith(".tif"):
            final_path = f"{output_dir_or_file.rstrip('/')}/{base_name}.tif"
        else:
            final_path = output_dir_or_file

    cached_file_path = None
    if verbose:
        import time as _time
        _t_file = _time.perf_counter()

    try:
        if cache is not None:
            file_url = cache_to_local(h5_url, localdir=cache, keep=keep,
                                      use_earthdata=use_earthdata, fs=input_fs)
            if not keep:
                cached_file_path = file_url
        else:
            file_url = h5_url

        # --- Metadata: always use h5py for grid info (reads only 2 coords, fast).
        # Datatree is used only for acquisition metadata where it adds value.
        dt = open_datatree_lazy(file_url, input_fs, verbose=verbose)
        use_datatree = (dt is not None)

        acq_meta = None
        if use_datatree:
            acq_meta = get_acquisition_metadata_from_datatree_gslc(dt)

        f = open_h5_lazy(file_url, input_fs)
        info = get_grid_info_gslc(f, frequency=frequency)
        if acq_meta is None:
            acq_meta = get_acquisition_metadata_gslc(f)

        f.close()

        # Inject processing metadata
        try:
            from importlib.metadata import version as _pkg_version
            acq_meta["OPENSEPPO_VERSION"] = _pkg_version("openseppo")
        except Exception:
            acq_meta["OPENSEPPO_VERSION"] = "unknown"

        if verbose:
            print(f"    [t] file open + metadata: {_time.perf_counter()-_t_file:.1f}s",
                  flush=True)
            _t_file = _time.perf_counter()

        date_str = acq_meta.get("ACQUISITION_DATE", "Unknown")
        if verbose:
            meta_label = "h5py+datatree" if use_datatree else "h5py"
            print(f"    Date: {date_str} | Grid: {info['res_x']:.1f}m ({frequency}) "
                  f"| Meta: {meta_label} | Transform: {mode_str}", flush=True)

        # --- Reprojection setup ---
        input_crs_obj = _parse_crs(info["crs"])
        dst_crs_obj   = _parse_crs(target_srs) if target_srs else input_crs_obj
        crs_changes   = (dst_crs_obj != input_crs_obj)

        # Convert projwin from projwin_srs to the CRS expected by downstream code.
        if projwin_srs and projwin:
            from openseppo.nisar.nisar_tools import reproject_projwin
            _dst_srs = target_srs if crs_changes else info["crs"]
            projwin = reproject_projwin(projwin, projwin_srs, _dst_srs)
            if verbose:
                print(f"    projwin_srs {projwin_srs} -> {_dst_srs}: {projwin}", flush=True)

        # Phase data must NOT be averaged by warp kernels; force nearest-neighbour
        _phase_mode = (logic_mode == "phase")
        _cslc_mode  = (logic_mode == "cslc")   # raw complex64, tiled GTiff
        _amp_mode   = (logic_mode == "amp")    # uint16 scaled amplitude
        _mag_mode   = (logic_mode == "mag")    # float32 raw magnitude
        _resample_method = "nearest" if (_phase_mode or _cslc_mode) else resample
        if (_phase_mode or _cslc_mode) and resample != "nearest":
            if verbose:
                _why = "phase" if _phase_mode else "cslc"
                print(f"    Note: {_why} mode forces nearest-neighbour resampling.", flush=True)

        # --- Auto square-pixel downscale ---
        if square_pixels and downscale_factor is None:
            _nat_x = abs(info["res_x"])
            _nat_y = abs(info["res_y"])
            if not math.isclose(_nat_x, _nat_y, rel_tol=1e-4):
                _coarser = max(_nat_x, _nat_y)
                _sq_x = max(1, round(_coarser / _nat_x))
                _sq_y = max(1, round(_coarser / _nat_y))
                downscale_factor = (_sq_x, _sq_y)
                if verbose:
                    _sq_method = "nearest" if (_phase_mode or _cslc_mode) else "block average"
                    print(f"    --square: native {_nat_x:.4g}x{_nat_y:.4g} m -> "
                          f"downscale ({_sq_x},{_sq_y}) [{_sq_method}] -> "
                          f"{_nat_x*_sq_x:.4g}x{_nat_y*_sq_y:.4g} m", flush=True)

        # --- Auto pre-downscale from -tr ---
        if target_res is not None and downscale_factor is None:
            _tr_x = abs(target_res[0])
            _tr_y = abs(target_res[1])
            _nat_x = abs(info["res_x"])
            _nat_y = abs(info["res_y"])
            _divisor = _RESAMPLE_PREDOWNSCALE_DIVISOR.get((_resample_method or "nearest").lower(), 1)

            if not crs_changes:
                _ratio_x = _tr_x / _nat_x
                _ratio_y = _tr_y / _nat_y
                _ix = int(round(_ratio_x))
                _iy = int(round(_ratio_y))
                if (_ix >= 1 and _iy >= 1
                        and math.isclose(_ratio_x, _ix, rel_tol=1e-4)
                        and math.isclose(_ratio_y, _iy, rel_tol=1e-4)
                        and (_ix > 1 or _iy > 1)):
                    if not _phase_mode and not _cslc_mode:
                        downscale_factor = _ix if _ix == _iy else (_ix, _iy)
                        if verbose:
                            _lbl = f"{_ix}x" if _ix == _iy else f"({_ix},{_iy})"
                            print(f"    Auto pre-downscale: {_lbl} block average "
                                  f"({_nat_x:.4g}x{_nat_y:.4g} -> "
                                  f"{_tr_x:.4g}x{_tr_y:.4g})", flush=True)
                    else:
                        if verbose:
                            _skip_why = "phase" if _phase_mode else "cslc"
                            print(f"    {_skip_why} mode: skipping auto pre-downscale "
                                  f"(use explicit -d for nearest decimation).", flush=True)
                else:
                    _ratio  = min(_ratio_x, _ratio_y)
                    _auto_d = (max(1, int(math.floor(_ratio / _divisor)))
                               if not _phase_mode and not _cslc_mode else 1)
                    if _auto_d > 1:
                        downscale_factor = _auto_d
                        if verbose:
                            print(f"    Auto pre-downscale: {_auto_d}x before resampling "
                                  f"({_nat_x:.4g} -> {_nat_x * _auto_d:.4g}, "
                                  f"then warp to {_tr_x:.4g})", flush=True)

        # --- Normalise downscale_factor to (_df_x, _df_y) for all further use ---
        if isinstance(downscale_factor, (tuple, list)):
            _df_x, _df_y = int(downscale_factor[0]), int(downscale_factor[1])
        elif downscale_factor and downscale_factor > 1:
            _df_x = _df_y = int(downscale_factor)
        else:
            _df_x = _df_y = 1

        # Determine whether a warp is needed due to an explicit target resolution
        needs_resample = False
        if target_res is not None and not crs_changes:
            eff_res_x = abs(info["res_x"]) * _df_x
            eff_res_y = abs(info["res_y"]) * _df_y
            already_matches = (
                math.isclose(eff_res_x, abs(target_res[0]), rel_tol=1e-6)
                and math.isclose(eff_res_y, abs(target_res[1]), rel_tol=1e-6)
            )
            needs_resample = not already_matches

        needs_reproject = crs_changes or needs_resample
        resample_enum = _get_resampling(_resample_method)
        if crs_changes and needs_reproject and verbose:
            src_epsg = input_crs_obj.to_epsg()
            src_label = f"EPSG:{src_epsg}" if src_epsg else info["crs"][:40]
            print(f"    Reprojecting: {src_label} -> {target_srs} "
                  f"(resample={_resample_method})", flush=True)

        # Expand projwin to native CRS when reprojecting
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

        # --- Pixel window ---
        if srcwin:
            col, row, w, h = srcwin
            if verbose:
                print(f"    Slice (Pixels): col={col}, row={row}, w={w}, h={h}", flush=True)
        elif native_projwin and native_projwin is not projwin:
            col, row, w, h = get_indices_from_extent(info["x"], info["y"], native_projwin)
            if verbose:
                print(f"    Slice (Map native): {native_projwin} -> Pixels: {col},{row},{w},{h}",
                      flush=True)
        elif projwin:
            col, row, w, h = get_indices_from_extent(info["x"], info["y"], projwin)
            if verbose:
                print(f"    Slice (Map): {projwin} -> Pixels: {col},{row},{w},{h}", flush=True)
        else:
            col, row, w, h = 0, 0, len(info["x"]), len(info["y"])
            if verbose:
                print(f"    Full extent (pixels): w={w}, h={h}", flush=True)

        # Align pixel grid for downscaling
        if target_align_pixels and (_df_x > 1 or _df_y > 1):
            curr_x_center = info["x"][col]
            curr_y_center = info["y"][row]
            dx, dy = info["res_x"], info["res_y"]
            curr_ulx = curr_x_center - abs(dx) / 2.0
            curr_uly = curr_y_center + abs(dy) / 2.0
            target_span_x = abs(dx) * _df_x
            target_span_y = abs(dy) * _df_y
            aligned_ulx = np.floor(curr_ulx / target_span_x) * target_span_x
            aligned_uly = np.ceil(curr_uly / target_span_y) * target_span_y
            diff_x = curr_ulx - aligned_ulx
            diff_y = aligned_uly - curr_uly
            shift_x = int(round(diff_x / abs(dx)))
            shift_y = int(round(diff_y / abs(dy)))
            new_col = col - shift_x
            new_row = row - shift_y
            if new_col < 0:
                offset_blocks = int(np.ceil(abs(new_col) / _df_x))
                new_col += offset_blocks * _df_x
            if new_row < 0:
                offset_blocks = int(np.ceil(abs(new_row) / _df_y))
                new_row += offset_blocks * _df_y
            if verbose:
                print(f"    Aligning Pixels: ({col},{row}) -> ({new_col},{new_row})", flush=True)
            col, row = new_col, new_row

        max_h, max_w = len(info["y"]), len(info["x"])
        col  = max(col, 0)
        row  = max(row, 0)
        if col + w > max_w:
            w = max_w - col
        if row + h > max_h:
            h = max_h - row
        if w <= 0 or h <= 0:
            raise ValueError("Resulting slice is empty/out of bounds.")

        # --- H5 subset output (complex data preserved) ---
        if output_format.lower() == "h5":
            x_c  = info["x"][col]
            y_c  = info["y"][row]
            _ulx = x_c  - abs(info["res_x"]) / 2.0
            _uly = y_c  + abs(info["res_y"]) / 2.0
            _tf  = from_origin(_ulx, _uly, abs(info["res_x"]), abs(info["res_y"]))
            if verbose:
                print(f"    Writing H5 subset ({w}x{h}, complex) ...", flush=True)

            fh = open_h5_lazy(file_url, input_fs)
            h5_bytes, written_freqs = _subset_gslc(
                fh, info["grid_path"], variable_names, col, row, w, h,
                all_frequencies=all_frequencies,
                src_url=file_url, auth_config=input_auth,
                read_workers=read_threads, verbose=verbose)
            fh.close()

            # Name the file for the frequencies actually written, which is not
            # necessarily the single requested one.
            pol_list_str = "".join(v.lower() for v in variable_names)
            suffix = f"-EBD_{''.join(written_freqs)}_{pol_list_str}.h5"
            h5_out_path = (final_path[:-4] if final_path.endswith(".tif") else final_path) + suffix

            def _wb(path, data):
                if path.startswith("s3://"):
                    with output_fs.open(path, "wb") as fout:
                        fout.write(data)
                else:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "wb") as fout:
                        fout.write(data)

            _wb(h5_out_path, h5_bytes)
            if verbose:
                print(f"    [OK] H5 subset: {os.path.basename(h5_out_path)}", flush=True)
            files_map_h5 = {var: h5_out_path for var in variable_names}
            return {"success": True, "h5_url": h5_url, "date": date_str,
                    "files_map": files_map_h5,
                    "info": {"w": w, "h": h, "transform": _tf,
                             "crs": info["crs"], "dtype": "complex64"}}

        # --- Read complex bands ---
        if verbose:
            print(f"    Extracting {len(variable_names)} complex bands ...", flush=True)
            _t_read = _time.perf_counter()

        # Read the complex bands and (when masking) the subswath mask in the same
        # file handle -- one HDF5 open.
        complex_bands, _mask_arr = _read_gslc_bands(
            file_url, input_fs, info["grid_path"],
            variable_names, row, h, col, w,
            with_mask=apply_mask,
        )
        if verbose:
            _tot_mb = sum(b.nbytes for b in complex_bands) / 1e6
            print(f"    [t] complex read ({len(complex_bands)} bands, {_tot_mb:.1f} MB): "
                  f"{_time.perf_counter()-_t_read:.1f}s", flush=True)
            _t_file = _time.perf_counter()

        # --- Subswath mask (default ON for non-h5 output; -nomask disables) ---
        # The GSLC "mask" grid holds the subswath number of each valid sample:
        # 1..254 = valid subswath, 0 = invalid, 255 = fill (outside acquisition
        # extent). Flagged pixels are set to the complex nodata (0+0j) at source
        # resolution -- before any downscale/transform/warp -- which each transform
        # already treats as nodata (-> NaN for pwr/mag/phase, 0+0j for cslc). The
        # h5 output path returns earlier, so this only affects COG/GTiff output.
        _mask_invalid = None
        if _mask_arr is not None:
            # Valid only where mask is a subswath number (1..254); 0/255 -> invalid
            _mask_invalid = ~((_mask_arr >= 1) & (_mask_arr <= 254))
            del _mask_arr
            if verbose:
                _n_inv = int(_mask_invalid.sum())
                print(f"    Masking: {_n_inv} fill/invalid px "
                      f"({100.0 * _n_inv / _mask_invalid.size:.1f}%) -> nodata on output", flush=True)
        elif apply_mask and verbose:
            print("    Note: no 'mask' grid found; masking skipped.", flush=True)

        # --- Output resolution ---
        orig_res_x, orig_res_y = info["res_x"], info["res_y"]
        x_center = info["x"][col]
        y_center = info["y"][row]
        ulx = x_center - abs(orig_res_x) / 2.0
        uly = y_center + abs(orig_res_y) / 2.0

        out_res_x = orig_res_x * _df_x
        out_res_y = orig_res_y * _df_y

        transform_obj = from_origin(ulx, uly, out_res_x, abs(out_res_y))
        crs_str = info["crs"]

        # --- Warp parameters (computed once) ---
        _n_th = str(num_threads) if num_threads is not None else "ALL_CPUS"
        if needs_reproject:
            w_eff   = (w - w % _df_x) // _df_x
            h_eff   = (h - h % _df_y) // _df_y
            _dt, _dw, _dh = calculate_default_transform(
                input_crs_obj, dst_crs_obj, w_eff, h_eff,
                left=ulx, bottom=uly - h_eff * abs(out_res_y),
                right=ulx + w_eff * out_res_x, top=uly,
            )
            if target_res:
                out_px, out_py = abs(target_res[0]), abs(target_res[1])
            else:
                out_px = abs(_dt.a)
                out_py = abs(_dt.e)

            if projwin:
                ulx_t, uly_t, lrx_t, lry_t = projwin
                snap_ulx = math.floor(ulx_t / out_px) * out_px
                snap_uly = math.ceil(uly_t  / out_py) * out_py
                snap_lrx = math.ceil(lrx_t  / out_px) * out_px
                snap_lry = math.floor(lry_t / out_py) * out_py
                dst_w = max(1, int(round((snap_lrx - snap_ulx) / out_px)))
                dst_h = max(1, int(round((snap_uly - snap_lry) / out_py)))
                dst_transform = from_origin(snap_ulx, snap_uly, out_px, out_py)
            elif target_res or target_align_pixels:
                left   = _dt.c
                top    = _dt.f
                right  = _dt.c + _dw * _dt.a
                bottom = _dt.f + _dh * _dt.e
                if target_align_pixels:
                    snap_ulx = math.floor(left  / out_px) * out_px
                    snap_uly = math.ceil(top   / out_py) * out_py
                    snap_lrx = math.ceil(right  / out_px) * out_px
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
                "src_transform": transform_obj,
                "src_crs":       input_crs_obj,
                "dst_transform": dst_transform,
                "dst_crs":       dst_crs_obj,
                "dst_width":     dst_w,
                "dst_height":    dst_h,
                "resampling":    resample_enum,
                "fill_holes":    fill_holes,
                "num_threads":   num_threads,
            }
            out_transform = dst_transform
            out_crs       = dst_crs_obj.to_wkt()
        else:
            warp_kw       = None
            out_transform = transform_obj
            out_crs       = crs_str

        def write_bytes(path, bytes_data):
            if path.startswith("s3://"):
                with output_fs.open(path, "wb") as f_out:
                    f_out.write(bytes_data)
            else:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "wb") as f_out:
                    f_out.write(bytes_data)

        files_map = {}
        _driver      = "GTiff" if output_format.upper() == "GTIFF" else "COG"
        _gtiff_extra = {"bigtiff": "YES"} if _driver == "GTiff" else {}
        # AMP (uint16) ->predictor 2 (integer); pwr/mag/phase (float32) ->predictor 3
        _predictor = 2 if _amp_mode else 3
        _write_extra = {"num_threads": _n_th, "predictor": _predictor}
        if _driver == "COG":
            _write_extra["overview_resampling"] = "average"

        # --- Process each polarisation band ---
        for i, var in enumerate(variable_names):
            if verbose:
                print(f"    [{i+1}/{len(variable_names)}] Processing {var} ...", flush=True)

            band_data = complex_bands[i]  # complex64, 2-D

            # Apply subswath mask: set fill/invalid pixels to complex nodata (0+0j)
            # before any downscale/transform/warp.
            if _mask_invalid is not None:
                band_data[_mask_invalid] = 0

            # --- CSLC: write raw complex64 tiled GeoTIFF ---
            if _cslc_mode:
                _cslc_data = band_data.astype(np.complex64)
                # Nearest decimation only (block-averaging complex is invalid)
                if _df_x > 1 or _df_y > 1:
                    _cslc_data = _cslc_data[::_df_y, ::_df_x]

                _c_transform = out_transform
                _c_crs = out_crs
                if needs_reproject and warp_kw is not None:
                    from rasterio.warp import reproject as _warp_reproject, Resampling as _Resampling
                    _dst_h = warp_kw["dst_height"]
                    _dst_w = warp_kw["dst_width"]
                    _cslc_dst = np.zeros((_dst_h, _dst_w), dtype=np.complex64)
                    _warp_reproject(
                        _cslc_data, _cslc_dst,
                        src_transform=warp_kw["src_transform"],
                        src_crs=warp_kw["src_crs"],
                        dst_transform=warp_kw["dst_transform"],
                        dst_crs=warp_kw["dst_crs"],
                        resampling=_Resampling.nearest,
                    )
                    _cslc_data = _cslc_dst
                    _c_transform = warp_kw["dst_transform"]
                    _c_crs = dst_crs_obj.to_wkt()

                h_out, w_out = _cslc_data.shape
                out_transform = _c_transform
                out_crs = _c_crs

                pol_str = var.lower()
                suffix = f"-EBD_{frequency}_{pol_str}_{mode_str}.tif"
                band_path = (final_path[:-4] if final_path.endswith(".tif") else final_path) + suffix

                _band_tags = dict(acq_meta, TRANSFORM_MODE=mode_str,
                                  COMPLEX_NODATA_CONVENTION="0+0j")
                _cslc_profile = {
                    "driver":    "GTiff",
                    "height":    h_out,
                    "width":     w_out,
                    "count":     1,
                    "dtype":     "complex64",
                    "crs":       out_crs,
                    "transform": out_transform,
                    "compress":  "deflate",
                    "predictor": 1,
                    "tiled":     True,
                    "blockxsize": 512,
                    "blockysize": 512,
                    "bigtiff":   "IF_SAFER",
                }
                with rasterio.Env(GDAL_NUM_THREADS=_n_th):
                    with MemoryFile() as memfile:
                        with memfile.open(**_cslc_profile) as dst:
                            dst.write(_cslc_data, 1)
                            dst.set_band_description(1, var)
                            dst.update_tags(**_band_tags)
                            dst.update_tags(1, Date=date_str)
                        memfile.seek(0)
                        write_bytes(band_path, memfile.read())
                        files_map[var] = band_path
                del _cslc_data
                gc.collect()
                continue

            # --- Downscale (on complex data before transform) ---
            # Phase: nearest-neighbour decimation (no averaging on cyclic data).
            # AMP (uint16): convert to power first, downscale power, then scale.
            # pwr / mag: convert to real, downscale, keep as float32.
            if (_df_x > 1 or _df_y > 1) and not _phase_mode:
                # Convert to power for downscaling (correct radiometric average)
                real_data = complex_to_power(band_data)
                real_data = real_data[np.newaxis, :, :]
                real_data = perform_downscaling(real_data, (_df_y, _df_x))[0]
                # real_data is now downscaled power (float32)
                if logic_mode == "pwr":
                    band_data = real_data
                elif _amp_mode:
                    band_data = pwr_to_amp(real_data)   # uint16
                elif _mag_mode:
                    band_data = np.sqrt(np.where(np.isfinite(real_data) & (real_data > 0),
                                                  real_data, np.nan)).astype(np.float32)
                else:
                    band_data = real_data
            elif (_df_x > 1 or _df_y > 1) and _phase_mode:
                band_data = band_data[::_df_y, ::_df_x]

            # --- Convert complex ->target dtype (when no downscale was applied) ---
            if isinstance(band_data, np.ndarray) and np.iscomplexobj(band_data):
                if logic_mode == "pwr":
                    band_data = complex_to_power(band_data)
                elif _amp_mode:
                    band_data = complex_to_amp_uint16(band_data)
                elif _mag_mode:
                    band_data = complex_to_magnitude(band_data)
                elif _phase_mode:
                    band_data = complex_to_phase(band_data)
                else:
                    band_data = complex_to_power(band_data)

            # band_data is now 2-D real array

            # --- Fill interior holes (not for phase; meaningful only for intensity) ---
            if fill_holes and not _phase_mode and band_data.dtype != np.uint16:
                band_data = _fill_nodata_nn(band_data)

            # --- Reproject / resample ---
            if needs_reproject and warp_kw is not None:
                if verbose:
                    print(f"        Reprojecting {var} ...", flush=True)
                band_data = _reproject_power_band(band_data, **warp_kw)

            _, h_out, w_out = band_data[np.newaxis].shape if band_data.ndim == 2 else band_data.shape
            if band_data.ndim == 2:
                h_out, w_out = band_data.shape

            # Determine output dtype, nodata, and per-band predictor
            if _amp_mode:
                out_dtype   = "uint16"
                out_nodata  = 0
                _b_pred     = 2
            else:
                out_dtype   = "float32"
                out_nodata  = float("nan")
                band_data   = band_data.astype(np.float32)
                band_data[~np.isfinite(band_data)] = np.nan
                _b_pred     = 3

            # --- Build output path ---
            pol_str = var.lower()  # e.g. "hh", "hv"
            suffix = f"-EBD_{frequency}_{pol_str}_{mode_str}.tif"
            if final_path.endswith(".tif"):
                band_path = final_path[:-4] + suffix
            else:
                band_path = final_path + suffix

            # Per-band GDAL metadata
            _band_tags = dict(acq_meta)
            _band_tags["TRANSFORM_MODE"] = mode_str
            if _amp_mode:
                _band_tags["DB_FORMULA"] = "dB = 20*log10(DN) - 83"
            elif _phase_mode:
                _band_tags["PHASE_UNITS"] = "radians"
                _band_tags["PHASE_RANGE"] = "-pi to pi"

            _bw = dict(_write_extra, predictor=_b_pred)
            profile = {
                "driver":    _driver,
                "height":    h_out,
                "width":     w_out,
                "count":     1,
                "dtype":     out_dtype,
                "crs":       out_crs,
                "transform": out_transform,
                "compress":  "deflate",
                "nodata":    out_nodata,
                **_gtiff_extra,
                **_bw,
            }
            if _phase_mode and _driver == "COG":
                profile["overview_resampling"] = "nearest"

            with rasterio.Env(GDAL_NUM_THREADS=_n_th, GDAL_OVR_PROPAGATE_NODATA="NO"):
                with MemoryFile() as memfile:
                    with memfile.open(**profile) as dst:
                        dst.write(band_data.astype(out_dtype), 1)
                        dst.set_band_description(1, var)
                        dst.update_tags(**_band_tags)
                        dst.update_tags(1, Date=date_str)
                    memfile.seek(0)
                    write_bytes(band_path, memfile.read())
                    files_map[var] = band_path

            del band_data
            gc.collect()

        del complex_bands
        gc.collect()

        if verbose:
            print(f"    [t] write: {_time.perf_counter()-_t_file:.1f}s", flush=True)

        # --- Snapshot VRT (all bands in one VRT per acquisition) ---
        # Skipped for cslc: complex64 is not supported by the VRT helper.
        if vrt and len(files_map) > 1 and not _cslc_mode:
            pol_list_str = "".join(v.lower() for v in variable_names if v in files_map)
            vrt_suffix = f"-EBD_{frequency}_{pol_list_str}_{mode_str}.vrt"
            vrt_path = (final_path[:-4] if final_path.endswith(".tif") else final_path) + vrt_suffix
            _bsc_files = [files_map[v] for v in variable_names if v in files_map]
            _vrt_meta  = dict(acq_meta, TRANSFORM_MODE=mode_str)
            _vrt_dtype = "uint16" if _amp_mode else "float32"
            _vrt_nd    = 0 if _amp_mode else float("nan")
            _final_w, _final_h = w_out, h_out
            vrt_xml = generate_vrt_xml_single_step(
                _final_w, _final_h, out_transform, out_crs,
                _bsc_files, list(variable_names),
                date_str, dtype=_vrt_dtype, nodata=_vrt_nd,
                metadata=_vrt_meta,
            )
            write_bytes(vrt_path, vrt_xml.encode("utf-8"))
            if verbose:
                print(f"    Generated Snapshot VRT: {os.path.basename(vrt_path)}", flush=True)

        _out_dtype = "uint16" if _amp_mode else ("complex64" if _cslc_mode else "float32")
        return {
            "success": True,
            "h5_url": h5_url,
            "date": date_str,
            "files_map": files_map,
            "info": {
                "w": w_out, "h": h_out,
                "transform": out_transform,
                "crs": out_crs,
                "dtype": _out_dtype,
            },
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "h5_url": h5_url, "error": str(e)}
    finally:
        if cached_file_path and os.path.exists(cached_file_path):
            try:
                os.unlink(cached_file_path)
            except OSError:
                pass


# =========================================================
# 5. BATCH ENTRY POINT
# =========================================================


def process_chunk_task_gslc(
    h5_url, variable_names, output_path,
    srcwin=None, projwin=None, transform_mode="pwr", frequency=None,
    single_bands=True, vrt=True, downscale_factor=None,
    target_align_pixels=True, input_auth=None, output_auth=None,
    time_series_vrt=True, list_grids=False, list_vars=False, cache=None, keep=False,
    verbose=False, target_srs=None, target_res=None, resample="cubic",
    output_format="COG", fill_holes=False, num_threads=None, read_threads=8,
    square_pixels=False, projwin_srs=None, apply_mask=True,
    all_frequencies=False,
):
    """
    Batch entry point for GSLC conversion.

    *h5_url* may be a list of URL strings (local, s3://, https://) or a single URL.
    """
    use_earthdata = False
    if input_auth is None:
        input_auth = {"use_earthdata": False}
    if "use_earthdata" in input_auth:
        use_earthdata = input_auth["use_earthdata"]
    if output_auth is None:
        output_auth = {}

    urls = h5_url if isinstance(h5_url, list) else [h5_url]

    # Login once per process for Earthdata URLs
    if use_earthdata and HAS_EARTHACCESS and urls:
        if verbose:
            import time as _time
            _t0 = _time.perf_counter()
        _earthaccess_login(verbose=verbose)
        if verbose:
            elapsed = _time.perf_counter() - _t0
            if elapsed > 1:
                print(f"    [t] earthaccess login:  {elapsed:.1f}s", flush=True)

    is_batch = len(urls) > 1

    mode_str = transform_mode if transform_mode else "pwr"
    if transform_mode:
        if transform_mode.lower() == "amp":
            mode_str = "AMP"
        elif transform_mode.lower() == "mag":
            mode_str = "mag"
        elif transform_mode.lower() == "phase":
            mode_str = "phase"

    results_meta = []

    try:
        _https_earthdata = use_earthdata and urls and urls[0].startswith("https://")
        input_fs = None if _https_earthdata else create_s3_fs(input_auth)

        # --- LIST GRIDS MODE ---
        if list_grids:
            print(f"Inspecting GSLC file: {urls[0]}")
            try:
                f = open_h5_lazy(urls[0], input_fs)
                struct = inspect_h5_structure_gslc(f)
                f.close()

                print("\nAvailable Grids in GSLC HDF5:")
                for freq, details in struct.items():
                    if "error" in details:
                        print(f"  Frequency {freq}: Error - {details['error']}")
                    else:
                        print(f"  Frequency {freq}:")
                        print(f"    CRS: {details['crs']}")
                        print(f"    Raster Size:  {details['ncols']} x {details['nrows']} pixels")
                        print(f"    Resolution: X={details['res_x']:.2f}, Y={details['res_y']:.2f}")
                        bbox = details["bbox"]
                        print(f"    Extent (W,S,E,N): [{bbox[0]:.2f}, {bbox[1]:.2f}, "
                              f"{bbox[2]:.2f}, {bbox[3]:.2f}]")
                        print(f"    Footprint (Lon/Lat): {details.get('poly_geo', 'N/A')}")
                        print(f"    Footprint (Native):  {details.get('poly_native', 'N/A')}")
                        print(f"    Frame Size:          {details.get('dims', 'N/A')}")
                        print(f"    Variables (complex polarisations):")
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
                return f"Error inspecting GSLC file: {e}"

        # --- LIST ALL VARIABLES MODE ---
        if list_vars:
            print(f"Listing all variables in: {urls[0]}")
            try:
                f = open_h5_lazy(urls[0], input_fs)
                paths = list_h5_variables(f)
                f.close()
                print("\n".join(paths))
                return "Listing Complete."
            except Exception as e:
                import traceback
                traceback.print_exc()
                return f"Error listing variables: {e}"

        # --- AUTO-DETECT VARIABLES ---
        target_freq = frequency if frequency else "A"
        if variable_names is None or len(variable_names) == 0:
            if verbose:
                print(f"No variables specified. Auto-detecting GSLC polarisations for "
                      f"Frequency {target_freq} ...")
            try:
                f = open_h5_lazy(urls[0], input_fs)
                struct = inspect_h5_structure_gslc(f)
                f.close()

                if target_freq in struct and "vars" in struct[target_freq]:
                    all_vars = struct[target_freq]["vars"]
                    # GSLC polarisation variables: 2-letter UPPERCASE (HH, HV, VH, VV)
                    filtered_vars = [v for v in all_vars if len(v) == 2 and v.isupper()]
                    if not filtered_vars:
                        return (f"Error: No polarisation variables (2-letter upper) found "
                                f"for Freq {target_freq}. Available: {all_vars}")
                    variable_names = filtered_vars
                    if verbose:
                        print(f"  -> Selected: {variable_names}")
                else:
                    return f"Error: Frequency {target_freq} not found in GSLC file."
            except Exception as e:
                return f"Error detecting variables: {e}"

        output_fs = None
        if output_path.startswith("s3://"):
            output_fs = create_s3_fs(output_auth)

        # Auto-enable caching for remote full-frame reads
        is_remote = urls[0].startswith("s3://") or urls[0].startswith("https://")
        if cache is None and not srcwin and not projwin and is_remote:
            cache = "y"

        for url in urls:
            res = _process_single_file_gslc(
                url, variable_names, output_path,
                srcwin, projwin, transform_mode, frequency,
                single_bands, vrt, downscale_factor, target_align_pixels,
                input_fs, output_fs,
                is_batch=is_batch, cache=cache, keep=keep,
                use_earthdata=use_earthdata, verbose=verbose,
                target_srs=target_srs, target_res=target_res,
                resample=resample, output_format=output_format,
                fill_holes=fill_holes, num_threads=num_threads,
                read_threads=read_threads,
                square_pixels=square_pixels,
                projwin_srs=projwin_srs,
                apply_mask=apply_mask,
                input_auth=input_auth,
                all_frequencies=all_frequencies,
            )
            results_meta.append(res)

        # --- Build simple time-series VRTs for batch runs ---
        if is_batch and time_series_vrt and output_format.lower() != "h5":
            valid_results = [r for r in results_meta if r["success"]]
            if not valid_results:
                return "Batch failed: No valid files processed."

            valid_results.sort(key=lambda x: x["date"])
            ref_info = valid_results[0]["info"]
            min_date = valid_results[0]["date"]
            max_date = valid_results[-1]["date"]

            all_same_geom = all(
                r["info"]["w"] == ref_info["w"]
                and r["info"]["h"] == ref_info["h"]
                and r["info"]["transform"] == ref_info["transform"]
                for r in valid_results
            )

            def write_bytes(path, data):
                if path.startswith("s3://"):
                    with output_fs.open(path, "wb") as fout:
                        fout.write(data)
                else:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "wb") as fout:
                        fout.write(data)

            _bsc_count = 0
            for var in variable_names:
                pol_str = var.lower()
                stack_items = []
                dates_list = []
                for r in valid_results:
                    fpath = r["files_map"].get(var)
                    if not fpath:
                        continue
                    item = {"path": fpath, "band_idx": 1, "date": r["date"]}
                    if not all_same_geom:
                        item["transform"] = r["info"]["transform"]
                        item["w"]         = r["info"]["w"]
                        item["h"]         = r["info"]["h"]
                    stack_items.append(item)
                    dates_list.append(r["date"].replace("-", ""))

                if not stack_items:
                    continue

                _ts_dtype = ref_info.get("dtype", "float32")
                if all_same_geom:
                    vrt_xml = generate_vrt_xml_timeseries(
                        ref_info["w"], ref_info["h"], ref_info["transform"],
                        ref_info["crs"], stack_items, dtype=_ts_dtype,
                    )
                else:
                    vrt_xml = generate_vrt_xml_timeseries_union(
                        ref_info["crs"], stack_items, dtype=_ts_dtype,
                    )

                vrt_name = construct_timeseries_filename(
                    valid_results[0]["h5_url"], min_date, max_date,
                    frequency, pol_str, mode_str,
                )
                out_dir = output_path.rstrip("/")
                vrt_full_path = f"{out_dir}/{vrt_name}"
                if verbose:
                    print(f"  --> TS VRT: {vrt_name}")
                write_bytes(vrt_full_path, vrt_xml.encode("utf-8"))
                write_bytes(
                    vrt_full_path.replace(".vrt", ".dates"),
                    "\n".join(dates_list).encode("utf-8"),
                )
                _bsc_count += 1

            return f"Batch Complete. Generated {_bsc_count} Time Series VRTs."

        if len(results_meta) == 1:
            return "Done" if results_meta[0]["success"] else results_meta[0].get("error", "Unknown error")
        return f"Batch Processed {len(results_meta)} files."

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Critical Error: {str(e)}"
