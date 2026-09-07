"""
openseppo.nisar.nisar_tools_sme2 -- NISAR SME2 processing core
**************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Core library for reading NISAR SME2 (Soil Moisture Estimate, L3) HDF5 files and
converting them to Cloud Optimized GeoTIFF (COG) or to a fully self-contained
HDF5 subset, in the same spirit as the GCOV and GUNW converters.

SME2 is structurally the simplest of the geocoded NISAR products: *every* 2-D
layer in the file sits on one shared grid.  ``grids/xCoordinates`` /
``yCoordinates`` are repeated inside each sub-group but are byte-identical to
the root pair (verified on both the BETA and PROVISIONAL tiers), so a single
window resolves the whole granule -- there is nothing like GUNW's three
independent grids at different resolutions.

Two properties of the product shape this module:

  * The grid is **EASE-Grid 2.0 Global (EPSG:6933)**, not UTM, at a nominal
    200 m posting (200.179 m in the files).  Coordinates are metres in the
    +/-17 000 km range, so a lon/lat ``-projwin`` passed without
    ``-projwin_srs`` would snap to an edge pixel and silently write a 1x1
    subset; ``infer_projwin_srs`` is applied on every window, as in GCOV.
    Granules also carry ``EASEGridRowIndex`` / ``EASEGridColumnIndex``, their
    window on the global grid, so tiles from different dates and tracks align
    pixel-for-pixel with no resampling.

  * Layer *paths are not stable across release tiers*: ``surfaceQualityFlag``
    sits under ``grids/ancillaryData/`` in BETA and directly under ``grids/``
    in PROVISIONAL.  Nothing here hardcodes a layer path -- the grid tree is
    walked and every 2-D dataset on the shared grid is discovered, so a layer
    that moves (or a new one that appears) is picked up rather than silently
    lost.  ``algorithmCandidates`` likewise varies: the sampled granules carry
    only DSG even where the run configuration requested DSG_TSR_PMI.

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
    generate_vrt_xml_single_step,
    cache_to_local,
)


# ---------------------------------------------------------------------------
# SME2-specific constants
# ---------------------------------------------------------------------------

SME2_GRID_BASE = "/science/LSAR/SME2/grids"
SME2_PRODUCT = "SME2"

# Layer groups selectable with --layer_group.  ``subpath`` is relative to
# SME2_GRID_BASE and may be templated with the algorithm / frequency chosen at
# runtime; ``group_token`` prefixes output filenames.  The layers themselves are
# *discovered*, never listed here (see the module docstring).
SME2_LAYER_GROUPS = {
    "soilMoisture": {
        "subpath": "",
        "group_token": "sm",
        "default": ["soilMoisture", "soilMoistureUncertainty", "retrievalQualityFlag"],
    },
    "algorithmCandidates": {
        "subpath": "algorithmCandidates/{algorithm}",
        "group_token": "alg{algorithm}",
        "default": ["soilMoisture", "soilMoistureUncertainty", "retrievalQualityFlag"],
    },
    "ancillaryData": {
        "subpath": "ancillaryData",
        "group_token": "anc",
        "default": None,  # all discovered layers
    },
    "radarData": {
        "subpath": "radarData/frequency{frequency}",
        "group_token": "rad{frequency}",
        "default": ["sigma0HH", "sigma0HV"],
    },
}

DEFAULT_LAYER_GROUP = "soilMoisture"

# Short filename tokens for the layers seen in the product spec.  A layer that
# is not listed (a new or renamed one) falls back to a lowercased name, so an
# unknown layer still converts rather than failing.
SME2_LAYER_TOKENS = {
    "soilMoisture": "sm",
    "soilMoistureUncertainty": "smunc",
    "retrievalQualityFlag": "rqf",
    "surfaceQualityFlag": "sqf",
    "algorithmParameterBeta": "beta",
    "algorithmParameterGamma": "gamma",
    "landCover": "lc",
    "localIncidenceAngle": "lia",
    "localIncidenceAngleUncertainty": "liaunc",
    "waterbodyFraction": "wbf",
    "sigma0HH": "s0hh",
    "sigma0HV": "s0hv",
    "sigma0VV": "s0vv",
    "sigma0VH": "s0vh",
    "noiseEquivalentBackscatterHH": "nebhh",
    "noiseEquivalentBackscatterHV": "nebhv",
    "noiseEquivalentBackscatterVV": "nebvv",
    "noiseEquivalentBackscatterVH": "nebvh",
    "numberOfLooksHH": "nlhh",
    "numberOfLooksHV": "nlhv",
    "numberOfLooksVV": "nlvv",
    "numberOfLooksVH": "nlvh",
}

# 1-D grid axes that must be windowed alongside the rasters.  The name decides
# the axis rather than the length, so a square window stays unambiguous.
_COL_AXES = {"xCoordinates", "longitude", "EASEGridColumnIndex"}
_ROW_AXES = {"yCoordinates", "latitude", "EASEGridRowIndex"}

# Datasets that describe the grid itself rather than a data layer; excluded
# from --vars and from the -lg layer listing.
_NON_LAYER = _COL_AXES | _ROW_AXES | {
    "projection", "xCoordinateSpacing", "yCoordinateSpacing",
    "listOfPolarizations", "listOfFrequencies", "centerFrequency",
    "rangeBandwidth",
}


def _layer_token(var):
    """Short filename token for a layer name."""
    return SME2_LAYER_TOKENS.get(var, var.lower())


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


def _scalar(v):
    if isinstance(v, np.ndarray):
        return v.reshape(-1)[0].item()
    if hasattr(v, "item"):
        return v.item()
    return v


# =========================================================
# 1. HDF5 INSPECTION & GRID INFO
# =========================================================


def available_algorithms_sme2(h5_handle):
    """Algorithm candidate sub-groups present (e.g. ['DSG'])."""
    ap = f"{SME2_GRID_BASE}/algorithmCandidates"
    if ap not in h5_handle:
        return []
    return [k for k in sorted(h5_handle[ap].keys())
            if isinstance(h5_handle[f"{ap}/{k}"], h5py.Group)]


def available_frequencies_sme2(h5_handle):
    """Frequency codes present under radarData (e.g. ['A', 'B'])."""
    rp = f"{SME2_GRID_BASE}/radarData"
    if rp not in h5_handle:
        return []
    return [k.replace("frequency", "") for k in sorted(h5_handle[rp].keys())
            if k.startswith("frequency")]


def resolve_group_path(h5_handle, layer_group, algorithm=None, frequency=None):
    """Absolute HDF5 path of a layer group, filling in algorithm / frequency.

    Defaults to the first algorithm and the first frequency present, so a
    granule carrying only DSG (or only frequency A) needs no extra flags.
    """
    spec = SME2_LAYER_GROUPS.get(layer_group)
    if spec is None:
        raise KeyError(f"Unknown layer group '{layer_group}'.")
    sub = spec["subpath"]
    if "{algorithm}" in sub:
        algs = available_algorithms_sme2(h5_handle)
        if algorithm is None:
            if not algs:
                raise KeyError("No algorithmCandidates sub-group in this granule.")
            algorithm = algs[0]
        elif algorithm not in algs:
            raise KeyError(f"Algorithm '{algorithm}' not in this granule "
                           f"(present: {algs}).")
        sub = sub.format(algorithm=algorithm)
    if "{frequency}" in sub:
        freqs = available_frequencies_sme2(h5_handle)
        if frequency is None:
            if not freqs:
                raise KeyError("No radarData/frequency* sub-group in this granule.")
            frequency = freqs[0]
        elif frequency not in freqs:
            raise KeyError(f"Frequency '{frequency}' not in this granule "
                           f"(present: {freqs}).")
        sub = sub.format(frequency=frequency)
    path = SME2_GRID_BASE if not sub else f"{SME2_GRID_BASE}/{sub}"
    if path not in h5_handle:
        raise KeyError(f"Group path '{path}' not found in SME2 file.")
    return path, algorithm, frequency


def group_token(layer_group, algorithm=None, frequency=None):
    """Filename token for a resolved layer group."""
    tok = SME2_LAYER_GROUPS[layer_group]["group_token"]
    return tok.format(algorithm=(algorithm or ""), frequency=(frequency or ""))


def _grid_shape(h5_handle):
    """(nrows, ncols) of the shared SME2 grid."""
    x = h5_handle[f"{SME2_GRID_BASE}/xCoordinates"]
    y = h5_handle[f"{SME2_GRID_BASE}/yCoordinates"]
    return int(y.shape[0]), int(x.shape[0])


def discover_layers(h5_handle, group_path, nrows=None, ncols=None):
    """Data layers directly under *group_path* that sit on the shared grid.

    Returns ``{name: {"dtype", "nodata", "token"}}``.  Discovery rather than a
    hardcoded table is what keeps a layer that moves between release tiers
    (surfaceQualityFlag) from being silently dropped.
    """
    if nrows is None or ncols is None:
        nrows, ncols = _grid_shape(h5_handle)
    out = {}
    grp = h5_handle[group_path]
    for name in sorted(grp.keys()):
        item = grp[name]
        if isinstance(item, h5py.Group) or name in _NON_LAYER:
            continue
        if item.ndim < 2 or item.shape[-2:] != (nrows, ncols):
            continue
        nod = item.attrs.get("_FillValue")
        out[name] = {
            "dtype": str(item.dtype),
            "nodata": (_scalar(nod) if nod is not None else None),
            "token": _layer_token(name),
        }
    return out


def inspect_h5_structure_sme2(f):
    """Scan an SME2 HDF5 file and return its grid geometry and layer groups."""
    if SME2_GRID_BASE not in f:
        return {"error": f"Path {SME2_GRID_BASE} not found in H5 (not an SME2 file?)."}

    poly_geo = "Not Found"
    ident = "/science/LSAR/identification/boundingPolygon"
    if ident in f:
        try:
            poly_geo = _decode(f[ident][()]).strip()
        except Exception as e:  # noqa: BLE001
            poly_geo = f"Metadata Error: {e}"

    x = f[f"{SME2_GRID_BASE}/xCoordinates"][()]
    y = f[f"{SME2_GRID_BASE}/yCoordinates"][()]
    crs = _ensure_utm_south(
        _crs_from_projection(f[f"{SME2_GRID_BASE}/projection"][()]), f)
    nrows, ncols = int(len(y)), int(len(x))

    structure = {
        "poly_geo": poly_geo,
        "crs": crs,
        "ncols": ncols,
        "nrows": nrows,
        "res_x": float(x[1] - x[0]) if len(x) > 1 else 0.0,
        "res_y": float(y[1] - y[0]) if len(y) > 1 else 0.0,
        "algorithms": available_algorithms_sme2(f),
        "frequencies": available_frequencies_sme2(f),
        "groups": {},
    }
    # EASE-Grid window on the global grid, when the product carries it
    for key, ds in (("ease_row", "EASEGridRowIndex"), ("ease_col", "EASEGridColumnIndex")):
        p = f"{SME2_GRID_BASE}/{ds}"
        if p in f:
            v = f[p][()]
            structure[key] = (int(v[0]), int(v[-1]))

    for gname, spec in SME2_LAYER_GROUPS.items():
        variants = [(None, None)]
        if "{algorithm}" in spec["subpath"]:
            variants = [(a, None) for a in structure["algorithms"]]
        elif "{frequency}" in spec["subpath"]:
            variants = [(None, fr) for fr in structure["frequencies"]]
        for alg, freq in variants:
            try:
                gpath, alg, freq = resolve_group_path(f, gname, alg, freq)
            except KeyError:
                continue
            layers = discover_layers(f, gpath, nrows, ncols)
            if not layers:
                continue
            label = gname
            if alg:
                label = f"{gname}/{alg}"
            elif freq:
                label = f"{gname}/frequency{freq}"
            structure["groups"][label] = {
                "path": gpath, "layers": layers,
                "algorithm": alg, "frequency": freq,
                "token": group_token(gname, alg, freq),
            }
    return structure


def print_structure_sme2(structure):
    """Human-readable dump of ``inspect_h5_structure_sme2`` output."""
    if "error" in structure:
        print(f"  ERROR: {structure['error']}")
        return
    print(f"\n--- SME2 grid: {structure['crs']}, "
          f"{structure['ncols']}x{structure['nrows']}, "
          f"{abs(structure['res_x']):g} m ---")
    if "ease_row" in structure and "ease_col" in structure:
        r0, r1 = structure["ease_row"]
        c0, c1 = structure["ease_col"]
        print(f"    EASE-Grid 2.0 window: rows {r0}-{r1}, cols {c0}-{c1}")
    if structure["algorithms"]:
        print(f"    algorithm candidates: {structure['algorithms']}")
    if structure["frequencies"]:
        print(f"    radar frequencies:    {structure['frequencies']}")
    for label, gi in structure["groups"].items():
        print(f"\n  {label}/   -> filename token '{gi['token']}'")
        for var, li in gi["layers"].items():
            print(f"      {var:34s} {li['dtype']:8s} "
                  f"nodata={li['nodata']}  -> token '{li['token']}'")


def get_grid_info_sme2(h5_handle, layer_group=DEFAULT_LAYER_GROUP,
                       algorithm=None, frequency=None):
    """Read grid coordinates and projection for one layer group.

    Every SME2 sub-group repeats the same axes, so the root pair is used when a
    group does not carry its own -- they are identical either way.
    """
    gpath, algorithm, frequency = resolve_group_path(
        h5_handle, layer_group, algorithm, frequency)
    cbase = gpath if f"{gpath}/xCoordinates" in h5_handle else SME2_GRID_BASE
    x = h5_handle[f"{cbase}/xCoordinates"][()]
    y = h5_handle[f"{cbase}/yCoordinates"][()]
    proj_val = h5_handle[f"{cbase}/projection"][()]
    crs = _ensure_utm_south(_crs_from_projection(proj_val), h5_handle)
    return {
        "x": x, "y": y,
        "res_x": float(x[1] - x[0]) if len(x) > 1 else 0.0,
        "res_y": float(y[1] - y[0]) if len(y) > 1 else 0.0,
        "crs": crs, "grid_path": gpath, "layer_group": layer_group,
        "algorithm": algorithm, "frequency": frequency,
    }


def get_acquisition_metadata_sme2(h5_handle):
    """Read acquisition time, CRID and versions for filenames / raster tags."""
    meta = {}
    idb = "/science/LSAR/identification"
    for key, tag in [("zeroDopplerStartTime", "ZERO_DOPPLER_START"),
                     ("zeroDopplerEndTime", "ZERO_DOPPLER_END"),
                     ("compositeReleaseId", "CRID"),
                     ("productVersion", "PRODUCT_VERSION"),
                     ("productDoi", "PRODUCT_DOI"),
                     ("trackNumber", "TRACK"),
                     ("frameNumber", "FRAME")]:
        p = f"{idb}/{key}"
        if p in h5_handle:
            try:
                meta[tag] = str(_decode(h5_handle[p][()]))
            except Exception:  # noqa: BLE001
                pass
    start = meta.get("ZERO_DOPPLER_START", "")
    if len(start) >= 10:
        meta["ACQUISITION_DATE"] = start[:10]
    sv = f"/science/LSAR/{SME2_PRODUCT}/metadata/processingInformation/algorithms/softwareVersion"
    if sv in h5_handle:
        try:
            meta["SOFTWARE_VERSION"] = str(_decode(h5_handle[sv][()]))
        except Exception:  # noqa: BLE001
            pass
    return meta


# =========================================================
# 2. WINDOW RESOLUTION
# =========================================================


def _resolve_window(info, srcwin, projwin, projwin_srs, verbose=False):
    """Return (col, row, w, h) on the shared SME2 grid.

    *projwin* corners are inferred / reprojected to the grid CRS first: the grid
    is EASE-Grid metres, so lon/lat corners taken literally would clamp to an
    edge pixel and yield a 1x1 window.
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
    """Map extent [ulx, uly, lrx, lry] for a pixel window on the grid."""
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


def _read_sme2_layer(fh, info, var, col, row, w, h):
    """Read one windowed SME2 layer as (array, dtype, nodata, is_integer).

    Integer layers (quality flags, land cover, looks) keep their native dtype
    and ``_FillValue``; float layers follow the GCOV/GUNW convention and are
    returned as float32 with NaN nodata, so the fill value never survives into
    a statistic.
    """
    dpath = f"{info['grid_path']}/{var}"
    ds = fh[dpath]
    data = ds[row:row + h, col:col + w]
    nod = ds.attrs.get("_FillValue")

    if np.issubdtype(data.dtype, np.integer):
        return data, str(data.dtype), (None if nod is None else _scalar(nod)), True

    out = data.astype(np.float32)
    if nod is not None:
        out[out == np.float32(_scalar(nod))] = np.nan
    out[~np.isfinite(out)] = np.nan
    return out, "float32", np.nan, False


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
    return _reproject_ancillary_band(
        data, src_transform=src_tf, src_crs=src_crs,
        dst_transform=dst_tf, dst_crs=dst_crs,
        dst_width=dst_w, dst_height=dst_h,
        resample_name=resampling.name if hasattr(resampling, "name") else "bilinear",
        num_threads=n_threads)


def _process_single_file_sme2(
    h5_url, variable_names, output_dir_or_file, srcwin, projwin, projwin_srs,
    layer_group, algorithm, frequency, output_format, vrt, downscale_factor,
    target_align_pixels, input_fs, output_fs, is_batch=False, cache=None,
    keep=False, use_earthdata=False, verbose=False, target_srs=None,
    target_res=None, resample="bilinear", num_threads=None, groups=None,
):
    """Convert one SME2 HDF5 file to COG/GTiff bands or a self-contained h5 subset.

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
        acq = get_acquisition_metadata_sme2(fh)
        date_str = acq.get("ACQUISITION_DATE", "")
        info = get_grid_info_sme2(fh, layer_group, algorithm, frequency)
        col, row, w, h = _resolve_window(info, srcwin, projwin, projwin_srs, verbose)

        # Default layer set: the group's catalogue default where those layers are
        # actually present, otherwise everything discovered on the grid.  Resolved
        # per granule because layers move between release tiers.
        present = discover_layers(fh, info["grid_path"])
        if not variable_names:
            dflt = SME2_LAYER_GROUPS[layer_group]["default"]
            variable_names = [v for v in (dflt or []) if v in present] or sorted(present)

        # ---- h5 subset branch -------------------------------------------
        if output_format.lower() == "h5":
            if verbose:
                print(f"    Window on the shared grid: col={col} row={row} "
                      f"w={w} h={h} @ {info['crs']}", flush=True)
            data_bytes = _subset_sme2(fh, info, col, row, w, h,
                                      verbose=verbose, groups=groups)
            out_path = final_base + "-EBD_SME2.h5"
            write_bytes(out_path, data_bytes)
            if verbose:
                print(f"    Wrote {out_path} ({len(data_bytes) / 1e6:.1f} MB)", flush=True)
            return {"h5_url": h5_url, "output": out_path, "format": "h5",
                    "date": date_str}

        # ---- raster branch ----------------------------------------------
        src_tf = _src_transform(info, col, row, w, h)
        src_crs = _parse_crs(info["crs"])
        if verbose:
            print(f"    {layer_group} window col={col} row={row} w={w} h={h} "
                  f"@ {info['crs']}", flush=True)

        needs_reproject = target_srs is not None
        if needs_reproject:
            dst_crs = _parse_crs(target_srs)
            left = src_tf.c
            top = src_tf.f
            right = left + w * abs(info["res_x"])
            bottom = top - h * abs(info["res_y"])
            res_kw = {"resolution": (target_res[0], target_res[1])} if target_res else {}
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

        gtok = group_token(layer_group, info["algorithm"], info["frequency"])
        generated = []
        band_infos = []
        out_tf, out_crs, w_out, h_out = src_tf, src_crs, w, h
        for var in variable_names:
            if var not in present:
                print(f"    [WARN] '{var}' is not a layer of {layer_group} in this "
                      f"granule (present: {sorted(present)}); skipped.", flush=True)
                continue
            arr, out_dtype, nodata, is_int = _read_sme2_layer(
                fh, info, var, col, row, w, h)

            if downscale_factor and downscale_factor > 1:
                if is_int:
                    arr = _downscale_block(arr[np.newaxis], downscale_factor,
                                           "mask_priority")[0].astype(out_dtype)
                else:
                    arr = perform_downscaling(arr[np.newaxis], downscale_factor)[0]

            if needs_reproject:
                rs = Resampling.nearest if is_int else _get_resampling(resample)
                arr = _warp_band(arr, out_dtype, src_tf, src_crs, dst_tf, dst_crs,
                                 dst_w, dst_h, rs, nodata, num_threads)
                out_tf, out_crs, h_out, w_out = dst_tf, dst_crs, dst_h, dst_w
            else:
                out_tf, out_crs, h_out, w_out = src_tf, src_crs, arr.shape[0], arr.shape[1]

            token = _layer_token(var)
            band_path = final_base + f"-EBD_{gtok}_{token}.tif"

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
            if info["algorithm"]:
                tags["OPENSEPPO_ALGORITHM"] = info["algorithm"]
            if info["frequency"] and layer_group == "radarData":
                tags["OPENSEPPO_FREQUENCY"] = info["frequency"]
            units = fh[f"{info['grid_path']}/{var}"].attrs.get("units")
            if units is not None:
                tags["UNITS"] = str(_decode(units))
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
            tok_str = "".join(b["token"] for b in band_infos)
            vrt_path = final_base + f"-EBD_{gtok}_{tok_str}.vrt"
            files = [b["path"] for b in band_infos]
            names = [b["var"] for b in band_infos]
            xml = generate_vrt_xml_single_step(
                w_out, h_out, out_tf, out_crs, files, names, date_str,
                dtype=band_infos[0]["dtype"], nodata=band_infos[0]["nodata"],
                metadata={"OPENSEPPO_LAYER_GROUP": layer_group})
            write_bytes(vrt_path, xml.encode("utf-8"))
            generated.append(vrt_path)

        return {"h5_url": h5_url, "outputs": generated, "format": output_format,
                "date": date_str, "crs": info["crs"], "transform": out_tf,
                "w": w_out, "h": h_out, "bands": band_infos,
                "layer_group": layer_group}
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
# 4. SELF-CONTAINED H5 SUBSET
# =========================================================


def _subset_sme2(src_f, info, col, row, w, h, verbose=False, groups=None):
    """Return bytes of a fully self-contained SME2 HDF5 subset.

    Because every 2-D layer shares one grid, the (col, row, w, h) window applies
    unchanged throughout -- there is no per-group re-derivation as in GUNW.
    Copied:

      * root attributes
      * /science/LSAR/identification/ (verbatim; boundingPolygon recomputed for
        the window)
      * /science/LSAR/SME2/metadata/ (verbatim -- processingInformation and
        sourceData carry no map grid)
      * /science/LSAR/SME2/grids/ -- every dataset on the shared grid windowed,
        including the 1-D axes (xCoordinates, yCoordinates, latitude, longitude
        and the EASEGridRow/ColumnIndex arrays, so the subset keeps its position
        on the global EASE grid)

    *groups* restricts which grid sub-groups are carried (default: all present);
    the root soil-moisture layers are always kept.
    """
    nrows, ncols = len(info["y"]), len(info["x"])

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

            def _window_group(src_grp, dst_parent, name):
                """Copy a grids sub-group, windowing everything on the shared grid."""
                g = dst_parent.require_group(name)
                _cpattr(src_grp, g)
                for k in src_grp:
                    child = src_grp[k]
                    if isinstance(child, h5py.Group):
                        _window_group(child, g, k)
                        continue
                    ds = child
                    if k in _COL_AXES and ds.ndim == 1 and ds.shape[0] == ncols:
                        data = ds[col:col + w]
                        sliced = True
                    elif k in _ROW_AXES and ds.ndim == 1 and ds.shape[0] == nrows:
                        data = ds[row:row + h]
                        sliced = True
                    elif ds.ndim >= 2 and ds.shape[-2:] == (nrows, ncols):
                        data = ds[..., row:row + h, col:col + w]
                        sliced = True
                    else:
                        data = ds[()]
                        sliced = False
                    if sliced and getattr(data, "ndim", 0) >= 2:
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
                    x_sub = info["x"][col:col + w]
                    y_sub = info["y"][row:row + h]
                    wkt = _geo_grid_bounding_polygon_wkt(
                        x_sub, y_sub,
                        info["res_x"] or float(x_sub[1] - x_sub[0]),
                        info["res_y"] or float(y_sub[1] - y_sub[0]),
                        info["crs"])
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

            # --- metadata/ (no map grid -> verbatim) ---
            meta_base = f"/science/LSAR/{SME2_PRODUCT}/metadata"
            if meta_base in src_f:
                prod_grp = dst.require_group(f"science/LSAR/{SME2_PRODUCT}")
                _cpattr(src_f[f"/science/LSAR/{SME2_PRODUCT}"], prod_grp)
                _cpgrp(meta_base, prod_grp, "metadata")

            # --- grids/ ---
            src_grids = src_f[SME2_GRID_BASE]
            present_groups = [k for k in src_grids.keys()
                              if isinstance(src_grids[k], h5py.Group)]
            if groups:
                selected = [g for g in present_groups if g in set(groups)]
                if not selected:
                    print(f"    [WARN] none of the requested groups {list(groups)} "
                          f"are present ({present_groups}); carrying all.", flush=True)
                    selected = present_groups
            else:
                selected = present_groups
            dropped = [g for g in present_groups if g not in selected]
            if dropped and verbose:
                print(f"    Dropping grid group(s) from subset: {dropped}", flush=True)

            grids_dst = dst.require_group(SME2_GRID_BASE.lstrip("/"))
            _cpattr(src_grids, grids_dst)
            # root-level datasets (soil moisture layers + the shared axes)
            for k in src_grids:
                if isinstance(src_grids[k], h5py.Group):
                    continue
                ds = src_grids[k]
                if k in _COL_AXES and ds.ndim == 1 and ds.shape[0] == ncols:
                    data = ds[col:col + w]
                    sliced = True
                elif k in _ROW_AXES and ds.ndim == 1 and ds.shape[0] == nrows:
                    data = ds[row:row + h]
                    sliced = True
                elif ds.ndim >= 2 and ds.shape[-2:] == (nrows, ncols):
                    data = ds[..., row:row + h, col:col + w]
                    sliced = True
                else:
                    data = ds[()]
                    sliced = False
                if sliced and getattr(data, "ndim", 0) >= 2:
                    cy = min(512, data.shape[-2])
                    cx = min(512, data.shape[-1])
                    d = grids_dst.create_dataset(
                        k, data=data, chunks=(cy, cx),
                        compression=ds.compression or "gzip",
                        compression_opts=(ds.compression_opts
                                          if ds.compression_opts is not None else 1),
                        shuffle=ds.shuffle)
                else:
                    d = grids_dst.create_dataset(k, data=data)
                _cpattr(ds, d)
            for gname in selected:
                _window_group(src_grids[gname], grids_dst, gname)

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


def process_sme2_task(
    h5_url, variable_names=None, output_path=None, srcwin=None, projwin=None,
    projwin_srs=None, layer_group=DEFAULT_LAYER_GROUP, algorithm=None,
    frequency=None, output_format="COG", vrt=True, downscale_factor=None,
    target_align_pixels=True, input_auth=None, output_auth=None,
    list_grids=False, verbose=False, cache=None, keep=False, target_srs=None,
    target_res=None, resample="bilinear", num_threads=None, read_threads=8,
    groups=None,
):
    """Batch-convert one or more SME2 HDF5 files to COG/GTiff or h5 subsets."""
    urls = [h5_url] if isinstance(h5_url, str) else list(h5_url)
    use_earthdata = bool(input_auth and input_auth.get("use_earthdata"))

    input_fs = None
    if urls and urls[0].startswith("s3://"):
        input_fs = create_s3_fs(input_auth)
    output_fs = None
    if output_path and output_path.startswith("s3://"):
        output_fs = create_s3_fs(output_auth)

    if use_earthdata and HAS_EARTHACCESS:
        _earthaccess_login(verbose=verbose)

    if list_grids:
        first = urls[0]
        local = first
        if cache and (first.startswith("s3://") or first.startswith("https://")):
            local = cache_to_local(first, keep=False, use_earthdata=use_earthdata,
                                   fs=input_fs)
        fh = open_h5_lazy(local, None if local != first else input_fs)
        try:
            print_structure_sme2(inspect_h5_structure_sme2(fh))
        finally:
            fh.close()
        return "Listed grids."

    is_batch = len(urls) > 1 or (output_path and output_path.endswith("/"))
    results = []
    for i, url in enumerate(urls):
        if verbose:
            print(f"\n[{i + 1}/{len(urls)}] {os.path.basename(url)}", flush=True)
        try:
            res = _process_single_file_sme2(
                url, variable_names, output_path, srcwin, projwin, projwin_srs,
                layer_group, algorithm, frequency, output_format, vrt,
                downscale_factor, target_align_pixels, input_fs, output_fs,
                is_batch=is_batch, cache=cache, keep=keep,
                use_earthdata=use_earthdata, verbose=verbose,
                target_srs=target_srs, target_res=target_res, resample=resample,
                num_threads=(num_threads or read_threads), groups=groups)
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            print(f"    [FAIL] {os.path.basename(url)}: {exc}", flush=True)
            if verbose:
                import traceback
                traceback.print_exc()

    ok = [r for r in results if r]
    return f"Processed {len(ok)}/{len(urls)} SME2 file(s)."
