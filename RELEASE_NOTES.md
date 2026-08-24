# v0.7.2

**Subsets carry `inputDataExceptionMask`**
- RSLC subsets dropped it entirely. The subsetter wrote an explicit list of swath datasets — `slantRange`, the scalars, `listOfPolarizations`, `validSamplesSubSwath*` and the SLC payload — and the per-pixel exception mask was named nowhere, so every subset silently lost the processor's record of which input samples were flagged anomalous. Any array posted on the frequency's own (azimuth, range) swath grid is now windowed with the same indices as the SLC payload, preserving dtype, chunking, compression and attributes; 1-D arrays on either axis are sliced, scalars copied verbatim, and anything on neither grid is reported rather than silently omitted. Verified byte-identical to the source window on both frequencies, each on its own range grid (A: 53676 samples, B: 6710). The mask is uint8 and near-uniform, so it added ~2 KB to an 8.3 MB subset.
- GCOV `-of h5` wrote only the covariance terms the caller asked for, so the subset was missing not just the exception mask but `mask`, `numberOfLooks`, `rtcGammaToSigmaFactor`, `listOfCovarianceTerms`, `listOfPolarizations` and the scalar spacings. `mask` and `rtcGammaToSigmaFactor` are the ones that matter: without them the subset cannot be subswath-masked or gamma-to-sigma converted the way the granule it came from can. Every array posted on that frequency's own grid is now windowed, scalars and short vectors copied. Each grid costs one windowed read — the same as a covariance term — and no array is ever read whole.
- GSLC `-of h5` already carried it; confirmed byte-identical to the source window against a granule.
- Integer flag grids keep their dtype. The GCOV writer cast every data variable to float32, which turned `mask`'s 255 fill and the exception codes into floating-point values something downstream may interpolate. They are written as uint8 now, carrying the source `_FillValue`, which netCDF accepts only at variable creation. A grid named explicitly through `-vars` takes the same path, so how a grid was asked for no longer decides its dtype.
- `listOfCovarianceTerms` and `listOfPolarizations` name what was actually written: `-vars HHHH` on a dual-pol granule yields `['HHHH']` and `['HH']` rather than inheriting the source's full list. This is the defect fixed for `listOfFrequencies` in v0.7.1, in the two vectors beside it.
- Each windowed grid keeps the source's own deflate level — gzip/1 for the float grids, gzip/9 for the near-uniform flag grids — instead of recompressing everything at level 4.
- In both GCOV and GSLC the source stores `frequencyB/inputDataExceptionMask` at frequency **A's** shape: 34704 x 35208 against B's own 4338 x 4401 in GCOV, 69408 x 70416 against 8802 in GSLC, with storage size byte-identical to A's array. It cannot be windowed on B's grid, and copying it verbatim would drag the full frame into the subset, so it is omitted with a warning naming both shapes. B's own `mask`, `numberOfLooks` and `rtcGammaToSigmaFactor` are windowed normally.

**RSLC `-projwin_srs` was accepted and then ignored**
- `seppo_nisar_rslc_convert` took `-projwin_srs`, passed it down to the file processor, and never used it. The `-projwin` corners went straight into the geolocationGrid lookup, which reads lon/lat, so any CRS other than EPSG:4326 was interpreted as degrees. A UTM box raised `ValueError: No geolocation grid points inside bbox [500000.0, 4752000.0, ...]` from inside the lookup — and coordinates that happen to land in a valid lon/lat range would have subsetted the wrong ground silently.
- The bbox is now converted to EPSG:4326 before the lookup with `reproject_projwin`, the same helper the GSLC converter uses, which densifies each edge before transforming so a rectangle in the source CRS becomes a box that covers the curve it maps to. A bare EPSG code (`-projwin_srs 32619`) is accepted alongside `EPSG:32619`, and EPSG:4326 input takes an exact fast path rather than a round trip.
- Verified on a granule: `-projwin -72.0 43.0 -71.9 42.9` and the same box in UTM 19N with `-projwin_srs EPSG:32619` select the same range window and azimuth windows agreeing to ~100 lines, the difference being the covering box a curved UTM rectangle requires. GCOV and GSLC already handled `-projwin_srs` correctly.

**RSLC subsetting speed: coalesced chunk reads, `--read_threads`, `--complevel`**
- The SLC payload is read from the granule's own chunk index rather than chunk by chunk through a generic block cache. The chunks a window needs are largely contiguous in the file -- for one 11467 x 9934 window, 480 chunks form 47 ranges for 5% more bytes -- so they are fetched as a few dozen large requests instead of a few hundred small ones. Every DAAC range request costs a fixed overhead, measured at ~0.3 s in-region and ~1 s from a laptop, so request count matters more than bytes on any high-latency path.
- `--read_threads N` (default 8, `1` disables) spreads those reads over subprocess workers, each taking one chunk-aligned azimuth stripe. Ranges are read sequentially within a worker: the workers already supply the concurrency, and multiplying it inside each one congests a narrow link -- doing so measured *slower* than no coalescing at all from a laptop.
- `--complevel 0-9` sets the gzip level for the payload and masks; omitted, the source's own setting is mirrored as before. Compression is single-process and is the largest remaining cost once reads are fast, so level 1 writes noticeably quicker for about 1% more file, and 0 stores uncompressed. The data is identical at every level -- only the container's packing changes.
- Measured on an 11467 x 9934 dual-pol subset, against v0.7.1: local 45 s unchanged (never read-bound) but 30 s at level 1; S3 in-region 87 s -> 52 s -> 36 s; HTTPS in-region 163 s -> 62 s -> 50 s; and from a laptop over the public internet, 10 min -> 7 min, which is within 5% of what that link's bandwidth alone requires. Output is byte-for-byte identical to v0.7.1 at the default compression.
- Verbose output now reports per-phase timings and splits each polarisation into read and write, which is what makes these costs separable.

**`-projwin` in degrees against a projected grid no longer subsets nothing**
- `-projwin` is in the granule's own CRS unless `-projwin_srs` says otherwise, so lon/lat corners against a UTM grid were read as metres.  Nothing rejected them: the window is found by snapping each corner to the nearest grid node, so a box far off the grid clamped to an edge pixel, and `get_indices_from_extent`'s guard against a zero-width window turned that into a 1x1 subset that was written and reported as `[OK]`.  The same happened for any `-projwin` that simply missed the granule -- in a batch over a time series, every date not covering the AOI wrote a 1x1 tile and the VRT stack was built over them.
- A y-delta under 5 is now read as EPSG:4326.  A projected NISAR grid is in metres and spans tens of thousands of them north to south, so 5 is below one pixel of a 10 m grid and cannot be a real window; read as degrees it is an ordinary request.  An explicit `-projwin_srs` is always honoured, a granule already on a geographic grid is untouched, and the substitution is announced rather than silent.  GCOV and GSLC both take this path.
- Verified on a granule: the bare degree box now selects 25535,17782,4124,4057 -- the same window as passing `-projwin_srs 4326` -- and a genuine UTM box of the same ground selects it too, with all three writing identical subsets.

**GCOV subsetting speed: coalesced chunk reads for `-of h5`, and parallel reads over HTTPS too**
- Raster output read the payload through parallel subprocess workers for `s3://` only; HTTPS fell back to serial reads on the belief that concurrency does not pay off there.  It does -- the same finding the GSLC converter already acts on -- so `--read_threads` now applies to both, and each worker opens through the page-aligned block cache rather than a hand-rolled 16 MB one.
- `-of h5` copies ~225 small metadata datasets, each its own round trip.  They are now planned and fetched concurrently before the write, the arrangement the GSLC subsetter uses: one traversal collects the paths and slices, the next writes from what came back, so the plan cannot drift from what is written.  Local input is untouched -- there is nothing to overlap.
- `-of h5` read its payload one hyperslab at a time.  A 4124 x 4057 window spans 81 of the granule's 512x512 chunks, and the covariance terms and the four ancillary grids each span the same 81, so a subset issued 486 chunk requests back to back.  Those reads now go through the coalescing parallel reader written for the RSLC subsetter -- chunk ranges taken from the granule's own index, 486 requests down to 110, spread over `--read_threads` workers.  That reader now lives in `nisar_tools` and both subsetters use it.
- **Stripe boundaries are snapped to the chunk grid in absolute file coordinates**, not merely made a multiple of the chunk height.  A window whose first row is not chunk-aligned -- 17782 against a 512 grid -- otherwise gave every stripe a share of two chunk rows, so each boundary chunk was fetched and decompressed by two workers: 81 distinct chunks read as 162, and 213.9 MB of wanted data fetched as 413.6 MB.  Aligned, the same window transfers 219.1 MB, 2.4% above the minimum.  This was in the RSLC reader from the start and is fixed for both: an 11467 x 9934 RSLC window goes from 1286 MB in 145 requests to 997 MB in 107.
- Coalescing is now bounded by how much it wastes, not by gap size alone: two chunks join one request when the gap is small *and* merging keeps the run's skipped bytes under 10% of its useful ones.  How close a window's chunks lie depends on how much of the raster width it spans, which differs by an order of magnitude between an RSLC and a GCOV subset, so a fixed gap cannot be safe for both.
- Measured in-region on a 4124 x 4057 dual-pol frequency-A window.  COG: local 3.5 s unchanged, `s3://` 8.2 s -> 5.2 s, HTTPS 19.8 s -> 11.9 s (the read itself 14.7 s -> 6.4 s).  `-of h5`: local 10.1 s unchanged (never read-bound), `s3://` 54 s -> 21 s, HTTPS 131 s -> 46 s.  RSLC timings are unchanged in-region (45 s local, 52 s `s3://`) with byte-identical output; the byte saving there is for links where bandwidth, not request count, is the limit.
- Measured end to end from a laptop over the public internet, the link these changes are actually for -- in-region the whole thing is nearly invisible.  The same subset: **6m53s -> 3m52s**.  The stages read as a lesson in what matters where: coalescing with unaligned stripes gave 6m20s (it fetched twice the bytes to save requests), aligning the stripes 4m08s, the coalesced metadata read 4m08s -> and splitting the metadata by file offset 3m52s.  1254 MB in ~700 requests became 274 MB in ~300.  What remains is roughly 91 s of bytes at that link's 3.0 MB/s, the latency of those requests, and 62 granule opens -- one per stripe task plus one per metadata worker.
- Where a transfer has to shrink further, `-noanc` is the largest single lever: the two float ancillary grids are 110 MB of the 219 MB payload for this window.  `-vars` on a single covariance term halves what is left.
- **The metadata prefetch reads its chunk ranges directly too.**  Its 225 datasets are small and scattered, which is the worst case for a page-aligned block cache: they sit roughly one per 4.2 MB block, so serving 8.3 MB of wanted data fetched 809 MB.  Taking their spans from the chunk index and serving them to h5py from memory -- the same machinery the payload uses, generalised past the 2-D case to any rank, chunked or contiguous -- brings that to 55 MB.  GSLC's prefetch takes the same path.
- **Workers are given neighbouring datasets, not a fair share of big ones.**  The split was round-robin by descending size, which scatters datasets that sit beside each other in the file across all eight workers and leaves nothing for the coalescer to merge.  Splitting by file offset instead took a GCOV subset from 372 requests to 196.  The runs are cut short and dealt round-robin rather than given one long run per worker: locality alone does not balance -- 212 of those 225 datasets share one 7.1 MB block at the end of the file, 91% dense and reachable in two requests, while the 13 radarGrid cubes are spread over 6.8 GB -- and a worker holding a single contiguous stretch of the large ones finished well after the rest, costing a GSLC subset ~8%.  Relaxing the waste budget is not an alternative: from 10% to 200% it saves only 372 requests -> 313, because the cubes genuinely are far apart.
- Together the two changes take one 4124 x 4057 dual-pol subset from 1254 MB on the wire to 274 MB: payload 445 -> 219 MB, metadata 809 -> 55 MB, and its request count from 372 to 196.  In-region that is nearly invisible -- bytes are cheap there, and the timings above move by a second either way -- which is exactly why it went unnoticed.  It is the whole cost on a home link: the 6m53s laptop run was 1254 MB at 3.0 MB/s.
- The metadata prefetch is deliberately not gated on transport or on how expensive the granule's own open was.  Gating it was tried and reverted: in-region, reading those 225 datasets serially instead measured 97 s against 42 s for the whole subset, and the GSLC subsetter records the same result off-region.  Each worker re-opening the granule costs less than the round trips it removes.
- Output is unchanged: the COGs are byte-identical, all 317 datasets of the GCOV h5 subset match the serial writer's from all three transports, GSLC's 257 likewise, and an RSLC subset is byte-identical (`58a474d2...`) local and from `s3://`.  (The h5 file's md5 is not stable run to run -- netCDF/h5repack packs it differently each time -- so equality here was checked dataset by dataset, not by digest.)

**RSLC terrain height: resolved once per run, and recorded**
- Height decides which geolocationGrid levels the `-projwin` lookup searches and how far range is padded, so it decides the window itself. It was resolved per granule from a live USGS query, which meant a time series built in auto mode used a different height per date by construction, and a transient outage silently substituted 1000 m -- producing a subset of different size and offset from the same granule subsetted an hour earlier. Two runs of one command here wrote boundingPolygon heights of 808.73 m and 207.25 m for this reason.
- `--max_height`/`--min_height` are authoritative now: when either is given nothing is queried at all, so a run is reproducible, offline, and works outside the US (the USGS Elevation Point Query Service is US-only, so every non-US granule had been falling back silently). The polygon height with explicit flags is `(max_height + min_height) / 2`, matching the fallback convention it replaces.
- Heights are resolved once per run and reused by every granule -- from the requested box under `-projwin`, or from the first granule's radar corners under `-srcwin`/`-coordwin`. A batch of N granules issues one lookup instead of N, and every date in the stack is subsetted against the same terrain assumption. For a time series, pass the heights explicitly; that is the only way to guarantee it.
- A failed lookup now says so loudly, naming the fallback values and warning that subsets written during an outage will not match subsets of the same granules written when the service answers.
- What was used is recorded on `identification/boundingPolygon`: `subset_terrain_height_meters`, `subset_terrain_height_source` (`user`/`usgs`/`default`), `subset_window_max_height_meters` and `subset_window_min_height_meters`. A stack can be checked for a common geometry by reading one attribute, and any subset can be reproduced later by passing the recorded heights.

**GCOV raster output: `inputDataExceptionMask` is a flag grid, not backscatter**
- Naming it in `-vars` for COG/GTiff produced a Float32 band called `…-EBD_A_in_pwr.tif`. It was absent from the ancillary-grid table, so it went through the power transform meant for backscatter and took its filename token from the first two letters of the variable name. It is written as `…-EBD_A_exceptionmask.tif` now: uint8, values untouched, and no nodata, since the product defines no fill value for it — which is what `-lg` already reported.
- Downscaling reduces a block by bitwise OR rather than by mean. The pixel value is itself a bitwise OR of exception codes, so a block carries every code any of its pixels raised; averaging would invent codes that were never set. Reprojection uses nearest, and COG overviews are now built with nearest for every uint8 flag grid rather than for `mask` alone.

**`-noanc` for GCOV h5 subsets**
- `-noanc/--no_ancillary_floats` omits the float ancillary grids (`numberOfLooks`, `rtcGammaToSigmaFactor`) from `-of h5` output. They are float32 at full window size and compress poorly — for a 15 km dual-frequency window they are 12.8 MB of a 25.5 MB subset, so dropping them returns the file to the 12.4 MB it was before this release while keeping the flag grids.
- The uint8 flag grids (`mask`, `inputDataExceptionMask`) are always kept: 2 KB each for the same window. A grid named explicitly with `-vars` is always written, `-noanc` or not.
- Carrying the ancillary grids costs about 0.4 s on that window (4.1 s to 4.5 s), four extra windowed reads per frequency; `-noanc` returns to 4.1 s. No effect on COG/GTiff output.

**`seppo_earthaccess_credentials`: `-s` and `-u` print only shell statements**
- Both modes are documented for `eval $(seppo_earthaccess_credentials -s)`, and both printed the version banner as their first line, which the shell then tried to run — `***: command not found` before every export. The banner is now printed only for the informational modes; `-t` already kept its notices on stderr for the same reason.

**Packaging**
- The conda recipe tracks releases again. It had been left at 0.5.3, and its source URL pointed at the GitHub tag tarball, whose sha256 no longer matches what GitHub serves — those archives are regenerated and their checksums drift. It now takes the PyPI sdist, which is immutable and already published on every `v*` tag by the release workflow.
- `recipe/update_recipe.py` re-points the recipe at a release: it reads the sdist's sha256 from PyPI, rewrites version and checksum, and resets the build number (or increments it when re-syncing the same version).

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
