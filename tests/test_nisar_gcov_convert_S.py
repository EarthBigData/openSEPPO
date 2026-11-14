"""seppo_nisar_gcov_convert_S on synthetic GCOV files in the CR and DH polarization modes."""

import glob
import os

import h5py
import numpy as np
import pytest
import rasterio

from openseppo.cli import nisar_gcov_convert_S as cli
from openseppo.nisar import nisar_tools_S as tools

CR = "NISAR_S2_PR_GCOV_031_077_D_084_2500_CRNA_A_20260923T132025_20260923T132056_P00500_M_P_I_001.h5"
DH = "NISAR_S2_UR_GCOV_035_012_A_006_3700_DHNA_A_20261106T002343_20261106T002416_P00500_M_P_I_001.h5"
DH_PR = "NISAR_S2_PR_GCOV_031_012_A_006_3700_DHNA_A_20260919T002342_20260919T002415_P00500_M_P_I_001.h5"
SIZE = 64

# Real granules, used when NISAR_S_EXAMPLES names a directory holding them (seppo_nisar_search_S
# --granule ... --download DIR).  Window: (xoff, yoff, size); terms: num, den, cross, ratio token.
REAL_GRANULES = {
    CR: ((8000, 8000, 512), ("RHRH", "RVRV", "RHRV", "rhrvra")),
    DH_PR: ((16000, 16000, 512), ("HHHH", "HVHV", "HHHV", "hhhvra")),
}


def write_gcov(path, like, cross, term):
    rng = np.random.default_rng(1)
    num = rng.uniform(0.01, 1.0, (SIZE, SIZE)).astype(np.float32)
    den = rng.uniform(0.01, 1.0, (SIZE, SIZE)).astype(np.float32)
    num[0, 0], den[0, 0] = 300.0, 1.0  # numerator saturates the dB and AMP bands
    num[0, 1], den[0, 1] = 1.0, 300.0  # denominator saturates them
    num[1, 0] = np.nan  # nodata
    with h5py.File(path, "w") as f:
        ident = f.create_group("science/SSAR/identification")
        ident["boundingPolygon"] = "POLYGON ((77 9, 77.1 9, 77.1 9.1, 77 9.1, 77 9))"
        ident["zeroDopplerStartTime"] = "2026-09-23T13:20:25.000000"
        g = f.create_group("science/SSAR/GCOV/grids/frequencyA")
        g["xCoordinates"] = 500000.0 + 20.0 * np.arange(SIZE)
        g["yCoordinates"] = 1000000.0 - 20.0 * np.arange(SIZE)
        g["projection"] = np.uint32(32643)
        g[like] = num
        g[cross] = den
        g[term] = (num * 0.5 + 0.1j).astype(np.complex64)
        g["rtcGammaToSigmaFactor"] = np.full((SIZE, SIZE), 0.5, dtype=np.float32)
        exc = np.zeros((SIZE, SIZE), dtype=np.uint8)
        exc[2, 2] = 64
        exc[3, 3] = 255
        g["inputDataExceptionMask"] = exc
    return num, den


@pytest.fixture
def granules(tmp_path):
    cr, dh, dh_pr = tmp_path / CR, tmp_path / DH, tmp_path / DH_PR
    return {
        "DH_PR": (str(dh_pr), *write_gcov(dh_pr, "HHHH", "HVHV", "HHHV"), "hh", "hv", "hhhvra"),
        "CR": (str(cr), *write_gcov(cr, "RHRH", "RVRV", "RHRV"), "rh", "rv", "rhrvra"),
        "DH": (str(dh), *write_gcov(dh, "HHHH", "HVHV", "HHHV"), "hh", "hv", "hhhvra"),
    }


def convert(out, *args):
    cli._main(["seppo_nisar_gcov_convert_S", *args, "-o", f"{out}/"])


def read(out, suffix):
    paths = glob.glob(f"{out}/*{suffix}")
    assert len(paths) == 1, (suffix, os.listdir(out))
    with rasterio.open(paths[0]) as d:
        return d.read(1)


@pytest.mark.parametrize("mode", ["CR", "DH", "DH_PR"])
def test_default_variables_follow_polarization_mode(granules, tmp_path, mode):
    path, num, _, like, cross, _ = granules[mode]
    convert(tmp_path / "o", "-i", path, "-pwr")
    valid = np.isfinite(num)
    assert np.allclose(read(tmp_path / "o", f"_{like}_pwr.tif")[valid], num[valid])
    assert read(tmp_path / "o", f"_{cross}_pwr.tif").shape == (SIZE, SIZE)


def test_mixed_mode_batch_uses_each_files_variables(granules, tmp_path):
    convert(tmp_path / "o", "-i", granules["CR"][0], granules["DH"][0], "-dB", "-dpratio")
    for mode in ("CR", "DH"):
        assert read(tmp_path / "o", f"{mode}NA_A_{'20260923T132025' if mode == 'CR' else '20261106T002343'}*{granules[mode][-1]}_dB.tif").shape == (SIZE, SIZE)


@pytest.mark.parametrize("mode", ["CR", "DH", "DH_PR"])
def test_ratio_db_is_exact_where_bands_saturate(granules, tmp_path, mode):
    path, num, den, like, _, token = granules[mode]
    convert(tmp_path / "o", "-i", path, "-dB", "-dpratio")
    ratio = read(tmp_path / "o", f"_{token}_dB.tif")
    valid = np.isfinite(num)
    assert np.allclose(ratio[valid], (10 * np.log10(num / den))[valid], atol=1e-3)
    assert ratio[0, 0] == pytest.approx(24.77, abs=0.01) and ratio[0, 1] == pytest.approx(-24.77, abs=0.01)
    assert np.isnan(ratio[1, 0])
    assert read(tmp_path / "o", f"_{like}_dB.tif")[0, 0] == pytest.approx(13.3)  # the band itself clips


@pytest.mark.parametrize(
    "flag,name,dtype,expected,nodata",
    [
        ("-pwr", "pwr", np.float32, 300.0, None),
        ("-amp", "AMP", np.uint16, np.sqrt(300.0) * 1000, 0),
        ("-DN", "DN", np.uint8, 255, 0),
    ],
)
def test_ratio_in_other_modes(granules, tmp_path, flag, name, dtype, expected, nodata):
    path, *_, token = granules["DH"]
    convert(tmp_path / "o", "-i", path, flag, "-dpratio")
    ratio = read(tmp_path / "o", f"_{token}_{name}.tif")
    assert ratio.dtype == dtype
    assert ratio[0, 0] == pytest.approx(expected, rel=1e-3)
    if nodata is not None:
        assert ratio[1, 0] == nodata


def test_ratio_adds_a_term_missing_from_vars(granules, tmp_path):
    path, *_, like, cross, token = granules["DH"]
    convert(tmp_path / "o", "-i", path, "-dB", "-dpratio", "-vars", "HHHH")
    for suffix in (like, cross, token):
        read(tmp_path / "o", f"_{suffix}_dB.tif")


def test_cross_term_has_its_own_file_alongside_ratio(granules, tmp_path):
    path, num, *_ = granules["DH"]
    convert(tmp_path / "o", "-i", path, "-pwr", "-dpratio", "-vars", "HHHH", "HVHV", "HHHV")
    valid = np.isfinite(num)
    assert np.allclose(read(tmp_path / "o", "_hhhv_pwr.tif")[valid], np.abs(num * 0.5 + 0.1j)[valid], rtol=1e-5)
    assert np.allclose(read(tmp_path / "o", "_hh_pwr.tif")[valid], num[valid])


def test_ratio_rejects_single_pol(tmp_path):
    single = tmp_path / CR.replace("CRNA", "SHNA")
    write_gcov(single, "HHHH", "HVHV", "HHHV")
    convert(tmp_path / "o", "-i", str(single), "-dB", "-dpratio")
    assert not glob.glob(f"{tmp_path}/o/*.tif")


def test_exception_mask_is_ancillary_and_downscales_by_or(granules, tmp_path):
    path, *_ = granules["DH"]
    convert(tmp_path / "o", "-i", path, "-vars", "HHHH", "inputDataExceptionMask", "-d", "2")
    mask = read(tmp_path / "o", "_exceptionmask.tif")
    assert mask.dtype == np.uint8 and mask[1, 1] == 255 and mask[0, 0] == 0
    convert(tmp_path / "o1", "-i", path, "-vars", "inputDataExceptionMask", "-d", "4")
    assert read(tmp_path / "o1", "_exceptionmask.tif")[0, 0] == 255


def test_reprojection_keeps_bright_targets(granules, tmp_path):
    path, *_ = granules["DH"]
    convert(tmp_path / "o", "-i", path, "-pwr", "-t_srs", "EPSG:4326", "--resample", "nearest")
    assert np.nanmax(read(tmp_path / "o", "_hh_pwr.tif")) > 100


def test_s3_reads_use_block_cache(tmp_path):
    path = tmp_path / "a.h5"
    with h5py.File(path, "w") as f:
        f["x"] = [1]
    seen = {}

    class FakeFS:
        def open(self, url, **kwargs):
            seen.update(kwargs)
            return open(path, "rb")

    with tools.open_h5_lazy("s3://bucket/a.h5", FakeFS()) as f:
        assert f["x"][0] == 1
    assert seen["cache_type"] == "blockcache"
    assert seen["block_size"] == 4 * 1024 * 1024



@pytest.mark.parametrize(
    "variables,expected",
    [
        (["RHRH", "RHRV", "RVRV", "mask", "numberOfLooks"], ["RHRH", "RVRV"]),
        (["HHHH", "HHHV", "HVHV", "mask"], ["HHHH", "HVHV"]),
        (["VHVH", "VVVH", "VVVV"], ["VVVV", "VHVH"]),
        (["HHHH", "mask"], ["HHHH"]),
        (["mask"], []),
    ],
)
def test_default_backscatter_vars(variables, expected):
    assert tools._default_backscatter_vars(variables) == expected


@pytest.mark.parametrize("var,token", [("RHRH", "rh"), ("HVHV", "hv"), ("RHRV", "rhrv"), ("HHHV", "hhhv"), ("mask", "ma")])
def test_pol_token(var, token):
    assert tools._pol_token(var) == token


def test_same_track_ur_and_pr_scenes_stack(granules, tmp_path):
    convert(tmp_path / "o", "-i", granules["DH"][0], granules["DH_PR"][0], "-dB", "-dpratio")
    stacks = glob.glob(f"{tmp_path}/o/*20260919*20261106*hh_dB.vrt")
    assert len(stacks) == 1, os.listdir(tmp_path / "o")
    with rasterio.open(stacks[0]) as d:
        assert d.count == 2


@pytest.mark.parametrize("name", list(REAL_GRANULES), ids=["CRNA-PR", "DHNA-PR"])
def test_real_granule_against_source(name, tmp_path):
    path = os.path.join(os.environ.get("NISAR_S_EXAMPLES", ""), name)
    if not os.path.exists(path):
        pytest.skip("real granule not available; set NISAR_S_EXAMPLES")
    (xoff, yoff, size), (num_var, den_var, cross_var, token) = REAL_GRANULES[name]
    window = ["-srcwin", str(xoff), str(yoff), str(size), str(size)]
    with h5py.File(path) as f:
        g = f["science/SSAR/GCOV/grids/frequencyA"]
        num, den, cross = (g[v][yoff:yoff + size, xoff:xoff + size] for v in (num_var, den_var, cross_var))
    valid = np.isfinite(num) & np.isfinite(den) & (num > 0) & (den > 0)
    assert valid.any()

    convert(tmp_path / "pwr", "-i", path, *window, "-pwr", "-dpratio", "-vars", num_var, den_var, cross_var)
    assert np.allclose(read(tmp_path / "pwr", f"_{tools._pol_token(num_var)}_pwr.tif")[valid], num[valid], rtol=1e-6)
    assert np.allclose(read(tmp_path / "pwr", f"_{tools._pol_token(cross_var)}_pwr.tif")[valid], np.abs(cross)[valid], rtol=1e-5)
    assert np.allclose(read(tmp_path / "pwr", f"_{token}_pwr.tif")[valid], (num / den)[valid], rtol=1e-5)

    convert(tmp_path / "db", "-i", path, *window, "-dB", "-dpratio")
    assert np.allclose(read(tmp_path / "db", f"_{token}_dB.tif")[valid], (10 * np.log10(num / den))[valid], atol=1e-3)
