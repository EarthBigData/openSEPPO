#!/usr/bin/env python
"""
seppo_nisar_coherence -- interferometric coherence from NISAR GSLC complex SLC data
*************************************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Compute pairwise interferometric coherence from co-registered NISAR GSLC
complex SLC files produced by seppo_nisar_gslc_convert -cslc.

  gamma = |<z1*conj(z2)>| / sqrt(<|z1|^2> * <|z2|^2>)

Spatial averaging uses a uniform boxcar window (default 5x5 pixels).
Outputs are uint8 DN (DN=round(coh*200), nodata=255) or float32 COGs/GeoTIFFs.

Inputs must be on the same spatial grid (same track, frame, frequency band).
NISAR GSLC products from the same track/frame are already co-registered and
geocoded to a common grid -- no additional registration is needed.

Usage examples:

1. Sequential pairs from a list of -cslc TIFs:
    seppo_nisar_coherence -i a_hh.tif b_hh.tif c_hh.tif -o out/

2. All pairs from a multi-band CSLC time-series VRT:
    seppo_nisar_coherence -i ts_HH_cslc.vrt -o out/ -pairs all

3. Custom window (3 rows x 9 cols, typical for GSLC at 20 m):
    seppo_nisar_coherence -i a.tif b.tif -o out/ -window 3 9

4. Plain GeoTIFF output to S3:
    seppo_nisar_coherence -i a.tif b.tif -o s3://bucket/coh/ -of GTiff

5. Verbose sequential pairs with all CPUs:
    seppo_nisar_coherence -i *.tif -o out/ -v

6. Crop + downscale 2x after coherence estimation:
    seppo_nisar_coherence -i *.tif -o out/ -projwin 847242 2570282 892239 2527678 -d 2

7. Reproject to geographic (EPSG:4326) at 0.0002 deg resolution:
    seppo_nisar_coherence -i *.tif -o out/ -t_srs EPSG:4326 -tr 0.0002

8. Anisotropic downscale (2x cols, 4x rows):
    seppo_nisar_coherence -i *.tif -o out/ -d 2 4
"""

import sys
import os
import time
import argparse
import shlex
from pprint import pprint

import openseppo.nisar.nisar_tools_coherence as nisar_tools_coherence


# -- seppo_parse_args shim ---------------------------------------------------

def seppo_parse_args(parser, a):
    return parser.parse_args(a[1:])


# ---------------------------------------------------------------------------


def myargsparse(a):
    """Parse command-line arguments for the NISAR coherence tool."""

    class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter,
                          argparse.RawDescriptionHelpFormatter):
        pass

    if isinstance(a, str):
        a = shlex.split(a)

    thisProg = os.path.basename(a[0])
    description = (
        "Compute interferometric coherence from co-registered NISAR GSLC "
        "complex SLC files.\n"
        "Inputs must be complex64 (CFloat32) GeoTIFFs from "
        "seppo_nisar_gslc_convert -cslc,\n"
        "or a multi-band VRT/TIF where each band is one acquisition."
    )

    parser = argparse.ArgumentParser(
        prog=thisProg, description=description,
        formatter_class=CustomFormatter,
    )

    # --- I/O ---
    parser.add_argument(
        "-i", "--input", nargs="+", required=True,
        help="Input complex64 GeoTIFF file(s) or a single multi-band VRT/TIF.  "
             "Multiple TIFs: band 1 of each file is one acquisition.  "
             "Single VRT/TIF: each band is one acquisition, band description "
             "used as date label.  Paths may be local or s3://.",
    )
    parser.add_argument(
        "-o", "--output", type=str, required=True,
        help="Output directory for coherence maps (local path or s3:// URI).",
    )

    # --- Coherence options ---
    parser.add_argument(
        "-window", "--window", nargs="+", type=int, default=[5, 5],
        metavar="N",
        help="Coherence estimation window: one integer (square) or two integers "
             "(rows cols, rows=range/azimuth depends on image orientation).  "
             "Default: 5 5.  Larger windows reduce noise but smooth edges.",
    )
    parser.add_argument(
        "-pairs", "--pairs", type=str, default="sequential",
        choices=["sequential", "all"],
        help="Pairing strategy.  "
             "sequential: pair acquisitions[i] with acquisitions[i+1] (N-1 pairs, default).  "
             "all: every unique pair i<j (N*(N-1)/2 pairs).",
    )

    # --- Output format / dtype ---
    parser.add_argument(
        "-of", "--output_format", type=str, default="COG",
        choices=["COG", "GTiff"],
        help="Output raster format.  Default: COG.",
    )
    parser.add_argument(
        "-no_DN", "--no_DN_8bit", action="store_true", dest="no_dn",
        help="Write coherence as float32 in [0, 1] with nodata=NaN instead of "
             "the default uint8 DN encoding (DN = round(coh * 200), nodata=255).",
    )

    # --- Post-processing: crop / downscale / reproject ---
    parser.add_argument(
        "-projwin", "--projwin", nargs=4, type=float, metavar=("ULX", "ULY", "LRX", "LRY"),
        help="Crop output to this bounding box (applied after coherence estimation).  "
             "ulx uly lrx lry.  Coordinates are in the native CRS unless -projwin_srs is given.",
    )
    parser.add_argument(
        "-projwin_srs", "--projwin_srs", type=str,
        help="CRS of the -projwin coordinates (e.g. EPSG:4326).  If omitted, "
             "-projwin is assumed to be in the native raster CRS.",
    )
    parser.add_argument(
        "-d", "--downscale", nargs="+", type=int, metavar="N",
        help="Block-average downscale factor applied after crop.  One integer "
             "(isotropic) or two integers X Y (columns rows).",
    )
    parser.add_argument(
        "-t_srs", "--t_srs", type=str, dest="t_srs",
        help="Output CRS for reprojection (EPSG:XXXX, WKT, or PROJ string).  "
             "Coherence is resampled with bilinear interpolation.",
    )
    parser.add_argument(
        "-tr", "--tr", nargs="+", type=float, metavar="RES",
        help="Output pixel size in target CRS map units.  One value (square) or "
             "two values X Y.  Can be combined with -t_srs.",
    )

    # --- Auth ---
    parser.add_argument(
        "--profile", type=str,
        help="AWS profile for both input and output.",
    )
    parser.add_argument(
        "--input_profile", type=str,
        help="AWS profile for reading input files (overrides --profile).",
    )
    parser.add_argument(
        "--output_profile", type=str,
        help="AWS profile for writing output files (overrides --profile).",
    )

    # --- VRT ---
    parser.add_argument(
        "-no_vrt", "--no_vrt", action="store_true", dest="no_vrt",
        help="Disable building a time-series VRT stacking all coherence pairs.",
    )

    # --- Misc ---
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose output.",
    )

    args = seppo_parse_args(parser, a)

    # Normalise window to exactly 2 elements
    if len(args.window) == 1:
        args.window = [args.window[0], args.window[0]]
    elif len(args.window) != 2:
        parser.error("-window accepts 1 integer (square) or 2 integers (rows cols).")
    for v in args.window:
        if v < 1:
            parser.error("-window values must be positive integers.")

    # Normalise -d to int (isotropic) or (factor_y, factor_x) tuple
    if args.downscale is not None:
        if len(args.downscale) == 1:
            args.downscale = args.downscale[0]
        elif len(args.downscale) == 2:
            # CLI gives X Y (cols, rows); internally we need (factor_y, factor_x)
            args.downscale = (args.downscale[1], args.downscale[0])
        else:
            parser.error("-d accepts 1 integer (isotropic) or 2 integers X Y.")

    # Normalise -tr to float (square) or (xres, yres) tuple
    if args.tr is not None:
        if len(args.tr) == 1:
            args.tr = args.tr[0]
        elif len(args.tr) == 2:
            args.tr = tuple(args.tr)
        else:
            parser.error("-tr accepts 1 value (square) or 2 values X Y.")

    if args.verbose:
        pprint(vars(args))

    return args


def get_auth_dict(profile_arg):
    """Build auth dict from a profile name or AWS environment variables."""
    auth = {}
    if profile_arg:
        auth["profile"] = profile_arg
        return auth
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        auth["key"]    = os.environ["AWS_ACCESS_KEY_ID"]
        auth["secret"] = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        if os.environ.get("AWS_SESSION_TOKEN"):
            auth["token"] = os.environ["AWS_SESSION_TOKEN"]
    return auth


def processing(args):
    input_profile  = args.input_profile  or args.profile
    output_profile = args.output_profile or args.profile
    input_auth  = get_auth_dict(input_profile)
    output_auth = get_auth_dict(output_profile)

    n_inputs = len(args.input)
    win_r, win_c = args.window
    dtype_label = "float32" if args.no_dn else "uint8-DN"
    print(
        f"NISAR Coherence  |  inputs={n_inputs}  |  "
        f"window={win_r}x{win_c}  |  pairs={args.pairs}  |  "
        f"format={args.output_format}  |  dtype={dtype_label}"
    )
    if args.projwin:
        print(f"  projwin: {args.projwin}")
    if args.downscale:
        _d = args.downscale
        d_str = (f"{_d[1]}x{_d[0]}" if isinstance(_d, tuple) else str(_d))
        print(f"  downscale: {d_str}")
    if args.t_srs:
        print(f"  t_srs: {args.t_srs}")
    if args.tr:
        print(f"  tr: {args.tr}")

    output_fs = None
    if args.output.startswith("s3://"):
        from openseppo.nisar.nisar_tools import create_s3_fs
        output_fs = create_s3_fs(get_auth_dict(args.output_profile or args.profile))

    results, msg = nisar_tools_coherence.process_coherence_pairs(
        input_paths=args.input,
        output_dir=args.output,
        window_rows=win_r,
        window_cols=win_c,
        pairs=args.pairs,
        output_format=args.output_format,
        float32=args.no_dn,
        input_auth=input_auth,
        output_auth=output_auth,
        num_threads=None,
        projwin=args.projwin,
        projwin_srs=args.projwin_srs,
        downscale=args.downscale,
        target_srs=args.t_srs,
        target_res=args.tr,
        verbose=args.verbose,
    )

    ok   = [r for r in results if r["success"]]
    fail = [r for r in results if not r["success"]]

    print(f"\n{msg}")

    if ok:
        print("\nWritten:")
        for r in ok:
            print(f"  {r['path']}")

    if fail:
        print(f"\nFailed ({len(fail)}):")
        for r in fail:
            print(f"  {r['label1']} x {r['label2']}: {r['error']}")

    if ok and not args.no_vrt:
        vrt_path = nisar_tools_coherence.build_coherence_vrt(
            results=results,
            output_dir=args.output,
            window_rows=win_r,
            window_cols=win_c,
            float32=args.no_dn,
            output_fs=output_fs,
            verbose=args.verbose,
        )
        if vrt_path:
            print(f"\nVRT:")
            print(f"  {vrt_path}")

    if fail:
        sys.exit(1)


def _main(a):
    args  = myargsparse(a)
    start = time.perf_counter()
    processing(args)
    end   = time.perf_counter()
    if args.verbose:
        dur = end - start
        print(f"\nRuntime: {int(dur / 60)}m {dur % 60:.2f}s")


def main():
    from openseppo import banner
    banner("seppo_nisar_coherence")
    _main(sys.argv)


if __name__ == "__main__":
    main()
