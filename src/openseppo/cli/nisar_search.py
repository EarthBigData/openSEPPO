#!/usr/bin/env python
"""
seppo_nisar_search -- NISAR Earthdata catalog search
****************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Author: Josef Kellndorfer
Date: 2026-02-28

Search NISAR product URLs via NASA Earthdata CMR using earthaccess.

Login is attempted from netrc (urs.earthdata.nasa.gov entry).
If no credentials are found an interactive prompt is shown automatically.

For --format url (default):
  s3:// URLs are returned by default; pass --https for https:// URLs.

For all other formats (csv, json, geojson, kml):
  Both url (s3://) and url_https columns are included in every record.

All column-based filters (track, frame, product, direction, cycle, ...) and
spatial filters (--bbox, --ullr, --wkt, --point, --geojson, ...) that are
natively supported by CMR are sent directly; remaining filters are applied in
Python after the CMR call.

By default only the latest CRID per unique scene is returned.
Pass --allcrids to include every processing version.

With --group results are organised by (track, direction, frame) and ordered
by start_time:
  stdout     section headers + URLs for each group
  --output   a directory; one file per group named:
            MISSION_PRODUCT_TRK_DIR_FRM_firstDate_lastDate_{suffix}.{ext}

Output formats: url (default), csv, json, geojson, kml

Dependencies (all conda-forge):
  earthaccess
"""

import os
import sys
import shlex
import json
import re
import argparse
import csv
import io
from datetime import datetime
from collections import defaultdict, OrderedDict

try:
    import earthaccess

    HAS_EARTHACCESS = True
except ImportError:
    HAS_EARTHACCESS = False


# -- Constants -----------------------------------------------------------------

ALL_COLUMNS = [
    "bucket",
    "mission",
    "inst_level",
    "proctype",
    "product",
    "cycle",
    "cycle2",
    "track",
    "direction",
    "frame",
    "mode",
    "polarization",
    "observation_mode",
    "start_time",
    "end_time",
    "start_time2",
    "end_time2",
    "crid",
    "accuracy",
    "coverage",
    "sds",
    "counter",
    "url",
    "url_https",
]

# Pair-acquisition products: granule name contains two cycle numbers and four timestamps.
_PAIR_PRODUCTS = frozenset({"RIFG", "RUNW", "GUNW", "ROFF", "GOFF"})
_SINGLE_PRODUCTS = frozenset({"RSLC", "GSLC", "GCOV", "SME2"})

GROUP_REQUIRED = [
    "mission",
    "track",
    "direction",
    "frame",
    "start_time",
    "end_time",
    "crid",
    "url",
    "url_https",
]

# CMR collection short_name mapping: (inst_level, product) -> [short_name, ...]
# Lists include BETA versions (current) followed by anticipated operational names.
# Use --short_name to override.
_NISAR_SHORT_NAMES = {
    ("L1", "RSLC"): ["NISAR_L1_RSLC_BETA_V1", "NISAR_L1_RSLC"],
    ("L1", "RIFG"): ["NISAR_L1_RIFG_BETA_V1", "NISAR_L1_RIFG"],
    ("L1", "ROFF"): ["NISAR_L1_ROFF_BETA_V1", "NISAR_L1_ROFF"],
    ("L1", "RUNW"): ["NISAR_L1_RUNW_BETA_V1", "NISAR_L1_RUNW"],
    ("L2", "GCOV"): ["NISAR_L2_GCOV_BETA_V1", "NISAR_L2_GCOV"],
    ("L2", "GOFF"): ["NISAR_L2_GOFF_BETA_V1", "NISAR_L2_GOFF"],
    ("L2", "GUNW"): ["NISAR_L2_GUNW_BETA_V1", "NISAR_L2_GUNW"],
    ("L2", "GSLC"): ["NISAR_L2_GSLC_BETA_V1", "NISAR_L2_GSLC"],
    ("L3", "SME2"): ["NISAR_L3_SME2_BETA_V1", "NISAR_L3_SME2"],
    (None, "GCOV"): ["NISAR_L2_GCOV_BETA_V1", "NISAR_L2_GCOV"],
    (None, "GUNW"): ["NISAR_L2_GUNW_BETA_V1", "NISAR_L2_GUNW"],
    (None, "GOFF"): ["NISAR_L2_GOFF_BETA_V1", "NISAR_L2_GOFF"],
    (None, "GSLC"): ["NISAR_L2_GSLC_BETA_V1", "NISAR_L2_GSLC"],
    (None, "RSLC"): ["NISAR_L1_RSLC_BETA_V1", "NISAR_L1_RSLC"],
    (None, "RIFG"): ["NISAR_L1_RIFG_BETA_V1", "NISAR_L1_RIFG"],
    (None, "ROFF"): ["NISAR_L1_ROFF_BETA_V1", "NISAR_L1_ROFF"],
    (None, "RUNW"): ["NISAR_L1_RUNW_BETA_V1", "NISAR_L1_RUNW"],
    (None, "SME2"): ["NISAR_L3_SME2_BETA_V1", "NISAR_L3_SME2"],
}


# --- Geometry helpers ----------------------------------------------------------


def ullr_to_wkt(ul_lon, ul_lat, lr_lon, lr_lat):
    w, n = ul_lon, ul_lat
    e, s = lr_lon, lr_lat
    return f"POLYGON(({w} {n}, {e} {n}, {e} {s}, {w} {s}, {w} {n}))"


def bbox_to_wkt(min_lon, min_lat, max_lon, max_lat):
    return f"POLYGON(({min_lon} {max_lat}, {max_lon} {max_lat}, " f"{max_lon} {min_lat}, {min_lon} {min_lat}, {min_lon} {max_lat}))"


def _geom_obj_to_wkt(geom):
    """GeoJSON geometry dict -> 2-D WKT string."""
    gtype = geom.get("type", "")
    coords = geom.get("coordinates")

    def pt(c):
        return f"{c[0]} {c[1]}"

    def ring(r):
        return "(" + ", ".join(pt(c) for c in r) + ")"

    if gtype == "Point":
        return f"POINT({pt(coords)})"
    if gtype == "MultiPoint":
        return "MULTIPOINT(" + ", ".join(pt(c) for c in coords) + ")"
    if gtype == "LineString":
        return "LINESTRING(" + ", ".join(pt(c) for c in coords) + ")"
    if gtype == "MultiLineString":
        return "MULTILINESTRING(" + ", ".join("(" + ", ".join(pt(c) for c in ln) + ")" for ln in coords) + ")"
    if gtype == "Polygon":
        return "POLYGON(" + ", ".join(ring(r) for r in coords) + ")"
    if gtype == "MultiPolygon":
        return "MULTIPOLYGON(" + ", ".join("(" + ", ".join(ring(r) for r in p) + ")" for p in coords) + ")"
    if gtype == "GeometryCollection":
        return "GEOMETRYCOLLECTION(" + ", ".join(_geom_obj_to_wkt(g) for g in geom.get("geometries", [])) + ")"
    raise ValueError(f"Unsupported geometry type: '{gtype}'")


def geojson_file_to_wkt_list(path):
    with open(path) as f:
        gj = json.load(f)
    gtype = gj.get("type", "")
    geoms = []
    if gtype == "FeatureCollection":
        for feat in gj.get("features", []):
            g = feat.get("geometry")
            if g:
                geoms.append(g)
    elif gtype == "Feature":
        g = gj.get("geometry")
        if g:
            geoms.append(g)
    else:
        geoms.append(gj)
    return [_geom_obj_to_wkt(g) for g in geoms]


def _wkt_polygon_to_tuples(wkt):
    """Extract outer ring from WKT POLYGON as list of (lon, lat) tuples."""
    m = re.search(r"POLYGON\s*\(\s*\(([^)]+)\)", wkt, re.IGNORECASE)
    if m:
        coords = []
        for pair in m.group(1).split(","):
            parts = pair.strip().split()
            if len(parts) >= 2:
                coords.append((float(parts[0]), float(parts[1])))
        return coords
    return None


# --- NISAR granule name parser -------------------------------------------------


def _parse_nisar_granule_name(gname):
    """Parse a NISAR granule name into component fields.

    Single-acquisition products (RSLC, GSLC, GCOV, SME2) - 18 tokens:
      NISAR_{IL}_{PT}_{PROD}_{CYL}_{REL}_{P}_{FRM}_{MODE}_{POLE}_{S}_{Start}_{End}_{CRID}_{A}_{C}_{LOC}_{CTR}
       [0]  [1]  [2]   [3]  [4]   [5]  [6] [7]   [8]   [9]  [10]  [11]       [12]   [13] [14][15] [16]  [17]
      Example:
        NISAR_L2_PR_GCOV_015_172_D_065_4005_DHDH_A_20260121T031851_20260121T031926_P05006_N_F_J_001

    Pair-acquisition products (RIFG, RUNW, GUNW, ROFF, GOFF) - 20 tokens:
      NISAR_{IL}_{PT}_{PROD}_{CYL}_{REL}_{P}_{FRM}_{SCY}_{MODE}_{PO}_{RefStart}_{RefEnd}_{SecStart}_{SecEnd}_{CRID}_{A}_{C}_{LOC}_{CTR}
       [0]  [1]  [2]   [3]  [4]   [5]  [6] [7]   [8]   [9]  [10]  [11]          [12]       [13]         [14]    [15] [16][17] [18]  [19]
      SCY (secondary cycle) is at [8] between FRM [7] and MODE [9].
      start_time/end_time = reference acquisition; start_time2/end_time2 = secondary.
      No observation_mode (S) field in pair products.
      Example:
        NISAR_L2_PR_GUNW_003_071_A_173_005_2000_SH_20251022T041142_20251022T041217_20251115T041143_20251115T041218_X05010_N_F_J_001

    Returns a dict with only the fields present for that product type,
    or an empty dict if the name cannot be parsed.
    """
    parts = gname.split("_")
    if len(parts) < 18 or parts[0] != "NISAR":
        return {}
    try:
        product = parts[3]
        if product in _PAIR_PRODUCTS:
            # Pair product - 20 tokens; SCY at [8], four timestamps at [11-14]
            if len(parts) < 20:
                return {}
            return {
                "mission": parts[0],
                "inst_level": parts[1],
                "proctype": parts[2],
                "product": parts[3],
                "cycle": int(parts[4]),
                "track": int(parts[5]),
                "direction": parts[6],
                "frame": int(parts[7]),
                "cycle2": int(parts[8]),
                "mode": parts[9],
                "polarization": parts[10],
                "start_time": datetime.strptime(parts[11], "%Y%m%dT%H%M%S"),
                "end_time": datetime.strptime(parts[12], "%Y%m%dT%H%M%S"),
                "start_time2": datetime.strptime(parts[13], "%Y%m%dT%H%M%S"),
                "end_time2": datetime.strptime(parts[14], "%Y%m%dT%H%M%S"),
                "crid": parts[15],
                "accuracy": parts[16],
                "coverage": parts[17],
                "sds": parts[18],
                "counter": parts[19],
            }
        else:
            # Single-acquisition product - 18 tokens; MODE at [8], no SCY
            return {
                "mission": parts[0],
                "inst_level": parts[1],
                "proctype": parts[2],
                "product": parts[3],
                "cycle": int(parts[4]),
                "track": int(parts[5]),
                "direction": parts[6],
                "frame": int(parts[7]),
                "mode": parts[8],
                "polarization": parts[9],
                "observation_mode": parts[10],
                "start_time": datetime.strptime(parts[11], "%Y%m%dT%H%M%S"),
                "end_time": datetime.strptime(parts[12], "%Y%m%dT%H%M%S"),
                "crid": parts[13],
                "accuracy": parts[14],
                "coverage": parts[15],
                "sds": parts[16],
                "counter": parts[17],
            }
    except (ValueError, IndexError):
        return {}


def _parse_dt(val):
    """Normalise to datetime from granule-name string, ISO string, or datetime."""
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        for fmt in ("%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    return None


# --- CMR direct query helpers --------------------------------------------------

# CMR granule search is a public API -- no authentication required.
CMR_GRANULE_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"


def _cmr_entry_to_geom(entry):
    """Extract footprint from a CMR JSON granule entry as a GeoJSON geometry dict.

    CMR polygon rings are strings of space-separated 'lat lon' pairs.
    CMR bounding boxes are strings of 'S W N E'.
    """
    if entry.get("polygons"):
        tokens = entry["polygons"][0][0].split()
        coords = [[float(tokens[i + 1]), float(tokens[i])] for i in range(0, len(tokens) - 1, 2)]
        if coords and coords[0] != coords[-1]:
            coords.append(coords[0])
        return {"type": "Polygon", "coordinates": [coords]}
    if entry.get("boxes"):
        s, w, n, e = [float(v) for v in entry["boxes"][0].split()]
        return {"type": "Polygon", "coordinates": [[[w, n], [e, n], [e, s], [w, s], [w, n]]]}
    return None


def _cmr_entry_to_records(entry):
    """Convert a CMR JSON granule entry to a list of record dicts (one per .h5 file).

    Every record contains both url (s3://) and url_https fields so the caller
    can choose which to emit without re-querying.
    """
    gname = entry.get("producer_granule_id") or entry.get("title", "")
    parsed = _parse_nisar_granule_name(gname)
    geom = _cmr_entry_to_geom(entry)

    s3_links, https_links = [], []
    for lnk in entry.get("links", []):
        href = lnk.get("href", "")
        if not href.endswith(".h5"):
            continue
        if href.startswith("s3://") or "s3#" in lnk.get("rel", ""):
            s3_links.append(href)
        elif href.startswith("https://"):
            https_links.append(href)

    n = max(len(s3_links), len(https_links))
    if n == 0:
        return []
    s3_links = s3_links + [None] * (n - len(s3_links))
    https_links = https_links + [None] * (n - len(https_links))

    records = []
    for s3_url, https_url in zip(s3_links, https_links):
        primary = s3_url or https_url
        rec = {
            "bucket": primary.split("/")[2] if primary and primary.startswith("s3://") else None,
            "mission": parsed.get("mission", "NISAR"),
            "inst_level": parsed.get("inst_level"),
            "proctype": parsed.get("proctype"),
            "product": parsed.get("product"),
            "cycle": parsed.get("cycle"),
            "track": parsed.get("track"),
            "direction": parsed.get("direction"),
            "frame": parsed.get("frame"),
            "mode": parsed.get("mode"),
            "polarization": parsed.get("polarization"),
            "start_time": parsed.get("start_time"),
            "end_time": parsed.get("end_time"),
            "crid": parsed.get("crid"),
            "accuracy": parsed.get("accuracy"),
            "coverage": parsed.get("coverage"),
            "sds": parsed.get("sds"),
            "counter": parsed.get("counter"),
            "url": s3_url,
            "url_https": https_url,
            "_geom": geom,
        }
        for _k in ("cycle2", "start_time2", "end_time2"):
            if _k in parsed:
                rec[_k] = parsed[_k]
        if "observation_mode" in parsed:
            rec["observation_mode"] = parsed["observation_mode"]
        records.append(rec)
    return records


# --- CMR search helpers --------------------------------------------------------


def _build_short_names(args):
    """Construct CMR short_name list from --short_name, --inst_level, --product."""
    if args.short_name:
        return list(args.short_name)
    products = args.product or [None]
    levels = args.inst_level or [None]
    names = []
    for lv in levels:
        for pr in products:
            names.extend(_NISAR_SHORT_NAMES.get((lv, pr), []))
    return list(OrderedDict.fromkeys(names))  # deduplicate, preserve order


def _build_granule_name_patterns(args):
    """Build CMR granule_name wildcard patterns from available filter arguments.

    When a filter field has multiple values (e.g. ``--frame 17 18``), the
    cartesian product of all multi-value fields is expanded so that each
    combination gets its own specific CMR pattern.  This pushes the filtering
    to the server side and avoids fetching unrelated granules.

    Tokens are assembled in name-field order and joined with '*' wildcards so
    each pattern can match any granule whose name contains those tokens in
    sequence.  Every pattern starts and ends with '*'.

    Single-acquisition token order:  product, cycle, track, direction, frame, mode, polarization
    Pair-acquisition token order:    product, cycle, track, direction, frame, cycle2, mode, polarization
      (cycle2 / SCY sits between FRM and MODE in pair-product names)

    Examples:
      --product GCOV --track 64 --frame 1 --mode 2005 --polarization DHDH
        -> ['NISAR*_GCOV*_064*_001*_2005*_DHDH*']
      --track 105 --frame 17 18
        -> ['NISAR*_*_*_105_*_*_017_*_*_*', 'NISAR*_*_*_105_*_*_018_*_*_*']

    Returns an empty list when no filtering token is available.
    """
    from itertools import product as _product

    is_pair = bool(args.product and len(args.product) == 1 and args.product[0] in _PAIR_PRODUCTS)

    def _vals(lst, fmt=None):
        """Return list of formatted token values, or ['*'] if empty/None."""
        if not lst:
            return ["*"]
        return [fmt.format(v) if fmt else str(v) for v in lst]

    # Each element is a list of possible values for that token position.
    token_slots = [
        _vals(args.product),
        _vals(args.cycle, "{:03d}"),
        _vals(args.track, "{:03d}"),
        _vals(args.direction),
        _vals(args.frame, "{:03d}"),
    ]
    if is_pair:
        cycle2 = getattr(args, "cycle2", None)
        token_slots.append(_vals(cycle2, "{:03d}"))
    token_slots.extend([
        _vals(args.mode),
        _vals(args.polarization),
    ])

    patterns = []
    for combo in _product(*token_slots):
        tokens = list(combo)
        # tokens[0] is product; short_name already covers collection/product
        # filtering.  Only send granule_name to CMR when at least one other
        # field has a specific value -- otherwise the all-wildcard pattern
        # forces expensive server-side regex on every granule name.
        if not any(t != "*" for t in tokens[1:]):
            continue
        pat = "NISAR*" + "".join(f"_{tok}" for tok in tokens) + "*"
        patterns.append(pat)
    return patterns


def _build_cmr_spatial(args):
    """Build earthaccess CMR spatial kwargs from geometry args."""
    if args.point:
        lon, lat = args.point
        if args.buffer:
            # buffer in degrees; 1 deg ~ 111 320 m
            return {"circle": (lon, lat, args.buffer * 111320)}
        return {"point": (lon, lat)}
    if args.bbox:
        return {"bounding_box": tuple(args.bbox)}
    if args.ullr:
        ul_lon, ul_lat, lr_lon, lr_lat = args.ullr
        return {"bounding_box": (ul_lon, lr_lat, lr_lon, ul_lat)}  # (W, S, E, N)
    if args.wkt:
        wkt = args.wkt.strip()
        pt_m = re.match(r"POINT\s*\(\s*([0-9.eE+\-]+)\s+([0-9.eE+\-]+)\s*\)", wkt, re.IGNORECASE)
        if pt_m:
            return {"point": (float(pt_m.group(1)), float(pt_m.group(2)))}
        tuples = _wkt_polygon_to_tuples(wkt)
        if tuples:
            return {"polygon": tuples}
    if args.geojson:
        wkt_list = geojson_file_to_wkt_list(args.geojson)
        if wkt_list:
            wkt = wkt_list[0] if (not args.union_geojson or len(wkt_list) == 1) else None
            if wkt:
                tuples = _wkt_polygon_to_tuples(wkt)
                if tuples:
                    return {"polygon": tuples}
            elif args.union_geojson and len(wkt_list) > 1:
                all_lons, all_lats = [], []
                for w in wkt_list:
                    t = _wkt_polygon_to_tuples(w) or []
                    all_lons.extend(c[0] for c in t)
                    all_lats.extend(c[1] for c in t)
                if all_lons:
                    return {"bounding_box": (min(all_lons), min(all_lats), max(all_lons), max(all_lats))}
    return {}


def search_earthaccess(args):
    """Search NISAR products via direct CMR HTTP query.

    CMR granule search is a public API -- no Earthdata authentication required.
    Uses the requests library (a transitive dependency of earthaccess).
    Returns a list of record dicts.
    """
    try:
        import requests as _requests
    except ImportError:
        print("Error: 'requests' is not installed. Install with: pip install requests", file=sys.stderr)
        sys.exit(1)

    # -- Build short_names list -------------------------------------------------
    short_names = list(args.short_name) if args.short_name else _build_short_names(args)
    if not short_names:
        short_names = [None]  # fall back to provider-level search

    # -- Build base CMR params --------------------------------------------------
    base_params = {}

    patterns = _build_granule_name_patterns(args)

    if args.start_time_after and args.start_time_before:
        base_params["temporal"] = f"{args.start_time_after},{args.start_time_before}"
    elif args.start_time_after:
        base_params["temporal"] = f"{args.start_time_after},"
    elif args.start_time_before:
        base_params["temporal"] = f",{args.start_time_before}"

    spatial = _build_cmr_spatial(args)
    if "point" in spatial:
        lon, lat = spatial["point"]
        base_params["point"] = f"{lon},{lat}"
    elif "bounding_box" in spatial:
        base_params["bounding_box"] = ",".join(str(v) for v in spatial["bounding_box"])
    elif "polygon" in spatial:
        base_params["polygon"] = ",".join(f"{lon},{lat}" for lon, lat in spatial["polygon"])
    elif "circle" in spatial:
        lon, lat, r = spatial["circle"]
        base_params["circle"] = f"{lon},{lat},{r:.0f}"

    if not short_names[0]:
        base_params["provider"] = "ASF"

    count = args.limit if (args.limit and args.limit > 0) else -1

    # When no patterns were generated, use a single query without granule_name
    if not patterns:
        patterns = [None]

    if args.verbose or args.dryrun:
        print("--- CMR direct query ---", file=sys.stderr)
        print(f"  short_names: {short_names}", file=sys.stderr)
        print(f"  params: {base_params}", file=sys.stderr)
        print(f"  patterns: {patterns}", file=sys.stderr)
        print(f"  count:  {count}", file=sys.stderr)
        print(file=sys.stderr)

    if args.dryrun:
        return []

    # -- Query CMR: for each (short_name, pattern) combo, paginate and collect --
    # Deduplicate across patterns by producer_granule_id to avoid returning the
    # same granule twice when patterns overlap.
    seen_ids = set()
    entries = []

    for sn in short_names:
        sn_found = False
        for pat in patterns:
            params = dict(base_params)
            if sn:
                params["short_name"] = sn
            if pat:
                params["producer_granule_id"] = pat
                params["options[producer_granule_id][pattern]"] = "true"
            params["page_size"] = min(2000, count) if count > 0 else 2000

            page_num = 1
            while True:
                params["page_num"] = page_num
                resp = _requests.get(CMR_GRANULE_URL, params=params, timeout=60)
                resp.raise_for_status()
                page_entries = resp.json()["feed"]["entry"]
                cmr_hits = int(resp.headers.get("CMR-Hits", 0))
                if args.verbose:
                    _pat_label = f" pattern={pat!r}" if pat else ""
                    print(f"  short_name={sn!r}{_pat_label} page {page_num}: {len(page_entries)} entries (CMR-Hits={cmr_hits})", file=sys.stderr)

                for e in page_entries:
                    gid = e.get("producer_granule_id") or e.get("title", "")
                    if gid not in seen_ids:
                        seen_ids.add(gid)
                        entries.append(e)
                        sn_found = True

                fetched_this_pat = page_num * params["page_size"]
                if count > 0 and len(entries) >= count:
                    break
                if fetched_this_pat >= cmr_hits:
                    break
                page_num += 1

            if count > 0 and len(entries) >= count:
                break

        if sn_found:
            break  # BETA collection found results; skip operational fallback

    if count > 0:
        entries = entries[:count]

    if args.verbose:
        print(f"CMR returned {len(entries)} granule(s).", file=sys.stderr)

    records = []
    for entry in entries:
        records.extend(_cmr_entry_to_records(entry))
    return records


# --- Column post-filtering -----------------------------------------------------


def _text_matches(val, filter_vals):
    """Match val against filter list; '%' triggers LIKE-style wildcard."""
    s = str(val or "")
    for fv in filter_vals:
        if "%" in fv:
            pattern = "^" + re.escape(fv).replace(r"\%", ".*") + "$"
            if re.match(pattern, s, re.IGNORECASE):
                return True
        elif s.upper() == str(fv).upper():
            return True
    return False


def _apply_column_filters(records, args):
    """Post-filter records against column filter args."""
    result = []
    for rec in records:
        keep = True

        for col in ("cycle", "cycle2", "track", "frame"):
            vals = getattr(args, col, None)
            if not vals:
                continue
            rval = rec.get(col)
            try:
                if int(rval) not in [int(v) for v in vals]:
                    keep = False
                    break
            except (TypeError, ValueError):
                keep = False
                break

        if not keep:
            continue

        for col in ("bucket", "mission", "inst_level", "proctype", "product", "direction", "mode", "polarization", "observation_mode", "crid", "accuracy", "coverage", "sds", "counter"):
            vals = getattr(args, col, None)
            if vals and not _text_matches(rec.get(col), vals):
                keep = False
                break

        if keep and args.url_pattern:
            url = rec.get("url") or rec.get("url_https") or ""
            if not _text_matches(url, [args.url_pattern]):
                keep = False

        if keep:
            result.append(rec)
    return result


# --- Latest-CRID filter --------------------------------------------------------


def _apply_latest_crid(records):
    """Keep only the record with the highest CRID per unique scene
    (track, frame, direction, product, start_time)."""
    groups = defaultdict(list)
    order = []
    for rec in records:
        dt = _parse_dt(rec.get("start_time"))
        key = (rec.get("track"), rec.get("frame"), rec.get("direction"), rec.get("product"), dt)
        if key not in groups:
            order.append(key)
        groups[key].append(rec)

    result = []
    for key in order:
        best = max(groups[key], key=lambda r: str(r.get("crid") or ""))
        result.append(best)
    return result


# --- Sorting for grouped output ------------------------------------------------


def _sort_for_group(records):
    def key(r):
        dt = _parse_dt(r.get("start_time"))
        return (
            r.get("track") or 0,
            r.get("direction") or "",
            r.get("frame") or 0,
            dt or datetime.min,
        )

    return sorted(records, key=key)


# --- Output formatters ---------------------------------------------------------


def _rec_props(rec):
    """Record dict with internal keys removed and datetimes stringified."""
    props = {}
    for k, v in rec.items():
        if k.startswith("_"):
            continue
        props[k] = v.isoformat() if isinstance(v, datetime) else v
    return props


def _to_geojson_str(records):
    features = []
    for rec in records:
        features.append(
            {
                "type": "Feature",
                "geometry": rec.get("_geom"),
                "properties": _rec_props(rec),
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}, indent=2, default=str)


def _geom_to_kml_polygon(geom):
    if not geom:
        return ""
    gtype = geom.get("type", "")
    coords = None
    if gtype == "Polygon":
        coords = geom["coordinates"][0]
    elif gtype == "MultiPolygon":
        coords = geom["coordinates"][0][0]
    if coords is None:
        return ""
    coord_str = " ".join(f"{c[0]},{c[1]},{c[2] if len(c) > 2 else 0}" for c in coords)
    return "    <Polygon><outerBoundaryIs><LinearRing>" f"<coordinates>{coord_str}</coordinates>" "</LinearRing></outerBoundaryIs></Polygon>"


def _to_kml_str(records):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        "  <name>NISAR Products</name>",
    ]
    for rec in records:
        url = rec.get("url") or rec.get("url_https", "")
        gname = os.path.splitext(os.path.basename(url))[0] if url else "unknown"
        props = _rec_props(rec)
        desc = "\n".join(f"{k}: {v}" for k, v in props.items() if v is not None)
        poly = _geom_to_kml_polygon(rec.get("_geom"))
        lines.append("  <Placemark>")
        lines.append(f"    <name>{gname}</name>")
        lines.append(f"    <description><![CDATA[{desc}]]></description>")
        if poly:
            lines.append(poly)
        lines.append("  </Placemark>")
    lines += ["</Document>", "</kml>"]
    return "\n".join(lines)


def format_output(records, fmt, columns=None, https=False):
    """Format a list of record dicts.

    For fmt='url': returns one URL per record (s3 by default; https if https=True).
    For all other formats: both url and url_https columns are included.
    """
    if not records:
        return []

    if fmt == "url":
        key = "url_https" if https else "url"
        return [str(r.get(key, "")) for r in records if r.get(key)]

    if fmt == "csv":
        cols = columns or [k for k in ALL_COLUMNS if k in records[0]]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for rec in records:
            w.writerow([(v.isoformat() if isinstance(v, datetime) else ("" if v is None else str(v))) for v in (rec.get(c) for c in cols)])
        return [buf.getvalue().rstrip("\r\n")]

    if fmt == "json":
        cols = columns or ALL_COLUMNS
        data = [{c: (v.isoformat() if isinstance(v, datetime) else v) for c in cols if (v := rec.get(c)) is not None or c in rec} for rec in records]
        return [json.dumps(data, indent=2, default=str)]

    if fmt == "geojson":
        return [_to_geojson_str(records)]

    if fmt == "kml":
        return [_to_kml_str(records)]

    return []


# --- Grouped output ------------------------------------------------------------


def _group_records(records):
    groups = defaultdict(list)
    order = []
    for rec in records:
        key = (rec.get("track"), rec.get("direction"), rec.get("frame"))
        if key not in groups:
            order.append(key)
        groups[key].append(rec)
    return order, groups


def _date_range(grp):
    starts = [_parse_dt(r.get("start_time")) for r in grp]
    ends = [_parse_dt(r.get("end_time")) for r in grp]
    starts = [s for s in starts if s]
    ends = [e for e in ends if e]
    first = min(starts).strftime("%Y%m%d") if starts else "unknown"
    last = max(ends).strftime("%Y%m%d") if ends else "unknown"
    return first, last


def _mission_str(grp):
    for r in grp:
        m = r.get("mission")
        if m:
            return str(m)
    return "NISAR"


def _product_str(grp):
    for r in grp:
        p = r.get("product")
        if p:
            return str(p).upper()
    return ""


def _group_file_base(mission, product, track, direction, frame, first, last, fmt):
    prod = f"_{product}" if product else ""
    base = f"{mission}{prod}_{track:03d}_{direction or 'X'}_{frame:03d}_{first}_{last}"
    return f"{base}_s3urls.txt" if fmt == "url" else f"{base}.{fmt}"


def output_grouped(records, args):
    order, groups = _group_records(records)

    if args.output:
        os.makedirs(args.output, exist_ok=True)
        for track, direction, frame in order:
            grp = groups[(track, direction, frame)]
            first, last = _date_range(grp)
            mission = _mission_str(grp)
            product = _product_str(grp)
            fname = _group_file_base(mission, product, track, direction, frame, first, last, args.format)
            fpath = os.path.join(args.output, fname)
            lines = format_output(grp, args.format, args.columns, https=args.https)
            content = "\n".join(lines) + ("\n" if lines else "")
            with open(fpath, "w") as fh:
                fh.write(content)
            if args.verbose:
                print(f"  {fname}  ({len(grp)} record(s))", file=sys.stderr)
    else:
        if args.format == "url":
            first_group = True
            for track, direction, frame in order:
                grp = groups[(track, direction, frame)]
                product = _product_str(grp)
                urls = format_output(grp, "url", https=args.https)
                if not first_group:
                    sys.stdout.write("\n")
                first_group = False
                prod_str = f" | Product: {product}" if product else ""
                print(f"=== Track: {track:03d} | Direction: {direction or '?'} | Frame: {frame:03d}{prod_str} ===")
                sys.stdout.write("\n".join(urls))
                if urls:
                    sys.stdout.write("\n")
        else:
            # Single structured document for non-url formats
            lines = format_output(records, args.format, args.columns, https=args.https)
            sys.stdout.write("\n".join(lines))
            if lines:
                sys.stdout.write("\n")


# --- Main processing -----------------------------------------------------------


def processing(args):
    records = search_earthaccess(args)
    if args.dryrun:
        return

    records = _apply_column_filters(records, args)
    if not args.allcrids:
        records = _apply_latest_crid(records)
    if args.group:
        records = _sort_for_group(records)

    if args.verbose:
        print(f"Found {len(records)} record(s).", file=sys.stderr)

    if not records:
        return

    if args.group:
        if args.verbose and args.output:
            print(f"Writing grouped files to: {args.output}", file=sys.stderr)
        output_grouped(records, args)
        return

    lines = format_output(records, args.format, args.columns, https=args.https)
    output = "\n".join(lines) + ("\n" if lines else "")

    if args.output:
        out_dir = os.path.dirname(args.output)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output, "w") as fh:
            fh.write(output)
        if args.verbose:
            print(f"Output written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)


# --- Argument parsing ----------------------------------------------------------


def myargsparse(a):
    if type(a) is str:
        a = shlex.split(a)

    class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        pass

    thisprog = os.path.basename(a[0])

    epilog = f"""
\nExamples:
\r  NOTE: --product GCOV is the default.


\r  All GCOV URLs for ascending track 64 (latest CRID, s3):
\r    {thisprog} --product GCOV --track 64 --direction A

\r  HTTPS URLs instead of s3:
\r    {thisprog} --product GCOV --track 64 --https

\r  Specify CMR short name directly:
\r    {thisprog} --short_name NISAR_L2_GCOV --track 64

\r  Include all CRID versions:
\r    {thisprog} --product GCOV --track 64 --allcrids

\r  Date range:
\r    {thisprog} --product GCOV --start_time_after 2024-01-01 --start_time_before 2024-06-01

\r  Grouped output to stdout (both URL columns in csv/json/geojson/kml):
\r    {thisprog} --product GCOV --group

\r  Grouped files to directory:
\r    {thisprog} --product GCOV --group -o /data/urls/

\r  GeoJSON output (includes both url and url_https):
\r    {thisprog} --product GCOV --track 64 --format geojson -o results.geojson

\r  KML output:
\r    {thisprog} --product GCOV --ullr -120 50 -100 40 --format kml

\r  Bounding box upper-left/lower-right:
\r    {thisprog} --ullr -120 50 -100 40

\r  Standard bbox (xmin ymin xmax ymax):
\r    {thisprog} --bbox -120 40 -100 50

\r  Point with buffer (degrees; converted to metres for CMR circle):
\r    {thisprog} --point -105.5 45.2 --buffer 2.0

\r  WKT polygon:
\r    {thisprog} --wkt "POLYGON((-120 40,-100 40,-100 50,-120 50,-120 40))"

\r  GeoJSON file - union all features:
\r    {thisprog} --geojson aoi.geojson --union_geojson --product GCOV --group

\r  Pair-acquisition product (GUNW) with secondary cycle:
\r    {thisprog} --product GUNW --track 71 --direction A --frame 173 --cycle 3 --cycle2 5

\r  Dry-run (show CMR kwargs without searching):
\r    {thisprog} --product GCOV --track 64 --dryrun
"""

    description = "SEPPO - Search NISAR product URLs via NASA Earthdata CMR (earthaccess).\n" "Credentials are read from  the netrc; an interactive prompt is shown if\n" "no entry is found.  Use --dryrun to inspect the CMR query without logging in.\n" "For --format url: s3:// by default, --https for https:// URLs.\n" "For all other formats: both url (s3) and url_https columns are included."

    p = argparse.ArgumentParser(prog=thisprog, description=description, epilog=epilog, formatter_class=CustomFormatter)

    # -- Column / metadata filters ---------------------------------------------
    cf = p.add_argument_group("Column / metadata filters (all accept one or more values)")
    cf.add_argument("--bucket", nargs="*", metavar="TEXT", help="S3 bucket name(s). Supports LIKE wildcards (%%).")
    cf.add_argument("--mission", nargs="*", metavar="CODE", help="Mission code(s) (e.g. NISAR)")
    cf.add_argument("--inst_level", nargs="*", metavar="CODE", help="Instrument (L-band) and Processing level(s) (e.g. L1 L2)")
    cf.add_argument("--proctype", nargs="*", metavar="CODE", help="Processing type(s)")
    cf.add_argument("--product", nargs="+", required=False, default=["GCOV"], metavar="CODE", help="Product type(s) (e.g. GCOV RSLC GSLC SME2 RIFG RUNW GUNW ROFF GOFF)", choices=sorted(_SINGLE_PRODUCTS.union(_PAIR_PRODUCTS)))
    cf.add_argument("--short_name", nargs="*", metavar="NAME", help="CMR short name(s) - overrides auto-construction from " "--inst_level + --product.  E.g. NISAR_L2_GCOV")
    cf.add_argument("--cycle", nargs="*", type=int, metavar="INT", help="Reference cycle number(s)")
    cf.add_argument("--cycle2", nargs="*", type=int, metavar="INT", help="Secondary cycle number(s) for pair-acquisition products " "(RIFG, RUNW, GUNW, ROFF, GOFF).  In the granule name SCY sits between FRM and MODE.")
    cf.add_argument("--track", nargs="*", type=int, metavar="INT", help="Track / relative-orbit number(s)")
    cf.add_argument("--direction", nargs="*", metavar="A|D", help="Flight direction: A (ascending) or D (descending)")
    cf.add_argument("--frame", nargs="*", type=int, metavar="INT", help="Frame number(s)")
    cf.add_argument("--mode", nargs="*", metavar="CODE", help="Acquisition mode(s)")
    cf.add_argument("--polarization", nargs="*", metavar="CODE", help="Polarization code(s). " "Single-acquisition products use a 4-char code {freq-A-pol}{freq-B-pol} " "combining the polarization of each NISAR frequency (A and B), " "where each 2-char code is: SH=Single H, SV=Single V, " "DH=Dual H (HH+HV), DV=Dual V (VV+VH), QP=Quad, NA=not operated. " "Examples: SHSH, SVSV, DHDH, DVDV, SHNA, NASH. " "Pair-acquisition products use a 2-char code (e.g. SH, SV, DH, DV).")
    cf.add_argument("--observation_mode", nargs="*", metavar="CODE", help="Observation mode code(s)")
    cf.add_argument("--crid", nargs="*", metavar="CODE", help="Composite release ID(s)")
    cf.add_argument("--accuracy", nargs="*", metavar="CODE", help="Accuracy code(s)")
    cf.add_argument("--coverage", nargs="*", metavar="CODE", help="Coverage code(s)")
    cf.add_argument("--sds", nargs="*", metavar="CODE", help="SDS code(s)")
    cf.add_argument("--counter", nargs="*", metavar="CODE", help="Counter value(s)")
    cf.add_argument("--url_pattern", metavar="PATTERN", help="LIKE pattern matched against url (e.g. '%%GCOV%%')")

    # -- Time filters ----------------------------------------------------------
    tf = p.add_argument_group("Time filters", "ISO 8601: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS.  " "Maps to CMR temporal parameter. Only for single-acquisition products " "(RSLC, GSLC, GCOV, SME2)")
    tf.add_argument("--start_time_after", metavar="DATETIME", help="Acquisition start >= DATETIME")
    tf.add_argument("--start_time_before", metavar="DATETIME", help="Acquisition start <= DATETIME")

    # -- Spatial filters -------------------------------------------------------
    sf = p.add_argument_group("Spatial filters")
    geom_src = sf.add_mutually_exclusive_group()
    geom_src.add_argument("--wkt", metavar="WKT", help="OGC WKT geometry in WGS84 (POINT, POLYGON, MULTIPOLYGON, ...)")
    geom_src.add_argument("--ullr", nargs=4, type=float, metavar=("UL_LON", "UL_LAT", "LR_LON", "LR_LAT"), help="Bounding box from upper-left / lower-right corners")
    geom_src.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"), help="Bounding box in (xmin ymin xmax ymax) order")
    geom_src.add_argument("--point", nargs=2, type=float, metavar=("LON", "LAT"), help="Point in WGS84 lon/lat.  Use --buffer for radius search.")
    geom_src.add_argument("--geojson", metavar="FILE", help="GeoJSON file.  First feature used unless --union_geojson.")
    sf.add_argument("--buffer", type=float, metavar="DEG", help="Buffer radius in degrees for --point (converted to metres: 1 deg ~ 111 km)")
    sf.add_argument("--union_geojson", action="store_true", default=False, help="Union all GeoJSON features into a single geometry")

    # -- Output ----------------------------------------------------------------
    og = p.add_argument_group("Output")
    og.add_argument("--group", action="store_true", default=False, help="Group by (track, direction, frame) ordered by start_time. " "stdout: section headers + URLs.  --output: directory, one file per group.")
    og.add_argument("--allcrids", action="store_true", default=False, help="Return all CRID versions; default keeps only the latest per scene.")
    og.add_argument("--https", action="store_true", default=False, help="For --format url: emit https:// URLs instead of s3:// (default s3).")
    og.add_argument("-o", "--output", metavar="PATH", help="Without --group: output file path.  With --group: output directory.")
    og.add_argument("--format", default="url", choices=["url", "csv", "json", "geojson", "kml"], help="Output format: url (one per line), csv, json, geojson, kml")
    og.add_argument("--columns", nargs="*", metavar="COL", help="Columns for csv/json output (default: all).  " "Example: --columns product track frame crid url url_https")
    og.add_argument("--limit", type=int, metavar="N", help="Maximum total number of CMR granules to retrieve (across all query patterns)")
    og.add_argument("-v", "--verbose", action="store_true", default=False, help="Print CMR kwargs, granule count, etc. to stderr")
    og.add_argument("--dryrun", action="store_true", default=False, help="Print the CMR kwargs without logging in or searching, then exit")

    args = p.parse_args(a[1:])

    if len(a) == 1:
        p.print_help()
        sys.exit(0)

    # Normalise direction
    if args.direction:
        args.direction = [d.upper() for d in args.direction]
        invalid = [d for d in args.direction if d not in ("A", "D")]
        if invalid:
            p.error(f"--direction: invalid value(s) {invalid!r}; expected 'A' or 'D'")

    if args.buffer and not args.point:
        print("Warning: --buffer is only used with --point; ignoring.", file=sys.stderr)

    return args


# --- Entry point ---------------------------------------------------------------


def _main(a):
    args = myargsparse(a)
    processing(args)


def main():
    from openseppo import banner
    args = myargsparse(sys.argv)
    if args.output:
        banner("seppo_nisar_search")
    processing(args)


if __name__ == "__main__":
    main()
