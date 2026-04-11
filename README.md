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
| `seppo_nisar_rslc_convert` | Subset NISAR L-band RSLC HDF5 files directly from S3/HTTPS; output is compatible with isce3, GAMMA Remote Sensing, and SEPPO for interferometric processing; geographic bbox, pixel, and coordinate subsetting; quicklook generation |
| `seppo_nisar_gcov_convert` | Convert NISAR GCOV HDF5 to Cloud Optimized GeoTIFF (COG), BigTIFF, or HDF5 subset with optional sigma0 conversion, reprojection, downscaling, and VRT time-series stacking |
| `seppo_nisar_gcov_convert_S` | S-band variant of `seppo_nisar_gcov_convert` |
| `seppo_nisar_gslc_convert` | Convert NISAR GSLC HDF5 complex data to COG: power, amplitude, magnitude, wrapped phase, or raw complex SLC (`-cslc`); supports reprojection, downscaling, and VRT stacking |
| `seppo_nisar_coherence` | Compute pairwise interferometric coherence from co-registered NISAR GSLC complex SLC files with optional crop, downscale, and reprojection |
| `seppo_nisar_search` | Search NISAR product URLs via NASA Earthdata CMR |
| `seppo_earthaccess_credentials` | Manage NASA Earthdata S3 credentials and bearer token |

---

## Installation

See the [Installation guide](https://openseppo.readthedocs.io/en/latest/installation/)
for full instructions including conda environment setup, pip install, and NASA Earthdata
credential configuration.

Quick start:

```bash
mamba create -n openseppo -c conda-forge openseppo aria2
conda activate openseppo
```

---

## Documentation

Full documentation is hosted at **[openseppo.readthedocs.io](https://openseppo.readthedocs.io)**.

| Document | Description |
|----------|-------------|
| [Installation](https://openseppo.readthedocs.io/en/latest/installation/) | Installation via conda, pip, and local clone |
| [seppo_nisar_gcov_convert examples](https://openseppo.readthedocs.io/en/latest/nisar_gcov_convert_examples/) | Full usage examples for `seppo_nisar_gcov_convert` |
| [Hawaii Volcanoes examples](doc/nisar_hawaii_examples.md) | End-to-end RSLC, GSLC, GCOV search + inspect + subset examples (Kilauea) |
| [seppo_nisar_rslc_convert CLI](doc/nisar_rslc_convert_cli.md) | CLI reference and examples for RSLC subsetting |
| [Dual-pol ratio](https://openseppo.readthedocs.io/en/latest/ratio/) | Dual-pol ratio output details and formulas |

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

(c) 2026 Earth Big Data LLC | https://earthbigdata.com
