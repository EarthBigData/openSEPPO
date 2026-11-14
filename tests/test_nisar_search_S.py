"""Tests for seppo_nisar_search_S against a scripted API; no network or real credentials."""

import base64
import itertools
import json
import os
import stat
import time

import pytest

from openseppo.cli import nisar_search_S as ns

GRANULE = "NISAR_L2_PR_GCOV_015_172_D_065_4005_DHDH_A_20260121T031851_20260121T031926_P05006_N_F_J_001"
PAIR = "NISAR_L2_PR_GUNW_003_071_A_173_005_2000_SH_20251022T041142_20251022T041217_20251115T041143_20251115T041218_X05010_N_F_J_001"
SECRET = "s3cret-pw"


class Resp:
    def __init__(self, status=200, body=None, headers=None, content=None, break_after=False):
        self.status_code = status
        self.content = content
        self.break_after = break_after
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.url = "https://example.invalid"

    def json(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield self.content
        if self.break_after:
            raise ns.requests.ConnectionError("dropped")


def NO_RESULTS():
    return Resp(404, {"ErrorCode": "404", "Description": "NO RESULTS FOUND", "Action": "No results found."})


class Session:
    """Replays scripted responses and records every call."""

    def __init__(self, token_responses=(), api_responses=()):
        self.token_responses = list(token_responses)
        self.api_responses = list(api_responses)
        self.post_calls = []
        self.api_calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return self.token_responses.pop(0)

    def request(self, method, url, headers=None, timeout=None, json=None, stream=False):
        self.api_calls.append({"method": method, "url": url, "headers": headers, "json": dict(json) if json else None})
        return self.api_responses.pop(0) if self.api_responses else NO_RESULTS()


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def token_body(access="access1", refresh_exp=None):
    return {"userId": "me", "access_token": access, "expires_in": 1200, "refresh_token": jwt(refresh_exp or time.time() + 18000)}


def page(features, next_link=None):
    return Resp(body={"features": features, "links": [next_link] if next_link else []})


def feature(fid=GRANULE, collection="NISAR_SSAR_GCOV", **props):
    return {"id": fid, "collection": collection, "geometry": {"type": "Point", "coordinates": [1, 2]}, "properties": props}


def args_for(*argv):
    return ns.myargsparse(["seppo_nisar_search_S", *argv])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    netrc = tmp_path / "netrc"
    netrc.write_text(f"machine {ns.API_HOST}\n  login me\n  password {SECRET}\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setattr(ns, "TOKEN_CACHE", str(tmp_path / "cache" / "token.json"))
    monkeypatch.setattr(ns.time, "sleep", lambda s: None)
    return tmp_path


# --- Search body ---------------------------------------------------------------


NOT_UR = {"op": "not", "args": [{"op": "like", "args": [{"property": "id"}, "%_UR_%"]}]}


def test_default_requests_search_operational_then_beta_separately():
    bodies = ns._build_requests(args_for("--allcrids"))
    assert [b["collections"] for b in bodies] == [["NISAR_SSAR_GCOV"], ["NISAR_SSAR-Beta_GCOV"]]
    assert bodies[0]["filter"] == NOT_UR and bodies[0]["filter-lang"] == "cql2-json"


def test_products_without_beta_collection():
    assert [b["collections"][0] for b in ns._build_requests(args_for("--product", "GUNW", "GSLC"))] == ["NISAR_SSAR_GUNW", "NISAR_SSAR_GSLC", "NISAR_SSAR-Beta_GSLC"]


def test_explicit_collection_overrides_default_product():
    args = args_for("--collection", "NISAR_SSAR_GSLC")
    assert args.product is None
    assert [b["collections"] for b in ns._build_requests(args)] == [["NISAR_SSAR_GSLC"]]


def test_filter_pushes_supported_columns_to_api():
    cql = ns._build_filter(args_for("--track", "19", "20", "--frame", "77", "--cycle", "35", "--direction", "D", "--online_only", "-ur"))
    assert cql == {
        "op": "and",
        "args": [
            {"op": "in", "args": [{"property": "Track"}, [19, 20]]},
            {"op": "eq", "args": [{"property": "Frame"}, 77]},
            {"op": "eq", "args": [{"property": "CycleNumber"}, 35]},
            {"op": "eq", "args": [{"property": "Node"}, "Descending"]},
            {"op": "eq", "args": [{"property": "Online"}, "Y"]},
        ],
    }


def test_no_filter_when_nothing_to_push():
    bodies = ns._build_requests(args_for("-ur"))
    assert "filter" not in bodies[0] and "filter-lang" not in bodies[0]


def test_datetime_windows_closed_range():
    assert ns._datetime_windows("2026-01-01", "2026-02-01T12:00:00") == ["2026-01-01T00:00:00Z/2026-02-01T12:00:00Z"]


def test_datetime_windows_close_open_ends():
    assert ns._datetime_windows(None, None) == [None]
    assert ns._datetime_windows(None, "2026-01-01") == ["2025-07-01T00:00:00Z/2026-01-01T00:00:00Z"]
    after = ns._datetime_windows("2026-10-01", None)
    assert len(after) == 1 and after[0].startswith("2026-10-01T00:00:00Z/") and not after[0].endswith("/")


def test_datetime_windows_split_long_range_newest_first_without_overlap():
    windows = ns._datetime_windows("2024-01-01", "2026-06-01")
    assert len(windows) == 3
    assert windows[0].endswith("/2026-06-01T00:00:00Z") and windows[-1].startswith("2024-01-01T00:00:00Z/")
    spans = [[ns._parse_dt(x) for x in w.split("/")] for w in windows]
    assert all((end - start).days <= 366 for start, end in spans)
    assert all((older[1] - newer[0]).total_seconds() == -1 for newer, older in itertools.pairwise(spans))


def test_datetime_windows_reject_bad_input():
    with pytest.raises(ns.BhoonidhiError, match="Empty time range"):
        ns._datetime_windows("2026-02-01", "2026-01-01")
    with pytest.raises(ns.BhoonidhiError, match="Invalid datetime"):
        ns._datetime_windows("yesterday", None)


def test_bbox_and_ullr_produce_same_polygon():
    bbox = ns._build_intersects(args_for("--bbox", "1", "2", "3", "4"))
    ullr = ns._build_intersects(args_for("--ullr", "1", "4", "3", "2"))
    assert bbox == ullr
    assert bbox["coordinates"][0][0] == [1, 2] and bbox["coordinates"][0][2] == [3, 4]


def test_point_buffer_ring_is_closed():
    geom = ns._build_intersects(args_for("--point", "10", "60", "--buffer", "1"))
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 33
    assert max(p[0] for p in ring) == pytest.approx(12.0)  # 1 deg of latitude at 60N


@pytest.mark.parametrize(
    "wkt,expected",
    [
        ("POINT(1 2)", {"type": "Point", "coordinates": [1.0, 2.0]}),
        ("point (-1.5 .5 100)", {"type": "Point", "coordinates": [-1.5, 0.5]}),
        ("MULTIPOINT((1 2), (3 4))", {"type": "MultiPoint", "coordinates": [[1.0, 2.0], [3.0, 4.0]]}),
        ("MULTIPOINT(1 2, 3 4)", {"type": "MultiPoint", "coordinates": [[1.0, 2.0], [3.0, 4.0]]}),
        ("MULTIPOLYGON(((0 0,1 0,1 1,0 0)),((5 5,6 5,6 6,5 5)))", None),
    ],
)
def test_wkt_to_geometry(wkt, expected):
    geom = ns._wkt_to_geometry(wkt)
    if expected:
        assert geom == expected
    else:
        assert geom["type"] == "MultiPolygon" and len(geom["coordinates"]) == 2


def test_wkt_rejects_unsupported():
    with pytest.raises(ns.BhoonidhiError):
        ns._wkt_to_geometry("GEOMETRYCOLLECTION(POINT(1 2))")


def test_geojson_union_uses_envelope(tmp_path):
    path = tmp_path / "aoi.geojson"
    poly = lambda x: {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[x, 0], [x + 1, 0], [x + 1, 1], [x, 0]]]}}
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [poly(0), poly(5)]}))
    first = ns._build_intersects(args_for("--geojson", str(path)))
    assert first["coordinates"][0][1] == [1.0, 0.0]
    union = ns._build_intersects(args_for("--geojson", str(path), "--union_geojson"))
    assert union["coordinates"][0][2] == [6.0, 1.0]


def test_dryrun_makes_no_requests(capsys):
    session = Session()
    ns.processing(args_for("--dryrun"), ns.BhoonidhiClient(session=session))
    assert not session.post_calls and not session.api_calls
    assert "NISAR_SSAR_GCOV" in capsys.readouterr().err


# --- Auth ----------------------------------------------------------------------


def test_login_caches_token_privately_and_reuses_it():
    session = Session([Resp(body=token_body())], [Resp(body={"collections": []}), Resp(body={"collections": []})])
    ns.BhoonidhiClient(session=session).list_collections()
    ns.BhoonidhiClient(session=session).list_collections()
    assert len(session.post_calls) == 1
    assert session.post_calls[0]["json"] == {"userId": "me", "password": SECRET, "grant_type": "password"}
    assert session.api_calls[1]["headers"]["Authorization"] == "Bearer access1"
    assert stat.S_IMODE(os.stat(ns.TOKEN_CACHE).st_mode) == 0o600
    with open(ns.TOKEN_CACHE) as fh:
        assert SECRET not in fh.read()


def expire_cached_access():
    cache = ns._load_token_cache()
    cache["expires_at"] = 0
    ns._save_token_cache(cache)


def test_expired_access_token_uses_refresh_not_password():
    first = token_body()
    ns.BhoonidhiClient(session=Session([Resp(body=first)]))._get_access_token()
    expire_cached_access()
    session = Session([Resp(body=token_body("access2"))])
    assert ns.BhoonidhiClient(session=session)._get_access_token() == "access2"
    assert session.post_calls[0]["json"] == {"userId": "me", "refresh_token": first["refresh_token"], "grant_type": "refresh_token"}


def test_expired_refresh_token_logs_in():
    ns.BhoonidhiClient(session=Session([Resp(body=token_body(refresh_exp=1))]))._get_access_token()
    expire_cached_access()
    session = Session([Resp(body=token_body("access2"))])
    ns.BhoonidhiClient(session=session)._get_access_token()
    assert session.post_calls[0]["json"]["grant_type"] == "password"


def test_failed_refresh_falls_back_to_login_but_rate_limit_does_not():
    ns.BhoonidhiClient(session=Session([Resp(body=token_body())]))._get_access_token()
    expire_cached_access()
    session = Session([Resp(400), Resp(body=token_body("access2"))])
    assert ns.BhoonidhiClient(session=session)._get_access_token() == "access2"
    expire_cached_access()
    session = Session([Resp(429)])
    with pytest.raises(ns.BhoonidhiError):
        ns.BhoonidhiClient(session=session)._get_access_token()
    assert len(session.post_calls) == 1


def test_cache_from_other_user_is_ignored():
    ns._save_token_cache({"userId": "other", "access_token": "theirs", "expires_at": time.time() + 999})
    session = Session([Resp(body=token_body())])
    assert ns.BhoonidhiClient(session=session)._get_access_token() == "access1"


def test_401_during_search_reauthorizes_once():
    session = Session([Resp(body=token_body()), Resp(body=token_body("access2"))], [Resp(401), page([feature()])])
    assert len(ns.BhoonidhiClient(session=session).search({"collections": ["c"]})) == 1
    assert session.api_calls[1]["headers"]["Authorization"] == "Bearer access2"


def test_persistent_401_raises():
    session = Session([Resp(body=token_body()), Resp(body=token_body("access2"))], [Resp(401), Resp(401, {"Description": "UNAUTHORIZED"})])
    with pytest.raises(ns.BhoonidhiError, match="UNAUTHORIZED"):
        ns.BhoonidhiClient(session=session).search({"collections": ["c"]})


@pytest.mark.parametrize("status,hint", [(401, "password"), (403, "sessions"), (429, "20 auth")])
def test_login_errors_explain_and_omit_password(status, hint):
    session = Session([Resp(status)])
    with pytest.raises(ns.BhoonidhiError) as e:
        ns.BhoonidhiClient(session=session)._get_access_token()
    assert hint in str(e.value) and SECRET not in str(e.value)


def test_verbose_never_prints_secrets(capsys):
    session = Session([Resp(body=token_body())], [page([feature()])])
    ns.processing(args_for("-v"), ns.BhoonidhiClient(session=session, verbose=True))
    out = capsys.readouterr()
    assert SECRET not in out.err + out.out and "access1" not in out.err + out.out


def test_netrc_prefers_api_host_and_accepts_portal_host(monkeypatch, tmp_path):
    path = tmp_path / "hosts"
    path.write_text("machine bhoonidhi.nrsc.gov.in login portal password p1\n")
    path.chmod(0o600)
    monkeypatch.setenv("NETRC", str(path))
    assert ns._read_netrc() == ("portal", "p1")
    path.write_text(f"machine bhoonidhi.nrsc.gov.in login portal password p1\nmachine {ns.API_HOST} login api password p2\n")
    assert ns._read_netrc() == ("api", "p2")


def test_no_credentials_noninteractive(monkeypatch, tmp_path):
    monkeypatch.setenv("NETRC", str(tmp_path / "missing"))
    monkeypatch.setattr(ns.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(ns.BhoonidhiError, match="machine bhoonidhi-api"):
        ns.BhoonidhiClient(session=Session())._get_access_token()


def test_netrc_parse_error_does_not_echo_contents(monkeypatch, tmp_path):
    bad = tmp_path / "bad"
    bad.write_text(f"{SECRET} garbage\n")
    bad.chmod(0o600)
    monkeypatch.setenv("NETRC", str(bad))
    with pytest.raises(ns.BhoonidhiError) as e:
        ns._read_netrc()
    assert SECRET not in str(e.value)


def test_logout_uses_refresh_token_and_removes_cache():
    body = token_body()
    ns.BhoonidhiClient(session=Session([Resp(body=body)]))._get_access_token()
    session = Session([Resp()])
    ns.BhoonidhiClient(session=session).logout()
    assert session.post_calls[0]["url"] == ns.LOGOUT_URL
    assert session.post_calls[0]["headers"]["Authorization"] == f"Bearer {body['refresh_token']}"
    assert not os.path.exists(ns.TOKEN_CACHE)


# --- Pagination and retries ----------------------------------------------------


def test_pagination_keeps_token_on_api_host():
    link = {"rel": "next", "href": "http://internal-stac:8080/search?token=abc&limit=500"}
    session = Session([Resp(body=token_body())], [page([feature("a")], link), page([feature("b")])])
    features = ns.BhoonidhiClient(session=session).search({"collections": ["c"]})
    assert [f["id"] for f in features] == ["a", "b"]
    assert all(c["url"] == ns.SEARCH_URL for c in session.api_calls)
    assert session.api_calls[1]["json"]["token"] == "abc"
    assert session.api_calls[1]["json"]["collections"] == ["c"]


def test_pagination_reads_post_style_link_body():
    link = {"rel": "next", "method": "POST", "href": ns.SEARCH_URL, "body": {"token": "xyz"}}
    session = Session([Resp(body=token_body())], [page([feature("a")], link), page([])])
    ns.BhoonidhiClient(session=session).search({})
    assert session.api_calls[1]["json"]["token"] == "xyz"


def test_unrecognised_pagination_raises_rather_than_truncating():
    link = {"rel": "next", "href": "https://x/search?cursor=1"}
    session = Session([Resp(body=token_body())], [page([feature("a")], link)])
    with pytest.raises(ns.BhoonidhiError, match="incomplete"):
        ns.BhoonidhiClient(session=session).search({})


def test_limit_sets_page_size_and_truncates():
    session = Session([Resp(body=token_body())], [page([feature(str(i)) for i in range(5)], {"rel": "next", "href": "h?token=t"})])
    features = ns.BhoonidhiClient(session=session).search({}, limit=3)
    assert len(features) == 3 and session.api_calls[0]["json"]["limit"] == 3


def test_page_size_capped_and_repeating_page_terminates(capsys):
    link = {"rel": "next", "href": "h?token=t"}
    session = Session([Resp(body=token_body())], [page([feature("a")], link), page([feature("a")], link)])
    assert len(ns.BhoonidhiClient(session=session).search({})) == 1
    assert session.api_calls[0]["json"]["limit"] == 500
    assert "incomplete" in capsys.readouterr().err


def test_no_results_404_is_an_empty_search():
    session = Session([Resp(body=token_body())])
    assert ns.BhoonidhiClient(session=session).search({}) == []


def test_no_results_404_ends_paging_and_keeps_earlier_pages():
    link = {"rel": "next", "href": "h?token=t"}
    session = Session([Resp(body=token_body())], [page([feature("a")], link), NO_RESULTS()])
    assert [f["id"] for f in ns.BhoonidhiClient(session=session).search({})] == ["a"]


def test_other_404_still_raises():
    session = Session([Resp(body=token_body())], [Resp(404, {"Description": "NOT FOUND"})])
    with pytest.raises(ns.BhoonidhiError, match="NOT FOUND"):
        ns.BhoonidhiClient(session=session).search({})


def test_empty_operational_collection_still_searches_beta():
    session = Session([Resp(body=token_body())], [NO_RESULTS(), page([feature("beta", "NISAR_SSAR-Beta_GCOV")])])
    bodies = [{"collections": ["op"]}, {"collections": ["beta"]}]
    assert [f["id"] for f in ns.BhoonidhiClient(session=session).search_requests(bodies)] == ["beta"]


def test_limit_spans_requests_and_stops_early():
    session = Session([Resp(body=token_body())], [page([feature("a"), feature("b")]), page([feature("c"), feature("d")])])
    features = ns.BhoonidhiClient(session=session).search_requests([{"collections": ["x"]}, {"collections": ["y"]}, {"collections": ["z"]}], limit=3)
    assert [f["id"] for f in features] == ["a", "b", "c"]
    assert [c["json"]["limit"] for c in session.api_calls] == [3, 1]


def test_transient_errors_retry_then_give_up():
    session = Session([Resp(body=token_body())], [Resp(429, headers={"Retry-After": "1"}), Resp(503), page([feature()])])
    assert len(ns.BhoonidhiClient(session=session).search({})) == 1
    session = Session([Resp(body=token_body())], [Resp(503)] * (ns.MAX_RETRIES + 1))
    ns._save_token_cache({"userId": "me", "access_token": "t", "expires_at": time.time() + 999})
    with pytest.raises(ns.BhoonidhiError, match="giving up"):
        ns.BhoonidhiClient(session=session).search({})


# --- Records and output --------------------------------------------------------


def test_record_from_standard_granule():
    rec = ns._feature_to_record(feature(GRANULE + ".h5", Online="Y"))
    assert (rec["track"], rec["frame"], rec["direction"], rec["product"]) == (172, 65, "D", "GCOV")
    assert rec["granule"] == GRANULE and rec["online"] == "Y"
    assert rec["url"] == f"{ns.DOWNLOAD_URL}?id={GRANULE}.h5&collection=NISAR_SSAR_GCOV"


def test_record_from_live_s_band_names():
    rec = ns._feature_to_record(feature("NISAR_S2_PR_GCOV_013_034_D_076_2500_CRNA_A_20260216T134101_20260216T134138_D00409_P_F_I_001", "NISAR_SSAR-Beta_GCOV", Node="Descending", Polarization="CP"))
    assert (rec["inst_level"], rec["track"], rec["frame"], rec["cycle"], rec["polarization"], rec["crid"]) == ("S2", 34, 76, 13, "CRNA", "D00409")


def test_record_from_pair_granule():
    rec = ns._feature_to_record(feature(PAIR, "NISAR_SSAR_GUNW"))
    assert (rec["cycle"], rec["cycle2"], rec["product"]) == (3, 5, "GUNW")


def test_record_falls_back_to_properties():
    rec = ns._feature_to_record(feature("N01_SSAR_X", Node="DESCENDING", polarization="DH", datetime="2026-01-02T03:04:05Z"))
    assert rec.get("track") is None and rec["direction"] == "D" and rec["polarization"] == "DH"
    assert rec["product"] == "GCOV" and rec["start_time"].day == 2


def records(*features):
    return [ns._feature_to_record(f) for f in features]


def test_latest_release_keeps_highest_crid():
    newer = GRANULE.replace("P05006", "P05007")
    kept = ns._filter_records(records(feature(GRANULE), feature(newer)), args_for("--track", "172"))
    assert [r["crid"] for r in kept] == ["P05007"]
    kept = ns._filter_records(records(feature(GRANULE), feature(newer)), args_for("--track", "172", "--allcrids"))
    assert len(kept) == 2


def test_unparsed_ids_are_not_collapsed_and_warned(capsys):
    kept = ns._filter_records(records(feature("x1"), feature("x2")), args_for("--product", "GCOV"))
    assert len(kept) == 2
    assert "not standard NISAR" in capsys.readouterr().err


def test_filters_and_online_only():
    other_track = GRANULE.replace("_172_", "_173_").replace("031851", "041851")
    recs = records(feature(GRANULE, Online="N"), feature(other_track, Online="Y"))
    assert len(ns._filter_records(recs, args_for("--track", "173"))) == 1
    kept = ns._filter_records(recs, args_for("--product", "GCOV", "--online_only"))
    assert [r["track"] for r in kept] == [173]


def test_urgent_response_excluded_by_default():
    ur = GRANULE.replace("_PR_", "_UR_")
    assert not ns._filter_records(records(feature(ur)), args_for("--product", "GCOV"))
    assert ns._filter_records(records(feature(ur)), args_for("--product", "GCOV", "-ur"))


@pytest.mark.parametrize("fmt", ["url", "csv", "json", "geojson", "kml"])
def test_all_formats(fmt, capsys):
    session = Session([Resp(body=token_body())], [page([feature(Online="N")])])
    ns.processing(args_for("--format", fmt), ns.BhoonidhiClient(session=session))
    captured = capsys.readouterr()
    assert GRANULE in captured.out and "offline" in captured.err
    if fmt == "csv":
        header = captured.out.splitlines()[0].split(",")
        assert header[:3] == ["granule", "collection", "online"] and "bucket" not in header
    if fmt == "kml":
        assert f"<name>{GRANULE}</name>" in captured.out


def test_group_output_to_directory(tmp_path):
    session = Session([Resp(body=token_body())], [page([feature(), feature("unparsed")])])
    out = tmp_path / "groups"
    ns.processing(args_for("--group", "-o", str(out)), ns.BhoonidhiClient(session=session))
    assert os.listdir(out) == ["NISAR_GCOV_172_D_065_20260121_20260121_bhoonidhi_urls.txt"]


def test_raw_returns_unmodified_features(capsys):
    session = Session([Resp(body=token_body())], [page([feature(Extra=1)])])
    ns.processing(args_for("--raw"), ns.BhoonidhiClient(session=session))
    assert json.loads(capsys.readouterr().out)["features"][0]["properties"] == {"Extra": 1}


def test_no_arguments_print_usage_only(capsys):
    with pytest.raises(SystemExit) as e:
        args_for()
    out = capsys.readouterr().out
    assert e.value.code == 0
    assert out.startswith("usage:") and "Examples:" not in out and "Spatial filters" not in out


def test_invalid_direction_rejected():
    with pytest.raises(SystemExit):
        args_for("--direction", "X")


# --- Granule selection and download --------------------------------------------

H5 = ns.HDF5_SIGNATURE + b"payload"
TOKEN_CACHE_OK = {"userId": "me", "access_token": "t", "expires_at": time.time() + 9999}


def body_resp(content, status=200, total=None):
    return Resp(status, headers={"Content-Length": str(total if total is not None else len(content))}, content=content)


def download_client(*responses):
    ns._save_token_cache(TOKEN_CACHE_OK)
    session = Session(api_responses=list(responses))
    return ns.BhoonidhiClient(session=session), session


def test_granule_selects_by_id_and_keeps_ur_and_product_from_name():
    ur = GRANULE.replace("_PR_", "_UR_")
    args = args_for("--granule", ur + ".h5", PAIR)
    bodies = ns._build_requests(args)
    assert [b["collections"][0] for b in bodies] == ["NISAR_SSAR_GCOV", "NISAR_SSAR_GUNW", "NISAR_SSAR-Beta_GCOV"]
    assert bodies[0]["filter"] == {"op": "in", "args": [{"property": "id"}, [ur, PAIR]]}


def test_granule_ur_survives_record_filter():
    ur = GRANULE.replace("_PR_", "_UR_")
    assert ns._filter_records(records(feature(ur)), args_for("--granule", ur))


def test_granule_keeps_each_named_release_and_product():
    newer = GRANULE.replace("P05006", "P05007")
    assert len(ns._filter_records(records(feature(GRANULE), feature(newer)), args_for("--granule", GRANULE, newer))) == 2
    assert ns._filter_records(records(feature(PAIR, "NISAR_SSAR_GUNW")), args_for("--granule", PAIR))


def test_download_writes_h5_and_skips_existing(tmp_path):
    client, session = download_client(body_resp(H5))
    path = client.download("G", "https://u", str(tmp_path))
    assert path == str(tmp_path / "G.h5")
    assert (tmp_path / "G.h5").read_bytes() == H5 and not (tmp_path / "G.h5.part").exists()
    assert client.download("G", "https://u", str(tmp_path)) == path
    assert len(session.api_calls) == 1


def test_download_resumes_partial_with_range(tmp_path):
    (tmp_path / "G.h5.part").write_bytes(H5[:4])
    client, session = download_client(body_resp(H5[4:], 206))
    client.download("G", "https://u", str(tmp_path))
    assert session.api_calls[0]["headers"]["Range"] == "bytes=4-"
    assert (tmp_path / "G.h5").read_bytes() == H5


def test_download_restarts_when_server_ignores_range(tmp_path):
    (tmp_path / "G.h5.part").write_bytes(b"stale")
    client, _ = download_client(body_resp(H5))
    client.download("G", "https://u", str(tmp_path))
    assert (tmp_path / "G.h5").read_bytes() == H5


def test_download_recovers_from_dropped_connection(tmp_path):
    dropped = Resp(200, headers={"Content-Length": str(len(H5))}, content=H5[:5], break_after=True)
    client, session = download_client(dropped, body_resp(H5[5:], 206))
    client.download("G", "https://u", str(tmp_path))
    assert session.api_calls[1]["headers"]["Range"] == "bytes=5-"
    assert (tmp_path / "G.h5").read_bytes() == H5


def test_download_resumes_after_short_stream(tmp_path):
    client, session = download_client(body_resp(H5[:5], total=len(H5)), body_resp(H5[5:], 206))
    client.download("G", "https://u", str(tmp_path))
    assert (tmp_path / "G.h5").read_bytes() == H5 and len(session.api_calls) == 2


def test_download_rejects_non_hdf5_and_keeps_no_final_file(tmp_path):
    client, _ = download_client(body_resp(b"<html>error</html>"))
    with pytest.raises(ns.BhoonidhiError, match="not an HDF5"):
        client.download("G", "https://u", str(tmp_path))
    assert not (tmp_path / "G.h5").exists()


def test_download_paces_requests(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(ns.time, "sleep", sleeps.append)
    client, _ = download_client(body_resp(H5), body_resp(H5))
    client.download("A", "https://u", str(tmp_path))
    client.download("B", "https://u", str(tmp_path))
    assert len(sleeps) == 1 and 0 < sleeps[0] <= ns.DOWNLOAD_MIN_INTERVAL


def test_download_checks_free_space(tmp_path, monkeypatch):
    monkeypatch.setattr(ns.shutil, "disk_usage", lambda d: type("U", (), {"free": 1})())
    client, _ = download_client(body_resp(H5))
    with pytest.raises(ns.BhoonidhiError, match="disk space"):
        client.download("G", "https://u", str(tmp_path))


def test_download_records_skips_offline_and_reports_failure(tmp_path, capsys):
    client, session = download_client(page([feature(Online="N")]))
    with pytest.raises(ns.BhoonidhiError, match="1 of 1"):
        ns.processing(args_for("--download", str(tmp_path / "d")), client)
    assert "offline" in capsys.readouterr().err
    assert all("download" not in c["url"] for c in session.api_calls)


def test_download_guard_requires_limit(tmp_path):
    feats = [feature(GRANULE.replace("_172_", f"_{i:03d}_")) for i in range(ns.DOWNLOAD_GUARD + 1)]
    client, session = download_client(page(feats))
    with pytest.raises(ns.BhoonidhiError, match="pass --limit"):
        ns.processing(args_for("--download", str(tmp_path / "d")), client)
    assert all("download" not in c["url"] for c in session.api_calls)
