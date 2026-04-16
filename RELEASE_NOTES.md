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
