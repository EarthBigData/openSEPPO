# seppo_nisar_search -- CLI Reference

Search NISAR product URLs via NASA Earthdata CMR (default) or the `asf_search`
package (`-asf`).

Credentials are read from `~/.netrc`; an interactive prompt is shown if no
entry is found. Use `--dryrun` to inspect the query without logging in.
For `--format url`: s3:// by default, `--https` for https:// URLs.
For all other formats: both `url` (s3) and `url_https` columns are included.

**Collections and releases.** NISAR products live in versioned CMR collections
(`NISAR_{level}_{product}_{tier}_V{n}`, e.g. `NISAR_L2_GCOV_BETA_V1`,
`NISAR_L2_GCOV_PROVISIONAL_V1`). The tool searches **all** versions in one query
via a wildcard short-name pattern, so new collection versions are picked up
automatically. By default only the **latest release** of each scene is returned
(newest tier, then highest CRID); the originating collection is available as the
`collection` column. Use `--collection` to keep a specific tier, or `--allcrids`
to return every version. Urgent Response (UR) products are excluded unless `-ur`
is given.

Both the default CMR path and the `-asf` path return identical results for the
same query.

---

## Usage

```
seppo_nisar_search [-h] [--bucket [TEXT ...]] [--collection [NAME ...]]
                  [--mission [CODE ...]] [--inst_level [CODE ...]]
                  [--proctype [CODE ...]] [--product CODE [CODE ...]]
                  [--short_name [NAME ...]] [--cycle [INT ...]] [--cycle2 [INT ...]]
                  [--track [INT ...]] [--direction [A|D ...]]
                  [--frame [INT ...]] [--mode [CODE ...]]
                  [--polarization [CODE ...]]
                  [--observation_mode [CODE ...]] [--crid [CODE ...]]
                  [--accuracy [CODE ...]] [--coverage [CODE ...]]
                  [--sds [CODE ...]] [--counter [CODE ...]]
                  [--url_pattern PATTERN]
                  [--start_time_after DATETIME] [--start_time_before DATETIME]
                  [--wkt WKT | --ullr UL_LON UL_LAT LR_LON LR_LAT |
                   --bbox MIN_LON MIN_LAT MAX_LON MAX_LAT |
                   --point LON LAT | --geojson FILE]
                  [--buffer DEG] [--union_geojson] [--group] [--allcrids] [-ur]
                  [--https] [-o PATH] [--format {url,csv,json,geojson,kml}]
                  [--columns [COL ...]] [--limit N] [-asf] [-v] [--dryrun]
```

---

## Arguments

### Metadata filters

All filters accept one or more values.

| Argument | Description |
|----------|-------------|
| `--product CODE [CODE ...]` | Product type(s). Default: `GCOV`. Other values: `RSLC`, `GSLC`, `SME2`, `RIFG`, `RUNW`, `GUNW`, `ROFF`, `GOFF`. |
| `--collection NAME` | Collection short name(s) to keep, e.g. `NISAR_L2_GCOV_PROVISIONAL_V1`. Supports LIKE wildcards (`%`), e.g. `'%PROVISIONAL%'`. Post-filter; by default the latest release of each scene is kept across all collections. |
| `--short_name NAME` | CMR short name(s) -- overrides the auto-generated wildcard pattern (e.g. `NISAR_L2_GCOV_BETA_V1`). Sent to CMR with the pattern option, so `%`-free names match exactly and `*` wildcards are honoured. |
| `--track INT [INT ...]` | Track / relative-orbit number(s). |
| `--direction A\|D` | Flight direction: `A` (ascending) or `D` (descending). |
| `--frame INT [INT ...]` | Frame number(s). |
| `--cycle INT [INT ...]` | Reference cycle number(s). |
| `--cycle2 INT [INT ...]` | Secondary cycle number(s) for pair products (RIFG, RUNW, GUNW, ROFF, GOFF). |
| `--polarization CODE` | Polarization code(s). Single-acquisition: 4-char combined code, e.g. `DHDH`, `DVDV`, `SHNA`. Pair products: 2-char, e.g. `DH`, `DV`. |
| `--mode CODE` | Acquisition mode code(s). |
| `--observation_mode CODE` | Observation mode code(s). |
| `--crid CODE` | Composite release ID(s). |
| `--accuracy CODE` | Accuracy code(s). |
| `--mission CODE` | Mission code(s), e.g. `NISAR`. |
| `--inst_level CODE` | Instrument and processing level(s), e.g. `L1`, `L2`. |
| `--proctype CODE` | Processing type(s). |
| `--bucket TEXT` | S3 bucket name(s). Supports LIKE wildcards (`%`). |
| `--coverage CODE` | Coverage code(s). |
| `--sds CODE` | SDS code(s). |
| `--counter CODE` | Counter value(s). |
| `--url_pattern PATTERN` | LIKE pattern matched against url, e.g. `'%GCOV%'`. |

### Time filters

ISO 8601 format: `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`. Only for single-acquisition products (RSLC, GSLC, GCOV, SME2).

| Argument | Description |
|----------|-------------|
| `--start_time_after DATETIME` | Acquisition start >= DATETIME. |
| `--start_time_before DATETIME` | Acquisition start <= DATETIME. |

### Spatial filters (mutually exclusive)

| Argument | Description |
|----------|-------------|
| `--ullr UL_LON UL_LAT LR_LON LR_LAT` | Bounding box from upper-left / lower-right corners. |
| `--bbox MIN_LON MIN_LAT MAX_LON MAX_LAT` | Bounding box in (xmin ymin xmax ymax) order. |
| `--point LON LAT` | Point in WGS84 lon/lat. Use `--buffer` for radius search. |
| `--wkt WKT` | OGC WKT geometry in WGS84 (POINT, POLYGON, MULTIPOLYGON, ...). |
| `--geojson FILE` | GeoJSON file. First feature used unless `--union_geojson`. |
| `--buffer DEG` | Buffer radius in degrees for `--point` (1 deg ~ 111 km). |
| `--union_geojson` | Union all GeoJSON features into a single geometry. |

### Output

| Argument | Description |
|----------|-------------|
| `--format {url,csv,json,geojson,kml}` | Output format. Default: `url` (one URL per line). |
| `-o PATH`, `--output` | Without `--group`: output file. With `--group`: output directory (one file per group). |
| `--group` | Group results by (track, direction, frame) ordered by start_time. |
| `--columns COL [COL ...]` | Columns for csv/json output (default: all). Includes the `collection` column. |
| `--allcrids` | Return every version (all collections and CRIDs); default keeps only the latest release per scene. |
| `-ur`, `--urgent_response` | Include Urgent Response (UR) products from the `NISAR_UR_L1`/`L2` collections. Excluded by default. |
| `--https` | Emit https:// URLs instead of s3:// (only for `--format url`). |
| `-asf`, `--asf_search` | Use the `asf_search` package (ASF SearchAPI) instead of the default direct CMR query. Requires `asf_search` (`mamba install -c conda-forge asf_search` or `pip install asf-search`). |
| `--limit N` | Maximum number of granules to retrieve. |
| `-v`, `--verbose` | Print CMR / asf_search kwargs, granule count, etc. to stderr. |
| `--dryrun` | Print the CMR / asf_search query without logging in or searching, then exit. |

---

## Examples

```bash
# All GCOV URLs for ascending track 64 (latest release, s3)
seppo_nisar_search --track 64 --direction A

# HTTPS URLs instead of s3
seppo_nisar_search --track 64 --https

# Date range
seppo_nisar_search --start_time_after 2024-01-01 --start_time_before 2024-06-01

# Bounding box (upper-left / lower-right)
seppo_nisar_search --ullr -120 50 -100 40

# Bounding box (xmin ymin xmax ymax)
seppo_nisar_search --bbox -120 40 -100 50

# Point with buffer radius (degrees)
seppo_nisar_search --point -105.5 45.2 --buffer 2.0

# WKT polygon
seppo_nisar_search --wkt "POLYGON((-120 40,-100 40,-100 50,-120 50,-120 40))"

# GeoJSON file -- union all features
seppo_nisar_search --geojson aoi.geojson --union_geojson --group

# Grouped output to a directory (one file per track/dir/frame)
seppo_nisar_search --group -o /data/urls/

# GeoJSON output (includes both url and url_https)
seppo_nisar_search --track 64 --format geojson -o results.geojson

# KML output
seppo_nisar_search --ullr -120 50 -100 40 --format kml

# Pair product (GUNW) with secondary cycle
seppo_nisar_search --product GUNW --track 71 --direction A --frame 173 --cycle 3 --cycle2 5

# Specify CMR short name / pattern directly
seppo_nisar_search --short_name NISAR_L2_GCOV_BETA_V1 --track 64

# Restrict to a specific collection tier (post-filter)
seppo_nisar_search --track 64 --collection '%PROVISIONAL%'

# Include all collections and CRID versions
seppo_nisar_search --track 64 --allcrids

# Include Urgent Response (UR) products (excluded by default)
seppo_nisar_search --product RSLC --track 72 --frame 79 -ur

# Use asf_search (ASF SearchAPI) instead of direct CMR
seppo_nisar_search --product GCOV --track 47 --frame 23 -asf

# Dry-run (show the CMR / asf_search query without searching)
seppo_nisar_search --track 64 --dryrun
```

---

## Full help output

```
usage: seppo_nisar_search [-h] [--bucket [TEXT ...]] [--collection [NAME ...]]
                          [--mission [CODE ...]] [--inst_level [CODE ...]]
                          [--proctype [CODE ...]] [--product CODE [CODE ...]]
                          [--short_name [NAME ...]] [--cycle [INT ...]]
                          [--cycle2 [INT ...]] [--track [INT ...]]
                          [--direction [A|D ...]] [--frame [INT ...]]
                          [--mode [CODE ...]] [--polarization [CODE ...]]
                          [--observation_mode [CODE ...]] [--crid [CODE ...]]
                          [--accuracy [CODE ...]] [--coverage [CODE ...]]
                          [--sds [CODE ...]] [--counter [CODE ...]]
                          [--url_pattern PATTERN]
                          [--start_time_after DATETIME]
                          [--start_time_before DATETIME]
                          [--wkt WKT | --ullr UL_LON UL_LAT LR_LON LR_LAT | --bbox MIN_LON MIN_LAT MAX_LON MAX_LAT | --point LON LAT | --geojson FILE]
                          [--buffer DEG] [--union_geojson] [--group]
                          [--allcrids] [-ur] [--https] [-o PATH]
                          [--format {url,csv,json,geojson,kml}]
                          [--columns [COL ...]] [--limit N] [-asf] [-v]
                          [--dryrun]

SEPPO - Search NISAR product URLs via NASA Earthdata CMR (earthaccess).
Credentials are read from  the netrc; an interactive prompt is shown if
no entry is found.  Use --dryrun to inspect the CMR query without logging in.
For --format url: s3:// by default, --https for https:// URLs.
For all other formats: both url (s3) and url_https columns are included.

options:
  -h, --help            show this help message and exit

Column / metadata filters (all accept one or more values):
  --bucket [TEXT ...]   S3 bucket name(s). Supports LIKE wildcards (%).
  --collection [NAME ...]
                        Collection short name(s) to keep, e.g.
                        NISAR_L2_GCOV_PROVISIONAL_V1. Supports LIKE wildcards
                        (%), e.g. '%PROVISIONAL%'. Applied as a post-filter;
                        by default (no --collection) the latest release of
                        each scene is kept across all collections.
  --mission [CODE ...]  Mission code(s) (e.g. NISAR)
  --inst_level [CODE ...]
                        Instrument (L-band) and Processing level(s) (e.g. L1 L2)
  --proctype [CODE ...]
                        Processing type(s)
  --product CODE [CODE ...]
                        Product type(s) (e.g. GCOV RSLC GSLC SME2 RIFG RUNW
                        GUNW ROFF GOFF) (default: ['GCOV'])
  --short_name [NAME ...]
                        CMR short name(s) - overrides auto-construction from
                        --inst_level + --product. E.g. NISAR_L2_GCOV
  --cycle [INT ...]     Reference cycle number(s)
  --cycle2 [INT ...]    Secondary cycle number(s) for pair-acquisition products
  --track [INT ...]     Track / relative-orbit number(s)
  --direction [A|D ...]
                        Flight direction: A (ascending) or D (descending)
  --frame [INT ...]     Frame number(s)
  --mode [CODE ...]     Acquisition mode(s)
  --polarization [CODE ...]
                        Polarization code(s). Single-acquisition: 4-char code
                        e.g. DHDH, DVDV, SHNA. Pair products: 2-char e.g. DH.
  --observation_mode [CODE ...]
                        Observation mode code(s)
  --crid [CODE ...]     Composite release ID(s)
  --accuracy [CODE ...]
                        Accuracy code(s)
  --coverage [CODE ...]
                        Coverage code(s)
  --sds [CODE ...]      SDS code(s)
  --counter [CODE ...]  Counter value(s)
  --url_pattern PATTERN
                        LIKE pattern matched against url (e.g. '%GCOV%')

Time filters:
  --start_time_after DATETIME
                        Acquisition start >= DATETIME
  --start_time_before DATETIME
                        Acquisition start <= DATETIME

Spatial filters:
  --wkt WKT             OGC WKT geometry in WGS84
  --ullr UL_LON UL_LAT LR_LON LR_LAT
                        Bounding box from upper-left / lower-right corners
  --bbox MIN_LON MIN_LAT MAX_LON MAX_LAT
                        Bounding box in (xmin ymin xmax ymax) order
  --point LON LAT       Point in WGS84 lon/lat
  --geojson FILE        GeoJSON file. First feature used unless --union_geojson.
  --buffer DEG          Buffer radius in degrees for --point (1 deg ~ 111 km)
  --union_geojson       Union all GeoJSON features into a single geometry

Output:
  --group               Group by (track, direction, frame) ordered by start_time
  --allcrids            Return every version (all collections and CRIDs);
                        default keeps only the latest release per scene.
  -ur, --urgent_response
                        Include Urgent Response (UR) products from the
                        NISAR_UR_L1/L2 collections. Excluded by default.
  --https               Emit https:// URLs instead of s3://
  -o PATH, --output PATH
                        Output file path (or directory with --group)
  --format {url,csv,json,geojson,kml}
                        Output format (default: url)
  --columns [COL ...]   Columns for csv/json output (default: all)
  --limit N             Maximum number of granules to retrieve
  -asf, --asf_search    Use the asf_search package (ASF SearchAPI) instead of
                        the default direct CMR query.
  -v, --verbose         Print CMR / asf_search kwargs, granule count, etc. to
                        stderr
  --dryrun              Print the CMR / asf_search query without searching,
                        then exit
```
