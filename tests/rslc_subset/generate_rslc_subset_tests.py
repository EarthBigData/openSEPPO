#!/usr/bin/env python3
"""
Generate RSLC subset corner test scripts for a given location.

Searches for ascending and descending RSLC scenes, computes 5 subset
bounding boxes per scene (center + 4 corners at the actual frame polygon
vertices), generates GeoJSON for visualization, subsetting commands,
and RTC processing commands.

Usage:
    python generate_rslc_subset_tests.py --lon -88.63 --lat 34.94 \
        --name mississippi --size 15 --s3prefix test/openSEPPO/cornertest

    python generate_rslc_subset_tests.py --lon -121.76 --lat 46.85 \
        --name rainier --size 15 --s3prefix test/openSEPPO/rainier_test \
        --max_height 4500
"""

import argparse
import json
import io
import os
import sys
import numpy as np
from contextlib import redirect_stdout
from shapely.geometry import shape, box


def get_frame_polygon(direction, lon, lat, start_date="2026-01-01",
                       end_date="2026-04-01"):
    """Search for one RSLC scene and return (url, polygon)."""
    from openseppo.cli.nisar_search import _main as search_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        search_main([
            "prog", "--product", "RSLC",
            "--point", str(lon), str(lat),
            "--direction", direction,
            "--start_time_after", start_date,
            "--start_time_before", end_date,
            "--https", "--limit", "1",
            "--format", "geojson",
        ])
    raw = buf.getvalue().strip()
    if not raw:
        return None, None, None
    gj = json.loads(raw)
    if not gj["features"]:
        return None, None, None
    feat = gj["features"][0]
    poly = shape(feat["geometry"])
    url = feat["properties"].get("url", "")
    # Also get URL from plain search
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        search_main([
            "prog", "--product", "RSLC",
            "--point", str(lon), str(lat),
            "--direction", direction,
            "--start_time_after", start_date,
            "--start_time_before", end_date,
            "--https", "--limit", "1",
        ])
    url = buf2.getvalue().strip().split("\n")[-1].strip()
    return url, poly, feat


def compute_subsets(poly, size_km=15, inward_km=11, target_lon=None,
                    target_lat=None):
    """Compute target + center + 4 corner subset boxes from the frame polygon."""
    coords = list(poly.exterior.coords)[:-1]
    centroid = poly.centroid

    # Half-sizes in degrees (~approximate)
    half_lat = size_km / 2 / 111.0
    half_lon = size_km / 2 / (111.0 * np.cos(np.radians(centroid.y)))

    # Find polygon corners
    nw = coords[np.argmax([lat - lon for lon, lat in coords])]
    ne = coords[np.argmax([lat + lon for lon, lat in coords])]
    se = coords[np.argmin([lat - lon for lon, lat in coords])]
    sw = coords[np.argmin([lat + lon for lon, lat in coords])]

    # Shift corners inward toward centroid
    inward = inward_km / 111.0  # approximate degrees

    def shift_inward(corner, centroid, dist):
        dx = centroid.x - corner[0]
        dy = centroid.y - corner[1]
        r = np.sqrt(dx**2 + dy**2)
        return (corner[0] + dx / r * dist, corner[1] + dy / r * dist)

    positions = {}
    if target_lon is not None and target_lat is not None:
        positions["target"] = (target_lon, target_lat)
    positions.update({
        "center": (centroid.x, centroid.y),
        "topleft": shift_inward(nw, centroid, inward),
        "topright": shift_inward(ne, centroid, inward),
        "bottomleft": shift_inward(sw, centroid, inward),
        "bottomright": shift_inward(se, centroid, inward),
    })

    subsets = {}
    for pos, (clon, clat) in positions.items():
        w = round(clon - half_lon, 5)
        s = round(clat - half_lat, 5)
        e = round(clon + half_lon, 5)
        n = round(clat + half_lat, 5)
        sub_box = box(w, s, e, n)
        inside_pct = poly.intersection(sub_box).area / sub_box.area * 100
        subsets[pos] = {"w": w, "s": s, "e": e, "n": n, "pct": inside_pct}

    return subsets


def generate_geojson(outdir, target_lon, target_lat, frames, all_subsets):
    """Write GeoJSON with frame footprints, subset boxes, and target point."""
    features = []

    for label, (url, poly, feat) in frames.items():
        if feat:
            feat["properties"]["type"] = "frame"
            feat["properties"]["orbit"] = label
            features.append(feat)

    for label, subsets in all_subsets.items():
        for pos, box_coords in subsets.items():
            w, s, e, n = box_coords["w"], box_coords["s"], box_coords["e"], box_coords["n"]
            features.append({
                "type": "Feature",
                "properties": {
                    "name": f"{label}_{pos}",
                    "type": "subset",
                    "inside_pct": round(box_coords["pct"]),
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
                },
            })

    features.append({
        "type": "Feature",
        "properties": {"name": "target", "type": "point"},
        "geometry": {"type": "Point", "coordinates": [target_lon, target_lat]},
    })

    path = os.path.join(outdir, "subset_boxes.geojson")
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, indent=2)
    print(f"GeoJSON: {path}")


def generate_subset_script(outdir, name, s3prefix, bucket, frames,
                            all_subsets, max_height_flag):
    """Write the subsetting shell script."""
    path = os.path.join(outdir, "run_subsets.sh")
    with open(path, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(f"# RSLC Corner Subset Test: {name}\n")
        f.write(f"# Auto-generated by generate_rslc_subset_tests.py\n")
        f.write(f"#\n")
        f.write(f"# Usage:\n")
        f.write(f"#   bash run_subsets.sh                           # output to S3\n")
        f.write(f"#   OUTDIR=/tmp/{name}_output bash run_subsets.sh  # output to local\n")
        f.write(f"set -e\n\n")
        f.write(f'BUCKET="${{BUCKET:-{bucket}}}"\n')
        f.write(f'S3PREFIX="${{S3PREFIX:-{s3prefix}}}"\n')
        f.write(f'OUTDIR="${{OUTDIR:-s3://$BUCKET/$S3PREFIX}}"\n\n')

        for label in ("asc", "desc"):
            url, poly, feat = frames.get(label.upper(), (None, None, None))
            if url is None:
                continue
            f.write(f'# === {label.upper()} ===\n')
            f.write(f'{label.upper()}="{url}"\n\n')

            for pos, bc in all_subsets.get(label.upper(), {}).items():
                tag = f"{label}_{pos}"
                pct = bc["pct"]
                f.write(f'echo "=== {tag} ({pct:.0f}% inside) ==="\n')
                f.write(f'seppo_nisar_rslc_convert -i "${{{label.upper()}}}" '
                        f'-o "${{OUTDIR}}/{tag}/" \\\n')
                f.write(f'    -projwin {bc["w"]} {bc["n"]} {bc["e"]} {bc["s"]} '
                        f'-vars HH -ql -v{max_height_flag}\n\n')

        f.write('echo "=== ALL SUBSETS DONE ==="\n')

    os.chmod(path, 0o755)
    print(f"Subset script: {path}")


def generate_rtc_script(outdir, name, s3prefix, bucket, frames, all_subsets):
    """Write the RTC processing shell script."""
    path = os.path.join(outdir, "run_rtc.sh")
    with open(path, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(f"# RTC Processing for: {name}\n")
        f.write(f"# Auto-generated by generate_rslc_subset_tests.py\n")
        f.write(f"#\n")
        f.write(f"# Requires: SEPPO's SARISCE module (seppo_sar_rtc_isce.py)\n")
        f.write(f"#   https://earthbigdata.com/seppo\n")
        f.write(f"#\n")
        f.write(f"# Usage:\n")
        f.write(f"#   bash run_rtc.sh\n")
        f.write(f"#   BUCKET=mybucket S3PREFIX=my/prefix bash run_rtc.sh\n")
        f.write(f"set -e\n\n")
        f.write(f'BUCKET="${{BUCKET:-{bucket}}}"\n')
        f.write(f'S3PREFIX="${{S3PREFIX:-{s3prefix}}}"\n')
        f.write(f'S3OUT="s3://$BUCKET/$S3PREFIX"\n\n')

        all_tags = []
        for label in ("asc", "desc"):
            if label.upper() not in all_subsets:
                continue
            for pos in all_subsets[label.upper()]:
                all_tags.append(f"{label}_{pos}")

        f.write(f'for subset in {" ".join(all_tags)}; do\n')
        f.write(f'    echo ""\n')
        f.write(f'    echo "=== RTC: ${{subset}} ==="\n')
        f.write(f'    H5=$(aws s3 ls "${{S3OUT}}/${{subset}}/" | grep "\\.h5$" | awk \'{{print $4}}\' | head -1)\n')
        f.write(f'    if [ -z "$H5" ]; then echo "SKIP: no h5"; continue; fi\n')
        f.write(f'    seppo_sar_rtc_isce.py -r -sensor NISAR -nd -na -pol hh -freq A \\\n')
        f.write(f'        -bucket "$BUCKET" -prefix "${{S3PREFIX}}/${{subset}}_rtc/" \\\n')
        f.write(f'        -i "${{S3OUT}}/${{subset}}/${{H5}}"\n')
        f.write(f'done\n\n')
        f.write(f'echo "=== ALL RTC DONE ==="\n')

    os.chmod(path, 0o755)
    print(f"RTC script: {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate RSLC subset corner test scripts")
    parser.add_argument("--lon", type=float, required=True, help="Target longitude")
    parser.add_argument("--lat", type=float, required=True, help="Target latitude")
    parser.add_argument("--name", type=str, required=True, help="Test name (for filenames)")
    parser.add_argument("--size", type=float, default=15, help="Subset size in km (default: 15)")
    parser.add_argument("--s3prefix", type=str, default="test/openSEPPO/cornertest",
                        help="S3 prefix for output")
    parser.add_argument("--bucket", type=str, default="seppo1-data", help="S3 bucket")
    parser.add_argument("--outdir", type=str, default=None,
                        help="Output directory (default: /tmp/{name}_cornertest)")
    parser.add_argument("--max_height", type=float, default=None,
                        help="Max terrain height for --max_height flag")
    parser.add_argument("--start_date", type=str, default="2026-01-01")
    parser.add_argument("--end_date", type=str, default="2026-04-01")
    args = parser.parse_args()

    outdir = args.outdir or f"/tmp/{args.name}_cornertest"
    os.makedirs(outdir, exist_ok=True)

    max_height_flag = f" --max_height {args.max_height:.0f}" if args.max_height else ""

    print(f"Generating corner test for '{args.name}' at ({args.lon}, {args.lat})")
    print(f"Output: {outdir}\n")

    # Search for scenes
    frames = {}
    all_subsets = {}
    for label, direction in [("ASC", "A"), ("DESC", "D")]:
        print(f"Searching {label}...")
        url, poly, feat = get_frame_polygon(direction, args.lon, args.lat,
                                             args.start_date, args.end_date)
        if url and poly:
            frames[label] = (url, poly, feat)
            subsets = compute_subsets(poly, size_km=args.size,
                                        target_lon=args.lon, target_lat=args.lat)
            all_subsets[label] = subsets
            print(f"  URL: {os.path.basename(url)[:80]}")
            for pos, bc in subsets.items():
                print(f"  {pos}: {bc['pct']:.0f}% inside")
        else:
            print(f"  No scene found")
        print()

    # Generate outputs
    generate_geojson(outdir, args.lon, args.lat, frames, all_subsets)
    generate_subset_script(outdir, args.name, args.s3prefix, args.bucket,
                           frames, all_subsets, max_height_flag)
    generate_rtc_script(outdir, args.name, args.s3prefix, args.bucket,
                        frames, all_subsets)

    print(f"\nDone. Files in {outdir}/")
    print(f"  1. View: open subset_boxes.geojson in QGIS/geojson.io")
    print(f"  2. Subset: bash run_subsets.sh")
    print(f"  3. RTC: bash run_rtc.sh")


if __name__ == "__main__":
    main()
