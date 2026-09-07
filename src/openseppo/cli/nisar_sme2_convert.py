#!/usr/bin/env python
"""
seppo_nisar_sme2_convert -- NISAR SME2 HDF5 to COG / self-contained HDF5 subset
*******************************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Convert NISAR SME2 (Soil Moisture Estimate, L3) HDF5 files to Cloud Optimized
GeoTIFF (COG/GTiff) per layer, or to a fully self-contained windowed HDF5
subset, with optional reprojection and downscaling -- the SME2 analogue of
seppo_nisar_gcov_convert.

Every SME2 layer sits on one shared grid, EASE-Grid 2.0 Global (EPSG:6933) at a
200 m posting, so one window covers the whole granule.  Layers are grouped for
selection with --layer_group:
    soilMoisture        (default): soilMoisture, soilMoistureUncertainty,
                                   retrievalQualityFlag [, surfaceQualityFlag]
    algorithmCandidates: the per-algorithm retrieval (--algorithm, e.g. DSG)
    ancillaryData      : landCover, localIncidenceAngle[,Uncertainty],
                         waterbodyFraction [, surfaceQualityFlag]
    radarData          : sigma0, noiseEquivalentBackscatter, numberOfLooks
                         per polarisation (--freq A|B)

Layers are discovered in the granule rather than assumed: SME2 moved
surfaceQualityFlag between release tiers, and a granule may carry only some of
the algorithm candidates.  Run -lg to see exactly what a file holds.

Because the granule is EASE-Grid metres, a lon/lat -projwin is detected and
reprojected rather than taken literally (which would yield a 1x1 subset).

Usage examples:
1. List grid, groups and layers in a granule:
    seppo_nisar_sme2_convert --h5 sme2.h5 -lg

2. Soil moisture layers as COGs, subset to an AOI (lon/lat):
    seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ \
        -projwin -117.2 37.8 -116.4 37.2 -projwin_srs 4326

3. Self-contained windowed HDF5 subset (all groups):
    seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ -of h5 \
        -projwin -117.2 37.8 -116.4 37.2 -projwin_srs 4326

3b. Soil-moisture-only h5 subset (drop radar and ancillary grids):
    seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ -of h5 \
        -groups algorithmCandidates

4. The DSG algorithm candidate, reprojected to WGS84:
    seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ \
        -lyr algorithmCandidates --algorithm DSG -t_srs 4326

5. Radar backscatter (frequency A) as COGs:
    seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ -lyr radarData -f A
"""

import sys
import os
import time
import argparse
import shlex
from pprint import pprint

import openseppo.nisar.nisar_tools_sme2 as sme2_tools


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
    description = ("Convert NISAR SME2 (soil moisture, L3) HDF5 data to Cloud Optimized "
                   "GeoTIFF (COG) or a self-contained windowed HDF5 subset.")
    parser = argparse.ArgumentParser(prog=thisProg, description=description,
                                     epilog=__doc__, formatter_class=CustomFormatter)

    # --- I/O ---
    parser.add_argument("-i", "--h5", type=str, nargs="+",
                        help="Input H5 URL(s) or a text file listing URLs.")
    parser.add_argument("-o", "--output", type=str,
                        help="Output directory (S3 or local). End in '/' for batch.")

    # --- Layer selection ---
    parser.add_argument("-lyr", "--layer_group", type=str,
                        default=sme2_tools.DEFAULT_LAYER_GROUP,
                        choices=list(sme2_tools.SME2_LAYER_GROUPS.keys()),
                        help="Which group of layers to rasterise. "
                             "Ignored for -of h5 (every group is windowed).")
    parser.add_argument("-vars", "--vars", nargs="+", default=None,
                        help="Layers to extract within the layer group. If omitted, the "
                             "group's default set is used (or every layer present).")
    parser.add_argument("--algorithm", type=str, default=None,
                        help="Algorithm candidate for -lyr algorithmCandidates "
                             "(e.g. DSG). Defaults to the first present.")
    parser.add_argument("-f", "--freq", type=str, default=None, choices=["A", "B"],
                        help="Frequency for -lyr radarData. Defaults to the first present.")

    parser.add_argument("-groups", "--groups", nargs="+", default=None,
                        help="For -of h5: restrict which grid sub-groups the subset "
                             "carries (algorithmCandidates, ancillaryData, radarData); "
                             "the root soil-moisture layers are always kept. "
                             "No effect on raster output (use --layer_group there).")
    parser.add_argument("-lg", "--list_grids", action="store_true",
                        help="Scan the first H5 file, list the grid and layers, then exit.")

    # --- Output format ---
    parser.add_argument("-of", "--output_format", type=str, default="COG",
                        choices=["COG", "GTiff", "h5"],
                        help="Output format: COG (default), GTiff (BigTIFF), "
                             "h5 (self-contained subset).")

    # --- Subsetting ---
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-srcwin", "--srcwin", nargs=4, type=int,
                       metavar=("XOFF", "YOFF", "XSIZE", "YSIZE"),
                       help="Pixel subset window on the SME2 grid.")
    group.add_argument("-projwin", "--projwin", nargs=4, type=float,
                       metavar=("ULX", "ULY", "LRX", "LRY"),
                       help="Geographic subset window. Native/target CRS unless "
                            "-projwin_srs is given.")
    parser.add_argument("-projwin_srs", "--projwin_srs", type=str,
                        help="CRS of -projwin coordinates (e.g. EPSG:4326 or 4326).")

    # --- Reprojection / resampling ---
    parser.add_argument("-t_srs", "--target_srs", type=str, default=None,
                        help="Target CRS for output (e.g. EPSG:4326 or 4326). "
                             "Default: native EASE-Grid.")
    parser.add_argument("-tr", "--target_res", type=float, nargs=2, metavar=("XRES", "YRES"),
                        default=None, help="Output pixel size in target CRS units (with -t_srs).")
    parser.add_argument("--no_tap", action="store_true",
                        help="Disable pixel-grid alignment (tap) of the reprojected origin.")
    parser.add_argument("--resample", type=str, default="bilinear",
                        help="Resampling for continuous layers on reprojection "
                             "(nearest/bilinear/cubic/...). Integer layers always use nearest.")
    parser.add_argument("-d", "--downscale", type=int, default=None,
                        help="Integer downscale factor (block reduce).")
    parser.add_argument("--no_vrt", action="store_true",
                        help="Disable the per-snapshot multi-layer VRT.")

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
                        help="Cache the whole remote granule locally first. 'y'/'yes' for a "
                             "temp dir. Off by default: a windowed read moves a small "
                             "fraction of the granule, so caching costs more than it saves "
                             "unless several groups are converted from the same file.")
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

    if not args.list_grids:
        print(f"Starting SME2 processing: {len(urls)} file(s).")
        print(f"Format: {args.output_format} | Layer group: {args.layer_group} | "
              f"Downscale: {args.downscale}")

    result = sme2_tools.process_sme2_task(
        h5_url=urls, variable_names=args.vars, output_path=args.output,
        srcwin=tuple(args.srcwin) if args.srcwin else None,
        projwin=tuple(args.projwin) if args.projwin else None,
        projwin_srs=args.projwin_srs, layer_group=args.layer_group,
        algorithm=args.algorithm, frequency=args.freq,
        output_format=args.output_format, vrt=(not args.no_vrt),
        downscale_factor=args.downscale,
        target_align_pixels=(not args.no_tap),
        input_auth=input_auth, output_auth=output_auth,
        list_grids=args.list_grids, verbose=args.verbose,
        cache=args.cache, keep=args.keep_cached,
        target_srs=args.target_srs, target_res=args.target_res,
        resample=args.resample, num_threads=args.warp_threads,
        read_threads=args.read_threads, groups=args.groups)
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
    banner("seppo_nisar_sme2_convert")
    _main(sys.argv)


if __name__ == "__main__":
    main()
