# seppo_nisar_gcov_convert — Examples

`seppo_nisar_gcov_convert` converts NISAR GCOV HDF5 files to Cloud Optimized GeoTIFF (COG),
BigTIFF (GTiff), or HDF5 subset (h5), with optional reprojection, downscaling, subsetting,
dual-pol ratio output, and VRT time-series stacking.

---

## Contents

- [Quick reference](#quick-reference)
- [Output modes](#output-modes)
- [Inspecting available grids](#inspecting-available-grids)
- [Selecting frequency and polarization](#selecting-frequency-and-polarization)
- [Batch processing from a URL list](#batch-processing-from-a-url-list)
- [Reprojection](#reprojection)
- [Downscaling](#downscaling)
- [Dual-pol ratio](#dual-pol-ratio)
- [Subsetting](#subsetting)
- [Output format](#output-format)
- [VRT time-series management](#vrt-time-series-management)
- [Caching remote files locally](#caching-remote-files-locally)
- [Authentication](#authentication)
- [Parallel I/O threads](#parallel-io-threads)
- [Verbose output](#verbose-output)
- [File naming conventions](#file-naming-conventions)
- [Common combined workflows](#common-combined-workflows)

---

## Quick reference

```
seppo_nisar_gcov_convert -i <input> -o <output> [options]
```

Input (`-i`) accepts:
- A single HDF5 file path or URL
- One or more HDF5 paths/URLs as separate arguments
- A text file where each line is a path or URL

Output (`-o`) is a local directory or an S3 prefix (must end in `/` for batch).

---

## Output modes

| Flag | Type | Conversion from dB |
|------|------|--------------------|
| *(none / `-pwr`)* | float32 | Linear power (default) |
| `-dB` | float32 | `dB = 10·log10(pwr)` |
| `-amp` | uint16 | `dB = 20·log10(amp) − 83`; nodata=0 |
| `-DN` | uint8 | `dB = −31.15 + DN × 0.15`; nodata=0 |

```bash
# Default: linear power (float32, nodata=NaN)
seppo_nisar_gcov_convert -i file.h5 -o out/

# Decibel float32
seppo_nisar_gcov_convert -i file.h5 -o out/ -dB

# Amplitude uint16
seppo_nisar_gcov_convert -i file.h5 -o out/ -amp

# DN uint8 (compact, good for quick browse)
seppo_nisar_gcov_convert -i file.h5 -o out/ -DN
```

---

## Inspecting available grids

Before converting, list the frequencies and polarizations in the file:

```bash
seppo_nisar_gcov_convert -i file.h5 -o out/ -lg
```

---

## Selecting frequency and polarization

```bash
# Frequency B instead of default A
seppo_nisar_gcov_convert -i file.h5 -o out/ -f B

# Extract only HHHH and HVHV from frequency A
seppo_nisar_gcov_convert -i file.h5 -o out/ -vars HHHH HVHV
```

---

## Batch processing from a URL list

```bash
# urls.txt contains one H5 URL per line
seppo_nisar_gcov_convert -i urls.txt -o s3://my-bucket/nisar/out/

# Multiple URLs directly on the command line
seppo_nisar_gcov_convert \
    -i s3://bucket/scene1.h5 s3://bucket/scene2.h5 \
    -o s3://my-bucket/nisar/out/
```

Authentication for NASA Earthdata S3 and HTTPS URLs is detected automatically.
For ASF DAAC S3 buckets and Earthdata HTTPS hosts, credentials are set via the
`~/.netrc` file or the `seppo_earthaccess_credentials` helper (see below).

---

## Reprojection

```bash
# Reproject to WGS84 geographic coordinates (EPSG:4326)
seppo_nisar_gcov_convert -i file.h5 -o out/ -t_srs 4326

# Reproject to a named EPSG, explicit pixel size in degrees (~100 m)
seppo_nisar_gcov_convert -i file.h5 -o out/ -t_srs 4326 -tr 0.001 0.001

# Change resampling method (default: cubic)
seppo_nisar_gcov_convert -i file.h5 -o out/ -t_srs 4326 --resample bilinear

# Fill interior NaN holes (e.g. from burst gaps) during reprojection
# Frame-boundary nodata is preserved; only interior isolated holes are filled
seppo_nisar_gcov_convert -i file.h5 -o out/ -t_srs 4326 --fill_holes

# Disable pixel-grid alignment (tap) — only relevant with -t_srs
seppo_nisar_gcov_convert -i file.h5 -o out/ -t_srs 4326 --no_tap

# Control number of threads for reprojection (default: all cores)
seppo_nisar_gcov_convert -i file.h5 -o out/ -t_srs 4326 --warp_threads 4
```

`--fill_holes` also works without `-t_srs` (fills holes in the native UTM grid).

---

## Downscaling

Downscale factor applies integer block averaging before writing. Useful for
quick-look or browse images.

```bash
# 2× downscale (~60 m from 30 m native)
seppo_nisar_gcov_convert -i file.h5 -o out/ -d 2

# 20× downscale for thumbnail in DN with dual-pol ratio (no separate band files)
seppo_nisar_gcov_convert -i file.h5 -o out/ -DN -d 20 -dpratio --no_single_bands
```

---

## Dual-pol ratio

Computes the like-pol / cross-pol power ratio for DH (HHHH/HVHV) or DV (VVVV/VHVH)
acquisitions. See [convert.md](convert.md) for ratio formulas per mode.

```bash
# Dual-pol ratio with default power mode (float32)
seppo_nisar_gcov_convert -i file.h5 -o out/ -dpratio

# Dual-pol ratio in dB
seppo_nisar_gcov_convert -i file.h5 -o out/ -dB -dpratio

# 3-band browse COG (HH, HV, ratio) at 20× downscale, no separate band files
seppo_nisar_gcov_convert -i file.h5 -o out/ -DN -d 20 -dpratio --no_single_bands
```

---

## Subsetting

Two mutually exclusive methods:

```bash
# Pixel window: xoff yoff xsize ysize
seppo_nisar_gcov_convert -i file.h5 -o out/ -srcwin 1000 2000 512 512

# Geographic window in native UTM CRS: ULX ULY LRX LRY
seppo_nisar_gcov_convert -i file.h5 -o out/ -projwin 400000 4200000 450000 4150000

# Geographic subset in lon/lat with reprojection to WGS84 at ~22 m resolution
seppo_nisar_gcov_convert -i file.h5 -o out/ \
    -projwin -120.5 37.2 -119.8 36.7 -t_srs 4326 -tr 0.0002 0.0002
```

---

## Output format

```bash
# Cloud Optimized GeoTIFF (default)
seppo_nisar_gcov_convert -i file.h5 -o out/ -of COG

# BigTIFF (no internal overview tiling)
seppo_nisar_gcov_convert -i file.h5 -o out/ -of GTiff

# Raw HDF5 subset
seppo_nisar_gcov_convert -i file.h5 -o out/ -of h5
```

---

## VRT time-series management

By default, per-snapshot VRTs and a multi-temporal time-series VRT stack are
generated alongside the COGs.

```bash
# Disable per-snapshot VRTs (time-series VRT still built from COG filenames)
seppo_nisar_gcov_convert -i file.h5 -o out/ --no_vrt

# Disable time-series VRT stack
seppo_nisar_gcov_convert -i file.h5 -o out/ --no_time_series

# Save multi-band COG (one file, bands = polarizations) instead of separate files
seppo_nisar_gcov_convert -i file.h5 -o out/ --no_single_bands

# Rebuild VRTs for all existing COGs in the output folder (no reprocessing)
seppo_nisar_gcov_convert -o out/ -ro

# Process new files AND rebuild master VRTs to include all old + new timesteps
seppo_nisar_gcov_convert -i new.h5 -o out/ -R
```

---

## Caching remote files locally

For full-frame reads of remote files (S3/HTTPS), local caching is auto-enabled
(`/dev/shm` or `/tmp`). Override explicitly:

```bash
# Auto temp directory on /dev/shm or /tmp
seppo_nisar_gcov_convert -i https://data.earthdatacloud.nasa.gov/... -o out/ -cache y

# Specific cache directory
seppo_nisar_gcov_convert -i https://... -o out/ -cache /scratch/cache/

# Keep cached file after processing
seppo_nisar_gcov_convert -i https://... -o out/ -cache /scratch/cache/ -keep

# Disable caching (e.g. when subsetting, only a small window is read)
seppo_nisar_gcov_convert -i https://... -o out/ -projwin 400000 4200000 450000 4150000
```

Caching is automatically skipped when `-srcwin` or `-projwin` is specified.

---

## Authentication

AWS credentials are resolved in this order:
1. Earthdata credentials (auto-detected for ASF DAAC S3 buckets and Earthdata HTTPS hosts)
2. Named AWS profile (`--profile`, `--input_profile`, `--output_profile`)
3. Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`)

```bash
# Named profile for both input and output
seppo_nisar_gcov_convert -i s3://... -o s3://my-bucket/out/ --profile my-profile

# Separate profiles for input (Earthdata DAAC) and output (own bucket)
seppo_nisar_gcov_convert \
    -i s3://sds-n-cumulus-prod-nisar-products/... \
    -o s3://my-bucket/out/ \
    --output_profile my-profile

# Set Earthdata S3 credentials in the shell environment before running
eval $(seppo_earthaccess_credentials -s)
seppo_nisar_gcov_convert -i s3://sds-n-cumulus-prod-nisar-products/... -o out/

# Generate/refresh Earthdata bearer token (used for HTTPS downloads)
seppo_earthaccess_credentials -t
```

See `seppo_earthaccess_credentials --help` for full credential helper usage.

---

## Parallel I/O threads

```bash
# 16 parallel S3/HTTPS read connections (default: 8)
seppo_nisar_gcov_convert -i urls.txt -o out/ --read_threads 16
```

---

## Verbose output

```bash
seppo_nisar_gcov_convert -i file.h5 -o out/ -v
```

---

## File naming conventions

All output files preserve the original NISAR HDF5 base name and append an
`-EBD_<freq>_<pol>_<mode>` suffix before the file extension.

### NISAR base name tokens

GCOV (single-acquisition) filenames contain 18 underscore-separated tokens:

```
NISAR_<il>_<pt>_<prod>_<cycle>_<track>_<dir>_<frame>_<mode>_<pol>_<obs>_<start>_<end>_<crid>_<acc>_<cov>_<sds>_<ctr>
 [0]  [1]  [2]   [3]    [4]    [5]    [6]   [7]   [8]   [9]  [10]  [11]   [12]   [13]  [14]  [15]  [16]  [17]
```

Example:
```
NISAR_L2_PR_GCOV_015_172_D_065_4005_DHDH_A_20260121T031851_20260121T031926_P05006_N_F_J_001
```

| Index | Field | Example | Description |
|-------|-------|---------|-------------|
| 1 | `il` | `L2` | Instrument and processing level |
| 2 | `pt` | `PR` | Processing type |
| 3 | `prod` | `GCOV` | Product name |
| 4 | `cycle` | `015` | Cycle number (3-digit) |
| 5 | `track` | `172` | Track / relative-orbit number (3-digit) |
| 6 | `dir` | `A` / `D` | Pass direction: Ascending / Descending |
| 7 | `frame` | `065` | Frame number (3-digit) |
| 8 | `mode` | `4005` | Acquisition mode code |
| 9 | `pol` | `DHDH` | Polarization code (4-char: freq-A + freq-B; see below) |
| 10 | `obs` | `A` | Observation mode |
| 11 | `start` | `20260121T031851` | Acquisition start time (UTC, `YYYYMMDDTHHmmss`) |
| 12 | `end` | `20260121T031926` | Acquisition end time (UTC) |
| 13 | `crid` | `P05006` | Composite Release ID |
| 14 | `acc` | `N` | Accuracy flag |
| 15 | `cov` | `F` | Coverage flag |
| 16 | `sds` | `J` | SDS code |
| 17 | `ctr` | `001` | File counter |

The polarization token (index 9) is a 4-character code combining the polarization
of frequency A (first 2 chars) and frequency B (last 2 chars):

| 2-char code | Meaning |
|-------------|---------|
| `SH` | Single H-pol |
| `SV` | Single V-pol |
| `DH` | Dual H-pol (HH + HV) |
| `DV` | Dual V-pol (VV + VH) |
| `QP` | Quad-pol (HH + HV + VV + VH) |
| `NA` | Frequency not operated |

Examples: `DHDH` (both frequencies dual-H), `SHNA` (freq A single-H, freq B off),
`QPDH` (freq A quad-pol, freq B dual-H).

### EBD suffix and polarization naming

The `-EBD_<freq>_<pol>_<mode>` suffix is appended to the NISAR base name.

The `pol` field uses **2-character** lowercase prefixes for single- and dual-pol acquisitions
(`HHHH` → `hh`, `HVHV` → `hv`, etc.), and **full 4-character** lowercase variable names
for quad-pol (QP) acquisitions (`HHHH` → `hhhh`, `HHVV` → `hhvv`, etc.).

QP is detected from token 9 of the filename: frequency A → starts with `QP` (e.g. `QPQP`);
frequency B → ends with `QP`.

| Acquisition | Example `-vars` | `pol` per file |
|-------------|-----------------|----------------|
| DH dual-pol | `HHHH HVHV` | `hh`, `hv` |
| DV dual-pol | `VVVV VHVH` | `vv`, `vh` |
| QP | `HHHH VVVV HHVV` | `hhhh`, `vvvv`, `hhvv` |

For multi-band output (`--no_single_bands`) the `pol` field is the concatenation of all
per-band pol strings in extraction order:

| Acquisition | Example `-vars` | multi-band `pol` |
|-------------|-----------------|------------------|
| DH dual-pol | `HHHH HVHV` | `hhhv` |
| QP | `HHHH VVVV HHVV` | `hhhhvvvvhhvv` |

The `mode` field matches the output mode flag: `pwr`, `dB`, `AMP`, `DN`.

### Single-band COG (default)

```
# DH dual-pol:
<NISAR_BASE>-EBD_A_hh_dB.tif
<NISAR_BASE>-EBD_A_hv_dB.tif

# QP (-vars HHHH VVVV HHVV):
<NISAR_BASE>-EBD_A_hhhh_pwr.tif
<NISAR_BASE>-EBD_A_vvvv_pwr.tif
<NISAR_BASE>-EBD_A_hhvv_pwr.tif
```

### Multi-band COG (`--no_single_bands`)

```
# DH dual-pol:
<NISAR_BASE>-EBD_A_hhhv_dB.tif

# QP (-vars HHHH VVVV HHVV):
<NISAR_BASE>-EBD_A_hhhhvvvvhhvv_pwr.tif
```

### Dual-pol ratio (`-dpratio`)

Three files: like-pol, cross-pol, and ratio band:

```
<NISAR_BASE>-EBD_A_hh_dB.tif
<NISAR_BASE>-EBD_A_hv_dB.tif
<NISAR_BASE>-EBD_A_hhhvra_dB.tif   # DH: HHHH/HVHV
```

With `--no_single_bands`, a single 3-band COG (band 1 = like-pol, 2 = cross-pol, 3 = ratio):

```
<NISAR_BASE>-EBD_A_hhhvra_dB.tif
```

### HDF5 subset (`-of h5`)

```
<NISAR_BASE>-EBD_A_hhhv.h5
```

### Per-snapshot VRT

One VRT per acquisition covering all polarizations:

```
<NISAR_BASE>-EBD_A_hhhv_dB.vrt
```

### Per-track time-series VRT

Built after processing; spans all cycles on the same track and direction.
Cycle and frame ranges appear as `min-max` when more than one value is present:

```
NISAR_<il>_<pt>_<prod>_<cycle_range>_<track>_<dir>_<frame_range>_<mode>_<pol>_<obs>_<min_start>_<max_end>_<acc>_<crid_prefix>-EBD_<freq>_<pol>_<mode>.vrt
```

Example (track 064 ascending, cycles 001–005):

```
NISAR_L_S_GCOV_001-005_064_A_003_2000_DH_20_20250101T120000_20251201T120000_M_P-EBD_A_hh_dB.vrt
```

### Spatial mosaic VRT (multiple frames per cycle)

When a track covers multiple frames, a per-cycle mosaic VRT is built first and used
as a source for the time-series VRT:

```
NISAR_..._<cycle>_<track>_<dir>_<frame_min>-<frame_max>_...-EBD_<freq>_<pol>_<mode>_mosaic.vrt
```

### Combined ascending + descending time-series VRT

When exactly one ascending and one descending track are present in the output folder,
an additional combined VRT is built that interleaves both pass directions chronologically.
Track and direction fields span both:

```
NISAR_..._<track_A>-<track_D>_A-D_...-EBD_<freq>_<pol>_<mode>.vrt
```

### Dates sidecar file

Each time-series VRT is accompanied by a `.dates` file listing one acquisition
date per line (ISO `YYYY-MM-DD`), in band order:

```
<time_series_vrt_name>.dates
```

---

## Common combined workflows

```bash
# Full pipeline: batch HTTPS → WGS84 dB COGs + time-series VRT, fill interior holes
seppo_nisar_gcov_convert \
    -i urls.txt \
    -o s3://my-bucket/nisar/dB/ \
    -dB -t_srs 4326 --fill_holes \
    --read_threads 16 \
    -v

# Browse images: WGS84 ~200 m, dual-pol ratio 3-band COG, no VRTs, direct to S3
seppo_nisar_gcov_convert \
    -i urls.txt \
    -o s3://my-bucket/nisar/browse/ \
    -dB -dpratio --no_single_bands --no_vrt \
    -t_srs 4326 -tr 0.002 0.002 --fill_holes

# Geographic subset in lon/lat at ~22 m, WGS84, amplitude output
seppo_nisar_gcov_convert \
    -i file.h5 \
    -o out/ \
    -amp \
    -projwin -120.5 37.2 -119.8 36.7 -t_srs 4326 -tr 0.0002 0.0002

# Subset + amplitude + rebuild master VRTs after
seppo_nisar_gcov_convert \
    -i new_scene.h5 \
    -o out/ \
    -amp \
    -projwin 400000 4200000 450000 4150000 \
    -R
```
