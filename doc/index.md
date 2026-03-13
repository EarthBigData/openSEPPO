# openSEPPO

**Open SEPPO Tools — Supporting Geospatial and Remote Sensing Data Processing**

openSEPPO is a growing set of open-source tools for processing and managing geospatial and 
remote sensing data, with good support for NASA/ISRO NISAR products. The tools are **designed to
work standalone** (on-premise, your laptop, cloud instances, ...),  and to integrate for scaling with the 
[SEPPO](https://earthbigdata.com/seppo) software by [Earth Big Data](https://earthbigdata.com). 

---

## Tools

| Command | Description |
|---------|-------------|
| `seppo_nisar_gcov_convert` | Convert NISAR GCOV HDF5 to Cloud Optimized GeoTIFF (COG), BigTIFF, or HDF5 subset with optional reprojection, downscaling, and VRT time-series stacking |
| `seppo_nisar_search` | Search NISAR product URLs via NASA Earthdata CMR |
| `seppo_earthaccess_credentials` | Manage NASA Earthdata S3 credentials and bearer token |

---

## Quick start — TL;DR

**IMPORTANT:** Ideally run on an AWS ec2 instance in `us-west-2` where NISAR data reside (32GB RAM for full scenes, less for subsets). Outside `us-west-2` add `--https` to the search command. Output supports `s3://my-bucket/prefix/`. See full documentation.

```bash
# 1. Install
mamba create -n openseppo -c conda-forge openseppo aria2 && conda activate openseppo

# 2. Cache Earthdata credentials
seppo_earthaccess_credentials -t

# 3. Find NISAR scenes — track 105, frames 17-18
# (omit the --https flag if on an AWS ec2 instance in us-west-2)
seppo_nisar_search --track 105 --frame 17 18 -o urls.txt --https

# 4. Convert to amplitude COGs at 20 m + time-series VRT stack
seppo_nisar_gcov_convert -i urls.txt -o out/ \
    -amp -projwin 636357 3497674 655829 3480149 -tr 20 20 -v
```

**→ [Full Quick Start guide with variants and output description](quickstart.md)**

See [Installation](installation.md) for pip, local clone, and credential setup options.

---

## Documentation

- [Quick Start](quickstart.md)
- [Installation](installation.md)

**seppo_nisar_gcov_convert**

- [CLI Reference](nisar_gcov_convert_cli.md)
- [Examples](nisar_gcov_convert_examples.md)
- [Dual-pol Ratio](ratio.md)

**seppo_nisar_search**

- [CLI Reference](nisar_search_cli.md)

**seppo_earthaccess_credentials**

- [CLI Reference](earthaccess_credentials_cli.md)

**Example using openSEPPO in a Jupyter Notebook**

- [openSEPPO_example.ipynb](openSEPPO_example.md)

---

## Useful links

| Resource | Description |
|----------|-------------|
| [ASF Vertex](https://search.asf.alaska.edu) | Alaska Satellite Facility visual data search — browse and download NISAR and other SAR products |
| [NISAR Data User Guide](https://nisar-docs.asf.alaska.edu/) | NISAR product format specifications, algorithm documents, and data access guides |
| [NISAR Science](https://science.nasa.gov/mission/nisar/) | Official NASA NISAR mission site — science overview, data products, and news |
| [NASA Earthdata sign-up](https://urs.earthdata.nasa.gov/users/new) | Register for a free NASA Earthdata account (required for data access) |
| [earthaccess](https://earthaccess.readthedocs.io) | Python library for NASA Earthdata authentication and S3 access (used internally by openSEPPO) |
| [TimeseriesSAR QGIS Plugin](https://github.com/EarthBigData/openSAR/tree/master/code/QGIS/v3/plugins) | Interactive time-series click/plot tool for SAR data in QGIS |

---

## License

Apache License 2.0 — (c) 2026 Earth Big Data LLC | [earthbigdata.com](https://earthbigdata.com)
