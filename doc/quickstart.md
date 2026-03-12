# Quick Start

Get from zero to NISAR GeoTIFFs in a few commands. This example processes
track 105 frames 17–18 over the Los Angeles basin.

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

Search for all available NISAR GCOV scenes for track 105, frames 17 and 18:

```bash
seppo_nisar_search --track 105 --frame 17 18 -o la_urls.txt
```

`la_urls.txt` now contains one `s3://` URL per line.

---

## 4 — Inspect available grids

Check which frequencies and polarizations are in the first file before converting:

```bash
seppo_nisar_gcov_convert -i la_urls.txt -lg
```

---

## 5 — Convert to GeoTIFF

Convert to amplitude-scaled Cloud Optimized GeoTIFFs at 50 m resolution,
clipped to the LA basin extent, with a time-series VRT stack:

```bash
seppo_nisar_gcov_convert \
    -i la_urls.txt \
    -o s3://my-bucket/NISAR/LA_50m/ \
    -amp \
    -projwin 598146.587 3576347.040 750714.190 3428083.178 \
    -tr 50 50 \
    -v
```

Replace `s3://my-bucket/NISAR/LA_50m/` with a local path (e.g. `out/`) if preferred.

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
seppo_nisar_gcov_convert \
    -i la_urls.txt \
    -o s3://my-bucket/NISAR/LA_anc_50m/ \
    -projwin 598146.587 3576347.040 750714.190 3428083.178 \
    -tr 50 50 \
    --vars mask numberOfLooks rtcGammaToSigmaFactor \
    -v
```

---

## Common variants

```bash
# dB float32, no spatial subset
seppo_nisar_gcov_convert -i la_urls.txt -o out/ -dB -v

# Compact uint8 browse + dual-pol ratio, 100 m, WGS84
seppo_nisar_gcov_convert -i la_urls.txt -o out/ \
    -DN -dpratio --no_single_bands \
    -t_srs 4326 -tr 0.001 0.001
```

---

See [Examples](nisar_gcov_convert_examples.md) for the full reference, or [CLI Reference](nisar_gcov_convert_cli.md) for all options.
