# NISAR Subset Test Generator

`generate_rslc_subset_tests.py` creates test scripts for subsetting NISAR
RSLC, GSLC, and GCOV products at a given location. It computes bounding boxes
at the target point and optionally at the frame center and 4 corners, then
generates shell scripts for subsetting, RTC processing, or a combined staging
pipeline.

## Quick start

```bash
# Search ASF catalog, generate RSLC subset + RTC scripts
python generate_rslc_subset_tests.py \
    --lon -88.63 --lat 34.94 --name mississippi --size 20 \
    --rslc --rtc --bucket seppo1-data --s3prefix test/openSEPPO/ms_test

# Use SDS S3 URLs, full pipeline (RSLC + RTC + GCOV + GSLC)
python generate_rslc_subset_tests.py \
    --lon -121.76 --lat 46.85 --name rainier --size 20 \
    --urls rslc_urls.txt --rslc --rtc --gcov --gslc --staging \
    --bucket seppo1-data --s3prefix test/openSEPPO/rainier_test \
    --max_height 4500
```

## Options

| Flag | Description |
|------|-------------|
| `--lon`, `--lat` | Target point (required) |
| `--name` | Test name used in filenames and script headers (required) |
| `--size` | Subset size in km (default: 15) |
| `--urls` | File with RSLC URLs — skips ASF catalog search |
| `--target_only` | Only the target point subset (no corners, no center) |
| `--rslc` | Generate RSLC h5 subset commands |
| `--gcov` | Generate GCOV power COG subset commands |
| `--gslc` | Generate GSLC power COG subset commands |
| `--rtc` | Generate RTC processing commands (requires `--rslc`) |
| `--staging` | Single `run_all.sh` combining selected products |
| `--bucket` | S3 bucket for output (default: seppo1-data) |
| `--s3prefix` | S3 key prefix for output |
| `--outdir` | Local directory for generated scripts |
| `--max_height` | Terrain max height in metres for RSLC subsetting |
| `--start_date`, `--end_date` | Date range for ASF catalog search |

If no product flags are given, `--rslc` is enabled by default.

## URL file format

One RSLC URL per line, optionally prefixed with `ASC` or `DESC`.
Direction is inferred from `_A_` / `_D_` in the URL if no label is given.

```
ASC s3://nisar-ops-rs-pop1/products/L1_L_RSLC/.../NISAR_L1_PR_RSLC_..._A_..._001.h5
DESC s3://nisar-ops-rs-pop1/products/L1_L_RSLC/.../NISAR_L1_PR_RSLC_..._D_..._001.h5
```

GSLC and GCOV URLs are derived automatically by replacing `L1` with `L2` and
`RSLC` with `GSLC`/`GCOV` (including the SDS directory path `L1_L_RSLC` ->
`L2_L_GSLC`). The derived URLs are verified on S3 before use; if unavailable,
the script falls back to ASF catalog search.

## Example 1: Mississippi (flat terrain, SDS products)

RSLC URLs in `mississippi_cornertest/sds_rslc_links.txt`:
```
s3://nisar-ops-rs-pop1/products/L1_L_RSLC/2026/01/02/NISAR_L1_PR_RSLC_009_076_A_019_4005_DHDH_A_20260102T110335_20260102T110409_X05013_N_F_J_001/NISAR_L1_PR_RSLC_009_076_A_019_4005_DHDH_A_20260102T110335_20260102T110409_X05013_N_F_J_001.h5
s3://nisar-ops-rs-pop1/products/L1_L_RSLC/2026/01/08/NISAR_L1_PR_RSLC_009_156_D_071_4005_DHDH_A_20260108T004405_20260108T004440_X05013_N_F_J_001/NISAR_L1_PR_RSLC_009_156_D_071_4005_DHDH_A_20260108T004405_20260108T004440_X05013_N_F_J_001.h5
```

Generate the staging script:
```bash
python generate_rslc_subset_tests.py \
    --lon -88.63 --lat 34.94 --name mississippi_sds --size 20 \
    --urls mississippi_cornertest/sds_rslc_links.txt \
    --rslc --gcov --gslc --rtc --staging \
    --bucket seppo1-data --s3prefix test/openSEPPO/mississippi_sds \
    --outdir mississippi_cornertest
```

This generates `mississippi_cornertest/run_all.sh` which for each of the 12
subsets (target + center + 4 corners, ascending + descending):

1. Creates an RSLC h5 subset with quicklook (`seppo_nisar_rslc_convert`)
2. Runs RTC via isce3 (`seppo_sar_rtc_isce.py`, requires SEPPO SARISCE)
3. Creates a GCOV power COG subset (`seppo_nisar_gcov_convert -pwr -of COG`)
4. Creates a GSLC power COG subset (`seppo_nisar_gslc_convert -pwr -of COG`)

Output structure on S3:
```
s3://seppo1-data/test/openSEPPO/mississippi_sds/
  asc_target/rslc/    -- RSLC h5 + quicklook PNG
  asc_target/rtc/     -- RTC gamma0 (isce3)
  asc_target/gcov/    -- GCOV power COG
  asc_target/gslc/    -- GSLC power COG
  asc_center/...
  asc_topleft/...
  ...
  desc_target/...
  desc_center/...
  ...
```

## Example 2: Mt. Rainier (mountainous terrain, SDS products)

RSLC URLs in `rainier_cornertest/sds_rslc_links.txt`:
```
s3://nisar-ops-rs-pop1/products/L1_L_RSLC/2026/03/29/NISAR_L1_PR_RSLC_016_106_A_025_4005_DHDH_A_20260329T130333_20260329T130408_X05013_N_F_J_001/NISAR_L1_PR_RSLC_016_106_A_025_4005_DHDH_A_20260329T130333_20260329T130408_X05013_N_F_J_001.h5
s3://nisar-ops-rs-pop1/products/L1_L_RSLC/2026/03/27/NISAR_L1_PR_RSLC_016_071_D_065_4005_DHDH_A_20260327T031034_20260327T031109_X05013_N_F_J_001/NISAR_L1_PR_RSLC_016_071_D_065_4005_DHDH_A_20260327T031034_20260327T031109_X05013_N_F_J_001.h5
```

Generate the staging script with `--max_height 4500` for correct terrain-aware
subsetting (Mt. Rainier summit at 4392 m):
```bash
python generate_rslc_subset_tests.py \
    --lon -121.76 --lat 46.85 --name rainier_sds --size 20 \
    --urls rainier_cornertest/sds_rslc_links.txt \
    --rslc --gcov --gslc --rtc --staging \
    --bucket seppo1-data --s3prefix test/openSEPPO/rainier_sds \
    --max_height 4500 \
    --outdir rainier_cornertest
```

The `--max_height` flag ensures the RSLC subsetter accounts for height-dependent
SAR range shift when computing pixel windows from the geographic bounding box.
Without it, the subsetter queries the USGS Elevation API automatically (with a
500 m buffer); `--max_height` is needed when USGS is unavailable or for
explicit control.

## Target-only mode

For quick single-subset tests without corners:
```bash
python generate_rslc_subset_tests.py \
    --lon -121.76 --lat 46.85 --name rainier_quick --size 20 \
    --urls rainier_cornertest/sds_rslc_links.txt \
    --gcov --gslc --target_only --staging \
    --bucket seppo1-data --s3prefix test/openSEPPO/rainier_quick
```

This generates only 2 subsets (asc_target + desc_target) with GCOV and GSLC
power COGs — no RSLC subsetting, no RTC.

## Requirements

- **openSEPPO** (all modes)
- **SEPPO SARISCE module** (`--rtc` only) — https://earthbigdata.com/seppo
- **AWS credentials** for SDS S3 bucket access when using `s3://nisar-ops-rs-pop1` URLs
