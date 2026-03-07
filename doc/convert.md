# seppo_nisar_gcov_convert

## Dual-pol Ratio (`-dpratio` / `--dualpol_ratio`)

Computes a polarimetric ratio between the like-pol and cross-pol channels.
Requires a **DH** or **DV** dual-pol acquisition; QP and single-pol modes are rejected with a warning.

| Polarization | Like-pol | Cross-pol | Ratio band name |
|---|---|---|---|
| DH (freq A) | HHHH | HVHV | HHHH/HVHV |
| DV (freq A) | VVVV | VHVH | VVVV/VHVH |

The individual like-pol and cross-pol bands are always written alongside the ratio.

### Output files (single-bands, DH example)

| File | Content |
|---|---|
| `…-EBD_A_hh_<MODE>.tif` | Like-pol (HHHH) |
| `…-EBD_A_hv_<MODE>.tif` | Cross-pol (HVHV) |
| `…-EBD_A_hhhvra_<MODE>.tif` | Ratio band |
| `…-EBD_A_hhhvra_<MODE>.vrt` | 3-band snapshot VRT |

With `--no_single_bands` the output is a single 3-band COG (band 1 = like-pol, band 2 = cross-pol, band 3 = ratio), useful for browse images (e.g. `-DN -d 20 --no_single_bands -dpratio`).

### Ratio formula per output mode

| Mode | Formula | Output dtype | Nodata | Clamp |
|---|---|---|---|---|
| `-pwr` (default) | `likepol / crosspol` | float32 | nan | — |
| `-amp` | `amp(likepol) / amp(crosspol) × 1000` | uint16 | 0 | [1, 65535] |
| `-DN` | `DN(likepol) − DN(crosspol) + 127` | uint8 | 0 | [1, 255] |
| `-dB` | `dB(likepol) − dB(crosspol)` | float32 | nan | — |

For `-amp` and `-DN` the nodata mask is determined before clamping (pixels where the denominator or either input is 0 or non-finite).
Clamping is then applied to valid pixels and nodata pixels are set to 0 last, so clamping never accidentally promotes a valid 0 to 1.
