# seppo_nisar_gcov_convert — CLI Reference

Convert NISAR HDF5 GCOV data to Cloud Optimized GeoTIFF (COG) with optional VRT stacking.

---

## Usage

```
seppo_nisar_gcov_convert [-h] [-i H5 [H5 ...]] [-o OUTPUT]
                         [-vars VARS [VARS ...]] [-f {A,B}] [-lg]
                         [-DN | -amp | -dB | -pwr] [-of {COG,GTiff,h5}]
                         [-dpratio] [-d DOWNSCALE] [--no_vrt]
                         [--no_time_series] [--no_single_bands]
                         [-srcwin XOFF YOFF XSIZE YSIZE | -projwin ULX ULY LRX LRY]
                         [--no_tap] [-t_srs TARGET_SRS] [-tr XRES YRES]
                         [--resample RESAMPLE] [--fill_holes]
                         [--warp_threads N] [--read_threads N]
                         [--profile PROFILE] [--input_profile INPUT_PROFILE]
                         [--output_profile OUTPUT_PROFILE]
                         [-ro] [-R] [-S] [-cache CACHE] [-keep] [-v]
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
| `-vars`, `--vars` | Grid variables to extract, e.g. `HHHH HVHV`. If omitted, all variables for the frequency are used. |
| `-f {A,B}`, `--freq` | Frequency band (`A` or `B`). Default: `A`. |
| `-lg`, `--list_grids` | Scan the first H5 file and list all available grids/frequencies/variables, then exit. |

### Scaling / Output Mode

| Argument | Description |
|----------|-------------|
| `-DN` | DN mode: uint8 scaled 1–255. `dB = -31.15 + DN × 0.15` (range -31 to +7.1 dB). DN=0 is nodata. |
| `-amp` | Amplitude mode: uint16. |
| `-dB` | dB mode: float32. |
| `-pwr` | Power mode: raw float32 (default). |
| `-dpratio`, `--dualpol_ratio` | Compute dual-pol power ratio: HHHH/HVHV (DH mode) or VVVV/VHVH (DV mode). Incompatible with QP or single-pol acquisitions. |

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
| `--fill_holes` | Fill interior NaN/±inf pixels with their nearest valid neighbour before resampling. Frame-boundary nodata is unaffected. |
| `--no_tap` | Disable pixel-grid alignment. By default the output origin is snapped to integer multiples of the target pixel size. |
| `-d DOWNSCALE`, `--downscale` | Manual downscale factor (integer). E.g. `2` for 2×2 block averaging. |
| `--warp_threads N` | Number of threads for reprojection. Default: all available CPU cores. |
| `--read_threads N` | Number of parallel S3/HTTPS connections for reading HDF5 chunks. Default: 8. |

### VRT Control

| Argument | Description |
|----------|-------------|
| `--no_vrt` | Disable generation of per-snapshot multi-pol VRTs. |
| `--no_time_series` | Disable generation of time-series VRT stacks. |
| `--no_single_bands` | Save a multi-band COG instead of separate files per polarization. |
| `-ro`, `--rebuild_only` | Skip processing and only rebuild VRTs in the output folder. |
| `-R`, `--rebuild_all_vrts` | After processing, rescan the output folder and rebuild all VRTs to include all timesteps (old + new). |
| `-S`, `--show_vrts` | Print a formatted summary of all VRTs in the output folder grouped by type (requires `-o`). No processing is performed. |

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

## Full help output

```
usage: nisar_gcov_convert.py [-h] [-i H5 [H5 ...]] [-o OUTPUT]
                             [-vars VARS [VARS ...]] [-f {A,B}] [-lg]
                             [-DN | -amp | -dB | -pwr] [-of {COG,GTiff,h5}]
                             [-dpratio] [-d DOWNSCALE] [--no_vrt]
                             [--no_time_series] [--no_single_bands]
                             [-srcwin XOFF YOFF XSIZE YSIZE | -projwin ULX ULY LRX LRY]
                             [--no_tap] [-t_srs TARGET_SRS] [-tr XRES YRES]
                             [--resample RESAMPLE] [--fill_holes]
                             [--warp_threads N] [--read_threads N]
                             [--profile PROFILE]
                             [--input_profile INPUT_PROFILE]
                             [--output_profile OUTPUT_PROFILE] [-ro] [-R] [-S]
                             [-cache CACHE] [-keep] [-v]

Convert NISAR HDF5 GCOV data to Cloud Optimized GeoTIFF (COG) with optional VRT stacking.

options:
  -h, --help            show this help message and exit
  -i H5 [H5 ...], --h5 H5 [H5 ...]
                        Input H5 URL(s) or path to a text file containing
                        URLs.
  -o OUTPUT, --output OUTPUT
                        Output directory path (S3 or local). Must end in '/'
                        for batch processing.
  -vars VARS [VARS ...], --vars VARS [VARS ...]
                        Grid Variables to extract, e.g. HHHH HVHV. If omitted,
                        ALL variables for the frequency are used.
  -f {A,B}, --freq {A,B}
                        Frequency (A/B). Default: A.
  -lg, --list_grids     Scan the first H5 file and list all available
                        grids/frequencies/variables, then exit.
  -DN, --DN             Set scaling mode to DN (uint8 scaled 1-255).
  -amp, --amp           Set scaling mode to Amplitude (uint16).
  -dB, --dB             Set scaling mode to dB (float32).
  -pwr, --power         Set scaling mode to Power (raw float32). Default.
  -of {COG,GTiff,h5}, --output_format {COG,GTiff,h5}
                        Output format: COG (default), GTiff (BigTIFF), h5.
  -dpratio, --dualpol_ratio
                        Compute dual-pol power ratio: HHHH/HVHV (DH mode) or
                        VVVV/VHVH (DV mode).
  -d DOWNSCALE, --downscale DOWNSCALE
                        Downscale factor (integer). E.g., 2 for 2x2 block
                        averaging.
  --no_vrt              Disable generation of per-snapshot VRTs.
  --no_time_series      Disable generation of Time Series VRT stacks.
  --no_single_bands     Save multi-band COG instead of separate files per pol.
  -srcwin XOFF YOFF XSIZE YSIZE, --srcwin XOFF YOFF XSIZE YSIZE
                        Pixel subset window.
  -projwin ULX ULY LRX LRY, --projwin ULX ULY LRX LRY
                        Geographic subset window (map coordinates).
  --no_tap              Disable pixel-grid alignment (tap).
  -t_srs TARGET_SRS, --target_srs TARGET_SRS
                        Target CRS for output (e.g. EPSG:4326 or bare 4326).
  -tr XRES YRES, --target_res XRES YRES
                        Explicit output pixel size in target CRS units.
  --resample RESAMPLE   Resampling method for reprojection
                        (nearest/bilinear/cubic/cubicspline/lanczos/average).
                        Default: cubic.
  --fill_holes          Fill interior NaN/±inf pixels with nearest valid
                        neighbour before resampling.
  --warp_threads N      Number of threads for reprojection. Default: all cores.
  --read_threads N      Number of parallel S3/HTTPS connections. Default: 8.
  --profile PROFILE     AWS Profile name (input and output).
  --input_profile INPUT_PROFILE
                        AWS Profile specifically for reading Input H5s.
  --output_profile OUTPUT_PROFILE
                        AWS Profile specifically for writing Output COGs.
  -ro, --rebuild_only   Skip processing and ONLY rebuild VRTs in output folder.
  -R, --rebuild_all_vrts
                        After processing, rebuild master VRTs for ALL timesteps.
  -S, --show_vrts       Print VRT summary grouped by type (requires -o).
  -cache CACHE, --cache CACHE
                        Local cache directory. Use 'y' for auto temp directory.
  -keep, --keep_cached  Keep cached H5 file locally (use with -cache).
  -v, --verbose         Verbose output.
```
