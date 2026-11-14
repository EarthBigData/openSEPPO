#!/usr/bin/env python
"""
seppo_nisar_search_S -- NISAR S-band catalog search via ISRO Bhoonidhi
**********************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Author: Josef Kellndorfer
Date: 2026-11-06

Search NISAR S-band (SSAR) products through the ISRO Bhoonidhi STAC API
(https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/).

Credentials are read from netrc (bhoonidhi-api.nrsc.gov.in entry).
If no credentials are found and stdin is a terminal, a prompt is shown.

The access and refresh tokens are cached in
~/.cache/openseppo/bhoonidhi_token.json (mode 600) and reused between runs:
the API allows 20 auth requests per hour per IP and a limited number of active
sessions per user.  Use --logout to end the cached session.

Collection, time, spatial, track, frame, cycle, direction and online filters and
the Urgent Response exclusion are sent to the API; the remaining column filters are
applied in Python to the fields parsed from the granule name, after --limit.

The operational collections (NISAR_SSAR_{product}) are searched first, then the
Beta ones (NISAR_SSAR-Beta_{product}), which hold earlier releases.  By default only
the latest release of each unique scene is returned.  Pass --allcrids to include
every processing version.

Output formats: url (default), csv, json, geojson, kml.  The url is the
Bhoonidhi download endpoint; fetching it requires a Bearer token, and products
with online = N must first be ordered through Bhoonidhi Browse&Order.

--download DIR fetches the results to DIR, one at a time because the API allows 2
download requests per minute per IP.  Interrupted downloads resume on the next run.

Dependencies (all conda-forge):
  requests
"""

import argparse
import base64
import getpass
import json
import math
import netrc
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from openseppo.cli.nisar_search import (
    _PAIR_PRODUCTS,
    _SINGLE_PRODUCTS,
    ALL_COLUMNS,
    _apply_column_filters,
    _apply_latest_release,
    _parse_dt,
    _parse_nisar_granule_name,
    _sort_for_group,
    format_output,
    geojson_file_to_wkt_list,
    output_grouped,
)

# -- Constants -----------------------------------------------------------------

API_HOST = "bhoonidhi-api.nrsc.gov.in"
# The account is the same for the portal and the API, so accept an entry under either host.
NETRC_HOSTS = (API_HOST, "bhoonidhi.nrsc.gov.in")
API_BASE = f"https://{API_HOST}"
TOKEN_URL = f"{API_BASE}/auth/token"
LOGOUT_URL = f"{API_BASE}/auth/logout"
COLLECTIONS_URL = f"{API_BASE}/data/collections"
SEARCH_URL = f"{API_BASE}/data/search"
DOWNLOAD_URL = f"{API_BASE}/download"
TOKEN_CACHE = os.path.expanduser("~/.cache/openseppo/bhoonidhi_token.json")

COLLECTION_PREFIX = "NISAR_SSAR_"
BETA_COLLECTION_PREFIX = "NISAR_SSAR-Beta_"
# SME2 is not listed among the Bhoonidhi NISAR collections, and only these have Beta ones.
PRODUCTS = sorted((_SINGLE_PRODUCTS | _PAIR_PRODUCTS) - {"SME2"})
BETA_PRODUCTS = frozenset({"GCOV", "GSLC", "RSLC"})

# The API rejects open-ended datetime ranges and ranges over 366 days (HTTP 406), so
# open ends are closed with these bounds and long ranges are split into windows.
MAX_WINDOW_DAYS = 365
MISSION_START = datetime(2025, 7, 1)  # before the NISAR launch

MAX_PAGE_SIZE = 500  # API maximum per search request
SEARCH_MIN_INTERVAL = 0.34  # API limit: 3 search requests per second per IP
TOKEN_MARGIN = 60  # seconds before expiry at which a token is no longer used
REQUEST_TIMEOUT = 120
RETRY_STATUS = frozenset({429, 502, 503, 504})
MAX_RETRIES = 4
DOWNLOAD_MIN_INTERVAL = 30.0  # API limit: 2 download requests per minute per IP
DOWNLOAD_GUARD = 50  # more products than this need an explicit --limit
DOWNLOAD_CHUNK = 1024 * 1024
HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
PAGINATION_KEYS = ("token", "page")

# Columns differ from seppo_nisar_search: one download url per product, no bucket,
# plus the granule name (not recoverable from the url basename) and online status.
S_COLUMNS = ["granule", "collection", "online"] + [c for c in ALL_COLUMNS if c not in ("bucket", "collection", "url_https")]

_NETRC_HELP = f"""Add an entry to ~/.netrc (and keep it mode 600):

    machine {API_HOST}
        login <your_bhoonidhi_userid>
        password <your_password>
"""


class BhoonidhiError(Exception):
    """API or credential failure, reported to the user without a traceback."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# -- Credentials ----------------------------------------------------------------


def _read_netrc():
    """Return (login, password) from the netrc entry, or None if there is none."""
    path = os.environ.get("NETRC") or os.path.expanduser("~/.netrc")
    if not os.path.exists(path):
        return None
    if os.stat(path).st_mode & 0o077:
        print(f"Warning: {path} is accessible by group/others; run: chmod 600 {path}", file=sys.stderr)
    try:
        parsed = netrc.netrc(path)
    except netrc.NetrcParseError as e:
        # Not str(e): its message can quote the offending token, which may be a password.
        raise BhoonidhiError(f"Could not parse {path} (line {e.lineno})") from None
    for host in NETRC_HOSTS:
        entry = parsed.hosts.get(host)
        if entry and entry[0] and entry[2]:
            return entry[0], entry[2]
    return None


def _prompt_credentials():
    if not sys.stdin.isatty():
        raise BhoonidhiError(f"No Bhoonidhi credentials found. {_NETRC_HELP}")
    print(f"No netrc entry for {API_HOST}; prompting.", file=sys.stderr)
    return input("Bhoonidhi userId: ").strip(), getpass.getpass("Bhoonidhi password: ")


def _jwt_expiry(token):
    """Expiry (epoch seconds) from a JWT's payload, or None.  The JWT is not verified."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


def _load_token_cache():
    try:
        with open(TOKEN_CACHE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_token_cache(cache):
    directory = os.path.dirname(TOKEN_CACHE)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bhoonidhi_token.")  # created mode 600
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(cache, fh)
        os.replace(tmp, TOKEN_CACHE)
    except BaseException:
        os.unlink(tmp)
        raise


def _describe_error(resp):
    """Human-readable message from the API error object {ErrorCode, Description, Action}."""
    try:
        body = {k.lower(): v for k, v in resp.json().items()}
    except (ValueError, AttributeError):
        return f"HTTP {resp.status_code}"
    parts = [str(body[k]) for k in ("description", "action") if body.get(k)]
    return f"HTTP {resp.status_code}: " + " ".join(parts) if parts else f"HTTP {resp.status_code}"


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        raise BhoonidhiError(f"{resp.url}: expected JSON, got {resp.headers.get('Content-Type')}") from None


# -- API client -----------------------------------------------------------------


class BhoonidhiClient:
    def __init__(self, session=None, verbose=False):
        self.session = session or requests.Session()
        self.verbose = verbose
        self._access_token = None
        self._last_search = 0.0
        self._last_download = None

    def _log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr)

    def _token_request(self, body, user):
        """POST to the token endpoint and cache the result.  body holds secrets: never log it."""
        try:
            resp = self.session.post(TOKEN_URL, json=body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise BhoonidhiError(f"Could not reach {TOKEN_URL}: {type(e).__name__}") from None
        if resp.status_code != 200:
            hints = {
                401: f" Check the Bhoonidhi userId and password. {_NETRC_HELP}",
                403: " Too many active sessions: end one, or run --logout on the machine holding it.",
                429: " The API allows 20 auth requests per hour per IP; wait before retrying.",
            }
            raise BhoonidhiError(f"Bhoonidhi login failed, {_describe_error(resp)}.{hints.get(resp.status_code, '')}", resp.status_code)
        data = _json(resp)
        if not data.get("access_token"):
            raise BhoonidhiError("Bhoonidhi login response did not include an access token.")
        cache = {
            "userId": data.get("userId") or user,
            "access_token": data["access_token"],
            "expires_at": time.time() + float(data.get("expires_in") or 0),
            "refresh_token": data.get("refresh_token"),
            "refresh_expires_at": _jwt_expiry(data.get("refresh_token")),
        }
        _save_token_cache(cache)
        return cache["access_token"]

    def _get_access_token(self, force_new=False):
        """Cached token if still valid, else refresh, else log in with the password."""
        creds = _read_netrc()
        cache = _load_token_cache()
        if cache and creds and cache.get("userId") != creds[0]:
            self._log("Cached token belongs to another user; ignoring it.")
            cache = None
        now = time.time()
        if cache and not force_new and cache.get("expires_at", 0) - TOKEN_MARGIN > now:
            return cache["access_token"]
        if cache and cache.get("refresh_token"):
            refresh_expiry = cache.get("refresh_expires_at")
            if refresh_expiry is None or refresh_expiry - TOKEN_MARGIN > now:
                self._log("Refreshing Bhoonidhi access token.")
                body = {"userId": cache.get("userId"), "refresh_token": cache["refresh_token"], "grant_type": "refresh_token"}
                try:
                    return self._token_request(body, cache.get("userId"))
                except BhoonidhiError as e:
                    if e.status == 429:
                        raise
                    self._log("Token refresh failed; logging in again.")
        user, password = creds or _prompt_credentials()
        self._log("Logging in to Bhoonidhi.")
        return self._token_request({"userId": user, "password": password, "grant_type": "password"}, user)

    def request(self, method, url, ok_status=(200,), **kwargs):
        """Authorized request; refreshes once on 401 and backs off on transient errors."""
        if self._access_token is None:
            self._access_token = self._get_access_token()
        extra_headers = kwargs.pop("headers", {})
        reauthorized = False
        attempt = 0
        while True:
            headers = {**extra_headers, "Authorization": f"Bearer {self._access_token}"}
            status, retry_after = None, None
            try:
                resp = self.session.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)
                status = resp.status_code
                if status in ok_status:
                    return resp
                if status == 401 and not reauthorized:
                    self._access_token = self._get_access_token(force_new=True)
                    reauthorized = True
                    continue
                if status not in RETRY_STATUS:
                    raise BhoonidhiError(f"{url}: {_describe_error(resp)}", status)
                retry_after = resp.headers.get("Retry-After")
                reason = _describe_error(resp)
            except requests.RequestException as e:
                reason = type(e).__name__
            attempt += 1
            if attempt > MAX_RETRIES:
                raise BhoonidhiError(f"{url}: giving up after {MAX_RETRIES} retries, {reason}", status)
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 5 * 2 ** (attempt - 1)
            self._log(f"{reason}; retrying in {delay:.0f}s")
            time.sleep(delay)

    def search(self, body, limit=None):
        """All features matching body (up to limit), following STAC pagination."""
        body = dict(body)
        features, seen = [], set()
        while True:
            body["limit"] = min(MAX_PAGE_SIZE, limit - len(features)) if limit else MAX_PAGE_SIZE
            wait = self._last_search + SEARCH_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_search = time.monotonic()
            try:
                page = _json(self.request("POST", SEARCH_URL, json=body))
            except BhoonidhiError as e:
                # The API reports both an empty search and the end of paging this way.
                if e.status == 404 and "NO RESULTS" in str(e).upper():
                    return features
                raise

            new = [f for f in page.get("features") or [] if f.get("id") not in seen]
            seen.update(f.get("id") for f in new)
            features.extend(new)
            self._log(f"  page: {len(new)} new feature(s), {len(features)} total")

            if limit and len(features) >= limit:
                return features[:limit]
            cursor = _next_cursor(page)
            if cursor is None:
                return features
            if not new:
                print("Warning: API offered a next page but returned no new features; results may be incomplete.", file=sys.stderr)
                return features
            body.update(cursor)

    def search_requests(self, requests_, limit=None):
        """Features for each request body in turn, up to limit in total."""
        features = []
        for body in requests_:
            remaining = limit - len(features) if limit else None
            features.extend(self.search(body, limit=remaining))
            if limit and len(features) >= limit:
                break
        return features

    def download(self, granule, url, directory):
        """Download one product to directory/granule.h5, resuming a partial file; returns its path."""
        final = os.path.join(directory, os.path.basename(granule) + ".h5")
        if os.path.exists(final):
            print(f"  already present: {final}", file=sys.stderr)
            return final
        part = final + ".part"

        attempt = 0
        while True:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            wait = (self._last_download or 0) + DOWNLOAD_MIN_INTERVAL - time.monotonic()
            if self._last_download is not None and wait > 0:
                time.sleep(wait)
            self._last_download = time.monotonic()
            headers = {"Range": f"bytes={have}-"} if have else {}
            with self.request("GET", url, ok_status=(200, 206, 416), stream=True, headers=headers) as resp:
                if resp.status_code == 416:  # nothing left beyond what we have
                    total = have
                else:
                    if resp.status_code == 200:  # server ignored the range: start over
                        have = 0
                    total = have + int(resp.headers.get("Content-Length") or 0)
                    if have == 0 and resp.status_code == 200 and total:
                        _require_free_space(directory, total)
                    received = self._stream_to(resp, part, have, total)
                    if received is None:
                        attempt += 1
                        if attempt > MAX_RETRIES:
                            raise BhoonidhiError(f"{granule}: download kept failing; the partial file is kept for resuming: {part}")
                        time.sleep(5 * 2 ** (attempt - 1))
                        continue
                    attempt = 0
            size = os.path.getsize(part)
            if total and size != total:
                continue  # stream ended early without an error: resume
            with open(part, "rb") as fh:
                if fh.read(len(HDF5_SIGNATURE)) != HDF5_SIGNATURE:
                    raise BhoonidhiError(f"{granule}: download is not an HDF5 file; left at {part}")
            os.replace(part, final)
            return final

    def _stream_to(self, resp, part, have, total):
        """Append a response body to part.  Returns bytes written, or None if the stream broke."""
        started = time.monotonic()
        written = 0
        next_report = started + 30
        try:
            with open(part, "ab" if have else "wb") as fh:
                for chunk in resp.iter_content(DOWNLOAD_CHUNK):
                    fh.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if now >= next_report:
                        next_report = now + 30
                        pct = f"{100 * (have + written) / total:.0f}%, " if total else ""
                        print(f"  {pct}{written / 1e6 / (now - started):.1f} MB/s", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  connection lost after {written / 1e6:.0f} MB ({type(e).__name__}); will resume", file=sys.stderr)
            return None
        return written

    def list_collections(self):
        return _json(self.request("GET", COLLECTIONS_URL))

    def logout(self):
        """End the cached session and remove the token cache."""
        cache = _load_token_cache()
        if not cache:
            print("No cached Bhoonidhi session.", file=sys.stderr)
            return
        headers = {"Authorization": f"Bearer {cache.get('refresh_token')}"}
        try:
            resp = self.session.post(LOGOUT_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise BhoonidhiError(f"Could not reach {LOGOUT_URL}: {type(e).__name__}") from None
        if resp.status_code != 200:
            raise BhoonidhiError(f"Bhoonidhi logout failed, {_describe_error(resp)}", resp.status_code)
        os.unlink(TOKEN_CACHE)
        print("Logged out of Bhoonidhi.", file=sys.stderr)


def _require_free_space(directory, needed):
    free = shutil.disk_usage(directory).free
    if free < needed:
        raise BhoonidhiError(f"Not enough disk space in {directory}: {needed / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free")


def _next_cursor(page):
    """Pagination fields from a page's rel=next link, or None on the last page.

    Only the cursor is taken from the link and re-sent to SEARCH_URL: the link's own
    host is server-supplied (STAC backends often report an internal one) and the
    Bearer token must not follow it.
    """
    link = next((x for x in page.get("links") or [] if x.get("rel") == "next"), None)
    if link is None:
        return None
    source = link.get("body") or {k: v[0] for k, v in parse_qs(urlsplit(link.get("href", "")).query).items()}
    cursor = {k: source[k] for k in PAGINATION_KEYS if k in source}
    if "page" in cursor:
        cursor["page"] = int(cursor["page"])
    if not cursor:
        raise BhoonidhiError(f"Unrecognised pagination link, results would be incomplete: {link}")
    return cursor


# -- Search body ----------------------------------------------------------------


def _wkt_to_geometry(wkt):
    """2-D GeoJSON geometry from WKT (the types the API's `intersects` accepts)."""
    types = {t.upper(): t for t in ("Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon")}
    m = re.fullmatch(r"\s*([A-Za-z]+)\s*(\(.*\))\s*", wkt, re.DOTALL)
    gtype = types.get(m.group(1).upper()) if m else None
    if not gtype:
        raise BhoonidhiError(f"Unsupported WKT geometry: {wkt[:60]}")
    num = r"[-+]?[\d.]+(?:[eE][-+]?\d+)?"
    try:
        body = re.sub(rf"({num})\s+({num})(?:\s+{num})?", lambda c: f"[{float(c[1])},{float(c[2])}]", m.group(2))
        coords = json.loads(body.replace("(", "[").replace(")", "]"))
    except ValueError:
        raise BhoonidhiError(f"Could not parse WKT: {wkt[:60]}") from None
    if gtype == "Point":
        coords = coords[0]
    elif gtype == "MultiPoint":  # both MULTIPOINT(1 2, 3 4) and MULTIPOINT((1 2), (3 4))
        coords = [c[0] if isinstance(c[0], list) else c for c in coords]
    return {"type": gtype, "coordinates": coords}


def _bbox_polygon(west, south, east, north):
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Polygon", "coordinates": [ring]}


def _flat_positions(coords):
    if coords and isinstance(coords[0], (int, float)):
        return [coords]
    return [p for c in coords for p in _flat_positions(c)]


def _build_intersects(args):
    """GeoJSON geometry for the `intersects` search field, or None."""
    if args.point:
        lon, lat = args.point
        if not args.buffer:
            return {"type": "Point", "coordinates": [lon, lat]}
        # Same degree-based radius as seppo_nisar_search, as a 32-sided polygon.
        dlon = args.buffer / max(math.cos(math.radians(lat)), 0.01)
        angles = [2 * math.pi * i / 32 for i in range(32)]
        ring = [[lon + dlon * math.cos(a), lat + args.buffer * math.sin(a)] for a in angles]
        return {"type": "Polygon", "coordinates": [ring + ring[:1]]}
    if args.bbox:
        west, south, east, north = args.bbox
        return _bbox_polygon(west, south, east, north)
    if args.ullr:
        west, north, east, south = args.ullr
        return _bbox_polygon(west, south, east, north)
    if args.wkt:
        return _wkt_to_geometry(args.wkt)
    if args.geojson:
        geoms = [_wkt_to_geometry(w) for w in geojson_file_to_wkt_list(args.geojson)]
        if not geoms:
            raise BhoonidhiError(f"No geometry found in {args.geojson}")
        if not args.union_geojson or len(geoms) == 1:
            return geoms[0]
        positions = [p for g in geoms for p in _flat_positions(g["coordinates"])]
        lons, lats = [p[0] for p in positions], [p[1] for p in positions]
        return _bbox_polygon(min(lons), min(lats), max(lons), max(lats))
    return None


def _parse_time_arg(val):
    dt = _parse_dt(val)
    if dt is None:
        raise BhoonidhiError(f"Invalid datetime: {val!r}; expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    return dt


def _datetime_windows(after, before):
    """Closed RFC 3339 ranges of at most MAX_WINDOW_DAYS covering [after, before], newest first."""
    if not after and not before:
        return [None]
    start = _parse_time_arg(after) if after else MISSION_START
    end = _parse_time_arg(before) if before else datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    if start > end:
        raise BhoonidhiError(f"Empty time range: {start.isoformat()} is after {end.isoformat()}")
    windows = []
    while end >= start:
        window_start = max(start, end - timedelta(days=MAX_WINDOW_DAYS))
        windows.append(f"{window_start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}")
        end = window_start - timedelta(seconds=1)
    return windows


def _values_clause(prop, values):
    op = "eq" if len(values) == 1 else "in"
    return {"op": op, "args": [{"property": prop}, values[0] if len(values) == 1 else values]}


def _build_filter(args):
    """CQL2 filter for the column filters the API can evaluate, or None.

    These NISAR properties were checked against live records, where they agree with
    the granule name.  Results are still post-filtered on the name by _filter_records.
    """
    clauses = []
    if args.granule:
        clauses.append(_values_clause("id", [g.removesuffix(".h5") for g in args.granule]))
    for prop, values in (("Track", args.track), ("Frame", args.frame), ("CycleNumber", args.cycle)):
        if values:
            clauses.append(_values_clause(prop, values))
    if args.direction:
        clauses.append(_values_clause("Node", [{"A": "Ascending", "D": "Descending"}[d] for d in args.direction]))
    if args.online_only:
        clauses.append(_values_clause("Online", ["Y"]))
    if not args.urgent_response and not args.granule:
        # Excluded here rather than only afterwards: UR products are the newest, so
        # they would otherwise consume --limit and leave nothing after filtering.
        clauses.append({"op": "not", "args": [{"op": "like", "args": [{"property": "id"}, "%_UR_%"]}]})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"op": "and", "args": clauses}


def _search_collections(args):
    if args.collection:
        return args.collection
    products = [p.upper() for p in args.product]
    return [COLLECTION_PREFIX + p for p in products] + [BETA_COLLECTION_PREFIX + p for p in products if p in BETA_PRODUCTS]


def _build_requests(args):
    """One search body per collection and time window, in search order.

    Collections are never combined in one request: a multi-collection search returned
    no results when its first collection had none, although a later one did.
    """
    common = {}
    intersects = _build_intersects(args)
    if intersects:
        common["intersects"] = intersects
    cql = _build_filter(args)
    if cql:
        common["filter"] = cql
        common["filter-lang"] = "cql2-json"

    bodies = []
    for collection in _search_collections(args):
        for window in _datetime_windows(args.start_time_after, args.start_time_before):
            body = {"collections": [collection], **common}
            if window:
                body["datetime"] = window
            bodies.append(body)
    return bodies


# -- Records --------------------------------------------------------------------


def _prop(props, name):
    """Case-insensitive property lookup: names differ across Bhoonidhi collections."""
    name = name.lower()
    return next((v for k, v in props.items() if k.lower() == name), None)


def _feature_to_record(feature):
    """STAC feature -> search record, in the shape the seppo_nisar_search helpers expect.

    Fields come from the granule name; item properties only fill what the name
    cannot supply (online status) or stand in when the name does not parse.
    """
    fid = feature.get("id") or ""
    props = feature.get("properties") or {}
    collection = feature.get("collection")
    granule = fid.removesuffix(".h5")
    node = str(_prop(props, "Node") or "")
    online = _prop(props, "Online")

    rec = {
        "granule": granule,
        "collection": collection,
        "online": str(online).upper() if online is not None else None,
        "product": collection.rsplit("_", 1)[-1] if collection else None,
        "direction": node[0].upper() if node else None,
        "polarization": _prop(props, "Polarization"),
        "start_time": _parse_dt(_prop(props, "start_datetime") or _prop(props, "datetime")),
        "end_time": _parse_dt(_prop(props, "end_datetime")),
    }
    rec.update(_parse_nisar_granule_name(granule))
    rec["url"] = f"{DOWNLOAD_URL}?{urlencode({'id': fid, 'collection': collection or ''})}"
    rec["_geom"] = feature.get("geometry")
    return rec


# --- Main processing -----------------------------------------------------------


def _print_collections(data):
    collections = data.get("collections") if isinstance(data, dict) else None
    if collections is None:
        print(json.dumps(data, indent=2))
        return
    for c in collections:
        print(f"{c.get('id')}\t{c.get('title') or ''}")


def _filter_records(records, args):
    """Same order as seppo_nisar_search: UR exclusion, column filters, latest release."""
    unparsed = sum(1 for r in records if r.get("track") is None)
    if unparsed:
        print(f"Warning: {unparsed} of {len(records)} product id(s) are not standard NISAR granule names; " "track, frame and cycle are unavailable for them, so they fail any filter on those columns.", file=sys.stderr)

    if not args.urgent_response and not args.granule:
        records = [r for r in records if r.get("proctype") != "UR"]
    records = _apply_column_filters(records, args)
    if args.online_only:
        records = [r for r in records if r.get("online") != "N"]
    if not args.allcrids:
        # Without a parsed name there is no scene key: such records would collapse together.
        parsed = _apply_latest_release([r for r in records if r.get("track") is not None])
        keep = {id(r) for r in parsed}
        records = [r for r in records if r.get("track") is None or id(r) in keep]
    return records


def _download_records(client, records, args):
    """Fetch records to args.download, continuing past failures and reporting them at the end."""
    if not args.download:
        return
    os.makedirs(args.download, exist_ok=True)
    failed = []
    for i, rec in enumerate(records, 1):
        granule = rec["granule"]
        print(f"[{i}/{len(records)}] {granule}", file=sys.stderr)
        if rec.get("online") == "N":
            print("  skipped: offline; order it through Bhoonidhi Browse&Order first", file=sys.stderr)
            failed.append(granule)
            continue
        try:
            path = client.download(granule, rec["url"], args.download)
            print(f"  {path} ({os.path.getsize(path) / 1e6:.0f} MB)", file=sys.stderr)
        except BhoonidhiError as e:
            print(f"  failed: {e}", file=sys.stderr)
            failed.append(granule)
    if failed:
        raise BhoonidhiError(f"{len(failed)} of {len(records)} download(s) did not complete: {', '.join(failed)}")


def processing(args, client=None):
    client = client or BhoonidhiClient(verbose=args.verbose)

    if args.logout:
        client.logout()
        return
    if args.list_collections:
        _print_collections(client.list_collections())
        return

    bodies = _build_requests(args)
    if args.verbose or args.dryrun:
        print("--- Bhoonidhi search ---", file=sys.stderr)
        print(f"  url:   {SEARCH_URL}", file=sys.stderr)
        for body in bodies:
            print(f"  body:  {json.dumps(body)}", file=sys.stderr)
        print(f"  limit: {args.limit}", file=sys.stderr)
    if args.dryrun:
        return

    features = client.search_requests(bodies, limit=args.limit)
    if args.raw:
        print(json.dumps({"type": "FeatureCollection", "features": features}, indent=2))
        return

    records = _filter_records([_feature_to_record(f) for f in features], args)

    if args.verbose:
        print(f"Found {len(records)} record(s).", file=sys.stderr)
    if not records:
        return

    offline = sum(1 for r in records if r.get("online") == "N")
    if offline:
        print(f"Note: {offline} of {len(records)} product(s) are offline: the API cannot download them until they " "are ordered through Bhoonidhi Browse&Order.  --online_only excludes them.", file=sys.stderr)

    if args.download and len(records) > DOWNLOAD_GUARD and not args.limit:
        raise BhoonidhiError(f"--download would fetch {len(records)} products; narrow the search or pass --limit N to confirm.")

    if args.group:
        ungrouped = sum(1 for r in records if r.get("track") is None)
        if ungrouped:
            print(f"Warning: --group omits {ungrouped} product(s) without a parsed track/frame.", file=sys.stderr)
            records = [r for r in records if r.get("track") is not None]
        if records:
            output_grouped(_sort_for_group(records), args)
        _download_records(client, records, args)
        return

    lines = format_output(records, args.format, args.columns)
    output = "\n".join(lines) + ("\n" if lines else "")

    if args.output:
        out_dir = os.path.dirname(args.output)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output, "w") as fh:
            fh.write(output)
        if args.verbose:
            print(f"Output written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)
    _download_records(client, records, args)


# --- Argument parsing ----------------------------------------------------------


def myargsparse(a):
    if type(a) is str:
        a = shlex.split(a)

    class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        pass

    thisprog = os.path.basename(a[0])

    epilog = f"""
\nExamples:
\r  NOTE: --product GCOV is the default.


\r  Dry-run (show the search request without logging in):
\r    {thisprog} --product GCOV --bbox 76 10 78 12 --dryrun

\r  List the collections available to your account:
\r    {thisprog} --list_collections

\r  GCOV download URLs for an area and date range, online products only:
\r    {thisprog} --bbox 76 10 78 12 --start_time_after 2026-01-01 --online_only

\r  Descending track 64, as CSV:
\r    {thisprog} --product GSLC --track 64 --direction D --format csv -o results.csv

\r  Pair-acquisition product (GUNW) with secondary cycle:
\r    {thisprog} --product GUNW --track 71 --frame 173 --cycle 3 --cycle2 5

\r  Grouped files to directory:
\r    {thisprog} --product GCOV --bbox 76 10 78 12 --group -o /data/urls/

\r  Download specific granules:
\r    {thisprog} --granule NISAR_S2_PR_GCOV_031_077_D_084_2500_CRNA_A_20260923T132025_20260923T132056_P00500_M_P_I_001 --download /data/nisar/

\r  Inspect unmodified API records (e.g. to see available properties):
\r    {thisprog} --product GCOV --limit 2 --raw
"""

    description = "SEPPO - Search NISAR S-band products via the ISRO Bhoonidhi API.\n" f"Credentials are read from the netrc ({API_HOST} entry); a prompt is shown if\n" "no entry is found.  Use --dryrun to inspect the query without logging in."

    p = argparse.ArgumentParser(prog=thisprog, description=description, epilog=epilog, formatter_class=CustomFormatter)

    # -- Column / metadata filters ---------------------------------------------
    cf = p.add_argument_group("Column / metadata filters (all accept one or more values)")
    cf.add_argument("--collection", nargs="*", metavar="NAME", help=f"Bhoonidhi collection ID(s) to search - overrides {COLLECTION_PREFIX}{{product}} and {BETA_COLLECTION_PREFIX}{{product}} construction from --product.")
    cf.add_argument("--granule", nargs="+", metavar="NAME", help="Exact granule name(s).  Urgent Response products are included when named here, and the product is taken from the name.")
    cf.add_argument("--mission", nargs="*", metavar="CODE", help="Mission code(s) (e.g. NISAR)")
    cf.add_argument("--inst_level", nargs="*", metavar="CODE", help="Instrument and processing level code(s) from the granule name")
    cf.add_argument("--proctype", nargs="*", metavar="CODE", help="Processing type(s)")
    cf.add_argument("--product", nargs="+", metavar="CODE", help="Product type(s), default GCOV unless --collection is given", choices=PRODUCTS)
    cf.add_argument("--cycle", nargs="*", type=int, metavar="INT", help="Reference cycle number(s)")
    cf.add_argument("--cycle2", nargs="*", type=int, metavar="INT", help="Secondary cycle number(s) for pair-acquisition products (RIFG, RUNW, GUNW, ROFF, GOFF).")
    cf.add_argument("--track", nargs="*", type=int, metavar="INT", help="Track / relative-orbit number(s)")
    cf.add_argument("--direction", nargs="*", metavar="A|D", help="Flight direction: A (ascending) or D (descending)")
    cf.add_argument("--frame", nargs="*", type=int, metavar="INT", help="Frame number(s)")
    cf.add_argument("--mode", nargs="*", metavar="CODE", help="Acquisition mode(s)")
    cf.add_argument("--polarization", nargs="*", metavar="CODE", help="Polarization code(s). Supports LIKE wildcards (%%).")
    cf.add_argument("--observation_mode", nargs="*", metavar="CODE", help="Observation mode code(s)")
    cf.add_argument("--crid", nargs="*", metavar="CODE", help="Composite release ID(s)")
    cf.add_argument("--accuracy", nargs="*", metavar="CODE", help="Accuracy code(s)")
    cf.add_argument("--coverage", nargs="*", metavar="CODE", help="Coverage code(s)")
    cf.add_argument("--sds", nargs="*", metavar="CODE", help="SDS code(s)")
    cf.add_argument("--counter", nargs="*", metavar="CODE", help="Counter value(s)")
    cf.add_argument("--url_pattern", metavar="PATTERN", help="LIKE pattern matched against url (e.g. '%%GCOV%%')")

    # -- Time filters ----------------------------------------------------------
    tf = p.add_argument_group("Time filters", "ISO 8601: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, UTC.  Maps to the API datetime parameter; long ranges are split into several requests.")
    tf.add_argument("--start_time_after", metavar="DATETIME", help="Acquisition start >= DATETIME")
    tf.add_argument("--start_time_before", metavar="DATETIME", help="Acquisition start <= DATETIME")

    # -- Spatial filters -------------------------------------------------------
    sf = p.add_argument_group("Spatial filters")
    geom_src = sf.add_mutually_exclusive_group()
    geom_src.add_argument("--wkt", metavar="WKT", help="OGC WKT geometry in WGS84 (POINT, POLYGON, MULTIPOLYGON, ...)")
    geom_src.add_argument("--ullr", nargs=4, type=float, metavar=("UL_LON", "UL_LAT", "LR_LON", "LR_LAT"), help="Bounding box from upper-left / lower-right corners")
    geom_src.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"), help="Bounding box in (xmin ymin xmax ymax) order")
    geom_src.add_argument("--point", nargs=2, type=float, metavar=("LON", "LAT"), help="Point in WGS84 lon/lat.  Use --buffer for radius search.")
    geom_src.add_argument("--geojson", metavar="FILE", help="GeoJSON file.  First feature used unless --union_geojson.")
    sf.add_argument("--buffer", type=float, metavar="DEG", help="Buffer radius in degrees for --point")
    sf.add_argument("--union_geojson", action="store_true", default=False, help="Search the bounding envelope of all GeoJSON features")

    # -- Output ----------------------------------------------------------------
    og = p.add_argument_group("Output")
    og.add_argument("--group", action="store_true", default=False, help="Group by (track, direction, frame) ordered by start_time. " "stdout: section headers + URLs.  --output: directory, one file per group.")
    og.add_argument("--allcrids", action="store_true", default=False, help="Return every processing version; default keeps only the latest release per scene.")
    og.add_argument("-ur", "--urgent_response", action="store_true", default=False, help="Include Urgent Response (UR) products. Excluded by default.")
    og.add_argument("--online_only", action="store_true", default=False, help="Exclude products marked offline (Online = N); these cannot be downloaded through the API.")
    og.add_argument("-o", "--output", metavar="PATH", help="Without --group: output file path.  With --group: output directory.")
    og.add_argument("--format", default="url", choices=["url", "csv", "json", "geojson", "kml"], help="Output format: url (one per line), csv, json, geojson, kml")
    og.add_argument("--columns", nargs="*", metavar="COL", default=S_COLUMNS, help="Columns for csv/json output.")
    og.add_argument("--limit", type=int, metavar="N", help="Maximum number of products to retrieve across all collections, before filters not handled by the API (e.g. --polarization, latest release)")
    og.add_argument("--download", metavar="DIR", help=f"Download the results to DIR as {{granule}}.h5, skipping files already there and resuming partial ones.  More than {DOWNLOAD_GUARD} products need an explicit --limit.")
    og.add_argument("--raw", action="store_true", default=False, help="Print the unmodified API features as JSON, skipping filters not handled by the API and output formatting")
    og.add_argument("--list_collections", action="store_true", default=False, help="List the collections available from the API and exit")
    og.add_argument("--logout", action="store_true", default=False, help="End the cached API session and remove the token cache, then exit")
    og.add_argument("-v", "--verbose", action="store_true", default=False, help="Print the search request, page counts, etc. to stderr")
    og.add_argument("--dryrun", action="store_true", default=False, help="Print the search request without logging in or searching, then exit")

    args = p.parse_args(a[1:])

    if len(a) == 1:
        p.print_usage()
        sys.exit(0)

    if args.granule:
        named = [_parse_nisar_granule_name(g.removesuffix(".h5")).get("product") for g in args.granule]
        if not args.collection and not args.product and all(named):
            args.product = list(dict.fromkeys(named))
        args.allcrids = True  # every named granule is wanted, whatever its release
    if not args.collection and not args.product:
        args.product = ["GCOV"]

    # Normalise direction
    if args.direction:
        args.direction = [d.upper() for d in args.direction]
        invalid = [d for d in args.direction if d not in ("A", "D")]
        if invalid:
            p.error(f"--direction: invalid value(s) {invalid!r}; expected 'A' or 'D'")

    if args.buffer and not args.point:
        print("Warning: --buffer is only used with --point; ignoring.", file=sys.stderr)

    if args.limit is not None and args.limit < 1:
        p.error("--limit must be at least 1")

    # Read by output_grouped: there is one url per product, and it names per-group url files.
    args.https = False
    args.url_file_suffix = "bhoonidhi_urls"

    return args


# --- Entry point ---------------------------------------------------------------


def _main(a):
    args = myargsparse(a)
    processing(args)


def main():
    from openseppo import banner
    args = myargsparse(sys.argv)
    if args.output:
        banner("seppo_nisar_search_S")
    try:
        processing(args)
    except BhoonidhiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
