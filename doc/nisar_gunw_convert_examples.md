# NISAR GUNW Interferometry -- Venezuela Coseismic Example

From `seppo_nisar_search` to a deformation interferogram and to a stack of them,
over the **Venezuela M7.2 / M7.5 earthquakes of 2026-06-24** seen by track
**162 ascending, frame 007**.

Everything streams straight from the ASF DAAC over **HTTPS** -- no granule
download. All you need is an Earthdata login in your `~/.netrc`
(see [`seppo_earthaccess_credentials`](earthaccess_credentials_cli.md)).

Every command below was run as shown; the numbers are the actual output.

A GUNW granule is a **pair**, not a date: each one is a 12-day interferogram
between a reference and a secondary acquisition, and its name carries both
acquisition times. The most interesting layer is `unwrappedPhase` -- line-of-sight
ground motion, in radians.

---

## Two minimal examples

Copy-pasteable, and both run anywhere over HTTPS. The notes afterwards explain
the choices.

### Example 1 -- one pair, subset to the deformation field

```bash
seppo_nisar_search --product GUNW \
    --point -68.60 10.30 --track 162 \
    --https --format url -o gunw_urls.txt
```

That returns 23 chained pairs for this track and frame. The one spanning the
earthquake is the `20260613 -> 20260625` pair:

```bash
grep _022_162_A_007_023_ gunw_urls.txt > gunw_co.txt

seppo_nisar_gunw_convert -i gunw_co.txt -o gunw_co_out/ \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326 \
    -vars unwrappedPhase coherenceMagnitude
```

```
---> Detected Earthdata HTTPS URL. Using Earthdata credentials.
    unwrappedInterferogram/HH window col=564 row=1583 w=3441 h=1814 @ EPSG:32619

Processed 1/1 GUNW file(s).                                       17.3 s
```

```
gunw_co_out/
  <pair>-EBD_A_unw_unwphase.tif        20 MB   unwrapped phase (radians)
  <pair>-EBD_A_unw_coh.tif             23 MB   coherence magnitude
  <pair>-EBD_A_unw_unwphasecoh.vrt      1 KB   the two as one 2-band file
```

Masked to coherent pixels (`coherenceMagnitude > 0.3`, 49.7% of the window) and
scaled to line of sight by the granule's own wavelength, that pair carries
**54.7 cm peak to peak**, -43.1 to +48.0 cm -- two elongated lobes along the
rupture.

Output stays on the granule's native UTM grid (EPSG:32619, 80 m), which is what
lets pairs from the same track and frame stack without resampling.

### Example 2 -- a stack of pairs, with the time-series VRT

The same command over the chained pairs either side of the event -- cycles 021
to 025, spanning 2026-06-01 to 2026-08-12:

```bash
grep -E "_02[1-5]_162_A_007_" gunw_urls.txt > gunw_series.txt

seppo_nisar_gunw_convert -i gunw_series.txt -o gunw_ts/ \
    -projwin -69.4 11.2 -66.9 9.9 -projwin_srs 4326 \
    -vars unwrappedPhase
```

```
    unwrappedInterferogram/HH window col=564 row=1583 w=3441 h=1814 @ EPSG:32619
    ... x5, the identical window on every pair

  --> time-series VRT: <...20260601T000000_20260719T235959...>-EBD_A_unw_unwphase.vrt (5 dates)

Processed 5/5 GUNW file(s); 1 time-series VRT(s) over 5 pairs.    47.8 s
```

```
gunw_ts/
  <...20260601T000000_20260719T235959...>-EBD_A_unw_unwphase.vrt     the stack
  <...20260601T000000_20260719T235959...>-EBD_A_unw_unwphase.dates   its band dates
  <...20260601T100657_..._20260613T100731...>-EBD_A_unw_unwphase.tif one COG per pair
  ... 4 more
```

The `.vrt` is the stack: one band per pair, ordered by reference acquisition,
each band described and tagged with that date. It opens as an ordinary 5-band
raster in GDAL, QGIS, rasterio or xarray:

```
5 bands   3441 x 1814   EPSG:32619   float32   nodata nan

  band 1   2026-06-01 -> 06-13    pre-seismic
  band 2   2026-06-13 -> 06-25    COSEISMIC -- contains the 06-24 rupture
  band 3   2026-06-25 -> 07-07    post-seismic
  band 4   2026-07-07 -> 07-19
  band 5   2026-07-19 -> 08-12    (24-day gap, cycle 026 missing)
```

**Each band is an independent 12-day interferogram, not a cumulative
displacement.** Summing a chain into cumulative motion needs a reference point
and unwrapping-error handling; that is a science step this converter
deliberately leaves to you. What it guarantees is that the bands are on one
grid, in time order, ready for it.

---

## Notes

**Choosing the window matters more than for a geocoded product.** A box too
small to contain the deformation field shows it as a ramp and hides it in the
atmosphere. The same coseismic pair over a tight `-projwin -68.85 10.55 -68.35
10.05` box (688 x 695) is indistinguishable from its neighbours -- 21.5 cm peak
to peak against 10-16 cm for the non-seismic pairs, with no visible lobes. Over
the wide box above, the fringes are unmistakable. Subset to the deformation, not
to the epicentre.

**The non-seismic bands are not empty, and that is not a bug.** Referenced to
the scene median without a coherence mask, all five bands in the stack carry 33
to 70 cm of peak-to-peak signal, most of it tropospheric and ionospheric path
delay rather than ground motion. L-band over the humid tropics is where that is
worst. The coseismic band is distinguished by a *sharp fault-parallel
discontinuity*, not by amplitude alone. Use `coherenceMagnitude` to mask and
`ionospherePhaseScreen` (in the same group, see `-lg`) to correct.

**`-lg` first, always.** GUNW is three independent grids at two resolutions:

```
--- Frequency A  (polarizations: ['HH']) ---
  unwrappedInterferogram/  [EPSG:32619, 4239x4149, 80m]  pols=['HH']
      unwrappedPhase                     float32    nodata=nan  -> token 'unwphase'
      coherenceMagnitude                 float32    nodata=nan  -> token 'coh'
      connectedComponents                uint16     nodata=65535  -> token 'concomp'
      ionospherePhaseScreen              float32    nodata=nan  -> token 'iono'
      ...
  pixelOffsets/            [EPSG:32619, 4239x4149, 80m]  pols=['HH']
  wrappedInterferogram/    [EPSG:32619, 16956x16596, 20m]  pols=['HH']
```

Raster output is one grid per run, chosen with `--layer_group` (default
`unwrappedInterferogram`), because the three grids have different resolutions
and cannot share a raster. `-of h5` carries all three at once.

**Grep the pair, not the date.** A GUNW name contains two datetime pairs, so
`grep 20260613` matches both the pair that *starts* on that date and the one
that *ends* on it. Match the cycle and frame fields (`_022_162_A_007_023_`)
instead. For the same reason the stack VRT's name collapses both datetime pairs
into one series span -- `20260601T000000_20260719T235959` -- rather than leaving
one granule's secondary times stranded in it.

**The stack span is over reference acquisitions.** Band 5's reference is
2026-07-19, so the filename ends there, while that pair's secondary reaches
2026-08-12. The `.dates` sidecar lists the same reference dates the bands are
labelled with.

**`--no_time_series`** disables the stacking; `--no_vrt` disables the per-pair
multi-layer VRT. The stack VRT stores relative paths, so move the directory as a
whole.

**Size.** The five-pair stack above is 107 MB for one layer over a 275 x 145 km
window at 80 m. Adding `coherenceMagnitude` roughly doubles it. The 20 m
`wrappedInterferogram` grid is 16x the pixels of the other two and complex --
avoid it unless you need the fringes themselves.

---

## Related

- [`seppo_nisar_gunw_convert` CLI Reference](nisar_gunw_convert_cli.md)
- [`seppo_nisar_search` CLI Reference](nisar_search_cli.md)
- [`seppo_nisar_sme2_convert` examples](nisar_sme2_convert_examples.md) -- the
  same two-example shape for a geocoded L3 product
