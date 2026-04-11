# openSEPPO

**Open SEPPO Tools -- Supporting Geospatial and Remote Sensing Data Processing**

openSEPPO is a growing set of open-source tools for processing and managing geospatial and
remote sensing data, with good support for NASA/ISRO NISAR products. The tools are **designed to
work standalone** (on-premise, your laptop, cloud instances, ...),  and to integrate for scaling with the
[SEPPO](https://earthbigdata.com/seppo) software by [Earth Big Data](https://earthbigdata.com).

---

## Tools

| Command | Description |
|---------|-------------|
| `seppo_nisar_rslc_convert` | Subset NISAR L-band RSLC HDF5 files directly from S3/HTTPS; output is compatible with isce3, GAMMA Remote Sensing, and SEPPO for interferometric processing; geographic bbox, pixel, and coordinate subsetting; quicklook generation |
| `seppo_nisar_gcov_convert` | Convert NISAR GCOV HDF5 to Cloud Optimized GeoTIFF (COG), BigTIFF, or HDF5 subset with optional sigma0 conversion, reprojection, downscaling, and VRT time-series stacking |
| `seppo_nisar_gcov_convert_S` | S-band variant of `seppo_nisar_gcov_convert` |
| `seppo_nisar_gslc_convert` | Convert NISAR GSLC HDF5 complex data to COG or HDF5 subset: power, amplitude, magnitude, wrapped phase, or raw complex SLC; HDF5 output preserves all metadata for isce3/GAMMA/SEPPO; supports subsetting, reprojection, downscaling, and VRT stacking |
| `seppo_nisar_coherence` | Compute pairwise interferometric coherence from co-registered NISAR GSLC complex SLC files with optional crop, downscale, and reprojection |
| `seppo_nisar_search` | Search NISAR product URLs via NASA Earthdata CMR |
| `seppo_earthaccess_credentials` | Manage NASA Earthdata S3 credentials and bearer token |

---

## TL;DR -- GCOV in 4 Steps

> Ideally run on an AWS EC2 instance in `us-west-2`.
> Outside `us-west-2`, add `--https` to the search command.

### 1. Install

```bash
mamba create -n openseppo -c conda-forge openseppo aria2
conda activate openseppo
```

### 2. Cache Earthdata credentials

```bash
seppo_earthaccess_credentials -t
```

### 3. Search GCOV data at a point and time range

```bash
seppo_nisar_search \
    --product GCOV \
    --point -155.27 19.42 \
    --start_time_after 2026-01-01 \
    --start_time_before 2026-04-01 \
    --https \
    --group \
    -o search_results/
```

### 4. Inspect a GCOV file

```bash
seppo_nisar_gcov_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5
```

### 5a. Subset to amplitude COG (default gamma0)

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_072_D_079_*.txt \
    -o output/gcov/ \
    -amp \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

### 5b. Subset to amplitude COG with sigma0 conversion

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_072_D_079_*.txt \
    -o output/gcov_sigma0/ \
    -amp -sigma0 \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

See [Installation](installation.md) for pip, local clone, and credential setup options.

---

## Quick Start -- All Product Types

All examples below use **Hawaii Volcanoes National Park (Kilauea)**, Track 072 Descending Frame 079.
This area has active lava flows, caldera structures, and tropical forest -- ideal for SAR.
Results should complete in **under 1 minute** from a laptop.

---

### Step 1: Search for NISAR Products

Search for available products over Hawaii Big Island and group by track/frame:

```bash
seppo_nisar_search \
    --product GCOV \
    --bbox -156.1 19.3 -154.8 20.3 \
    --start_time_after 2026-01-01 \
    --start_time_before 2026-04-01 \
    --https \
    --limit 30 \
    --group
```

This displays results grouped by track, direction, and frame:

```
=== Track: 072 | Direction: D | Frame: 079 | Product: GCOV ===
https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L2_PR_GCOV_009_072_D_079_...h5
https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L2_PR_GCOV_010_072_D_079_...h5
```

Save to a file for batch processing with `-o`:

```bash
seppo_nisar_search \
    --product GCOV \
    --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 \
    --start_time_before 2026-04-01 \
    --https \
    --group \
    -o search_results/
```

This creates `search_results/NISAR_GCOV_072_D_079_..._s3urls.txt` with one URL per line, ready for batch processing.

Search for other product types by changing `--product`:

```bash
# RSLC (radar-coordinates SLC for interferometry)
seppo_nisar_search --product RSLC --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 --start_time_before 2026-04-01 --https --group

# GSLC (geocoded SLC)
seppo_nisar_search --product GSLC --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 --start_time_before 2026-04-01 --https --group
```

---

### Step 2a: GCOV -- Backscatter Time Series to COG

Convert all GCOV acquisitions for Track 072 Frame 079 to amplitude COGs over Kilauea:

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_072_D_079_*.txt \
    -o output/gcov/ \
    -amp \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

This generates per-date COG files and automatically builds VRT time-series stacks.

To also generate sigma0 and reproject to WGS84:

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_072_D_079_*.txt \
    -o output/gcov_sigma0/ \
    -amp -sigma0 \
    -t_srs 4326 \
    -tr 0.0002 0.0002 \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

---

### Step 2b: GSLC -- Complex SLC to COG or HDF5

Convert a single GSLC acquisition to power COG:

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gslc/ \
    -pwr \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

Extract raw complex SLC for interferometry:

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gslc/ \
    -cslc \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -vars HH \
    -v
```

Subset to HDF5 (preserves complex data + all metadata for isce3/GAMMA/SEPPO):

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gslc/ \
    -of h5 \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

---

### Step 2c: RSLC -- Radar SLC Subset with Quicklook

Subset a single RSLC acquisition to HDF5 for interferometric processing with isce3, GAMMA, or SEPPO.
Includes a quicklook PNG showing detected backscatter over Kilauea caldera:

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/rslc/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH \
    -ql \
    -v
```

Output: 43 MB subsetted RSLC HDF5 + quicklook PNG (~32 seconds, no caching).

For a time series for InSAR:

```bash
seppo_nisar_search --product RSLC --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 --start_time_before 2026-04-01 \
    --https --group -o search_results/

seppo_nisar_rslc_convert \
    -i search_results/NISAR_RSLC_072_D_079_*.txt \
    -o output/rslc_stack/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH \
    -v
```

Each output is a self-contained RSLC HDF5 ready for pairwise interferometric processing.

---

## Documentation

### Getting Started

- [Quick Start](quickstart.md)
- [Installation](installation.md)

### Examples

- **[GCOV Processing Overview](gcov_processing_overview.md)** -- Detailed walkthrough
  of GCOV search, conversion, subsetting, reprojection, and time-series building
  in a Jupyter notebook.
- **[Hawaii Volcanoes examples](nisar_hawaii_examples.md)** -- Comprehensive search, inspect, and subset
  examples for RSLC, GSLC, and GCOV over Kilauea.

### CLI Reference

| Tool | Reference |
|------|-----------|
| `seppo_nisar_rslc_convert` | [CLI Reference](nisar_rslc_convert_cli.md) |
| `seppo_nisar_gcov_convert` | [CLI Reference](nisar_gcov_convert_cli.md) -- [Examples](nisar_gcov_convert_examples.md) -- [Dual-pol Ratio](ratio.md) |
| `seppo_nisar_gslc_convert` | [CLI Reference](nisar_gslc_convert_cli.md) |
| `seppo_nisar_coherence` | [CLI Reference](nisar_coherence_cli.md) |
| `seppo_nisar_search` | [CLI Reference](nisar_search_cli.md) |
| `seppo_earthaccess_credentials` | [CLI Reference](earthaccess_credentials_cli.md) |

---

## Useful Links

| Resource | Description |
|----------|-------------|
| [ASF Vertex](https://search.asf.alaska.edu) | Alaska Satellite Facility visual data search -- browse and download NISAR and other SAR products |
| [NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/) | NISAR product format specifications, algorithm documents, and data access guides |
| [NISAR Science](https://science.nasa.gov/mission/nisar/) | Official NASA NISAR mission site -- science overview, data products, and news |
| [NASA Earthdata sign-up](https://urs.earthdata.nasa.gov/users/new) | Register for a free NASA Earthdata account (required for data access) |
| [earthaccess](https://earthaccess.readthedocs.io) | Python library for NASA Earthdata authentication and S3 access (used internally by openSEPPO) |
| [TimeseriesSAR QGIS Plugin](https://github.com/EarthBigData/openSAR/tree/master/code/QGIS/v3/plugins) | Interactive time-series click/plot tool for SAR data in QGIS |

---

## License

Apache License 2.0 -- (c) 2026 Earth Big Data LLC | [earthbigdata.com](https://earthbigdata.com)
