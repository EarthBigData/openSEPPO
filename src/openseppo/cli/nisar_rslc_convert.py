#!/usr/bin/env python
"""
seppo_nisar_rslc_convert -- NISAR RSLC HDF5 subsetting tool
*************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Subset NISAR L-band RSLC (Radar-coordinates Single-Look Complex) HDF5 files
directly from S3 / HTTPS sources with high efficiency.  The output is a fully
self-contained RSLC HDF5 that preserves all metadata required by isce3 for
interferometric processing and generation of higher-level products (subsetted
GSLC, GCOV, etc.).

Subsetting modes:
  * Pixel window         (-srcwin AZ_OFF RG_OFF AZ_SIZE RG_SIZE)
  * Coordinate window    (-coordwin SR_START SR_END ZD_START ZD_END)
  * Geographic bbox      (-projwin ULX ULY LRX LRY -projwin_srs EPSG:4326)

Usage examples:

1. List RSLC structure:
    seppo_nisar_rslc_convert -i file.h5 -lg

2. Subset by pixel window (azimuth/range):
    seppo_nisar_rslc_convert -i file.h5 -o out/ -srcwin 1000 2000 5000 3000

3. Subset by coordinate window (slant range in m, zero-Doppler time in s):
    seppo_nisar_rslc_convert -i file.h5 -o out/ -coordwin 850000 860000 3.5 4.0

4. Subset by geographic bounding box (lon/lat):
    seppo_nisar_rslc_convert -i file.h5 -o out/ \\
        -projwin -120.5 35.5 -119.5 34.5 -projwin_srs EPSG:4326

5. Subset from S3 with caching:
    seppo_nisar_rslc_convert -i s3://bucket/file.h5 -o out/ \\
        -srcwin 0 0 5000 5000 -cache y -v

6. Subset only HH polarisation:
    seppo_nisar_rslc_convert -i file.h5 -o out/ -vars HH -srcwin 0 0 5000 5000

7. Include all frequencies:
    seppo_nisar_rslc_convert -i file.h5 -o out/ --all_freq -coordwin 850000 860000 3.5 4.0

8. Batch processing from URL list:
    seppo_nisar_rslc_convert -i urls.txt -o out/ -srcwin 0 0 5000 5000
"""

import sys
import os
import time
import argparse
import shlex

import openseppo.nisar.nisar_tools_rslc as nisar_tools_rslc


asf_buckets = [
    "sds-n-cumulus-prod-nisar-products",
    "sds-n-cumulus-prod-nisar-ur-products",
]


def seppo_parse_args(parser, a):
    return parser.parse_args(a[1:])


def myargsparse(a):
    class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter,
                          argparse.RawDescriptionHelpFormatter):
        pass

    if isinstance(a, str):
        a = shlex.split(a)

    prog = os.path.basename(a[0])
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Subset NISAR L-band RSLC HDF5 files.  Output is a valid RSLC HDF5 "
            "for isce3 interferometric processing (GSLC, GCOV generation)."
        ),
        formatter_class=CustomFormatter,
    )

    # --- I/O ---
    parser.add_argument(
        "-i", "--h5", type=str, nargs="+",
        help="Input RSLC H5 URL(s) or path to a text file containing URLs.",
    )
    parser.add_argument(
        "-o", "--output", type=str, required=False,
        help="Output directory (S3 or local).",
    )

    # --- Variables & Frequency ---
    parser.add_argument(
        "-vars", "--vars", nargs="+", default=None,
        help="Polarisation variables to include, e.g. HH HV.  "
             "If omitted, all 2-letter uppercase variables are included.",
    )
    parser.add_argument(
        "-f", "--freq", type=str, default=None, choices=["A", "B"],
        help="Frequency band (A/B).  Defaults to A, or to B when the "
             "granule's mode says frequency A was not acquired.",
    )
    parser.add_argument(
        "--all_freq", action="store_true",
        help="Include all available frequencies (A and B) in the subset.",
    )

    # --- Inspection ---
    parser.add_argument(
        "-lg", "--list_grids", action="store_true",
        help="Scan the RSLC file and list all available swaths/variables, "
             "then exit.  Requires -i.",
    )

    # --- Subsetting modes (mutually exclusive) ---
    sub = parser.add_mutually_exclusive_group()
    sub.add_argument(
        "-srcwin", "--srcwin", nargs=4, type=int,
        metavar=("AZ_OFF", "RG_OFF", "AZ_SIZE", "RG_SIZE"),
        help="Pixel subset window (azimuth offset, range offset, "
             "azimuth size, range size).",
    )
    sub.add_argument(
        "-coordwin", "--coordwin", nargs=4, type=float,
        metavar=("SR_START", "SR_END", "ZD_START", "ZD_END"),
        help="Coordinate subset window (slant range start/end in metres, "
             "zero-Doppler time start/end in seconds since orbit epoch).",
    )
    sub.add_argument(
        "-projwin", "--projwin", nargs=4, type=float,
        metavar=("ULX", "ULY", "LRX", "LRY"),
        help="Geographic bounding box.  Coordinates are in the CRS given "
             "by -projwin_srs (default: EPSG:4326 lon/lat degrees).",
    )
    parser.add_argument(
        "-projwin_srs", "--projwin_srs", type=str, default="EPSG:4326",
        help="CRS of the -projwin coordinates.  Default: EPSG:4326.",
    )

    # --- Terrain height for projwin ---
    parser.add_argument(
        "--max_height", type=float, default=None, metavar="M",
        help="Maximum terrain height (metres) for -projwin coordinate "
             "conversion and bounding polygon metadata.  Higher terrain "
             "shifts ground position toward near range.  If omitted, "
             "auto-detected from USGS Elevation API (falls back to 1000m "
             "if unavailable).  Set to 0 for flat terrain, 4000+ for "
             "volcanoes/mountains.",
    )
    parser.add_argument(
        "--min_height", type=float, default=None, metavar="M",
        help="Minimum terrain height (metres) for bounding polygon "
             "metadata.  Lower terrain shifts far-range ground positions "
             "further out.  If omitted, auto-detected from USGS Elevation "
             "API (falls back to 0m if unavailable).",
    )

    parser.add_argument(
        "--read_threads", type=int, default=8, metavar="N",
        help="Parallel readers for the SLC payload.  Each is a subprocess "
             "with its own HDF5 state, reading one chunk-aligned azimuth "
             "stripe, so several range requests are in flight at once "
             "(h5py's global lock makes threads useless here).  1 disables "
             "it.  Default: 8.",
    )

    parser.add_argument(
        "--complevel", type=int, default=None, choices=range(0, 10),
        metavar="0-9",
        help="gzip level for the SLC payload and the grid-borne masks.  "
             "Omitted, the source's own setting is mirrored (gzip/4 for NISAR "
             "RSLC).  Compression is single-process and dominates a subset: "
             "on a 326 MB payload slice gzip/4 took 6.7 s for 154.0 MB "
             "against gzip/1 at 4.1 s for 155.4 MB, i.e. 39%% less time for "
             "0.9%% more file.  0 stores uncompressed.  Values are identical "
             "either way -- only the container's packing changes.",
    )

    # --- Authentication ---
    parser.add_argument("--profile", type=str,
                        help="AWS profile name (input and output).")
    parser.add_argument("--input_profile", type=str,
                        help="AWS profile for reading input H5s.")
    parser.add_argument("--output_profile", type=str,
                        help="AWS profile for writing output H5s.")

    # --- Caching ---
    parser.add_argument(
        "-cache", "--cache", default=None,
        help="Local directory (or 'y') to cache remote H5 files before "
             "reading.  Auto-enabled for remote full-frame reads.",
    )
    parser.add_argument(
        "-keep", "--keep_cached", action="store_true",
        help="Retain cached H5 files after processing.",
    )

    # --- Quicklook ---
    parser.add_argument(
        "-ql", "--quicklook", action="store_true",
        help="Generate a backscatter quicklook PNG from the subset.  "
             "Shows detected sigma0 (single-look + multilooked) for each "
             "polarisation.",
    )
    parser.add_argument(
        "--ql_multilook", type=int, default=5, metavar="N",
        help="Multilook window size for quicklook (default: 5).",
    )

    # --- Misc ---
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose output.",
    )

    args = seppo_parse_args(parser, a)
    args.use_earthdata = False

    if not args.list_grids and not args.output:
        parser.error("-o/--output is required unless -lg/--list_grids is used.")
    if not args.h5:
        parser.error("-i/--h5 is required.")

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
        auth["key"] = os.environ["AWS_ACCESS_KEY_ID"]
        auth["secret"] = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        tok = os.environ.get("AWS_SESSION_TOKEN")
        if tok:
            auth["token"] = tok
    return auth


def processing(args):
    output_profile = args.output_profile or args.profile
    output_auth = get_auth_dict(output_profile, use_earthdata=False)

    # Check S3 write access early before any processing
    if args.output and args.output.startswith("s3://"):
        from openseppo.nisar.nisar_tools import check_s3_write_access
        check_s3_write_access(args.output, output_auth)

    # --- Parse input URLs ---
    def _is_url(s):
        return s.startswith("s3://") or s.startswith("https://") or os.path.isfile(s)

    urls = []
    for item in args.h5:
        if os.path.isfile(item) and not item.endswith(".h5"):
            with open(item) as fh:
                urls.extend(l.strip() for l in fh if _is_url(l.strip()))
        elif item.startswith("s3://") and not item.endswith(".h5"):
            import fsspec
            with fsspec.open(item, "r") as fh:
                urls.extend(l.strip() for l in fh if _is_url(l.strip()))
        else:
            urls.append(item)

    if not urls:
        print("Error: no valid input URLs found.")
        sys.exit(1)

    # --- Single-mode stacks, and the frequency default ---
    # An unset -f follows the granule: A normally, B when the mode says
    # frequency A was not acquired.
    from openseppo.nisar.nisar_tools import (
        check_uniform_mode, frequency_from_pol_code)
    try:
        _mode, _pol = check_uniform_mode(urls)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.freq is None:
        _only = frequency_from_pol_code(_pol)
        if _only:
            print(f"---> Mode {_mode}_{_pol}: only frequency {_only} acquired; using -f {_only}.")
        args.freq = _only or "A"

    # --- Auto-detect earthdata ---
    if urls[0].startswith("s3://"):
        bucket = urls[0].split("/")[2]
        if bucket in asf_buckets and not args.use_earthdata:
            print("---> Detected ASF DAAC Bucket. Using Earthdata credentials.")
            args.use_earthdata = True
    elif urls[0].startswith("https://") and not args.use_earthdata:
        ea_hosts = ["earthdatacloud.nasa.gov", "urs.earthdata.nasa.gov",
                     "e4ftl01.cr.usgs.gov"]
        if any(h in urls[0] for h in ea_hosts):
            print("---> Detected Earthdata HTTPS URL. Using Earthdata credentials.")
            args.use_earthdata = True

    input_profile = args.input_profile or args.profile
    input_auth = get_auth_dict(input_profile, args.use_earthdata)

    if not args.list_grids:
        print(f"Starting RSLC Subset Processing: {len(urls)} file(s).")
        if args.srcwin:
            print(f"Mode: srcwin {args.srcwin}")
        elif args.coordwin:
            print(f"Mode: coordwin {args.coordwin}")
        elif args.projwin:
            print(f"Mode: projwin {args.projwin} ({args.projwin_srs})")
        else:
            print("Mode: full frame copy")
        print(f"Freq: {args.freq} | All frequencies: {args.all_freq}")

    try:
        result = nisar_tools_rslc.process_rslc_subset(
            h5_url=urls,
            variable_names=args.vars,
            output_path=args.output if args.output else ".",
            srcwin=tuple(args.srcwin) if args.srcwin else None,
            coordwin=tuple(args.coordwin) if args.coordwin else None,
            projwin=tuple(args.projwin) if args.projwin else None,
            projwin_srs=args.projwin_srs if args.projwin else None,
            frequency=args.freq,
            input_auth=input_auth,
            output_auth=output_auth,
            list_grids=args.list_grids,
            cache=args.cache,
            keep=args.keep_cached,
            verbose=args.verbose,
            all_frequencies=args.all_freq,
            quicklook=args.quicklook,
            ql_multilook=args.ql_multilook,
            max_height=args.max_height,
            min_height=args.min_height,
            read_workers=args.read_threads,
            complevel=args.complevel,
        )
        print(f"\n{result}")

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
        d = end - start
        print(f"\nRuntime: {int(d/60)}m {d%60:.2f}s\n")


def main():
    from openseppo import banner
    banner("seppo_nisar_rslc_convert")
    _main(sys.argv)


if __name__ == "__main__":
    main()
