#!/usr/bin/env python
"""
seppo_nisar_gunw_convert -- NISAR GUNW HDF5 to COG / self-contained HDF5 subset
*******************************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Convert NISAR GUNW (Geocoded Unwrapped interferogram) HDF5 files to Cloud
Optimized GeoTIFF (COG/GTiff) per interferometric layer, or to a fully
self-contained windowed HDF5 subset, with optional reprojection and downscaling
-- the GUNW analogue of seppo_nisar_gcov_convert.

GUNW layers are grouped into three geocoded grids of differing resolution,
selected with --layer_group:
    unwrappedInterferogram  (default): unwrappedPhase, coherenceMagnitude,
                                       connectedComponents, ionospherePhaseScreen[,Uncertainty]
    pixelOffsets           : alongTrackOffset, slantRangeOffset, correlationSurfacePeak
    wrappedInterferogram   : wrappedInterferogram (complex -> phase), coherenceMagnitude
The group-level `mask` (uint32 bit-encoded) is selectable in any group.

Raster output is single-grid (one layer group per run).  The -of h5 subset
carries every group, each windowed on its own axes, and is self-contained
(identification, orbit, attitude, processingInformation, radarGrid).

Usage examples:
1. List layers/grids in a granule:
    seppo_nisar_gunw_convert --h5 gunw.h5 -lg

2. Core unwrapped layers as COGs, subset to an AOI (lon/lat):
    seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ \
        -projwin -66.6 9.6 -66.2 9.3 -projwin_srs 4326

3. Self-contained windowed HDF5 subset (all groups):
    seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ -of h5 \
        -projwin -66.6 9.6 -66.2 9.3 -projwin_srs 4326

3b. Lightweight deformation-only h5 subset (drop the 20 m wrapped grid):
    seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ -of h5 \
        -groups unwrappedInterferogram pixelOffsets \
        -projwin -66.6 9.6 -66.2 9.3 -projwin_srs 4326

4. Pixel-offset layers, reprojected to WGS84:
    seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ -lyr pixelOffsets -t_srs 4326

5. COGs plus a coseismic PDF report with the epicenter marked:
    seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ \
        -projwin -68.85 10.55 -68.35 10.05 -projwin_srs 4326 \
        --report --report_format pdf --epicenter -68.60 10.30
"""

import sys
import os
import time
import argparse
import shlex
from pprint import pprint

import openseppo.nisar.nisar_tools_gunw as gunw_tools


asf_buckets = ["sds-n-cumulus-prod-nisar-products", "sds-n-cumulus-prod-nisar-ur-products"]


def seppo_parse_args(parser, a):
    return parser.parse_args(a[1:])


def myargsparse(a):
    class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter,
                          argparse.RawDescriptionHelpFormatter):
        pass

    if isinstance(a, str):
        a = shlex.split(a)

    thisProg = os.path.basename(a[0])
    description = ("Convert NISAR GUNW HDF5 data to Cloud Optimized GeoTIFF (COG) "
                  "or a self-contained windowed HDF5 subset.")
    parser = argparse.ArgumentParser(prog=thisProg, description=description,
                                     epilog=__doc__, formatter_class=CustomFormatter)

    # --- I/O ---
    parser.add_argument("-i", "--h5", type=str, nargs="+",
                        help="Input H5 URL(s) or a text file listing URLs.")
    parser.add_argument("-o", "--output", type=str,
                        help="Output directory (S3 or local). End in '/' for batch.")

    # --- Layer selection ---
    parser.add_argument("-lyr", "--layer_group", type=str, default=gunw_tools.DEFAULT_LAYER_GROUP,
                        choices=list(gunw_tools.GUNW_LAYER_GROUPS.keys()),
                        help="Which GUNW grid to rasterise (raster output is single-grid). "
                             "Ignored for -of h5 (every group is windowed).")
    parser.add_argument("-vars", "--vars", nargs="+", default=None,
                        help="Layers to extract within the layer group. If omitted, the "
                             "group's default set is used.")
    parser.add_argument("-f", "--freq", type=str, default=None, choices=["A", "B"],
                        help="Frequency (A/B). Defaults to the first present in the granule.")
    parser.add_argument("-pol", "--pol", type=str, default=None,
                        help="Polarisation subgroup (e.g. HH). Defaults to the first present.")

    parser.add_argument("-groups", "--groups", nargs="+", default=None,
                        choices=list(gunw_tools.GUNW_LAYER_GROUPS.keys()),
                        help="For -of h5: restrict which grid sub-groups the subset carries "
                             "(default: all). Dropping wrappedInterferogram (the ~4x-finer "
                             "complex grid) removes most of the payload for a deformation-only "
                             "subset. No effect on raster output (use --layer_group there).")
    parser.add_argument("-lg", "--list_grids", action="store_true",
                        help="Scan the first H5 file, list all grids/layers, then exit.")

    # --- Output format ---
    parser.add_argument("-of", "--output_format", type=str, default="COG",
                        choices=["COG", "GTiff", "h5"],
                        help="Output format: COG (default), GTiff (BigTIFF), h5 (self-contained subset).")

    # --- Subsetting ---
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-srcwin", "--srcwin", nargs=4, type=int,
                       metavar=("XOFF", "YOFF", "XSIZE", "YSIZE"),
                       help="Pixel subset window on the selected layer group's grid.")
    group.add_argument("-projwin", "--projwin", nargs=4, type=float,
                       metavar=("ULX", "ULY", "LRX", "LRY"),
                       help="Geographic subset window. Native/target CRS unless -projwin_srs is given.")
    parser.add_argument("-projwin_srs", "--projwin_srs", type=str,
                        help="CRS of -projwin coordinates (e.g. EPSG:4326 or 4326).")

    # --- Reprojection / resampling ---
    parser.add_argument("-t_srs", "--target_srs", type=str, default=None,
                        help="Target CRS for output (e.g. EPSG:4326 or 4326). Default: native.")
    parser.add_argument("-tr", "--target_res", type=float, nargs=2, metavar=("XRES", "YRES"),
                        default=None, help="Output pixel size in target CRS units (with -t_srs).")
    parser.add_argument("--no_tap", action="store_true",
                        help="Disable pixel-grid alignment (tap) of the reprojected origin.")
    parser.add_argument("--resample", type=str, default="bilinear",
                        help="Resampling for continuous layers on reprojection "
                             "(nearest/bilinear/cubic/...). Integer layers always use nearest.")
    parser.add_argument("-d", "--downscale", type=int, default=None,
                        help="Integer downscale factor (block reduce).")
    parser.add_argument("--no_time_series", action="store_true",
                        help="Disable the time-series VRT stacks built over a batch of "
                             "granules (one VRT per layer, one band per interferometric "
                             "pair, ordered by reference acquisition).")
    parser.add_argument("--no_vrt", action="store_true",
                        help="Disable the per-snapshot multi-layer VRT.")

    # --- Visualization / event report ---
    parser.add_argument("-report", "--report", action="store_true",
                        help="(EXPERIMENTAL) Also produce a coseismic InSAR quick-look report: "
                             "wrapped fringes (cyclic), relative LOS displacement (seismic "
                             "diverging colormap, cm), coherence, and an info panel.")
    parser.add_argument("--report_format", type=str, default="png",
                        choices=["png", "pdf"], help="Report image format. Default: png.")
    parser.add_argument("-epicenter", "--epicenter", nargs=2, type=float,
                        metavar=("LON", "LAT"),
                        help="Mark an event epicenter (lon lat, EPSG:4326) on the report panels.")

    # --- Auth ---
    parser.add_argument("--profile", type=str, help="AWS profile (input and output).")
    parser.add_argument("--input_profile", type=str, help="AWS profile for reading inputs.")
    parser.add_argument("--output_profile", type=str, help="AWS profile for writing outputs.")

    # --- Threads / caching ---
    parser.add_argument("--read_threads", type=int, default=8, metavar="N",
                        help="Parallel connections for reading HDF5 chunks/metadata. Default: 8.")
    parser.add_argument("--warp_threads", type=int, default=None, metavar="N",
                        help="Threads for reprojection. Default: all cores.")
    parser.add_argument("-cache", "--cache", default=None, action="store",
                        help="Cache remote files locally first. 'y'/'yes' for a temp dir.")
    parser.add_argument("-keep", "--keep_cached", action="store_true",
                        help="With -cache, keep the cached H5 file.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")

    args = seppo_parse_args(parser, a)
    args.use_earthdata = False

    if args.verbose:
        pprint(vars(args))

    if not args.list_grids and not args.output:
        parser.error("the following arguments are required: --output/-o (unless --list_grids).")
    if not args.h5:
        parser.error("the following arguments are required: --h5/-i.")
    return args


def get_auth_dict(profile_arg, use_earthdata=False):
    auth = {}
    if use_earthdata:
        auth["use_earthdata"] = True
        return auth
    if profile_arg:
        auth["profile"] = profile_arg
        return auth
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        auth["key"] = os.environ.get("AWS_ACCESS_KEY_ID")
        auth["secret"] = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if os.environ.get("AWS_SESSION_TOKEN"):
            auth["token"] = os.environ.get("AWS_SESSION_TOKEN")
    return auth


def _read_url_list(items):
    def _is_url(s):
        return s.startswith("s3://") or s.startswith("https://") or os.path.isfile(s)
    urls = []
    for item in items:
        if os.path.isfile(item) and not item.endswith(".h5"):
            with open(item) as fh:
                urls.extend([ln.strip() for ln in fh if _is_url(ln.strip())])
        elif item.startswith("s3://") and not item.endswith(".h5"):
            import fsspec
            with fsspec.open(item, "r") as fh:
                urls.extend([ln.strip() for ln in fh if _is_url(ln.strip())])
        else:
            urls.append(item)
    return urls


def processing(args):
    output_profile = args.output_profile or args.profile
    output_auth = get_auth_dict(output_profile, use_earthdata=False)

    if args.output and args.output.startswith("s3://"):
        from openseppo.nisar.nisar_tools import check_s3_write_access
        check_s3_write_access(args.output, output_auth)

    urls = _read_url_list(args.h5)
    if not urls:
        print("Error: no valid input URLs found.", file=sys.stderr)
        sys.exit(1)

    # Auth auto-detection
    if urls[0].startswith("s3://"):
        bucket = urls[0].split("/")[2]
        if bucket in asf_buckets:
            print("---> Detected ASF DAAC bucket. Using Earthdata credentials.")
            args.use_earthdata = True
    elif urls[0].startswith("https://"):
        earthdata_hosts = ["earthdatacloud.nasa.gov", "urs.earthdata.nasa.gov",
                           "asf.earthdatacloud.nasa.gov"]
        if any(h in urls[0] for h in earthdata_hosts):
            print("---> Detected Earthdata HTTPS URL. Using Earthdata credentials.")
            args.use_earthdata = True

    input_profile = args.input_profile or args.profile
    input_auth = get_auth_dict(input_profile, args.use_earthdata)

    # Auto-cache remote full-frame reads (no subset) as GCOV does.
    is_remote = urls[0].startswith("s3://") or urls[0].startswith("https://")
    if (args.cache is None and not args.list_grids and not args.srcwin
            and not args.projwin and is_remote):
        args.cache = "y"

    if not args.list_grids:
        print(f"Starting GUNW processing: {len(urls)} file(s).")
        print(f"Format: {args.output_format} | Layer group: {args.layer_group} | "
              f"Freq: {args.freq or 'auto'} | Downscale: {args.downscale}")

    result = gunw_tools.process_gunw_task(
        h5_url=urls, variable_names=args.vars, output_path=args.output,
        srcwin=tuple(args.srcwin) if args.srcwin else None,
        projwin=tuple(args.projwin) if args.projwin else None,
        projwin_srs=args.projwin_srs, frequency=(args.freq or "A"),
        layer_group=args.layer_group, pol=args.pol,
        output_format=args.output_format, vrt=(not args.no_vrt),
        time_series_vrt=(not args.no_time_series),
        downscale_factor=args.downscale,
        target_align_pixels=(not args.no_tap),
        input_auth=input_auth, output_auth=output_auth,
        list_grids=args.list_grids, verbose=args.verbose,
        cache=args.cache, keep=args.keep_cached,
        target_srs=args.target_srs, target_res=args.target_res,
        resample=args.resample, num_threads=args.warp_threads,
        read_threads=args.read_threads, groups=args.groups,
        report=args.report, report_format=args.report_format,
        epicenter=tuple(args.epicenter) if args.epicenter else None)
    print("\n" + str(result))


def _main(a):
    args = myargsparse(a)
    start = time.perf_counter()
    try:
        processing(args)
    except Exception as e:  # noqa: BLE001
        print(f"\nCRITICAL FAILURE: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    if args.verbose:
        dur = time.perf_counter() - start
        print(f"\nRuntime: {int(dur // 60)}m {dur % 60:.2f}s\n")


def main():
    from openseppo import banner
    banner("seppo_nisar_gunw_convert")
    _main(sys.argv)


if __name__ == "__main__":
    main()
