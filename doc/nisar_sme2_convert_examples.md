# NISAR SME2 Soil Moisture -- South Dakota Irrigation Example

A minimum end-to-end example: from `seppo_nisar_search` to a soil moisture COG
for a **50 x 50 km** subset over the centre-pivot irrigation of the **James
River valley near Huron, Beadle County, South Dakota** (44.55 N, 98.35 W), on a
round 0.002 degree WGS84 grid that repeats exactly across dates.

Everything streams straight from the ASF DAAC over **HTTPS** -- no granule
download. All you need is an Earthdata login in your `~/.netrc`
(see [`seppo_earthaccess_credentials`](earthaccess_credentials_cli.md)).

Every command below was run as shown; the numbers are the actual output.

---

## Two minimal examples

Copy-pasteable, and both run anywhere over HTTPS. The numbered walkthrough after
them explains what each flag does.

### Example 1 -- one date, subset to an AOI

```bash
seppo_nisar_search --product SME2 \
    --point -98.35 44.55 --track 171 \
    --start_time_after 2026-07-20 --start_time_before 2026-07-21 \
    --https --format url -o granule.txt

seppo_nisar_sme2_convert -i granule.txt -o sd_single/ \
    -projwin -98.66 44.77 -98.04 44.33 -projwin_srs 4326
```

```
---> Detected Earthdata HTTPS URL. Using Earthdata credentials.
    soilMoisture window col=1225 row=596 w=300 h=202 @ EPSG:6933

Processed 1/1 SME2 file(s).                                        2.7 s
```

```
sd_single/
  <granule>-EBD_sm_sm.tif           192 KB   soil moisture (m3/m3)
  <granule>-EBD_sm_smunc.tif        189 KB   retrieval uncertainty
  <granule>-EBD_sm_rqf.tif            8 KB   retrieval quality flag
  <granule>-EBD_sm_smsmuncrqf.vrt     2 KB   the three as one 3-band file
```

Output stays on the granule's native EASE-Grid 2.0 (EPSG:6933, 200 m) -- which
is what lets dates stack with no resampling. Add `-t_srs EPSG:4326 -tr 0.002
0.002` to land on a round WGS84 grid instead (section 3).

### Example 2 -- a time series, with the stack VRT

The same command over a list of dates: widen the search, keep the AOI, point
`-o` at a directory.

```bash
seppo_nisar_search --product SME2 \
    --point -98.35 44.55 --track 171 \
    --start_time_after 2026-05-01 --start_time_before 2026-09-01 \
    --https --format url -o sd_urls.txt

seppo_nisar_sme2_convert -i sd_urls.txt -o sd_ts/ \
    -projwin -98.66 44.77 -98.04 44.33 -projwin_srs 4326 \
    -vars soilMoisture
```

```
---> Converting 5 granules, 4 at a time.
    [1/5] ..._20260626T013931_....h5
    [2/5] ..._20260708T013931_....h5
    [3/5] ..._20260720T013930_....h5
    [4/5] ..._20260813T013929_....h5
    [5/5] ..._20260825T013928_....h5

Processed 5/5 SME2 file(s); 1 time-series VRT(s) over 5 dates.     4.2 s
```

```
sd_ts/
  <...20260626T000000_20260825T235959...>-EBD_sm_sm.vrt      the stack
  <...20260626T000000_20260825T235959...>-EBD_sm_sm.dates    its band dates
  <...20260626T013931...>-EBD_sm_sm.tif                      one COG per date
  ... 4 more
```

That `.vrt` is the time series: one band per date, in date order, each band
described and tagged with its acquisition date. It opens as an ordinary 5-band
raster in GDAL, QGIS, rasterio or xarray:

```
5 bands   300 x 202   EPSG:6933   float32   nodata nan

  2026-06-26   97.9% valid   mean 0.125
  2026-07-08   97.8% valid   mean 0.143
  2026-07-20   98.9% valid   mean 0.157
  2026-08-13   98.9% valid   mean 0.259   <- wet-up
  2026-08-25   98.9% valid   mean 0.149
```

To tell the files apart: the stack carries a `T000000_...T235959` date *span* in
its name, while the per-date COGs carry real acquisition times. Drop `-vars
soilMoisture` to get uncertainty and quality-flag stacks as well, one VRT per
layer. `--no_time_series` turns the stacking off, and `-j N` sets how many
granules convert at once (4 by default). The VRT stores relative paths, so move
the directory as a whole.

---

## 1. Find a granule

SME2 is a Level-3 product, one granule per acquisition, so a point search, a
track and a date range are enough:

```bash
seppo_nisar_search \
    --product SME2 \
    --point -98.35 44.55 --track 171 \
    --start_time_after 2026-07-01 --start_time_before 2026-07-31 \
    --https --format url
```

```
https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L3_SME2_PROVISIONAL_V1/NISAR_L3_PR_SME2_024_171_D_066_..._20260708T013931_....h5
https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L3_SME2_PROVISIONAL_V1/NISAR_L3_PR_SME2_025_171_D_066_..._20260720T013930_....h5
```

Without `--https` the search returns the `s3://` form of the same granules;
`seppo_nisar_sme2_convert` accepts either. `--format csv` adds the track, frame,
direction and acquisition time as columns.

> **Why `--track 171`.** Without it, a point search returns every granule whose
> footprint contains that point, including ones whose frame edge clips the AOI:
> over this box track 070 delivers only ~54% valid pixels, while tracks 171
> (descending) and 033 (ascending) cover ~99%. Fixing the track also fixes the
> geometry, which is what makes the dates stack. Drop `--track` and search with
> `--bbox` over the whole AOI to see which tracks cover it.

This example uses the 2026-07-20 granule:

```bash
GRANULE=https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L3_SME2_PROVISIONAL_V1/NISAR_L3_PR_SME2_025_171_D_066_4005_DHDH_A_20260720T013930_20260720T014005_P05023_N_F_J_001/NISAR_L3_PR_SME2_025_171_D_066_4005_DHDH_A_20260720T013930_20260720T014005_P05023_N_F_J_001.h5
```

---

## 2. Look inside first (optional)

```bash
seppo_nisar_sme2_convert -i $GRANULE -lg
```

```
--- SME2 grid: EPSG:6933, 2295x1530, 200.179 m ---
    EASE-Grid 2.0 window: rows 10170-11699, cols 37980-40274
    algorithm candidates: ['DSG', 'TSR']
    radar frequencies:    ['A', 'B']

  soilMoisture/   -> filename token 'sm'
      retrievalQualityFlag               int16    nodata=-9999  -> token 'rqf'
      soilMoisture                       float32  nodata=-9999.0  -> token 'sm'
      soilMoistureUncertainty            float32  nodata=-9999.0  -> token 'smunc'
      surfaceQualityFlag                 int16    nodata=-9999  -> token 'sqf'
  ...
```

Layers are discovered from the granule, not assumed: which algorithm candidates
exist (here `DSG` **and** `TSR`) and which group `surfaceQualityFlag` sits in
both vary between granules and release tiers.

---

## 3. Subset to 50 x 50 km and write the COG

A 50 x 50 km box on the ground around 44.55 N, 98.35 W is
`-98.66 44.77 -98.04 44.33` in lon/lat:

```bash
seppo_nisar_sme2_convert \
    -i $GRANULE \
    -o sd_irrigation/ \
    -projwin -98.66 44.77 -98.04 44.33 -projwin_srs 4326 \
    -t_srs EPSG:4326 -tr 0.002 0.002
```

`-t_srs EPSG:4326 -tr 0.002 0.002` reprojects out of the native EASE-Grid onto a
round WGS84 grid, and pixel-grid alignment (tap, on by default) snaps the origin
to an exact multiple of the target resolution:

```
---> Detected Earthdata HTTPS URL. Using Earthdata credentials.
    soilMoisture window col=1225 row=596 w=300 h=202 @ EPSG:6933
    Wrote <granule>-EBD_sm_sm.tif    (312x222, float32)
    Wrote <granule>-EBD_sm_smunc.tif (312x222, float32)
    Wrote <granule>-EBD_sm_rqf.tif   (312x222, int16)

Runtime: 0m 2.56s
```

```
sd_irrigation/
  <granule>-EBD_sm_sm.tif           221 KB   soil moisture (m3/m3)
  <granule>-EBD_sm_smunc.tif        216 KB   retrieval uncertainty
  <granule>-EBD_sm_rqf.tif            8 KB   retrieval quality flag
  <granule>-EBD_sm_smsmuncrqf.vrt     2 KB   the three stacked
```

The output bounds are exact multiples of the 0.002 degree pixel:

```
W -98.662000   N 44.774000   E -98.038000   S 44.330000     312 x 222
```

Note these are not literally the corners you typed. The projwin first snaps
outward to whole EASE-Grid source pixels (the `300x202` window in the log), and
the reprojected extent then snaps outward again to the target lattice. What you
are guaranteed is that the result covers your box and lands on the 0.002 degree
grid -- pass `--no_tap` if you would rather keep the reprojected corners
unrounded.

For just the soil moisture band and nothing else, add `-vars soilMoisture` --
one file, and no VRT (the VRT only appears with more than one band).

The result over this AOI: 98.1% valid pixels, soil moisture 0.094 to 0.231
m3/m3 between the 5th and 95th percentile, mean 0.157 -- dryland at the low end,
irrigated pivots at the wet end.

---

## 4. The same window for every date

Because the target grid is absolute, the identical command against the other
granule from step 1 produces a raster on exactly the same lattice -- no
resampling, no alignment step, ready to stack:

```
2026-07-08   312x222   W -98.6620  N 44.7740   97.1% valid   mean 0.143
2026-07-20   312x222   W -98.6620  N 44.7740   98.1% valid   mean 0.157
```

```bash
seppo_nisar_search --product SME2 --point -98.35 44.55 --track 171 \
    --start_time_after 2026-07-01 --start_time_before 2026-07-31 \
    --https --format url -o sd_urls.txt

seppo_nisar_sme2_convert -i sd_urls.txt -o sd_irrigation/ \
    -projwin -98.66 44.77 -98.04 44.33 -projwin_srs 4326 \
    -t_srs EPSG:4326 -tr 0.002 0.002 -vars soilMoisture
```

`-i` accepts a text file of URLs, and an output directory ending in `/` batches
them, converting up to `-j` granules at a time (4 by default). A batch also
writes the time-series VRT stacks shown in
[minimal example 2](#example-2----a-time-series-with-the-stack-vrt) -- one per layer, one
band per date -- unless `--no_time_series` is given.

---

## Notes

**Without `-t_srs`, output stays on the native EASE-Grid** (EPSG:6933), an
equal-area projection whose 200.179 m pixel is true at 30 degrees latitude. At
44.55 N a 50 x 50 km *ground* box spans about 61 km of map easting and 41 km of
northing -- the `300x202` source window in the log, not a square one. Area is
preserved; shape is not. Reprojecting to EPSG:4326 as above sidesteps this;
`-srcwin XOFF YOFF XSIZE YSIZE` is the way to ask for a fixed pixel count on the
native grid instead. Native-grid subsets also keep their `EASEGridRowIndex` /
`EASEGridColumnIndex`, so they stack across dates and tracks without any
reprojection at all.

**`-projwin_srs 4326` is optional but recommended.** EASE-Grid coordinates are
metres in the +/-17 000 km range, so lon/lat corners would otherwise snap to a
single pixel. The tool detects this and reads the corners as lon/lat anyway,
printing an INFO line; passing the flag makes the intent explicit and silences
the notice.

**Choosing `-tr`.** 0.002 degrees is about 158 m east-west and 222 m
north-south at this latitude, against a 200 m source pixel -- a reasonable round
number here. Any value works; the tap alignment is what makes dates comparable,
not the specific resolution. Continuous layers resample with `--resample`
(bilinear by default) and integer layers always use nearest.

**`s3://` gives the same pixels, faster in-region.** Swapping the HTTPS URLs
for the `s3://` form produces **byte-identical** COGs. The difference is speed
and where you run: from an EC2 instance in `us-west-2` the single-granule
conversion above takes 1.31 s over `s3://` against 2.56 s over HTTPS, because
the S3 path probes the file's 2 MiB HDF5 page size and reads page-aligned
blocks, while the HTTPS path uses a 4 MiB default block. Off AWS, HTTPS is the
only option and the run is bandwidth-bound anyway.

**Don't cache for one subset.** The granule is ~90 MB; this windowed read moves
about 7-15 MB. `-cache` only pays off when several layer groups are converted
from the same file.

---

## Related

- [`seppo_nisar_sme2_convert` CLI Reference](nisar_sme2_convert_cli.md)
- [`seppo_nisar_search` CLI Reference](nisar_search_cli.md)
