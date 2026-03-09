# Installation

`openseppo` requires **MacOS or Linux**.


##  pip  (From PyPI)

Use `pip` if `mamba/conda` is not an option or not working. 

```bash
pip install "openseppo[nisar]" 
```

--- 

##  conda/mamba 

### Create a new environment with all dependencies

```bash
mamba env create -n openseppo -c conda-forge \
    "python>=3.12" \
    openseppo \
    aria2
conda activate openseppo
```

### Install into an existing environment

```bash
conda activate myenv
mamba install -c conda-forge openseppo aria2
```

---


### From a local clone (development / editable install)

```bash
git clone https://github.com/EarthBigData/openSEPPO.git
cd openSEPPO
pip install -e ".[nisar]"
```

The `[nisar]` extra installs the full dependency stack required for
`seppo_nisar_gcov_convert` and `seppo_nisar_search`:
`earthaccess`, `h5py`, `numpy`, `rasterio`, `s3fs`, `xarray`, `scipy`, `pyproj`.

The base install (no extra) provides only `seppo_nisar_search` and
`seppo_earthaccess_credentials`, which require only `requests` and `earthaccess`.

---

## aria2

`aria2` is a multi-connection download accelerator used 
to cache remote HDF5 file https:// urls quickly. It is a
system-level tool and must be installed via conda/mamba (or your OS package manager)
rather than pip.


When installing `openseppo` via *pip*, install `aria2` separately via conda or your OS package manager:

```bash
# conda/mamba
mamba install -c conda-forge aria2

# macOS (Homebrew)
brew install aria2

# Linux (apt)
sudo apt install aria2
```

---

## NASA Earthdata credentials

All tools that access NISAR data require a free NASA Earthdata account.
It is advantageous to store your credentials in `~/.netrc`:

```
machine urs.earthdata.nasa.gov
    login <your_username>
    password <your_password>
```

Or use the interactive login prompt that `earthaccess` shows automatically on
first use. To pre-generate and cache the bearer token used for HTTPS downloads:

```bash
seppo_earthaccess_credentials -t
```

---

## Installed CLI tools

| Command | Description |
|---------|-------------|
| `seppo_nisar_gcov_convert` | Convert NISAR GCOV HDF5 to COG/GTiff/HDF5 |
| `seppo_nisar_search` | Search NISAR products via NASA Earthdata CMR |
| `seppo_earthaccess_credentials` | Manage Earthdata S3 credentials and bearer token |
