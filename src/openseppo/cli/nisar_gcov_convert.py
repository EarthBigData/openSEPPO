#!/usr/bin/env python
"""
seppo_nisar_gcov_convert — NISAR GCOV HDF5 to Cloud Optimized GeoTIFF converter
*********************************************************************************
openSEPPO — Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Convert NISAR GCOV HDF5 files to Cloud Optimized GeoTIFF (COG) with optional
reprojection, downscaling, VRT time-series stacking, and dual-pol ratio output.

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
import rasterio
from rasterio.transform import from_origin
import openseppo.nisar.nisar_tools as nisar_tools


# ── seppo_parse_args shim ─────────────────────────────────────────────────────
# Drops the config-file override feature from seppopy.tools.args; all CLI flags
# are preserved and work identically.

def seppo_parse_args(parser, a):
    return parser.parse_args(a[1:])


# ─────────────────────────────────────────────────────────────────────────────

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
    description = "Convert NISAR HDF5 GCOV data to Cloud Optimized GeoTIFF (COG) with optional VRT stacking."

    epilog = ""

    parser = argparse.ArgumentParser(prog=thisProg, description=description, epilog=epilog, formatter_class=CustomFormatter)

    # --- I/O Arguments ---
    parser.add_argument("-i", "--h5", type=str, nargs="+", help="Input H5 URL(s) or path to a text file containing URLs.")
    parser.add_argument("-o", "--output", type=str, required=False, help="Output directory path (S3 or local). Must end in '/' for batch processing.")

    # Defaults set to None to allow auto-detection in nisar_tools
    parser.add_argument("-vars", "--vars", nargs="+", default=None, help="Grid Variables to extract, e.g. HHHH HVHV. If omitted, ALL variables for the frequency are used.")
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
    parser.add_argument("-of", "--output_format", type=str, default="COG",
                        choices=["COG", "GTiff", "h5"],
                        help="Output format: COG (default), GTiff (BigTIFF), h5 (raw HDF5 subset).")

    # --- Other Processing Options ---
    parser.add_argument("-dpratio", "--dualpol_ratio", action="store_true", help="Compute dual-pol power ratio: HHHH/HVHV (DH mode) or VVVV/VHVH (DV mode). Incompatible with QP or single-pol acquisitions.")
    parser.add_argument("-d", "--downscale", type=int, default=None, help="Downscale factor (integer). E.g., 2 for 2x2 block averaging.")

    # --- VRT & Output Structure ---
    parser.add_argument("--no_vrt", action="store_true", help="Disable generation of per-snapshot VRTs.")
    parser.add_argument("--no_time_series", action="store_true", help="Disable generation of Time Series VRT stacks (even if other VRTs are on).")
    parser.add_argument("--no_single_bands", action="store_false", dest="single_bands", help="Save multi-band COG instead of separate files per pol.")

    # --- Subsetting ---
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-srcwin", "--srcwin", nargs=4, type=int, metavar=("XOFF", "YOFF", "XSIZE", "YSIZE"), help="Pixel subset window.")
    group.add_argument("-projwin", "--projwin", nargs=4, type=float, metavar=("ULX", "ULY", "LRX", "LRY"), help="Geographic subset window (map coordinates).")
    parser.add_argument("--no_tap", action="store_true", help="Disable pixel-grid alignment (tap). By default, output origin is snapped to integer multiples of the target pixel size.")
    parser.add_argument("-t_srs", "--target_srs", type=str, default=None, help="Target CRS for output (e.g. EPSG:4326 or bare 4326). If omitted, output stays in native UTM CRS.")
    parser.add_argument("-tr", "--target_res", type=float, nargs=2, metavar=("XRES", "YRES"), default=None, help="Explicit output pixel size in target CRS units (e.g. -tr 0.001 0.001 for ~100m in degrees). Only used with --target_srs.")
    parser.add_argument("--resample", type=str, default="cubic", help="Resampling method for reprojection (nearest/bilinear/cubic/cubicspline/lanczos/average). Default: cubic.")
    parser.add_argument("--fill_holes", action="store_true", help="Fill interior NaN/±inf pixels (those enclosed by valid data) with their nearest valid neighbour. Frame-boundary nodata is unaffected. Prevents the resampling kernel from seeing isolated invalid pixels inside the valid image area.")
    parser.add_argument("--warp_threads", type=int, default=None, metavar="N", help="Number of threads for reprojection. Default: all available CPU cores.")
    parser.add_argument("--read_threads", type=int, default=8, metavar="N", help="Number of parallel S3/HTTPS connections for reading HDF5 chunks. Each band×stripe gets its own connection. Default: 8.")

    # --- Authentication (Distinct Input/Output) ---
    parser.add_argument("--profile", type=str, help="AWS Profile name (applies to both Input and Output unless overridden).")
    parser.add_argument("--input_profile", type=str, help="AWS Profile specifically for reading Input H5s.")
    parser.add_argument("--output_profile", type=str, help="AWS Profile specifically for writing Output COGs.")
    # --- Management Flags ---
    parser.add_argument("-ro", "--rebuild_only", action="store_true", help="Skip processing and ONLY rebuild VRTs in the output folder.")
    parser.add_argument("-R", "--rebuild_all_vrts", action="store_true", help="After processing the new files, scan the output folder and (re)build the master VRTs to include ALL timesteps (old + new).")
    parser.add_argument("-S", "--show_vrts", action="store_true", help="Print a summary of all VRTs in the output folder grouped by type (requires -o). No processing is performed.")
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


def _parse_nisar_tif_meta(tif_path):
    """Parse NISAR metadata tokens from a COG TIF filename. Returns dict or None."""
    basename = os.path.basename(tif_path)
    if "-EBD_" not in basename:
        return None
    nisar_base, ebd_raw = basename.split("-EBD_", 1)
    ebd_raw = ebd_raw.removesuffix(".tif")  # e.g. "A_hh_AMP"
    ebd_tokens = ebd_raw.split("_")  # ["A", "hh", "AMP"]
    if len(ebd_tokens) < 3:
        return None
    tokens = nisar_base.split("_")
    if len(tokens) < 18 or tokens[0] != "NISAR":
        return None
    try:
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
            "pol_str": ebd_tokens[1],
            "mode_str": ebd_tokens[2],
            "path": tif_path,
            "nisar_base": nisar_base,
        }
    except (IndexError, ValueError):
        return None


def _list_nisar_tifs(output_path, frequency, mode_str, output_fs=None):
    """List NISAR COG TIF files matching frequency and mode in output_path."""
    tag = f"-EBD_{frequency}_"
    suffix = f"_{mode_str}.tif"
    if output_fs:
        bucket_path = output_path.replace("s3://", "")
        try:
            files = output_fs.ls(bucket_path)
            return [f"s3://{f}" for f in files if f.endswith(".tif") and tag in f and suffix in f]
        except Exception:
            return []
    return sorted(glob.glob(os.path.join(output_path, f"*{tag}*{suffix}")))


def _read_tif_geo(tif_path, output_fs=None):
    """Return (transform, w, h, crs_wkt, dtype) from a TIF file, or None on error."""
    try:
        if output_fs:
            with output_fs.open(tif_path, "rb") as fobj:
                with rasterio.open(fobj) as ds:
                    return ds.transform, ds.width, ds.height, ds.crs.to_wkt(), ds.dtypes[0]
        with rasterio.open(tif_path) as ds:
            return ds.transform, ds.width, ds.height, ds.crs.to_wkt(), ds.dtypes[0]
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


def _gdal_nodata(dtype):
    d = str(dtype).lower()
    return "0" if ("int" in d or "byte" in d) else "nan"


def _generate_mosaic_vrt_xml(frame_items, crs_wkt, dtype):
    """
    Spatial mosaic VRT: all frame_items (different extents, same date) are merged
    into a single band. Returns (xml_str, union_transform, union_w, union_h).

    frame_items: [{"path", "transform", "w", "h"}]
    """
    vrt_dtype = nisar_tools.get_gdal_dtype(dtype)
    nodata_val = _gdal_nodata(dtype)
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
        f'  <VRTRasterBand dataType="{vrt_dtype}" band="1">',
        f"    <NoDataValue>{nodata_val}</NoDataValue>",
        f"    <ColorInterp>Gray</ColorInterp>",
    ]
    for it in frame_items:
        dx = int(round((it["transform"].c - union_ulx) / res_x))
        dy = int(round((union_uly - it["transform"].f) / res_y))
        src_path, rel_attr = _vrt_src_entry(it["path"])
        lines.append(
            f"    <ComplexSource>\n"
            f'      <SourceFilename relativeToVRT="{rel_attr}">{src_path}</SourceFilename>\n'
            f"      <SourceBand>1</SourceBand>\n"
            f'      <SourceProperties RasterXSize="{it["w"]}" RasterYSize="{it["h"]}" DataType="{vrt_dtype}" BlockXSize="512" BlockYSize="512" />\n'
            f'      <SrcRect xOff="0" yOff="0" xSize="{it["w"]}" ySize="{it["h"]}" />\n'
            f'      <DstRect xOff="{dx}" yOff="{dy}" xSize="{it["w"]}" ySize="{it["h"]}" />\n'
            f"      <NODATA>{nodata_val}</NODATA>\n"
            f"    </ComplexSource>"
        )
    lines += [
        "  </VRTRasterBand>",
        '  <OverviewList resampling="nearest">2 4 8</OverviewList>',
        "</VRTDataset>",
    ]
    return "\n".join(lines), union_tf, union_w, union_h


def _generate_ts_union_vrt_xml(crs_wkt, stack_items, dtype):
    """
    Time-series VRT with union spatial extent (one band per timestep).
    Used when items have different spatial extents (e.g. A vs D tracks).

    stack_items: [{"path", "band_idx", "date", "transform", "w", "h"}]
    """
    vrt_dtype = nisar_tools.get_gdal_dtype(dtype)
    nodata_val = _gdal_nodata(dtype)
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
    For ≤4 combos the track, direction, and frame tokens are each hyphen-joined in
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

    return (f"NISAR_{il}_{pt}_{prod}_{cycle_str}_{track_str}_{dir_str}_"
            f"{frame_str}_{mode}_{pol}_{obs_mode}_{min_start}_{max_end}"
            f"-EBD_{freq}_{pol_str}_{mode_str}.vrt")


def _make_ts_vrt(ts_items, crs_wkt, dtype):
    """Choose between same-extent and union time-series VRT builder."""
    same_geo = all(it["w"] == ts_items[0]["w"] and it["h"] == ts_items[0]["h"] and it["transform"] == ts_items[0]["transform"] for it in ts_items)
    if same_geo:
        return nisar_tools.generate_vrt_xml_timeseries(ts_items[0]["w"], ts_items[0]["h"], ts_items[0]["transform"], crs_wkt, ts_items, dtype=dtype)
    return _generate_ts_union_vrt_xml(crs_wkt, ts_items, dtype)


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


def _print_vrt_summary(output_path, snapshot_vrts, per_track_vrts, combined_vrts):
    """Print a formatted VRT summary grouped for copy-paste into a GIS application."""
    is_s3 = output_path.startswith("s3://")
    if is_s3:
        bucket = output_path.replace("s3://", "").split("/")[0]
        print(f"\nBucket: {bucket}")

        def key(p):
            return p.replace("s3://", "").split("/", 1)[1]
    else:
        print(f"\nPath: {output_path.rstrip('/')}")

        def key(p):
            return p

    if snapshot_vrts:
        print()
        print("Single dates:")
        for p in sorted(snapshot_vrts):
            print(key(p))

    if per_track_vrts:
        print()
        print("Time series by track:")
        for p in sorted(per_track_vrts):
            print(key(p))

    if combined_vrts:
        print()
        print("Combined time series:")
        for p in sorted(combined_vrts):
            print(key(p))


def build_track_vrts(output_path, frequency, mode_str, verbose=False, output_auth=None):
    """
    Post-process: build per-track (and combined) time-series VRTs, then print
    a formatted summary of all VRTs grouped by type for GIS copy-paste.

    Rules:
      - Multiple frames for the same track+direction:
          build a spatial mosaic VRT per cycle, then a time-series VRT over those.
      - Single frame per track+direction:
          build a per-track time-series VRT directly from individual TIF files.
      - More than one (track, direction) group:
          additionally build a combined multi-track time-series VRT with a
          _track_frames.txt sidecar listing all track/direction/frame combos.
    """
    import s3fs as _s3fs

    out_dir = output_path.rstrip("/")

    # Set up filesystem
    output_fs = None
    if output_path.startswith("s3://"):
        auth = output_auth or {}
        if "profile" in auth:
            output_fs = _s3fs.S3FileSystem(profile=auth["profile"])
        elif "key" in auth:
            output_fs = _s3fs.S3FileSystem(key=auth["key"], secret=auth["secret"], token=auth.get("token"))
        else:
            output_fs = _s3fs.S3FileSystem(anon=False)

    def write_vrt(path, xml_str):
        data = xml_str.encode("utf-8")
        if output_fs:
            with output_fs.open(path, "wb") as fh:
                fh.write(data)
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)

    # 1. Collect TIF files and parse metadata + geometry
    tif_files = _list_nisar_tifs(output_path, frequency, mode_str, output_fs)
    if not tif_files:
        if verbose:
            print("  build_track_vrts: no matching TIF files found.")
        return

    all_metas = []
    for fpath in tif_files:
        meta = _parse_nisar_tif_meta(fpath)
        if meta is None:
            continue
        geo = _read_tif_geo(fpath, output_fs)
        if geo is None:
            continue
        tf, w, h, crs_wkt, dtype = geo
        meta.update({"transform": tf, "w": w, "h": h, "crs_wkt": crs_wkt, "dtype": dtype})
        all_metas.append(meta)

    if not all_metas:
        if verbose:
            print("  build_track_vrts: could not read metadata from any TIF.")
        return

    if verbose:
        print(f"  build_track_vrts: {len(all_metas)} TIF files across "
              f"{len({m['track'] for m in all_metas})} track(s).")

    # Use CRS/dtype from first file as reference
    ref_crs = all_metas[0]["crs_wkt"]
    ref_dtype = all_metas[0]["dtype"]

    per_track_vrts = []
    combined_vrts = []

    # 2. Iterate by polarization (handles multi-pol batches independently)
    by_pol = defaultdict(list)
    for m in all_metas:
        by_pol[m["pol_str"]].append(m)

    for pol_str, pol_metas in sorted(by_pol.items()):
        crs_wkt = pol_metas[0]["crs_wkt"]
        dtype = pol_metas[0]["dtype"]

        # 3. Group by (track, direction)
        by_td = defaultdict(list)
        for m in pol_metas:
            by_td[(m["track"], m["direction"])].append(m)

        # Accumulate per-(track,direction) time-series items
        # {(track, direction): {"ts_items": [...], "metas": [...]}}
        track_dir_ts = {}

        for (track, direction), td_metas in sorted(by_td.items()):
            frames_in_group = sorted({m["frame"] for m in td_metas})

            if len(frames_in_group) > 1:
                # ── Multiple frames: mosaic per cycle, then time-series over mosaics ──
                if verbose:
                    print(f"    Track {track:03d}/{direction}: "
                          f"{len(frames_in_group)} frames → mosaic VRTs")

                by_cycle = defaultdict(list)
                for m in td_metas:
                    by_cycle[m["cycle"]].append(m)

                mosaic_items = []
                for cycle, cyc_metas in sorted(by_cycle.items()):
                    frame_items = sorted(cyc_metas, key=lambda x: x["frame"])
                    mosaic_xml, union_tf, union_w, union_h = _generate_mosaic_vrt_xml(frame_items, crs_wkt, dtype)
                    m0 = frame_items[0]
                    min_fr = min(m["frame"] for m in frame_items)
                    max_fr = max(m["frame"] for m in frame_items)
                    min_st = min(m["start_time"] for m in frame_items)
                    max_et = max(m["end_time"] for m in frame_items)
                    mosaic_name = (f"NISAR_{m0['il']}_{m0['pt']}_{m0['prod']}_{cycle:03d}_{track:03d}_"
                                   f"{direction}_{min_fr:03d}-{max_fr:03d}_{m0['mode']}_{m0['polarization']}_"
                                   f"{m0['obs_mode']}_{min_st}_{max_et}_{m0['accuracy']}_{m0['crid']}"
                                   f"-EBD_{frequency}_{pol_str}_{mode_str}_mosaic.vrt")
                    mosaic_path = f"{out_dir}/{mosaic_name}"
                    write_vrt(mosaic_path, mosaic_xml)
                    if verbose:
                        print(f"      Mosaic VRT: {mosaic_name}")

                    ds = min_st[:8]
                    mosaic_items.append(
                        {
                            "path": mosaic_path,
                            "band_idx": 1,
                            "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}",
                            "transform": union_tf,
                            "w": union_w,
                            "h": union_h,
                        }
                    )

                ts_xml = _make_ts_vrt(mosaic_items, crs_wkt, dtype)
                ts_name = _track_vrt_filename(td_metas, pol_str, mode_str)
                ts_path = f"{out_dir}/{ts_name}"
                write_vrt(ts_path, ts_xml)
                if verbose:
                    print(f"    TS VRT (track {track:03d}/{direction}): {ts_name}")
                per_track_vrts.append(ts_path)
                track_dir_ts[(track, direction)] = {"ts_items": mosaic_items, "metas": td_metas}

            else:
                # ── Single frame: build time-series directly from TIF files ──
                sorted_metas = sorted(td_metas, key=lambda x: x["start_time"])
                ts_items = []
                for m in sorted_metas:
                    ds = m["start_time"][:8]
                    ts_items.append(
                        {
                            "path": m["path"],
                            "band_idx": 1,
                            "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}",
                            "transform": m["transform"],
                            "w": m["w"],
                            "h": m["h"],
                        }
                    )

                ts_xml = _make_ts_vrt(ts_items, crs_wkt, dtype)
                ts_name = _track_vrt_filename(td_metas, pol_str, mode_str)
                ts_path = f"{out_dir}/{ts_name}"
                write_vrt(ts_path, ts_xml)
                if verbose:
                    print(f"    TS VRT (track {track:03d}/{direction}): {ts_name}")
                per_track_vrts.append(ts_path)
                track_dir_ts[(track, direction)] = {"ts_items": ts_items, "metas": td_metas}

        # 4. Combined multi-track VRT: build when more than one (track, direction) group
        if len(track_dir_ts) > 1:
            combined_items = sorted(
                [item for info in track_dir_ts.values() for item in info["ts_items"]],
                key=lambda x: x["date"],
            )
            combined_metas = [m for info in track_dir_ts.values() for m in info["metas"]]
            combined_xml = _make_ts_vrt(combined_items, crs_wkt, dtype)
            combined_name = _track_vrt_filename(combined_metas, pol_str, mode_str)
            combined_path = f"{out_dir}/{combined_name}"
            write_vrt(combined_path, combined_xml)
            if verbose:
                print(f"    Combined TS VRT: {combined_name}")
            combined_vrts.append(combined_path)

            # Sidecar: list all (track, direction, frame) combos sorted by (track, frame, dir)
            tf_combos = sorted(
                {(m["track"], m["direction"], m["frame"]) for m in combined_metas},
                key=lambda x: (x[0], x[2], x[1]),
            )
            sidecar_content = "\n".join(f"{t:03d}_{d}_{f:03d}" for t, d, f in tf_combos) + "\n"
            sidecar_path = combined_path[:-4] + "_track_frames.txt"
            write_vrt(sidecar_path, sidecar_content)
            if verbose:
                print(f"    Sidecar: {os.path.basename(sidecar_path)}")

    # 5. Collect snapshot VRTs: all .vrt files in the directory that are not
    #    per-track, combined, or internal mosaic VRTs.
    #    Snapshot VRTs use a multi-pol suffix (e.g. "hhhv", 4+ chars).
    #    Any time-series VRT (old or new style) uses a single-pol suffix (2 chars).
    import re as _re
    _snap_pol_re = _re.compile(r"-EBD_[A-Za-z]_([a-zA-Z]{3,})_[^_]+\.vrt$")
    ts_and_combined = set(per_track_vrts + combined_vrts)
    all_dir_vrts = _list_vrts_in_dir(out_dir, output_fs)
    snapshot_vrts = [
        p for p in all_dir_vrts
        if p not in ts_and_combined
        and not p.endswith("_mosaic.vrt")
        and _snap_pol_re.search(os.path.basename(p))
    ]

    _print_vrt_summary(output_path, snapshot_vrts, per_track_vrts, combined_vrts)


def processing(args):
    # 1. Determine Output Auth
    output_profile = args.output_profile if args.output_profile else args.profile
    output_auth = get_auth_dict(output_profile, use_earthdata=False)  # Output unlikely to be Earthdata

    # 2a. Logic: Show VRTs Only (Immediate Exit)
    if args.show_vrts:
        build_track_vrts(
            output_path=args.output,
            frequency=args.freq,
            mode_str=args.mode if args.mode else "pwr",
            verbose=False,
            output_auth=output_auth,
        )
        return

    # 2b. Logic: Rebuild Only (Immediate Exit)
    if args.rebuild_only:
        print(f"Rebuilding VRTs in {args.output}...")
        # Rebuild per-date snapshot VRTs only (time-series handled by build_track_vrts below)
        res = nisar_tools.rebuild_vrts(output_path=args.output, variable_names=args.vars, transform_mode=args.mode, frequency=args.freq, auth_config=output_auth, verbose=args.verbose, build_ts=False)
        print(res)
        print("\nBuilding per-track time series VRTs...")
        build_track_vrts(
            output_path=args.output,
            frequency=args.freq,
            mode_str=args.mode if args.mode else "pwr",
            verbose=args.verbose,
            output_auth=output_auth,
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
        result = nisar_tools.process_chunk_task(h5_url=urls, variable_names=args.vars, output_path=args.output, srcwin=tuple(args.srcwin) if args.srcwin else None, projwin=tuple(args.projwin) if args.projwin else None, transform_mode=args.mode, frequency=args.freq, single_bands=args.single_bands, vrt=(not args.no_vrt), downscale_factor=args.downscale, target_align_pixels=(not args.no_tap), input_auth=input_auth, output_auth=output_auth, time_series_vrt=(not args.no_time_series), list_grids=args.list_grids, verbose=args.verbose, cache=args.cache, keep=args.keep_cached, target_srs=args.target_srs, target_res=args.target_res, resample=args.resample, output_format=args.output_format, fill_holes=args.fill_holes, num_threads=args.warp_threads, read_threads=args.read_threads, dualpol_ratio=args.dualpol_ratio)
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

        # 6. Conditional Rebuild (Post-Processing)
        if args.rebuild_all_vrts:
            print(f"\n[Triggered] Rebuilding snapshot VRTs in {args.output} to include new timesteps...")
            rebuild_res = nisar_tools.rebuild_vrts(output_path=args.output, variable_names=args.vars, transform_mode=args.mode, frequency=args.freq, auth_config=output_auth, verbose=args.verbose, build_ts=False)
            print(rebuild_res)

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
