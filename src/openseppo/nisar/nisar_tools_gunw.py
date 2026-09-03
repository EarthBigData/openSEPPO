"""
openseppo.nisar.nisar_tools_gunw -- NISAR GUNW processing core
**************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Core library for reading NISAR GUNW (Geocoded Unwrapped interferogram, L2 pair
product) HDF5 files and converting them to Cloud Optimized GeoTIFF (COG) or to a
fully self-contained HDF5 subset, in the same spirit as the GCOV converter.

GUNW differs from GCOV/GSLC in one structural way that shapes this module: its
grids are *not* flat under ``frequency{X}``.  Each frequency carries three
sub-groups, each an independent geocoded grid with its own coordinate axes and
resolution, and each with a per-polarisation subgroup:

  grids/frequency{X}/unwrappedInterferogram/{pol}/  (unwrappedPhase, coherence,
      connectedComponents, ionospherePhaseScreen[,Uncertainty])   -- native grid
  grids/frequency{X}/pixelOffsets/{pol}/            (alongTrackOffset,
      slantRangeOffset, correlationSurfacePeak)                    -- native grid
  grids/frequency{X}/wrappedInterferogram/{pol}/    (wrappedInterferogram
      (complex64), coherenceMagnitude)                    -- ~4x finer grid

Because ``frequency{X}/{group}/{pol}`` carries its own xCoordinates /
yCoordinates / projection, it behaves as a self-contained grid_path and the
shared nisar_tools geocoded helpers can point straight at it.

Raster output is single-grid (one layer group at a time, like GCOV's
single-frequency rule), selected with ``--layer_group``.  The ``-of h5`` subset
carries *every* group, each windowed on its own axes, plus orbit / attitude /
processingInformation / radarGrid metadata and a window-accurate
boundingPolygon.

Shared authentication, filesystem, reprojection, and VRT helpers are imported
directly from nisar_tools to avoid duplication.
"""

import os
import gc
import tempfile
import numpy as np
import h5py
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.transform import from_origin
from rasterio.io import MemoryFile

import openseppo.nisar.nisar_tools as nisar_tools
from openseppo.nisar.nisar_tools import (
    create_s3_fs,
    _earthaccess_login,
    HAS_EARTHACCESS,
    open_h5_lazy,
    _geo_grid_bounding_polygon_wkt,
    _ensure_utm_south,
    _parse_crs,
    _get_resampling,
    _reproject_ancillary_band,
    get_indices_from_extent,
    infer_projwin_srs,
    reproject_projwin,
    perform_downscaling,
    _downscale_block,
    get_gdal_dtype,
    generate_vrt_xml_single_step,
    cache_to_local,
    construct_timeseries_filename,
)


# ---------------------------------------------------------------------------
# GUNW-specific constants
# ---------------------------------------------------------------------------

GUNW_GRID_BASE = "/science/LSAR/GUNW/grids"
GUNW_PRODUCT = "GUNW"

# Per-subgroup layer catalogue.  For each layer:
#   token      -- short name used in output filenames
#   integer    -- keep native integer dtype and use nearest resampling
#   complex    -- source is complex; raster output is its phase (angle, radians)
# ``group_token`` disambiguates the same layer name across groups
# (coherenceMagnitude appears in two groups) in output filenames.
GUNW_LAYER_GROUPS = {
    "unwrappedInterferogram": {
        "group_token": "unw",
        "default": ["unwrappedPhase", "coherenceMagnitude", "connectedComponents"],
        "layers": {
            "unwrappedPhase":                   {"token": "unwphase", "integer": False, "complex": False},
            "coherenceMagnitude":               {"token": "coh",      "integer": False, "complex": False},
            "connectedComponents":              {"token": "concomp",  "integer": True,  "complex": False},
            "ionospherePhaseScreen":            {"token": "iono",     "integer": False, "complex": False},
            "ionospherePhaseScreenUncertainty": {"token": "ionounc",  "integer": False, "complex": False},
            "mask":                             {"token": "mask",     "integer": True,  "complex": False},
        },
    },
    "pixelOffsets": {
        "group_token": "off",
        "default": ["alongTrackOffset", "slantRangeOffset", "correlationSurfacePeak"],
        "layers": {
            "alongTrackOffset":        {"token": "azoff", "integer": False, "complex": False},
            "slantRangeOffset":        {"token": "rgoff", "integer": False, "complex": False},
            "correlationSurfacePeak":  {"token": "corr",  "integer": False, "complex": False},
            "mask":                    {"token": "mask",  "integer": True,  "complex": False},
        },
    },
    "wrappedInterferogram": {
        "group_token": "wrap",
        "default": ["wrappedInterferogram", "coherenceMagnitude"],
        "layers": {
            "wrappedInterferogram": {"token": "wrapphase", "integer": False, "complex": True},
            "coherenceMagnitude":   {"token": "coh",       "integer": False, "complex": False},
            "mask":                 {"token": "mask",      "integer": True,  "complex": False},
        },
    },
}

DEFAULT_LAYER_GROUP = "unwrappedInterferogram"

# Layers that live at the sub-group level (shared by every polarisation) rather
# than inside the per-pol subgroup.
_GROUP_LEVEL_LAYERS = {"mask"}


def _layer_spec(layer_group, var):
    """Return the catalogue spec dict for *var* in *layer_group*, or None."""
    grp = GUNW_LAYER_GROUPS.get(layer_group)
    if not grp:
        return None
    return grp["layers"].get(var)


def _crs_from_projection(proj_val):
    """Normalise a NISAR ``projection`` dataset value to a CRS string."""
    if hasattr(proj_val, "decode"):
        return proj_val.decode()
    try:
        return f"EPSG:{int(proj_val)}"
    except (TypeError, ValueError):
        return str(proj_val)


def _decode(val):
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    if isinstance(val, np.ndarray):
        return [_decode(v) for v in val.tolist()]
    return val


# =========================================================
# 1. HDF5 INSPECTION & GRID INFO
# =========================================================


def inspect_h5_structure_gunw(f):
    """
    Scan a GUNW HDF5 file and return a dict keyed by frequency code.

    Each frequency value maps each layer-group name to a dict with the grid CRS,
    dimensions, resolution, polarisation list, and the available data layers with
    dtype / nodata.  Mirrors ``inspect_h5_structure`` for GCOV but resolves the
    nested ``{group}/{pol}`` grids.
    """
    structure = {}
    if GUNW_GRID_BASE not in f:
        return {"error": f"Path {GUNW_GRID_BASE} not found in H5 (not a GUNW file?)."}

    # bounding polygon corners for a footprint display (best effort)
    poly_geo = "Not Found"
    ident = "/science/LSAR/identification/boundingPolygon"
    if ident in f:
        try:
            poly_geo = _decode(f[ident][()]).strip()
        except Exception as e:  # noqa: BLE001
            poly_geo = f"Metadata Error: {e}"

    for freq_key in sorted(f[GUNW_GRID_BASE].keys()):
        if not freq_key.startswith("frequency"):
            continue
        freq_code = freq_key.replace("frequency", "")
        fpath = f"{GUNW_GRID_BASE}/{freq_key}"
        freq_entry = {"poly_geo": poly_geo, "groups": {}}

        # scalar frequency-level fields
        lop = f"{fpath}/listOfPolarizations"
        if lop in f:
            freq_entry["polarizations"] = _decode(f[lop][()])

        for group_name in GUNW_LAYER_GROUPS:
            gpath = f"{fpath}/{group_name}"
            if gpath not in f:
                continue
            # polarisation subgroups are the 2-letter uppercase children
            pols = [k for k in f[gpath].keys()
                    if isinstance(f[f"{gpath}/{k}"], h5py.Group)]
            group_info = {"pols": pols, "layers": {}}
            # read grid geometry from the group level (shared across pols)
            try:
                proj_val = f[f"{gpath}/projection"][()]
                crs = _ensure_utm_south(_crs_from_projection(proj_val), f)
                x = f[f"{gpath}/xCoordinates"][()]
                y = f[f"{gpath}/yCoordinates"][()]
                group_info["crs"] = crs
                group_info["ncols"] = int(len(x))
                group_info["nrows"] = int(len(y))
                group_info["res_x"] = float(x[1] - x[0]) if len(x) > 1 else 0.0
                group_info["res_y"] = float(y[1] - y[0]) if len(y) > 1 else 0.0
            except Exception:  # noqa: BLE001
                pass

            # enumerate layers (group-level + per-pol)
            for var, spec in GUNW_LAYER_GROUPS[group_name]["layers"].items():
                if var in _GROUP_LEVEL_LAYERS:
                    dpath = f"{gpath}/{var}"
                else:
                    dpath = f"{gpath}/{pols[0]}/{var}" if pols else None
                if not dpath or dpath not in f:
                    continue
                ds = f[dpath]
                nod = ds.attrs.get("_FillValue")
                group_info["layers"][var] = {
                    "dtype": str(ds.dtype),
                    "nodata": (_decode(nod) if nod is not None else None),
                    "token": spec["token"],
                }
            freq_entry["groups"][group_name] = group_info
        structure[freq_code] = freq_entry
    return structure


def print_structure_gunw(structure):
    """Human-readable dump of ``inspect_h5_structure_gunw`` output."""
    if "error" in structure:
        print(f"  ERROR: {structure['error']}")
        return
    for freq_code, fe in structure.items():
        pols = fe.get("polarizations")
        print(f"\n--- Frequency {freq_code}"
              + (f"  (polarizations: {pols})" if pols else "") + " ---")
        for gname, gi in fe.get("groups", {}).items():
            geom = ""
            if "crs" in gi:
                geom = (f"  [{gi['crs']}, {gi['ncols']}x{gi['nrows']}, "
                        f"{abs(gi['res_x']):g}m]")
            print(f"  {gname}/{geom}  pols={gi.get('pols')}")
            for var, li in gi.get("layers", {}).items():
                print(f"      {var:34s} {li['dtype']:10s} "
                      f"nodata={li['nodata']}  -> token '{li['token']}'")


def get_grid_info_gunw(h5_handle, frequency="A", layer_group=DEFAULT_LAYER_GROUP,
                       pol=None):
    """Read grid coordinates and projection for one (frequency, group, pol).

    Returns a dict with ``x``, ``y`` (1-D coordinate arrays), ``res_x``,
    ``res_y``, ``crs`` (string), ``grid_path`` (``.../{group}/{pol}``),
    ``group_path`` (``.../{group}``), ``freq``, ``pol``.
    """
    gpath = f"{GUNW_GRID_BASE}/frequency{frequency}/{layer_group}"
    if gpath not in h5_handle:
        raise KeyError(f"Group path '{gpath}' not found in GUNW file.")
    if pol is None:
        pol = _first_pol(h5_handle, frequency, layer_group)
    ppath = f"{gpath}/{pol}"
    # coordinates live beside the data (per-pol) and also at the group level;
    # prefer the per-pol axes since that is the grid the data sits on.
    cbase = ppath if f"{ppath}/xCoordinates" in h5_handle else gpath
    x = h5_handle[f"{cbase}/xCoordinates"][()]
    y = h5_handle[f"{cbase}/yCoordinates"][()]
    proj_val = h5_handle[f"{cbase}/projection"][()]
    crs = _ensure_utm_south(_crs_from_projection(proj_val), h5_handle)
    return {
        "x": x, "y": y,
        "res_x": float(x[1] - x[0]) if len(x) > 1 else 0.0,
        "res_y": float(y[1] - y[0]) if len(y) > 1 else 0.0,
        "crs": crs, "grid_path": ppath, "group_path": gpath,
        "freq": frequency, "pol": pol,
    }


def _first_pol(h5_handle, frequency, layer_group):
    """First polarisation subgroup present under a layer group."""
    gpath = f"{GUNW_GRID_BASE}/frequency{frequency}/{layer_group}"
    lop = f"{GUNW_GRID_BASE}/frequency{frequency}/listOfPolarizations"
    if lop in h5_handle:
        for p in h5_handle[lop][()]:
            p = _decode(p)
            if f"{gpath}/{p}" in h5_handle:
                return p
    # fall back to the first 2-letter uppercase group child
    for k in sorted(h5_handle[gpath].keys()):
        if isinstance(h5_handle[f"{gpath}/{k}"], h5py.Group):
            return k
    raise KeyError(f"No polarisation subgroup found under {gpath}")


def available_frequencies_gunw(h5_handle):
    """List frequency codes present in the GUNW grids group."""
    if GUNW_GRID_BASE not in h5_handle:
        return []
    return [k.replace("frequency", "") for k in sorted(h5_handle[GUNW_GRID_BASE].keys())
            if k.startswith("frequency")]


def get_acquisition_metadata_gunw(h5_handle):
    """Read reference/secondary acquisition dates and CRID for filenames/tags.

    GUNW is a pair product: identification carries reference* and secondary*
    zero-Doppler times rather than a single acquisition time.  The reference
    start time is used as the product's primary date token.
    """
    meta = {}
    idb = "/science/LSAR/identification"
    for key, tag in [("referenceZeroDopplerStartTime", "REFERENCE_START"),
                     ("secondaryZeroDopplerStartTime", "SECONDARY_START"),
                     ("compositeReleaseId", "CRID"),
                     ("productVersion", "PRODUCT_VERSION")]:
        p = f"{idb}/{key}"
        if p in h5_handle:
            try:
                meta[tag] = _decode(h5_handle[p][()])
            except Exception:  # noqa: BLE001
                pass
    # ACQUISITION_DATE (reference) as YYYY-MM-DD for VRT/date tags
    ref = meta.get("REFERENCE_START", "")
    if len(ref) >= 10:
        meta["ACQUISITION_DATE"] = ref[:10]
    tb = f"/science/LSAR/{GUNW_PRODUCT}/metadata/orbit/temporalBaseline"
    if tb in h5_handle:
        try:
            meta["TEMPORAL_BASELINE_DAYS"] = str(int(h5_handle[tb][()]))
        except Exception:  # noqa: BLE001
            pass
    return meta


# =========================================================
# 2. WINDOW RESOLUTION
# =========================================================


def _resolve_window(info, srcwin, projwin, projwin_srs, verbose=False):
    """Return (col, row, w, h) on the grid described by *info*.

    *srcwin* is a pixel window (xoff, yoff, xsize, ysize) on that grid.
    *projwin* is (ulx, uly, lrx, lry); its CRS is inferred / reprojected to the
    grid's native CRS before snapping to pixel indices, exactly as GCOV does.
    With neither, the full grid is returned.
    """
    x, y = info["x"], info["y"]
    if srcwin:
        col, row, w, h = (int(srcwin[0]), int(srcwin[1]),
                          int(srcwin[2]), int(srcwin[3]))
        col = max(0, min(col, len(x) - 1))
        row = max(0, min(row, len(y) - 1))
        w = max(1, min(w, len(x) - col))
        h = max(1, min(h, len(y) - row))
        return col, row, w, h
    if projwin:
        eff_srs = infer_projwin_srs(projwin, projwin_srs, info["crs"], verbose)
        pw = projwin
        if eff_srs and _parse_crs(eff_srs) != _parse_crs(info["crs"]):
            pw = reproject_projwin(projwin, eff_srs, info["crs"])
        return get_indices_from_extent(x, y, pw)
    return 0, 0, len(x), len(y)


def _window_extent(info, col, row, w, h):
    """Map extent [ulx, uly, lrx, lry] for a pixel window on *info*'s grid."""
    xs = info["x"][col:col + w]
    ys = info["y"][row:row + h]
    return [float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1])]


def _src_transform(info, col, row, w, h):
    """Affine transform for the windowed array (pixel-corner origin)."""
    xs = info["x"][col:col + w]
    ys = info["y"][row:row + h]
    dx = info["res_x"] or (float(xs[1] - xs[0]) if len(xs) > 1 else 1.0)
    dy = info["res_y"] or (float(ys[1] - ys[0]) if len(ys) > 1 else -1.0)
    ulx = float(xs[0]) - dx / 2.0
    uly = float(ys[0]) - dy / 2.0
    return from_origin(ulx, uly, abs(dx), abs(dy))


# =========================================================
# 3. BAND READING & RASTER CONVERSION
# =========================================================


def _read_gunw_layer(fh, info, layer_group, var, col, row, w, h):
    """Read one windowed GUNW layer as a 2-D array plus (dtype, nodata, integer).

    Complex layers (wrappedInterferogram) are returned as their phase
    (angle, radians, float32).  Group-level layers (mask) are read from the
    sub-group rather than the per-pol subgroup.
    """
    spec = _layer_spec(layer_group, var) or {"integer": False, "complex": False}
    if var in _GROUP_LEVEL_LAYERS:
        dpath = f"{info['group_path']}/{var}"
    else:
        dpath = f"{info['grid_path']}/{var}"
    ds = fh[dpath]
    data = ds[row:row + h, col:col + w]
    nod = ds.attrs.get("_FillValue")

    if spec["complex"] or np.iscomplexobj(data):
        out = np.angle(data).astype(np.float32)
        out[~np.isfinite(np.abs(data))] = np.nan
        return out, "float32", np.nan, False
    if spec["integer"]:
        nodata = None if nod is None else _scalar(nod)
        return data, str(data.dtype), nodata, True
    out = data.astype(np.float32)
    out[~np.isfinite(out)] = np.nan
    return out, "float32", np.nan, False


def _scalar(v):
    if isinstance(v, np.ndarray):
        return v.reshape(-1)[0].item()
    if hasattr(v, "item"):
        return v.item()
    return v


def _warp_band(data, dtype, src_tf, src_crs, dst_tf, dst_crs, dst_w, dst_h,
               resampling, nodata, num_threads=None):
    """Reproject one band preserving its dtype (integer or float)."""
    n_threads = num_threads if num_threads is not None else os.cpu_count() or 1
    is_int = "int" in dtype
    if is_int:
        fill = nodata if nodata is not None else 0
        dst = np.full((dst_h, dst_w), fill, dtype=dtype)
        src = np.ascontiguousarray(data)
        reproject(source=src, destination=dst,
                  src_transform=src_tf, src_crs=src_crs,
                  dst_transform=dst_tf, dst_crs=dst_crs,
                  src_nodata=fill, dst_nodata=fill,
                  resampling=resampling, num_threads=n_threads)
        return dst
    # float path via the shared single-pass ancillary warp (NaN nodata)
    return _reproject_ancillary_band(
        data, src_transform=src_tf, src_crs=src_crs,
        dst_transform=dst_tf, dst_crs=dst_crs,
        dst_width=dst_w, dst_height=dst_h,
        resample_name=resampling.name if hasattr(resampling, "name") else "bilinear",
        num_threads=n_threads)


def _process_single_file_gunw(
    h5_url, variable_names, output_dir_or_file, srcwin, projwin, projwin_srs,
    frequency, layer_group, pol, output_format, single_bands, vrt,
    downscale_factor, target_align_pixels, input_fs, output_fs,
    is_batch=False, cache=None, keep=False, use_earthdata=False, verbose=False,
    target_srs=None, target_res=None, resample="bilinear", num_threads=None,
    all_groups=False,
):
    """Convert one GUNW HDF5 file to COG/GTiff bands or a self-contained h5 subset.

    Returns a dict describing the outputs (paths + geo) for VRT stacking.
    """
    local_path = h5_url
    _cached = None
    if cache and (h5_url.startswith("s3://") or h5_url.startswith("https://")):
        local_path = cache_to_local(h5_url, localdir=(None if cache in ("y", "yes") else cache),
                                     keep=keep, use_earthdata=use_earthdata, fs=input_fs)
        _cached = local_path if not keep else None
        read_fs = None
    else:
        read_fs = input_fs

    def write_bytes(path, data):
        if output_fs is not None:
            with output_fs.open(path, "wb") as fo:
                fo.write(data)
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as fo:
                fo.write(data)

    base = os.path.basename(h5_url)
    if base.endswith(".h5"):
        base = base[:-3]
    if output_dir_or_file.endswith("/") or is_batch:
        final_base = output_dir_or_file.rstrip("/") + "/" + base
    elif output_dir_or_file.endswith(".h5") or output_dir_or_file.endswith(".tif"):
        final_base = output_dir_or_file.rsplit(".", 1)[0]
    else:
        final_base = output_dir_or_file

    try:
        fh = open_h5_lazy(local_path, read_fs)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not open {h5_url}: {exc}")

    try:
        acq = get_acquisition_metadata_gunw(fh)
        date_str = acq.get("ACQUISITION_DATE", "")

        # ---- h5 subset branch -------------------------------------------
        if output_format.lower() == "h5":
            info = get_grid_info_gunw(fh, frequency, layer_group, pol)
            col, row, w, h = _resolve_window(info, srcwin, projwin, projwin_srs, verbose)
            if verbose:
                print(f"    Window on {layer_group}/{info['pol']}: "
                      f"col={col} row={row} w={w} h={h}", flush=True)
            input_auth = {"use_earthdata": True} if use_earthdata else None
            data_bytes = _subset_gunw(
                fh, frequency, info, col, row, w, h,
                src_url=(h5_url if _cached is None else None),
                auth_config=input_auth,
                read_workers=(num_threads or 8), verbose=verbose)
            out_path = final_base + f"-EBD_{frequency}_GUNW.h5"
            write_bytes(out_path, data_bytes)
            if verbose:
                print(f"    Wrote {out_path} ({len(data_bytes) / 1e6:.1f} MB)", flush=True)
            return {"h5_url": h5_url, "output": out_path, "format": "h5",
                    "date": date_str}

        # ---- raster branch ----------------------------------------------
        info = get_grid_info_gunw(fh, frequency, layer_group, pol)
        col, row, w, h = _resolve_window(info, srcwin, projwin, projwin_srs, verbose)
        src_tf = _src_transform(info, col, row, w, h)
        src_crs = _parse_crs(info["crs"])
        if verbose:
            print(f"    {layer_group}/{info['pol']} window col={col} row={row} "
                  f"w={w} h={h} @ {info['crs']}", flush=True)

        # reprojection target
        needs_reproject = target_srs is not None
        if needs_reproject:
            dst_crs = _parse_crs(target_srs)
            left = src_tf.c
            top = src_tf.f
            right = left + w * abs(info["res_x"])
            bottom = top - h * abs(info["res_y"])
            if target_res:
                res_kw = {"resolution": (target_res[0], target_res[1])}
            else:
                res_kw = {}
            dst_tf, dst_w, dst_h = calculate_default_transform(
                src_crs, dst_crs, w, h, left, bottom, right, top, **res_kw)
            if target_align_pixels and dst_tf is not None:
                rx, ry = dst_tf.a, dst_tf.e
                ox = np.floor(dst_tf.c / rx) * rx
                oy = np.ceil(dst_tf.f / abs(ry)) * abs(ry)
                shift_c = int(round((dst_tf.c - ox) / rx))
                shift_f = int(round((oy - dst_tf.f) / abs(ry)))
                dst_tf = from_origin(ox, oy, abs(rx), abs(ry))
                dst_w += abs(shift_c)
                dst_h += abs(shift_f)
        else:
            dst_crs, dst_tf, dst_w, dst_h = src_crs, src_tf, w, h

        gtok = GUNW_LAYER_GROUPS[layer_group]["group_token"]
        generated = []
        band_infos = []
        for var in variable_names:
            spec = _layer_spec(layer_group, var)
            if spec is None:
                print(f"    [WARN] '{var}' is not a known layer of "
                      f"{layer_group}; skipped.", flush=True)
                continue
            arr, out_dtype, nodata, is_int = _read_gunw_layer(
                fh, info, layer_group, var, col, row, w, h)

            # downscale
            if downscale_factor and downscale_factor > 1:
                if is_int:
                    arr = _downscale_block(arr[np.newaxis], downscale_factor,
                                           "mask_priority")[0].astype(out_dtype)
                else:
                    arr = perform_downscaling(arr[np.newaxis], downscale_factor)[0]

            # reproject
            if needs_reproject:
                rs = Resampling.nearest if is_int else _get_resampling(resample)
                arr = _warp_band(arr, out_dtype, src_tf, src_crs, dst_tf, dst_crs,
                                 dst_w, dst_h, rs, nodata, num_threads)
                out_tf, out_crs, h_out, w_out = dst_tf, dst_crs, dst_h, dst_w
            else:
                out_tf, out_crs, h_out, w_out = src_tf, src_crs, arr.shape[0], arr.shape[1]

            token = spec["token"]
            suffix = f"-EBD_{frequency}_{gtok}_{token}.tif"
            band_path = final_base + suffix

            driver = "COG" if output_format.upper() == "COG" else "GTiff"
            predictor = 2 if is_int else 3
            profile = {"driver": driver, "height": h_out, "width": w_out,
                       "count": 1, "dtype": out_dtype, "crs": out_crs,
                       "transform": out_tf, "compress": "deflate",
                       "predictor": predictor, "nodata": nodata}
            if driver == "COG":
                profile["overview_resampling"] = "nearest" if is_int else "average"
            else:
                profile.update(BIGTIFF="IF_SAFER", tiled=True,
                               blockxsize=512, blockysize=512)

            tags = dict(acq)
            tags["OPENSEPPO_LAYER_GROUP"] = layer_group
            tags["OPENSEPPO_LAYER"] = var
            with rasterio.Env(GDAL_NUM_THREADS=(num_threads or os.cpu_count() or 1),
                              GDAL_OVR_PROPAGATE_NODATA="NO"):
                with MemoryFile() as mem:
                    with mem.open(**profile) as dst:
                        dst.write(arr, 1)
                        dst.set_band_description(1, var)
                        dst.update_tags(**{k: v for k, v in tags.items() if v is not None})
                        if date_str:
                            dst.update_tags(1, Date=date_str)
                    mem.seek(0)
                    write_bytes(band_path, mem.read())
            generated.append(band_path)
            band_infos.append({"path": band_path, "var": var, "token": token,
                               "dtype": out_dtype, "nodata": nodata})
            if verbose:
                print(f"    Wrote {os.path.basename(band_path)} "
                      f"({w_out}x{h_out}, {out_dtype})", flush=True)
            del arr
            gc.collect()

        # snapshot multi-band VRT
        if vrt and len(band_infos) > 1:
            pol_list_str = "".join(b["token"] for b in band_infos)
            vrt_path = final_base + f"-EBD_{frequency}_{gtok}_{pol_list_str}.vrt"
            files = [b["path"] for b in band_infos]
            names = [b["var"] for b in band_infos]
            # bands may differ in dtype/nodata; VRT uses the first band's
            xml = generate_vrt_xml_single_step(
                w_out if needs_reproject else w, h_out if needs_reproject else h,
                out_tf, out_crs, files, names, date_str,
                dtype=band_infos[0]["dtype"], nodata=band_infos[0]["nodata"],
                metadata={"OPENSEPPO_LAYER_GROUP": layer_group})
            write_bytes(vrt_path, xml.encode("utf-8"))
            generated.append(vrt_path)

        return {"h5_url": h5_url, "outputs": generated, "format": output_format,
                "date": date_str, "crs": info["crs"],
                "transform": out_tf, "w": (w_out if needs_reproject else w),
                "h": (h_out if needs_reproject else h),
                "bands": band_infos, "layer_group": layer_group,
                "frequency": frequency}
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
        if _cached and os.path.exists(_cached):
            try:
                os.unlink(_cached)
            except OSError:
                pass


# =========================================================
# 4. H5 SELF-CONTAINED SUBSET WRITER
# =========================================================


def _subset_gunw(src_f, frequency, ref_info, col, row, w, h,
                 src_url=None, auth_config=None, read_workers=1, verbose=False):
    """Return bytes of a fully self-contained GUNW HDF5 subset.

    The window is defined by (col, row, w, h) on *ref_info*'s grid (the selected
    layer group / pol); its map extent drives the windowing of every other grid,
    each on its own coordinate axes.  Copied:

      * root attributes
      * /science/LSAR/identification/ (verbatim; boundingPolygon recomputed for
        the window, listOfFrequencies set to what was written)
      * /science/LSAR/GUNW/metadata/orbit/ + /attitude/ (verbatim; small,
        reference+secondary)
      * /science/LSAR/GUNW/metadata/processingInformation/ (verbatim)
      * /science/LSAR/GUNW/metadata/radarGrid/ (windowed on its own coarse grid)
      * /science/LSAR/GUNW/grids/frequency{X}/ -- every sub-group
        (unwrappedInterferogram, pixelOffsets, wrappedInterferogram) windowed on
        its own axes to the same map extent, so grids at different resolutions
        all cover the requested ground.

    Scattered metadata datasets are optionally prefetched with a parallel reader
    for remote sources, matching the GSLC subsetter.
    """
    extent = _window_extent(ref_info, col, row, w, h)

    fd, tmp_path = tempfile.mkstemp(suffix=".h5")
    os.close(fd)
    try:
        with h5py.File(tmp_path, "w") as dst:

            def _cpattr(src, dst_obj):
                for k, v in src.attrs.items():
                    try:
                        dst_obj.attrs[k] = v
                    except Exception:  # noqa: BLE001
                        pass

            def _cpds(path, dst_grp, name=None):
                ds = src_f[path]
                d = dst_grp.create_dataset(name or path.split("/")[-1], data=ds[()])
                _cpattr(ds, d)

            def _cpgrp(path, dst_parent, name=None):
                src_grp = src_f[path]
                g = dst_parent.require_group(name or path.split("/")[-1])
                _cpattr(src_grp, g)
                for item in src_grp:
                    child = src_grp[item]
                    if isinstance(child, h5py.Group):
                        _cpgrp(f"{path}/{item}", g, item)
                    else:
                        _cpds(f"{path}/{item}", g)

            def _subset_meta_grid(src_grp, dst_parent, name, margin=2,
                                  collect=None, prefetch=None):
                """Copy a group, slicing any dataset on the group's own
                (yCoordinates, xCoordinates) grid to *extent*.  Recurses into
                child groups (each may carry its own axes at its own
                resolution).  Two traversal modes -- *collect* (plan a parallel
                read) and *prefetch* (write from an already-fetched dict) --
                share this walk so the plan cannot drift from what is written.
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
                    c = max(0, c - margin); r = max(0, r - margin)
                    ww = min(nx - c, ww + 2 * margin); hh = min(ny - r, hh + 2 * margin)
                else:
                    nx = ny = None

                for k in src_grp:
                    child = src_grp[k]
                    if isinstance(child, h5py.Group):
                        _subset_meta_grid(child, g, k, margin,
                                          collect=collect, prefetch=prefetch)
                        continue
                    ds = child
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
                    # compress windowed 2-D+ arrays; copy scalars/vectors plain
                    if getattr(data, "ndim", 0) >= 2 and sl is not None:
                        cy = min(512, data.shape[-2])
                        cx = min(512, data.shape[-1])
                        chunks = (data.shape[:-2] + (cy, cx)) if data.ndim > 2 else (cy, cx)
                        d = g.create_dataset(
                            k, data=data, chunks=chunks,
                            compression=ds.compression or "gzip",
                            compression_opts=(ds.compression_opts
                                              if ds.compression_opts is not None else 1),
                            shuffle=ds.shuffle)
                    else:
                        d = g.create_dataset(k, data=data)
                    _cpattr(ds, d)

            # --- root attributes ---
            _cpattr(src_f, dst)

            # --- identification/ ---
            ident_src = "/science/LSAR/identification"
            if ident_src in src_f:
                _cpgrp(ident_src, dst, ident_src.lstrip("/"))
                id_grp = dst[ident_src.lstrip("/")]
                bp_path = f"{ident_src}/boundingPolygon"
                bp_src = src_f[bp_path] if bp_path in src_f else None
                try:
                    x_sub = ref_info["x"][col:col + w]
                    y_sub = ref_info["y"][row:row + h]
                    wkt = _geo_grid_bounding_polygon_wkt(
                        x_sub, y_sub,
                        ref_info["res_x"] or float(x_sub[1] - x_sub[0]),
                        ref_info["res_y"] or float(y_sub[1] - y_sub[0]),
                        ref_info["crs"])
                    if "boundingPolygon" in id_grp:
                        del id_grp["boundingPolygon"]
                    d = id_grp.create_dataset("boundingPolygon", data=np.bytes_(wkt))
                    if bp_src is not None:
                        _cpattr(bp_src, d)
                except Exception as exc:  # noqa: BLE001
                    if "boundingPolygon" in id_grp:
                        del id_grp["boundingPolygon"]
                    print(f"    [WARN] boundingPolygon could not be recomputed "
                          f"({exc}); omitting rather than keeping the "
                          f"full-granule footprint.", flush=True)

            # --- metadata/ ---
            meta_base = f"/science/LSAR/{GUNW_PRODUCT}/metadata"
            meta_grp = dst.require_group(meta_base.lstrip("/"))
            # orbit / attitude have no map grid -> verbatim; every other group
            # (processingInformation, radarGrid) is subsetted on its own axes.
            _verbatim = {"orbit", "attitude"}
            _subsettable = [g for g in src_f[meta_base].keys() if g not in _verbatim]

            _prefetch = None
            if read_workers > 1 and src_url and src_url.startswith(("s3://", "https://")):
                _plan = []
                for gname in _subsettable:
                    _subset_meta_grid(src_f[f"{meta_base}/{gname}"], meta_grp,
                                      gname, collect=_plan)
                if verbose:
                    print(f"    Prefetching {len(_plan)} metadata datasets with "
                          f"{read_workers} workers ...", flush=True)
                try:
                    _prefetch = nisar_tools.parallel_read_datasets(
                        src_url, auth_config, _plan, workers=read_workers,
                        verbose=verbose, src_f=src_f)
                except Exception as exc:  # noqa: BLE001
                    if verbose:
                        print(f"    [INFO] parallel prefetch unavailable ({exc}); "
                              f"reading serially.", flush=True)

            for gname in src_f[meta_base].keys():
                if gname in _verbatim:
                    _cpgrp(f"{meta_base}/{gname}", meta_grp, gname)
                else:
                    _subset_meta_grid(src_f[f"{meta_base}/{gname}"], meta_grp,
                                      gname, prefetch=_prefetch)

            # --- grids/frequency{X}/ (every sub-group, windowed on own axes) ---
            grids_base = f"{GUNW_GRID_BASE}/frequency{frequency}"
            gsci = dst.require_group(GUNW_GRID_BASE.lstrip("/"))
            _cpattr(src_f[GUNW_GRID_BASE], gsci)
            _subset_meta_grid(src_f[grids_base], gsci, f"frequency{frequency}")

            # listOfFrequencies -> what was written
            lof = f"{ident_src}/listOfFrequencies"
            if lof in src_f and ident_src.lstrip("/") in dst:
                id_grp = dst[ident_src.lstrip("/")]
                if "listOfFrequencies" in id_grp:
                    del id_grp["listOfFrequencies"]
                d = id_grp.create_dataset("listOfFrequencies",
                                          data=np.array([frequency], dtype="S1"))
                _cpattr(src_f[lof], d)

        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# =========================================================
# 5. BATCH DRIVER
# =========================================================


def process_gunw_task(
    h5_url, variable_names=None, output_path=None, srcwin=None, projwin=None,
    projwin_srs=None, frequency="A", layer_group=DEFAULT_LAYER_GROUP, pol=None,
    output_format="COG", single_bands=True, vrt=True, downscale_factor=None,
    target_align_pixels=True, input_auth=None, output_auth=None,
    time_series_vrt=True, list_grids=False, verbose=False, cache=None, keep=False,
    target_srs=None, target_res=None, resample="bilinear", num_threads=None,
    read_threads=8,
):
    """Batch-convert one or more GUNW HDF5 files to COG/GTiff or h5 subsets.

    Mirrors ``process_chunk_task`` (GCOV) for the GUNW nested-grid layout.
    """
    urls = [h5_url] if isinstance(h5_url, str) else list(h5_url)
    use_earthdata = bool(input_auth and input_auth.get("use_earthdata"))

    # filesystems
    input_fs = None
    if urls and urls[0].startswith("s3://"):
        input_fs = create_s3_fs(input_auth)
    output_fs = None
    if output_path and output_path.startswith("s3://"):
        output_fs = create_s3_fs(output_auth)

    if use_earthdata and HAS_EARTHACCESS:
        _earthaccess_login(verbose=verbose)

    # list-grids: scan the first file and exit
    if list_grids:
        first = urls[0]
        local = first
        if cache and (first.startswith("s3://") or first.startswith("https://")):
            local = cache_to_local(first, keep=False, use_earthdata=use_earthdata,
                                    fs=input_fs)
        fh = open_h5_lazy(local, None if local != first else input_fs)
        try:
            print_structure_gunw(inspect_h5_structure_gunw(fh))
        finally:
            fh.close()
        return "Listed grids."

    # resolve default layers for the chosen group
    if not variable_names:
        variable_names = GUNW_LAYER_GROUPS[layer_group]["default"]

    is_batch = len(urls) > 1 or (output_path and output_path.endswith("/"))
    results = []
    for i, url in enumerate(urls):
        if verbose:
            print(f"\n[{i + 1}/{len(urls)}] {os.path.basename(url)}", flush=True)
        try:
            res = _process_single_file_gunw(
                url, variable_names, output_path, srcwin, projwin, projwin_srs,
                frequency, layer_group, pol, output_format, single_bands, vrt,
                downscale_factor, target_align_pixels, input_fs, output_fs,
                is_batch=is_batch, cache=cache, keep=keep,
                use_earthdata=use_earthdata, verbose=verbose,
                target_srs=target_srs, target_res=target_res, resample=resample,
                num_threads=(num_threads or read_threads))
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            print(f"    [FAIL] {os.path.basename(url)}: {exc}", flush=True)
            if verbose:
                import traceback
                traceback.print_exc()

    ok = [r for r in results if r]
    return f"Processed {len(ok)}/{len(urls)} GUNW file(s)."
