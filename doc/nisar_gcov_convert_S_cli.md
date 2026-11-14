# seppo_nisar_gcov_convert_S -- CLI Reference

Convert NISAR S-band HDF5 GCOV data to Cloud Optimized GeoTIFF (COG) with optional VRT stacking.
This is the S-band variant of [`seppo_nisar_gcov_convert`](nisar_gcov_convert_cli.md) (Beta release).
S-band granules are found and downloaded with [`seppo_nisar_search_S`](nisar_search_S_cli.md).

---

## Usage

```
seppo_nisar_gcov_convert_S [-h] [-i H5 [H5 ...]] [-o OUTPUT]
                           [-vars VARS [VARS ...]] [-f {A,B}] [-lg]
                           [-DN | -amp | -dB | -pwr] [-of {COG,GTiff,h5}]
                           [-dpratio] [-sigma0] [-d DOWNSCALE] [--no_vrt]
                           [--no_time_series] [--no_single_bands]
                           [-srcwin XOFF YOFF XSIZE YSIZE | -projwin ULX ULY LRX LRY]
                           [-projwin_srs PROJWIN_SRS] [--no_tap]
                           [-t_srs TARGET_SRS] [-tr XRES YRES]
                           [--resample RESAMPLE] [--fill_holes]
                           [--warp_threads N] [--read_threads N]
                           [--profile PROFILE]
                           [--input_profile INPUT_PROFILE]
                           [--output_profile OUTPUT_PROFILE] [-ro] [-S]
                           [-vsis3] [--reset_vrts] [-cache CACHE] [-keep]
                           [-v]
```

---

## Differences from seppo_nisar_gcov_convert

- **Polarization modes.** S-band acquires in compact-pol (CR, terms `RHRH`, `RVRV`, `RHRV`), dual-pol (DH, DV) and single-pol modes. The mode is read from the granule name, e.g. `CRNA` or `DHNA`.
- **Default variables.** Without `-vars`, each file uses the power terms of its own polarization mode, so one batch can mix modes. A mixed batch gets one time-series stack per CRS and set of terms.
- **No masking.** The subswath `mask` grid is not applied to the backscatter, so there is no `-nomask`. Pixels with `mask` = `0` (partially focused) are kept. Extract `mask` with `-vars` to apply it yourself.
- **Options not available:** `--all_freq`, `-noanc` / `--no_ancillary_floats`, `-nomask` and `-t_srs wagner7`. The per-polarization validity bits of `inputDataExceptionMask` (CRID 5032 and later) are not used.

---

## Arguments

### Input / Output

| Argument | Description |
|----------|-------------|
| `-i`, `--h5` | Input H5 URL(s) or path to a text file containing URLs. |
| `-o`, `--output` | Output directory path (S3 or local). Must end in `/` for batch processing. |
| `-of {COG,GTiff,h5}`, `--output_format` | Output format: `COG` (default), `GTiff` (BigTIFF), `h5` (raw HDF5 subset). |

### Variables and Frequency

| Argument | Description |
|----------|-------------|
| `-vars`, `--vars` | Grid variables to extract, e.g. `RHRH RVRV RHRV` or `HHHH HVHV HHHV`. If omitted, the power terms of each file's polarization mode are used: `RHRH RVRV` (CR), `HHHH HVHV` (DH), `VVVV VHVH` (DV), and `HHHH` or `VVVV` (single-pol). Ancillary grids (`mask`, `inputDataExceptionMask`, `numberOfLooks`, `rtcGammaToSigmaFactor`) are also supported; see [Ancillary Grid Handling](#ancillary-grid-handling). |
| `-f {A,B}`, `--freq` | Frequency (`A` or `B`). Default: `A`. |
| `-lg`, `--list_grids` | Scan the first H5 file and list all available grids/frequencies/variables, then exit. |

Cross terms such as `RHRV` or `HHHV` are complex. They are written as their magnitude, scaled like the power terms, each to its own output file.

### Scaling / Output Mode

| Argument | Description |
|----------|-------------|
| `-DN` | DN mode: uint8 scaled 1-255. `dB = -31.15 + DN x 0.15` (range -31 to +7.1 dB). DN=0 is nodata. |
| `-amp` | Amplitude mode: uint16. `dB = 20*log10(DN) - 83`. DN=0 is nodata. |
| `-dB` | dB mode: float32. Value is dB directly. |
| `-pwr` | Power mode: raw float32 (default). `dB = 10*log10(DN)`. |
| `-dpratio`, `--dualpol_ratio` | Compute the dual-pol power ratio from the file's polarization mode: `RHRH/RVRV` (CR), `HHHH/HVHV` (DH), `VVVV/VHVH` (DV). A ratio term missing from `-vars` is added. Single-pol granules are skipped with a warning. Ancillary grids are excluded when `-dpratio` is active; process them in a separate run. See [S-band ratio](ratio.md#s-band-seppo_nisar_gcov_convert_s). |
| `-sigma0`, `--sigma0` | Convert gamma0 backscatter to sigma0 by multiplying power values with the `rtcGammaToSigmaFactor` layer from the GCOV file. Applied before any downscaling or resampling. |

### Ancillary Grid Handling

When ancillary variables are included in `--vars`:

| Variable | Output suffix | Downscale | Warp resampling | dtype | nodata |
|----------|--------------|-----------|-----------------|-------|--------|
| `mask` | `_mask.tif` | Priority (255 > 0 > subswath) | nearest | uint8 | 255 |
| `inputDataExceptionMask` | `_exceptionmask.tif` | Bitwise OR (fill wins) | nearest | uint8 | 255 |
| `numberOfLooks` | `_nlooks.tif` | sum | sum | float32 | NaN |
| `rtcGammaToSigmaFactor` | `_gamma2sigma.tif` | mean | average | float32 | NaN |

Ancillary grids bypass backscatter scaling modes (`-amp`, `-dB`, `-DN`) and are always written as separate TIFs.

### Spatial Subsetting

| Argument | Description |
|----------|-------------|
| `-srcwin XOFF YOFF XSIZE YSIZE` | Pixel-coordinate subset window. |
| `-projwin ULX ULY LRX LRY` | Geographic subset window. Coordinates are in the native or target CRS unless `-projwin_srs` is given. |
| `-projwin_srs PROJWIN_SRS` | CRS of the `-projwin` coordinates (e.g. `EPSG:4326`). |

### Resampling and Reprojection

| Argument | Description |
|----------|-------------|
| `-t_srs TARGET_SRS` | Target CRS for output (e.g. `EPSG:4326` or bare `4326`). If omitted, output stays in native UTM. Reprojected power is clamped to the source band's own range. |
| `-tr XRES YRES` | Explicit output pixel size in target CRS units (e.g. `-tr 0.001 0.001` for ~100 m in degrees). Only used with `-t_srs`. |
| `--resample` | Resampling method: `nearest`, `bilinear`, `cubic` (default), `cubicspline`, `lanczos`, `average`. |
| `--fill_holes` | Fill interior NaN/+/-inf pixels with their nearest valid neighbour before resampling. Frame-boundary nodata is unaffected. |
| `--no_tap` | Disable pixel-grid alignment. By default the output origin is snapped to integer multiples of the target pixel size. |
| `-d DOWNSCALE`, `--downscale` | Manual downscale factor (integer). E.g. `2` for 2x2 block averaging. |
| `--warp_threads N` | Number of threads for reprojection. Default: all available CPU cores. |
| `--read_threads N` | Number of parallel S3/HTTPS connections for reading HDF5 chunks (default 8). Each band x stripe gets its own connection. |

### VRT Control

| Argument | Description |
|----------|-------------|
| `--no_vrt` | Disable all VRT output: per-snapshot, per-track and time-series VRTs (implies `--no_time_series`). Build the VRTs once afterwards with `--rebuild_only`. |
| `--no_time_series` | Disable generation of time-series VRT stacks. |
| `--no_single_bands` | Save a multi-band COG instead of separate files per polarization. |
| `-ro`, `--rebuild_only` | Skip processing and rebuild VRTs in the output folder from existing TIFs. Auto-detects scaling mode. |
| `-S`, `--show_vrts` | Print a summary of all VRTs and TIFs in the output folder (requires `-o`). Auto-detects scaling mode. |
| `-vsis3`, `--vsis3` | With `-S`: print S3 paths as `/vsis3/` URIs for direct use in QGIS/GDAL. |
| `--reset_vrts` | Delete all existing VRTs in the output folder before rebuilding. Use with `-ro` for a clean slate. |

### AWS / Cloud

| Argument | Description |
|----------|-------------|
| `--profile` | AWS profile name (applies to both input and output unless overridden). |
| `--input_profile` | AWS profile specifically for reading input H5 files. |
| `--output_profile` | AWS profile specifically for writing output COGs. |
| `-cache CACHE`, `--cache` | Local directory to cache files first; `y` or `yes` creates a temp directory on `/dev/shm` or `/tmp`. Same behavior as in [`seppo_nisar_gcov_convert`](nisar_gcov_convert_cli.md#aws-cloud), including `CacheDiskFullError` (exit status 1) when the cache disk fills up. |
| `-keep`, `--keep_cached` | Keep the cached H5 file locally after processing (use with `-cache`). A complete copy already in the cache directory is reused. |

### General

| Argument | Description |
|----------|-------------|
| `-v`, `--verbose` | Verbose output. |
| `-h`, `--help` | Show help and exit. |

---

## Output File Names

Output files are named `<granule>-EBD_<freq>_<pol>_<MODE>.tif`. Power terms use their two-letter prefix (`rh`, `rv`, `hh`, `hv`, ...), cross terms their full name (`rhrv`, `hhhv`), and ratio files `rhrvra`, `hhhvra` or `vvvhra`.

| File (CR example, `-dB -dpratio`) | Content |
|---|---|
| `...-EBD_A_rh_dB.tif` | RHRH |
| `...-EBD_A_rv_dB.tif` | RVRV |
| `...-EBD_A_rhrvra_dB.tif` | Ratio RHRH/RVRV |

---

## Examples

```bash
# Find S-band GCOV granules and download them
seppo_nisar_search_S --point 77.1951 9.4448 --start_time_after 2026-09-01 --download /data/nisar_S/

# List the grids of a granule
seppo_nisar_gcov_convert_S -i /data/nisar_S/NISAR_S2_PR_GCOV_031_077_D_084_2500_CRNA_A_20260923T132025_20260923T132056_P00500_M_P_I_001.h5 -lg

# dB COGs of the default power terms plus the dual-pol ratio, for all files in a directory
seppo_nisar_gcov_convert_S -i /data/nisar_S/*.h5 -o /data/nisar_S/cog/ -dB -dpratio

# Compact-pol terms including the cross term, reprojected to 100 m in EPSG:4326
seppo_nisar_gcov_convert_S -i granule.h5 -o out/ -vars RHRH RVRV RHRV -t_srs EPSG:4326 -tr 0.001 0.001

# Browse image: 3-band DN COG at 20x downscale
seppo_nisar_gcov_convert_S -i granule.h5 -o browse/ -DN -d 20 --no_single_bands -dpratio
```

Run `seppo_nisar_gcov_convert_S --help` for the full option list.
