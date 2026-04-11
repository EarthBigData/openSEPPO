# v0.5.0

**New tools**
- `seppo_nisar_rslc_convert`: subset NISAR L-band RSLC HDF5 files directly from S3/HTTPS with cloud-optimised I/O.  Output is a fully self-contained RSLC HDF5 compatible with isce3, GAMMA Remote Sensing, and SEPPO for interferometric processing and generation of higher-level products (GSLC, GCOV).

**RSLC subsetting features**
- Three subsetting modes: pixel window (`-srcwin`), radar coordinate window (`-coordwin`), and geographic bounding box (`-projwin` with lon/lat)
- Geographic bbox lookup via the on-file geolocationGrid (coordinateX/Y) -- no orbit-based computation needed, fast and accurate
- Cloud-optimised: reads only needed HDF5 chunks via HTTP range requests; no full-file download required for small subsets
- Complete metadata preservation for isce3: orbit, attitude, calibration, geolocation grids, processing information all included
- Metadata grids (geolocationGrid, calibration geometry, antenna patterns, dopplerCentroid) are subsetted to the subset extent for compact output
- Unrequested polarisation calibration groups are skipped to reduce output size and I/O
- Per-frequency range support: freq A and B can have different range dimensions
- Quicklook generation (`-ql`): detected sigma0 with calibration factors applied, single-look + multilooked
- Batch processing from URL lists
- Output to local or S3

**New module**
- `openseppo.nisar.radar_geometry`: pure-Python/numpy SAR geometry routines (Hermite orbit interpolation, rdr2geo, geo2rdr) following isce3 algorithms.  No compiled-library dependency.

**Performance (27 GB RSLC over HTTPS, no caching)**
- Small subset (0.1 x 0.1 deg, 1 pol): ~26 s, 4.8 MB output
- Medium subset (0.4 x 0.4 deg, 1 pol): ~2 min, 502 MB output
- Medium subset (0.4 x 0.4 deg, 2 pol): ~4.5 min, 995 MB output

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
