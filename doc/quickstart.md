# Quick Start

Get from zero to NISAR GeoTIFFs in a few commands. This example processes
track 105 frames 17–18.

**IMPORTANT:** Ideally run on an AWS ec2 instance in `us-west-2` where NISAR data reside (32GB RAM for full scenes, less for subsets). Outside `us-west-2` add `--https` to the search command. Output supports `s3://my-bucket/prefix/`. If full scenes are requested, caching is automatically turned on (`--cache y`) See full documentation.

---

## 1 — Install

```bash
mamba create -n openseppo -c conda-forge openseppo aria2
conda activate openseppo
```

---

## 2 — Credentials

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

## 3 — Find scenes

Search for NISAR GCOV scenes for track 105, frames 17 and 18:
(omit `--https` flag  if on an ec2 instance in us-west-2)

```bash
# omit --https if on an AWS ec2 instance in us-west-2
seppo_nisar_search --track 105 --frame 17 18 --start_time_before 2026-01-17 \
    -o urls.txt --https
```

`urls.txt` now contains one URL per line.

---

## 4 — Inspect available grids

Check which frequencies and polarizations are in the first file before converting:

```bash
seppo_nisar_gcov_convert -i urls.txt -lg
```

---

## 5 — Convert to GeoTIFF

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
  NISAR_..._hh_AMP.tif       ← HH backscatter (uint16, amplitude, COG)
  NISAR_..._hv_AMP.tif       ← HV backscatter (uint16, amplitude, COG)
  NISAR_..._hhhv_AMP.vrt     ← per-scene snapshot VRT (both pols)
  NISAR_..._hh_AMP.vrt       ← time-series VRT stack (1 band per date)
  NISAR_..._hv_AMP.vrt        ← time-series VRT stack
```

---

## Optional: ancillary layers

Extract mask, number of looks, and the gamma-to-sigma conversion factor
(no backscatter scaling applied):

```bash
seppo_nisar_gcov_convert -i urls.txt -o out_anc/ \
    -projwin 636357 3497674 655829 3480149 -tr 20 20 \
    --vars mask numberOfLooks rtcGammaToSigmaFactor \
    -v
```

---

## Common variants

```bash
# dB float32, no spatial subset
seppo_nisar_gcov_convert -i urls.txt -o out/ -dB -v

# Compact uint8 browse + dual-pol ratio, 100 m, WGS84
seppo_nisar_gcov_convert -i urls.txt -o out/ \
    -DN -dpratio --no_single_bands \
    -t_srs 4326 -tr 0.001 0.001
```

---

See [Examples](nisar_gcov_convert_examples.md) for the full reference, or [CLI Reference](nisar_gcov_convert_cli.md) for all options.

---

## Load the time-series VRT stack into Python

The per-track time-series VRT (one band per acquisition date) can be opened
lazily with `rioxarray` — no data is read until you actually index or compute:

```python
import rioxarray

s3 = "s3://seppo1-data/NISAR/test3/NISAR_L2_PR_GCOV_005-010_105-105_A-A_017-018_4005_DHDH_A_20251117T111904_20260116T112015-EBD_A_hh_AMP.vrt"

# Open lazily — bands = acquisition dates, nothing is read yet
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

For integrating openSEPPO into a Python script or notebook — including programmatic search and conversion — see the
[Jupyter Notebook example](openSEPPO_example.md).
