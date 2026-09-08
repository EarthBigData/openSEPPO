# openSEPPO

**Open SEPPO Tools -- Supporting Geospatial and Remote Sensing Data Processing**

openSEPPO is a growing set of open-source tools for processing and managing geospatial and
remote sensing data, with good support for NASA/ISRO NISAR products. The tools are **designed to
work standalone** (on-premise, your laptop, cloud instances, ...),  and to integrate for scaling with the
[SEPPO](https://earthbigdata.com/seppo) software by [Earth Big Data](https://earthbigdata.com).

---

**Contents**

- [Tools](#tools)
- [TL;DR -- GCOV in 4 Steps](#tldr-gcov-in-4-steps)
- [Command-Line Examples by Product Type](#command-line-examples-by-product-type)
  - [GCOV -- Geocoded Backscatter (Covariance)](#gcov-geocoded-backscatter-covariance)
  - [GSLC -- Geocoded Single Look Complex Data](#gslc-geocoded-single-look-complex-data)
  - [RSLC -- Radar-coordinates SLC](#rslc-radar-coordinates-slc)
  - [GUNW -- Geocoded Unwrapped Interferogram (InSAR)](#gunw-geocoded-unwrapped-interferogram-insar)
  - [SME2 -- Soil Moisture (L3)](#sme2-soil-moisture-l3)
- [Visualizing COGs and VRTs in GIS](#visualizing-cogs-and-vrts-in-gis)
- [Documentation](#documentation)
  - [Getting Started](#getting-started)
  - [Examples](#examples)
  - [CLI Reference](#cli-reference)
- [Useful Links](#useful-links)

---

## Tools

| Command | Description |
|---------|-------------|
| `seppo_nisar_rslc_convert` | Subset NISAR L-band RSLC HDF5 files directly from S3/HTTPS; output is compatible with isce3, GAMMA Remote Sensing, and SEPPO for interferometric processing; geographic bbox, pixel, and coordinate subsetting; quicklook generation |
| `seppo_nisar_gcov_convert` | Convert NISAR GCOV HDF5 to Cloud Optimized GeoTIFF (COG), BigTIFF, or HDF5 subset with optional sigma0 conversion, reprojection, downscaling, and VRT time-series stacking |
| `seppo_nisar_gcov_convert_S` | S-band variant of `seppo_nisar_gcov_convert` (Beta release) |
| `seppo_nisar_gslc_convert` | Convert NISAR GSLC HDF5 complex data to COG or HDF5 subset: power, amplitude, magnitude, wrapped phase, or raw complex SLC; HDF5 output preserves all metadata for isce3/GAMMA/SEPPO; supports subsetting, reprojection, downscaling, and VRT stacking |
| `seppo_nisar_gunw_convert` | Convert NISAR GUNW (L2 InSAR pair) HDF5 to COG or HDF5 subset: unwrapped/wrapped interferogram, coherence, connected components, and pixel offsets across the three geocoded grids; subsetting, reprojection, downscaling, VRT time-series stacking, and an experimental coseismic quick-look (`--report`) |
| `seppo_nisar_sme2_convert` | Convert NISAR SME2 (L3 soil moisture) HDF5 to COG or HDF5 subset on the EASE-Grid 2.0 grid: soil moisture, algorithm candidates, ancillary and quality layers; subsetting, reprojection, downscaling, concurrent batch conversion, and VRT time-series stacking |
| `seppo_nisar_coherence` | Compute pairwise interferometric coherence from co-registered NISAR GSLC complex SLC files with optional crop, downscale, and reprojection |
| `seppo_nisar_search` | Search NISAR product URLs via NASA Earthdata CMR |
| `seppo_earthaccess_credentials` | Manage NASA Earthdata S3 credentials and bearer token |

---

## TL;DR -- GCOV in 4 Steps

> Ideally run on an AWS EC2 instance in `us-west-2`.
> Outside `us-west-2`, add `--https` to the search command.

### 1. Install

```bash
mamba create -n openseppo -c conda-forge openseppo aria2 matplotlib
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
    --start_time_after 2026-06-01 \
    --start_time_before 2026-08-01 \
    --https \
    --group \
    -o search_results/
```

### 4. Inspect a GCOV file

```bash
seppo_nisar_gcov_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L2_PR_GCOV_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5
```

### 5a. Subset to amplitude COG (default gamma0)

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_151_A_011_*.txt \
    -o output/gcov/ \
    -amp \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

### 5b. Subset to amplitude COG with sigma0 conversion

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_151_A_011_*.txt \
    -o output/gcov_sigma0/ \
    -amp -sigma0 \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

See [Installation](installation.md) for pip, local clone, and credential setup options.

---

## Command-Line Examples by Product Type

All examples below use **Hawaii Volcanoes National Park (Kilauea)**, Track 151 Ascending Frame 011.
This area has active lava flows, caldera structures, and tropical forest -- ideal for SAR.
Results should complete in **under 1 minute** from a laptop.

---

### GCOV -- Geocoded Backscatter (Covariance)

#### Search

```bash
seppo_nisar_search \
    --product GCOV \
    --bbox -156.1 19.3 -154.8 20.3 \
    --start_time_after 2026-06-01 \
    --start_time_before 2026-08-01 \
    --https \
    --limit 30 \
    --group
```

Save Track 151 Frame 011 URLs for batch processing:

```bash
seppo_nisar_search \
    --product GCOV \
    --track 151 --frame 11 --direction A --mode 4005 \
    --start_time_after 2026-06-01 \
    --start_time_before 2026-08-01 \
    --https --group \
    -o search_results/
```

#### Inspect

```bash
seppo_nisar_gcov_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L2_PR_GCOV_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5
```

#### Subset -- single date, gamma0 dB COG

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L2_PR_GCOV_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5 \
    -o output/gcov/ \
    -dB \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

Output: one COG per polarisation (HHHH, HVHV) in gamma0 dB.  Open directly in QGIS or any GDAL-compatible viewer.

#### Subset -- single date, gamma0 AMP COG, add a dual-pol ratio band

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L2_PR_GCOV_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5 \
    -o output/gcov/ \
    -amp -dpratio \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

Output: amplitude COGs for HHHH, HVHV, plus an additional HHHH/HVHV ratio band.  High ratio values indicate dominant surface scattering (bare lava), low values indicate volume scattering (forest canopy).

#### Subset -- amplitude COG time series

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_151_A_011_*.txt \
    -o output/gcov/ \
    -amp \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

Generates per-date COG files and automatically builds VRT time-series stacks.

#### Subset -- sigma0 reprojected to WGS84

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_151_A_011_*.txt \
    -o output/gcov_sigma0/ \
    -amp -sigma0 \
    -t_srs 4326 -tr 0.0002 0.0002 \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

---

### GSLC -- Geocoded Single Look Complex Data

#### Search

```bash
seppo_nisar_search \
    --product GSLC \
    --track 151 --frame 11 --direction A --mode 4005 \
    --start_time_after 2026-06-01 \
    --start_time_before 2026-08-01 \
    --https --group \
    -o search_results/
```

#### Inspect

```bash
seppo_nisar_gslc_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_PROVISIONAL_V1/NISAR_L2_PR_GSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L2_PR_GSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5
```

#### Subset -- power COG

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_PROVISIONAL_V1/NISAR_L2_PR_GSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L2_PR_GSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5 \
    -o output/gslc/ \
    -pwr \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

#### Subset -- raw complex SLC for interferometry

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_PROVISIONAL_V1/NISAR_L2_PR_GSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L2_PR_GSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5 \
    -o output/gslc/ \
    -cslc \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -vars HH \
    -v
```

#### Subset -- HDF5 (preserves complex data + all metadata)

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_PROVISIONAL_V1/NISAR_L2_PR_GSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L2_PR_GSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5 \
    -o output/gslc/ \
    -of h5 \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

---

### RSLC -- Radar-coordinates SLC

#### Search

```bash
seppo_nisar_search \
    --product RSLC \
    --track 151 --frame 11 --direction A --mode 4005 \
    --start_time_after 2026-06-01 \
    --start_time_before 2026-08-01 \
    --https --group \
    -o search_results/
```

#### Inspect

```bash
seppo_nisar_rslc_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_PROVISIONAL_V1/NISAR_L1_PR_RSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L1_PR_RSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5
```

#### Subset -- HH with quicklook

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_PROVISIONAL_V1/NISAR_L1_PR_RSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L1_PR_RSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5 \
    -o output/rslc/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH \
    -ql -v
```

Output: 43 MB subsetted RSLC HDF5 + quicklook PNG (~32 seconds, no caching).
Compatible with isce3, GAMMA Remote Sensing, and SEPPO.

#### Subset -- dual-pol HH+HV with quicklook

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_PROVISIONAL_V1/NISAR_L1_PR_RSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001/NISAR_L1_PR_RSLC_025_151_A_011_4005_DHDH_A_20260718T155041_20260718T155059_P05023_N_P_J_001.h5 \
    -o output/rslc/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH HV \
    -ql -v
```

Output: 86 MB subsetted RSLC HDF5 + quicklook PNG with 4 panels (~59 seconds).
The quicklook shows HH (co-pol) and HV (cross-pol) side by side, each with
single-look and 5x5 multilooked views.  HV highlights vegetation (volume
scattering) while HH shows surface roughness.

#### Subset -- time series for InSAR

```bash
seppo_nisar_rslc_convert \
    -i search_results/NISAR_RSLC_151_A_011_*.txt \
    -o output/rslc_stack/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH \
    -v
```

Each output is a self-contained RSLC HDF5 ready for pairwise interferometric processing.

---

### GUNW -- Geocoded Unwrapped Interferogram (InSAR)

GUNW is an L2 InSAR **pair** product -- one granule holds the interferogram between a
reference and a secondary acquisition. The example below leaves Kilauea and uses the
**Venezuela M7.2 / M7.5 earthquakes of 2026-06-24** (track 162 ascending, frame 007),
where the coseismic signal is the point of the product. See the full
[GUNW examples](nisar_gunw_convert_examples.md) for the stack and `-of h5` workflows.

#### Search

```bash
seppo_nisar_search --product GUNW \
    --point -68.60 10.30 --track 162 \
    --https --format url -o gunw_urls.txt
```

`--track 162` limits the search to a single track, so the returned pairs share one
geometry and stack. A GUNW name carries two acquisition datetimes, so match the cycle
and frame fields (not a date) to pick the pair spanning the event:

```bash
grep _022_162_A_007_023_ gunw_urls.txt > gunw_co.txt
```

#### Inspect

```bash
seppo_nisar_gunw_convert -lg -i gunw_co.txt
```

GUNW is three independent grids at two resolutions (`unwrappedInterferogram` and
`pixelOffsets` at 80 m, `wrappedInterferogram` at 20 m). Raster output is one grid
per run, chosen with `--layer_group` (default `unwrappedInterferogram`).

#### Subset -- unwrapped phase + coherence COGs

```bash
seppo_nisar_gunw_convert -i gunw_co.txt -o gunw_co_out/ \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326 \
    -vars unwrappedPhase coherenceMagnitude
```

Output stays on the granule's native UTM grid (EPSG:32619, 80 m), which lets pairs from
the same track and frame stack without resampling. `-vars` selects layers within the
group; omitted, the default set is `unwrappedPhase`, `coherenceMagnitude`,
`connectedComponents`. Add `-t_srs`/`-tr` to reproject.

#### Subset -- a stack of pairs with the time-series VRT

```bash
seppo_nisar_gunw_convert -i gunw_series.txt -o gunw_ts/ \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326 \
    -vars unwrappedPhase
```

A batch writes one VRT per layer with one band per pair, ordered by reference
acquisition. Each band is a single pair's interferogram -- nothing is summed or
differenced. `-of h5` writes a self-contained windowed subset of all three grids at once.

---

### SME2 -- Soil Moisture (L3)

SME2 is the L3 soil moisture product, one granule per acquisition on a global
**EASE-Grid 2.0** (EPSG:6933, 200 m) that repeats exactly across dates. The example
below uses centre-pivot irrigation in the **James River valley near Huron, South Dakota**.
See the full [SME2 examples](nisar_sme2_convert_examples.md) for the reprojected and
full-frame workflows.

#### Search

```bash
seppo_nisar_search --product SME2 \
    --point -98.35 44.55 --track 171 \
    --start_time_after 2026-05-01 --start_time_before 2026-09-01 \
    --https --format url -o sd_urls.txt
```

`--track 171` limits the search to a single track, so the returned granules share one
geometry and stack.

#### Inspect

```bash
seppo_nisar_sme2_convert -lg -i sd_urls.txt
```

Layers are discovered from the granule, not assumed -- which algorithm candidates exist
and which group each quality flag sits in vary between granules and release tiers.
`--layer_group` selects `soilMoisture` (default), `algorithmCandidates`, `ancillaryData`,
or `radarData`.

#### Subset -- soil moisture COG time series

```bash
seppo_nisar_sme2_convert -i sd_urls.txt -o sd_ts/ \
    -projwin -98.66 44.77 -98.04 44.33 -projwin_srs 4326 \
    -vars soilMoisture
```

Because the EASE-Grid is absolute, repeat passes of a frame resolve to the identical
window and stack with no resampling. A batch converts up to `-j` granules at once
(4 by default) and writes one VRT per layer with one band per date. Add
`-t_srs EPSG:4326 -tr 0.002 0.002` to reproject onto a WGS84 lattice.

---

## Visualizing COGs and VRTs in GIS

openSEPPO outputs Cloud Optimized GeoTIFFs (COGs) and GDAL Virtual Rasters (VRTs)
that open directly in any GDAL-compatible GIS application.

### QGIS

**Single file:**  Drag and drop a `.tif` or `.vrt` file from your file browser into the
QGIS map canvas.  Or use *Layer > Add Layer > Add Raster Layer* and browse to the file.

**Time-series VRT:**  The time-series VRT stacks contain one band per date.
Open the VRT, then use the [TimeseriesSAR QGIS Plugin](https://github.com/EarthBigData/openSAR/tree/master/code/QGIS/v3/plugins)
to click anywhere on the map and plot the backscatter time series interactively.

**S3 output (option 1 -- /vsis3/):**  Run `seppo_nisar_gcov_convert -S -vsis3` to list
output paths as `/vsis3/` URIs.  Paste them into QGIS via *Layer > Add Layer > Add Raster Layer*
using the URI as the source.

**S3 output (option 2 -- cloud protocol):**  In QGIS, go to *Layer > Add Layer > Add Raster Layer*,
set *Source type* to **Protocol: HTTP(S), cloud, etc.**, select *Type:* **AWS S3**, enter the
*Bucket or Container* name (from the `---> Bucket:` output line) and the *Object Key*
(from the VRT/TIF listing).

### ArcGIS Pro

**Single file:**  Use *Map > Add Data > Data* and browse to the `.tif` file.  COGs are
natively supported in ArcGIS Pro 2.x+.

**VRT files:**  ArcGIS Pro supports GDAL VRTs via the *Raster Dataset* option.  Add the `.vrt`
file the same way as a GeoTIFF.  For time-series VRTs, each band appears as a separate
layer in the raster properties.

**S3 output:**  Configure an S3 cloud storage connection via
*Insert > Connections > Cloud Storage Connection*, then browse to the COGs.

### Command-line (gdal)

```bash
# Quick preview
gdalinfo output/gcov/NISAR_..._AMP.tif

# Convert to PNG for quick viewing
gdal_translate -of PNG -scale output/gcov/NISAR_..._AMP.tif preview.png
```

---

## Documentation

### Getting Started

- [Installation](installation.md)
- [GCOV and GSLC Workflows](quickstart.md) -- step-by-step GCOV and GSLC workflows with search, conversion, and time-series stacking

### Examples

- **[Hawaii Volcanoes examples](nisar_hawaii_examples.md)** -- Comprehensive command-line examples
  for RSLC, GSLC, and GCOV search, inspect, and subset over Kilauea.
- **[Python API / Jupyter Integration](gcov_processing_overview.md)** -- Use openSEPPO
  tools as a Python API in scripts and Jupyter notebooks.  Demonstrates programmatic
  data search, GCOV conversion, subsetting, and time-series building.

### CLI Reference

| Tool | Reference |
|------|-----------|
| `seppo_nisar_rslc_convert` | [CLI Reference](nisar_rslc_convert_cli.md) |
| `seppo_nisar_gcov_convert` | [CLI Reference](nisar_gcov_convert_cli.md) -- [Examples](nisar_gcov_convert_examples.md) -- [Dual-pol Ratio](ratio.md) |
| `seppo_nisar_gslc_convert` | [CLI Reference](nisar_gslc_convert_cli.md) |
| `seppo_nisar_gunw_convert` | [CLI Reference](nisar_gunw_convert_cli.md) -- [Examples](nisar_gunw_convert_examples.md) |
| `seppo_nisar_sme2_convert` | [CLI Reference](nisar_sme2_convert_cli.md) -- [Examples](nisar_sme2_convert_examples.md) |
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
