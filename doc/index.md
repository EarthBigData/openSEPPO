# openSEPPO

**Open SEPPO Tools -- Supporting Geospatial and Remote Sensing Data Processing**

openSEPPO is a growing set of open-source tools for processing and managing geospatial and
remote sensing data, with good support for NASA/ISRO NISAR products. The tools are **designed to
work standalone** (on-premise, your laptop, cloud instances, ...),  and to integrate for scaling with the
[SEPPO](https://earthbigdata.com/seppo) software by [Earth Big Data](https://earthbigdata.com).

---

## Tools

| Command | Description |
|---------|-------------|
| `seppo_nisar_rslc_convert` | Subset NISAR L-band RSLC HDF5 files directly from S3/HTTPS; output is compatible with isce3, GAMMA Remote Sensing, and SEPPO for interferometric processing; geographic bbox, pixel, and coordinate subsetting; quicklook generation |
| `seppo_nisar_gcov_convert` | Convert NISAR GCOV HDF5 to Cloud Optimized GeoTIFF (COG), BigTIFF, or HDF5 subset with optional sigma0 conversion, reprojection, downscaling, and VRT time-series stacking |
| `seppo_nisar_gcov_convert_S` | S-band variant of `seppo_nisar_gcov_convert` |
| `seppo_nisar_gslc_convert` | Convert NISAR GSLC HDF5 complex data to COG: power, amplitude, magnitude, wrapped phase, or raw complex SLC with optional reprojection, downscaling, and VRT stacking |
| `seppo_nisar_coherence` | Compute pairwise interferometric coherence from co-registered NISAR GSLC complex SLC files with optional crop, downscale, and reprojection |
| `seppo_nisar_search` | Search NISAR product URLs via NASA Earthdata CMR |
| `seppo_earthaccess_credentials` | Manage NASA Earthdata S3 credentials and bearer token |

---

## Quick Start -- TL;DR

**IMPORTANT:** Ideally run on an AWS ec2 instance in `us-west-2` where NISAR data reside (32 GB RAM recommended for full scenes, less for subsets). Outside `us-west-2` add `--https` to the search command. Output supports `s3://my-bucket/prefix/`. See full documentation.

```bash
# 1. Install
mamba create -n openseppo -c conda-forge openseppo aria2 && conda activate openseppo

# 2. Cache Earthdata credentials
seppo_earthaccess_credentials -t

# 3. Find NISAR scenes -- track 151, frame 12 (Hawaii Big Island)
seppo_nisar_search --track 151 --frame 12 --direction A \
    --start_time_before 2026-01-01 -o urls_gcov.txt --https --product GCOV

# 4. Convert GCOV to amplitude COGs with geographic subset
seppo_nisar_gcov_convert -i urls_gcov.txt -o out/ \
    -amp -projwin -155.55 19.9 -155.35 19.7 -projwin_srs EPSG:4326 -v

# 5. Subset RSLC for interferometric processing
seppo_nisar_rslc_convert \
    -i https://nisar.asf.earthdatacloud.nasa.gov/.../NISAR_L1_PR_RSLC_...h5 \
    -o out/rslc/ \
    -projwin -155.55 19.9 -155.35 19.7 -vars HH -ql -v
```

**-> [Full Quick Start guide (GCOV and GSLC workflows)](quickstart.md)**

See [Installation](installation.md) for pip, local clone, and credential setup options.

---

## Getting Started

- [Quick Start](quickstart.md)
- [Installation](installation.md)

---

## Examples

### End-to-End Workflows

- **[Hawaii Big Island examples](nisar_hawaii_examples.md)** -- Search, inspect, and subset
  RSLC, GSLC, and GCOV products over Mauna Kea.  Covers all three product types with
  geographic bbox subsetting, time-series building, and output to S3.

### Detailed Processing Guides

- **[GCOV Processing Overview](gcov_processing_overview.md)** -- Detailed walkthrough
  of searching NISAR data and processing GCOV products: data search, COG conversion,
  subsetting, amplitude/dB/DN modes, reprojection, downscaling, sigma0, dual-pol ratio,
  VRT time-series stacking, and Jupyter notebook integration.

---

## CLI Reference

### RSLC (Radar-coordinates SLC)

- **[seppo_nisar_rslc_convert](nisar_rslc_convert_cli.md)** -- Subset RSLC HDF5 files
  with geographic bbox (`-projwin`), pixel window (`-srcwin`), or radar coordinate
  window (`-coordwin`).  Output is compatible with isce3, GAMMA Remote Sensing, and
  SEPPO.  Includes quicklook generation (`-ql`).

### GCOV (Geocoded Covariance)

- **[seppo_nisar_gcov_convert](nisar_gcov_convert_cli.md)** -- Convert to COG/GTiff/H5
  with power, amplitude, dB, or DN output modes.  Supports subsetting, reprojection,
  downscaling, sigma0 conversion, dual-pol ratio, and VRT time-series stacking.
- **[GCOV Examples](nisar_gcov_convert_examples.md)** -- Detailed usage examples.
- **[Dual-pol Ratio](ratio.md)** -- Dual-pol ratio output details and formulas.

### GSLC (Geocoded SLC)

- **[seppo_nisar_gslc_convert](nisar_gslc_convert_cli.md)** -- Convert complex SLC to
  power, amplitude, magnitude, wrapped phase, or raw complex COG/GTiff/H5.  Supports
  subsetting, reprojection, downscaling, and VRT stacking.

### Coherence

- **[seppo_nisar_coherence](nisar_coherence_cli.md)** -- Compute pairwise interferometric
  coherence from co-registered GSLC complex SLC files.

### Search and Credentials

- **[seppo_nisar_search](nisar_search_cli.md)** -- Search NISAR products via NASA
  Earthdata CMR.  Filter by track, frame, direction, date range, geographic extent.
- **[seppo_earthaccess_credentials](earthaccess_credentials_cli.md)** -- Manage NASA
  Earthdata S3 credentials and bearer token.

---

## Useful Links

| Resource | Description |
|----------|-------------|
| [ASF Vertex](https://search.asf.alaska.edu) | Alaska Satellite Facility visual data search -- browse and download NISAR and other SAR products |
| [NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/) | NISAR product format specifications, algorithm documents, and data access guides |
| [NISAR Science](https://science.nasa.gov/mission/nisar/) | Official NASA NISAR mission site -- science overview, data products, and news |
| [NASA Earthdata sign-up](https://urs.earthdata.nasa.gov/users/new) | Register for a free NASA Earthdata account (required for data access) |
| [earthaccess](https://earthaccess.readthedocs.io) | Python library for NASA Earthdata authentication and S3 access (used internally by openSEPPO) |
| [TimeseriesSAR QGIS Plugin](https://github.com/EarthBigData/openSAR/tree/master/code/QGIS/v3/plugins) | Interactive time-series click/plot tool for SAR data in QGIS |

---

## License

Apache License 2.0 -- (c) 2026 Earth Big Data LLC | [earthbigdata.com](https://earthbigdata.com)
