# NISAR Hawaii Volcanoes -- Search, Inspect, and Subset Examples

End-to-end examples for RSLC, GSLC, and GCOV products covering
**Hawaii Volcanoes National Park (Kilauea caldera and active lava flows)**.

All examples use **Track 072, Frame 079 (Descending, Dual-pol HH+HV)**
and a geographic bbox over Kilauea: `-155.33 19.47 -155.20 19.37`.
Each command completes in under 1 minute from a laptop over HTTPS.

---

## 1. Search for Products

### Find all product types over Hawaii Big Island

```bash
seppo_nisar_search \
    --product GCOV \
    --bbox -156.1 19.3 -154.8 20.3 \
    --start_time_after 2026-01-01 \
    --start_time_before 2026-04-01 \
    --https \
    --limit 30 \
    --group
```

### Save Track 072 Frame 079 URLs for batch processing

```bash
# GCOV
seppo_nisar_search --product GCOV --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 --start_time_before 2026-04-01 \
    --https --group -o search_results/

# GSLC
seppo_nisar_search --product GSLC --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 --start_time_before 2026-04-01 \
    --https --group -o search_results/

# RSLC
seppo_nisar_search --product RSLC --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 --start_time_before 2026-04-01 \
    --https --group -o search_results/
```

---

## 2. Inspect Products

### Inspect RSLC structure

```bash
seppo_nisar_rslc_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5
```

### Inspect GCOV structure

```bash
seppo_nisar_gcov_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5
```

### Inspect GSLC structure

```bash
seppo_nisar_gslc_convert -lg -i \
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5
```

---

## 3. RSLC Subsetting

### Subset Kilauea area (HH only, with quicklook)

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/rslc/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH \
    -ql -v
```

Output: 43 MB subsetted RSLC HDF5 + quicklook PNG (~32 seconds, no caching).
The quicklook shows Kilauea caldera clearly visible as a dark oval (smooth lava lake).

### Dual-pol subset (HH + HV)

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/rslc/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH HV \
    -ql -v
```

### Output to S3

```bash
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L1_RSLC_BETA_V1/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L1_PR_RSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o s3://mybucket/hawaii/rslc/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH HV \
    --output_profile myprofile \
    -v
```

---

## 4. GCOV Conversion and Subsetting

### Convert to amplitude COG over Kilauea

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gcov/ \
    -amp \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

### Sigma0 reprojected to WGS84

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gcov/ \
    -sigma0 \
    -t_srs 4326 -tr 0.0002 0.0002 \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

### Time series (batch from search output)

```bash
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_072_D_079_*.txt \
    -o output/gcov_timeseries/ \
    -amp \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

### Dual-pol ratio

```bash
seppo_nisar_gcov_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GCOV_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gcov/ \
    -dpratio \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

---

## 5. GSLC Conversion and Subsetting

### Convert to power COG

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gslc/ \
    -pwr \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

### Extract raw complex SLC for interferometry

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gslc/ \
    -cslc \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -vars HH \
    -v
```

### Subset to HDF5 (preserves complex data + all metadata)

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gslc/ \
    -of h5 \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

### Extract wrapped phase

```bash
seppo_nisar_gslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GSLC_BETA_V1/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001/NISAR_L2_PR_GSLC_009_072_D_079_4005_DHDH_A_20260102T045817_20260102T045836_X05010_N_P_J_001.h5 \
    -o output/gslc/ \
    -phase \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -vars HH \
    -v
```

---

## 6. Batch Processing (Time Series)

### GCOV time series

```bash
# Step 1: Search and save URLs
seppo_nisar_search --product GCOV \
    --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 --start_time_before 2026-07-01 \
    --https --group \
    -o search_results/

# Step 2: Convert all to COG with subsetting
seppo_nisar_gcov_convert \
    -i search_results/NISAR_GCOV_072_D_079_*.txt \
    -o output/gcov_timeseries/ \
    -amp \
    -projwin -155.33 19.47 -155.20 19.37 \
    -projwin_srs EPSG:4326 \
    -v
```

### RSLC time series for interferometry

```bash
# Search
seppo_nisar_search --product RSLC \
    --track 72 --frame 79 --direction D \
    --start_time_after 2026-01-01 --start_time_before 2026-07-01 \
    --https --group \
    -o search_results/

# Subset all to the same geographic extent
seppo_nisar_rslc_convert \
    -i search_results/NISAR_RSLC_072_D_079_*.txt \
    -o output/rslc_stack/ \
    -projwin -155.33 19.47 -155.20 19.37 \
    -vars HH \
    -v
```

Each output is a fully self-contained RSLC HDF5 that can be processed
independently with isce3, GAMMA Remote Sensing, or SEPPO.

---

## Area of Interest

| Parameter | Value |
|-----------|-------|
| Location | Hawaii Volcanoes National Park (Kilauea) |
| Bounding box (lon/lat) | `-155.33 19.47 -155.20 19.37` |
| NISAR Track | 072 |
| NISAR Frame | 079 |
| Direction | Descending |
| Polarisation | Dual-pol HH+HV (DHDH) |
| First acquisition | 2026-01-02 |
| Repeat cycle | 12 days |

This area features active lava flows, caldera structures, tropical rainforest,
and bare lava fields -- ideal for demonstrating SAR processing capabilities.
