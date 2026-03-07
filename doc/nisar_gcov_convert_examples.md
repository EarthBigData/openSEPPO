# seppo_nisar_gcov_convert — Examples

`seppo_nisar_gcov_convert` converts NISAR GCOV HDF5 files to Cloud Optimized GeoTIFF (COG)
with optional reprojection, downscaling, subsetting, dual-pol ratio output, and VRT time-series stacking.

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

## Common combined workflows

```bash
# Full pipeline: batch HTTPS → WGS84 dB COGs + time-series VRT, fill interior holes
seppo_nisar_gcov_convert \
    -i urls.txt \
    -o s3://my-bucket/nisar/dB/ \
    -dB -t_srs 4326 --fill_holes \
    --read_threads 16 \
    -v

# Quick browse: 20× downscale, DN, dual-pol ratio, 3-band COG, WGS84
seppo_nisar_gcov_convert \
    -i file.h5 \
    -o browse/ \
    -DN -d 20 -dpratio --no_single_bands \
    -t_srs 4326 --fill_holes

# Subset + amplitude + rebuild master VRTs after
seppo_nisar_gcov_convert \
    -i new_scene.h5 \
    -o out/ \
    -amp \
    -projwin 400000 4200000 450000 4150000 \
    -R
```
