"""
openseppo.nisar.nisar_tools_rslc -- NISAR RSLC subsetting core
***************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Core library for subsetting NISAR L-band RSLC (Radar-coordinates Single-Look
Complex) HDF5 files.  The output is a fully self-contained RSLC HDF5 that
preserves all metadata required by isce3 for interferometric processing and
generation of higher-level products (GSLC, GCOV).

NISAR RSLC HDF5 layout (observed from real products)
------------------------------------------------------
::

    /science/LSAR/RSLC/swaths/
        zeroDopplerTime            (n_azimuth,)      SHARED across frequencies
        zeroDopplerTimeSpacing     scalar
        frequencyA/
            slantRange             (n_range_A,)
            HH, HV, ...           (n_azimuth, n_range_A)  complex64
            validSamplesSubSwath1  (n_azimuth, 2)
            scalar metadata ...
        frequencyB/
            slantRange             (n_range_B,)       different from A
            HH, HV, ...           (n_azimuth, n_range_B)  complex64
            ...
    /science/LSAR/RSLC/metadata/
        orbit/                     copied verbatim
        attitude/                  copied verbatim
        calibrationInformation/    copied verbatim
        geolocationGrid/           copied verbatim
        processingInformation/     copied verbatim
    /science/LSAR/identification/
        zeroDopplerStartTime       updated for subset
        zeroDopplerEndTime         updated for subset
        boundingPolygon            recomputed via rdr2geo
        ... (all others copied verbatim)
"""

import os
import gc
import re
import tempfile
import numpy as np
import h5py

import openseppo.nisar.nisar_tools as nisar_tools
from openseppo.nisar.nisar_tools import (
    create_s3_fs,
    _earthaccess_login,
    HAS_EARTHACCESS,
    open_h5_lazy,
    _decode_h5_scalar,
    cache_to_local,
)
from openseppo.nisar.radar_geometry import (
    OrbitInterpolator,
    rdr2geo,
    rdr2geo_corners,
    geo2rdr_bbox,
    corners_to_wkt,
)

# ---------------------------------------------------------------------------
# RSLC-specific constants  (L-band only; S-band is a separate module)
# ---------------------------------------------------------------------------

RSLC_SWATH_BASE = "/science/LSAR/RSLC/swaths"
RSLC_META_BASE = "/science/LSAR/RSLC/metadata"
RSLC_IDENT_BASE = "/science/LSAR/identification"


# =========================================================
# 1. HDF5 INSPECTION
# =========================================================

def inspect_rslc_structure(f):
    """Scan an RSLC HDF5 and return structure dict."""
    structure = {}

    # identification
    ident = {}
    for key in ("zeroDopplerStartTime", "zeroDopplerEndTime",
                "boundingPolygon", "compositeReleaseId",
                "trackNumber", "frameNumber", "absoluteOrbitNumber",
                "orbitPassDirection", "lookDirection", "listOfFrequencies",
                "missionId", "productType", "radarBand"):
        p = f"{RSLC_IDENT_BASE}/{key}"
        if p in f:
            try:
                val = f[p][()]
                if isinstance(val, (bytes, np.bytes_)):
                    ident[key] = val.decode()
                elif isinstance(val, np.ndarray) and val.ndim == 0:
                    v = val.item()
                    ident[key] = v.decode() if isinstance(v, (bytes, np.bytes_)) else v
                elif isinstance(val, np.ndarray):
                    ident[key] = [x.decode() if isinstance(x, (bytes, np.bytes_)) else x
                                  for x in val.flat]
                else:
                    ident[key] = val
            except Exception:
                pass

    # orbit
    orbit_info = {}
    orb = f"{RSLC_META_BASE}/orbit"
    if orb in f:
        try:
            orbit_info["n_vectors"] = f[f"{orb}/time"].shape[0]
            for k in ("interpMethod", "orbitType"):
                p = f"{orb}/{k}"
                if p in f:
                    orbit_info[k] = _decode_h5_scalar(f[p][()])
        except Exception:
            pass

    if RSLC_SWATH_BASE not in f:
        return {"error": f"Path {RSLC_SWATH_BASE} not found."}

    # shared zeroDopplerTime
    zd_path = f"{RSLC_SWATH_BASE}/zeroDopplerTime"
    n_azimuth = 0
    if zd_path in f:
        zd_ds = f[zd_path]
        n_azimuth = zd_ds.shape[0]
        structure["_zeroDopplerTime"] = {
            "n_azimuth": n_azimuth,
            "start_s": float(zd_ds[0]),
            "end_s": float(zd_ds[-1]),
            "spacing_s": float(zd_ds[1] - zd_ds[0]) if n_azimuth > 1 else 0.0,
        }

    # per-frequency
    for freq_key in sorted(f[RSLC_SWATH_BASE].keys()):
        if not freq_key.startswith("frequency"):
            continue
        freq_code = freq_key.replace("frequency", "")
        sw = f"{RSLC_SWATH_BASE}/{freq_key}"
        try:
            sr_ds = f[f"{sw}/slantRange"]
            n_rg = sr_ds.shape[0]

            variables = []
            var_details = {}
            for item in sorted(f[sw].keys()):
                obj = f[f"{sw}/{item}"]
                if (isinstance(obj, h5py.Dataset) and len(obj.shape) == 2
                        and len(item) == 2 and item.isupper()):
                    variables.append(item)
                    var_details[item] = {"dtype": str(obj.dtype),
                                         "shape": obj.shape, "chunks": obj.chunks}

            scalars = {}
            for sn in ("processedCenterFrequency", "processedRangeBandwidth",
                       "slantRangeSpacing", "acquiredCenterFrequency",
                       "acquiredRangeBandwidth", "nominalAcquisitionPRF",
                       "sceneCenterAlongTrackSpacing",
                       "sceneCenterGroundRangeSpacing", "numberOfSubSwaths"):
                sp = f"{sw}/{sn}"
                if sp in f:
                    try:
                        scalars[sn] = float(f[sp][()])
                    except Exception:
                        pass

            structure[freq_code] = {
                "n_range": n_rg, "n_azimuth": n_azimuth,
                "slantRange_start_m": float(sr_ds[0]),
                "slantRange_end_m": float(sr_ds[-1]),
                "slantRange_spacing_m": float(sr_ds[1] - sr_ds[0]) if n_rg > 1 else 0.0,
                "vars": variables, "var_details": var_details, "scalars": scalars,
            }
        except Exception as e:
            structure[freq_code] = {"error": str(e)}

    structure["_identification"] = ident
    structure["_orbit"] = orbit_info
    return structure


def get_swath_info(h5_handle, frequency="A"):
    """Read coordinate arrays for a frequency.  zeroDopplerTime is shared."""
    sw = f"{RSLC_SWATH_BASE}/frequency{frequency}"
    zd_path = f"{RSLC_SWATH_BASE}/zeroDopplerTime"
    try:
        sr_ds = h5_handle[f"{sw}/slantRange"]
        zd_ds = h5_handle[zd_path]
    except KeyError as e:
        raise KeyError(f"RSLC swath data not found: {e}")
    return {"slantRange": sr_ds[:], "zeroDopplerTime": zd_ds[:],
            "n_range": sr_ds.shape[0], "n_azimuth": zd_ds.shape[0],
            "swath_path": sw, "freq": frequency}


def get_acquisition_metadata(h5_handle):
    meta = {}
    try:
        ts = _decode_h5_scalar(h5_handle[f"{RSLC_IDENT_BASE}/zeroDopplerStartTime"][()])
        if "T" in ts:
            d, t = ts.split("T")
            meta["ACQUISITION_DATE"] = d
            meta["ACQUISITION_TIME"] = t
        else:
            meta["ACQUISITION_DATETIME"] = ts
    except Exception:
        pass
    for k, p in [("CRID", f"{RSLC_IDENT_BASE}/compositeReleaseId"),
                 ("ISCE3_VERSION",
                  f"{RSLC_META_BASE}/processingInformation/algorithms/softwareVersion")]:
        try:
            meta[k] = _decode_h5_scalar(h5_handle[p][()])
        except Exception:
            pass
    return meta


def _build_orbit_interpolator(h5_handle):
    """Build OrbitInterpolator.  Orbit times are seconds since an epoch
    derived from the identification zeroDopplerStartTime."""
    orb = f"{RSLC_META_BASE}/orbit"
    return OrbitInterpolator(h5_handle[f"{orb}/time"][:],
                             h5_handle[f"{orb}/position"][:],
                             h5_handle[f"{orb}/velocity"][:])


class _PartialFile:
    """
    Virtual read-only file wrapper: holds the first *N* bytes in memory,
    reports *virtual_size* to h5py.  Reads beyond cached region return
    zeros.  Memory = downloaded data, NOT virtual size.
    """
    __slots__ = ("_data", "_size", "_pos")

    def __init__(self, data, virtual_size):
        self._data = data if isinstance(data, (bytes, bytearray)) else bytes(data)
        self._size = virtual_size
        self._pos = 0

    def read(self, n=-1):
        if n < 0:
            n = self._size - self._pos
        end = min(self._pos + n, len(self._data))
        if self._pos < end:
            result = self._data[self._pos:end]
            pad = n - len(result)
            self._pos += n
            return result + (b"\x00" * pad if pad > 0 else b"")
        self._pos += n
        return b"\x00" * n

    def seek(self, pos, whence=0):
        if whence == 0:
            self._pos = pos
        elif whence == 1:
            self._pos += pos
        elif whence == 2:
            self._pos = self._size + pos
        return self._pos

    def tell(self):
        return self._pos

    @property
    def mode(self):
        return "rb"


def _open_header(url, input_fs, use_earthdata=False,
                  header_bytes=4 * 1024 * 1024, verbose=False):
    """
    Download the first *header_bytes* (default 4 MB) of a remote NISAR
    HDF5 file into memory and open with h5py.

    This captures the HDF5 superblock and B-tree, making the full group/
    dataset tree navigable.  Dataset byte offsets and sizes are available
    via ``ds.id.get_offset()`` / ``ds.id.get_storage_size()`` but actual
    data reads will return zeros for regions beyond the header.

    Returns ``(h5py.File, full_file_size)`` or ``(None, 0)`` on failure.
    """
    try:
        if url.startswith("https://"):
            if HAS_EARTHACCESS:
                import earthaccess
                session = earthaccess.get_requests_https_session()
            else:
                import requests
                session = requests
            headers = {"Range": f"bytes=0-{header_bytes - 1}"}
            resp = session.get(url, headers=headers, stream=True)
            resp.raise_for_status()
            data = b"".join(resp.iter_content(chunk_size=4 * 1024 * 1024))
            cr = resp.headers.get("Content-Range", "")
            full_size = int(cr.split("/")[-1]) if "/" in cr else 0

        elif url.startswith("s3://"):
            fs = input_fs if input_fs is not None else create_s3_fs()
            full_size = fs.info(url)["size"]
            with fs.open(url, "rb") as s3f:
                data = s3f.read(header_bytes)
        else:
            return None, 0

        if verbose:
            print(f"    Header cache: {len(data) / 1e6:.1f} MB "
                  f"(full file: {full_size / 1e9:.1f} GB)", flush=True)

        pf = _PartialFile(data, full_size)
        return h5py.File(pf, "r"), full_size

    except Exception as e:
        if verbose:
            print(f"    Warning: header cache failed ({e})", flush=True)
    return None, 0


def _collect_metadata_reads(header_f, frequencies, var_pols,
                             zd_min, zd_max, sr_min, sr_max):
    """
    Walk the HDF5 tree via the header handle and collect a list of
    (hdf5_path, byte_offset, byte_size) for all metadata datasets that
    need to be copied or subsetted.

    Skips:
    - SLC polarisation data (read separately as targeted range reads)
    - Calibration groups for unrequested polarisations
    - Datasets with None offset (inline/header storage -- already readable)
    """
    skip_freq_groups = {f"{RSLC_SWATH_BASE}/frequency{fq}" for fq in frequencies}
    skip_freq_groups.add(f"{RSLC_SWATH_BASE}/zeroDopplerTime")
    skip_freq_groups.add(f"{RSLC_SWATH_BASE}/zeroDopplerTimeSpacing")

    reads = []  # (path, offset, size)
    inline = []  # (path,) -- datasets with data in the header

    def _visit(name, obj):
        if not isinstance(obj, h5py.Dataset):
            return
        full_path = f"/{name}"

        # Skip SLC data in swath frequency groups
        for sg in skip_freq_groups:
            if full_path.startswith(sg):
                return

        # Skip calibration for unrequested pols
        # e.g. /science/LSAR/RSLC/metadata/calibrationInformation/frequencyA/VH/...
        parts = full_path.split("/")
        for i, p in enumerate(parts):
            if len(p) == 2 and p.isupper() and p not in var_pols:
                # Check this is under calibrationInformation
                if "calibrationInformation" in parts[:i]:
                    return

        off = obj.id.get_offset()
        sz = obj.id.get_storage_size()
        if off is None or sz == 0:
            inline.append(full_path)
        else:
            reads.append((full_path, off, sz))

    header_f.visititems(_visit)
    return reads, inline


def _look_side_from(h5_or_dst):
    ld = f"{RSLC_IDENT_BASE}/lookDirection"
    if ld in h5_or_dst:
        try:
            if _decode_h5_scalar(h5_or_dst[ld][()]).strip().lower().startswith("l"):
                return -1
        except Exception:
            pass
    return 1


# =========================================================
# 2. COORDINATE SUBSETTING HELPERS
# =========================================================

def _indices_from_slant_range(sr, sr_s, sr_e):
    idx = np.where((sr >= sr_s) & (sr <= sr_e))[0]
    if len(idx) == 0:
        raise ValueError(f"No slant-range samples in [{sr_s}, {sr_e}]. "
                         f"File: [{sr[0]:.2f}, {sr[-1]:.2f}] m.")
    return int(idx[0]), int(idx[-1] - idx[0] + 1)


def _indices_from_zero_doppler(zd, zd_s, zd_e):
    idx = np.where((zd >= zd_s) & (zd <= zd_e))[0]
    if len(idx) == 0:
        raise ValueError(f"No azimuth lines in [{zd_s}, {zd_e}]. "
                         f"File: [{zd[0]:.6f}, {zd[-1]:.6f}] s.")
    return int(idx[0]), int(idx[-1] - idx[0] + 1)


def _compute_subset_window(info, srcwin, coordwin, projwin, projwin_srs,
                            h5_handle, verbose):
    """Compute (az_off, az_size, rg_off, rg_size) for one frequency."""
    n_az, n_rg = info["n_azimuth"], info["n_range"]

    if srcwin:
        az_off, rg_off, az_size, rg_size = srcwin
    elif coordwin:
        sr_s, sr_e, zd_s, zd_e = coordwin
        rg_off, rg_size = _indices_from_slant_range(info["slantRange"], sr_s, sr_e)
        az_off, az_size = _indices_from_zero_doppler(info["zeroDopplerTime"], zd_s, zd_e)
        if verbose:
            print(f"      coordwin -> az=[{az_off}:{az_off+az_size}], "
                  f"rg=[{rg_off}:{rg_off+rg_size}]", flush=True)
    elif projwin:
        ulx, uly, lrx, lry = projwin
        orbit = _build_orbit_interpolator(h5_handle)
        az_min, az_max, sr_min, sr_max = geo2rdr_bbox(
            min(ulx, lrx), min(uly, lry), max(ulx, lrx), max(uly, lry),
            orbit, height=0.0)
        rg_off, rg_size = _indices_from_slant_range(info["slantRange"], sr_min, sr_max)
        az_off, az_size = _indices_from_zero_doppler(info["zeroDopplerTime"], az_min, az_max)
        if verbose:
            print(f"      projwin -> az=[{az_off}:{az_off+az_size}], "
                  f"rg=[{rg_off}:{rg_off+rg_size}]", flush=True)
    else:
        az_off, rg_off, az_size, rg_size = 0, 0, n_az, n_rg

    az_off = max(0, az_off)
    rg_off = max(0, rg_off)
    az_size = min(az_size, n_az - az_off)
    rg_size = min(rg_size, n_rg - rg_off)
    return (az_off, az_size, rg_off, rg_size) if az_size > 0 and rg_size > 0 else None


# =========================================================
# 3. DEEP COPY HELPERS
# =========================================================

def _copy_attrs(src, dst):
    for k, v in src.attrs.items():
        try:
            dst.attrs.create(k, v)
        except Exception:
            try:
                dst.attrs[k] = v
            except Exception:
                pass


def _copy_dataset(src_ds, dst_group, name, data=None, data_src_ds=None):
    """Copy a dataset.  *data_src_ds* is the remote handle for reads;
    *src_ds* is the header handle for structure.  Falls back gracefully."""
    reader = data_src_ds if data_src_ds is not None else src_ds
    try:
        if data is None:
            data = reader[()]
    except OSError:
        return None
    kw = {}
    try:
        fv = src_ds.fillvalue
        if fv is not None:
            kw["fillvalue"] = fv
    except Exception:
        pass
    dst_ds = dst_group.create_dataset(name, data=data, **kw)
    # Copy attrs from whichever handle can read them
    try:
        _copy_attrs(reader, dst_ds)
    except OSError:
        try:
            _copy_attrs(src_ds, dst_ds)
        except OSError:
            pass
    return dst_ds


def _deep_copy_group(src_group, dst_group, skip_paths=None, data_src=None):
    """*data_src*: h5py.File to read data from (if different from src_group's file)."""
    if skip_paths is None:
        skip_paths = set()
    # Copy group attrs -- prefer data_src (remote) since header may have truncated heap
    src_path = src_group.name
    if data_src is not None and src_path in data_src:
        try:
            _copy_attrs(data_src[src_path], dst_group)
        except OSError:
            _copy_attrs(src_group, dst_group)
    else:
        _copy_attrs(src_group, dst_group)
    for name in src_group:
        src_path = f"{src_group.name}/{name}".replace("//", "/")
        if src_path in skip_paths:
            continue
        lnk = src_group.get(name, getlink=True)
        if isinstance(lnk, h5py.SoftLink):
            dst_group[name] = h5py.SoftLink(lnk.path); continue
        if isinstance(lnk, h5py.ExternalLink):
            dst_group[name] = h5py.ExternalLink(lnk.filename, lnk.path); continue
        obj = src_group[name]
        if isinstance(obj, h5py.Group):
            _deep_copy_group(obj, dst_group.require_group(name), skip_paths, data_src)
        elif isinstance(obj, h5py.Dataset):
            ds_reader = data_src[src_path] if data_src is not None and src_path in data_src else None
            _copy_dataset(obj, dst_group, name, data_src_ds=ds_reader)


# =========================================================
# 4. SPATIALLY-DEPENDENT METADATA UPDATES
# =========================================================

def _adjust_valid_samples(vs_data, rg_start, rg_count):
    """Adjust validSamplesSubSwathN for range subset."""
    if vs_data.size == 0:
        return vs_data
    out = vs_data.copy().astype(np.int32)
    rg_end = rg_start + rg_count
    for c in range(0, out.shape[1] - 1, 2):
        out[:, c] = np.clip(out[:, c], rg_start, rg_end) - rg_start
        out[:, c + 1] = np.clip(out[:, c + 1], rg_start, rg_end) - rg_start
    return out


def _update_identification_times(dst, az_start, az_count, verbose):
    """Update zeroDopplerStartTime/EndTime from the subsetted zeroDopplerTime."""
    sp = f"{RSLC_IDENT_BASE}/zeroDopplerStartTime"
    ep = f"{RSLC_IDENT_BASE}/zeroDopplerEndTime"
    if sp not in dst or ep not in dst:
        return
    zd_path = "science/LSAR/RSLC/swaths/zeroDopplerTime"
    if zd_path not in dst:
        return
    zd = dst[zd_path][:]
    if len(zd) == 0:
        return

    # Read original start time to get the date prefix and compute offset
    try:
        from datetime import datetime, timedelta
        orig_start_str = _decode_h5_scalar(dst[sp][()])
        orig_start = datetime.strptime(orig_start_str.split(".")[0], "%Y-%m-%dT%H:%M:%S")

        # zeroDopplerTime values are seconds-of-day
        # Reconstruct absolute times using the date from the original start
        date_base = orig_start.replace(hour=0, minute=0, second=0, microsecond=0)
        new_start = date_base + timedelta(seconds=float(zd[0]))
        new_end = date_base + timedelta(seconds=float(zd[-1]))
        s0 = new_start.strftime("%Y-%m-%dT%H:%M:%S.%f")
        s1 = new_end.strftime("%Y-%m-%dT%H:%M:%S.%f")

        del dst[sp]; dst.create_dataset(sp.lstrip("/"), data=np.bytes_(s0))
        del dst[ep]; dst.create_dataset(ep.lstrip("/"), data=np.bytes_(s1))
        if verbose:
            print(f"    Updated zeroDopplerStartTime: {s0}", flush=True)
            print(f"    Updated zeroDopplerEndTime:   {s1}", flush=True)
    except Exception as e:
        if verbose:
            print(f"    Warning: could not update times: {e}", flush=True)


def _update_bounding_polygon(dst, src_f, frequencies, per_freq_windows,
                              orig_n_az, verbose):
    """Update boundingPolygon via rdr2geo; fallback to interpolation."""
    bp = f"{RSLC_IDENT_BASE}/boundingPolygon"
    if bp not in dst:
        return

    freq = frequencies[0]
    sw = f"science/LSAR/RSLC/swaths/frequency{freq}"
    zd_path = "science/LSAR/RSLC/swaths/zeroDopplerTime"
    sr_path = f"{sw}/slantRange"
    if zd_path not in dst or sr_path not in dst:
        return
    zd = dst[zd_path][:]
    sr = dst[sr_path][:]
    if len(zd) == 0 or len(sr) == 0:
        return

    # rdr2geo
    orb_p = "science/LSAR/RSLC/metadata/orbit"
    try:
        orbit = OrbitInterpolator(dst[f"{orb_p}/time"][:],
                                  dst[f"{orb_p}/position"][:],
                                  dst[f"{orb_p}/velocity"][:])
        corners = rdr2geo_corners(orbit, zd, sr, look_side=_look_side_from(dst))
        if corners is not None:
            del dst[bp]
            dst.create_dataset(bp.lstrip("/"), data=np.bytes_(corners_to_wkt(corners)))
            if verbose:
                print("    Updated boundingPolygon (rdr2geo)", flush=True)
            return
    except Exception:
        pass

    # Fallback: interpolation
    win = per_freq_windows.get(freq)
    if win is None:
        return
    az_s, az_n, rg_s, rg_n = win
    try:
        orig_info = get_swath_info(src_f, freq)
        o_rg = orig_info["n_range"]
        orig_wkt = _decode_h5_scalar(src_f[bp][()])
        matches = re.findall(r"([-\d.]+)\s+([-\d.]+)", orig_wkt)
        if len(matches) < 4:
            return
        pts = [(float(m[0]), float(m[1])) for m in matches[:4]]
        lons, lats = [p[0] for p in pts], [p[1] for p in pts]
        af0, af1 = az_s / orig_n_az, (az_s + az_n) / orig_n_az
        rf0, rf1 = rg_s / o_rg, (rg_s + rg_n) / o_rg
        def _bi(v, a, r):
            return v[0]+(v[1]-v[0])*r + ((v[3]+(v[2]-v[3])*r)-(v[0]+(v[1]-v[0])*r))*a
        nc = [(_bi(lons,a,r), _bi(lats,a,r))
              for a,r in [(af0,rf0),(af0,rf1),(af1,rf1),(af1,rf0)]]
        del dst[bp]
        dst.create_dataset(bp.lstrip("/"), data=np.bytes_(corners_to_wkt(nc)))
        if verbose:
            print("    Updated boundingPolygon (interpolated)", flush=True)
    except Exception as e:
        if verbose:
            print(f"    Warning: could not update boundingPolygon: {e}", flush=True)


def _update_list_of_polarizations(sw_dst, included_vars, verbose):
    lop = "listOfPolarizations"
    if lop not in sw_dst:
        return
    pols = sorted(v for v in included_vars if len(v) == 2 and v.isupper())
    if not pols:
        return
    old_attrs = dict(sw_dst[lop].attrs)
    del sw_dst[lop]
    ds = sw_dst.create_dataset(lop, data=np.array(pols, dtype=f"S{max(len(p) for p in pols)}"))
    for k, v in old_attrs.items():
        try:
            ds.attrs.create(k, v)
        except Exception:
            pass
    if verbose:
        print(f"    Updated listOfPolarizations: {pols}", flush=True)


# =========================================================
# 5. METADATA GRID SUBSETTING
# =========================================================

def _safe_read(ds, slices=None):
    """Read a dataset, returning None on OSError (truncated/partial cache)."""
    try:
        return ds[slices] if slices is not None else ds[()]
    except OSError:
        return None


def _subset_metadata_group(src_grp, dst_grp, zd_min, zd_max, sr_min, sr_max,
                            verbose, data_src=None):
    """
    Subset a metadata group that has its own zeroDopplerTime and slantRange
    coordinate axes (e.g. geolocationGrid, calibrationInformation/geometry,
    processingInformation/parameters).

    Finds indices on the group's own coordinate arrays that cover
    [zd_min, zd_max] x [sr_min, sr_max] with a 1-sample margin for
    interpolation, then slices all datasets that match those dimensions.
    Datasets that don't match are copied verbatim.
    Unreadable datasets (partial cache) are silently skipped.
    """
    zd_name = "zeroDopplerTime"
    sr_name = "slantRange"
    has_zd = zd_name in src_grp
    has_sr = sr_name in src_grp

    def _reader(ds_name):
        """Get the dataset to read data from (data_src or src_grp)."""
        if data_src is not None:
            p = f"{src_grp.name}/{ds_name}".replace("//", "/")
            if p in data_src:
                return data_src[p]
        return src_grp[ds_name]

    if not has_zd and not has_sr:
        _deep_copy_group(src_grp, dst_grp, data_src=data_src)
        return

    # Build index ranges for available axes
    zd_i0, zd_i1, n_zd_orig = 0, 0, 0
    sr_i0, sr_i1, n_sr_orig = 0, 0, 0

    if has_zd:
        zd_arr = _reader(zd_name)[:]
        n_zd_orig = len(zd_arr)
        zd_idx = np.where((zd_arr >= zd_min) & (zd_arr <= zd_max))[0]
        if len(zd_idx) == 0:
            _deep_copy_group(src_grp, dst_grp, data_src=data_src); return
        zd_i0 = max(0, int(zd_idx[0]) - 1)
        zd_i1 = min(n_zd_orig, int(zd_idx[-1]) + 2)

    if has_sr:
        sr_arr = _reader(sr_name)[:]
        n_sr_orig = len(sr_arr)
        sr_idx = np.where((sr_arr >= sr_min) & (sr_arr <= sr_max))[0]
        if len(sr_idx) == 0:
            _deep_copy_group(src_grp, dst_grp, data_src=data_src); return
        sr_i0 = max(0, int(sr_idx[0]) - 1)
        sr_i1 = min(n_sr_orig, int(sr_idx[-1]) + 2)

    gp = src_grp.name
    if data_src is not None and gp in data_src:
        try:
            _copy_attrs(data_src[gp], dst_grp)
        except OSError:
            _copy_attrs(src_grp, dst_grp)
    else:
        _copy_attrs(src_grp, dst_grp)

    for name in src_grp:
        lnk = src_grp.get(name, getlink=True)
        if isinstance(lnk, h5py.SoftLink):
            dst_grp[name] = h5py.SoftLink(lnk.path); continue
        if isinstance(lnk, h5py.ExternalLink):
            dst_grp[name] = h5py.ExternalLink(lnk.filename, lnk.path); continue

        obj = src_grp[name]
        if isinstance(obj, h5py.Group):
            child = dst_grp.require_group(name)
            _deep_copy_group(obj, child, data_src=data_src)
            continue
        if not isinstance(obj, h5py.Dataset):
            continue

        sh = obj.shape
        rd = _reader(name)

        # Coordinate arrays (already read into zd_arr/sr_arr)
        if name == zd_name and has_zd:
            d = dst_grp.create_dataset(name, data=zd_arr[zd_i0:zd_i1])
            _copy_attrs(obj, d); continue
        if name == sr_name and has_sr:
            d = dst_grp.create_dataset(name, data=sr_arr[sr_i0:sr_i1])
            _copy_attrs(obj, d); continue

        # 3-D: (height, zeroDopplerTime, slantRange)
        if (len(sh) == 3 and has_zd and has_sr
                and sh[1] == n_zd_orig and sh[2] == n_sr_orig):
            data = rd[:, zd_i0:zd_i1, sr_i0:sr_i1]
            d = dst_grp.create_dataset(name, data=data)
            _copy_attrs(obj, d)
            if verbose:
                print(f"    Subsetted metadata {name}: {sh} -> {data.shape}", flush=True)
            continue

        # 2-D: (zeroDopplerTime, slantRange)
        if (len(sh) == 2 and has_zd and has_sr
                and sh[0] == n_zd_orig and sh[1] == n_sr_orig):
            data = rd[zd_i0:zd_i1, sr_i0:sr_i1]
            d = dst_grp.create_dataset(name, data=data)
            _copy_attrs(obj, d)
            if verbose:
                print(f"    Subsetted metadata {name}: {sh} -> {data.shape}", flush=True)
            continue

        # 2-D with only slantRange axis
        if len(sh) == 2 and has_sr and sh[1] == n_sr_orig:
            data = rd[:, sr_i0:sr_i1]
            d = dst_grp.create_dataset(name, data=data)
            _copy_attrs(obj, d)
            if verbose:
                print(f"    Subsetted metadata {name}: {sh} -> {data.shape}", flush=True)
            continue

        # 2-D with only zeroDopplerTime axis
        if len(sh) == 2 and has_zd and sh[0] == n_zd_orig:
            d = dst_grp.create_dataset(name, data=rd[zd_i0:zd_i1, :])
            _copy_attrs(obj, d); continue

        # 1-D matching zeroDopplerTime
        if len(sh) == 1 and has_zd and sh[0] == n_zd_orig:
            d = dst_grp.create_dataset(name, data=rd[zd_i0:zd_i1])
            _copy_attrs(obj, d); continue

        # 1-D matching slantRange
        if len(sh) == 1 and has_sr and sh[0] == n_sr_orig:
            d = dst_grp.create_dataset(name, data=rd[sr_i0:sr_i1])
            _copy_attrs(obj, d); continue

        # No match -- copy verbatim
        p = f"{src_grp.name}/{name}".replace("//", "/")
        ds_r = data_src[p] if data_src is not None and p in data_src else None
        _copy_dataset(obj, dst_grp, name, data_src_ds=ds_r)


def _has_coord_axes(grp):
    """Check if a group has its own zeroDopplerTime or slantRange datasets."""
    return "zeroDopplerTime" in grp or "slantRange" in grp


def _deep_copy_group_smart(src_group, dst_group, skip_paths,
                            zd_min, zd_max, sr_min, sr_max, verbose,
                            skip_pols=None, data_src=None):
    """
    Like _deep_copy_group but:
    - Detects sub-groups with coordinate axes and subsets them.
    - Skips calibration sub-groups for unrequested polarizations
      (e.g. skips frequencyA/VH/ when only HH was requested).
    """
    if skip_paths is None:
        skip_paths = set()
    if skip_pols is None:
        skip_pols = set()

    # Prefer data_src for attrs (remote has full heap)
    gp = src_group.name
    if data_src is not None and gp in data_src:
        try:
            _copy_attrs(data_src[gp], dst_group)
        except OSError:
            _copy_attrs(src_group, dst_group)
    else:
        _copy_attrs(src_group, dst_group)

    for name in src_group:
        src_path = f"{src_group.name}/{name}".replace("//", "/")
        if src_path in skip_paths:
            continue

        # Skip calibration groups for unrequested polarizations
        if skip_pols and len(name) == 2 and name.isupper() and name not in skip_pols:
            if verbose:
                print(f"    Skipping calibration group {name} "
                      f"(not in requested pols)", flush=True)
            continue

        lnk = src_group.get(name, getlink=True)
        if isinstance(lnk, h5py.SoftLink):
            dst_group[name] = h5py.SoftLink(lnk.path); continue
        if isinstance(lnk, h5py.ExternalLink):
            dst_group[name] = h5py.ExternalLink(lnk.filename, lnk.path); continue

        obj = src_group[name]
        if isinstance(obj, h5py.Group):
            child = dst_group.require_group(name)
            if _has_coord_axes(obj):
                _subset_metadata_group(obj, child,
                                        zd_min, zd_max, sr_min, sr_max,
                                        verbose, data_src=data_src)
            else:
                _deep_copy_group_smart(obj, child, skip_paths,
                                        zd_min, zd_max, sr_min, sr_max,
                                        verbose, skip_pols, data_src)
        elif isinstance(obj, h5py.Dataset):
            src_path = f"{src_group.name}/{name}".replace("//", "/")
            ds_r = data_src[src_path] if data_src is not None and src_path in data_src else None
            _copy_dataset(obj, dst_group, name, data_src_ds=ds_r)


# =========================================================
# 6. DIMENSION-AWARE DATASET SUBSETTING
# =========================================================

def _subset_swath_dataset(item, item_name, sw_dst,
                          orig_n_az, orig_n_rg,
                          az_s, az_n, rg_s, rg_n,
                          is_slc, verbose):
    """Subset a dataset based on its shape vs original dimensions."""
    import time as _time
    sh = item.shape

    if len(sh) == 2 and sh[0] == orig_n_az and sh[1] == orig_n_rg:
        if verbose and is_slc:
            _t = _time.perf_counter()
            print(f"    Reading {item_name} [{az_s}:{az_s+az_n}, "
                  f"{rg_s}:{rg_s+rg_n}] ...", flush=True)
        data = item[az_s:az_s+az_n, rg_s:rg_s+rg_n]
        if is_slc:
            ds = sw_dst.create_dataset(item_name, data=data,
                                       chunks=(min(128,az_n), min(512,rg_n)),
                                       compression="gzip", compression_opts=4)
        else:
            ds = sw_dst.create_dataset(item_name, data=data)
        _copy_attrs(item, ds)
        if verbose and is_slc:
            print(f"    [t] {item_name}: {_time.perf_counter()-_t:.1f}s "
                  f"({data.nbytes/1e6:.1f} MB)", flush=True)
        if is_slc:
            del data; gc.collect()
        return

    if len(sh) == 2 and sh[0] == orig_n_az:
        # Azimuth-indexed 2-D (e.g. validSamplesSubSwathN)
        data = item[az_s:az_s+az_n, :]
        ds = sw_dst.create_dataset(item_name, data=data)
        _copy_attrs(item, ds)
        if verbose:
            print(f"    Subsetted {item_name} rows: {sh} -> {data.shape}", flush=True)
        return

    if len(sh) == 2 and sh[1] == orig_n_rg:
        data = item[:, rg_s:rg_s+rg_n]
        ds = sw_dst.create_dataset(item_name, data=data)
        _copy_attrs(item, ds)
        if verbose:
            print(f"    Subsetted {item_name} cols: {sh} -> {data.shape}", flush=True)
        return

    if len(sh) == 1 and sh[0] == orig_n_az:
        data = item[az_s:az_s+az_n]
        ds = sw_dst.create_dataset(item_name, data=data)
        _copy_attrs(item, ds)
        if verbose:
            print(f"    Subsetted {item_name} (1-D az): {sh[0]} -> {az_n}", flush=True)
        return

    if len(sh) == 1 and sh[0] == orig_n_rg:
        data = item[rg_s:rg_s+rg_n]
        ds = sw_dst.create_dataset(item_name, data=data)
        _copy_attrs(item, ds)
        if verbose:
            print(f"    Subsetted {item_name} (1-D rg): {sh[0]} -> {rg_n}", flush=True)
        return

    _copy_dataset(item, sw_dst, item_name)


# =========================================================
# 6. RSLC SUBSETTER -- CORE
# =========================================================

def _subset_rslc_to_h5(src_f, frequencies, var_by_freq,
                        az_start, az_count,
                        per_freq_rg_windows, per_freq_n_rg,
                        orig_n_az,
                        output_path, verbose=False,
                        data_src=None):
    """
    Write a subsetted RSLC HDF5 fully compatible with isce3.

    *src_f* is used for tree structure, attributes, and shapes (can be
    a 4 MB header-only handle).  *data_src* is used for actual dataset
    reads (the remote streaming handle).  If *data_src* is None, *src_f*
    is used for both (local file or full cache case).
    """
    import time as _time

    # Skip frequency groups during deep copy (rebuild them manually)
    skip_paths = {f"{RSLC_SWATH_BASE}/frequency{fq}" for fq in frequencies}
    # Also skip the shared zeroDopplerTime (we'll subset it)
    skip_paths.add(f"{RSLC_SWATH_BASE}/zeroDopplerTime")
    skip_paths.add(f"{RSLC_SWATH_BASE}/zeroDopplerTimeSpacing")

    if verbose:
        _t0 = _time.perf_counter()
        print("    Copying metadata tree ...", flush=True)

    # Compute coordinate bounds for metadata grid subsetting
    # Use the widest range across all frequencies
    all_sr_min = min(src_f[f"{RSLC_SWATH_BASE}/frequency{fq}/slantRange"][rg_s]
                     for fq, (rg_s, rg_n) in per_freq_rg_windows.items())
    all_sr_max = max(src_f[f"{RSLC_SWATH_BASE}/frequency{fq}/slantRange"][rg_s + rg_n - 1]
                     for fq, (rg_s, rg_n) in per_freq_rg_windows.items())
    zd_src_arr = src_f[f"{RSLC_SWATH_BASE}/zeroDopplerTime"]
    zd_min = float(zd_src_arr[az_start])
    zd_max = float(zd_src_arr[az_start + az_count - 1])

    # data_src handle for actual data reads (remote or same as src_f)
    df = data_src if data_src is not None else src_f

    # Collect all requested pols for calibration group filtering
    all_pols = set()
    for fq, vlist in var_by_freq.items():
        all_pols.update(vlist)

    with h5py.File(output_path, "w") as dst:
        _copy_attrs(src_f, dst)

        # Deep-copy everything except swath frequency groups and shared zd.
        # Metadata groups with coordinate axes are subsetted.
        # Calibration groups for unrequested polarizations are skipped.
        for top_name in src_f:
            lnk = src_f.get(top_name, getlink=True)
            if isinstance(lnk, h5py.SoftLink):
                dst[top_name] = h5py.SoftLink(lnk.path); continue
            if isinstance(lnk, h5py.ExternalLink):
                dst[top_name] = h5py.ExternalLink(lnk.filename, lnk.path); continue
            obj = src_f[top_name]
            if isinstance(obj, h5py.Group):
                dst_grp = dst.require_group(top_name)
                _deep_copy_group_smart(obj, dst_grp, skip_paths,
                                       zd_min, zd_max, all_sr_min, all_sr_max,
                                       verbose, skip_pols=all_pols,
                                       data_src=df)
            elif isinstance(obj, h5py.Dataset):
                ds_r = df[f"/{top_name}"] if df is not src_f and f"/{top_name}" in df else None
                _copy_dataset(obj, dst, top_name, data_src_ds=ds_r)

        if verbose:
            print(f"    [t] metadata copy: {_time.perf_counter()-_t0:.1f}s", flush=True)

        # --- Shared zeroDopplerTime (subsetted) ---
        zd_src_path = f"{RSLC_SWATH_BASE}/zeroDopplerTime"
        if zd_src_path in src_f:
            zd_sub = df[zd_src_path][az_start:az_start+az_count]
            zd_src = src_f[zd_src_path]  # for attributes
            sw_grp = dst.require_group(RSLC_SWATH_BASE.lstrip("/"))
            d = sw_grp.create_dataset("zeroDopplerTime", data=zd_sub)
            _copy_attrs(zd_src, d)
            if verbose:
                print(f"    zeroDopplerTime: [{zd_sub[0]:.6f}, {zd_sub[-1]:.6f}] s "
                      f"({az_count} lines)", flush=True)

        # --- zeroDopplerTimeSpacing (copy verbatim) ---
        zdts_path = f"{RSLC_SWATH_BASE}/zeroDopplerTimeSpacing"
        if zdts_path in src_f:
            _copy_dataset(src_f[zdts_path], sw_grp, "zeroDopplerTimeSpacing")

        # --- Per-frequency swath groups ---
        for freq in frequencies:
            rg_start, rg_count = per_freq_rg_windows[freq]
            o_rg = per_freq_n_rg[freq]
            sw_src_path = f"{RSLC_SWATH_BASE}/frequency{freq}"
            if sw_src_path not in src_f:
                continue

            sw_src = src_f[sw_src_path]
            sw_dst = dst.require_group(sw_src_path.lstrip("/"))
            _copy_attrs(sw_src, sw_dst)
            vars_inc = set(var_by_freq.get(freq, []))

            # slantRange (subsetted) -- small, read from metadata source
            sr_src = src_f[f"{sw_src_path}/slantRange"]
            sr_sub = sr_src[rg_start:rg_start+rg_count]
            d = sw_dst.create_dataset("slantRange", data=sr_sub)
            _copy_attrs(sr_src, d)
            if verbose:
                print(f"    Freq {freq}: slantRange [{sr_sub[0]:.2f}, {sr_sub[-1]:.2f}] m "
                      f"({rg_count} samples)", flush=True)

            for item_name in sorted(sw_src.keys()):
                if item_name == "slantRange":
                    continue
                lnk = sw_src.get(item_name, getlink=True)
                if isinstance(lnk, h5py.SoftLink):
                    sw_dst[item_name] = h5py.SoftLink(lnk.path); continue
                if isinstance(lnk, h5py.ExternalLink):
                    sw_dst[item_name] = h5py.ExternalLink(lnk.filename, lnk.path); continue
                item = sw_src[item_name]
                if isinstance(item, h5py.Group):
                    _deep_copy_group(item, sw_dst.require_group(item_name)); continue
                if not isinstance(item, h5py.Dataset):
                    continue

                # validSamplesSubSwathN -- read from df (azimuth-indexed)
                if item_name.startswith("validSamplesSubSwath") and len(item.shape) == 2:
                    vs_rd = df[f"{sw_src_path}/{item_name}"]
                    vs = _adjust_valid_samples(
                        vs_rd[az_start:az_start+az_count, :],
                        rg_start, rg_count)
                    ch_az = min(512, az_count)
                    ch_c = min(vs.shape[1], 512) if vs.shape[1] > 0 else 1
                    d = sw_dst.create_dataset(item_name, data=vs,
                                              chunks=(ch_az, ch_c),
                                              compression="gzip", compression_opts=4)
                    _copy_attrs(item, d)
                    if verbose:
                        print(f"    Subsetted {item_name}: {vs.shape}", flush=True)
                    continue

                if item_name == "listOfPolarizations":
                    _copy_dataset(item, sw_dst, item_name); continue

                # Dimension-aware generic subsetting
                # All data reads go through df (remote or local)
                is_slc = item_name in vars_inc
                read_item = df[f"{sw_src_path}/{item_name}"]
                _subset_swath_dataset(read_item, item_name, sw_dst,
                                      orig_n_az, o_rg,
                                      az_start, az_count, rg_start, rg_count,
                                      is_slc, verbose)

            _update_list_of_polarizations(sw_dst, vars_inc, verbose)

        # Post-processing updates
        _update_identification_times(dst, az_start, az_count, verbose)
        _update_bounding_polygon(dst, src_f, frequencies,
                                  {f: (az_start, az_count, *per_freq_rg_windows[f])
                                   for f in frequencies},
                                  orig_n_az, verbose)

    if verbose:
        print(f"    Output: {output_path} ({os.path.getsize(output_path)/1e6:.1f} MB)",
              flush=True)


# =========================================================
# 7. SINGLE-FILE PROCESSOR
# =========================================================

def _process_single_file(h5_url, variable_names, output_dir,
                          srcwin, coordwin, projwin, projwin_srs, frequency,
                          input_fs, output_fs,
                          cache=None, keep=False, use_earthdata=False,
                          verbose=False, all_frequencies=False):
    import time as _time
    h5_basename = h5_url.split("/")[-1]
    base_name = h5_basename[:-3] if h5_basename.lower().endswith(".h5") else h5_basename
    if verbose:
        print(f"\n--> Processing RSLC: {h5_basename}", flush=True)
        _t0 = _time.perf_counter()

    cached_file_path = None
    try:
        if cache is not None:
            file_url = cache_to_local(h5_url, localdir=cache, keep=keep,
                                      use_earthdata=use_earthdata, fs=input_fs)
            if not keep:
                cached_file_path = file_url
        else:
            file_url = h5_url

        f = open_h5_lazy(file_url, input_fs)

        # Determine frequencies
        if all_frequencies:
            frequencies = [k.replace("frequency", "")
                           for k in sorted(f[RSLC_SWATH_BASE].keys())
                           if k.startswith("frequency")] if RSLC_SWATH_BASE in f else []
        else:
            frequencies = [frequency]
        if not frequencies:
            f.close()
            return {"success": False, "h5_url": h5_url, "error": "No frequency groups."}

        # Determine variables per frequency
        var_by_freq = {}
        for fq in frequencies:
            sw = f"{RSLC_SWATH_BASE}/frequency{fq}"
            if sw not in f:
                continue
            if variable_names:
                var_by_freq[fq] = [v for v in variable_names if f"{sw}/{v}" in f]
            else:
                var_by_freq[fq] = [n for n in sorted(f[sw].keys())
                                   if isinstance(f[f"{sw}/{n}"], h5py.Dataset)
                                   and len(f[f"{sw}/{n}"].shape) == 2
                                   and len(n) == 2 and n.isupper()]
        if not var_by_freq:
            f.close()
            return {"success": False, "h5_url": h5_url, "error": "No variables found."}

        # Shared azimuth info
        zd_path = f"{RSLC_SWATH_BASE}/zeroDopplerTime"
        zd_array = f[zd_path][:]
        orig_n_az = len(zd_array)

        # Get primary frequency info for azimuth window computation
        primary = list(var_by_freq.keys())[0]
        primary_info = get_swath_info(f, primary)

        # Compute azimuth window (shared)
        if srcwin:
            az_off, _, az_size, _ = srcwin
        elif coordwin:
            _, _, zd_s, zd_e = coordwin
            az_off, az_size = _indices_from_zero_doppler(zd_array, zd_s, zd_e)
        elif projwin:
            ulx, uly, lrx, lry = projwin
            orbit = _build_orbit_interpolator(f)
            az_min, az_max, _, _ = geo2rdr_bbox(
                min(ulx, lrx), min(uly, lry), max(ulx, lrx), max(uly, lry),
                orbit, height=0.0)
            az_off, az_size = _indices_from_zero_doppler(zd_array, az_min, az_max)
        else:
            az_off, az_size = 0, orig_n_az

        az_off = max(0, az_off)
        az_size = min(az_size, orig_n_az - az_off)
        if az_size <= 0:
            f.close()
            return {"success": False, "h5_url": h5_url, "error": "Azimuth subset empty."}

        # Per-frequency range windows
        per_freq_rg_windows = {}
        per_freq_n_rg = {}
        for fq in list(var_by_freq.keys()):
            info = get_swath_info(f, fq)
            o_rg = info["n_range"]
            per_freq_n_rg[fq] = o_rg

            if srcwin:
                _, rg_off, _, rg_size = srcwin
            elif coordwin:
                sr_s, sr_e, _, _ = coordwin
                rg_off, rg_size = _indices_from_slant_range(info["slantRange"], sr_s, sr_e)
            elif projwin:
                orbit = _build_orbit_interpolator(f)
                _, _, sr_min, sr_max = geo2rdr_bbox(
                    min(ulx, lrx), min(uly, lry), max(ulx, lrx), max(uly, lry),
                    orbit, height=0.0)
                rg_off, rg_size = _indices_from_slant_range(info["slantRange"], sr_min, sr_max)
            else:
                rg_off, rg_size = 0, o_rg

            rg_off = max(0, rg_off)
            rg_size = min(rg_size, o_rg - rg_off)
            if rg_size <= 0:
                if verbose:
                    print(f"    Freq {fq}: range subset empty, skipping.", flush=True)
                del var_by_freq[fq]
                continue
            per_freq_rg_windows[fq] = (rg_off, rg_size)

        if not var_by_freq:
            f.close()
            return {"success": False, "h5_url": h5_url, "error": "All subsets empty."}

        # Validate: -srcwin with mixed-range frequencies
        if srcwin and len(per_freq_rg_windows) > 1:
            dims = {fq: per_freq_n_rg[fq] for fq in per_freq_rg_windows}
            if len(set(dims.values())) > 1:
                f.close()
                return {"success": False, "h5_url": h5_url,
                        "error": f"-srcwin is ambiguous with different range dims {dims}. "
                                 f"Use -coordwin or -projwin."}

        acq_meta = get_acquisition_metadata(f)

        if verbose:
            for fq in var_by_freq:
                ro, rn = per_freq_rg_windows[fq]
                print(f"    Freq {fq}: az=[{az_off}:{az_off+az_size}] "
                      f"rg=[{ro}:{ro+rn}] vars={var_by_freq[fq]}", flush=True)

        # Output filename
        primary_rg = per_freq_rg_windows[primary]
        tag = (f"_subset_az{az_off}-{az_off+az_size}_rg{primary_rg[0]}-{primary_rg[0]+primary_rg[1]}"
               if srcwin or coordwin or projwin else "")
        out_name = f"{base_name}{tag}.h5"

        if output_dir.startswith("s3://"):
            fd, local_out = tempfile.mkstemp(suffix=".h5"); os.close(fd)
        else:
            os.makedirs(output_dir, exist_ok=True)
            local_out = os.path.join(output_dir, out_name)

        # For remote files without full cache: use 4 MB header for tree
        # navigation (plan phase), then targeted reads from remote handle
        # for metadata and SLC data (execute phase).
        is_remote = file_url.startswith("s3://") or file_url.startswith("https://")
        header_f = None
        if is_remote and cache is None:
            if verbose:
                _tm = _time.perf_counter()
            header_f, _full_sz = _open_header(
                file_url, input_fs, use_earthdata=use_earthdata,
                verbose=verbose)
            if header_f is not None and verbose:
                print(f"    [t] header download: "
                      f"{_time.perf_counter() - _tm:.1f}s", flush=True)

        # header_f: use for tree structure traversal (group hierarchy,
        #           dataset shapes, attributes, coordinate arrays)
        # f:        use for actual data reads (SLC + metadata payloads)
        tree_f = header_f if header_f is not None else f

        _subset_rslc_to_h5(tree_f, list(var_by_freq.keys()), var_by_freq,
                            az_off, az_size,
                            per_freq_rg_windows, per_freq_n_rg,
                            orig_n_az,
                            local_out, verbose=verbose,
                            data_src=f)
        f.close()
        if header_f is not None:
            header_f.close()

        if output_dir.startswith("s3://"):
            s3_out = f"{output_dir.rstrip('/')}/{out_name}"
            if verbose:
                print(f"    Uploading to {s3_out} ...", flush=True)
            with open(local_out, "rb") as fh:
                with output_fs.open(s3_out, "wb") as s3fh:
                    s3fh.write(fh.read())
            os.unlink(local_out)
            final_path = s3_out
        else:
            final_path = local_out

        if verbose:
            print(f"    [t] total: {_time.perf_counter()-_t0:.1f}s", flush=True)

        return {"success": True, "h5_url": h5_url, "output": final_path,
                "date": acq_meta.get("ACQUISITION_DATE", "Unknown"),
                "subset": {"az_off": az_off, "az_size": az_size,
                           "rg_off": primary_rg[0], "rg_size": primary_rg[1]}}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "h5_url": h5_url, "error": str(e)}
    finally:
        if cached_file_path and os.path.exists(cached_file_path):
            try:
                os.unlink(cached_file_path)
            except OSError:
                pass


# =========================================================
# 8. BATCH ENTRY POINT
# =========================================================

def process_rslc_subset(h5_url, variable_names, output_path,
                         srcwin=None, coordwin=None, projwin=None, projwin_srs=None,
                         frequency="A", input_auth=None, output_auth=None,
                         list_grids=False, cache=None, keep=False,
                         verbose=False, all_frequencies=False):
    """Batch entry point for RSLC subsetting."""
    use_earthdata = False
    if input_auth is None:
        input_auth = {"use_earthdata": False}
    if "use_earthdata" in input_auth:
        use_earthdata = input_auth["use_earthdata"]
    if output_auth is None:
        output_auth = {}
    urls = h5_url if isinstance(h5_url, list) else [h5_url]

    if use_earthdata and HAS_EARTHACCESS and urls:
        _earthaccess_login(verbose=verbose)

    try:
        _https_ea = use_earthdata and urls and urls[0].startswith("https://")
        input_fs = None if _https_ea else create_s3_fs(input_auth)

        if list_grids:
            print(f"Inspecting RSLC file: {urls[0]}")
            try:
                f = open_h5_lazy(urls[0], input_fs)
                struct = inspect_rslc_structure(f)
                f.close()
                ident = struct.pop("_identification", {})
                orbit = struct.pop("_orbit", {})
                zd_info = struct.pop("_zeroDopplerTime", {})
                print("\n=== NISAR RSLC Structure ===")
                if ident:
                    print("\nIdentification:")
                    for k, v in sorted(ident.items()):
                        vs = str(v)
                        print(f"  {k}: {vs[:120]}{'...' if len(vs)>120 else ''}")
                if orbit:
                    print(f"\nOrbit:\n  State vectors: {orbit.get('n_vectors','?')}")
                    for k in ("interpMethod", "orbitType"):
                        if k in orbit:
                            print(f"  {k}: {orbit[k]}")
                if zd_info:
                    print(f"\nShared zeroDopplerTime:")
                    print(f"  {zd_info['n_azimuth']} azimuth lines")
                    print(f"  [{zd_info['start_s']:.6f}, {zd_info['end_s']:.6f}] s  "
                          f"(spacing: {zd_info['spacing_s']:.8f} s)")
                for freq, det in sorted(struct.items()):
                    if freq.startswith("_"):
                        continue
                    print(f"\nFrequency {freq}:")
                    if "error" in det:
                        print(f"  Error: {det['error']}"); continue
                    print(f"  Range: {det['n_range']} samples, "
                          f"[{det['slantRange_start_m']:.2f}, {det['slantRange_end_m']:.2f}] m  "
                          f"(spacing: {det['slantRange_spacing_m']:.4f} m)")
                    sc = det.get("scalars", {})
                    if sc:
                        print("  Radar parameters:")
                        for k, v in sorted(sc.items()):
                            print(f"    {k}: {v}")
                    print("  SLC variables:")
                    vd = det.get("var_details", {})
                    for vn in det["vars"]:
                        iv = vd.get(vn, {})
                        print(f"    {vn:10s}  dtype={iv.get('dtype','?')}  "
                              f"shape={iv.get('shape','?')}  chunks={iv.get('chunks','?')}")
                return "Inspection Complete."
            except Exception as e:
                import traceback; traceback.print_exc()
                return f"Error: {e}"

        output_fs = None
        if output_path.startswith("s3://"):
            output_fs = create_s3_fs(output_auth)

        is_remote = urls[0].startswith("s3://") or urls[0].startswith("https://")
        if cache is None and not srcwin and not coordwin and not projwin and is_remote:
            cache = "y"

        results = []
        for url in urls:
            res = _process_single_file(url, variable_names, output_path,
                                        srcwin, coordwin, projwin, projwin_srs,
                                        frequency, input_fs, output_fs,
                                        cache=cache, keep=keep,
                                        use_earthdata=use_earthdata,
                                        verbose=verbose,
                                        all_frequencies=all_frequencies)
            results.append(res)
            print(f"  [{'OK' if res['success'] else 'FAIL'}] "
                  f"{res.get('output', res.get('error', '?'))}")

        return f"Processed {sum(1 for r in results if r['success'])}/{len(results)} files."

    except Exception as e:
        import traceback; traceback.print_exc()
        return f"Critical Error: {str(e)}"
