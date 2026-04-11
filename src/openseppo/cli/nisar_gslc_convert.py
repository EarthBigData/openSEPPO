#!/usr/bin/env python
"""
seppo_nisar_gslc_convert -- NISAR GSLC HDF5 to Cloud Optimized GeoTIFF converter
***********************************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Convert NISAR GSLC (Geocoded Single-Look Complex) HDF5 files to Cloud Optimized
GeoTIFF (COG) or GTiff.  Output modes:

  * Power intensity  (|z|^2, float32)                     -- default (-pwr)
  * Scaled amplitude (uint16, GCOV-compatible)                        (-amp)
  * Raw magnitude    (|z|, float32)                                   (-mag)
  * Wrapped phase    (angle(z), float32, radians, range -pi ... pi)   (-phase)
  * Complex SLC      (complex64, tiled GTiff, no overviews)           (-cslc)

Spatial subsetting (srcwin / projwin), reprojection, downscaling, interior hole
filling, and VRT time-series stacking are all supported.

Usage Examples:
1. List available grids/variables in a GSLC file:
    seppo_nisar_gslc_convert -i file.h5 -lg

2. Convert to power COG (default):
    seppo_nisar_gslc_convert -i file.h5 -o out/

3. Convert to scaled amplitude COG (uint16, GCOV-compatible):
    seppo_nisar_gslc_convert -i file.h5 -o out/ -amp

4. Convert to raw float32 magnitude:
    seppo_nisar_gslc_convert -i file.h5 -o out/ -mag

5. Extract wrapped phase:
    seppo_nisar_gslc_convert -i file.h5 -o out/ -phase

6. Extract raw complex SLC (for interferometry/coherence):
    seppo_nisar_gslc_convert -i file.h5 -o out/ -cslc

7. Subset by geographic window:
    seppo_nisar_gslc_convert -i file.h5 -o out/ -projwin 300000 4200000 400000 4100000

8. Subset by pixel window:
    seppo_nisar_gslc_convert -i file.h5 -o out/ -srcwin 100 200 512 512

9. Reproject to WGS84:
    seppo_nisar_gslc_convert -i file.h5 -o out/ -t_srs 4326

10. Extract only HH polarisation:
    seppo_nisar_gslc_convert -i file.h5 -o out/ -vars HH

11. Write raw complex H5 subset:
    seppo_nisar_gslc_convert -i file.h5 -o out/ -of h5 -projwin 300000 4200000 400000 4100000

12. Rebuild VRTs from existing COGs:
    seppo_nisar_gslc_convert -ro -o out/ -pwr
"""

import sys
import os
import re
import time
import glob
import argparse
import shlex
import math
from pprint import pprint
from collections import defaultdict

import rasterio
from rasterio.transform import from_origin

import openseppo.nisar.nisar_tools_gslc as nisar_tools_gslc
import openseppo.nisar.nisar_tools as nisar_tools  # for shared VRT helpers


# -- seppo_parse_args shim -------------------------------------------------------

def seppo_parse_args(parser, a):
    return parser.parse_args(a[1:])


# -------------------------------------------------------------------------------

asf_buckets = ["sds-n-cumulus-prod-nisar-products", "sds-n-cumulus-prod-nisar-ur-products"]


def myargsparse(a):
    """Parse command-line arguments for the NISAR GSLC converter."""

    class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter,
                          argparse.RawDescriptionHelpFormatter):
        pass

    if isinstance(a, str):
        a = shlex.split(a)

    thisProg = os.path.basename(a[0])
    description = ("Convert NISAR GSLC HDF5 complex data to Cloud Optimized GeoTIFF (COG). "
                   "Supports power, amplitude, and wrapped-phase output.")

    parser = argparse.ArgumentParser(
        prog=thisProg, description=description,
        formatter_class=CustomFormatter,
    )

    # --- I/O ---
    parser.add_argument(
        "-i", "--h5", type=str, nargs="+",
        help="Input GSLC H5 URL(s) or path to a text file containing URLs.",
    )
    parser.add_argument(
        "-o", "--output", type=str, required=False,
        help="Output directory (S3 or local). Must end in '/' for batch processing.",
    )

    # --- Variables & Frequency ---
    parser.add_argument(
        "-vars", "--vars", nargs="+", default=None,
        help="Polarisation variables to extract, e.g. HH HV.  "
             "If omitted, ALL 2-letter upper-case variables for the frequency are used.",
    )
    parser.add_argument(
        "-f", "--freq", type=str, default="A", choices=["A", "B"],
        help="Frequency band (A/B). Default: A.",
    )

    # --- List Grids ---
    parser.add_argument(
        "-lg", "--list_grids", action="store_true",
        help="Scan the first H5 file and list all available grids/frequencies/variables, "
             "then exit.  Requires -i.",
    )
    parser.add_argument(
        "-lv", "--list_vars", action="store_true",
        help="Print a flat list of all paths in the first H5 file, then exit.  Requires -i.",
    )

    # --- Output mode (mutually exclusive) ---
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-pwr", "--power", action="store_const", dest="mode", const="pwr",
        help="Output power intensity |z|^2 (float32).  Default.",
    )
    mode_group.add_argument(
        "-amp", "--amp", action="store_const", dest="mode", const="AMP",
        help="Output scaled amplitude (uint16, identical formula to GCOV -amp): "
             "sqrt(|z|^2 * 10^8.3), range 1-65535, nodata=0.  "
             "Directly comparable to GCOV amplitude output.",
    )
    mode_group.add_argument(
        "-mag", "--magnitude", action="store_const", dest="mode", const="mag",
        help="Output raw magnitude |z| (float32, nodata=NaN).  "
             "Use this for linear-scale magnitude without integer scaling.",
    )
    mode_group.add_argument(
        "-phase", "--phase", action="store_const", dest="mode", const="phase",
        help="Output wrapped phase angle(z) in radians (float32, range -pi ... pi).  "
             "Reprojection uses nearest-neighbour automatically.",
    )
    mode_group.add_argument(
        "-cslc", "--cslc", action="store_const", dest="mode", const="cslc",
        help="Output raw complex SLC (complex64 / CFloat32, tiled GeoTIFF, deflate, "
             "predictor=1, no overviews).  Preserves full magnitude and phase for "
             "interferometry and coherence processing.  Nodata convention: 0+0j pixels "
             "are invalid.  Reprojection uses nearest-neighbour automatically.",
    )

    # --- Output format ---
    parser.add_argument(
        "-of", "--output_format", type=str, default="COG",
        choices=["COG", "GTiff", "h5"],
        help="Output format: COG (default), GTiff (BigTIFF), h5 (raw complex HDF5 subset).",
    )

    # --- Downscaling ---
    parser.add_argument(
        "-d", "--downscale", type=int, nargs="+", default=None,
        metavar="N",
        help="Downscale factor: one integer for isotropic (e.g. -d 2) or two integers "
             "for anisotropic X Y (e.g. -d 2 4).  Block average for pwr/amp/mag; "
             "nearest decimation for phase/cslc.",
    )
    parser.add_argument(
        "--square", action="store_true",
        help="Auto-downscale to square pixels by averaging to the coarser native spacing. "
             "Ignored if -d is also supplied.",
    )

    # --- VRT & output structure ---
    parser.add_argument(
        "--no_vrt", action="store_true",
        help="Disable generation of per-snapshot VRTs.",
    )
    parser.add_argument(
        "--no_time_series", action="store_true",
        help="Disable generation of time-series VRT stacks.",
    )
    parser.add_argument(
        "--no_single_bands", action="store_false", dest="single_bands",
        help="Save multi-band COG instead of separate files per polarisation.",
    )

    # --- Spatial subsetting (mutually exclusive) ---
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-srcwin", "--srcwin", nargs=4, type=int,
        metavar=("XOFF", "YOFF", "XSIZE", "YSIZE"),
        help="Pixel subset window.",
    )
    group.add_argument(
        "-projwin", "--projwin", nargs=4, type=float,
        metavar=("ULX", "ULY", "LRX", "LRY"),
        help="Geographic subset window.  Coordinates are in the native or target CRS "
             "unless -projwin_srs is given.",
    )
    parser.add_argument(
        "-projwin_srs", "--projwin_srs", type=str,
        help="CRS of the -projwin coordinates (e.g. EPSG:4326).  If omitted, "
             "-projwin is assumed to be in the native or target raster CRS.",
    )

    # --- Reprojection ---
    parser.add_argument(
        "--no_tap", action="store_true",
        help="Disable pixel-grid alignment (tap).",
    )
    parser.add_argument(
        "-t_srs", "--target_srs", type=str, default=None,
        help="Target CRS (e.g. EPSG:4326 or bare 4326). "
             "If omitted, output stays in native UTM CRS.",
    )
    parser.add_argument(
        "-tr", "--target_res", type=float, nargs=2,
        metavar=("XRES", "YRES"), default=None,
        help="Explicit output pixel size in target CRS units. Only used with --target_srs.",
    )
    parser.add_argument(
        "--resample", type=str, default="nearest",
        help="Resampling method for reprojection "
             "(nearest/bilinear/cubic/cubicspline/lanczos/average). "
             "Default: nearest (phase-safe; bilinear or cubic for pwr/amp).",
    )
    parser.add_argument(
        "--fill_holes", action="store_true",
        help="Fill interior NaN pixels with nearest valid neighbour (pwr/amp only). "
             "Not applied for phase output.",
    )
    parser.add_argument(
        "--warp_threads", type=int, default=None, metavar="N",
        help="Number of threads for reprojection. Default: all available CPU cores.",
    )
    parser.add_argument(
        "--read_threads", type=int, default=8, metavar="N",
        help="(Reserved) S3/HTTPS connections for reading. Default: 8.",
    )

    # --- Authentication ---
    parser.add_argument(
        "--profile", type=str,
        help="AWS profile name (applies to both input and output unless overridden).",
    )
    parser.add_argument("--input_profile",  type=str,
                        help="AWS profile for reading input H5s.")
    parser.add_argument("--output_profile", type=str,
                        help="AWS profile for writing output COGs.")

    # --- Management ---
    parser.add_argument(
        "-ro", "--rebuild_only", action="store_true",
        help="Skip processing; rebuild VRTs from existing TIFs in -o.",
    )
    parser.add_argument(
        "-S", "--show_vrts", action="store_true",
        help="Print a summary of all VRTs/TIFs in -o (read-only).",
    )
    parser.add_argument(
        "-vsis3", "--vsis3", action="store_true",
        help="With -S: print paths as /vsis3/ URIs for QGIS/GDAL.",
    )
    parser.add_argument(
        "--reset_vrts", action="store_true",
        help="Delete all existing VRTs in -o before rebuilding (use with -ro).",
    )
    parser.add_argument(
        "-cache", "--cache", default=None,
        help="Local directory (or 'y') to cache remote H5 files before processing.",
    )
    parser.add_argument(
        "-keep", "--keep_cached", action="store_true",
        help="Retain cached H5 files after processing (use with -cache).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose output.",
    )

    parser.set_defaults(single_bands=True)
    parser.set_defaults(mode="pwr")  # default to power

    args = seppo_parse_args(parser, a)
    args.use_earthdata = False  # auto-detected later

    # Normalise -d to a single int or (x, y) tuple stored in args.downscale
    if args.downscale is not None:
        if len(args.downscale) == 1:
            args.downscale = args.downscale[0]
        elif len(args.downscale) == 2:
            args.downscale = tuple(args.downscale)
        else:
            parser.error("-d/--downscale accepts 1 integer (isotropic) or 2 integers (X Y).")

    if args.verbose:
        pprint(vars(args))

    if not args.list_grids and not args.list_vars and not args.output:
        parser.error("the following arguments are required: --output/-o "
                     "(unless --list_grids or --list_vars is used)")

    if not args.rebuild_only and not args.show_vrts and not args.h5:
        parser.error("the following arguments are required: --h5/-i "
                     "(unless --rebuild_only or --show_vrts is used)")

    return args


def get_auth_dict(profile_arg, use_earthdata=False):
    """Construct auth dict from profile, earthdata flag, or env vars."""
    auth = {}
    if use_earthdata:
        auth["use_earthdata"] = True
        return auth
    if profile_arg:
        auth["profile"] = profile_arg
        return auth
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        auth["key"]    = os.environ.get("AWS_ACCESS_KEY_ID")
        auth["secret"] = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if os.environ.get("AWS_SESSION_TOKEN"):
            auth["token"] = os.environ.get("AWS_SESSION_TOKEN")
    return auth


# =========================================================
# VRT BUILDING HELPERS (GSLC-specific mode strings)
# =========================================================

# GSLC output mode suffixes (no dB/DN for GSLC)
_KNOWN_BSC_MODES  = ("pwr", "AMP", "mag", "phase", "cslc")
# GSLC has no ancillary grids in the same sense as GCOV
_KNOWN_ANC_SUFFIXES: set = set()


def _parse_nisar_tif_meta(tif_path):
    """Parse NISAR metadata from a GSLC TIF filename. Returns dict or None."""
    basename = os.path.basename(tif_path)
    if "-EBD_" not in basename or not basename.endswith(".tif"):
        return None
    nisar_base, ebd_raw = basename.split("-EBD_", 1)
    ebd_raw = ebd_raw.removesuffix(".tif")
    ebd_tokens = ebd_raw.split("_")   # ["A", "hh", "pwr"] or ["A", "hh", "phase"]
    if len(ebd_tokens) < 2:
        return None
    tokens = nisar_base.split("_")
    if len(tokens) < 18 or tokens[0] != "NISAR":
        return None
    try:
        return {
            "il":         tokens[1],
            "pt":         tokens[2],
            "prod":       tokens[3],
            "cycle":      int(tokens[4]),
            "track":      int(tokens[5]),
            "direction":  tokens[6],
            "frame":      int(tokens[7]),
            "mode":       tokens[8],
            "polarization": tokens[9],
            "obs_mode":   tokens[10],
            "start_time": tokens[11],
            "end_time":   tokens[12],
            "crid":       tokens[13],
            "accuracy":   tokens[14],
            "freq":       ebd_tokens[0],
            "pol_str":    ebd_tokens[1] if len(ebd_tokens) >= 2 else "",
            "mode_str":   ebd_tokens[2] if len(ebd_tokens) >= 3 else None,
            "is_ancillary": False,
            "path":       tif_path,
            "nisar_base": nisar_base,
        }
    except (IndexError, ValueError):
        return None


def _list_all_nisar_tifs(output_path, frequency, mode_str=None, output_fs=None):
    """List all GSLC TIF files in output_path matching the given frequency and mode."""
    tag = f"-EBD_{frequency}_"
    bsc_suffixes = ([f"_{mode_str}.tif"] if mode_str
                    else [f"_{m}.tif" for m in _KNOWN_BSC_MODES])

    def _matches(f):
        return f.endswith(".tif") and tag in f and any(f.endswith(s) for s in bsc_suffixes)

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
    return sorted(set(results))


def _read_tif_geo(tif_path, output_fs=None):
    """Return geo info and GDAL tags from a TIF, or None on error."""
    try:
        if output_fs:
            with output_fs.open(tif_path, "rb") as fobj:
                with rasterio.open(fobj) as ds:
                    return {"transform": ds.transform, "w": ds.width, "h": ds.height,
                            "crs_wkt": ds.crs.to_wkt(), "dtype": ds.dtypes[0],
                            "nodata": ds.nodata, "tags": ds.tags()}
        with rasterio.open(tif_path) as ds:
            return {"transform": ds.transform, "w": ds.width, "h": ds.height,
                    "crs_wkt": ds.crs.to_wkt(), "dtype": ds.dtypes[0],
                    "nodata": ds.nodata, "tags": ds.tags()}
    except Exception:
        return None


def _vrt_src_entry(path):
    """Return (vrt_src_path, relativeToVRT_attr) for use in VRT XML."""
    if path.startswith("s3://"):
        return "/vsis3/" + path[5:], "0"
    return os.path.basename(path), "1"


def _gdal_nodata_str(nodata, dtype):
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
    """Spatial mosaic VRT for same-date multi-frame scenes."""
    vrt_dtype  = nisar_tools.get_gdal_dtype(dtype)
    nodata_val = _gdal_nodata_str(frame_items[0].get("nodata"), dtype)
    res_x = abs(frame_items[0]["transform"].a)
    res_y = abs(frame_items[0]["transform"].e)

    union_ulx = min(it["transform"].c for it in frame_items)
    union_uly = max(it["transform"].f for it in frame_items)
    union_lrx = max(it["transform"].c + it["w"] * res_x for it in frame_items)
    union_lry = min(it["transform"].f - it["h"] * res_y for it in frame_items)
    union_w   = max(1, int(round((union_lrx - union_ulx) / res_x)))
    union_h   = max(1, int(round((union_uly - union_lry) / res_y)))
    union_tf  = from_origin(union_ulx, union_uly, res_x, res_y)

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
        lines.append(
            f"    <ComplexSource>\n"
            f'      <SourceFilename relativeToVRT="{rel_attr}">{src_path}</SourceFilename>\n'
            f"      <SourceBand>1</SourceBand>\n"
            f'      <SourceProperties RasterXSize="{it["w"]}" RasterYSize="{it["h"]}" '
            f'DataType="{vrt_dtype}" BlockXSize="512" BlockYSize="512" />\n'
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


def _generate_ts_union_vrt_xml(crs_wkt, stack_items, dtype, nodata=None, metadata=None):
    """Time-series VRT with union spatial extent (one band per timestep)."""
    vrt_dtype  = nisar_tools.get_gdal_dtype(dtype)
    if nodata is None:
        nodata = stack_items[0].get("nodata")
    nodata_val = _gdal_nodata_str(nodata, dtype)
    res_x = abs(stack_items[0]["transform"].a)
    res_y = abs(stack_items[0]["transform"].e)

    union_ulx = min(it["transform"].c for it in stack_items)
    union_uly = max(it["transform"].f for it in stack_items)
    union_lrx = max(it["transform"].c + it["w"] * res_x for it in stack_items)
    union_lry = min(it["transform"].f - it["h"] * res_y for it in stack_items)
    union_w   = max(1, int(round((union_lrx - union_ulx) / res_x)))
    union_h   = max(1, int(round((union_uly - union_lry) / res_y)))
    union_tf  = from_origin(union_ulx, union_uly, res_x, res_y)

    geo = f"{union_tf.c}, {union_tf.a}, {union_tf.b}, {union_tf.f}, {union_tf.d}, {union_tf.e}"
    lines = [
        f'<VRTDataset rasterXSize="{union_w}" rasterYSize="{union_h}">',
        f'  <SRS dataAxisToSRSAxisMapping="1,2">{crs_wkt}</SRS>',
        f"  <GeoTransform>{geo}</GeoTransform>",
    ]
    if metadata:
        lines.append(nisar_tools._vrt_metadata_xml(metadata))
    for i, it in enumerate(stack_items):
        dx   = int(round((it["transform"].c - union_ulx) / res_x))
        dy   = int(round((union_uly - it["transform"].f) / res_y))
        date = it.get("date", "")
        src_path, rel_attr = _vrt_src_entry(it["path"])
        lines.append(
            f'  <VRTRasterBand dataType="{vrt_dtype}" band="{i + 1}">\n'
            f"    <NoDataValue>{nodata_val}</NoDataValue>\n"
            f"    <Description>{date}</Description>\n"
            f'    <Metadata><MDI key="Date">{date}</MDI></Metadata>\n'
            f"    <SimpleSource>\n"
            f'      <SourceFilename relativeToVRT="{rel_attr}">{src_path}</SourceFilename>\n'
            f'      <SourceBand>{it["band_idx"]}</SourceBand>\n'
            f'      <SrcRect xOff="0" yOff="0" xSize="{it["w"]}" ySize="{it["h"]}" />\n'
            f'      <DstRect xOff="{dx}" yOff="{dy}" xSize="{it["w"]}" ySize="{it["h"]}" />\n'
            f"    </SimpleSource>\n"
            f"  </VRTRasterBand>"
        )
    lines.append("</VRTDataset>")
    return "\n".join(lines)


def _track_vrt_filename(metas, pol_str, mode_str):
    """Build a NISAR GSLC time-series VRT filename from a list of metadata dicts."""
    m0 = metas[0]
    il, pt, prod = m0["il"], m0["pt"], m0["prod"]
    obs_mode, mode, pol = m0["obs_mode"], m0["mode"], m0["polarization"]
    freq = m0["freq"]

    cycles = sorted({m["cycle"] for m in metas})
    cycle_str = f"{min(cycles):03d}-{max(cycles):03d}" if len(cycles) > 1 else f"{cycles[0]:03d}"

    combos = sorted({(m["track"], m["direction"], m["frame"]) for m in metas})
    if len(combos) <= 4:
        track_str = "-".join(f"{t:03d}" for t, d, f in combos)
        dir_str   = "-".join(d for t, d, f in combos)
        frame_str = "-".join(f"{f:03d}" for t, d, f in combos)
    else:
        all_dirs  = sorted({d for _, d, _ in combos})
        track_str = "999"
        dir_str   = "-".join(all_dirs)
        frame_str = "999"

    min_start = min(m["start_time"] for m in metas)
    max_end   = max(m["end_time"]   for m in metas)

    if mode_str:
        ebd = f"-EBD_{freq}_{pol_str}_{mode_str}.vrt"
    else:
        ebd = f"-EBD_{freq}_{pol_str}.vrt"

    return (f"NISAR_{il}_{pt}_{prod}_{cycle_str}_{track_str}_{dir_str}_"
            f"{frame_str}_{mode}_{pol}_{obs_mode}_{min_start}_{max_end}"
            f"{ebd}")


def _make_ts_vrt(ts_items, crs_wkt, dtype, nodata=None, metadata=None):
    """Choose between same-extent and union time-series VRT builder."""
    if nodata is None:
        nodata = ts_items[0].get("nodata")
    same_geo = all(
        it["w"] == ts_items[0]["w"]
        and it["h"] == ts_items[0]["h"]
        and it["transform"] == ts_items[0]["transform"]
        for it in ts_items
    )
    if same_geo:
        return nisar_tools.generate_vrt_xml_timeseries(
            ts_items[0]["w"], ts_items[0]["h"],
            ts_items[0]["transform"], crs_wkt,
            ts_items, dtype=dtype, nodata=nodata, metadata=metadata,
        )
    return _generate_ts_union_vrt_xml(crs_wkt, ts_items, dtype,
                                      nodata=nodata, metadata=metadata)


def _green(text):
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        return f"\033[32m{text}\033[0m"
    return text


def _print_vrt_summary(output_path, summary, vsis3=False):
    """Print a structured VRT/TIF summary."""
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

    for section, label in [("backscatter", "GSLC bands")]:
        data   = summary.get(section, {})
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


def show_output_summary(output_path, frequency, output_auth=None, vsis3=False):
    """Scan output directory and print a structured summary (read-only)."""
    import s3fs as _s3fs

    out_dir   = output_path.rstrip("/")
    output_fs = None
    if output_path.startswith("s3://"):
        auth = output_auth or {}
        if "profile" in auth:
            output_fs = _s3fs.S3FileSystem(profile=auth["profile"])
        elif "key" in auth:
            output_fs = _s3fs.S3FileSystem(key=auth["key"], secret=auth["secret"],
                                            token=auth.get("token"))
        else:
            output_fs = _s3fs.S3FileSystem(anon=False)

    if output_fs:
        bucket_path = output_path.replace("s3://", "").rstrip("/")
        try:
            all_files = [f"s3://{f}" for f in output_fs.ls(bucket_path)]
        except Exception:
            all_files = []
    else:
        all_files = sorted(glob.glob(os.path.join(out_dir, "*")))

    tag  = f"-EBD_{frequency}"
    tifs = [f for f in all_files if f.endswith(".tif") and tag in f]
    vrts = [f for f in all_files if f.endswith(".vrt") and tag in f]

    summary = {"backscatter": {"single_dates": tifs, "mosaics": [],
                                "ts_by_track": vrts, "combined_ts": []}}
    _print_vrt_summary(output_path, summary, vsis3=vsis3)


# =========================================================
# BUILD TRACK VRTs (4-phase, GSLC-adapted)
# =========================================================


def build_track_vrts(
    output_path, frequency, mode_str,
    verbose=False, output_auth=None, vsis3=False, reset_vrts=False,
):
    """
    Post-process: build VRTs in four phases, then print a structured summary.

    Phase 1 -- Grid mosaic VRTs (when multiple frames share a cycle/track).
    Phase 2 -- Single-date multi-pol VRTs (snapshot VRTs per acquisition).
    Phase 3 -- Per-track time-series VRTs (one band per date).
    Phase 4 -- Combined multi-track time-series VRTs (when >1 track).
    """
    import s3fs as _s3fs

    out_dir   = output_path.rstrip("/")
    output_fs = None
    if output_path.startswith("s3://"):
        auth = output_auth or {}
        if "profile" in auth:
            output_fs = _s3fs.S3FileSystem(profile=auth["profile"])
        elif "key" in auth:
            output_fs = _s3fs.S3FileSystem(key=auth["key"], secret=auth["secret"],
                                            token=auth.get("token"))
        else:
            output_fs = _s3fs.S3FileSystem(anon=False)

    if reset_vrts:
        if verbose:
            print("    Deleting existing VRTs...", flush=True)
        if output_fs:
            bucket_path = output_path.replace("s3://", "").rstrip("/")
            try:
                existing  = output_fs.ls(bucket_path)
                vrt_files = [f for f in existing if f.endswith(".vrt")]
                for vf in vrt_files:
                    output_fs.rm(vf)
            except Exception:
                pass
        else:
            for vf in glob.glob(os.path.join(out_dir, "*.vrt")):
                os.remove(vf)

    _is_s3       = output_path.startswith("s3://")
    _local_vrt_dir = None
    if _is_s3:
        import tempfile as _tempfile
        _local_vrt_dir = _tempfile.mkdtemp(prefix="openseppo_gslc_vrts_")

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
        if not _local_vrt_dir or not _is_s3:
            return
        import subprocess
        cmd = ["aws", "s3", "sync", _local_vrt_dir, out_dir + "/",
               "--exclude", "*", "--include", "*.vrt", "--include", "*.txt",
               "--no-progress"]
        if verbose:
            print(f"    Syncing VRTs to {out_dir}/...", flush=True)
        subprocess.run(cmd, check=True)
        import shutil
        shutil.rmtree(_local_vrt_dir, ignore_errors=True)

    # --- Collect all TIFs and parse metadata ---
    tif_files = _list_all_nisar_tifs(output_path, frequency, mode_str, output_fs)
    parsed = []
    for fpath in tif_files:
        meta = _parse_nisar_tif_meta(fpath)
        if meta is not None:
            parsed.append((fpath, meta))

    # Read geo from one TIF per (track, direction) group
    _td_groups = defaultdict(list)
    for fpath, meta in parsed:
        _td_groups[(meta["track"], meta["direction"])].append((fpath, meta))

    _geo_cache = {}
    for (trk, dir_), group in _td_groups.items():
        _sample = group[0][0]
        geo = _read_tif_geo(_sample, output_fs)
        if geo is not None:
            _geo_cache[(trk, dir_)] = geo

    all_metas = []
    for fpath, meta in parsed:
        geo = _geo_cache.get((meta["track"], meta["direction"]))
        if geo is None:
            continue
        meta.update(geo)
        all_metas.append(meta)

    # Extract VRT-level metadata from the first TIF
    _vrt_meta = {}
    if all_metas:
        _src_tags = all_metas[0].get("tags", {})
        for _k in ("TRANSFORM_MODE", "OPENSEPPO_VERSION", "CRID", "ISCE3_VERSION"):
            if _k in _src_tags:
                _vrt_meta[_k] = _src_tags[_k]

    if verbose and all_metas:
        print(f"  build_track_vrts: {len(all_metas)} TIFs "
              f"across {len({m['track'] for m in all_metas})} track(s).")

    summary = {"backscatter": {"single_dates": [], "mosaics": [], "ts_by_track": [],
                                "combined_ts": []}}

    # Group by pol_str (e.g. "hh", "hv")
    by_pol = defaultdict(list)
    for m in all_metas:
        by_pol[m["pol_str"]].append(m)

    date_pol_sources = defaultdict(list)

    for pol_str, pol_metas in sorted(by_pol.items()):
        dtype = pol_metas[0]["dtype"]
        _ms   = pol_metas[0]["mode_str"]

        by_td = defaultdict(list)
        for m in pol_metas:
            by_td[(m["track"], m["direction"])].append(m)

        track_dir_ts = {}

        for (track, direction), td_metas in sorted(by_td.items()):
            _td_crs = td_metas[0]["crs_wkt"]
            frames  = sorted({m["frame"] for m in td_metas})

            # Phase 1: Grid mosaic VRTs (multiple frames)
            if len(frames) > 1:
                by_cycle = defaultdict(list)
                for m in td_metas:
                    by_cycle[m["cycle"]].append(m)

                mosaic_items = []
                for cycle, cyc_metas in sorted(by_cycle.items()):
                    frame_items = sorted(cyc_metas, key=lambda x: x["frame"])
                    cyc_frames  = sorted({m["frame"] for m in frame_items})

                    if len(cyc_frames) > 1:
                        mosaic_xml, union_tf, union_w, union_h = _generate_mosaic_vrt_xml(
                            frame_items, _td_crs, dtype, metadata=_vrt_meta,
                        )
                        m0 = frame_items[0]
                        min_fr, max_fr = cyc_frames[0], cyc_frames[-1]
                        min_st = min(m["start_time"] for m in frame_items)
                        max_et = max(m["end_time"]   for m in frame_items)
                        ebd = (f"-EBD_{frequency}_{pol_str}_{_ms}.vrt" if _ms
                               else f"-EBD_{frequency}_{pol_str}.vrt")
                        mosaic_name = (
                            f"NISAR_{m0['il']}_{m0['pt']}_{m0['prod']}_{cycle:03d}_{track:03d}_"
                            f"{direction}_{min_fr:03d}-{max_fr:03d}_{m0['mode']}_{m0['polarization']}_"
                            f"{m0['obs_mode']}_{min_st}_{max_et}_{m0['crid']}_{m0['accuracy']}"
                            f"{ebd}"
                        )
                        mosaic_path = f"{out_dir}/{mosaic_name}"
                        write_vrt(mosaic_path, mosaic_xml)
                        summary["backscatter"]["mosaics"].append(mosaic_path)

                        ds_str = min_st[:8]
                        mi = {"path": mosaic_path, "band_idx": 1,
                              "date": f"{ds_str[:4]}-{ds_str[4:6]}-{ds_str[6:]}",
                              "transform": union_tf, "w": union_w, "h": union_h,
                              "nodata": m0.get("nodata")}
                        mosaic_items.append(mi)
                        date_pol_sources[(track, direction, mi["date"])].append(
                            (mosaic_path, pol_str, _ms))
                    else:
                        m = frame_items[0]
                        ds_str = m["start_time"][:8]
                        ti = {"path": m["path"], "band_idx": 1,
                              "date": f"{ds_str[:4]}-{ds_str[4:6]}-{ds_str[6:]}",
                              "transform": m["transform"], "w": m["w"], "h": m["h"],
                              "nodata": m.get("nodata")}
                        mosaic_items.append(ti)
                        date_pol_sources[(track, direction, ti["date"])].append(
                            (m["path"], pol_str, _ms))
                        summary["backscatter"]["single_dates"].append(m["path"])

                track_dir_ts[(track, direction)] = {"ts_items": mosaic_items,
                                                     "metas": td_metas}
            else:
                sorted_metas = sorted(td_metas, key=lambda x: x["start_time"])
                ts_items = []
                for m in sorted_metas:
                    ds_str = m["start_time"][:8]
                    ti = {"path": m["path"], "band_idx": 1,
                          "date": f"{ds_str[:4]}-{ds_str[4:6]}-{ds_str[6:]}",
                          "transform": m["transform"], "w": m["w"], "h": m["h"],
                          "nodata": m.get("nodata")}
                    ts_items.append(ti)
                    date_pol_sources[(track, direction, ti["date"])].append(
                        (m["path"], pol_str, _ms))
                    summary["backscatter"]["single_dates"].append(m["path"])
                track_dir_ts[(track, direction)] = {"ts_items": ts_items, "metas": td_metas}

        # Phase 3: Per-track time-series VRTs
        for (track, direction), info in sorted(track_dir_ts.items()):
            ts_items  = info["ts_items"]
            td_metas  = info["metas"]
            _td_crs   = td_metas[0]["crs_wkt"]
            if len(ts_items) > 1:
                ts_xml  = _make_ts_vrt(ts_items, _td_crs, dtype, metadata=_vrt_meta)
                ts_name = _track_vrt_filename(td_metas, pol_str, _ms)
                ts_path = f"{out_dir}/{ts_name}"
                write_vrt(ts_path, ts_xml)
                summary["backscatter"]["ts_by_track"].append(ts_path)
                if verbose:
                    print(f"    TS VRT: {ts_name}")

        # Phase 4: Combined multi-track VRT
        _all_crs = {info["metas"][0]["crs_wkt"] for info in track_dir_ts.values()}
        if len(track_dir_ts) > 1 and len(_all_crs) == 1:
            _combined_crs = _all_crs.pop()
            combined_items = sorted(
                [item for info in track_dir_ts.values() for item in info["ts_items"]],
                key=lambda x: x["date"],
            )
            if len(combined_items) > 1:
                combined_metas = [m for info in track_dir_ts.values() for m in info["metas"]]
                combined_xml  = _make_ts_vrt(combined_items, _combined_crs, dtype,
                                             metadata=_vrt_meta)
                combined_name = _track_vrt_filename(combined_metas, pol_str, _ms)
                combined_path = f"{out_dir}/{combined_name}"
                write_vrt(combined_path, combined_xml)
                summary["backscatter"]["combined_ts"].append(combined_path)

    # Phase 2: Single-date multi-pol VRTs
    for (_trk, _dir, date), pol_paths in sorted(date_pol_sources.items()):
        seen   = set()
        unique = []
        for path, pstr, ms in pol_paths:
            if path not in seen:
                seen.add(path)
                unique.append((path, pstr, ms))
        if len(unique) > 1:
            _pol_order = {"hh": 0, "hv": 1, "vh": 2, "vv": 3,
                          "rh": 0, "rv": 1}
            unique.sort(key=lambda x: (_pol_order.get(x[1], 9), x[1]))
            band_files = [p for p, _, _ in unique]
            band_names = [ps for _, ps, _ in unique]
            _tif_ms    = unique[0][2]
            ref_geo    = _geo_cache.get((_trk, _dir))
            if ref_geo:
                tf   = ref_geo["transform"]
                w, h = ref_geo["w"], ref_geo["h"]
                crs_w, dt = ref_geo["crs_wkt"], ref_geo["dtype"]
                nd   = ref_geo["nodata"]
                pol_list_str = "".join(band_names)
                ebd  = f"-EBD_{frequency}_{pol_list_str}_{_tif_ms}.vrt"
                src_base = os.path.basename(band_files[0])
                if "-EBD_" in src_base:
                    vrt_name = src_base.split("-EBD_")[0] + ebd
                else:
                    vrt_name = src_base.replace(".tif", "") + ebd
                vrt_path = f"{out_dir}/{vrt_name}"
                vrt_xml  = nisar_tools.generate_vrt_xml_single_step(
                    w, h, tf, crs_w, band_files, band_names, date,
                    dtype=dt, nodata=nd, metadata=_vrt_meta,
                )
                write_vrt(vrt_path, vrt_xml)
                covered = set(band_files)
                summary["backscatter"]["single_dates"] = [
                    p for p in summary["backscatter"]["single_dates"]
                    if p not in covered
                ]
                summary["backscatter"]["single_dates"].append(vrt_path)

    _sync_vrts_to_s3()
    _print_vrt_summary(output_path, summary, vsis3=vsis3)


# =========================================================
# MAIN PROCESSING
# =========================================================


def processing(args):
    output_profile = args.output_profile if args.output_profile else args.profile
    output_auth    = get_auth_dict(output_profile, use_earthdata=False)

    # Check S3 write access early before any processing
    if hasattr(args, 'output') and args.output and args.output.startswith("s3://"):
        from openseppo.nisar.nisar_tools import check_s3_write_access
        check_s3_write_access(args.output, output_auth)

    # Show output summary (read-only)
    if args.show_vrts:
        show_output_summary(
            output_path=args.output,
            frequency=args.freq,
            output_auth=output_auth,
            vsis3=args.vsis3,
        )
        return

    # Rebuild VRTs only
    if args.rebuild_only:
        print(f"Rebuilding VRTs in {args.output} ...")
        build_track_vrts(
            output_path=args.output,
            frequency=args.freq,
            mode_str=args.mode if args.mode else "pwr",
            verbose=args.verbose,
            output_auth=output_auth,
            reset_vrts=args.reset_vrts,
        )
        return

    if not args.h5:
        print("Error: --h5/-i is required for processing.")
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

    # Auto-detect earthdata authentication
    if urls[0].startswith("s3://"):
        bucket = urls[0].split("/")[2]
        if bucket in asf_buckets and not args.use_earthdata:
            print("---> Detected ASF DAAC Bucket. Using Earthdata credentials.")
            args.use_earthdata = True
    elif urls[0].startswith("https://") and not args.use_earthdata:
        earthdata_hosts = ["earthdatacloud.nasa.gov", "urs.earthdata.nasa.gov",
                           "e4ftl01.cr.usgs.gov"]
        if any(h in urls[0] for h in earthdata_hosts):
            print("---> Detected Earthdata HTTPS URL. Using Earthdata credentials.")
            args.use_earthdata = True

    input_profile = args.input_profile if args.input_profile else args.profile
    input_auth    = get_auth_dict(input_profile, args.use_earthdata)

    print(f"Starting GSLC Batch Processing: {len(urls)} files.")
    _ds_label = (f"{args.downscale[0]}x{args.downscale[1]}"
                 if isinstance(args.downscale, tuple) else str(args.downscale))
    _sq_label = " (--square)" if args.square and args.downscale is None else ""
    print(f"Mode: {args.mode} | Freq: {args.freq} | Downscale: {_ds_label}{_sq_label}")

    try:
        result = nisar_tools_gslc.process_chunk_task_gslc(
            h5_url=urls,
            variable_names=args.vars,
            output_path=args.output,
            srcwin=tuple(args.srcwin) if args.srcwin else None,
            projwin=tuple(args.projwin) if args.projwin else None,
            projwin_srs=args.projwin_srs,
            transform_mode=args.mode,
            frequency=args.freq,
            single_bands=args.single_bands,
            vrt=(not args.no_vrt),
            downscale_factor=args.downscale,
            target_align_pixels=(not args.no_tap),
            input_auth=input_auth,
            output_auth=output_auth,
            time_series_vrt=(not args.no_time_series),
            list_grids=args.list_grids,
            list_vars=args.list_vars,
            verbose=args.verbose,
            cache=args.cache,
            keep=args.keep_cached,
            target_srs=args.target_srs,
            target_res=args.target_res,
            resample=args.resample,
            output_format=args.output_format,
            fill_holes=args.fill_holes,
            num_threads=args.warp_threads,
            read_threads=args.read_threads,
            square_pixels=args.square,
        )
        print("\n" + str(result))

        if not args.no_time_series and not args.list_grids and not args.list_vars and args.output:
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
    args  = myargsparse(a)
    start = time.perf_counter()
    processing(args)
    end   = time.perf_counter()

    if args.verbose:
        duration = end - start
        minutes  = int(duration / 60)
        seconds  = duration - minutes * 60
        print(f"\nRuntime: {minutes}m {seconds:.2f}s\n")


def main():
    _main(sys.argv)


if __name__ == "__main__":
    main()
