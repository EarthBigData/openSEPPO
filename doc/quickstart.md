# Quick Start

Two independent workflows are covered here. Pick the one that matches your data:

- **[GCOV workflow](#gcov-workflow)** — backscatter COGs from NISAR GCOV products (gamma0/sigma0 intensity)
- **[GSLC workflow](#gslc-workflow)** — power/amplitude/phase/coherence from NISAR GSLC complex SLC products

Steps 1–2 (install and credentials) are shared.

---

## GCOV Workflow

Backscatter amplitude or intensity COGs from NISAR GCOV products (track 105, frames 17–18).

**IMPORTANT:** Ideally run on an AWS ec2 instance in `us-west-2` where NISAR data reside (32GB RAM recommended for full scenes, less for subsets). Outside `us-west-2` add `--https` to the search command. Output supports `s3://my-bucket/prefix/`. If full scenes are requested, caching is automatically turned on (`--cache y`) See full documentation.

---

## 1 -- Install

```bash
mamba create -n openseppo -c conda-forge openseppo aria2
conda activate openseppo
```

---

## 2 -- Credentials

Store your [NASA Earthdata](https://urs.earthdata.nasa.gov/users/new) login in `~/.netrc` once:

```
machine urs.earthdata.nasa.gov
    login <your_username>
    password <your_password>
```

Then pre-cache the bearer token:

```bash
seppo_earthaccess_credentials -t
```

---

## 3 -- Find scenes

Search for NISAR GCOV scenes for track 105, frames 17 and 18:
(omit `--https` flag  if on an ec2 instance in us-west-2)

```bash
seppo_nisar_search --track 105 --frame 17 18 --start_time_before 2026-01-17 \
    -o urls.txt --https
```

`urls.txt` now contains one URL per line.

---

## 4 -- Inspect available grids

Check which frequencies and polarizations are in the first file before converting:

```bash
seppo_nisar_gcov_convert -i urls.txt -lg
```

---

## 5 -- Convert to GeoTIFF

Convert to amplitude-scaled Cloud Optimized GeoTIFFs at 20 m resolution,
clipped to a subset provided in projection coordinates crossing the two frames, with a time-series VRT stack:

```bash
seppo_nisar_gcov_convert -i urls.txt -o out/ \
    -amp -projwin 636357 3497674 655829 3480149 -tr 20 20 -v
```

Replace `out/` with an S3 path (e.g. `s3://my-bucket/NISAR/out/`) if preferred.

Output:

```
out/
  NISAR_..._hh_AMP.tif       <- HH backscatter (uint16, amplitude, COG)
  NISAR_..._hv_AMP.tif       <- HV backscatter (uint16, amplitude, COG)
  NISAR_..._hhhv_AMP.vrt     <- per-scene snapshot VRT (both pols)
  NISAR_..._hh_AMP.vrt       <- time-series VRT stack (1 band per date)
  NISAR_..._hv_AMP.vrt        <- time-series VRT stack
```

---

## Optional: ancillary layers

Extract mask, number of looks, and the gamma-to-sigma conversion factor.
Ancillary grids bypass backscatter scaling and receive specialized
downscaling (mask: priority 255>0>subswath, numberOfLooks: sum):

```bash
seppo_nisar_gcov_convert -i urls.txt -o out_anc/ \
    -projwin 636357 3497674 655829 3480149 -tr 20 20 \
    --vars mask numberOfLooks rtcGammaToSigmaFactor \
    -v
```

Output: `_mask.tif` (uint8, nodata=255), `_nlooks.tif` (float32), `_gamma2sigma.tif` (float32).

---

## Common variants

```bash
# dB float32, no spatial subset
seppo_nisar_gcov_convert -i urls.txt -o out/ -dB -v

# Compact uint8 browse + dual-pol ratio, 100 m, WGS84
seppo_nisar_gcov_convert -i urls.txt -o out/ \
    -DN -dpratio --no_single_bands \
    -t_srs 4326 -tr 0.001 0.001

# Sigma0 conversion
seppo_nisar_gcov_convert -i urls.txt -o out/ -sigma0 -amp

# Rebuild VRTs from existing TIFs (auto-detects mode)
seppo_nisar_gcov_convert -o out/ -ro

# Show output summary with /vsis3/ paths for QGIS
seppo_nisar_gcov_convert -o s3://my-bucket/out/ -S -vsis3
```

---

See [Examples](nisar_gcov_convert_examples.md) for the full reference, or [CLI Reference](nisar_gcov_convert_cli.md) for all options.

---

## Load the time-series VRT stack into Python

The per-track time-series VRT (one band per acquisition date) can be opened
lazily with `rioxarray` -- no data is read until you actually index or compute:

```python
import rioxarray

s3 = "s3://seppo1-data/NISAR/test3/NISAR_L2_PR_GCOV_005-010_105-105_A-A_017-018_4005_DHDH_A_20251117T111904_20260116T112015-EBD_A_hh_AMP.vrt"

# Open lazily -- bands = acquisition dates, nothing is read yet
da = rioxarray.open_rasterio(s3, chunks={})
print(da)  # shape, dims, CRS, dtype

# Spatial subset and compute
subset = da.isel(x=slice(100, 300), y=slice(100, 300))
subset.isel(band=0).compute().plot()
```

The `band` dimension corresponds to acquisition order. Each band's
`Description` attribute holds the acquisition date; the companion
`.dates` sidecar file lists them one per line in band order.

---

## Using openSEPPO in Python / Jupyter

For integrating openSEPPO into a Python script or notebook -- including programmatic search and conversion -- see the
[Python API / Jupyter Integration](gcov_processing_overview.md).

---

---

## GSLC Workflow

Power, amplitude, phase, and interferometric coherence from NISAR GSLC complex SLC products.

**IMPORTANT:** Same AWS/`us-west-2` advice applies. GSLC pixels have fixed ~5 m azimuth (Y) spacing; X spacing varies by bandwidth (77 MHz: 2.5 m, 40 MHz: 5 m, 20 MHz: 10 m, 5 MHz: 40 m). Use `--square` to produce square pixels in one step.

---

### 1 -- Install and credentials

Same as the GCOV workflow -- see [steps 1–2 above](#gcov-workflow).

---

### 2 -- Find GSLC scenes

Search for NISAR **GSLC** scenes (same track/frame syntax):

```bash
# Search GSLC scenes -- track 135, frame 77
# (omit --https if on an ec2 instance in us-west-2)
seppo_nisar_search --product GSLC --track 135 --frame 77 \
    --start_time_before 2026-02-01 -o gslc_urls.txt --https
```

`gslc_urls.txt` now contains one GSLC HDF5 URL per line.

---

### 3 -- Inspect available grids

```bash
seppo_nisar_gslc_convert -i gslc_urls.txt -lg
```

---

### 4 -- Convert to power COGs

Convert all GSLC scenes to power intensity GeoTIFFs, square pixels, clipped to an area of interest:

```bash
seppo_nisar_gslc_convert -i gslc_urls.txt -o out_gslc/ \
    -pwr --square \
    -projwin 72.39 23.21 72.82 22.81 -projwin_srs EPSG:4326 \
    -v
```

Output:

```
out_gslc/
  NISAR_..._HH_pwr.tif        <- HH power intensity (float32, COG)
  NISAR_..._HH_pwr.vrt        <- time-series VRT (1 band per date)
```

---

### 5 -- Extract complex SLC for coherence

To compute coherence you need the raw complex pixels. Extract them with `-cslc`:

```bash
seppo_nisar_gslc_convert -i gslc_urls.txt -o out_cslc/ \
    -cslc \
    -projwin 72.39 23.21 72.82 22.81 -projwin_srs EPSG:4326 \
    -v
```

Output:

```
out_cslc/
  NISAR_..._HH_cslc.tif      <- HH complex SLC (complex64, tiled GTiff)
  NISAR_..._HH_cslc.vrt      <- time-series VRT
```

---

### 6 -- Compute interferometric coherence

Compute sequential pairwise coherence from the complex SLC TIFs.
Use a 3×9 window for more isotropic ground coverage at 20 MHz pixel spacing (10 m × 5 m):

```bash
seppo_nisar_coherence -i out_cslc/*_HH_cslc.tif -o out_coh/ \
    -window 3 9 -v
```

Output:

```
out_coh/
  NISAR_..._HH_COH_w03x09_20251119_20260118.tif   <- uint8 COG, DN=round(coh*200)
  NISAR_..._HH_COH_w03x09.vrt                     <- time-series VRT
```

Recover coherence values: `coh = DN / 200` (nodata=255).

---

### 7 -- Coherence with crop and downscale

Crop to the area of interest and downscale 2× after coherence estimation:

```bash
seppo_nisar_coherence -i out_cslc/*_HH_cslc.tif -o out_coh/ \
    -window 3 9 \
    -projwin 72.39 23.21 72.82 22.81 -projwin_srs EPSG:4326 \
    -d 2 \
    -v
```

---

### 8 -- All pairs, reprojected to geographic

```bash
seppo_nisar_coherence -i out_cslc/*_HH_cslc.tif -o out_coh_geo/ \
    -window 3 9 -pairs all \
    -t_srs EPSG:4326 -tr 0.0001 \
    -v
```

---

### Common GSLC variants

```bash
# Amplitude output (GCOV-compatible uint16)
seppo_nisar_gslc_convert -i gslc_urls.txt -o out/ -amp --square

# Wrapped phase
seppo_nisar_gslc_convert -i gslc_urls.txt -o out/ -phase

# HH only, anisotropic downscale (2x cols, 4x rows)
seppo_nisar_gslc_convert -i gslc_urls.txt -o out/ -pwr -vars HH -d 2 4

# Float32 coherence (no DN encoding)
seppo_nisar_coherence -i *.tif -o out/ -no_DN

# Write coherence directly to S3
seppo_nisar_coherence -i *.tif -o s3://my-bucket/coh/ -window 3 9
```

---

See [GSLC Convert CLI Reference](nisar_gslc_convert_cli.md) and [Coherence CLI Reference](nisar_coherence_cli.md) for all options.
