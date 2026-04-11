#!/usr/bin/env python
"""
seppo_nisar_gcov_convert_S -- NISAR S-band GCOV HDF5 to Cloud Optimized GeoTIFF converter
*********************************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Convert NISAR S-band GCOV HDF5 files to Cloud Optimized GeoTIFF (COG) with
optional reprojection, downscaling, VRT time-series stacking, coherence, and
RH/RV ratio output.

Usage Examples:
1. Standard conversion (default power, float32):
    seppo_nisar_gcov_convert --h5 urls.txt --output s3://bucket/out/

2. Convert to amplitude (uint16):
    seppo_nisar_gcov_convert --h5 file.h5 --output out/ -amp

3. Convert to DN (uint8, scaled 1-255):
    seppo_nisar_gcov_convert --h5 file.h5 --output out/ -DN

4. Reproject to WGS84, fill interior holes, cubic resampling:
    seppo_nisar_gcov_convert --h5 file.h5 --output out/ -t_srs 4326 --fill_holes

5. Rebuild VRTs only:
    seppo_nisar_gcov_convert --rebuild-only --output s3://bucket/out/ -dB
"""

import sys
import os
import re
import time
import glob
import argparse
import shlex
from pprint import pprint
from collections import defaultdict
import math
import rasterio
from rasterio.transform import from_origin
import openseppo.nisar.nisar_tools_S as nisar_tools


# -- seppo_parse_args shim -----------------------------------------------------
# Drops the config-file override feature from seppopy.tools.args; all CLI flags
# are preserved and work identically.


def seppo_parse_args(parser, a):
    return parser.parse_args(a[1:])


# -----------------------------------------------------------------------------

asf_buckets = ["sds-n-cumulus-prod-nisar-products", "sds-n-cumulus-prod-nisar-ur-products"]


def myargsparse(a):
    """
    Parses arguments for the NISAR processor.
    """

    class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        pass

    if isinstance(a, str):
        a = shlex.split(a)

    thisProg = os.path.basename(a[0])
    description = "Convert NISAR S-band HDF5 GCOV data to Cloud Optimized GeoTIFF (COG) with optional VRT stacking."

    epilog = ""

    parser = argparse.ArgumentParser(prog=thisProg, description=description, epilog=epilog, formatter_class=CustomFormatter)

    # --- I/O Arguments ---
    parser.add_argument("-i", "--h5", type=str, nargs="+", help="Input H5 URL(s) or path to a text file containing URLs.")
    parser.add_argument("-o", "--output", type=str, required=False, help="Output directory path (S3 or local). Must end in '/' for batch processing.")

    # Defaults set to None to allow auto-detection in nisar_tools
    parser.add_argument("-vars", "--vars", nargs="+", default=None, help="Grid Variables to extract, e.g. RHRH RVRV RHRV. If omitted, RHRH and RVRV are used by default.")
    parser.add_argument("-f", "--freq", type=str, default="A", help="Frequency (A/B). If omitted, defaults to A", choices=["A", "B"])

    # List Grids Flag
    parser.add_argument("-lg", "--list_grids", action="store_true", help="Scan the first H5 file and list all available grids/frequencies/variables, then exit.")

    # --- Mode Flags (Mutually Exclusive) ---
    # Maps flags to 'args.mode' variable
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("-DN", "--DN", action="store_const", dest="mode", const="DN", help="Set scaling mode to DN (uint8 scaled 1-255).")
    mode_group.add_argument("-amp", "--amp", action="store_const", dest="mode", const="AMP", help="Set scaling mode to Amplitude (uint16).")
    mode_group.add_argument("-dB", "--dB", action="store_const", dest="mode", const="dB", help="Set scaling mode to dB (float32).")
    mode_group.add_argument("-pwr", "--power", action="store_const", dest="mode", const="pwr", help="Set scaling mode to Power (raw float32). Default behavior.")

    # --- Output Format ---
    parser.add_argument("-of", "--output_format", type=str, default="COG", choices=["COG", "GTiff", "h5"], help="Output format: COG (default), GTiff (BigTIFF), h5 (raw HDF5 subset).")

    # --- Other Processing Options ---
    parser.add_argument("-dpratio", "--dualpol_ratio", action="store_true", help="Compute S-band RH/RV power ratio: RHRH/RVRV (CR mode).")
    parser.add_argument("-sigma0", "--sigma0", action="store_true", help="Convert gamma0 backscatter to sigma0 by multiplying power values with the rtcGammaToSigmaFactor layer from the GCOV file. Applied before any downscaling or resampling.")
    parser.add_argument("-d", "--downscale", type=int, default=None, help="Downscale factor (integer). E.g., 2 for 2x2 block averaging.")

    # --- VRT & Output Structure ---
    parser.add_argument("--no_vrt", action="store_true", help="Disable generation of per-snapshot VRTs.")
    parser.add_argument("--no_time_series", action="store_true", help="Disable generation of Time Series VRT stacks (even if other VRTs are on).")
    parser.add_argument("--no_single_bands", action="store_false", dest="single_bands", help="Save multi-band COG instead of separate files per pol.")

    # --- Subsetting ---
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-srcwin", "--srcwin", nargs=4, type=int, metavar=("XOFF", "YOFF", "XSIZE", "YSIZE"), help="Pixel subset window.")
    group.add_argument("-projwin", "--projwin", nargs=4, type=float, metavar=("ULX", "ULY", "LRX", "LRY"), help="Geographic subset window. Coordinates are in native or target CRS unless -projwin_srs is given.")
    parser.add_argument("-projwin_srs", "--projwin_srs", type=str, help="CRS of the -projwin coordinates (e.g. EPSG:4326). If omitted, -projwin is in the native or target raster CRS.")
    parser.add_argument("--no_tap", action="store_true", help="Disable pixel-grid alignment (tap). By default, output origin is snapped to integer multiples of the target pixel size.")
    parser.add_argument("-t_srs", "--target_srs", type=str, default=None, help="Target CRS for output (e.g. EPSG:4326 or bare 4326). If omitted, output stays in native UTM CRS.")
    parser.add_argument("-tr", "--target_res", type=float, nargs=2, metavar=("XRES", "YRES"), default=None, help="Explicit output pixel size in target CRS units (e.g. -tr 0.001 0.001 for ~100m in degrees). Only used with --target_srs.")
    parser.add_argument("--resample", type=str, default="cubic", help="Resampling method for reprojection (nearest/bilinear/cubic/cubicspline/lanczos/average). Default: cubic.")
    parser.add_argument("--fill_holes", action="store_true", help="Fill interior NaN/+/-inf pixels (those enclosed by valid data) with their nearest valid neighbour. Frame-boundary nodata is unaffected. Prevents the resampling kernel from seeing isolated invalid pixels inside the valid image area.")
    parser.add_argument("--warp_threads", type=int, default=None, metavar="N", help="Number of threads for reprojection. Default: all available CPU cores.")
    parser.add_argument("--read_threads", type=int, default=8, metavar="N", help="Number of parallel S3/HTTPS connections for reading HDF5 chunks. Each bandxstripe gets its own connection. Default: 8.")

    # --- Authentication (Distinct Input/Output) ---
    parser.add_argument("--profile", type=str, help="AWS Profile name (applies to both Input and Output unless overridden).")
    parser.add_argument("--input_profile", type=str, help="AWS Profile specifically for reading Input H5s.")
    parser.add_argument("--output_profile", type=str, help="AWS Profile specifically for writing Output COGs.")
    # --- Management Flags ---
    parser.add_argument("-ro", "--rebuild_only", action="store_true", help="Skip processing and rebuild VRTs in the output folder from existing TIFs. Auto-detects scaling mode.")
    parser.add_argument("-S", "--show_vrts", action="store_true", help="Print a summary of all VRTs and TIFs in the output folder (requires -o). Auto-detects scaling mode.")
    parser.add_argument("-vsis3", "--vsis3", action="store_true", help="With -S: print paths as /vsis3/ URIs for direct use in QGIS/GDAL.")
    parser.add_argument("--reset_vrts", action="store_true", help="Delete all existing VRTs in the output folder before rebuilding. Use with -ro for a clean slate.")
    parser.add_argument("-cache", "--cache", default=None, action="store", help="Local path to a directory to cache files from urls first. Accepts 'y' or 'yes' to create a local temp directory (on /dev/shm or /tmp if available).")
    parser.add_argument("-keep", "--keep_cached", action="store_true", help="Use with -cache to keep to cached h5 file locally.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")

    # Set Defaults
    parser.set_defaults(single_bands=True)
    parser.set_defaults(mode="pwr")  # Default to power if no flag set

    args = seppo_parse_args(parser, a)
    args.use_earthdata = False  # auto-detected later in processing() based on input URLs

    if args.verbose:
        pprint(vars(args))

    # --- CONDITIONAL REQUIREMENT CHECK ---
    # Output is required UNLESS we are only listing grids
    if not args.list_grids and not args.output:
        parser.error("the following arguments are required: --output/-o (unless --list_grids is used)")

    # H5 is required unless we are only rebuilding or showing VRTs
    if not args.rebuild_only and not args.show_vrts and not args.h5:
        parser.error("the following arguments are required: --h5/-i (unless --rebuild_only or --show_vrts is used)")

    return args


def get_auth_dict(profile_arg, use_earthdata=False):
    """Helper to construct auth dictionary based on args and env vars."""
    auth = {}

    # 1. Earthdata (Input only usually)
    if use_earthdata:

        auth["use_earthdata"] = True
        return auth

    # 2. Profile
    if profile_arg:
        auth["profile"] = profile_arg
        return auth

    # 3. Environment Variables (Fallback)
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        auth["key"] = os.environ.get("AWS_ACCESS_KEY_ID")
        auth["secret"] = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if os.environ.get("AWS_SESSION_TOKEN"):
            auth["token"] = os.environ.get("AWS_SESSION_TOKEN")

    return auth


# =========================================================
# TRACK VRT HELPERS
# =========================================================


_KNOWN_ANC_SUFFIXES = {"mask", "nlooks", "gamma2sigma"}


def _parse_nisar_tif_meta(tif_path):
    """Parse NISAR metadata tokens from a COG TIF filename. Returns dict or None.

    Handles both backscatter TIFs (3 EBD tokens: freq, pol, mode) and
    ancillary TIFs (2 EBD tokens: freq, suffix like mask/nlooks/gamma2sigma).
    """
    basename = os.path.basename(tif_path)
    if "-EBD_" not in basename:
        return None
    nisar_base, ebd_raw = basename.split("-EBD_", 1)
    if not basename.endswith(".tif"):
        return None
    ebd_raw = ebd_raw.removesuffix(".tif")
    ebd_tokens = ebd_raw.split("_")  # ["A", "hh", "AMP"] or ["A", "mask"]
    if len(ebd_tokens) < 2:
        return None
    tokens = nisar_base.split("_")
    if len(tokens) < 18 or tokens[0] != "NISAR":
        return None
    try:
        is_anc = len(ebd_tokens) == 2 and ebd_tokens[1] in _KNOWN_ANC_SUFFIXES
        return {
            "il": tokens[1],
            "pt": tokens[2],
            "prod": tokens[3],
            "cycle": int(tokens[4]),
            "track": int(tokens[5]),
            "direction": tokens[6],
            "frame": int(tokens[7]),
            "mode": tokens[8],
            "polarization": tokens[9],
            "obs_mode": tokens[10],
            "start_time": tokens[11],
            "end_time": tokens[12],
            "crid": tokens[13],
            "accuracy": tokens[14],
            "freq": ebd_tokens[0],
            "pol_str": ebd_tokens[1] if not is_anc else ebd_tokens[1],
            "mode_str": ebd_tokens[2] if len(ebd_tokens) >= 3 else None,
            "is_ancillary": is_anc,
            "path": tif_path,
            "nisar_base": nisar_base,
        }
    except (IndexError, ValueError):
        return None


_KNOWN_BSC_MODES = ("pwr", "dB", "AMP", "DN")


def _list_all_nisar_tifs(output_path, frequency, mode_str=None, output_fs=None):
    """List all NISAR TIF files (backscatter + ancillary) in output_path.

    If *mode_str* is None, auto-detect by scanning for all known modes.
    """
    tag = f"-EBD_{frequency}_"
    bsc_suffixes = [f"_{mode_str}.tif"] if mode_str else [f"_{m}.tif" for m in _KNOWN_BSC_MODES]
    anc_suffixes = tuple(f"_{s}.tif" for s in _KNOWN_ANC_SUFFIXES)

    def _matches(f):
        if not f.endswith(".tif") or tag not in f:
            return False
        return any(f.endswith(s) for s in bsc_suffixes) or any(f.endswith(s) for s in anc_suffixes)

    if output_fs:
        bucket_path = output_path.replace("s3://", "")
        try:
            files = output_fs.ls(bucket_path)
            return [f"s3://{f}" for f in files if _matches(f)]
        except Exception:
            return []
    results = []
    for bsuf in bsc_suffixes:
        results.extend(glob.glob(os.path.join(output_path, f"*{tag}*{bsuf}")))
    for s in _KNOWN_ANC_SUFFIXES:
        results.extend(glob.glob(os.path.join(output_path, f"*{tag}{s}.tif")))
    return sorted(set(results))


def _read_tif_geo(tif_path, output_fs=None):
    """Return dict with geo info and GDAL tags from a TIF, or None on error."""
    try:
        if output_fs:
            with output_fs.open(tif_path, "rb") as fobj:
                with rasterio.open(fobj) as ds:
                    tags = ds.tags()
                    return {"transform": ds.transform, "w": ds.width, "h": ds.height, "crs_wkt": ds.crs.to_wkt(), "dtype": ds.dtypes[0], "nodata": ds.nodata, "tags": tags}
        with rasterio.open(tif_path) as ds:
            tags = ds.tags()
            return {"transform": ds.transform, "w": ds.width, "h": ds.height, "crs_wkt": ds.crs.to_wkt(), "dtype": ds.dtypes[0], "nodata": ds.nodata, "tags": tags}
    except Exception:
        return None


def _vrt_src_entry(path):
    """Return (vrt_src_path, relativeToVRT_attr) for use in a VRT SourceFilename.

    S3 paths are converted to /vsis3/ and use relativeToVRT="0".
    Local paths use the basename and relativeToVRT="1" (VRT and source in same dir).
    """
    if path.startswith("s3://"):
        return "/vsis3/" + path[5:], "0"
    return os.path.basename(path), "1"


def _gdal_nodata_str(nodata, dtype):
    """Return a nodata string for VRT XML, preferring the explicit value from the TIF."""
    if nodata is not None:
        try:
            if math.isnan(nodata):
                return "nan"
        except (TypeError, ValueError):
            pass
        v = int(nodata) if nodata == int(nodata) else nodata
        return str(v)
    d = str(dtype).lower()
    return "0" if ("int" in d or "byte" in d) else "nan"


def _generate_mosaic_vrt_xml(frame_items, crs_wkt, dtype, metadata=None):
    """
    Spatial mosaic VRT: all frame_items (different extents, same date) are merged
    into a single band. Returns (xml_str, union_transform, union_w, union_h).

    frame_items: [{"path", "transform", "w", "h", "nodata"(optional)}]
    """
    vrt_dtype = nisar_tools.get_gdal_dtype(dtype)
    nodata_val = _gdal_nodata_str(frame_items[0].get("nodata"), dtype)
    res_x = abs(frame_items[0]["transform"].a)
    res_y = abs(frame_items[0]["transform"].e)

    union_ulx = min(it["transform"].c for it in frame_items)
    union_uly = max(it["transform"].f for it in frame_items)
    union_lrx = max(it["transform"].c + it["w"] * res_x for it in frame_items)
    union_lry = min(it["transform"].f - it["h"] * res_y for it in frame_items)
    union_w = max(1, int(round((union_lrx - union_ulx) / res_x)))
    union_h = max(1, int(round((union_uly - union_lry) / res_y)))
    union_tf = from_origin(union_ulx, union_uly, res_x, res_y)

    geo = f"{union_tf.c}, {union_tf.a}, {union_tf.b}, {union_tf.f}, {union_tf.d}, {union_tf.e}"
    lines = [
        f'<VRTDataset rasterXSize="{union_w}" rasterYSize="{union_h}">',
        f'  <SRS dataAxisToSRSAxisMapping="1,2">{crs_wkt}</SRS>',
        f"  <GeoTransform>{geo}</GeoTransform>",
    ]
    if metadata:
        lines.append(nisar_tools._vrt_metadata_xml(metadata))
    lines += [
        f'  <VRTRasterBand dataType="{vrt_dtype}" band="1">',
        f"    <NoDataValue>{nodata_val}</NoDataValue>",
        f"    <ColorInterp>Gray</ColorInterp>",
    ]
    for it in frame_items:
        dx = int(round((it["transform"].c - union_ulx) / res_x))
        dy = int(round((union_uly - it["transform"].f) / res_y))
        src_path, rel_attr = _vrt_src_entry(it["path"])
        lines.append(f"    <ComplexSource>\n" f'      <SourceFilename relativeToVRT="{rel_attr}">{src_path}</SourceFilename>\n' f"      <SourceBand>1</SourceBand>\n" f'      <SourceProperties RasterXSize="{it["w"]}" RasterYSize="{it["h"]}" DataType="{vrt_dtype}" BlockXSize="512" BlockYSize="512" />\n' f'      <SrcRect xOff="0" yOff="0" xSize="{it["w"]}" ySize="{it["h"]}" />\n' f'      <DstRect xOff="{dx}" yOff="{dy}" xSize="{it["w"]}" ySize="{it["h"]}" />\n' f"      <NODATA>{nodata_val}</NODATA>\n" f"    </ComplexSource>")
    lines += [
        "  </VRTRasterBand>",
        '  <OverviewList resampling="nearest">2 4 8</OverviewList>',
        "</VRTDataset>",
    ]
    return "\n".join(lines), union_tf, union_w, union_h


def _generate_ts_union_vrt_xml(crs_wkt, stack_items, dtype, nodata=None, metadata=None):
    """
    Time-series VRT with union spatial extent (one band per timestep).
    Used when items have different spatial extents (e.g. A vs D tracks).

    stack_items: [{"path", "band_idx", "date", "transform", "w", "h", "nodata"(optional)}]
    """
    vrt_dtype = nisar_tools.get_gdal_dtype(dtype)
    if nodata is None:
        nodata = stack_items[0].get("nodata")
    nodata_val = _gdal_nodata_str(nodata, dtype)
    res_x = abs(stack_items[0]["transform"].a)
    res_y = abs(stack_items[0]["transform"].e)

    union_ulx = min(it["transform"].c for it in stack_items)
    union_uly = max(it["transform"].f for it in stack_items)
    union_lrx = max(it["transform"].c + it["w"] * res_x for it in stack_items)
    union_lry = min(it["transform"].f - it["h"] * res_y for it in stack_items)
    union_w = max(1, int(round((union_lrx - union_ulx) / res_x)))
    union_h = max(1, int(round((union_uly - union_lry) / res_y)))
    union_tf = from_origin(union_ulx, union_uly, res_x, res_y)

    geo = f"{union_tf.c}, {union_tf.a}, {union_tf.b}, {union_tf.f}, {union_tf.d}, {union_tf.e}"
    lines = [
        f'<VRTDataset rasterXSize="{union_w}" rasterYSize="{union_h}">',
        f'  <SRS dataAxisToSRSAxisMapping="1,2">{crs_wkt}</SRS>',
        f"  <GeoTransform>{geo}</GeoTransform>",
    ]
    if metadata:
        lines.append(nisar_tools._vrt_metadata_xml(metadata))
    for i, it in enumerate(stack_items):
        dx = int(round((it["transform"].c - union_ulx) / res_x))
        dy = int(round((union_uly - it["transform"].f) / res_y))
        date = it.get("date", "")
        src_path, rel_attr = _vrt_src_entry(it["path"])
        lines.append(f'  <VRTRasterBand dataType="{vrt_dtype}" band="{i + 1}">\n' f"    <NoDataValue>{nodata_val}</NoDataValue>\n" f"    <Description>{date}</Description>\n" f'    <Metadata><MDI key="Date">{date}</MDI></Metadata>\n' f"    <SimpleSource>\n" f'      <SourceFilename relativeToVRT="{rel_attr}">{src_path}</SourceFilename>\n' f'      <SourceBand>{it["band_idx"]}</SourceBand>\n' f'      <SrcRect xOff="0" yOff="0" xSize="{it["w"]}" ySize="{it["h"]}" />\n' f'      <DstRect xOff="{dx}" yOff="{dy}" xSize="{it["w"]}" ySize="{it["h"]}" />\n' f"    </SimpleSource>\n" f"  </VRTRasterBand>")
    lines.append("</VRTDataset>")
    return "\n".join(lines)


def _track_vrt_filename(metas, pol_str, mode_str):
    """
    Build a NISAR time-series VRT filename from a list of metadata dicts.

    Unique (track, direction, frame) combos are sorted by (track, direction, frame).
    For <=4 combos the track, direction, and frame tokens are each hyphen-joined in
    combo order (preserving the pairing).  For >4 combos the tokens collapse to
    "999", all-directions (A, D, or A-D), "999".
    """
    m0 = metas[0]
    il, pt, prod = m0["il"], m0["pt"], m0["prod"]
    obs_mode, mode, pol = m0["obs_mode"], m0["mode"], m0["polarization"]
    freq = m0["freq"]

    cycles = sorted({m["cycle"] for m in metas})
    cycle_str = f"{min(cycles):03d}-{max(cycles):03d}" if len(cycles) > 1 else f"{cycles[0]:03d}"

    combos = sorted({(m["track"], m["direction"], m["frame"]) for m in metas})
    if len(combos) <= 4:
        track_str = "-".join(f"{t:03d}" for t, d, f in combos)
        dir_str = "-".join(d for t, d, f in combos)
        frame_str = "-".join(f"{f:03d}" for t, d, f in combos)
    else:
        all_dirs = sorted({d for _, d, _ in combos})
        track_str = "999"
        dir_str = "-".join(all_dirs)
        frame_str = "999"

    min_start = min(m["start_time"] for m in metas)
    max_end = max(m["end_time"] for m in metas)

    if mode_str:
        ebd = f"-EBD_{freq}_{pol_str}_{mode_str}.vrt"
    else:
        ebd = f"-EBD_{freq}_{pol_str}.vrt"
    return f"NISAR_{il}_{pt}_{prod}_{cycle_str}_{track_str}_{dir_str}_" f"{frame_str}_{mode}_{pol}_{obs_mode}_{min_start}_{max_end}" f"{ebd}"


def _make_ts_vrt(ts_items, crs_wkt, dtype, nodata=None, metadata=None):
    """Choose between same-extent and union time-series VRT builder."""
    if nodata is None:
        nodata = ts_items[0].get("nodata")
    same_geo = all(it["w"] == ts_items[0]["w"] and it["h"] == ts_items[0]["h"] and it["transform"] == ts_items[0]["transform"] for it in ts_items)
    if same_geo:
        return nisar_tools.generate_vrt_xml_timeseries(ts_items[0]["w"], ts_items[0]["h"], ts_items[0]["transform"], crs_wkt, ts_items, dtype=dtype, nodata=nodata, metadata=metadata)
    return _generate_ts_union_vrt_xml(crs_wkt, ts_items, dtype, nodata=nodata, metadata=metadata)


def _list_vrts_in_dir(out_dir, output_fs):
    """Return all .vrt paths in out_dir (S3 or local)."""
    import glob as _glob

    if output_fs:
        bucket_path = out_dir.replace("s3://", "")
        try:
            return [f"s3://{f}" for f in output_fs.ls(bucket_path) if f.endswith(".vrt")]
        except Exception:
            return []
    return _glob.glob(os.path.join(out_dir, "*.vrt"))


def show_output_summary(output_path, frequency, output_auth=None, vsis3=False):
    """Scan output directory and print a structured summary of existing files.

    No VRTs are built or modified -- read-only listing.
    """
    import s3fs as _s3fs

    out_dir = output_path.rstrip("/")
    output_fs = None
    if output_path.startswith("s3://"):
        auth = output_auth or {}
        if "profile" in auth:
            output_fs = _s3fs.S3FileSystem(profile=auth["profile"])
        elif "key" in auth:
            output_fs = _s3fs.S3FileSystem(key=auth["key"], secret=auth["secret"], token=auth.get("token"))
        else:
            output_fs = _s3fs.S3FileSystem(anon=False)

    # List all files in directory
    if output_fs:
        bucket_path = output_path.replace("s3://", "").rstrip("/")
        try:
            all_files = [f"s3://{f}" for f in output_fs.ls(bucket_path)]
        except Exception:
            all_files = []
    else:
        all_files = sorted(glob.glob(os.path.join(out_dir, "*")))

    tag = f"-EBD_{frequency}"
    tifs = [f for f in all_files if f.endswith(".tif") and tag in f]
    vrts = [f for f in all_files if f.endswith(".vrt") and tag in f]

    # Classify TIFs and VRTs into backscatter vs ancillary
    anc_suffixes = tuple(f"_{s}.tif" for s in _KNOWN_ANC_SUFFIXES)
    anc_vrt_suffixes = tuple(f"_{s}.vrt" for s in _KNOWN_ANC_SUFFIXES)

    summary = {"backscatter": {"single_dates": [], "mosaics": [], "ts_by_track": [], "combined_ts": []}, "ancillary": {"single_dates": [], "mosaics": [], "ts_by_track": [], "combined_ts": []}}

    for f in tifs:
        if any(f.endswith(s) for s in anc_suffixes):
            summary["ancillary"]["single_dates"].append(f)
        else:
            summary["backscatter"]["single_dates"].append(f)

    for f in vrts:
        bn = os.path.basename(f)
        is_anc = any(f.endswith(s) for s in anc_vrt_suffixes)
        cat = "ancillary" if is_anc else "backscatter"
        # Classify by name pattern
        if "-" in bn.split("_")[4] if len(bn.split("_")) > 4 else False:
            # Cycle range (e.g. 003-009) suggests time-series
            summary[cat]["ts_by_track"].append(f)
        else:
            summary[cat]["single_dates"].append(f)

    _print_vrt_summary(output_path, summary, vsis3=vsis3)


def _green(text):
    """Wrap text in green ANSI escape if stdout is a terminal."""
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        return f"\033[32m{text}\033[0m"
    return text


def _print_vrt_summary(output_path, summary, vsis3=False):
    """Print a structured VRT/TIF summary for copy-paste into a GIS application.

    *summary* is a dict with keys "backscatter" and "ancillary", each mapping
    to a dict with optional sub-keys:
        "single_dates"  -- list of paths (snapshot VRTs or single-frame TIFs)
        "mosaics"       -- list of paths (spatial mosaic VRTs)
        "ts_by_track"   -- list of paths (per-track time-series VRTs)
        "combined_ts"   -- list of paths (multi-track combined VRTs)
    If *vsis3* is True, S3 paths are printed as /vsis3/ URIs for QGIS/GDAL.
    """
    is_s3 = output_path.startswith("s3://")
    if is_s3:
        bucket = output_path.replace("s3://", "").split("/")[0]
        if vsis3:
            def key(p):
                return "/vsis3/" + p[5:] if p.startswith("s3://") else p
        else:
            def key(p):
                return p.replace("s3://", "").split("/", 1)[1]
    else:
        def key(p):
            return p

    for section, label in [("ancillary", "Ancillary"), ("backscatter", "Backscatter")]:
        data = summary.get(section, {})
        singles = data.get("single_dates", [])
        mosaics = data.get("mosaics", [])
        ts_track = data.get("ts_by_track", [])
        combined = data.get("combined_ts", [])
        if not any([singles, mosaics, ts_track, combined]):
            continue
        print(f"\n{_green(f'---> {label}:')}")
        for p in sorted(singles + mosaics):
            print(key(p))
        if ts_track:
            print(f"\n{_green(f'---> {label} time series by track:')}")
            for p in sorted(ts_track):
                print(key(p))
        if combined:
            print(f"\n{_green(f'---> {label} combined time series:')}")
            for p in sorted(combined):
                print(key(p))

    # Show path/bucket at the end for easier pasting
    if is_s3:
        if vsis3:
            print(f"\n{_green('---> /vsis3/ Path:')}")
            print(f"/vsis3/{output_path[5:].rstrip('/')}")
        else:
            print(f"\n{_green('---> Bucket:')}")
            print(bucket)
    else:
        print(f"\n{_green('---> Path:')}")
        print(output_path.rstrip("/"))


def build_track_vrts(output_path, frequency, mode_str, verbose=False, output_auth=None, vsis3=False, reset_vrts=False):
    """
    Post-process: build VRTs in four phases, then print a structured summary.

    Phase 1 -- Grid mosaic VRTs: for each (track, dir, cycle, grid), when
               multiple frames exist, build a spatial mosaic VRT.
    Phase 2 -- Single-date multi-pol VRTs: for each acquisition date, combine
               backscatter polarizations into one VRT (from mosaics or TIFs).
    Phase 3 -- Per-track time-series VRTs: one band per date, per grid variable.
    Phase 4 -- Combined multi-track time-series VRTs: when >1 track exists.
    """
    import s3fs as _s3fs

    out_dir = output_path.rstrip("/")

    output_fs = None
    if output_path.startswith("s3://"):
        auth = output_auth or {}
        if "profile" in auth:
            output_fs = _s3fs.S3FileSystem(profile=auth["profile"])
        elif "key" in auth:
            output_fs = _s3fs.S3FileSystem(key=auth["key"], secret=auth["secret"], token=auth.get("token"))
        else:
            output_fs = _s3fs.S3FileSystem(anon=False)

    # Delete all existing VRTs if --reset_vrts requested
    if reset_vrts:
        if verbose:
            print("    Deleting existing VRTs...", flush=True)
        if output_fs:
            bucket_path = output_path.replace("s3://", "").rstrip("/")
            try:
                existing = output_fs.ls(bucket_path)
                vrt_files = [f for f in existing if f.endswith(".vrt")]
                for vf in vrt_files:
                    output_fs.rm(vf)
                if verbose:
                    print(f"    Deleted {len(vrt_files)} VRTs from S3.", flush=True)
            except Exception:
                pass
        else:
            for vf in glob.glob(os.path.join(out_dir, "*.vrt")):
                os.remove(vf)
                if verbose:
                    print(f"    Deleted: {os.path.basename(vf)}", flush=True)

    # For S3 output, write VRTs to a local temp dir then sync in one shot.
    _is_s3 = output_path.startswith("s3://")
    _local_vrt_dir = None
    if _is_s3:
        import tempfile as _tempfile

        _local_vrt_dir = _tempfile.mkdtemp(prefix="openseppo_vrts_")

    def write_vrt(path, xml_str):
        data = xml_str.encode("utf-8")
        if _local_vrt_dir:
            local_path = os.path.join(_local_vrt_dir, os.path.basename(path))
            with open(local_path, "wb") as fh:
                fh.write(data)
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)

    def _sync_vrts_to_s3():
        """Sync local VRT temp dir to S3 in one bulk operation."""
        if not _local_vrt_dir or not _is_s3:
            return
        import subprocess

        cmd = ["aws", "s3", "sync", _local_vrt_dir, out_dir + "/", "--exclude", "*", "--include", "*.vrt", "--include", "*.txt", "--no-progress"]
        if verbose:
            print(f"    Syncing VRTs to {out_dir}/...", flush=True)
        subprocess.run(cmd, check=True)
        import shutil

        shutil.rmtree(_local_vrt_dir, ignore_errors=True)

    # --- Collect all TIFs (backscatter + ancillary) and parse metadata ---
    tif_files = _list_all_nisar_tifs(output_path, frequency, mode_str, output_fs)
    # Parse filenames (fast, no I/O)
    parsed = []
    for fpath in tif_files:
        meta = _parse_nisar_tif_meta(fpath)
        if meta is not None:
            parsed.append((fpath, meta))

    # Read TIF geo + tags in parallel (ThreadPool for local I/O, also safe for S3)
    # Read geo+tags from one TIF per (track, direction) group and apply to all.
    # All TIFs in a group share the same CRS/transform/dimensions from the same
    # source H5 + processing parameters.  Avoids N rasterio opens on S3.
    _td_groups = defaultdict(list)
    for fpath, meta in parsed:
        _td_groups[(meta["track"], meta["direction"])].append((fpath, meta))

    _geo_cache = {}  # (track, direction) -> geo dict
    for (trk, dir_), group in _td_groups.items():
        # Read geo from the first backscatter TIF (has tags like RADIOMETRY);
        # fall back to first ancillary if no backscatter in group.
        _sample = next((fp for fp, m in group if not m["is_ancillary"]), group[0][0])
        geo = _read_tif_geo(_sample, output_fs)
        if geo is not None:
            _geo_cache[(trk, dir_)] = geo

    all_metas = []
    for fpath, meta in parsed:
        geo = _geo_cache.get((meta["track"], meta["direction"]))
        if geo is None:
            continue
        # Copy shared geo but keep per-file nodata from the ancillary table
        _file_geo = dict(geo)
        if meta["is_ancillary"]:
            # Map suffix back to HDF5 variable name for _ANCILLARY_GRIDS lookup
            _suffix_to_var = {v[0]: k for k, v in nisar_tools._ANCILLARY_GRIDS.items()}
            _anc_var = _suffix_to_var.get(meta["pol_str"])
            if _anc_var:
                _file_geo["nodata"] = nisar_tools._ancillary_nodata(_anc_var)
                _file_geo["dtype"] = nisar_tools._ancillary_out_dtype(_anc_var)
        meta.update(_file_geo)
        all_metas.append(meta)

    bsc_metas = [m for m in all_metas if not m["is_ancillary"]]
    anc_metas = [m for m in all_metas if m["is_ancillary"]]

    # Check for mixed radiometry -- refuse to build VRTs if inconsistent
    _radiometries = {m["tags"].get("RADIOMETRY") for m in bsc_metas if m.get("tags")}
    _radiometries.discard(None)
    if len(_radiometries) > 1:
        print(f"  WARNING: mixed radiometry found ({_radiometries}). " f"Cannot build VRTs from inconsistent data. " f"Reprocess with a single radiometry setting.", file=sys.stderr)
        _print_vrt_summary(output_path, summary={"backscatter": {}, "ancillary": {}}, vsis3=vsis3)
        return

    # Extract VRT-level metadata from the first backscatter TIF's tags
    _vrt_meta = {}
    if bsc_metas:
        _src_tags = bsc_metas[0].get("tags", {})
        for _k in ("RADIOMETRY", "DB_FORMULA", "OPENSEPPO_VERSION", "CRID", "ISCE3_VERSION"):
            if _k in _src_tags:
                _vrt_meta[_k] = _src_tags[_k]

    if verbose and all_metas:
        print(f"  build_track_vrts: {len(bsc_metas)} backscatter + {len(anc_metas)} ancillary TIFs " f"across {len({m['track'] for m in all_metas})} track(s).")

    # Accumulate results for the summary display
    summary = {"backscatter": {"single_dates": [], "mosaics": [], "ts_by_track": [], "combined_ts": []}, "ancillary": {"single_dates": [], "mosaics": [], "ts_by_track": [], "combined_ts": []}}

    # ======================================================================
    # Process each category (backscatter, then ancillary) through 4 phases
    # ======================================================================
    for category, cat_metas in [("backscatter", bsc_metas), ("ancillary", anc_metas)]:
        if not cat_metas:
            continue

        # Group by pol_str (e.g. "hh", "hv" or "mask", "nlooks", "gamma2sigma")
        by_pol = defaultdict(list)
        for m in cat_metas:
            by_pol[m["pol_str"]].append(m)

        # Phase 2 collector: per-track per-date sources for multi-pol VRTs
        # Key: (track, direction, date) -> [(path, pol_str, mode_str)]
        date_pol_sources = defaultdict(list)

        for pol_str, pol_metas in sorted(by_pol.items()):
            dtype = pol_metas[0]["dtype"]
            _ms = pol_metas[0]["mode_str"]  # None for ancillary

            # Group by (track, direction)
            by_td = defaultdict(list)
            for m in pol_metas:
                by_td[(m["track"], m["direction"])].append(m)

            track_dir_ts = {}

            for (track, direction), td_metas in sorted(by_td.items()):
                _td_crs = td_metas[0]["crs_wkt"]  # per-track CRS
                frames = sorted({m["frame"] for m in td_metas})

                # --- Phase 1: Grid mosaic VRTs ---
                if len(frames) > 1:
                    by_cycle = defaultdict(list)
                    for m in td_metas:
                        by_cycle[m["cycle"]].append(m)

                    mosaic_items = []
                    for cycle, cyc_metas in sorted(by_cycle.items()):
                        frame_items = sorted(cyc_metas, key=lambda x: x["frame"])
                        cyc_frames = sorted({m["frame"] for m in frame_items})

                        if len(cyc_frames) > 1:
                            # Multiple frames in this cycle -- build spatial mosaic
                            _mosaic_meta = _vrt_meta if category == "backscatter" else None
                            mosaic_xml, union_tf, union_w, union_h = _generate_mosaic_vrt_xml(frame_items, _td_crs, dtype, metadata=_mosaic_meta)
                            m0 = frame_items[0]
                            min_fr, max_fr = cyc_frames[0], cyc_frames[-1]
                            min_st = min(m["start_time"] for m in frame_items)
                            max_et = max(m["end_time"] for m in frame_items)
                            ebd = f"-EBD_{frequency}_{pol_str}_{_ms}.vrt" if _ms else f"-EBD_{frequency}_{pol_str}.vrt"
                            mosaic_name = f"NISAR_{m0['il']}_{m0['pt']}_{m0['prod']}_{cycle:03d}_{track:03d}_" f"{direction}_{min_fr:03d}-{max_fr:03d}_{m0['mode']}_{m0['polarization']}_" f"{m0['obs_mode']}_{min_st}_{max_et}_{m0['crid']}_{m0['accuracy']}" f"{ebd}"
                            mosaic_path = f"{out_dir}/{mosaic_name}"
                            write_vrt(mosaic_path, mosaic_xml)
                            summary[category]["mosaics"].append(mosaic_path)
                            if verbose:
                                print(f"      Mosaic: {mosaic_name}")

                            ds = min_st[:8]
                            mi = {"path": mosaic_path, "band_idx": 1, "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", "transform": union_tf, "w": union_w, "h": union_h, "nodata": m0.get("nodata")}
                            mosaic_items.append(mi)
                            if category == "backscatter":
                                date_pol_sources[(track, direction, mi["date"])].append((mosaic_path, pol_str, _ms))
                        else:
                            # Single frame in this cycle -- use TIF directly
                            m = frame_items[0]
                            ds = m["start_time"][:8]
                            ti = {"path": m["path"], "band_idx": 1, "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", "transform": m["transform"], "w": m["w"], "h": m["h"], "nodata": m.get("nodata")}
                            mosaic_items.append(ti)
                            if category == "backscatter":
                                date_pol_sources[(track, direction, ti["date"])].append((m["path"], pol_str, _ms))
                            summary[category]["single_dates"].append(m["path"])

                    track_dir_ts[(track, direction)] = {"ts_items": mosaic_items, "metas": td_metas}

                else:
                    # Single frame -- use TIF directly
                    sorted_metas = sorted(td_metas, key=lambda x: x["start_time"])
                    ts_items = []
                    for m in sorted_metas:
                        ds = m["start_time"][:8]
                        ti = {"path": m["path"], "band_idx": 1, "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", "transform": m["transform"], "w": m["w"], "h": m["h"], "nodata": m.get("nodata")}
                        ts_items.append(ti)
                        # Track TIF for potential multi-pol VRT (backscatter)
                        if category == "backscatter":
                            date_pol_sources[(track, direction, ti["date"])].append((m["path"], pol_str, _ms))
                        # Add raw TIF to single_dates (replaced by VRT in Phase 2 if applicable)
                        summary[category]["single_dates"].append(m["path"])

                    track_dir_ts[(track, direction)] = {"ts_items": ts_items, "metas": td_metas}

            # --- Phase 3: Per-track time-series VRTs ---
            for (track, direction), info in sorted(track_dir_ts.items()):
                ts_items = info["ts_items"]
                td_metas = info["metas"]
                _td_crs = td_metas[0]["crs_wkt"]
                if len(ts_items) > 1:
                    _ts_meta = _vrt_meta if category == "backscatter" else None
                    ts_xml = _make_ts_vrt(ts_items, _td_crs, dtype, metadata=_ts_meta)
                    ts_name = _track_vrt_filename(td_metas, pol_str, _ms)
                    ts_path = f"{out_dir}/{ts_name}"
                    write_vrt(ts_path, ts_xml)
                    summary[category]["ts_by_track"].append(ts_path)
                    if verbose:
                        print(f"    TS VRT: {ts_name}")

            # --- Phase 4: Combined multi-track VRT (only when all tracks share the same CRS) ---
            _all_crs = {info["metas"][0]["crs_wkt"] for info in track_dir_ts.values()}
            if len(track_dir_ts) > 1 and len(_all_crs) == 1:
                _combined_crs = _all_crs.pop()
                combined_items = sorted(
                    [item for info in track_dir_ts.values() for item in info["ts_items"]],
                    key=lambda x: x["date"],
                )
                if len(combined_items) > 1:
                    combined_metas = [m for info in track_dir_ts.values() for m in info["metas"]]
                    _c_meta = _vrt_meta if category == "backscatter" else None
                    combined_xml = _make_ts_vrt(combined_items, _combined_crs, dtype, metadata=_c_meta)
                    combined_name = _track_vrt_filename(combined_metas, pol_str, _ms)
                    combined_path = f"{out_dir}/{combined_name}"
                    write_vrt(combined_path, combined_xml)
                    summary[category]["combined_ts"].append(combined_path)
                    if verbose:
                        print(f"    Combined TS VRT: {combined_name}")

                    tf_combos = sorted(
                        {(m["track"], m["direction"], m["frame"]) for m in combined_metas},
                        key=lambda x: (x[0], x[2], x[1]),
                    )
                    sidecar_content = "\n".join(f"{t:03d}_{d}_{f:03d}" for t, d, f in tf_combos) + "\n"
                    sidecar_path = combined_path[:-4] + "_track_frames.txt"
                    write_vrt(sidecar_path, sidecar_content)

        # --- Phase 2: Single-date multi-pol VRTs (backscatter only, per track) ---
        if category == "backscatter" and date_pol_sources:
            for (_trk, _dir, date), pol_paths in sorted(date_pol_sources.items()):
                # Deduplicate and sort by pol_str
                seen = set()
                unique = []
                for path, pstr, ms in pol_paths:
                    if path not in seen:
                        seen.add(path)
                        unique.append((path, pstr, ms))
                # Only build multi-pol VRT when >1 polarization
                if len(unique) > 1:
                    # Band order: likepol (hh/vv), crosspol (hv/vh), ratio last
                    _pol_order = {"rh": 0, "rv": 1, "rhrv": 2, "rhrvratio": 3}
                    unique.sort(key=lambda x: (_pol_order.get(x[1], 1), x[1]))
                    band_files = [p for p, _, _ in unique]
                    band_names = [ps for _, ps, _ in unique]
                    _tif_ms = unique[0][2]  # mode_str from TIF metadata
                    ref_geo = _geo_cache.get((_trk, _dir))
                    if ref_geo:
                        tf = ref_geo["transform"]
                        w, h = ref_geo["w"], ref_geo["h"]
                        crs_w, dt = ref_geo["crs_wkt"], ref_geo["dtype"]
                        nd = ref_geo["nodata"]
                        pol_list_str = "".join(band_names)
                        ebd = f"-EBD_{frequency}_{pol_list_str}_{_tif_ms}.vrt"
                        src_base = os.path.basename(band_files[0])
                        if "-EBD_" in src_base:
                            vrt_name = src_base.split("-EBD_")[0] + ebd
                        else:
                            vrt_name = src_base.replace(".tif", "") + ebd
                        vrt_path = f"{out_dir}/{vrt_name}"
                        vrt_xml = nisar_tools.generate_vrt_xml_single_step(w, h, tf, crs_w, band_files, band_names, date, dtype=dt, nodata=nd, metadata=_vrt_meta)
                        write_vrt(vrt_path, vrt_xml)
                        # Replace individual pol TIFs/VRTs with the multi-pol VRT
                        covered = set(band_files)
                        summary["backscatter"]["single_dates"] = [p for p in summary["backscatter"]["single_dates"] if p not in covered]
                        summary["backscatter"]["mosaics"] = [p for p in summary["backscatter"]["mosaics"] if p not in covered]
                        summary["backscatter"]["single_dates"].append(vrt_path)

    _sync_vrts_to_s3()
    _print_vrt_summary(output_path, summary, vsis3=vsis3)


def processing(args):
    # 1. Determine Output Auth
    output_profile = args.output_profile if args.output_profile else args.profile
    output_auth = get_auth_dict(output_profile, use_earthdata=False)  # Output unlikely to be Earthdata

    # Check S3 write access early before any processing
    if hasattr(args, 'output') and args.output and args.output.startswith("s3://"):
        from openseppo.nisar.nisar_tools import check_s3_write_access
        check_s3_write_access(args.output, output_auth)

    # 2a. Logic: Show Output Summary Only (Immediate Exit, read-only)
    if args.show_vrts:
        show_output_summary(
            output_path=args.output,
            frequency=args.freq,
            output_auth=output_auth,
            vsis3=args.vsis3,
        )
        return

    # 2b. Logic: Rebuild Only (Immediate Exit)
    if args.rebuild_only:
        print(f"Rebuilding VRTs in {args.output}...")
        build_track_vrts(
            output_path=args.output,
            frequency=args.freq,
            mode_str=None,  # auto-detect from existing TIFs
            verbose=args.verbose,
            output_auth=output_auth,
            reset_vrts=args.reset_vrts,
        )
        return

    # 3.1 Input Handling
    if not args.h5:
        print("Error: --h5 argument is required for processing (or use --rebuild-only).")
        sys.exit(1)

    def _is_url(s):
        return s.startswith("s3://") or s.startswith("https://") or os.path.isfile(s)

    urls = []
    for item in args.h5:
        if os.path.isfile(item) and not item.endswith(".h5"):
            with open(item, "r") as f:
                urls.extend([line.strip() for line in f if _is_url(line.strip())])
        elif item.startswith("s3://") and not item.endswith(".h5"):
            import fsspec

            with fsspec.open(item, "r") as f:
                urls.extend([line.strip() for line in f if _is_url(line.strip())])
        else:
            urls.append(item)

    # 3.2 Determine Input Auth
    if urls[0].startswith("s3://"):
        bucket = urls[0].split("/")[2]
        if bucket in asf_buckets and args.use_earthdata is False:
            print("---> Detected ASF DAAC Bucket. Using earth access credentials.")
            args.use_earthdata = True
    elif urls[0].startswith("https://") and not args.use_earthdata:
        earthdata_hosts = ["earthdatacloud.nasa.gov", "urs.earthdata.nasa.gov", "e4ftl01.cr.usgs.gov"]
        if any(h in urls[0] for h in earthdata_hosts):
            print("---> Detected Earthdata HTTPS URL. Using Earthdata credentials.")
            args.use_earthdata = True

    input_profile = args.input_profile if args.input_profile else args.profile
    input_auth = get_auth_dict(input_profile, args.use_earthdata)

    # 4. Run Batch Processing
    # Auto-enable caching when no spatial subset is given (full-frame reads benefit from local cache)
    is_remote = urls[0].startswith("s3://") or urls[0].startswith("https://")
    if args.cache is None and not args.srcwin and not args.projwin and is_remote:
        args.cache = "y"

    print(f"Starting Batch Processing: {len(urls)} files.")
    print(f"Mode: {args.mode} | Freq: {args.freq} | Downscale: {args.downscale}")

    try:
        result = nisar_tools.process_chunk_task(h5_url=urls, variable_names=args.vars, output_path=args.output, srcwin=tuple(args.srcwin) if args.srcwin else None, projwin=tuple(args.projwin) if args.projwin else None, projwin_srs=args.projwin_srs, transform_mode=args.mode, frequency=args.freq, single_bands=args.single_bands, vrt=(not args.no_vrt), downscale_factor=args.downscale, target_align_pixels=(not args.no_tap), input_auth=input_auth, output_auth=output_auth, time_series_vrt=(not args.no_time_series), list_grids=args.list_grids, verbose=args.verbose, cache=args.cache, keep=args.keep_cached, target_srs=args.target_srs, target_res=args.target_res, resample=args.resample, output_format=args.output_format, fill_holes=args.fill_holes, num_threads=args.warp_threads, read_threads=args.read_threads, dualpol_ratio=args.dualpol_ratio, sigma0=args.sigma0)
        print("\n" + str(result))

        # 5. Build per-track (and combined A+D) time-series VRTs
        if not args.no_time_series and not args.list_grids and args.output:
            print("\nBuilding per-track time series VRTs...")
            build_track_vrts(
                output_path=args.output,
                frequency=args.freq,
                mode_str=args.mode if args.mode else "pwr",
                verbose=args.verbose,
                output_auth=output_auth,
            )

    except Exception as e:
        print(f"\nCRITICAL FAILURE: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


def _main(a):
    args = myargsparse(a)

    start = time.perf_counter()
    processing(args)
    end = time.perf_counter()

    if args.verbose:
        duration = end - start
        minutes = int(duration / 60)
        seconds = duration - minutes * 60
        print(f"\nRuntime: {minutes}m {seconds:.2f}s\n")


def main():
    _main(sys.argv)


if __name__ == "__main__":
    main()
