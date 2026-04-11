# NISAR Hawaii Big Island -- Search, Inspect, and Subset Examples

End-to-end examples for RSLC, GSLC, and GCOV products covering Hawaii Big Island.
All examples use **Track 151, Frame 12 (Ascending, Dual-pol HH+HV)** acquired on
2025-12-02.

---

## 1. Search for Products

### Find RSLC products over the Big Island

```bash
seppo_nisar_search \
    --product RSLC \
    --bbox -156.1 19.3 -154.8 20.3 \
    --start_time_after 2025-12-01 \
    --start_time_before 2026-01-01 \
    --https \
    --limit 10
```

### Find matching GCOV and GSLC

```bash
# GCOV (geocoded covariance -- backscatter power)
seppo_nisar_search --product GCOV \
    --bbox -156.1 19.3 -154.8 20.3 \
    --start_time_after 2025-12-01 --start_time_before 2026-01-01 \
    --https --limit 5

# GSLC (geocoded single-look complex)
seppo_nisar_search --product GSLC \
    --bbox -156.1 19.3 -154.8 20.3 \
    --start_time_after 2025-12-01 --start_time_before 2026-01-01 \
    --https --limit 5
```

### Search by track and frame

```bash
seppo_nisar_search --product RSLC \
    --track 151 --frame 12 --direction A \
    --start_time_after 2025-12-01 --start_time_before 2026-03-01 \
    --https
```

---

## 2. Inspect Products

### Inspect RSLC structure

```bash
seppo_nisar_rslc_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5
```

Shows: frequencies, polarisations, dimensions, slant range / zero-Doppler time ranges, orbit info.

### Inspect GCOV structure

```bash
seppo_nisar_gcov_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5
```

Shows: CRS, resolution, pixel extent, backscatter variables (HHHH, HVHV, ...), ancillary grids (mask, numberOfLooks).

### Inspect GSLC structure

```bash
seppo_nisar_gslc_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5
```

Shows: CRS, resolution, pixel extent, complex polarisation variables (HH, HV), dtype.

---

## 3. RSLC Subsetting

### Subset Mauna Kea area (HH only, with quicklook)

A ~20 km x 25 km box covering Mauna Kea summit and slopes:

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/rslc/ \
    -projwin -155.55 19.9 -155.35 19.7 \
    -vars HH \
    -ql -v
```

Output: subsetted RSLC HDF5 + quicklook PNG.  The output is compatible with isce3, GAMMA Remote Sensing, and SEPPO for interferometric processing.

### Dual-pol subset (HH + HV)

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/rslc/ \
    -projwin -155.55 19.9 -155.35 19.7 \
    -vars HH HV \
    -ql -v
```

### Subset with caching (recommended for large subsets)

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/rslc/ \
    -projwin -156.0 20.2 -154.9 19.4 \
    -cache y \
    -ql -v
```

### Output to S3

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L1_PR_RSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o s3://mybucket/hawaii/rslc/ \
    -projwin -155.55 19.9 -155.35 19.7 \
    -vars HH HV \
    --output_profile myprofile \
    -v
```

---

## 4. GCOV Conversion and Subsetting

### Convert to Cloud Optimized GeoTIFF (default: power dB)

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gcov/ \
    -projwin 180000 2210000 200000 2185000 \
    -v
```

Note: `-projwin` for GCOV uses the native UTM coordinates (metres).  Use `-projwin_srs EPSG:4326` to specify lon/lat:

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gcov/ \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

### Convert to scaled amplitude (uint16, GCOV-compatible)

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gcov/ \
    -amp \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

### Reproject to WGS84 geographic

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gcov/ \
    -t_srs 4326 \
    -tr 0.0002 0.0002 \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

### Subset to HDF5 (preserves all metadata)

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gcov/ \
    -of h5 \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

### Compute sigma0 from gamma0

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gcov/ \
    -sigma0 \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

### Dual-pol ratio

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gcov/ \
    -dpratio \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

---

## 5. GSLC Conversion and Subsetting

### Convert to power COG (default)

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gslc/ \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

### Extract raw complex SLC for interferometry

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gslc/ \
    -cslc \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -vars HH \
    -v
```

### Subset to HDF5 (preserves complex data + all metadata)

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gslc/ \
    -of h5 \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

### Extract wrapped phase

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gslc/ \
    -phase \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -vars HH \
    -v
```

### Downscale to square pixels

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001/NISAR_L2_PR_GSLC_006_151_A_012_4005_DHDH_A_20251202T155058_20251202T155131_X05009_N_F_J_001.h5 \
    -o output/gslc/ \
    --square \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

---

## 6. Batch Processing (Time Series)

### Build a GCOV time series

```bash
# Step 1: Search and save URLs
seppo_nisar_search --product GCOV \
    --track 151 --frame 12 --direction A \
    --start_time_after 2025-12-01 --start_time_before 2026-06-01 \
    --https \
    -o hawaii_gcov_urls.txt

# Step 2: Convert all to COG with subsetting
seppo_nisar_gcov_convert \
    -i hawaii_gcov_urls.txt \
    -o output/gcov_timeseries/ \
    -projwin -155.55 19.9 -155.35 19.7 \
    -projwin_srs EPSG:4326 \
    -v
```

This generates per-date COGs and automatically builds VRT time-series stacks.

### Build an RSLC time series for interferometry

```bash
# Search for all RSLC acquisitions on Track 151
seppo_nisar_search --product RSLC \
    --track 151 --frame 12 --direction A \
    --start_time_after 2025-12-01 --start_time_before 2026-06-01 \
    --https \
    -o hawaii_rslc_urls.txt

# Subset all to the same geographic extent
seppo_nisar_rslc_convert \
    -i hawaii_rslc_urls.txt \
    -o output/rslc_timeseries/ \
    -projwin -155.55 19.9 -155.35 19.7 \
    -vars HH \
    -v
```

Each output is a fully self-contained RSLC HDF5 that can be processed
independently with isce3, GAMMA Remote Sensing, or SEPPO.

---

## Area of Interest

All examples use **Hawaii Big Island, Mauna Kea area**:

| Parameter | Value |
|-----------|-------|
| Bounding box (lon/lat) | `-155.55 19.9 -155.35 19.7` |
| NISAR Track | 151 |
| NISAR Frame | 12 |
| Direction | Ascending |
| Polarisation | Dual-pol HH+HV (DHDH) |
| First acquisition | 2025-12-02 |
| Repeat cycle | 12 days |

This area has diverse terrain: lava flows, tropical forest, alpine desert,
observatories, and coastline -- ideal for demonstrating SAR processing capabilities.
