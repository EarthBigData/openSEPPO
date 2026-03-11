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

## Quick start

PIP:
```bash
pip install "openseppo[nisar]"
# Also install aria2 via conda or OS installers
```

CONDA/MAMBA:
```bash
mamba env create -n openseppo -c conda-forge "python>=3.12" openseppo aria2
conda activate openseppo
```

See [Installation](installation.md) for full instructions including pip and local clone options.

---

## Documentation

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

---

## License

Apache License 2.0 — (c) 2026 Earth Big Data LLC | [earthbigdata.com](https://earthbigdata.com)
