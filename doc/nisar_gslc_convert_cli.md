# seppo_nisar_gslc_convert -- CLI Reference

Convert NISAR GSLC (Geocoded Single-Look Complex) HDF5 files to Cloud Optimized GeoTIFF (COG) with optional VRT stacking.

---

## Usage

```
seppo_nisar_gslc_convert [-h] [-i H5 [H5 ...]] [-o OUTPUT]
                         [-vars VARS [VARS ...]] [-f {A,B}] [-lg] [-lv]
                         [-pwr | -amp | -mag | -phase | -cslc]
                         [-of {COG,GTiff,h5}]
                         [-d N | -d Nx Ny] [--square]
                         [--no_vrt] [--no_time_series] [--no_single_bands]
                         [-srcwin XOFF YOFF XSIZE YSIZE | -projwin ULX ULY LRX LRY]
                         [-projwin_srs CRS]
                         [--no_tap] [-t_srs TARGET_SRS] [-tr XRES YRES]
                         [--resample RESAMPLE] [--fill_holes]
                         [--warp_threads N] [--read_threads N]
                         [--profile PROFILE] [--input_profile INPUT_PROFILE]
                         [--output_profile OUTPUT_PROFILE]
                         [-ro] [-S] [-vsis3] [--reset_vrts]
                         [-cache CACHE] [-keep] [-v]
```

---

## Arguments

### Input / Output

| Argument | Description |
|----------|-------------|
| `-i`, `--h5` | Input GSLC H5 URL(s) or path to a text file containing URLs (local or s3://). |
| `-o`, `--output` | Output directory path (S3 or local). Must end in `/` for batch processing. |
| `-of {COG,GTiff,h5}`, `--output_format` | Output format: `COG` (default), `GTiff` (BigTIFF), `h5` (raw complex HDF5 subset). |

### Variables and Frequency

| Argument | Description |
|----------|-------------|
| `-vars`, `--vars` | Polarization variables to extract, e.g. `HH HV`. If omitted, all 2-letter upper-case variables for the frequency are used. |
| `-f {A,B}`, `--freq` | Frequency band (`A` or `B`). Default: `A`. |
| `-lg`, `--list_grids` | Scan the first H5 file and list all available grids, frequencies, and variables, then exit. Requires `-i`. |
| `-lv`, `--list_vars` | Print a flat list of all HDF5 paths in the first file, then exit. Requires `-i`. |

### Output Mode

Exactly one mode may be selected. Default is `-pwr`.

| Argument | Description |
|----------|-------------|
| `-pwr` | Power intensity \|z\|² (float32, nodata=NaN). Default. `dB = 10*log10(DN)`. |
| `-amp` | Scaled amplitude (uint16, GCOV-compatible): `sqrt(\|z\|² × 10^8.3)`, range 1–65535, nodata=0. `dB = 20*log10(DN) - 83`. Directly comparable to `seppo_nisar_gcov_convert -amp` output. |
| `-mag` | Raw magnitude \|z\| (float32, nodata=NaN). |
| `-phase` | Wrapped phase `angle(z)` in radians (float32, range −π … π). Reprojection uses nearest-neighbour automatically. |
| `-cslc` | Raw complex SLC (complex64 / CFloat32, tiled GeoTIFF, deflate). Preserves full magnitude and phase for interferometry and coherence. Nodata: 0+0j pixels are invalid. Reprojection uses nearest-neighbour automatically. |

### Downscaling

| Argument | Description |
|----------|-------------|
| `-d N` or `-d Nx Ny`, `--downscale` | Downscale factor. One integer applies the same factor to both range (X) and azimuth (Y). Two integers set range and azimuth factors independently (e.g. `-d 2 4` for 2× in range, 4× in azimuth). Block-average for pwr/amp/mag; nearest decimation for phase/cslc. |
| `--square` | Auto-downscale to square pixels by averaging along the finer native axis. Example: 20 MHz data (10 m × 5 m) → 10 m × 10 m; 77 MHz data (2.5 m × 5 m) → 5 m × 5 m. 40 MHz data is already square. Ignored if `-d` is also supplied. |

### Masking

| Argument | Description |
|----------|-------------|
| `-nomask`, `--nomask` | Disable masking. By default (non-`h5` output), the GSLC subswath `mask` grid is applied when present. |

The `mask` grid encodes the subswath number of each valid sample: `1`–`254` = valid subswath, `0` = invalid, `255` = fill (outside the radar acquisition extent). Flagged pixels (`0` or `255`) are set to the complex nodata `0+0j` at the source resolution **before** any downscaling, transform, or resampling — so each output mode propagates them correctly (NaN for `-pwr`/`-amp`/`-mag`/`-phase`, `0+0j` for `-cslc`). Pass `-nomask`/`--nomask` to keep all pixels. Masking has no effect on `-of h5` output, and is skipped silently if the file has no `mask` grid.

### Spatial Subsetting

`-srcwin` and `-projwin` are mutually exclusive.

| Argument | Description |
|----------|-------------|
| `-srcwin XOFF YOFF XSIZE YSIZE` | Pixel-coordinate subset window (column offset, row offset, width, height). |
| `-projwin ULX ULY LRX LRY` | Geographic subset window in map coordinates: upper-left X, upper-left Y, lower-right X, lower-right Y. Coordinates are in the native (or target) CRS unless `-projwin_srs` is given. |
| `-projwin_srs CRS` | CRS of the `-projwin` coordinates (e.g. `EPSG:4326` or bare `4326`). If omitted, `-projwin` is assumed to be in the native raster CRS. When combined with `-t_srs`, the window is reprojected to the target CRS first, then to native CRS, ensuring the pre-warp crop is large enough to fill the full output image. |

### Reprojection

| Argument | Description |
|----------|-------------|
| `-t_srs TARGET_SRS` | Target CRS for output (e.g. `EPSG:4326` or bare `4326`). If omitted, output stays in native UTM. |
| `-tr XRES YRES` | Explicit output pixel size in target CRS units (e.g. `-tr 0.0001 0.0001` for ~10 m in degrees). Only meaningful with `-t_srs`. |
| `--resample` | Resampling method: `nearest` (default, phase-safe), `bilinear`, `cubic`, `cubicspline`, `lanczos`, `average`. |
| `--fill_holes` | Fill interior NaN pixels with their nearest valid neighbour before resampling (pwr/amp only). |
| `--no_tap` | Disable pixel-grid alignment. By default the output origin is snapped to integer multiples of the target pixel size. |
| `--warp_threads N` | Number of threads for reprojection. Default: all available CPU cores. |
| `--read_threads N` | Number of parallel S3/HTTPS connections for reading. Default: 8. |

### VRT Control

| Argument | Description |
|----------|-------------|
| `--no_vrt` | Disable generation of per-snapshot multi-pol VRTs. |
| `--no_time_series` | Disable generation of time-series VRT stacks. |
| `--no_single_bands` | Save a multi-band COG instead of separate files per polarization. |
| `-ro`, `--rebuild_only` | Skip processing; rebuild all VRTs in `-o` from existing TIFs. |
| `-S`, `--show_vrts` | Print a structured summary of all VRTs and TIFs in `-o` (read-only). |
| `-vsis3` | With `-S`: print S3 paths as `/vsis3/` URIs for direct paste into QGIS/GDAL. |
| `--reset_vrts` | Delete all existing VRTs in `-o` before rebuilding (use with `-ro`). |

### AWS / Cloud

| Argument | Description |
|----------|-------------|
| `--profile` | AWS profile name (applies to both input and output). |
| `--input_profile` | AWS profile specifically for reading input H5 files. |
| `--output_profile` | AWS profile specifically for writing output COGs. |
| `-cache CACHE` | Local directory to cache remote H5 files before processing. Use `y` or `yes` to auto-create a temp directory on `/dev/shm` or `/tmp`. |
| `-keep` | Keep cached H5 files after processing (use with `-cache`). |

### General

| Argument | Description |
|----------|-------------|
| `-v`, `--verbose` | Verbose output. |
| `-h`, `--help` | Show help and exit. |

---

## Output Modes Summary

| Mode | Flag | dtype | nodata | Notes |
|------|------|-------|--------|-------|
| Power | `-pwr` | float32 | NaN | Default. `dB = 10*log10(DN)` |
| Amplitude | `-amp` | uint16 | 0 | GCOV-compatible. `dB = 20*log10(DN) - 83` |
| Magnitude | `-mag` | float32 | NaN | Linear magnitude |
| Phase | `-phase` | float32 | NaN | Radians, −π … π |
| Complex SLC | `-cslc` | complex64 | 0+0j | For interferometry / coherence |

---

## NISAR GSLC Pixel Spacing

NISAR GSLC geocoded pixels have fixed Y (azimuth) spacing of ~5 m; X (range) spacing depends on bandwidth:

| Bandwidth | X spacing | Y spacing | `--square` output | Typical use |
|-----------|-----------|-----------|-------------------|-------------|
| 77 MHz | ~2.5 m | ~5 m | ~5 m × 5 m | Ultra-high resolution |
| 40 MHz | ~5 m | ~5 m | — (already square) | High resolution, interferometry |
| 20 MHz | ~10 m | ~5 m | ~10 m × 10 m | Standard mapping, coherence |
| 5 MHz | ~40 m | ~5 m | ~40 m × 40 m | Wide-area backscatter |

Use `--square` to produce square pixels in one step, or `-d Nx Ny` to set range and azimuth downscale factors explicitly.

---

## Usage Examples

```bash
# 1. List available grids/variables
seppo_nisar_gslc_convert -i file.h5 -lg

# 2. Convert to power COG (default)
seppo_nisar_gslc_convert -i file.h5 -o out/

# 3. Scaled amplitude COG, GCOV-compatible
seppo_nisar_gslc_convert -i file.h5 -o out/ -amp

# 4. Square pixels from 20 MHz data (5 m x 10 m → 10 m x 10 m)
seppo_nisar_gslc_convert -i file.h5 -o out/ -pwr --square

# 5. Raw complex SLC for coherence (preserve full fidelity)
seppo_nisar_gslc_convert -i file.h5 -o out/ -cslc

# 6. Subset by geographic window (native UTM)
seppo_nisar_gslc_convert -i file.h5 -o out/ -pwr \
    -projwin 847242 2570282 892239 2527678

# 7. Subset in geographic coordinates (lon/lat)
seppo_nisar_gslc_convert -i file.h5 -o out/ -pwr \
    -projwin 72.39 23.21 72.82 22.81 -projwin_srs EPSG:4326

# 8. Reproject to WGS84 at 0.0001° (~10 m)
seppo_nisar_gslc_convert -i file.h5 -o out/ -amp \
    -t_srs 4326 -tr 0.0001 0.0001

# 9. Reproject with lon/lat projwin
seppo_nisar_gslc_convert -i file.h5 -o out/ -amp \
    -projwin 72.39 23.21 72.82 22.81 -projwin_srs EPSG:4326 \
    -t_srs 4326 -tr 0.0001 0.0001

# 10. HH only, 2x range × 4x azimuth downscale
seppo_nisar_gslc_convert -i file.h5 -o out/ -pwr -vars HH -d 2 4

# 11. Process a list of S3 URLs, write to S3
seppo_nisar_gslc_convert -i urls.txt -o s3://my-bucket/gslc/out/ -amp -v

# 12. Rebuild VRTs from existing COGs
seppo_nisar_gslc_convert -ro -o out/ -pwr
```

---

## TIF Metadata Tags

Each output TIF includes GDAL tags:

| Tag | Description |
|-----|-------------|
| `ACQUISITION_DATE` | Source HDF5 acquisition date |
| `ACQUISITION_TIME` | Source HDF5 acquisition time |
| `CRID` | NISAR Composite Release ID |
| `ISCE3_VERSION` | ISCE3 software version |
| `OPENSEPPO_VERSION` | openSEPPO package version |
| `OUTPUT_MODE` | `pwr`, `AMP`, `mag`, `phase`, or `cslc` |
