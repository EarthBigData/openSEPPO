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
| `seppo_nisar_gcov_convert_S` | S-band variant of `seppo_nisar_gcov_convert` (Beta release) |
| `seppo_nisar_gslc_convert` | Convert NISAR GSLC HDF5 complex data to COG or HDF5 subset: power, amplitude, magnitude, wrapped phase, or raw complex SLC (`-cslc`); supports subsetting, reprojection, downscaling, and VRT stacking |
| `seppo_nisar_gunw_convert` | Convert NISAR GUNW (geocoded unwrapped interferogram, L2 InSAR pair) HDF5 to COG, BigTIFF, or a self-contained HDF5 subset; unwrapped and wrapped interferogram and pixel-offset grids, with subsetting, reprojection, downscaling and time-series stacks |
| `seppo_nisar_sme2_convert` | Convert NISAR SME2 (soil moisture, L3) HDF5 to COG, BigTIFF, or a self-contained HDF5 subset on the native EASE-Grid 2.0; soil moisture, algorithm candidates, ancillary and radar layers, with subsetting, reprojection, and downscaling |
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
mamba create -n openseppo -c conda-forge openseppo aria2 matplotlib
conda activate openseppo
```

---

## Documentation

Full documentation with quick start, examples, and CLI reference is available in two places:

- **In this repository:** [doc/index.md](doc/index.md) — includes TL;DR, copy-pasteable examples for GCOV, GSLC, and RSLC
- **Read the Docs:** [openseppo.readthedocs.io](https://openseppo.readthedocs.io)

| Document | Description |
|----------|-------------|
| [Installation](https://openseppo.readthedocs.io/en/latest/installation/) | Installation via conda, pip, and local clone |
| [Hawaii Volcanoes examples](https://openseppo.readthedocs.io/en/latest/nisar_hawaii_examples/) | End-to-end RSLC, GSLC, GCOV search + inspect + subset examples (Kilauea) |
| [Python API / Jupyter Integration](https://openseppo.readthedocs.io/en/latest/gcov_processing_overview/) | Use openSEPPO as a Python API in scripts and Jupyter notebooks |
| [GCOV examples](https://openseppo.readthedocs.io/en/latest/nisar_gcov_convert_examples/) | Full usage examples for `seppo_nisar_gcov_convert` |
| [RSLC CLI reference](https://openseppo.readthedocs.io/en/latest/nisar_rslc_convert_cli/) | CLI reference and examples for RSLC subsetting |
| [GSLC CLI reference](https://openseppo.readthedocs.io/en/latest/nisar_gslc_convert_cli/) | CLI reference for GSLC conversion |
| [GCOV CLI reference](https://openseppo.readthedocs.io/en/latest/nisar_gcov_convert_cli/) | CLI reference for GCOV conversion |
| [GUNW CLI reference](https://openseppo.readthedocs.io/en/latest/nisar_gunw_convert_cli/) | CLI reference for GUNW interferogram conversion |
| [GUNW examples](https://openseppo.readthedocs.io/en/latest/nisar_gunw_convert_examples/) | Search-to-interferogram and interferogram-stack example over the 2026 Venezuela earthquakes |
| [SME2 CLI reference](https://openseppo.readthedocs.io/en/latest/nisar_sme2_convert_cli/) | CLI reference for SME2 soil moisture conversion |
| [SME2 examples](https://openseppo.readthedocs.io/en/latest/nisar_sme2_convert_examples/) | Search-to-COG soil moisture example over South Dakota irrigation |
| [Dual-pol ratio](https://openseppo.readthedocs.io/en/latest/ratio/) | Dual-pol ratio output details and formulas |

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

(c) 2026 Earth Big Data LLC | https://earthbigdata.com
