# seppo_nisar_rslc_convert -- CLI Reference

Subset NISAR L-band RSLC (Radar-coordinates Single-Look Complex) HDF5 files directly from S3 or HTTPS sources.  The output is a fully self-contained RSLC HDF5 compatible with isce3, GAMMA Remote Sensing, and SEPPO for interferometric processing and generation of higher-level products (GSLC, GCOV).

---

## Usage

```
seppo_nisar_rslc_convert [-h] -i H5 [H5 ...] [-o OUTPUT]
                         [-vars VARS [VARS ...]] [-f {A,B}] [--all_freq]
                         [-lg]
                         [-srcwin AZ RG AZ_SZ RG_SZ |
                          -coordwin SR0 SR1 ZD0 ZD1 |
                          -projwin ULX ULY LRX LRY]
                         [-projwin_srs CRS]
                         [--max_height M] [--min_height M]
                         [--read_threads N] [--complevel 0-9]
                         [-ql] [--ql_multilook N]
                         [--profile PROFILE]
                         [--input_profile P] [--output_profile P]
                         [-cache DIR] [-keep] [-v]
```

---

## Arguments

### Input / Output

| Argument | Description |
|----------|-------------|
| `-i`, `--h5` | Input RSLC H5 URL(s) or path to a text file containing URLs.  Supports local paths, `s3://`, and `https://` (Earthdata). |
| `-o`, `--output` | Output directory (local or S3).  Required unless `-lg` is used. |

### Variables and Frequency

| Argument | Description |
|----------|-------------|
| `-vars`, `--vars` | Polarisation variables to include, e.g. `HH HV`.  If omitted, all 2-letter uppercase SLC variables are included. |
| `-f {A,B}`, `--freq` | Frequency band.  Default: `A`. |
| `--all_freq` | Include all available frequencies (A and B) in the subset. |

### Inspection

| Argument | Description |
|----------|-------------|
| `-lg`, `--list_grids` | Scan the RSLC file and list all swaths, frequencies, variables, orbit info, and geolocation grid extent, then exit. |

### Subsetting Modes

Exactly one mode may be selected.  If none is given, the full frame is copied.

| Argument | Description |
|----------|-------------|
| `-srcwin AZ_OFF RG_OFF AZ_SIZE RG_SIZE` | Pixel-based subset window (azimuth offset, range offset, azimuth size, range size). |
| `-coordwin SR_START SR_END ZD_START ZD_END` | Coordinate window: slant range start/end in metres, zero-Doppler time start/end in seconds (of day). |
| `-projwin ULX ULY LRX LRY` | Geographic bounding box.  Coordinates are in the CRS given by `-projwin_srs`. |
| `-projwin_srs CRS` | CRS for `-projwin` coordinates.  Default: `EPSG:4326` (lon/lat degrees). |

**How `-projwin` works:**  The on-file geolocationGrid (lon/lat at every radar coordinate) is used as a lookup table to convert the geographic bbox to pixel indices -- no orbit-based geometry computation needed.  This is fast (~1 second) and accurate.  Coordinates given in any other CRS are converted to lon/lat first, with each edge densified before transforming so a rectangle in the source CRS becomes a box covering the curve it maps to.

### Terrain Height

| Argument | Description |
|----------|-------------|
| `--max_height M` | Maximum terrain height (metres).  Selects which geolocationGrid height levels the `-projwin` lookup searches and how far range is padded. |
| `--min_height M` | Minimum terrain height (metres).  Used with `--max_height` for the boundingPolygon height. |

Height matters because it decides which part of the ground the radar window covers: in SAR geometry higher terrain shifts a ground position toward near range, and the boundingPolygon is projected onto a single height, which moves lon/lat by roughly 1/tan(incidence) -- about 1.45 m per metre for NISAR.

**Heights are resolved once per run and reused for every granule.** Precedence:

1. `--max_height` / `--min_height` if given.  Supplied heights are authoritative: nothing is queried, so the run is reproducible, works offline, and works outside the US.  The polygon height is `(max_height + min_height) / 2`.
2. Otherwise a single USGS Elevation Point Query over the requested box (or, for `-srcwin`/`-coordwin`, the first granule's radar corners).  The window uses the maximum sampled height + 500 m; the polygon uses the mean + 500 m.
3. Otherwise 1000 m / 0 m, with a warning.

**For a time series, pass `--max_height` and `--min_height` explicitly.** The USGS service is US-only and can fail transiently, and a run that falls back selects different geolocationGrid height levels than a run that does not -- so the same granule subsetted twice can produce windows of different size and offset, which will not stack.  One explicit height for the whole stack removes that dependency; any error is then a systematic shift shared by every date rather than per-date jitter.

Every subset records what it used, as attributes on `identification/boundingPolygon`:

| Attribute | Meaning |
|-----------|---------|
| `subset_terrain_height_meters` | Height the boundingPolygon was projected onto. |
| `subset_terrain_height_source` | `user`, `usgs` or `default`. |
| `subset_window_max_height_meters` | Height that selected the geolocationGrid levels and range padding. |
| `subset_window_min_height_meters` | Minimum height used. |

Checking that attribute across a stack is enough to tell whether every date was built against the same geometry.

### Quicklook

| Argument | Description |
|----------|-------------|
| `-ql`, `--quicklook` | Generate a backscatter quicklook PNG alongside the H5 output.  Shows detected sigma0 (single-look + multilooked) for each polarisation.  Calibration factors (scaleFactor, sigma0 area) are applied where available. |
| `--ql_multilook N` | Multilook window size for the quicklook.  Default: `5`. |

### Authentication

| Argument | Description |
|----------|-------------|
| `--profile` | AWS profile name (applies to both input and output unless overridden). |
| `--input_profile` | AWS profile for reading input H5 files. |
| `--output_profile` | AWS profile for writing output files. |

Earthdata credentials are auto-detected for ASF DAAC S3 buckets and Earthdata HTTPS URLs.

### Caching

| Argument | Description |
|----------|-------------|
| `-cache DIR` | Cache remote H5 files locally before reading.  Use `y` for automatic temp directory.  Recommended for large subsets. |
| `-keep` | Retain cached files after processing. |

**When to use `-cache`:**  For small subsets (`-projwin` with a small bbox), caching is unnecessary -- the tool reads only the needed chunks via HTTP range requests.  For large subsets (>25% of the frame), caching the full file first is faster because it avoids thousands of individual range requests.

### Performance

| Argument | Description |
|----------|-------------|
| `--read_threads N` | Parallel readers for the SLC image data (default 8, `1` disables).  Each is a subprocess reading one chunk-aligned azimuth stripe, fetching its chunks as coalesced byte ranges.  Helps remote input only. |
| `--complevel 0-9` | gzip level for the SLC image data and masks.  Omitted, the source's setting is mirrored (gzip/4).  `1` writes ~35% faster for ~1% more file; `0` stores uncompressed.  Values are unchanged either way. |

### Miscellaneous

| Argument | Description |
|----------|-------------|
| `-v`, `--verbose` | Verbose output with per-phase timings, and read/write split per polarisation. |

---

## How It Works

The tool uses NISAR's cloud-optimised HDF5 layout:

1. **Metadata at the front** -- orbit, attitude, calibration scalars, coordinate arrays, and identification data are packed in the first ~8 MB of the file.  These are read efficiently with minimal range requests.

2. **geolocationGrid for bbox lookup** -- the on-file `coordinateX` (longitude) and `coordinateY` (latitude) arrays at each `(height, zeroDopplerTime, slantRange)` grid point provide a direct lon/lat-to-pixel mapping.  No orbit-based geo2rdr computation needed.

3. **Targeted SLC reads** -- only the HDF5 chunks containing the requested subset are downloaded via HTTP range requests.

4. **Explicit output construction** -- every group and dataset in the output is enumerated explicitly.  No blind deep-copy.  Metadata grids (geolocationGrid, calibration geometry, antenna patterns, dopplerCentroid) are subsetted to cover only the subset extent.

5. **Complete isce3 compatibility** -- the output preserves all metadata required by isce3: orbit, attitude, calibration, geolocation grids, processing information, and identification (with updated zeroDopplerStartTime/EndTime and boundingPolygon).

---

## Output Structure

The subsetted H5 file contains:

| Group | Content |
|-------|---------|
| `/science/LSAR/identification/` | All fields copied; `zeroDopplerStartTime`, `zeroDopplerEndTime`, and `boundingPolygon` updated for the subset. |
| `/science/LSAR/RSLC/metadata/orbit/` | Full orbit state vectors (verbatim). |
| `/science/LSAR/RSLC/metadata/attitude/` | Full attitude data (verbatim). |
| `/science/LSAR/RSLC/metadata/calibrationInformation/` | Per-frequency and per-polarisation calibration.  Unrequested polarisations are skipped.  Antenna patterns and geometry grids subsetted. |
| `/science/LSAR/RSLC/metadata/geolocationGrid/` | 3-D grids (coordinateX/Y, incidenceAngle, etc.) subsetted to cover the subset extent. |
| `/science/LSAR/RSLC/metadata/processingInformation/` | Algorithms, inputs copied verbatim.  dopplerCentroid and coordinate arrays subsetted. |
| `/science/LSAR/RSLC/swaths/zeroDopplerTime` | Subsetted to azimuth window. |
| `/science/LSAR/RSLC/swaths/frequency{A,B}/` | `slantRange` subsetted.  SLC data subsetted.  `validSamplesSubSwath1` subsetted and index-adjusted.  `listOfPolarizations` updated. Scalar metadata copied verbatim. |

---

## Performance

Tested from a laptop over HTTPS (Earthdata) against a 27 GB NISAR RSLC file (54720 az x 54548 rg, dual-pol HH+HV, L-band).

| Subset size | Pixels | SLC data | Time | Notes |
|-------------|--------|----------|------|-------|
| 0.1° x 0.1° bbox, 1 pol | 2,817 x 321 | 7.2 MB | ~26 s | Metadata reads dominate |
| 0.4° x 0.4° bbox, 1 pol | 11,567 x 8,664 | 802 MB | ~2 min | SLC read dominates |
| 0.4° x 0.4° bbox, 2 pol | 11,567 x 8,664 | 1.6 GB | ~4.5 min | 2x SLC reads + S3 upload |
| Full frame, 1 pol | 54,720 x 54,548 | ~11 GB | Use `-cache y` | Cache first, then subset |

**From us-west-2 EC2 (in-region S3):**  Expect 5-10x faster SLC reads due to low-latency S3 access.

**Where the time goes.**  A subset is read + write.  The read is a set of chunk byte ranges taken from the granule's chunk index and fetched as a few dozen coalesced requests, spread over `--read_threads` workers; it only matters for remote input.  The write is single-process gzip and is the same everywhere, which is what `--complevel` acts on.  Measured on an 11467 x 9934 dual-pol subset (870 MB out):

| Input | v0.7.1 | defaults | with `--complevel 1` |
|-------|--------|----------|----------------------|
| local file | 45 s | 45 s | 30 s |
| `s3://`, in-region | 87 s | 52 s | 36 s |
| HTTPS, in-region | 163 s | 62 s | 50 s |
| HTTPS, laptop over public internet (25 Mbps) | -- | -- | 7 min |

On a slow link the read is bandwidth-bound and little else helps: the laptop figure above is within 5% of the time its bandwidth alone requires for the ~950 MB the two polarisations need.  Fetching less is then the only lever -- `-vars HH` halves it, and a smaller `-projwin` scales linearly.

**Timing breakdown** (typical small subset, no cache):
- Earthdata login: instant (cached JWT token)
- Metadata reads (orbit, attitude, cal, geolocation grid): ~5-15 s
- geolocationGrid bbox lookup: <1 s
- SLC data read (targeted range requests): proportional to data size
- Output write: <1 s for small subsets

---

## Examples

### 1. Inspect an RSLC file

```bash
seppo_nisar_rslc_convert -i file.h5 -lg
```

### 2. Subset by geographic bounding box (lon/lat)

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5 \
    -o /tmp/subset/ \
    -projwin -104.8 46.7 -104.4 46.3 \
    -vars HH \
    -v
```

### 3. Dual-pol subset with quicklook

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5 \
    -o /tmp/subset/ \
    -projwin -104.8 46.7 -104.4 46.3 \
    -vars HH HV \
    -ql -v
```

### 4. Subset by pixel window

```bash
seppo_nisar_rslc_convert \
    -i file.h5 -o out/ \
    -srcwin 1000 2000 5000 3000
```

### 5. Subset by radar coordinate window

```bash
seppo_nisar_rslc_convert \
    -i file.h5 -o out/ \
    -coordwin 900000 950000 43020.0 43030.0
```

### 6. Output to S3

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5 \
    -o s3://mybucket/rslc_subsets/ \
    -projwin -104.8 46.7 -104.4 46.3 \
    -vars HH HV \
    --output_profile myprofile \
    -v
```

### 7. Large subset with local caching

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5 \
    -o out/ \
    -projwin -107.0 47.5 -103.5 46.0 \
    -cache y \
    -v
```

### 8. Batch processing from a URL list

```bash
# urls.txt contains one RSLC URL per line
seppo_nisar_rslc_convert \
    -i urls.txt -o out/ \
    -projwin -104.8 46.7 -104.4 46.3 \
    -vars HH -v
```

### 9. Both frequencies

```bash
seppo_nisar_rslc_convert \
    -i file.h5 -o out/ \
    --all_freq \
    -projwin -104.8 46.7 -104.4 46.3 \
    -v
```
