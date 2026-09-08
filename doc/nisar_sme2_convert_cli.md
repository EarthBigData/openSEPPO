# seppo_nisar_sme2_convert -- CLI Reference

Convert NISAR SME2 (Soil Moisture Estimate, L3) HDF5 files to Cloud Optimized GeoTIFF (COG) or to a self-contained windowed HDF5 subset.

Every SME2 layer sits on one shared grid -- **EASE-Grid 2.0 Global (EPSG:6933)** at a 200 m posting -- so a single window covers the whole granule. Granules also carry their row/column window on the global EASE grid, so tiles from different dates and tracks align pixel-for-pixel with no resampling.

Layers are **discovered in each granule** rather than assumed: SME2 moved `surfaceQualityFlag` between release tiers, and a granule may carry only some of the algorithm candidates. Run `-lg` to see exactly what a file holds.

---

## Usage

```
seppo_nisar_sme2_convert [-h] [-i H5 [H5 ...]] [-o OUTPUT]
                         [-lyr {soilMoisture,algorithmCandidates,ancillaryData,radarData}]
                         [-vars VARS [VARS ...]] [--algorithm ALGORITHM] [-f {A,B}]
                         [-groups GROUPS [GROUPS ...]] [-lg]
                         [-of {COG,GTiff,h5}]
                         [-srcwin XOFF YOFF XSIZE YSIZE | -projwin ULX ULY LRX LRY]
                         [-projwin_srs CRS]
                         [-t_srs TARGET_SRS] [-tr XRES YRES] [--no_tap]
                         [--resample RESAMPLE] [-d DOWNSCALE] [--no_vrt]
                         [--profile PROFILE] [--input_profile INPUT_PROFILE]
                         [--output_profile OUTPUT_PROFILE]
                         [-j N] [--read_threads N] [--warp_threads N]
                         [-cache CACHE] [-keep] [-v]
```

---

## Arguments

### Input / Output

| Argument | Description |
|----------|-------------|
| `-i`, `--h5` | Input SME2 H5 URL(s) or path to a text file containing URLs (local, `s3://`, or `https://`). |
| `-o`, `--output` | Output directory path (S3 or local). Must end in `/` for batch processing. |
| `-of {COG,GTiff,h5}`, `--output_format` | Output format: `COG` (default), `GTiff` (BigTIFF), `h5` (self-contained subset). |

### Layer Selection

| Argument | Description |
|----------|-------------|
| `-lyr`, `--layer_group` | Which group of layers to rasterise: `soilMoisture` (default), `algorithmCandidates`, `ancillaryData`, `radarData`. Ignored for `-of h5`, which windows every group. |
| `-vars`, `--vars` | Layers to extract within the group. Default: the group's standard set, or every layer present. |
| `--algorithm` | Algorithm candidate for `-lyr algorithmCandidates` (e.g. `DSG`). Default: the first present. |
| `-f {A,B}`, `--freq` | Frequency for `-lyr radarData`. Default: the first present. |
| `-groups`, `--groups` | For `-of h5`: restrict which grid sub-groups the subset carries. The root soil-moisture layers are always kept. |
| `-lg`, `--list_grids` | Scan the first file, list the grid, groups and layers, then exit. |

### Layer groups

| Group | Layers |
|-------|--------|
| `soilMoisture` | `soilMoisture`, `soilMoistureUncertainty`, `retrievalQualityFlag`, `surfaceQualityFlag` |
| `algorithmCandidates` | Per-algorithm retrieval: `soilMoisture`, `soilMoistureUncertainty`, `retrievalQualityFlag`, `algorithmParameterBeta`, `algorithmParameterGamma` |
| `ancillaryData` | `landCover`, `localIncidenceAngle`, `localIncidenceAngleUncertainty`, `waterbodyFraction` |
| `radarData` | `sigma0HH/HV`, `noiseEquivalentBackscatterHH/HV`, `numberOfLooksHH/HV` |

Float layers are written as float32 with NaN nodata; integer layers (quality flags, land cover, looks) keep their native dtype and `_FillValue`.

### Subsetting

| Argument | Description |
|----------|-------------|
| `-srcwin XOFF YOFF XSIZE YSIZE` | Pixel subset window on the SME2 grid. |
| `-projwin ULX ULY LRX LRY` | Geographic subset window. |
| `-projwin_srs CRS` | CRS of the `-projwin` corners (e.g. `4326`). Lon/lat corners given without this are detected and reprojected rather than read as EASE-Grid metres. |

### Reprojection / Resampling

| Argument | Description |
|----------|-------------|
| `-t_srs`, `--target_srs` | Target CRS for output. Default: the native EASE-Grid. |
| `-tr XRES YRES`, `--target_res` | Output pixel size in target CRS units (with `-t_srs`). |
| `--no_tap` | Disable pixel-grid alignment of the reprojected origin. |
| `--resample` | Resampling for continuous layers on reprojection. Integer layers always use nearest. |
| `-d`, `--downscale` | Integer downscale factor (block reduce). |
| `--no_vrt` | Disable the per-snapshot multi-layer VRT. |

### Authentication, Threads, Caching

| Argument | Description |
|----------|-------------|
| `--profile`, `--input_profile`, `--output_profile` | AWS profile(s). ASF DAAC buckets and Earthdata HTTPS URLs switch to Earthdata credentials automatically. |
| `--no_time_series` | Disable the time-series VRT stacks built over a batch: one VRT per layer, one band per date, in date order, with a `.dates` sidecar. Repeat passes resolve to the same EASE-Grid window, so they stack with no resampling; dates that only partly cover the box are stacked on their union instead. |
| `-j N`, `--jobs N` | Granules converted concurrently in a batch, each in its own process. `1` converts them one at a time. Default: 4. |
| `--read_threads N` | Parallel connections for reading the selected layer windows out of one remote granule. Default: 1 (serial) -- an SME2 layer is only 16 chunks, so spawning readers costs more than it saves; raise it on a high-latency link. Ignored with `-j > 1`. |
| `--warp_threads N` | Threads for reprojection. Default: all cores. |
| `-cache`, `-keep` | Fetch the whole granule locally before reading, and optionally keep it. Off by default -- a windowed read moves a small fraction of the granule, so caching only pays off when several groups are converted from the same file. |
| `-v`, `--verbose` | Verbose output. |

---

## Examples

For a worked search-to-COG walkthrough over an irrigated area, see
[SME2 examples](nisar_sme2_convert_examples.md).

```bash
# List the grid, groups and layers in a granule
seppo_nisar_sme2_convert --h5 sme2.h5 -lg

# Soil moisture layers as COGs, subset to an AOI (lon/lat)
seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ \
    -projwin -117.2 37.8 -116.4 37.2 -projwin_srs 4326

# Self-contained windowed HDF5 subset (all groups)
seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ -of h5 \
    -projwin -117.2 37.8 -116.4 37.2 -projwin_srs 4326

# The DSG algorithm candidate, reprojected to WGS84
seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ \
    -lyr algorithmCandidates --algorithm DSG -t_srs 4326

# Radar backscatter (frequency A) as COGs
seppo_nisar_sme2_convert --h5 sme2.h5 -o out/ -lyr radarData -f A

# Straight from the DAAC (Earthdata credentials are detected)
seppo_nisar_sme2_convert \
    --h5 s3://sds-n-cumulus-prod-nisar-products/NISAR_L3_SME2_PROVISIONAL_V1/<granule>/<granule>.h5 \
    -o out/ -projwin -117.2 37.8 -116.4 37.2 -projwin_srs 4326
```

Find granules with `seppo_nisar_search --product SME2`.

---

## Output naming

```
<granule>-EBD_<group token>_<layer token>.tif
<granule>-EBD_SME2.h5
```

Group tokens are `sm`, `alg<ALGORITHM>` (e.g. `algDSG`), `anc`, and `rad<FREQ>` (e.g. `radA`). Layer tokens are short forms of the layer name (`sm`, `smunc`, `rqf`, `sqf`, `lc`, `lia`, `wbf`, `s0hh`, ...), listed by `-lg`.
