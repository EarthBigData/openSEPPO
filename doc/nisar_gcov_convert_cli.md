# seppo_nisar_gcov_convert -- CLI Reference

Convert NISAR HDF5 GCOV data to Cloud Optimized GeoTIFF (COG) with optional VRT stacking.

---

## Usage

```
seppo_nisar_gcov_convert [-h] [-i H5 [H5 ...]] [-o OUTPUT]
                         [-vars VARS [VARS ...]] [-f {A,B}] [-lg]
                         [-DN | -amp | -dB | -pwr] [-of {COG,GTiff,h5}]
                         [-dpratio] [-sigma0] [-d DOWNSCALE] [--no_vrt]
                         [--no_time_series] [--no_single_bands]
                         [-srcwin XOFF YOFF XSIZE YSIZE | -projwin ULX ULY LRX LRY]
                         [--no_tap] [-t_srs TARGET_SRS] [-tr XRES YRES]
                         [--resample RESAMPLE] [--fill_holes]
                         [--warp_threads N] [--read_threads N]
                         [--profile PROFILE] [--input_profile INPUT_PROFILE]
                         [--output_profile OUTPUT_PROFILE]
                         [-ro] [-S] [-vsis3] [-cache CACHE] [-keep] [-v]
```

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
| `-vars`, `--vars` | Grid variables to extract, e.g. `HHHH HVHV`. Ancillary grids (`mask`, `numberOfLooks`, `rtcGammaToSigmaFactor`) are also supported and receive specialized processing. If omitted, all covariance variables for the frequency are used. |
| `-f {A,B}`, `--freq` | Frequency band (`A` or `B`). Default: `A`. |
| `-lg`, `--list_grids` | Scan the first H5 file and list all available grids/frequencies/variables with dtype and nodata, then exit. |

### Scaling / Output Mode

| Argument | Description |
|----------|-------------|
| `-DN` | DN mode: uint8 scaled 1-255. `dB = -31.15 + DN x 0.15` (range -31 to +7.1 dB). DN=0 is nodata. |
| `-amp` | Amplitude mode: uint16. `dB = 20*log10(DN) - 83`. DN=0 is nodata. |
| `-dB` | dB mode: float32. Value is dB directly. |
| `-pwr` | Power mode: raw float32 (default). `dB = 10*log10(DN)`. |
| `-dpratio`, `--dualpol_ratio` | Compute dual-pol power ratio: HHHH/HVHV (DH mode) or VVVV/VHVH (DV mode). Ancillary grids are automatically excluded when `-dpratio` is active; process them in a separate run. |
| `-sigma0`, `--sigma0` | Convert gamma0 backscatter to sigma0 by multiplying power values with the `rtcGammaToSigmaFactor` layer from the GCOV file. Applied before any downscaling or resampling. |
| `-nomask`, `--nomask` | Disable masking. See [Backscatter Masking](#backscatter-masking) below. |

### Backscatter Masking

For non-`h5` output (COG/GTiff), the GCOV subswath `mask` grid is **applied to the backscatter by default**. The `mask` encodes the subswath number of each valid sample: `1`–`254` = valid subswath, `0` = invalid (the multilooking ensemble was not fully focused), `255` = fill (outside the radar acquisition extent). Pixels flagged `0` or `255` are set to nodata; only valid-subswath pixels are kept.

Masking is applied at the source resolution **before** any downscaling, transform, or resampling, so fill/invalid pixels never contaminate block-averaging or the warp kernel. Pass `-nomask`/`--nomask` to keep all pixels. Masking has no effect on `-of h5` output (the raw subset is written unchanged), and is skipped silently if the file has no `mask` grid.

### Ancillary Grid Handling

When ancillary variables (`mask`, `numberOfLooks`, `rtcGammaToSigmaFactor`) are included in `--vars`:

| Variable | Output suffix | Downscale | Warp resampling | dtype | nodata |
|----------|--------------|-----------|-----------------|-------|--------|
| `mask` | `_mask.tif` | Priority (255 > 0 > subswath) | nearest | uint8 | 255 |
| `numberOfLooks` | `_nlooks.tif` | sum | sum | float32 | NaN |
| `rtcGammaToSigmaFactor` | `_gamma2sigma.tif` | mean | average | float32 | NaN |

Ancillary grids bypass backscatter scaling modes (`-amp`, `-dB`, `-DN`) and are always written as separate TIFs.

### Spatial Subsetting

| Argument | Description |
|----------|-------------|
| `-srcwin XOFF YOFF XSIZE YSIZE` | Pixel-coordinate subset window. |
| `-projwin ULX ULY LRX LRY` | Geographic subset window in map coordinates. |

### Resampling and Reprojection

| Argument | Description |
|----------|-------------|
| `-t_srs TARGET_SRS` | Target CRS for output (e.g. `EPSG:4326` or bare `4326`). If omitted, output stays in native UTM. |
| `-tr XRES YRES` | Explicit output pixel size in target CRS units (e.g. `-tr 0.001 0.001` for ~100 m in degrees). Triggers auto pre-downscaling when a large reduction factor is implied. |
| `--resample` | Resampling method: `nearest`, `bilinear`, `cubic` (default), `cubicspline`, `lanczos`, `average`. |
| `--fill_holes` | Fill interior NaN/+/-inf pixels with their nearest valid neighbour before resampling. Frame-boundary nodata is unaffected. Higher memory usage. |
| `--no_tap` | Disable pixel-grid alignment. By default the output origin is snapped to integer multiples of the target pixel size. |
| `-d DOWNSCALE`, `--downscale` | Manual downscale factor (integer). E.g. `2` for 2x2 block averaging. |
| `--warp_threads N` | Number of threads for reprojection. Default: all available CPU cores. |
| `--read_threads N` | Number of parallel S3/HTTPS connections for reading HDF5 chunks. Default: 8. |

### VRT Control

| Argument | Description |
|----------|-------------|
| `--no_vrt` | Disable generation of per-snapshot multi-pol VRTs. |
| `--no_time_series` | Disable generation of time-series VRT stacks. |
| `--no_single_bands` | Save a multi-band COG instead of separate files per polarization. |
| `-ro`, `--rebuild_only` | Skip processing and rebuild all VRTs in the output folder from existing TIFs. Auto-detects scaling mode. |
| `-S`, `--show_vrts` | Print a structured summary of all VRTs and TIFs in the output folder (requires `-o`). Auto-detects scaling mode -- no need to pass `-amp`/`-dB` etc. |
| `-vsis3`, `--vsis3` | With `-S`: print S3 paths as `/vsis3/` URIs for direct paste into QGIS/GDAL. |

### AWS / Cloud

| Argument | Description |
|----------|-------------|
| `--profile` | AWS profile name (applies to both input and output unless overridden). |
| `--input_profile` | AWS profile specifically for reading input H5 files. |
| `--output_profile` | AWS profile specifically for writing output COGs. |
| `-cache CACHE`, `--cache` | Local directory to cache files first. Use `y` or `yes` to auto-create a temp directory on `/dev/shm` or `/tmp`. |
| `-keep`, `--keep_cached` | Keep the cached H5 file locally after processing (use with `-cache`). |

### General

| Argument | Description |
|----------|-------------|
| `-v`, `--verbose` | Verbose output. |
| `-h`, `--help` | Show help and exit. |

---

## VRT Generation Pipeline

After processing (and with `-ro`), VRTs are built in four phases:

1. **Grid mosaic VRTs** -- when multiple frames exist for the same track/direction/cycle, a spatial mosaic VRT is built per grid variable.

2. **Single-date multi-pol VRTs** -- for each acquisition date and track, all backscatter polarizations (and ratio if present) are combined into one multi-band VRT. Band order: likepol, crosspol, ratio.

3. **Per-track time-series VRTs** -- one band per date, per grid variable (backscatter or ancillary), per track. Only built when >1 timestep exists. Combined time-series VRTs across tracks are only built when all tracks share the same CRS.

4. **Combined multi-track time-series VRTs** -- when >1 track exists and all tracks share the same CRS, a combined VRT interleaves all dates.

VRT metadata includes `RADIOMETRY` (gamma0/sigma0), `DB_FORMULA`, `CRID`, `ISCE3_VERSION`, and `OPENSEPPO_VERSION` for backscatter VRTs. Ancillary VRTs do not include radiometry metadata.

The `-S` summary output is organized into **Ancillary** and **Backscatter** sections with sub-sections for single dates, time series by track, and combined time series.

---

## TIF Metadata Tags

Each output TIF includes GDAL tags:

| Tag | Description | Applies to |
|-----|-------------|------------|
| `ACQUISITION_DATE` | Source H5 acquisition date | All |
| `ACQUISITION_TIME` | Source H5 acquisition time | All |
| `CRID` | NISAR Composite Release ID | All |
| `ISCE3_VERSION` | ISCE3 software version | All |
| `OPENSEPPO_VERSION` | openSEPPO package version | All |
| `RADIOMETRY` | `gamma0` or `sigma0` | Backscatter only |
| `DB_FORMULA` | Formula to convert pixel values to dB | Backscatter only |
