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

## Quick start

```bash
conda create -n openseppo -c conda-forge "python>=3.12" openseppo aria2
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

---

## License

Apache License 2.0 — (c) 2026 Earth Big Data LLC | [earthbigdata.com](https://earthbigdata.com)
