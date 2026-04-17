#!/usr/bin/env python3
"""
Generate NISAR subset test scripts for a given location.

Searches for ascending and descending RSLC scenes (or reads URLs from a file),
computes 6 subset bounding boxes per scene (target + center + 4 corners at the
actual frame polygon vertices), generates GeoJSON for visualization, subsetting
commands for RSLC (and optionally GSLC/GCOV power COGs), and RTC processing
commands.

Usage:
    # Search for scenes at a location
    python generate_rslc_subset_tests.py --lon -88.63 --lat 34.94 \
        --name mississippi --size 15 --s3prefix test/openSEPPO/cornertest

    # With max_height and larger subsets
    python generate_rslc_subset_tests.py --lon -121.76 --lat 46.85 \
        --name rainier --size 20 --s3prefix test/openSEPPO/rainier_test \
        --max_height 4500

    # Use a URL file, include GSLC/GCOV power COGs for comparison
    python generate_rslc_subset_tests.py --lon -121.76 --lat 46.85 \
        --name rainier --size 20 --urls rslc_urls.txt --gcov --gslc

URL file format (one URL per line, optional ASC/DESC label):
    ASC https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5
    DESC https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5
"""

import argparse
import json
import io
import os
import re
import sys
import numpy as np
from contextlib import redirect_stdout
from shapely.geometry import shape, box


def rslc_url_to_product(url, product):
    """Derive GSLC or GCOV URL from an RSLC URL by replacing L1->L2, RSLC->product.
    Handles both filename (NISAR_L1_PR_RSLC_...) and SDS directory (L1_L_RSLC/)."""
    return url.replace("L1_L_RSLC", f"L2_L_{product}").replace("_L1_", "_L2_").replace("RSLC", product)


def search_product_url(product, lon, lat, direction, start_date, end_date):
    """Search for a GSLC/GCOV URL via nisar_search. Returns URL or None."""
    from openseppo.cli.nisar_search import _main as search_main
    buf = io.StringIO()
    with redirect_stdout(buf):
        search_main([
            "prog", "--product", product,
            "--point", str(lon), str(lat),
            "--direction", direction,
            "--start_time_after", start_date,
            "--start_time_before", end_date,
            "--https", "--limit", "1",
        ])
    raw = buf.getvalue().strip()
    if raw:
        return raw.split("\n")[-1].strip()
    return None


def resolve_product_url(rslc_url, product, lon, lat):
    """Get GSLC/GCOV URL by replacing L1->L2, RSLC->product in the RSLC URL.

    For S3 URLs, verifies the file exists with ``aws s3 ls``.
    For HTTPS URLs, verifies with a HEAD request.
    Falls back to nisar_search if verification fails.
    """
    import subprocess
    derived = rslc_url_to_product(rslc_url, product)

    # Verify the derived URL exists
    if derived.startswith("s3://"):
        try:
            ret = subprocess.run(
                ["aws", "s3", "ls", derived],
                capture_output=True, text=True, timeout=15)
            if ret.returncode == 0 and ret.stdout.strip():
                return derived
        except Exception:
            pass
    else:
        try:
            import requests
            r = requests.head(derived, timeout=10, allow_redirects=True)
            if r.status_code < 400:
                return derived
        except Exception:
            pass

    # Fall back to search
    m = re.search(r'_([AD])_\d{3}_', rslc_url)
    direction = m.group(1) if m else "A"
    dm = re.search(r'_(\d{4})(\d{2})(\d{2})T', rslc_url)
    if dm:
        from datetime import datetime, timedelta
        dt = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        start = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        end = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start, end = "2024-01-01", "2027-01-01"

    searched = search_product_url(product, lon, lat, direction, start, end)
    return searched or derived  # use derived as last resort


def read_url_file(path):
    """Read RSLC URLs from a file.

    Format: one URL per line, optionally prefixed with ASC/DESC label.
    Direction is inferred from _A_/_D_ in the URL if no label given.
    """
    urls = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                label, url = parts[0].upper(), parts[1]
                if label in ("ASC", "A"):
                    urls["ASC"] = url
                elif label in ("DESC", "D"):
                    urls["DESC"] = url
            elif len(parts) == 1:
                url = parts[0]
                if "_A_" in url and "ASC" not in urls:
                    urls["ASC"] = url
                elif "_D_" in url and "DESC" not in urls:
                    urls["DESC"] = url
    return urls


def get_frame_polygon_from_url(url, lon, lat):
    """Get the frame polygon by reading the boundingPolygon from the HDF5 file.

    Works for S3, HTTPS, and local file URLs.  Falls back to nisar_search
    for HTTPS URLs if direct read fails.
    """
    from shapely.geometry import Polygon as ShapelyPolygon

    # Try reading boundingPolygon directly from the file
    try:
        from openseppo.nisar.nisar_tools_rslc import open_h5_lazy, create_s3_fs
        if url.startswith("s3://"):
            fs = create_s3_fs({})
        else:
            fs = None
        f = open_h5_lazy(url, fs)
        bp_path = "science/LSAR/identification/boundingPolygon"
        val = f[bp_path][()].decode() if hasattr(f[bp_path][()], "decode") else str(f[bp_path][()])
        f.close()
        # Parse WKT (handle both comma-separated and space-only formats)
        pairs = re.findall(r'([-\d.]+)\s+([-\d.]+)', val)
        if len(pairs) >= 3:
            coords = [(float(lon_), float(lat_)) for lon_, lat_ in pairs]
            poly = ShapelyPolygon(coords)
            # Build a minimal GeoJSON feature
            feat = {
                "type": "Feature",
                "properties": {"url": url},
                "geometry": poly.__geo_interface__,
            }
            return poly, feat
    except Exception as e:
        print(f"    Warning: direct read failed ({e}), trying search...")

    # Fall back to nisar_search for HTTPS URLs
    if url.startswith("https://"):
        from openseppo.cli.nisar_search import _main as search_main
        m = re.search(r'_([AD])_\d{3}_', url)
        if not m:
            return None, None
        direction = m.group(1)
        dm = re.search(r'_(\d{4})(\d{2})(\d{2})T', url)
        if dm:
            from datetime import datetime, timedelta
            dt = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            start = (dt - timedelta(days=30)).strftime("%Y-%m-%d")
            end = (dt + timedelta(days=30)).strftime("%Y-%m-%d")
        else:
            start, end = "2024-01-01", "2027-01-01"
        buf = io.StringIO()
        with redirect_stdout(buf):
            search_main([
                "prog", "--product", "RSLC",
                "--point", str(lon), str(lat),
                "--direction", direction,
                "--start_time_after", start,
                "--start_time_before", end,
                "--https", "--limit", "1",
                "--format", "geojson",
            ])
        raw = buf.getvalue().strip()
        if raw:
            gj = json.loads(raw)
            if gj["features"]:
                feat = gj["features"][0]
                return shape(feat["geometry"]), feat

    return None, None


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
                    target_lat=None, target_only=False):
    """Compute subset boxes from the frame polygon.

    If *target_only*, returns only the target-point subset.
    Otherwise returns target + center + 4 corners.
    """
    coords = list(poly.exterior.coords)[:-1]
    centroid = poly.centroid

    # Half-sizes in degrees (~approximate)
    half_lat = size_km / 2 / 111.0
    half_lon = size_km / 2 / (111.0 * np.cos(np.radians(centroid.y)))

    positions = {}
    if target_lon is not None and target_lat is not None:
        positions["target"] = (target_lon, target_lat)

    if not target_only:
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
                            all_subsets, max_height_flag,
                            product_urls=None, rslc=True):
    """Write the subsetting shell script.

    product_urls: dict {label: {"GSLC": url, "GCOV": url}} for extra products.
    rslc: include RSLC h5 subset commands.
    """
    path = os.path.join(outdir, "run_subsets.sh")
    product_urls = product_urls or {}
    has_gslc = any("GSLC" in v for v in product_urls.values())
    has_gcov = any("GCOV" in v for v in product_urls.values())
    products = []
    if rslc:
        products.append("RSLC")
    if has_gslc:
        products.append("GSLC")
    if has_gcov:
        products.append("GCOV")

    with open(path, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(f"# NISAR Subset Test: {name}\n")
        f.write(f"# Products: {', '.join(products)}\n")
        f.write(f"# Auto-generated by generate_rslc_subset_tests.py\n")
        f.write(f"#\n")
        f.write(f"# Usage:\n")
        f.write(f"#   bash run_subsets.sh\n")
        f.write(f"set -e\n\n")
        f.write(f'OUTDIR="s3://{bucket}/{s3prefix}"\n\n')

        for label in ("asc", "desc"):
            url, poly, feat = frames.get(label.upper(), (None, None, None))
            if url is None:
                continue
            f.write(f'# === {label.upper()} ===\n')
            if rslc:
                f.write(f'{label.upper()}_RSLC="{url}"\n')
            pu = product_urls.get(label.upper(), {})
            if "GSLC" in pu:
                f.write(f'{label.upper()}_GSLC="{pu["GSLC"]}"\n')
            if "GCOV" in pu:
                f.write(f'{label.upper()}_GCOV="{pu["GCOV"]}"\n')
            f.write(f'\n')

            for pos, bc in all_subsets.get(label.upper(), {}).items():
                tag = f"{label}_{pos}"
                pct = bc["pct"]
                projwin = f'{bc["w"]} {bc["n"]} {bc["e"]} {bc["s"]}'

                f.write(f'echo "=== {tag} ({pct:.0f}% inside) ==="\n')

                # RSLC subset (h5 for isce3)
                if rslc:
                    f.write(f'seppo_nisar_rslc_convert -i "${{{label.upper()}_RSLC}}" '
                            f'-o "${{OUTDIR}}/{tag}/" \\\n')
                    f.write(f'    -projwin {projwin} '
                            f'-vars HH -ql -v{max_height_flag}\n')

                # GSLC subset (power COG)
                if "GSLC" in pu:
                    f.write(f'seppo_nisar_gslc_convert -i "${{{label.upper()}_GSLC}}" '
                            f'-o "${{OUTDIR}}/{tag}/" \\\n')
                    f.write(f'    -projwin {projwin} -projwin_srs EPSG:4326 '
                            f'-pwr -of COG -vars HH -v\n')

                # GCOV subset (power COG)
                if "GCOV" in pu:
                    f.write(f'seppo_nisar_gcov_convert -i "${{{label.upper()}_GCOV}}" '
                            f'-o "${{OUTDIR}}/{tag}/" \\\n')
                    f.write(f'    -projwin {projwin} -projwin_srs EPSG:4326 '
                            f'-pwr -of COG -vars HHHH -v\n')

                f.write(f'\n')

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
        f.write(f"set -e\n\n")
        f.write(f'BUCKET="{bucket}"\n')
        f.write(f'S3PREFIX="{s3prefix}"\n')
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


def generate_staging_script(outdir, name, s3prefix, bucket, frames,
                             all_subsets, max_height_flag,
                             product_urls=None, rslc=True, rtc=False,
                             output_profile=None):
    """Write a single run_all.sh that stages selected products
    into a structured output directory."""
    path = os.path.join(outdir, "run_all.sh")
    product_urls = product_urls or {}
    has_gslc = any("GSLC" in v for v in product_urls.values())
    has_gcov = any("GCOV" in v for v in product_urls.values())

    with open(path, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(f"# NISAR Full Staging Pipeline: {name}\n")
        f.write(f"# Auto-generated by generate_rslc_subset_tests.py\n")
        f.write(f"#\n")
        stages = []
        if rslc:
            stages.append("RSLC subset")
        if rtc:
            stages.append("RTC (isce3)")
        if has_gcov:
            stages.append("GCOV COG")
        if has_gslc:
            stages.append("GSLC COG")
        f.write(f"# Stages: {' -> '.join(stages)}\n")
        f.write(f"# Output structure:\n")
        if rslc:
            f.write(f"#   $OUTDIR/<subset>/rslc/    -- RSLC h5 subset + quicklook\n")
        if rtc:
            f.write(f"#   $OUTDIR/<subset>/rtc/     -- RTC gamma0 (isce3 via SEPPO SARISCE)\n")
        if has_gcov:
            f.write(f"#   $OUTDIR/<subset>/gcov/    -- GCOV power COG (openSEPPO)\n")
        if has_gslc:
            f.write(f"#   $OUTDIR/<subset>/gslc/    -- GSLC power COG (openSEPPO)\n")
        f.write(f"#\n")
        f.write(f"# Requires: openSEPPO, SEPPO SARISCE module (for RTC)\n")
        f.write(f"#   https://earthbigdata.com/seppo\n")
        f.write(f"#\n")
        f.write(f"# Usage:\n")
        f.write(f"#   bash run_all.sh\n")
        f.write(f"set -e\n\n")
        f.write(f'BUCKET="{bucket}"\n')
        f.write(f'S3PREFIX="{s3prefix}"\n')
        f.write(f'OUTDIR="s3://$BUCKET/$S3PREFIX"\n\n')

        # Detect if input URLs are from NISAR SDS (need credential switching)
        any_url = next((u for u, _, _ in frames.values() if u), "")
        sds_input = "nisar-ops-rs-" in any_url
        profile_flag = f" --output_profile {output_profile}" if output_profile else ""

        if sds_input:
            f.write(f"# Credential helpers for SDS input / user output buckets\n")
            f.write(f'creds_sds() {{ eval $(seppo_aws_credentials.py -a); }}\n')
            f.write(f'creds_user() {{ eval $(seppo_aws_credentials.py -u); }}\n\n')

        # Declare URLs
        for label in ("asc", "desc"):
            url, poly, feat = frames.get(label.upper(), (None, None, None))
            if url is None:
                continue
            f.write(f'# === {label.upper()} URLs ===\n')
            if rslc:
                f.write(f'{label.upper()}_RSLC="{url}"\n')
            pu = product_urls.get(label.upper(), {})
            if "GSLC" in pu:
                f.write(f'{label.upper()}_GSLC="{pu["GSLC"]}"\n')
            if "GCOV" in pu:
                f.write(f'{label.upper()}_GCOV="{pu["GCOV"]}"\n')
            f.write(f'\n')

        # Process each subset
        for label in ("asc", "desc"):
            if label.upper() not in all_subsets:
                continue
            pu = product_urls.get(label.upper(), {})

            for pos, bc in all_subsets[label.upper()].items():
                tag = f"{label}_{pos}"
                pct = bc["pct"]
                projwin = f'{bc["w"]} {bc["n"]} {bc["e"]} {bc["s"]}'

                f.write(f'echo ""\n')
                f.write(f'echo "{"=" * 60}"\n')
                f.write(f'echo "=== {tag} ({pct:.0f}% inside) ==="\n')
                f.write(f'echo "{"=" * 60}"\n\n')

                # RSLC subset (reads from SDS, writes to user bucket)
                if rslc:
                    if sds_input:
                        f.write(f'creds_sds\n')
                    f.write(f'echo "--- [{tag}] RSLC subset ---"\n')
                    f.write(f'seppo_nisar_rslc_convert -i "${{{label.upper()}_RSLC}}" '
                            f'-o "${{OUTDIR}}/{tag}/rslc/" \\\n')
                    f.write(f'    -projwin {projwin} '
                            f'-vars HH -ql -v{max_height_flag}{profile_flag}\n\n')

                # RTC from RSLC subset (reads/writes user bucket)
                if rtc:
                    if sds_input:
                        f.write(f'creds_user\n')
                    f.write(f'echo "--- [{tag}] RTC (isce3) ---"\n')
                    f.write(f'H5=$(ls "${{OUTDIR}}/{tag}/rslc/"*.h5 2>/dev/null | head -1)\n')
                    f.write(f'if [ -z "$H5" ]; then\n')
                    f.write(f'    # Try S3\n')
                    f.write(f'    H5_NAME=$(aws s3 ls "${{OUTDIR}}/{tag}/rslc/" 2>/dev/null '
                            f'| grep "\\.h5$" | awk \'{{print $4}}\' | head -1)\n')
                    f.write(f'    if [ -n "$H5_NAME" ]; then\n')
                    f.write(f'        H5="${{OUTDIR}}/{tag}/rslc/${{H5_NAME}}"\n')
                    f.write(f'    fi\n')
                    f.write(f'fi\n')
                    f.write(f'if [ -n "$H5" ]; then\n')
                    f.write(f'    seppo_sar_rtc_isce.py -r -sensor NISAR -nd -na -pol hh -freq A \\\n')
                    f.write(f'        -bucket "$BUCKET" -prefix "${{S3PREFIX}}/{tag}/rtc/" \\\n')
                    f.write(f'        -i "$H5"\n')
                    f.write(f'else\n')
                    f.write(f'    echo "SKIP RTC: no RSLC h5 found for {tag}"\n')
                    f.write(f'fi\n\n')

                # GCOV power COG (reads from SDS, writes to user bucket)
                if "GCOV" in pu:
                    if sds_input:
                        f.write(f'creds_sds\n')
                    f.write(f'echo "--- [{tag}] GCOV power COG ---"\n')
                    f.write(f'seppo_nisar_gcov_convert -i "${{{label.upper()}_GCOV}}" '
                            f'-o "${{OUTDIR}}/{tag}/gcov/" \\\n')
                    f.write(f'    -projwin {projwin} -projwin_srs EPSG:4326 '
                            f'-pwr -of COG -vars HHHH -v{profile_flag}\n\n')

                # GSLC power COG (reads from SDS, writes to user bucket)
                if "GSLC" in pu:
                    if sds_input:
                        f.write(f'creds_sds\n')
                    f.write(f'echo "--- [{tag}] GSLC power COG ---"\n')
                    f.write(f'seppo_nisar_gslc_convert -i "${{{label.upper()}_GSLC}}" '
                            f'-o "${{OUTDIR}}/{tag}/gslc/" \\\n')
                    f.write(f'    -projwin {projwin} -projwin_srs EPSG:4326 '
                            f'-pwr -of COG -vars HH -v{profile_flag}\n\n')

        f.write(f'echo ""\n')
        f.write(f'echo "{"=" * 60}"\n')
        f.write(f'echo "=== ALL STAGING DONE ==="\n')
        f.write(f'echo "Output: $OUTDIR"\n')
        f.write(f'echo "{"=" * 60}"\n')

    os.chmod(path, 0o755)
    print(f"Staging script: {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate NISAR subset test scripts (RSLC, GSLC, GCOV)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
URL file format (--urls):
  ASC https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5
  DESC https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5
Labels (ASC/DESC) are optional; direction is inferred from _A_/_D_ in the URL.
""")
    parser.add_argument("--lon", type=float, required=True, help="Target longitude")
    parser.add_argument("--lat", type=float, required=True, help="Target latitude")
    parser.add_argument("--name", type=str, required=True, help="Test name (for filenames)")
    parser.add_argument("--size", type=float, default=15, help="Subset size in km (default: 15)")
    parser.add_argument("--target_only", action="store_true",
                        help="Only generate subset at the target point (no corners, no center)")
    parser.add_argument("--urls", type=str, default=None,
                        help="File with RSLC URLs (skip scene search)")
    parser.add_argument("--rslc", action="store_true",
                        help="Generate RSLC h5 subset commands")
    parser.add_argument("--gcov", action="store_true",
                        help="Generate GCOV power COG subset commands")
    parser.add_argument("--gslc", action="store_true",
                        help="Generate GSLC power COG subset commands")
    parser.add_argument("--rtc", action="store_true",
                        help="Generate RTC processing commands (requires --rslc)")
    parser.add_argument("--staging", action="store_true",
                        help="Generate a single run_all.sh combining the "
                             "products selected via --rslc, --gcov, --gslc, --rtc")
    parser.add_argument("--output_profile", type=str, default=None,
                        help="AWS profile for writing subsets (--output_profile on "
                             "openSEPPO tools, e.g. 'josefk')")
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

    # Default: if no product flags given, enable --rslc
    if not (args.rslc or args.gcov or args.gslc):
        args.rslc = True

    if args.rtc and not args.rslc:
        parser.error("--rtc requires --rslc")

    outdir = args.outdir or f"/tmp/{args.name}_cornertest"
    os.makedirs(outdir, exist_ok=True)

    max_height_flag = f" --max_height {args.max_height:.0f}" if args.max_height else ""

    products = []
    if args.rslc:
        products.append("RSLC")
    if args.rtc:
        products.append("RTC")
    if args.gslc:
        products.append("GSLC")
    if args.gcov:
        products.append("GCOV")

    print(f"Generating corner test for '{args.name}' at ({args.lon}, {args.lat})")
    print(f"Subset size: {args.size} km")
    print(f"Products: {', '.join(products)}")
    if args.gcov or args.gslc:
        extras = []
        if args.gslc:
            extras.append("GSLC")
        if args.gcov:
            extras.append("GCOV")
        print(f"Additional products: {', '.join(extras)} (power COG)")
    print(f"Output: {outdir}\n")

    frames = {}
    all_subsets = {}

    if args.urls:
        # Read URLs from file
        url_map = read_url_file(args.urls)
        for label, url in url_map.items():
            print(f"Loading {label} from URL file...")
            poly, feat = get_frame_polygon_from_url(url, args.lon, args.lat)
            if poly:
                frames[label] = (url, poly, feat)
                subsets = compute_subsets(poly, size_km=args.size,
                                         target_lon=args.lon, target_lat=args.lat,
                                         target_only=args.target_only)
                all_subsets[label] = subsets
                print(f"  URL: {os.path.basename(url)[:80]}")
                for pos, bc in subsets.items():
                    print(f"  {pos}: {bc['pct']:.0f}% inside")
            else:
                print(f"  Could not determine frame polygon")
            print()
    else:
        # Search for scenes
        for label, direction in [("ASC", "A"), ("DESC", "D")]:
            print(f"Searching {label}...")
            url, poly, feat = get_frame_polygon(direction, args.lon, args.lat,
                                                 args.start_date, args.end_date)
            if url and poly:
                frames[label] = (url, poly, feat)
                subsets = compute_subsets(poly, size_km=args.size,
                                         target_lon=args.lon, target_lat=args.lat,
                                         target_only=args.target_only)
                all_subsets[label] = subsets
                print(f"  URL: {os.path.basename(url)[:80]}")
                for pos, bc in subsets.items():
                    print(f"  {pos}: {bc['pct']:.0f}% inside")
            else:
                print(f"  No scene found")
            print()

    if not frames:
        print("No frames found. Exiting.")
        sys.exit(1)

    # Resolve GSLC/GCOV URLs if requested
    product_urls = {}  # {label: {"GSLC": url, "GCOV": url}}
    if args.gslc or args.gcov:
        for label, (rslc_url, _, _) in frames.items():
            pu = {}
            for product in (["GSLC"] if args.gslc else []) + (["GCOV"] if args.gcov else []):
                print(f"Resolving {label} {product}...", end=" ")
                url = resolve_product_url(rslc_url, product, args.lon, args.lat)
                pu[product] = url
                print(f"{os.path.basename(url)[:70]}")
            product_urls[label] = pu
        print()

    # Generate outputs
    generate_geojson(outdir, args.lon, args.lat, frames, all_subsets)

    if args.staging:
        generate_staging_script(outdir, args.name, args.s3prefix, args.bucket,
                                frames, all_subsets, max_height_flag,
                                product_urls=product_urls,
                                rslc=args.rslc, rtc=args.rtc,
                                output_profile=args.output_profile)
    else:
        generate_subset_script(outdir, args.name, args.s3prefix, args.bucket,
                               frames, all_subsets, max_height_flag,
                               product_urls=product_urls, rslc=args.rslc)
        if args.rtc:
            generate_rtc_script(outdir, args.name, args.s3prefix, args.bucket,
                                frames, all_subsets)

    print(f"\nDone. Files in {outdir}/")
    print(f"  1. View: open subset_boxes.geojson in QGIS/geojson.io")
    if args.staging:
        print(f"  2. Run all: bash run_all.sh")
    else:
        print(f"  2. Subset: bash run_subsets.sh")
        if args.rtc:
            print(f"  3. RTC: bash run_rtc.sh")


if __name__ == "__main__":
    main()
