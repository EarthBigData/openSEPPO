# seppo_nisar_gcov_convert

## Dual-pol Ratio (`-dpratio` / `--dualpol_ratio`)

Computes a polarimetric ratio between the like-pol and cross-pol channels.
Requires a **DH**, **DV** or **QP** acquisition; single-pol modes are rejected with a warning.

| Polarization | Like-pol | Cross-pol | Ratio band name |
|---|---|---|---|
| DH (freq A) | HHHH | HVHV | HHHH/HVHV |
| DV (freq A) | VVVV | VHVH | VVVV/VHVH |
| QP, `-vars` omitted | HHHH | HVHV | HHHH/HVHV |
| QP, `-vars` given | first polarization in `-vars` | second polarization in `-vars` | e.g. VVVV/VHVH |

The individual like-pol and cross-pol bands are always written alongside the ratio.

### Quad-pol (QP) input

- Without `-vars`, only HHHH and HVHV are extracted, and the ratio is HHHH/HVHV.
- With `-vars`, every listed grid is extracted, and the ratio is the first listed polarization divided by the second (ancillary grids in the list are skipped when counting). `-vars VVVV VHVH HHHH` writes VVVV, VHVH and HHHH plus the ratio VVVV/VHVH.
- Both ratio terms must be diagonal (power) terms such as HHHH, HVHV, VVVV or VHVH. An off-diagonal complex term (e.g. HHHV, HHVV) in the first two places skips the granule with a warning.
- QP band files keep their full four-letter names (`hhhh`, `hvhv`, ...), as they do without `-dpratio`. The ratio file is named from the two-letter prefixes of the two terms, e.g. `hhhvra` or `vvvhra`.

`-dpratio` with exactly one polarization in `-vars` (e.g. `-vars HHHH`) is an error, for any acquisition mode.

### Output files (single-bands, DH example)

| File | Content |
|---|---|
| `...-EBD_A_hh_<MODE>.tif` | Like-pol (HHHH) |
| `...-EBD_A_hv_<MODE>.tif` | Cross-pol (HVHV) |
| `...-EBD_A_hhhvra_<MODE>.tif` | Ratio band |
| `...-EBD_A_hhhvra_<MODE>.vrt` | 3-band snapshot VRT |

### Output files (single-bands, QP example, `-vars` omitted)

| File | Content |
|---|---|
| `...-EBD_A_hhhh_<MODE>.tif` | HHHH |
| `...-EBD_A_hvhv_<MODE>.tif` | HVHV |
| `...-EBD_A_hhhvra_<MODE>.tif` | Ratio band HHHH/HVHV |
| `...-EBD_A_hhhhhvhvhhhvra_<MODE>.vrt` | 3-band snapshot VRT |

With `--no_single_bands` the output is a single 3-band COG (band 1 = like-pol, band 2 = cross-pol, band 3 = ratio), useful for browse images (e.g. `-DN -d 20 --no_single_bands -dpratio`).

### Ratio formula per output mode

| Mode | Formula | Output dtype | Nodata | Clamp |
|---|---|---|---|---|
| `-pwr` (default) | `likepol / crosspol` | float32 | nan | -- |
| `-amp` | `amp(likepol) / amp(crosspol) x 1000` | uint16 | 0 | [1, 65535] |
| `-DN` | `DN(likepol) - DN(crosspol) + 127` | uint8 | 0 | [1, 255] |
| `-dB` | `dB(likepol) - dB(crosspol)` | float32 | nan | -- |

For `-amp` and `-DN` the nodata mask is determined before clamping (pixels where the denominator or either input is 0 or non-finite).
Clamping is then applied to valid pixels and nodata pixels are set to 0 last, so clamping never accidentally promotes a valid 0 to 1.
