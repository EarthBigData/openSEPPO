# v0.7.1

**GSLC / GCOV: self-contained `-of h5` subsets**
- `-of h5` now writes a complete, standalone NISAR product rather than bare grid arrays. The subset carries `/science/LSAR/identification/` (all fields), and the product's `metadata/` tree — `orbit` and `attitude` verbatim, `processingInformation` and `calibrationInformation` windowed where they sit on a map grid. GCOV subsets went from 5 datasets to 306; GSLC from a grid-only file to 257.
- Grid groups carry `xCoordinates`/`yCoordinates`, `projection`, `listOfPolarizations`, `validSamplesSubSwath`, and every 2-D array posted on that frequency's own grid (SLC/covariance variables, `mask`, `inputDataExceptionMask`), with the source's chunking and compression preserved. Scalars are copied verbatim.
- GCOV `-of h5` output remains NetCDF-4/GDAL-readable; complex GSLC variables are written as NetCDF compound `(r, i)` types, which `h5py` reads back as `complex64`.

**Frequency selection: follows the granule, not a fixed default**
- An unset `-f` no longer hard-defaults to frequency A. It is resolved from the granule name's polarization field, which carries two characters per frequency with the literal `NA` where a frequency was not acquired: `DHDH` = both, `NADV` = no frequency A, `DHNA` = no frequency B. A frequency-B-only granule (`NADV`) previously produced **no raster output at all**, failing with `Error: Frequency A not found in file or has no variables`.
- Applies to `seppo_nisar_gcov_convert`, `seppo_nisar_gslc_convert` and `seppo_nisar_rslc_convert`. RSLC's `-f` default changed from `A` to unset; naming a frequency explicitly behaves exactly as before.
- `--all_freq` added to `seppo_nisar_gcov_convert` and `seppo_nisar_gslc_convert`, matching the flag RSLC already had: for `-of h5` it writes every frequency present, each windowed on its **own** grid. Frequencies are not co-posted — in a `DHDH` granule GCOV frequency B is 80 m against A's 10 m, and GSLC frequency B is 40 m in x but 5 m in y — so each window is recomputed per frequency rather than reusing A's indices. Passing `--all_freq` with raster output raises `ValueError`: A and B cannot share one raster.
- `listOfFrequencies` now names what was actually written. Previously a single-frequency subset of a two-frequency granule inherited the source's list and claimed both. Fixed for GCOV, GSLC and RSLC.

**Stacks must be single-mode**
- All three converters now refuse an `-i` list that mixes acquisition modes, naming every mode found and exiting non-zero. Mode and polarization decide which frequencies exist and how grids are posted, so a mixed list cannot produce a coherent time series.

**Bounding polygons**
- GSLC/GCOV `-of h5` subsets recompute `boundingPolygon` for the subset window instead of inheriting the full-granule footprint. The polygon mirrors isce3's `make_geo_grid_bounding_polygon`: 11 points per edge (41 vertices), counter-clockwise from the upper-left, 3-D `lon lat height` vertices, extent taken to the outer pixel **edges** rather than centres. Sampling every edge matters because straight lines in the product CRS are curved in lon/lat. If the recomputation fails the field is removed with a warning rather than silently left describing the whole granule.
- Fixed invalid WKT in the subset writer: coordinate pairs were space-separated instead of comma-separated, so `-lg` reported footprints with duplicated and transposed corners. (The same defect was fixed for RSLC `corners_to_wkt` in v0.5.3.)
- RSLC `boundingPolygon` rebuilt to match isce3's `getGeoPerimeter`, the routine the RSLC focus workflow itself uses: 41 vertices instead of 4, 3-D instead of 2-D, and interpolation performed in radar (time, range) coordinates with each point converted through `rdr2geo` — not interpolated in lon/lat.
- **Fixed clockwise winding on left-looking granules.** The corner order was fixed at near-early → far-early → far-late → near-late, which isce3 defines only for right-looking geometry; left-looking requires near-early → near-late → far-late → far-early to come out counter-clockwise on the map. Left-looking subsets were producing rings wound opposite to the source granule and to the OGR Simple Features convention.
- RSLC polygon height is now a single constant, the mean of the four corner elevation queries, replacing the per-corner heights of v0.5.3. Height is not bookkeeping — it is the surface `rdr2geo` projects onto, so it moves lon/lat by roughly 1/tan(incidence), about 1.45 m per metre for NISAR. Interpolating heights along an edge would assert a terrain profile nothing supports, since real relief can depart from the corner-to-corner line by more than the corners differ from each other.

**GSLC `-of h5`: swath extents describe the subset, not the source granule**
- `metadata/sourceData/swaths` and `identification/zeroDopplerStartTime`/`EndTime` are now narrowed to the window. They were inherited verbatim from the source, so a subset advertised the full granule's `numberOfRangeSamples`/`numberOfAzimuthLines` — a 15 km GSLC window reported 52648 × 30400 rather than 6733 × 4549, and readers that derive radar-geometry parameters from those fields emitted sample counts contradicting the product's own geocoded grid.
- Every value — `numberOfRangeSamples` per frequency, `numberOfAzimuthLines`, `slantRangeStart`, near/far incidence angle, and the zero-Doppler times — is derived from the already-subsetted `metadata/radarGrid` cube, so the group stays mutually consistent rather than having one field moved out from under `slantRangeStart` beside it.
- Extents are clamped to the values inherited from the source: the cube is written with a margin so it brackets the window, and unclamped its extremes reached past the granule's own end time.
- A geocoded window is a map rectangle whose radar footprint is a skewed quadrilateral, so these are *bounding* radar extents. Each rewritten field's `description` attribute records that, and the fields are left untouched with a warning if the cube is missing or all-NaN.

**Subset windows: covering, not nearest**
- Window indices are now chosen by cell overlap rather than nearest-node snapping, so the subset covers every cell the requested extent touches. A coarse second frequency previously came out a row and a column short of the extent its companion covered.

**Cloud read performance**
- `open_h5_lazy` opens paged HDF5 files with fsspec `cache_type="blockcache"` (an LRU of page-aligned blocks) instead of a single-region byte cache, with the block size derived from the file's own page size rather than assumed. `probe_h5_page_params` reads `get_file_space_strategy()`/`get_file_space_page_size()` once per file and caches the result; non-paged files keep the previous 4 MiB default.
- The HDF5 page buffer is sized from the same probe (`page_buf_size`, capped at 64 MiB), with a fallback open if the library rejects it.
- Added `parallel_read_datasets`, a process-pool reader for the scattered metadata arrays that dominate a subset's latency (`h5py`'s global lock rules out threads). It degrades to a serial read when `__main__` is not an importable file, so interactive sessions, `python -` and `python -c` still work.
- End-to-end for a GSLC `-of h5` subset from S3 in-region: 2m38s → 35.4 s with the access-layer change, → 18.9 s with the parallel reader. Over HTTPS: 1m38s → 53 s. These are all **in-region** figures; see the next entry for what the same code did from outside the region.
- **The blockcache above reached only `s3://`; HTTPS now gets it too.** `open_h5_lazy`'s Earthdata HTTPS branch handed the URL to `earthaccess.open()`, which returns a file wrapped in fsspec's default 16 MiB `BackgroundBlockCache` — large blocks plus a speculative next-block prefetch, neither of which suits the scattered reads an HDF5 subset issues. A NISAR granule's ~200 metadata datasets sit roughly one per 4 MiB page, so each cost a 16 MiB block and a discarded prefetch. The branch now opens through `earthaccess.get_fsspec_https_session()` with the same page-aligned `cache_type="blockcache"` the S3 branch uses, falling back to `earthaccess.open()` if the session cannot be built. For one GSLC window this cut bytes fetched from 3825 MB to 981 MB — a ~100 MB subset had been costing nearly 4 GB of transfer.
- The effect is dominated by how much bandwidth the client has. From a laptop over the public internet the same GSLC `-of h5` command went from **14m19s to about 5 minutes**; in-region, where the wasted bytes were nearly free, 63.3 s → 53.6 s. Output is unchanged: the subset written before and after is byte-for-byte identical, since only the transport's caching policy differs.
- No HDF5 page buffer is used on the HTTPS path. It measured 977 MB against 981 MB without — no benefit — and it would not repay the extra remote open that probing the page size over HTTPS costs.
- `--read_threads` is now wired up; it was previously documented as reserved. Leave it at its default for remote input, including HTTPS from outside the region: the worker processes each re-open the granule and so re-read its shared group tree, but that costs only ~26% more bytes and is repaid several times over, because a single TCP stream cannot fill a long-haul path. Measured from a laptop, `--read_threads 1` took about 9 minutes against 8 workers' 5 minutes.

# v0.7.0

**Search: `asf_search` backend (`-asf`)**
- `seppo_nisar_search` can now query via the `asf_search` package (ASF SearchAPI) as an alternative to the default direct-CMR query: pass `-asf`/`--asf_search`. Column, spatial, and time filters map to native asf_search parameters; remaining filters are post-applied in Python, so the two backends return identical `url` (s3://) and `url_https` results for the same query. Requires `asf_search` (`mamba install -c conda-forge asf_search` or `pip install asf-search`); imported lazily so it is only needed when `-asf` is used.

**Search: all collection versions covered automatically**
- The CMR path now searches **every** collection version of a product in a single request via a wildcard short-name pattern (`NISAR_{level|*}_{product}_*` with the CMR pattern option), instead of the previous static BETA-only list. New collection tiers (currently `BETA_V1` and `PROVISIONAL_V1`, operational versions later) are picked up automatically with no code change. Previously the tool returned only `*_BETA_V1` granules and silently missed the newer `*_PROVISIONAL_V1` products.

**Search: latest-release selection and collection filtering**
- By default only the **latest release** of each scene is returned: the newest collection tier wins (unknown/future tiers rank above known ones; the `V{n}` version breaks ties), then the highest CRID. Urgent Response (UR) and standard products of the same acquisition are kept distinct.
- New `collection` column records each granule's originating collection (e.g. `NISAR_L2_GCOV_PROVISIONAL_V1`), available in csv/json/geojson/kml output.
- New `--collection` post-filter keeps a specific tier, with LIKE wildcards, e.g. `--collection '%PROVISIONAL%'`.
- `--allcrids` now returns every collection **and** CRID version (previously CRID versions only).

**Search: Urgent Response products (`-ur`)**
- Urgent Response products (proctype `UR`, in the `NISAR_UR_L1`/`NISAR_UR_L2` collections) are now **excluded by default**. Pass `-ur`/`--urgent_response` to include them; the flag adds the UR collections to the CMR query and keeps UR results from the asf path, so both backends stay in parity.

**Search: bug fixes**
- Fixed LIKE wildcard matching (`%`) in all text filters (`--bucket`, `--url_pattern`, `--collection`): patterns silently matched nothing on Python ≥ 3.7 because `re.escape` no longer escapes `%`.
- `*STATS.h5` QA sidecar files are now excluded from results on both search paths (the CMR path previously included them, which made per-scene deduplication fragile).

# v0.6.0

**GCOV / GSLC: subswath mask applied by default**
- `seppo_nisar_gcov_convert` and `seppo_nisar_gslc_convert` now apply the product's subswath `mask` grid to the backscatter by default when writing non-`h5` output (COG/GTiff). The `mask` encodes the subswath number of each valid sample: `1`–`254` = valid subswath, `0` = invalid (multilooking/interpolation ensemble not fully focused), `255` = fill (outside the radar acquisition extent). Pixels flagged `0` or `255` are set to nodata (GCOV: NaN/0; GSLC: complex `0+0j`), keeping only valid-subswath pixels.
- Masking is applied at the source resolution **before** any downscaling, transform, or resampling, so fill/invalid pixels never contaminate block-averaging or the warp kernel.
- Added `-nomask`/`--nomask` to both tools to opt out and keep all pixels. Masking has no effect on `-of h5` output (raw subset written unchanged) and is skipped silently when the file has no `mask` grid.

# v0.5.3

**RSLC subsetting: terrain-aware bounding polygon**
- `boundingPolygon` metadata is now computed with per-corner terrain heights via USGS Elevation Point Query Service (+500m buffer), producing an accurate ground footprint that accounts for height-dependent SAR range shift at each corner individually
- Added `--min_height` CLI flag (complements `--max_height`) for non-US scenes where USGS is unavailable; near-range corners use `max_height`, far-range corners use `min_height` as fallback
- Fixed `corners_to_wkt` to produce valid WKT with comma-separated coordinate pairs (was missing commas, causing downstream WKT parsers to fail)

**RSLC subsetting: height-aware pixel clipping**
- `_bbox_to_pixels` height resolution factored out into `_resolve_max_height` for reuse
- Added `_query_elevation_point` (single-point USGS lookup) and `_query_corner_elevations` (per-corner with directional fallbacks: near-range→max_height, far-range→min_height)

**Search tool**
- `seppo_nisar_search`: banner no longer printed when output goes to stdout (URLs, GeoJSON); only prints when `--output` file is specified, keeping stdout clean for piping and programmatic use

**Test infrastructure**
- Added `tests/rslc_subset/generate_rslc_subset_tests.py`: generic corner test generator that searches RSLC scenes, computes 6 subset bounding boxes per scene (target point + frame center + 4 corners at polygon vertices), and generates GeoJSON, subsetting scripts, and RTC scripts
- Scripts support configurable bucket/prefix via environment variables (`BUCKET`, `S3PREFIX`, `OUTDIR`)
- RTC scripts note SEPPO SARISCE module requirement

# v0.5.2

**RSLC subsetting fixes**
- Fixed `_bbox_to_pixels` to use ALL height levels of the geolocationGrid (union across -500m to 9000m). Previously only used h=0, causing subsets to be shifted east in areas with significant terrain (e.g. Hawaii volcanoes).
- Fixed `validSamplesSubSwath` adjustment to use inclusive index convention matching the NISAR spec. Previously could produce `last_valid` one past the end of the subset array.
- Properly handle `[0,0]` no-data convention and lines entirely outside the subset range.
- Subsetted datasets now preserve all source HDF5 attributes (including `units`), fixing isce3 `Attribute 'units' not found` errors.
- SLC dataset chunk size and compression settings now match the source file (was hardcoded to 128x512 / gzip level 4).
- `validSamplesSubSwath` dtype preserved as source (`uint32`), storage as uncompressed contiguous matching source format.
- Added `_create_ds` helper to ensure all subsetted metadata datasets inherit source attributes.
- Version banner (`*** openSEPPO <prog> <version> (<date>) ***`) printed on all CLI tool invocations.

# v0.5.1

**Bug fixes**
- Fixed mkdocs navigation: added RSLC, GSLC, coherence CLI references, Hawaii examples, and Python API guide to sidebar
- Fixed TOC anchor links in index.md for mkdocs compatibility
- Fixed stale link to renamed `gcov_processing_overview.md`
- Updated installation docs: complete CLI tools table, corrected dependency descriptions
- S3 write permission check: fixed `--dryrun` approach (does not test permissions); now uses multipart upload initiate/abort via `aws s3api` (no objects created)
- Fixed `/dev/null` warning in S3 write check
- Added matplotlib to all installation instructions (required for RSLC quicklook generation)

# v0.5.0

**New tool: `seppo_nisar_rslc_convert`**
- Subset NISAR L-band RSLC HDF5 files directly from S3/HTTPS with cloud-optimised I/O
- Output is a fully self-contained RSLC HDF5 compatible with isce3, GAMMA Remote Sensing, and SEPPO for interferometric processing and generation of higher-level products (GSLC, GCOV)
- Three subsetting modes: pixel window (`-srcwin`), radar coordinate window (`-coordwin`), and geographic bounding box (`-projwin` with lon/lat)
- Geographic bbox lookup via the on-file geolocationGrid (coordinateX/Y) -- no orbit-based computation needed, fast and accurate
- Cloud-optimised: reads only needed HDF5 chunks via HTTP range requests; no full-file download required for small subsets
- Complete metadata preservation for isce3: orbit, attitude, calibration, geolocation grids, processing information all included
- Metadata grids (geolocationGrid, calibration geometry, antenna patterns, dopplerCentroid) are subsetted to the subset extent for compact output
- Unrequested polarisation calibration groups are skipped to reduce output size and I/O
- Per-frequency range support: freq A and B can have different range dimensions
- Quicklook generation (`-ql`): detected sigma0 with calibration factors applied, single-look + multilooked, per-polarisation panels
- Batch processing from URL lists
- Output to local or S3

**New module: `openseppo.nisar.radar_geometry`**
- Pure-Python/numpy SAR geometry routines (Hermite orbit interpolation, rdr2geo, geo2rdr) following isce3 algorithms
- No compiled-library dependency (no isce3/conda required)
- Used for bounding polygon recomputation in RSLC subsets

**GSLC improvements**
- HDF5 subset (`-of h5`) now includes complete metadata: orbit, attitude, identification, calibration, processing information, mask, and all scalar parameters
- Output compression matches source file settings (gzip level + shuffle)
- Fully compatible with isce3, GAMMA Remote Sensing, and SEPPO

**Improvements across all tools**
- S3 write permission check: validates credentials and bucket access before processing using multipart upload initiate/abort (no objects created)
- VRT summary output: path/bucket shown only at end; time-series sections omitted for single-date runs

**Performance (RSLC, 27 GB file over HTTPS, no caching)**
- Kilauea subset (~13 x 11 km, 1 pol): ~32 s, 43 MB output
- Kilauea subset (~13 x 11 km, 2 pol): ~59 s, 86 MB output
- Medium subset (~40 x 40 km, 1 pol): ~2 min, 502 MB output
- Medium subset (~40 x 40 km, 2 pol): ~4.5 min, 995 MB output

**Documentation**
- New comprehensive documentation with TL;DR quick start, command-line examples by product type (GCOV, GSLC, RSLC), and GIS visualization guide
- Hawaii Volcanoes National Park (Kilauea) examples with real URLs for all product types
- Python API / Jupyter integration guide
- CLI reference for `seppo_nisar_rslc_convert`

# v0.4.0

**New tools**
- `seppo_nisar_gslc_convert`: convert NISAR GSLC HDF5 to COG/GTiff/h5; output modes: power, amplitude (GCOV-compatible uint16), magnitude, wrapped phase, raw complex SLC (`-cslc`); supports reprojection, downscaling (`-d Nx Ny`, `--square`), spatial subsetting, and VRT time-series stacking
- `seppo_nisar_coherence`: compute pairwise interferometric coherence from co-registered GSLC complex SLC files; configurable boxcar window, sequential or all-pairs mode, uint8 DN or float32 output, post-processing crop/downscale/reproject, VRT stacking

**New features across tools**
- `-projwin_srs`: supply `-projwin` coordinates in any CRS (e.g. `EPSG:4326`); automatically reprojected to native raster CRS before subsetting, with extra margin to fully cover the output after any warping
- `-d Nx Ny`: anisotropic downscaling — set range and azimuth (or X and Y) factors independently; single value applies the same factor to both axes
- `seppo_nisar_search`: product type (GSLC, GCOV, …) now included in output filenames and section headers
- `seppo_nisar_coherence`: post-processing flags `-projwin`, `-projwin_srs`, `-d`, `-t_srs`, `-tr` applied after coherence estimation (order: crop → downscale → reproject)

**Bug fixes**
- GSLC converter: fixed incorrect pixel size and array dimensions when reading grid coordinates via datatree; now always uses h5py for grid metadata
- Coherence reprojection: fixed `calculate_default_transform` error when passing explicit bounds alongside resolution

# v0.3.0

- seppo_nisar_gcov_convert_S: Enables NISAR S-band support

# v0.2.0

- add -sigma0 flag to enable gamma0 to sigma0 conversion on-the-fly

# v0.1.3

- NISAR L-band GCOV hd5 convertsion and subsetting tool (COG, GTiff, h5 formats)
