# openSEPPO

**Open SEPPO Tools — Supporting Geospatial and Remote Sensing Data Processing**

openSEPPO provides open-source tools for processing and managing geospatial and SAR
remote sensing data, with a focus on NASA NISAR products. The tools are designed to
scale readily with the [SEPPO](https://earthbigdata.com/seppo) software by
[Earth Big Data](https://earthbigdata.com).

---

## Tools

| Command | Description |
|---------|-------------|
| `seppo_nisar_gcov_convert` | Convert NISAR GCOV HDF5 to Cloud Optimized GeoTIFF (COG), BigTIFF, or HDF5 subset with optional reprojection, downscaling, and VRT time-series stacking |
| `seppo_nisar_search` | Search NISAR product URLs via NASA Earthdata CMR |
| `seppo_earthaccess_credentials` | Manage NASA Earthdata S3 credentials and bearer token |

---

## Installation

See [doc/installation.md](doc/installation.md) for full instructions including
conda environment setup, pip install, and NASA Earthdata credential configuration.

Quick start:

```bash
conda create -n openseppo -c conda-forge openseppo aria2
conda activate openseppo
```

---

## Documentation

| File | Description |
|------|-------------|
| [doc/installation.md](doc/installation.md) | Installation via conda, pip, and local clone |
| [doc/nisar_gcov_convert_examples.md](doc/nisar_gcov_convert_examples.md) | Full usage examples for `seppo_nisar_gcov_convert` |
| [doc/ratio.md](doc/ratio.md) | Dual-pol ratio output details and formulas |

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

(c) 2026 Earth Big Data LLC | https://earthbigdata.com
