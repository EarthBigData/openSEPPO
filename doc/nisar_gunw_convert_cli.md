# seppo_nisar_gunw_convert -- CLI Reference

Convert NISAR GUNW (Geocoded Unwrapped interferogram, the L2 InSAR pair product) HDF5 files to Cloud Optimized GeoTIFF (COG) or to a self-contained windowed HDF5 subset.

A GUNW granule is a **pair**, not a single acquisition: each one is an interferogram between a reference and a secondary pass, and its name carries both acquisition times.

Unlike the other geocoded NISAR products, GUNW is **three independent grids at two resolutions**, nested as `frequency{X}/{group}/{pol}`. Each carries its own coordinate axes and projection, so each behaves as a self-contained grid path. `unwrappedInterferogram` and `pixelOffsets` sit at ~80 m; `wrappedInterferogram` is ~4x finer at ~20 m and complex. Raster output is therefore **single-grid** -- one `--layer_group` per run -- while `-of h5` carries all three, each windowed on its own axes to the same ground.

Run `-lg` to see what a granule actually holds.

---

## Usage

```
seppo_nisar_gunw_convert [-h] [-i H5 [H5 ...]] [-o OUTPUT]
                         [-lyr {unwrappedInterferogram,pixelOffsets,wrappedInterferogram}]
                         [-vars VARS [VARS ...]] [-f {A,B}] [-pol POL]
                         [-groups GROUP [GROUP ...]] [-lg]
                         [-of {COG,GTiff,h5}]
                         [-srcwin XOFF YOFF XSIZE YSIZE | -projwin ULX ULY LRX LRY]
                         [-projwin_srs CRS]
                         [-t_srs TARGET_SRS] [-tr XRES YRES] [--no_tap]
                         [--resample RESAMPLE] [-d DOWNSCALE]
                         [--no_time_series] [--no_vrt]
                         [-report] [--report_format {png,pdf}] [-epicenter LON LAT]
                         [--profile PROFILE] [--input_profile INPUT_PROFILE]
                         [--output_profile OUTPUT_PROFILE]
                         [--read_threads N] [--warp_threads N]
                         [-cache CACHE] [-keep] [-v]
```

---

## Arguments

### Input / Output

| Argument | Description |
|----------|-------------|
| `-i`, `--h5` | Input GUNW H5 URL(s) or path to a text file containing URLs (local, `s3://`, or `https://`). |
| `-o`, `--output` | Output directory path (S3 or local). Must end in `/` for batch processing. |
| `-of {COG,GTiff,h5}`, `--output_format` | Output format: `COG` (default), `GTiff` (BigTIFF), `h5` (self-contained subset). |

### Layer Selection

| Argument | Description |
|----------|-------------|
| `-lyr`, `--layer_group` | Which GUNW grid to rasterise: `unwrappedInterferogram` (default), `pixelOffsets`, `wrappedInterferogram`. Raster output is single-grid. Ignored for `-of h5`, which windows every group. |
| `-vars`, `--vars` | Layers to extract within the group. Default: the group's standard set. |
| `-f {A,B}`, `--freq` | Frequency. Default: the first present in the granule. |
| `-pol`, `--pol` | Polarisation subgroup (e.g. `HH`). Default: the first present. |
| `-groups`, `--groups` | For `-of h5`: restrict which grid sub-groups the subset carries. Default: all three. |
| `-lg`, `--list_grids` | Scan the first file, list every grid and layer, then exit. |

### Layer groups

| Group | Token | Resolution | Layers (default set in **bold**) |
|-------|-------|------------|----------------------------------|
| `unwrappedInterferogram` | `unw` | ~80 m | **`unwrappedPhase`**, **`coherenceMagnitude`**, **`connectedComponents`**, `ionospherePhaseScreen`, `ionospherePhaseScreenUncertainty`, `mask` |
| `pixelOffsets` | `off` | ~80 m | **`alongTrackOffset`**, **`slantRangeOffset`**, **`correlationSurfacePeak`**, `mask` |
| `wrappedInterferogram` | `wrap` | ~20 m | **`wrappedInterferogram`**, **`coherenceMagnitude`**, `mask` |

Continuous layers are written as float32 with NaN nodata and resampled with `--resample` on reprojection. Integer layers -- `connectedComponents` (uint16) and the bit-encoded `mask` (uint32) -- keep their native dtype, carry the source `_FillValue`, and use nearest resampling and nearest COG overviews so codes are never invented. The complex `wrappedInterferogram` layer is written as its **phase** (`angle`, radians, float32).

`unwrappedPhase` is in **radians**; multiply by `wavelength / 4pi` for line-of-sight displacement. The wavelength is the granule's own `centerFrequency` -- which is what `-report` uses for its centimetre panel.

### Subsetting

| Argument | Description |
|----------|-------------|
| `-srcwin XOFF YOFF XSIZE YSIZE` | Pixel subset window on the selected layer group's grid. |
| `-projwin ULX ULY LRX LRY` | Geographic subset window. |
| `-projwin_srs CRS` | CRS of the `-projwin` corners (e.g. `4326`). Lon/lat corners given without this are detected and reprojected rather than read as projected metres. |

Subset to the **deformation field**, not to the epicentre: a window smaller than the signal renders it as a ramp indistinguishable from atmosphere. See [GUNW examples](nisar_gunw_convert_examples.md).

### Reprojection / Resampling / VRTs

| Argument | Description |
|----------|-------------|
| `-t_srs`, `--target_srs` | Target CRS for output. Default: the granule's native UTM. |
| `-tr XRES YRES`, `--target_res` | Output pixel size in target CRS units (with `-t_srs`). |
| `--no_tap` | Disable pixel-grid alignment of the reprojected origin. |
| `--resample` | Resampling for continuous layers on reprojection. Integer layers always use nearest. |
| `-d`, `--downscale` | Integer downscale factor (block reduce). |
| `--no_vrt` | Disable the per-pair multi-layer VRT. |
| `--no_time_series` | Disable the time-series VRT stacks built over a batch: one VRT per layer, one band per interferometric pair, ordered by reference acquisition, with a `.dates` sidecar. Pairs from the same track and frame resolve to the same window, so they stack with no resampling; pairs that only partly cover the box are stacked on their union instead. **Each band is an independent 12-day interferogram, not a cumulative displacement** -- summing a chain needs a reference point and unwrapping-error handling, which this converter leaves to you. |

### Event report (experimental)

| Argument | Description |
|----------|-------------|
| `-report`, `--report` | **EXPERIMENTAL.** Also render a coseismic InSAR quick-look: wrapped fringes (cyclic colormap), relative LOS displacement in cm (diverging, referenced to the high-coherence median, scaled by the granule's own wavelength, masked to coherence > 0.3), coherence, and an info panel with acquisition dates, temporal baseline and displacement statistics. Runs independently of `-of`, reading straight from the grid. Requires `matplotlib`. |
| `--report_format {png,pdf}` | Report image format. Default: `png`. |
| `-epicenter LON LAT` | Mark an event epicentre (EPSG:4326) on the report panels. |

The panel layout, colormaps and referencing defaults are provisional and labelled experimental on the figure itself.

### Authentication, Threads, Caching

| Argument | Description |
|----------|-------------|
| `--profile`, `--input_profile`, `--output_profile` | AWS profile(s). ASF DAAC buckets and Earthdata HTTPS URLs switch to Earthdata credentials automatically. |
| `--read_threads N` | Default: 8. For `-of h5` this is the worker count for the coalescing parallel reader that fetches the windowed grid payload and the scattered metadata. It also serves as the fallback thread count for reprojection when `--warp_threads` is not given. |
| `--warp_threads N` | Threads for reprojection. Default: all cores. |
| `-cache`, `-keep` | Fetch the whole granule locally before reading, and optionally keep it. |
| `-v`, `--verbose` | Verbose output, including per-phase timings and the prefetch plan. |

---

## Examples

For a worked search-to-interferogram and search-to-stack walkthrough over the 2026 Venezuela earthquakes, see [GUNW examples](nisar_gunw_convert_examples.md).

```bash
# List every grid and layer in a granule
seppo_nisar_gunw_convert --h5 gunw.h5 -lg

# Core unwrapped layers as COGs, subset to an AOI (lon/lat)
seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326

# Deformation only, one layer
seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ -vars unwrappedPhase \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326

# Self-contained windowed HDF5 subset, all three grids
seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ -of h5 \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326

# ... or without the 20 m complex grid, which is most of the payload
seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ -of h5 \
    -groups unwrappedInterferogram pixelOffsets \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326

# Pixel offsets instead of the interferogram
seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ -lyr pixelOffsets

# Coseismic quick-look report (experimental)
seppo_nisar_gunw_convert --h5 gunw.h5 -o out/ \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326 \
    --report -epicenter -68.60 10.30

# A stack of pairs: batch a URL list, get the time-series VRT
seppo_nisar_gunw_convert -i pairs.txt -o stack/ -vars unwrappedPhase \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326
```

Find pairs with `seppo_nisar_search --product GUNW`. A GUNW name contains two datetime pairs, so match the cycle and frame fields (`_022_162_A_007_023_`) rather than a date when picking one out of a list.

---

## Output naming

```
<granule>-EBD_<freq>_<group token>_<layer token>.tif      per-pair rasters
<granule>-EBD_<freq>_<group token>_<layer tokens>.vrt     the pair's layers
<granule>-EBD_<freq>_GUNW.h5                              self-contained subset
<granule>-EBD_<freq>_report.png                           experimental report
```

Group tokens are `unw`, `off` and `wrap`; layer tokens are the short forms in the table above (`unwphase`, `coh`, `concomp`, `iono`, `azoff`, `rgoff`, `corr`, `wrapphase`, `mask`), all listed by `-lg`.

A time-series VRT replaces **both** of the granule name's datetime pairs with one series span, so the stack is easy to tell from the per-pair files:

```
<...20260601T000000_20260719T235959...>-EBD_A_unw_unwphase.vrt     the stack
<...20260601T000000_20260719T235959...>-EBD_A_unw_unwphase.dates   its band dates
<...20260601T100657_..._20260613T100731...>-EBD_A_unw_unwphase.tif one pair
```

The span runs over **reference** acquisitions, matching the dates the bands are labelled with and the `.dates` sidecar lists; the last pair's secondary acquisition reaches beyond it.
