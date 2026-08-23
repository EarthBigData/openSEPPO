"""
openseppo.nisar.nisar_tools_rslc -- NISAR RSLC subsetting core
***************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Subset NISAR L-band RSLC HDF5 files with cloud-optimised I/O.

The output is a self-contained RSLC HDF5 for isce3 interferometric
processing.  The approach:

  1. **Explicit construction** -- every group and dataset in the output
     is enumerated; nothing is deep-copied blindly.
  2. **geolocationGrid bbox lookup** -- for ``-projwin``, the on-file
     lon/lat grids (coordinateX/Y) give exact pixel indices without
     orbit-based geo2rdr.
  3. **Metadata grids subsetted** -- geolocationGrid, calibration
     geometry, antenna patterns, and dopplerCentroid are sliced to
     cover the subset extent.
  4. **Auto-cache for remote** -- remote files are cached locally
     before subsetting (same ``-cache``/``-keep`` pattern as GCOV/GSLC).
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SW = "/science/LSAR/RSLC/swaths"
_META = "/science/LSAR/RSLC/metadata"
_ID = "/science/LSAR/identification"


# =========================================================
# 1. HELPERS
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


def _create_ds(src_f, src_path, dst_grp, name, data, **kw):
    """Create a dataset with subsetted *data* and copy attributes from the
    source dataset at *src_path*.  Returns the new dataset."""
    src_ds = src_f[src_path]
    ds = dst_grp.create_dataset(name, data=data, **kw)
    _copy_attrs(src_ds, ds)
    return ds


def _copy_ds(src_f, path, dst_grp, name=None, data=None):
    """Copy one dataset from src_f[path] into dst_grp.  If *data* is
    provided, use it instead of reading.  Returns the new dataset."""
    src_ds = src_f[path]
    if data is None:
        data = src_ds[()]
    kw = {}
    try:
        fv = src_ds.fillvalue
        if fv is not None:
            kw["fillvalue"] = fv
    except Exception:
        pass
    dst_ds = dst_grp.create_dataset(name or path.split("/")[-1],
                                     data=data, **kw)
    _copy_attrs(src_ds, dst_ds)
    return dst_ds


# Chunks whose byte ranges are this close are fetched as one request.  The
# window's chunks are largely contiguous in the file, so a small tolerance
# collapses hundreds of chunk reads into a few dozen ranges for a few percent
# of extra bytes: for one 11467 x 9934 window, 480 chunks -> 47 ranges, 475 ->
# 500 MB.
_COALESCE_GAP = 1024 * 1024


def _chunk_byte_ranges(ds, r0, r1, c0, c1, gap=_COALESCE_GAP):
    """Coalesced ``(start, end)`` file ranges holding the chunks of a window.

    NISAR granules are written with a chunk index that gives every chunk's
    offset and size up front, and reading it is free even remotely (480
    targeted lookups measured at 0.01 s over HTTPS).  Returns None when the
    dataset is not chunked or the index cannot be read, so the caller keeps
    its ordinary path.
    """
    chunks = ds.chunks
    if not chunks:
        return None
    try:
        spans = []
        for r in range((r0 // chunks[0]) * chunks[0], r1, chunks[0]):
            for c in range((c0 // chunks[1]) * chunks[1], c1, chunks[1]):
                ci = ds.id.get_chunk_info_by_coord((r, c))
                if ci is None or ci.byte_offset is None:
                    continue
                spans.append((ci.byte_offset, ci.byte_offset + ci.size))
    except Exception:
        return None
    if not spans:
        return None
    spans.sort()
    runs = []
    start, end = spans[0]
    for s0, e0 in spans[1:]:
        if s0 - end <= gap:
            end = max(end, e0)
        else:
            runs.append((start, end))
            start, end = s0, e0
    runs.append((start, end))
    return runs


class _PrefetchedFile:
    """File object serving known ranges from memory, the rest from *base*.

    h5py reads more than chunk data -- superblock, B-tree nodes, dataset
    headers -- and those reads must still work.  Serving them from a cached
    file rather than a bare one is what makes prefetching pay: measured
    in-region on a 146 MB window, prefetch over an uncached file took 17.3 s
    against 13.4 s for the ordinary path, while prefetch over a blockcached
    one took 6.0 s.
    """

    def __init__(self, base, parts):
        self.base = base
        self.parts = sorted(parts.items())
        self.size = base.size
        self.pos = 0

    def seek(self, pos, whence=0):
        if whence == 0:
            self.pos = pos
        elif whence == 1:
            self.pos += pos
        else:
            self.pos = self.size + pos
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n < 0:
            n = self.size - self.pos
        start, stop = self.pos, self.pos + n
        for (a, b), buf in self.parts:
            if a <= start and stop <= b:
                self.pos = stop
                return buf[start - a: stop - a]
        self.base.seek(start)
        out = self.base.read(n)
        self.pos = start + len(out)
        return out

    def close(self):
        self.base.close()


def _remote_fs_and_file(file_url, s3_creds, use_earthdata, block_size):
    """``(filesystem, file)`` for a remote granule, or ``(None, None)``.

    The filesystem is kept so ranges can be fetched with one request each
    (``cat_ranges``), while the file carries the page-aligned blockcache that
    open_h5_lazy uses, for everything not prefetched.  *block_size* is passed
    in from the parent, which has already probed it -- probing per worker
    would cost an extra remote open each.
    """
    from openseppo.nisar.nisar_tools import (_earthaccess_https_fs,
                                             _earthaccess_login, HAS_EARTHACCESS)
    try:
        if file_url.startswith("s3://"):
            import s3fs
            fs = s3fs.S3FileSystem(key=s3_creds.get("key"),
                                   secret=s3_creds.get("secret"),
                                   token=s3_creds.get("token"))
        elif file_url.startswith("https://"):
            if use_earthdata and HAS_EARTHACCESS:
                _earthaccess_login()
            fs = _earthaccess_https_fs()
            if fs is None:
                return None, None
        else:
            return None, None
        return fs, fs.open(file_url, mode="rb", cache_type="blockcache",
                           block_size=block_size)
    except Exception:
        return None, None


def _slc_stripe_worker(task):
    """Read one azimuth stripe of one dataset, in a subprocess.

    h5py holds its global 'phil' lock for the whole of a read -- including the
    network wait -- so threads serialise on it and buy nothing.  A separate
    process has its own HDF5 state and its own lock, which is what lets several
    range requests be in flight at once.  Mirrors the GCOV/GSLC reader, except
    that the data is returned in the source dtype: an RSLC subset has to write
    the samples back verbatim, so nothing is cast here.

    When the caller supplies coalesced chunk ranges, they are fetched with one
    request each and served to h5py from memory; every DAAC range request costs
    a fixed ~0.3 s in-region and ~1 s from a laptop, so issuing a few dozen
    instead of a few hundred is worth more than the extra bytes coalescing
    pulls in.  Any failure falls back to the ordinary read.
    """
    (file_url, s3_creds, use_earthdata, ds_path, r0, r1, c0, c1,
     ranges, block_size) = task
    from openseppo.nisar.nisar_tools import (open_h5_lazy, _earthaccess_login,
                                             HAS_EARTHACCESS)

    if ranges:
        fs, base = _remote_fs_and_file(file_url, s3_creds, use_earthdata,
                                       block_size)
        if base is not None:
            try:
                blobs = fs.cat_ranges([file_url] * len(ranges),
                                      [a for a, _ in ranges],
                                      [b for _, b in ranges])
                pf = _PrefetchedFile(base, dict(zip(ranges, blobs)))
                fh = h5py.File(pf, driver="fileobj", mode="r",
                               libver="latest", rdcc_nbytes=0)
                try:
                    return r0, fh[ds_path][r0:r1, c0:c1]
                finally:
                    fh.close()
                    pf.close()
            except Exception:
                try:
                    base.close()
                except Exception:
                    pass

    fs = None
    if file_url.startswith("s3://"):
        import s3fs
        fs = s3fs.S3FileSystem(key=s3_creds.get("key"),
                               secret=s3_creds.get("secret"),
                               token=s3_creds.get("token"))
    elif file_url.startswith("https://") and use_earthdata and HAS_EARTHACCESS:
        _earthaccess_login()
    fh = open_h5_lazy(file_url, fs)
    try:
        return r0, fh[ds_path][r0:r1, c0:c1]
    finally:
        fh.close()


# A window smaller than this is read serially: below it the process spawn and
# the per-worker file open cost more than the concurrency returns.
_PARALLEL_READ_MIN_BYTES = 64 * 1024 * 1024


def _read_window(src_f, ds_path, r0, r1, c0, c1, pool=None, workers=1,
                 file_url=None, s3_creds=None, use_earthdata=False,
                 verbose=False):
    """Read ``[r0:r1, c0:c1]`` of *ds_path*, in parallel stripes when it pays.

    Falls back to the plain h5py read whenever there is no pool, one worker,
    or the window is small -- so the serial path stays exactly what it was.
    Stripes are aligned to the dataset's own chunk height, so no chunk is
    decompressed by two workers.
    """
    ds = src_f[ds_path]
    nbytes = (r1 - r0) * (c1 - c0) * ds.dtype.itemsize
    if pool is None or workers <= 1 or nbytes < _PARALLEL_READ_MIN_BYTES:
        return ds[r0:r1, c0:c1]

    import math
    chunk_h = (ds.chunks or (512, 512))[0]
    stripe_h = max(chunk_h,
                   math.ceil((r1 - r0) / workers / chunk_h) * chunk_h)

    # The parent already has the granule open, so the chunk index and the
    # probed block size are read once here rather than in every worker.
    block_size = None
    if file_url and not file_url.startswith(("s3://", "https://")):
        prefetch = False           # local input: nothing to coalesce
    else:
        prefetch = True
        try:
            from openseppo.nisar.nisar_tools import probe_h5_page_params
            block_size, _ = probe_h5_page_params(file_url)
        except Exception:
            block_size = None

    tasks, starts = [], []
    r = r0
    while r < r1:
        r_end = min(r + stripe_h, r1)
        ranges = (_chunk_byte_ranges(ds, r, r_end, c0, c1)
                  if prefetch else None)
        tasks.append((file_url, s3_creds or {}, use_earthdata,
                      ds_path, r, r_end, c0, c1, ranges, block_size))
        starts.append(r)
        r = r_end

    if len(tasks) < 2:
        return ds[r0:r1, c0:c1]

    out = np.empty((r1 - r0, c1 - c0), dtype=ds.dtype)
    if verbose:
        _nr = sum(len(t[8]) for t in tasks if t[8])
        _how = (f", {_nr} coalesced range requests" if _nr else "")
        print(f"    Reading {ds_path.split('/')[-1]} in {len(tasks)} stripes "
              f"of {stripe_h} lines across {workers} workers{_how} ...",
              flush=True)
    for r_start, stripe in pool.map(_slc_stripe_worker, tasks):
        out[r_start - r0: r_start - r0 + stripe.shape[0]] = stripe
    return out


def _deflate_opts(src_ds, complevel=None):
    """``(compression, compression_opts, shuffle)`` for a copy of *src_ds*.

    *complevel* None mirrors the source, which is what a faithful copy of an
    archive product wants.  An explicit level trades size for time on the
    write, which dominates a subset: measured on a 326 MB slice of one RSLC
    payload, gzip/4 took 6.7 s for 154.0 MB against gzip/1 at 4.1 s for
    155.4 MB -- 39% less time for 0.9% more file.  0 stores the data
    uncompressed (still chunked), for scratch products that are read once.
    """
    if complevel is None:
        opts = src_ds.compression_opts
        return (src_ds.compression or "gzip",
                opts if opts is not None else 4,
                src_ds.shuffle)
    if int(complevel) == 0:
        return (None, None, False)
    return ("gzip", int(complevel), src_ds.shuffle)


def _copy_group_shallow(src_f, grp_path, dst_grp):
    """Copy group attributes only (no datasets/children)."""
    if grp_path in src_f:
        _copy_attrs(src_f[grp_path], dst_grp)


def _copy_group_all(src_f, grp_path, dst_parent, name=None):
    """Recursively copy an entire group with all datasets and attrs."""
    src_grp = src_f[grp_path]
    g = dst_parent.require_group(name or grp_path.split("/")[-1])
    _copy_attrs(src_grp, g)
    for item_name in src_grp:
        obj = src_grp[item_name]
        if isinstance(obj, h5py.Group):
            _copy_group_all(src_f, f"{grp_path}/{item_name}", g, item_name)
        elif isinstance(obj, h5py.Dataset):
            _copy_ds(src_f, f"{grp_path}/{item_name}", g)


def _slice_range(coord_array, lo, hi):
    """Find index range in a 1-D sorted array covering [lo, hi].

    Returns the tightest range of indices whose coordinate values
    bracket [lo, hi].  No extra margin is added -- the metadata
    grids must not extend significantly beyond the SLC data extent
    to avoid buffer overflows in downstream processors.

    When [lo, hi] falls outside the array range (e.g. projwin extends
    beyond data), returns the nearest edge indices instead of the full
    array.
    """
    n = len(coord_array)
    if n == 0:
        return 0, 0
    # Find the bracketing indices: the last point <= lo and first point >= hi
    i_lo = max(0, int(np.searchsorted(coord_array, lo)) - 1)
    i_hi = min(n, int(np.searchsorted(coord_array, hi, side="right")) + 1)
    if i_lo >= i_hi:
        # Edge case: single point
        nearest = int(np.argmin(np.abs(coord_array - (lo + hi) / 2)))
        i_lo = nearest
        i_hi = nearest + 1
    return i_lo, i_hi


# =========================================================
# 2. INSPECTION
# =========================================================

def inspect_rslc(f):
    """Return a printable structure summary of an RSLC HDF5 file."""
    lines = ["=== NISAR RSLC Structure ==="]

    # Identification
    lines.append("\nIdentification:")
    for key in sorted(f[_ID].keys()):
        try:
            v = f[f"{_ID}/{key}"][()]
            if isinstance(v, (bytes, np.bytes_)):
                v = v.decode()
            elif isinstance(v, np.ndarray) and v.ndim == 0:
                vi = v.item()
                v = vi.decode() if isinstance(vi, (bytes, np.bytes_)) else vi
            elif isinstance(v, np.ndarray):
                v = [x.decode() if isinstance(x, (bytes, np.bytes_)) else x
                     for x in v.flat]
            s = str(v)
            lines.append(f"  {key}: {s[:120]}{'...' if len(s)>120 else ''}")
        except Exception:
            pass

    # Orbit
    orb = f"{_META}/orbit"
    if orb in f:
        lines.append(f"\nOrbit: {f[f'{orb}/time'].shape[0]} state vectors")
        for k in ("interpMethod", "orbitType"):
            p = f"{orb}/{k}"
            if p in f:
                lines.append(f"  {k}: {_decode_h5_scalar(f[p][()])}")

    # Shared zeroDopplerTime
    zd = f[f"{_SW}/zeroDopplerTime"]
    lines.append(f"\nShared zeroDopplerTime: {zd.shape[0]} lines, "
                 f"[{zd[0]:.6f}, {zd[-1]:.6f}] s")

    # Per-frequency
    for fk in sorted(f[_SW].keys()):
        if not fk.startswith("frequency"):
            continue
        fc = fk.replace("frequency", "")
        sw = f"{_SW}/{fk}"
        sr = f[f"{sw}/slantRange"]
        lines.append(f"\nFrequency {fc}: {sr.shape[0]} range samples, "
                     f"[{sr[0]:.2f}, {sr[-1]:.2f}] m")
        # SLC vars
        for item in sorted(f[sw].keys()):
            obj = f[f"{sw}/{item}"]
            if (isinstance(obj, h5py.Dataset) and len(obj.shape) == 2
                    and len(item) == 2 and item.isupper()):
                lines.append(f"  {item}: {obj.shape} {obj.dtype}")

    # GeolocationGrid
    geo = f"{_META}/geolocationGrid"
    if geo in f:
        cx = f[f"{geo}/coordinateX"]
        lines.append(f"\nGeolocationGrid: {cx.shape} (height, az, rg)")
        lines.append(f"  EPSG: {f[f'{geo}/epsg'][()]}")

    return "\n".join(lines)


# =========================================================
# 3. BBOX -> PIXEL INDICES via geolocationGrid
# =========================================================

def _query_elevation_point(lon, lat):
    """Query terrain elevation at a single point from the USGS Elevation
    Point Query Service.  Returns height in metres, or None on failure."""
    try:
        import requests
        r = requests.get(
            f"https://epqs.nationalmap.gov/v1/json?x={lon}&y={lat}"
            f"&wkid=4326&units=Meters&includeDate=false", timeout=5)
        return float(r.json()["value"])
    except Exception:
        return None


def new_height_context(max_height=None, min_height=None):
    """Terrain-height context for one run (one CLI invocation).

    Heights decide which geolocationGrid height levels the bbox lookup
    searches, how far range is padded, and the surface the boundingPolygon is
    projected onto.  Resolving them per granule -- which is what a per-file
    USGS query does -- gives every date in a time series its own geometry, so
    the context resolves once and every granule in the run reuses it.
    """
    return {"user_max": max_height, "user_min": min_height,
            "max": None, "min": None, "polygon": None, "source": None}


def resolve_heights(ctx, points=None, verbose=False):
    """Resolve *ctx* once, from user flags or a single elevation lookup.

    Precedence: user flags (no network at all) > one USGS query over *points*
    (lon, lat) > documented defaults.  *points* is whatever geometry the
    caller has -- the projwin corners and centre, or the granule's radar
    corners -- and is only consulted on the first call that carries any; later
    calls return the already-resolved context untouched.

    Sets ``max``/``min`` (the window: cube levels and range padding),
    ``polygon`` (the single height boundingPolygon is projected onto) and
    ``source`` ("user", "usgs" or "default").
    """
    if ctx.get("max") is not None:
        return ctx

    if ctx["user_max"] is not None or ctx["user_min"] is not None:
        # Supplied heights are authoritative -- nothing is queried, so the run
        # is reproducible and works offline and outside the US.
        ctx["max"] = float(ctx["user_max"]) if ctx["user_max"] is not None else 1000.0
        ctx["min"] = float(ctx["user_min"]) if ctx["user_min"] is not None else 0.0
        ctx["polygon"] = (ctx["max"] + ctx["min"]) / 2.0
        ctx["source"] = "user"
    elif points:
        heights = [_query_elevation_point(lon, lat) for lon, lat in points]
        if heights and all(h is not None for h in heights):
            # +500 m for terrain variability between the sampled points.
            ctx["max"] = max(heights) + 500.0
            ctx["min"] = 0.0
            ctx["polygon"] = float(np.mean(heights)) + 500.0
            ctx["source"] = "usgs"
        else:
            ctx["max"], ctx["min"], ctx["polygon"] = 1000.0, 0.0, 500.0
            ctx["source"] = "default"
            print("    [WARN] USGS elevation lookup unavailable -- falling back "
                  "to max 1000 m / min 0 m for this run.\n"
                  "           A successful lookup selects different "
                  "geolocationGrid height levels, so subsets written now will "
                  "NOT match subsets of the same granules written when the "
                  "service answers.\n"
                  "           Pass --max_height/--min_height to pin the "
                  "geometry (the service is US-only, so it always fails "
                  "outside the US).", flush=True)
    else:
        return ctx        # nothing to resolve from yet -- try again with geometry

    if verbose or ctx["source"] == "default":
        print(f"    Terrain heights ({ctx['source']}): window max "
              f"{ctx['max']:.0f} m, min {ctx['min']:.0f} m, polygon "
              f"{ctx['polygon']:.0f} m -- resolved once, reused for every "
              f"granule in this run", flush=True)
    return ctx


def _bbox_to_pixels(src_f, lon_min, lat_min, lon_max, lat_max, freq="A",
                     max_height=None, verbose=False):
    """
    Convert a lon/lat bounding box to (az_off, az_size, rg_off, rg_size)
    using the geolocationGrid coordinateX/Y arrays.

    *max_height* is the value resolve_heights already settled for the run;
    this function never queries the network, so every granule in a stack
    searches the same height levels.

    Higher terrain shifts ground position toward near range in SAR
    geometry.  Approximate range padding by max_height at 35 deg incidence:
        500m -> ~0.9km,  1000m -> ~1.7km,  4000m -> ~7km
    """
    geo = f"{_META}/geolocationGrid"
    heights = src_f[f"{geo}/heightAboveEllipsoid"][:]

    if max_height is None:
        max_height = 1000.0
    if verbose:
        print(f"    Terrain max height: {max_height:.0f}m", flush=True)

    # Select height levels from 0 to max_height
    h_mask = (heights >= 0) & (heights <= max_height)
    if not np.any(h_mask):
        h_mask = np.zeros(len(heights), dtype=bool)
        h_mask[int(np.argmin(np.abs(heights)))] = True
    h_indices = np.where(h_mask)[0]

    lon_all = src_f[f"{geo}/coordinateX"][h_indices, :, :]
    lat_all = src_f[f"{geo}/coordinateY"][h_indices, :, :]

    # Find grid cells inside the bbox across selected height levels
    mask = np.zeros(lon_all.shape[1:], dtype=bool)
    for i in range(len(h_indices)):
        mask |= ((lon_all[i] >= lon_min) & (lon_all[i] <= lon_max) &
                 (lat_all[i] >= lat_min) & (lat_all[i] <= lat_max))

    if not np.any(mask):
        raise ValueError(
            f"No geolocation grid points inside bbox "
            f"[{lon_min}, {lat_min}, {lon_max}, {lat_max}]. "
            f"Grid lon range: [{lon_all.min():.4f}, {lon_all.max():.4f}], "
            f"lat range: [{lat_all.min():.4f}, {lat_all.max():.4f}]")

    az_idx, rg_idx = np.where(mask)
    geo_az_i0 = int(az_idx.min())
    geo_az_i1 = int(az_idx.max()) + 1
    geo_rg_i0 = int(rg_idx.min())
    geo_rg_i1 = int(rg_idx.max()) + 1

    # Expand geolocation grid indices to ensure the bbox is fully
    # covered.  The SAR swath is rotated relative to lon/lat, so a
    # rectangular bbox maps to a parallelogram in radar coordinates.
    # The far-range corners extend further in azimuth; the azimuth
    # margin scales with terrain height (elevation shifts ground
    # position in range, which projects into azimuth via rotation).
    # Range margin is fixed at 1 cell (terrain effect on range is
    # already handled by the height-level selection above).
    geo_zd = src_f[f"{geo}/zeroDopplerTime"][:]
    geo_sr = src_f[f"{geo}/slantRange"][:]

    # Adaptive azimuth margin from terrain height
    inc_rad = np.radians(35)
    sr_shift = max_height / np.sin(inc_rad)
    az_spacing = abs(geo_zd[1] - geo_zd[0]) if len(geo_zd) > 1 else 0.066
    az_spacing_m = az_spacing * 7500.0 if az_spacing < 1.0 else az_spacing
    az_shift = sr_shift * np.sin(np.radians(15))
    az_margin = max(2, int(np.ceil(az_shift / az_spacing_m)))

    geo_az_i0 = max(0, geo_az_i0 - az_margin)
    geo_az_i1 = min(len(geo_zd), geo_az_i1 + az_margin)
    geo_rg_i0 = max(0, geo_rg_i0 - 1)
    geo_rg_i1 = min(len(geo_sr), geo_rg_i1 + 1)

    zd_lo = geo_zd[geo_az_i0]
    zd_hi = geo_zd[geo_az_i1 - 1]
    sr_lo = geo_sr[geo_rg_i0]
    sr_hi = geo_sr[geo_rg_i1 - 1]

    # Map to fine SLC pixel indices
    slc_zd = src_f[f"{_SW}/zeroDopplerTime"][:]
    slc_sr = src_f[f"{_SW}/frequency{freq}/slantRange"][:]

    az_off = int(np.searchsorted(slc_zd, zd_lo))
    az_end = int(np.searchsorted(slc_zd, zd_hi, side="right"))
    rg_off = int(np.searchsorted(slc_sr, sr_lo))
    rg_end = int(np.searchsorted(slc_sr, sr_hi, side="right"))

    az_off = max(0, az_off)
    az_end = min(len(slc_zd), az_end)
    rg_off = max(0, rg_off)
    rg_end = min(len(slc_sr), rg_end)

    return az_off, az_end - az_off, rg_off, rg_end - rg_off


# =========================================================
# 4. CORE SUBSETTER  (explicit construction)
# =========================================================

def _subset_rslc(src_f, dst_path, frequencies, var_by_freq,
                 az_off, az_count, per_freq_rg,
                 verbose=False, max_height=None, min_height=None,
                 height_ctx=None, file_url=None, input_fs=None,
                 use_earthdata=False, read_workers=1, complevel=None):
    """
    Build a subsetted RSLC HDF5 by explicitly constructing every group
    and dataset.  Nothing is deep-copied.

    Parameters
    ----------
    src_f : h5py.File (open, read)
    dst_path : str (local output path)
    frequencies : list of str ("A", "B", ...)
    var_by_freq : dict  {freq: [pol_names]}
    az_off, az_count : int  (shared azimuth window)
    per_freq_rg : dict  {freq: (rg_off, rg_count)}
    """
    import time as _t

    if height_ctx is None:
        height_ctx = new_height_context(max_height, min_height)

    # One process pool for the whole subset: spawning costs a fresh interpreter
    # per worker, so it is paid once rather than per polarisation.
    _pool = None
    _s3_creds = None
    if read_workers > 1 and file_url:
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing as _mp
        from openseppo.nisar.nisar_tools import _s3_creds_from_fs
        _s3_creds = _s3_creds_from_fs(input_fs)
        try:
            _pool = ProcessPoolExecutor(
                max_workers=read_workers, mp_context=_mp.get_context("spawn"))
        except Exception as exc:
            print(f"    [WARN] parallel reader unavailable ({exc}); "
                  f"reading serially.", flush=True)
            _pool = None

    n_az_orig = src_f[f"{_SW}/zeroDopplerTime"].shape[0]
    az_end = az_off + az_count

    # Compute coordinate bounds for metadata grid subsetting
    slc_zd = src_f[f"{_SW}/zeroDopplerTime"]
    zd_lo = float(slc_zd[az_off])
    zd_hi = float(slc_zd[az_end - 1])

    sr_lo = min(float(src_f[f"{_SW}/frequency{fq}/slantRange"][ro])
                for fq, (ro, _) in per_freq_rg.items())
    sr_hi = max(float(src_f[f"{_SW}/frequency{fq}/slantRange"][ro + rn - 1])
                for fq, (ro, rn) in per_freq_rg.items())

    all_pols = set()
    for vl in var_by_freq.values():
        all_pols.update(vl)

    if verbose:
        t0 = _t.perf_counter()

    with h5py.File(dst_path, "w") as dst:
        # Per-phase timing: the payload is only part of the work, and on a
        # remote granule the scattered metadata reads are their own cost.
        _phase_t = [_t.perf_counter()]

        def _mark(label):
            if verbose:
                now = _t.perf_counter()
                print(f"    [t] {label}: {now - _phase_t[0]:.1f}s", flush=True)
                _phase_t[0] = now

        # --- Root attributes ---
        _copy_attrs(src_f, dst)

        # ============================================================
        # /science/LSAR/identification  (copy all, update 3 fields)
        # ============================================================
        _copy_group_all(src_f, _ID, dst, _ID.lstrip("/"))

        id_grp = dst[_ID.lstrip("/")]

        # listOfFrequencies must name what was actually written, not what the
        # source carried: a default subset holds one frequency even when the
        # granule has two, and the copied value would claim both.
        try:
            src_lof = src_f.get(f"{_ID}/listOfFrequencies")
            vals = np.array([str(fq) for fq in frequencies], dtype="S1")
            if "listOfFrequencies" in id_grp:
                del id_grp["listOfFrequencies"]
            lof = id_grp.create_dataset(
                "listOfFrequencies",
                data=vals,
                dtype=(src_lof.dtype if src_lof is not None else vals.dtype))
            if src_lof is not None:
                _copy_attrs(src_lof, lof)
            if verbose:
                print(f"    listOfFrequencies -> {[str(q) for q in frequencies]}",
                      flush=True)
        except Exception as e:
            if verbose:
                print(f"    Warning: listOfFrequencies update failed: {e}",
                      flush=True)

        # Update zeroDopplerStartTime / EndTime
        from datetime import datetime, timedelta
        try:
            orig = _decode_h5_scalar(id_grp["zeroDopplerStartTime"][()])
            base = datetime.strptime(orig.split(".")[0], "%Y-%m-%dT%H:%M:%S")
            base = base.replace(hour=0, minute=0, second=0, microsecond=0)
            s0 = (base + timedelta(seconds=zd_lo)).strftime("%Y-%m-%dT%H:%M:%S.%f")
            s1 = (base + timedelta(seconds=zd_hi)).strftime("%Y-%m-%dT%H:%M:%S.%f")
            del id_grp["zeroDopplerStartTime"]
            id_grp.create_dataset("zeroDopplerStartTime", data=np.bytes_(s0))
            del id_grp["zeroDopplerEndTime"]
            id_grp.create_dataset("zeroDopplerEndTime", data=np.bytes_(s1))
            if verbose:
                print(f"    Updated times: {s0} .. {s1}", flush=True)
        except Exception as e:
            if verbose:
                print(f"    Warning: time update failed: {e}", flush=True)

        # Update boundingPolygon from radar geometry with per-corner terrain heights
        try:
            from openseppo.nisar.radar_geometry import (
                OrbitInterpolator, rdr2geo_corners, rdr2geo_perimeter,
                perimeter_to_wkt)
            orb_p = f"{_META}/orbit"
            orbit = OrbitInterpolator(
                src_f[f"{orb_p}/time"][:],
                src_f[f"{orb_p}/position"][:],
                src_f[f"{orb_p}/velocity"][:])
            look = -1  # default
            ld = f"{_ID}/lookDirection"
            if ld in src_f:
                if _decode_h5_scalar(src_f[ld][()]).strip().lower().startswith("l"):
                    look = -1
                else:
                    look = 1
            sub_zd = src_f[f"{_SW}/zeroDopplerTime"][az_off:az_end]
            fq0 = frequencies[0]
            ro0, rn0 = per_freq_rg[fq0]
            sub_sr = src_f[f"{_SW}/frequency{fq0}/slantRange"][ro0:ro0+rn0]

            # First pass at h=0 to get approximate corner locations
            corners_h0 = rdr2geo_corners(orbit, sub_zd, sub_sr, look_side=look)
            if corners_h0:
                # The run's height, resolved once.  In -srcwin/-coordwin mode
                # there was no bbox to resolve from earlier, so these corners
                # are the first geometry available -- and the result is then
                # reused by every later granule in the run.
                resolve_heights(height_ctx, points=corners_h0, verbose=verbose)
                if verbose:
                    labels = ["near-early", "far-early", "far-late", "near-late"]
                    for lbl, (lon, lat) in zip(labels, corners_h0):
                        print(f"      {lbl}: ({lon:.4f}, {lat:.4f})", flush=True)
                # Second pass: densified perimeter at one representative height.
                # A single height avoids asserting a terrain profile along each
                # edge; see rdr2geo_perimeter for why that matters.
                mean_h = float(height_ctx["polygon"])
                ring = rdr2geo_perimeter(orbit, sub_zd, sub_sr,
                                         look_side=look, height=mean_h)
            else:
                ring, mean_h = None, 0.0

            if ring:
                del id_grp["boundingPolygon"]
                bp = id_grp.create_dataset(
                    "boundingPolygon", data=np.bytes_(perimeter_to_wkt(ring)))
                # Record the heights the subset was built with, and where they
                # came from.  Height moves lon/lat by roughly 1/tan(incidence),
                # so without this a stack cannot be checked for a common
                # geometry, and a subset cannot be reproduced later.
                bp.attrs["subset_terrain_height_meters"] = float(mean_h)
                bp.attrs["subset_terrain_height_source"] = np.bytes_(
                    str(height_ctx["source"] or "default"))
                bp.attrs["subset_window_max_height_meters"] = float(
                    height_ctx["max"] if height_ctx["max"] is not None else 1000.0)
                bp.attrs["subset_window_min_height_meters"] = float(
                    height_ctx["min"] if height_ctx["min"] is not None else 0.0)
                if verbose:
                    print(f"    Updated boundingPolygon (rdr2geo, {len(ring)} pts, "
                          f"h={mean_h:.0f}m, source={height_ctx['source']})",
                          flush=True)
        except Exception as e:
            if verbose:
                print(f"    Warning: boundingPolygon update failed: {e}",
                      flush=True)

        _mark("identification + boundingPolygon")

        # ============================================================
        # /science/LSAR/RSLC/metadata/orbit  (copy verbatim, small)
        # ============================================================
        _copy_group_all(src_f, f"{_META}/orbit",
                        dst.require_group("science/LSAR/RSLC/metadata"), "orbit")

        # ============================================================
        # /science/LSAR/RSLC/metadata/attitude  (copy verbatim, small)
        # ============================================================
        _copy_group_all(src_f, f"{_META}/attitude",
                        dst["science/LSAR/RSLC/metadata"], "attitude")

        if verbose:
            print(f"    Copied orbit + attitude", flush=True)

        _mark("orbit + attitude")

        # ============================================================
        # /science/LSAR/RSLC/metadata/processingInformation
        # ============================================================
        pi = f"{_META}/processingInformation"
        pi_dst = dst.require_group(pi.lstrip("/"))

        # algorithms + inputs: copy verbatim (small strings)
        _copy_group_all(src_f, f"{pi}/algorithms", pi_dst, "algorithms")
        _copy_group_all(src_f, f"{pi}/inputs", pi_dst, "inputs")

        # parameters: has zeroDopplerTime/slantRange axes -> subset
        pp = f"{pi}/parameters"
        pp_dst = pi_dst.require_group("parameters")
        _copy_attrs(src_f[pp], pp_dst)

        # Shared parameter datasets
        pp_zd = src_f[f"{pp}/zeroDopplerTime"][:]
        pp_sr = src_f[f"{pp}/slantRange"][:]
        zd_i0, zd_i1 = _slice_range(pp_zd, zd_lo, zd_hi)
        sr_i0, sr_i1 = _slice_range(pp_sr, sr_lo, sr_hi)

        _create_ds(src_f, f"{pp}/zeroDopplerTime", pp_dst,
                   "zeroDopplerTime", pp_zd[zd_i0:zd_i1])
        _create_ds(src_f, f"{pp}/slantRange", pp_dst,
                   "slantRange", pp_sr[sr_i0:sr_i1])

        for ds_name in ("rangeChirpWeighting", "azimuthChirpWeighting",
                        "runConfigurationContents"):
            p = f"{pp}/{ds_name}"
            if p in src_f:
                _copy_ds(src_f, p, pp_dst)

        # referenceTerrainHeight: indexed by zeroDopplerTime
        rth = f"{pp}/referenceTerrainHeight"
        if rth in src_f:
            _create_ds(src_f, rth, pp_dst,
                       "referenceTerrainHeight", src_f[rth][zd_i0:zd_i1])

        # Per-frequency dopplerCentroid
        for fq in frequencies:
            pfq = f"{pp}/frequency{fq}"
            if pfq not in src_f:
                continue
            pfq_dst = pp_dst.require_group(f"frequency{fq}")
            _copy_attrs(src_f[pfq], pfq_dst)
            for coord in ("zeroDopplerTime", "slantRange"):
                cp = f"{pfq}/{coord}"
                if cp in src_f:
                    arr = src_f[cp][:]
                    if coord == "zeroDopplerTime":
                        ci0, ci1 = _slice_range(arr, zd_lo, zd_hi)
                    else:
                        ci0, ci1 = _slice_range(arr, sr_lo, sr_hi)
                    _create_ds(src_f, cp, pfq_dst, coord, arr[ci0:ci1])
            dc = f"{pfq}/dopplerCentroid"
            if dc in src_f:
                dc_zd = src_f[f"{pfq}/zeroDopplerTime"][:]
                dc_sr = src_f[f"{pfq}/slantRange"][:]
                dz0, dz1 = _slice_range(dc_zd, zd_lo, zd_hi)
                ds0, ds1 = _slice_range(dc_sr, sr_lo, sr_hi)
                _create_ds(src_f, dc, pfq_dst,
                           "dopplerCentroid", src_f[dc][dz0:dz1, ds0:ds1])
                if verbose:
                    orig_sh = src_f[dc].shape
                    new_sh = (dz1-dz0, ds1-ds0)
                    print(f"    dopplerCentroid freq{fq}: {orig_sh} -> {new_sh}",
                          flush=True)

        if verbose:
            print(f"    Copied processingInformation", flush=True)

        _mark("processingInformation")

        # ============================================================
        # /science/LSAR/RSLC/metadata/calibrationInformation
        # ============================================================
        cal = f"{_META}/calibrationInformation"
        cal_dst = dst.require_group(cal.lstrip("/"))
        _copy_attrs(src_f[cal], cal_dst)

        # crosstalk: copy verbatim (small 1-D arrays)
        _copy_group_all(src_f, f"{cal}/crosstalk", cal_dst, "crosstalk")

        # geometry: has zeroDopplerTime/slantRange -> subset
        cg = f"{cal}/geometry"
        if cg in src_f:
            cg_dst = cal_dst.require_group("geometry")
            _copy_attrs(src_f[cg], cg_dst)
            cg_zd = src_f[f"{cg}/zeroDopplerTime"][:]
            cg_sr = src_f[f"{cg}/slantRange"][:]
            gz0, gz1 = _slice_range(cg_zd, zd_lo, zd_hi)
            gs0, gs1 = _slice_range(cg_sr, sr_lo, sr_hi)
            _create_ds(src_f, f"{cg}/zeroDopplerTime", cg_dst,
                       "zeroDopplerTime", cg_zd[gz0:gz1])
            _create_ds(src_f, f"{cg}/slantRange", cg_dst,
                       "slantRange", cg_sr[gs0:gs1])
            for ds_name in ("beta0", "sigma0", "gamma0"):
                p = f"{cg}/{ds_name}"
                if p in src_f:
                    _create_ds(src_f, p, cg_dst, ds_name,
                               src_f[p][gz0:gz1, gs0:gs1])

        # Per-frequency calibration
        for fq in frequencies:
            cfq = f"{cal}/frequency{fq}"
            if cfq not in src_f:
                continue
            cfq_dst = cal_dst.require_group(f"frequency{fq}")
            _copy_attrs(src_f[cfq], cfq_dst)

            # Scalars: commonDelay, faradayRotation
            for sc in ("commonDelay", "faradayRotation"):
                p = f"{cfq}/{sc}"
                if p in src_f:
                    _copy_ds(src_f, p, cfq_dst)

            # Per-pol scalars: only for requested pols
            for pol in sorted(src_f[cfq].keys()):
                pol_path = f"{cfq}/{pol}"
                obj = src_f[pol_path]
                if not isinstance(obj, h5py.Group):
                    continue
                if len(pol) == 2 and pol.isupper():
                    # Only copy requested pols
                    if pol not in all_pols:
                        if verbose:
                            print(f"    Skipping cal {fq}/{pol}", flush=True)
                        continue
                    _copy_group_all(src_f, pol_path, cfq_dst, pol)

            # elevationAntennaPattern: has zeroDopplerTime/slantRange
            eap = f"{cfq}/elevationAntennaPattern"
            if eap in src_f:
                eap_dst = cfq_dst.require_group("elevationAntennaPattern")
                _copy_attrs(src_f[eap], eap_dst)
                eap_zd = src_f[f"{eap}/zeroDopplerTime"][:]
                eap_sr = src_f[f"{eap}/slantRange"][:]
                ez0, ez1 = _slice_range(eap_zd, zd_lo, zd_hi)
                es0, es1 = _slice_range(eap_sr, sr_lo, sr_hi)
                _create_ds(src_f, f"{eap}/zeroDopplerTime", eap_dst,
                           "zeroDopplerTime", eap_zd[ez0:ez1])
                _create_ds(src_f, f"{eap}/slantRange", eap_dst,
                           "slantRange", eap_sr[es0:es1])
                # Antenna pattern arrays per pol
                for pol in sorted(src_f[eap].keys()):
                    if pol in ("zeroDopplerTime", "slantRange"):
                        continue
                    p = f"{eap}/{pol}"
                    if isinstance(src_f[p], h5py.Dataset) and len(src_f[p].shape) == 2:
                        _create_ds(src_f, p, eap_dst, pol,
                                   src_f[p][ez0:ez1, es0:es1])
                        if verbose:
                            print(f"    EAP {fq}/{pol}: {src_f[p].shape} -> "
                                  f"{(ez1-ez0, es1-es0)}", flush=True)

            # noiseEquivalentBackscatter: small, has slantRange axis
            neb = f"{cfq}/noiseEquivalentBackscatter"
            if neb in src_f:
                neb_dst = cfq_dst.require_group("noiseEquivalentBackscatter")
                _copy_attrs(src_f[neb], neb_dst)
                for ds_name in sorted(src_f[neb].keys()):
                    p = f"{neb}/{ds_name}"
                    ds = src_f[p]
                    if isinstance(ds, h5py.Dataset):
                        # Only copy pols that are requested
                        if (len(ds_name) == 2 and ds_name.isupper()
                                and ds_name not in all_pols):
                            continue
                        _copy_ds(src_f, p, neb_dst)

        if verbose:
            print(f"    Copied calibrationInformation", flush=True)

        _mark("calibrationInformation")

        # ============================================================
        # /science/LSAR/RSLC/metadata/geolocationGrid  (subset)
        # ============================================================
        geo = f"{_META}/geolocationGrid"
        geo_dst = dst.require_group(geo.lstrip("/"))
        _copy_attrs(src_f[geo], geo_dst)

        geo_zd = src_f[f"{geo}/zeroDopplerTime"][:]
        geo_sr = src_f[f"{geo}/slantRange"][:]
        gz0, gz1 = _slice_range(geo_zd, zd_lo, zd_hi)
        gs0, gs1 = _slice_range(geo_sr, sr_lo, sr_hi)

        _create_ds(src_f, f"{geo}/zeroDopplerTime", geo_dst,
                   "zeroDopplerTime", geo_zd[gz0:gz1])
        _create_ds(src_f, f"{geo}/slantRange", geo_dst,
                   "slantRange", geo_sr[gs0:gs1])
        _copy_ds(src_f, f"{geo}/epsg", geo_dst)
        _copy_ds(src_f, f"{geo}/heightAboveEllipsoid", geo_dst)

        for ds_name in ("coordinateX", "coordinateY",
                        "alongTrackUnitVectorX", "alongTrackUnitVectorY",
                        "elevationAngle", "groundTrackVelocity",
                        "incidenceAngle", "losUnitVectorX", "losUnitVectorY"):
            p = f"{geo}/{ds_name}"
            if p in src_f:
                data = src_f[p][:, gz0:gz1, gs0:gs1]
                _create_ds(src_f, p, geo_dst, ds_name, data)
                if verbose:
                    print(f"    geoGrid {ds_name}: {src_f[p].shape} -> "
                          f"{data.shape}", flush=True)

        _mark("geolocationGrid")

        # ============================================================
        # /science/LSAR/RSLC/swaths  (subset SLC data)
        # ============================================================
        sw_dst = dst.require_group(_SW.lstrip("/"))
        _copy_attrs(src_f[_SW], sw_dst)

        # Shared zeroDopplerTime (subsetted)
        sw_dst.create_dataset("zeroDopplerTime",
                              data=src_f[f"{_SW}/zeroDopplerTime"][az_off:az_end])
        _copy_attrs(src_f[f"{_SW}/zeroDopplerTime"], sw_dst["zeroDopplerTime"])

        # zeroDopplerTimeSpacing (scalar, verbatim)
        zdts = f"{_SW}/zeroDopplerTimeSpacing"
        if zdts in src_f:
            _copy_ds(src_f, zdts, sw_dst)

        # Per-frequency swaths
        for fq in frequencies:
            rg_off, rg_count = per_freq_rg[fq]
            rg_end = rg_off + rg_count
            n_rg_orig = src_f[f"{_SW}/frequency{fq}/slantRange"].shape[0]

            sw_fq = f"{_SW}/frequency{fq}"
            fq_dst = sw_dst.require_group(f"frequency{fq}")
            _copy_attrs(src_f[sw_fq], fq_dst)

            # slantRange (subsetted)
            fq_dst.create_dataset(
                "slantRange",
                data=src_f[f"{sw_fq}/slantRange"][rg_off:rg_end])
            _copy_attrs(src_f[f"{sw_fq}/slantRange"], fq_dst["slantRange"])

            if verbose:
                sr = fq_dst["slantRange"]
                print(f"    Freq {fq}: slantRange [{sr[0]:.2f}, {sr[-1]:.2f}] m "
                      f"({rg_count} samples)", flush=True)

            # Scalar metadata
            for sc in ("slantRangeSpacing", "processedCenterFrequency",
                       "acquiredCenterFrequency", "acquiredRangeBandwidth",
                       "nominalAcquisitionPRF", "processedAzimuthBandwidth",
                       "processedRangeBandwidth", "sceneCenterAlongTrackSpacing",
                       "sceneCenterGroundRangeSpacing", "numberOfSubSwaths"):
                p = f"{sw_fq}/{sc}"
                if p in src_f:
                    _copy_ds(src_f, p, fq_dst)

            # listOfPolarizations (update to match requested vars)
            pols = sorted(var_by_freq.get(fq, []))
            if pols:
                fq_dst.create_dataset(
                    "listOfPolarizations",
                    data=np.array(pols, dtype=f"S{max(len(p) for p in pols)}"))
                lop_src = f"{sw_fq}/listOfPolarizations"
                if lop_src in src_f:
                    _copy_attrs(src_f[lop_src], fq_dst["listOfPolarizations"])

            # validSamplesSubSwathN (subset rows, adjust columns)
            for item in sorted(src_f[sw_fq].keys()):
                if not item.startswith("validSamplesSubSwath"):
                    continue
                p = f"{sw_fq}/{item}"
                ds = src_f[p]
                if len(ds.shape) != 2:
                    continue
                vs = ds[az_off:az_end, :].copy()
                # Adjust range indices (keep original dtype)
                orig_dtype = ds.dtype
                vs_i = vs.astype(np.int64)
                # validSamples uses inclusive indices [first_valid, last_valid]
                # Preserve [0,0] convention for lines with no valid data
                for c in range(0, vs_i.shape[1] - 1, 2):
                    first = vs_i[:, c]
                    last = vs_i[:, c + 1]
                    # Mark lines with no overlap as invalid [0,0]
                    no_data = (first == 0) & (last == 0)  # original no-data
                    no_overlap = (first >= rg_end) | (last < rg_off)
                    invalid = no_data | no_overlap
                    # Clip and shift valid lines
                    vs_i[:, c] = np.clip(first, rg_off, rg_end - 1) - rg_off
                    vs_i[:, c+1] = np.clip(last, rg_off, rg_end - 1) - rg_off
                    # Reset invalid lines
                    vs_i[invalid, c] = 0
                    vs_i[invalid, c+1] = 0
                vs = vs_i.astype(orig_dtype)
                # Match original: uncompressed, contiguous (no chunks)
                d = fq_dst.create_dataset(item, data=vs)
                _copy_attrs(ds, d)
                if verbose:
                    print(f"    {item}: {ds.shape} -> {vs.shape}", flush=True)

            # Grid-borne swath arrays not covered above -- the per-pixel
            # inputDataExceptionMask, and anything a later product version
            # posts beside it.  They sit on the same (azimuth, range) grid as
            # the SLC payload and take the same window.  The explicit lists
            # above never named the mask, so every subset silently dropped
            # the processor's per-sample record of input-data anomalies.
            _pol_names = {k for k in src_f[sw_fq]
                          if len(k) == 2 and k.isupper()}
            for item in sorted(src_f[sw_fq].keys()):
                if item in fq_dst or item in _pol_names:
                    continue
                ds = src_f[f"{sw_fq}/{item}"]
                if not isinstance(ds, h5py.Dataset):
                    continue
                if ds.shape == (n_az_orig, n_rg_orig):
                    _chunks = ds.chunks or (512, 512)
                    _c, _o, _sh = _deflate_opts(ds, complevel)
                    _create_ds(
                        src_f, f"{sw_fq}/{item}", fq_dst, item,
                        ds[az_off:az_end, rg_off:rg_end],
                        chunks=(min(_chunks[0], az_count),
                                min(_chunks[1], rg_count)),
                        compression=_c, compression_opts=_o, shuffle=_sh)
                    if verbose:
                        print(f"    {item}: {ds.shape} -> "
                              f"({az_count}, {rg_count})", flush=True)
                elif ds.ndim == 0:
                    _copy_ds(src_f, f"{sw_fq}/{item}", fq_dst)
                elif ds.ndim == 1 and ds.shape[0] == n_az_orig:
                    _copy_ds(src_f, f"{sw_fq}/{item}", fq_dst,
                             data=ds[az_off:az_end])
                elif ds.ndim == 1 and ds.shape[0] == n_rg_orig:
                    _copy_ds(src_f, f"{sw_fq}/{item}", fq_dst,
                             data=ds[rg_off:rg_end])
                else:
                    # Copying verbatim would drag the full frame into the
                    # subset, so omit it and say so.
                    print(f"    [WARN] {sw_fq}/{item} has shape {ds.shape}, "
                          f"which does not match this frequency's swath "
                          f"{(n_az_orig, n_rg_orig)}; omitted from the "
                          f"subset.", flush=True)

            _mark("swath ancillary (scalars, validSamples, masks)")

            # SLC polarisation data (the main payload)
            for pol in pols:
                p = f"{sw_fq}/{pol}"
                if p not in src_f:
                    continue
                if verbose:
                    _tp = _t.perf_counter()
                    print(f"    Reading {fq}/{pol} [{az_off}:{az_end}, "
                          f"{rg_off}:{rg_end}] ...", flush=True)

                slc_data = _read_window(
                    src_f, p, az_off, az_end, rg_off, rg_end,
                    pool=_pool, workers=read_workers, file_url=file_url,
                    s3_creds=_s3_creds, use_earthdata=use_earthdata,
                    verbose=verbose)
                if verbose:
                    _tr = _t.perf_counter()
                # Match original NISAR chunk size (512, 512)
                src_ds = src_f[p]
                src_chunks = src_ds.chunks or (512, 512)
                ch_az = min(src_chunks[0], az_count)
                ch_rg = min(src_chunks[1], rg_count)
                _comp, _opts, _shuf = _deflate_opts(src_ds, complevel)
                d = fq_dst.create_dataset(
                    pol, data=slc_data,
                    chunks=(ch_az, ch_rg),
                    compression=_comp, compression_opts=_opts,
                    shuffle=_shuf)
                _copy_attrs(src_f[p], d)

                if verbose:
                    mb = slc_data.nbytes / 1e6
                    _now = _t.perf_counter()
                    # Split so the two costs are separable: the read is what
                    # the parallel workers act on, the write is single-process
                    # gzip and unaffected by them.
                    print(f"    [t] {pol}: {_now - _tp:.1f}s "
                          f"(read {_tr - _tp:.1f}s + write {_now - _tr:.1f}s, "
                          f"{mb:.1f} MB)", flush=True)
                del slc_data
                gc.collect()

    if _pool is not None:
        _pool.shutdown()

    if verbose:
        sz = os.path.getsize(dst_path) / 1e6
        print(f"    Output: {dst_path} ({sz:.1f} MB)", flush=True)
        print(f"    [t] total subset: {_t.perf_counter()-t0:.1f}s", flush=True)


# =========================================================
# 5. SINGLE-FILE PROCESSOR
# =========================================================

def _process_single_file(h5_url, variable_names, output_dir,
                          srcwin, coordwin, projwin, projwin_srs, frequency,
                          input_fs, output_fs,
                          cache=None, keep=False, use_earthdata=False,
                          verbose=False, all_frequencies=False,
                          max_height=None, min_height=None, height_ctx=None,
                          read_workers=8, complevel=None):
    import time as _time
    h5_basename = h5_url.split("/")[-1]
    base_name = (h5_basename[:-3] if h5_basename.lower().endswith(".h5")
                 else h5_basename)
    if verbose:
        print(f"\n--> Processing RSLC: {h5_basename}", flush=True)
        _t0 = _time.perf_counter()

    cached_file = None
    try:
        # Cache if needed
        if cache is not None:
            file_url = cache_to_local(
                h5_url, localdir=cache, keep=keep,
                use_earthdata=use_earthdata, fs=input_fs)
            if not keep:
                cached_file = file_url
        else:
            file_url = h5_url

        f = open_h5_lazy(file_url, input_fs)

        # Determine frequencies
        if all_frequencies:
            frequencies = [k.replace("frequency", "")
                           for k in sorted(f[_SW].keys())
                           if k.startswith("frequency")]
        else:
            frequencies = [frequency]

        # Determine variables per frequency
        var_by_freq = {}
        for fq in frequencies:
            sw = f"{_SW}/frequency{fq}"
            if sw not in f:
                continue
            if variable_names:
                var_by_freq[fq] = [v for v in variable_names
                                   if f"{sw}/{v}" in f]
            else:
                var_by_freq[fq] = [n for n in sorted(f[sw].keys())
                                   if (isinstance(f[f"{sw}/{n}"], h5py.Dataset)
                                       and len(f[f"{sw}/{n}"].shape) == 2
                                       and len(n) == 2 and n.isupper())]
        if not var_by_freq:
            f.close()
            return {"success": False, "h5_url": h5_url,
                    "error": "No variables found."}

        # -projwin coordinates arrive in -projwin_srs, but the geolocationGrid
        # lookup below reads lon/lat, so anything else has to be converted
        # first.  projwin_srs was accepted and then ignored, so a UTM or polar
        # bbox was taken as degrees: the lookup found no overlap and the file
        # was skipped, or -- for coordinates that happen to fall in [-180, 90]
        # -- it silently subsetted the wrong ground.
        if projwin and projwin_srs:
            _psrs = str(projwin_srs).strip()
            if _psrs.isdigit():
                _psrs = f"EPSG:{_psrs}"
            if _psrs.upper() != "EPSG:4326":
                from openseppo.nisar.nisar_tools import reproject_projwin
                projwin = reproject_projwin(projwin, _psrs, "EPSG:4326")
                if verbose:
                    print(f"    projwin_srs {_psrs} -> EPSG:4326: "
                          f"({projwin[0]:.6f}, {projwin[1]:.6f}, "
                          f"{projwin[2]:.6f}, {projwin[3]:.6f})", flush=True)

        if height_ctx is None:
            height_ctx = new_height_context(max_height, min_height)
        if projwin:
            # Resolve from the requested box -- its corners and centre -- so a
            # batch run queries once for the whole stack rather than once per
            # granule.
            _ulx, _uly, _lrx, _lry = projwin
            _x0, _x1 = min(_ulx, _lrx), max(_ulx, _lrx)
            _y0, _y1 = min(_uly, _lry), max(_uly, _lry)
            resolve_heights(height_ctx, points=[
                (_x0, _y0), (_x1, _y0), (_x0, _y1), (_x1, _y1),
                ((_x0 + _x1) / 2, (_y0 + _y1) / 2)], verbose=verbose)

        # Shared azimuth info
        slc_zd = f[f"{_SW}/zeroDopplerTime"][:]
        n_az = len(slc_zd)

        # Compute azimuth window
        if srcwin:
            az_off, _, az_size, _ = srcwin
        elif coordwin:
            _, _, zd_s, zd_e = coordwin
            idx = np.where((slc_zd >= zd_s) & (slc_zd <= zd_e))[0]
            if len(idx) == 0:
                f.close()
                return {"success": False, "h5_url": h5_url,
                        "error": f"No azimuth lines in zd [{zd_s}, {zd_e}]"}
            az_off, az_size = int(idx[0]), int(idx[-1] - idx[0] + 1)
        elif projwin:
            ulx, uly, lrx, lry = projwin
            primary = list(var_by_freq.keys())[0]
            az_off, az_size, _, _ = _bbox_to_pixels(
                f, min(ulx, lrx), min(uly, lry), max(ulx, lrx), max(uly, lry),
                freq=primary, max_height=height_ctx["max"], verbose=verbose)
        else:
            az_off, az_size = 0, n_az

        az_off = max(0, az_off)
        az_size = min(az_size, n_az - az_off)
        if az_size <= 0:
            f.close()
            return {"success": False, "h5_url": h5_url,
                    "error": "Azimuth subset empty."}

        # Per-frequency range windows
        per_freq_rg = {}
        for fq in list(var_by_freq.keys()):
            slc_sr = f[f"{_SW}/frequency{fq}/slantRange"][:]
            n_rg = len(slc_sr)
            if srcwin:
                _, rg_off, _, rg_size = srcwin
            elif coordwin:
                sr_s, sr_e, _, _ = coordwin
                idx = np.where((slc_sr >= sr_s) & (slc_sr <= sr_e))[0]
                if len(idx) == 0:
                    del var_by_freq[fq]; continue
                rg_off, rg_size = int(idx[0]), int(idx[-1] - idx[0] + 1)
            elif projwin:
                _, _, rg_off, rg_size = _bbox_to_pixels(
                    f, min(ulx, lrx), min(uly, lry),
                    max(ulx, lrx), max(uly, lry), freq=fq,
                    max_height=height_ctx["max"], verbose=verbose)
            else:
                rg_off, rg_size = 0, n_rg
            rg_off = max(0, rg_off)
            rg_size = min(rg_size, n_rg - rg_off)
            if rg_size <= 0:
                del var_by_freq[fq]; continue
            per_freq_rg[fq] = (rg_off, rg_size)

        if not var_by_freq:
            f.close()
            return {"success": False, "h5_url": h5_url,
                    "error": "All range subsets empty."}

        # Validate srcwin with mixed-freq dimensions
        if srcwin and len(per_freq_rg) > 1:
            dims = {fq: f[f"{_SW}/frequency{fq}/slantRange"].shape[0]
                    for fq in per_freq_rg}
            if len(set(dims.values())) > 1:
                f.close()
                return {"success": False, "h5_url": h5_url,
                        "error": f"-srcwin ambiguous with mixed range dims {dims}. "
                                 f"Use -coordwin or -projwin."}

        acq_meta = {}
        try:
            ts = _decode_h5_scalar(f[f"{_ID}/zeroDopplerStartTime"][()])
            if "T" in ts:
                d, t = ts.split("T")
                acq_meta["ACQUISITION_DATE"] = d
        except Exception:
            pass

        if verbose:
            for fq in var_by_freq:
                ro, rn = per_freq_rg[fq]
                print(f"    Freq {fq}: az=[{az_off}:{az_off+az_size}] "
                      f"rg=[{ro}:{ro+rn}] vars={var_by_freq[fq]}", flush=True)

        # Output path
        primary = list(var_by_freq.keys())[0]
        pr = per_freq_rg[primary]
        tag = (f"_subset_az{az_off}-{az_off+az_size}"
               f"_rg{pr[0]}-{pr[0]+pr[1]}"
               if srcwin or coordwin or projwin else "")
        out_name = f"{base_name}{tag}.h5"

        if output_dir.startswith("s3://"):
            fd, local_out = tempfile.mkstemp(suffix=".h5"); os.close(fd)
        else:
            os.makedirs(output_dir, exist_ok=True)
            local_out = os.path.join(output_dir, out_name)

        # === SUBSET ===
        _subset_rslc(f, local_out, list(var_by_freq.keys()), var_by_freq,
                     az_off, az_size, per_freq_rg, verbose=verbose,
                     max_height=max_height, min_height=min_height,
                     height_ctx=height_ctx, file_url=file_url,
                     input_fs=input_fs, use_earthdata=use_earthdata,
                     read_workers=read_workers, complevel=complevel)

        f.close()

        # Upload to S3
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
                           "rg_off": pr[0], "rg_size": pr[1]}}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "h5_url": h5_url, "error": str(e)}
    finally:
        if cached_file and os.path.exists(cached_file):
            try:
                os.unlink(cached_file)
            except OSError:
                pass


# =========================================================
# 6. QUICKLOOK
# =========================================================

def generate_quicklook(h5_path, multilook=5, verbose=False):
    """
    Generate a backscatter quicklook PNG from a subsetted RSLC HDF5.

    Shows detected sigma0 (single-look + multilooked) for each
    polarisation, with calibration applied where available.

    Returns the output PNG path.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.ndimage import uniform_filter
    from scipy.interpolate import RegularGridInterpolator

    f = h5py.File(h5_path, "r")

    cal = f"{_META}/calibrationInformation"
    slc_zd = f[f"{_SW}/zeroDopplerTime"][:]

    # Collect all SLC panels
    panels = []
    for fk in sorted(f[_SW].keys()):
        if not fk.startswith("frequency"):
            continue
        fc = fk.replace("frequency", "")
        slc_sr = f[f"{_SW}/{fk}/slantRange"][:]

        for pol in sorted(f[f"{_SW}/{fk}"].keys()):
            p = f"{_SW}/{fk}/{pol}"
            obj = f[p]
            if not (isinstance(obj, h5py.Dataset) and len(obj.shape) == 2
                    and len(pol) == 2 and pol.isupper()):
                continue

            slc = obj[:]
            pwr = np.abs(slc).astype(np.float64) ** 2

            # Apply scaleFactor if available
            sf_path = f"{cal}/frequency{fc}/{pol}/scaleFactor"
            if sf_path in f:
                sf = float(f[sf_path][()])
                pwr *= sf * sf

            # Apply sigma0 area factor if available
            cg = f"{cal}/geometry"
            if f"{cg}/sigma0" in f and f"{cg}/zeroDopplerTime" in f:
                cg_zd = f[f"{cg}/zeroDopplerTime"][:]
                cg_sr = f[f"{cg}/slantRange"][:]
                s0_grid = f[f"{cg}/sigma0"][:].astype(np.float64)
                if len(cg_zd) >= 2 and len(cg_sr) >= 2:
                    interp = RegularGridInterpolator(
                        (cg_zd, cg_sr), s0_grid,
                        method="linear", bounds_error=False,
                        fill_value=None)
                    zg, sg = np.meshgrid(slc_zd, slc_sr, indexing="ij")
                    area = interp((zg, sg))
                    pwr /= area

            pwr[pwr <= 0] = np.nan
            db = 10 * np.log10(pwr)

            # Multilook
            ml = uniform_filter(pwr, size=multilook)
            ml[ml <= 0] = np.nan
            ml_db = 10 * np.log10(ml)

            panels.append((f"{fc}/{pol}", db, ml_db))

    n = len(panels)
    fig, axes = plt.subplots(n, 2, figsize=(10, 5 * n), squeeze=False)

    for i, (label, db, ml_db) in enumerate(panels):
        vmin = np.nanpercentile(ml_db, 2)
        vmax = np.nanpercentile(ml_db, 98)

        ax1 = axes[i, 0]
        im1 = ax1.imshow(db, aspect="auto", cmap="gray",
                          vmin=vmin, vmax=vmax, interpolation="nearest")
        ax1.set_title(f"{label} single-look", fontsize=11)
        ax1.set_xlabel("Range")
        ax1.set_ylabel("Azimuth")
        plt.colorbar(im1, ax=ax1, shrink=0.6, label="dB")

        ax2 = axes[i, 1]
        im2 = ax2.imshow(ml_db, aspect="auto", cmap="gray",
                          vmin=vmin, vmax=vmax, interpolation="nearest")
        ax2.set_title(f"{label} {multilook}x{multilook} multilook", fontsize=11)
        ax2.set_xlabel("Range")
        ax2.set_ylabel("Azimuth")
        plt.colorbar(im2, ax=ax2, shrink=0.6, label="dB")

    # Title
    ident = _ID
    parts = []
    try:
        parts.append(f[f"{ident}/zeroDopplerStartTime"][()].decode().split(".")[0])
    except Exception:
        pass
    try:
        parts.append(f"Track {int(f[f'{ident}/trackNumber'][()])}")
    except Exception:
        pass
    try:
        parts.append(f"Frame {int(f[f'{ident}/frameNumber'][()])}")
    except Exception:
        pass
    shape_str = f"{slc_zd.shape[0]} az x {panels[0][1].shape[1]} rg"
    fig.suptitle(f"RSLC Quicklook  |  {' | '.join(parts)}\n{shape_str}",
                 fontsize=10, y=1.0)

    plt.tight_layout()
    out_png = h5_path.replace(".h5", "_quicklook.png")
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    f.close()

    if verbose:
        print(f"    Quicklook: {out_png}", flush=True)
    return out_png


# =========================================================
# 7. BATCH ENTRY POINT
# =========================================================

def process_rslc_subset(h5_url, variable_names, output_path,
                         srcwin=None, coordwin=None,
                         projwin=None, projwin_srs=None,
                         frequency="A", input_auth=None, output_auth=None,
                         list_grids=False, cache=None, keep=False,
                         verbose=False, all_frequencies=False,
                         quicklook=False, ql_multilook=5,
                         max_height=None, min_height=None, read_workers=8,
                         complevel=None):
    """Batch entry point for RSLC subsetting."""
    use_earthdata = False
    if input_auth is None:
        input_auth = {"use_earthdata": False}
    if "use_earthdata" in input_auth:
        use_earthdata = input_auth["use_earthdata"]
    if output_auth is None:
        output_auth = {}
    urls = h5_url if isinstance(h5_url, list) else [h5_url]

    if use_earthdata and HAS_EARTHACCESS:
        _earthaccess_login(verbose=verbose)

    try:
        _https_ea = use_earthdata and urls and urls[0].startswith("https://")
        input_fs = None if _https_ea else create_s3_fs(input_auth)

        # --- INSPECT MODE ---
        if list_grids:
            f = open_h5_lazy(urls[0], input_fs)
            print(inspect_rslc(f))
            f.close()
            return "Inspection Complete."

        # --- PROCESSING ---
        output_fs = None
        if output_path.startswith("s3://"):
            output_fs = create_s3_fs(output_auth)

        # One height context for the run: resolved by the first granule that
        # has geometry, then reused, so every date in a stack is subsetted
        # against the same terrain assumption.
        height_ctx = new_height_context(max_height, min_height)

        results = []
        for url in urls:
            res = _process_single_file(
                url, variable_names, output_path,
                srcwin, coordwin, projwin, projwin_srs,
                frequency, input_fs, output_fs,
                cache=cache, keep=keep,
                use_earthdata=use_earthdata,
                verbose=verbose, all_frequencies=all_frequencies,
                max_height=max_height, min_height=min_height,
                height_ctx=height_ctx, read_workers=read_workers,
                complevel=complevel)
            results.append(res)
            if res["success"]:
                print(f"  [OK] {res['output']}")
                if quicklook and not res["output"].startswith("s3://"):
                    ql_path = generate_quicklook(
                        res["output"], multilook=ql_multilook, verbose=verbose)
                    print(f"  [QL] {ql_path}")
            else:
                print(f"  [FAIL] {res['h5_url']}: {res.get('error', '?')}")

        return f"Processed {sum(1 for r in results if r['success'])}/{len(results)} files."

    except Exception as e:
        import traceback; traceback.print_exc()
        return f"Critical Error: {str(e)}"
