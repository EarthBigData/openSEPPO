# NISAR GUNW -- Search-to-COG and Stack Examples

From `seppo_nisar_search` to interferogram COGs, and to a time-series VRT stack
of them, using track **162 ascending, frame 007**.

Everything streams straight from the ASF DAAC over **HTTPS** -- no granule
download. All you need is an Earthdata login in your `~/.netrc`
(see [`seppo_earthaccess_credentials`](earthaccess_credentials_cli.md)).

Every command below was run as shown; the numbers are the actual output.

A GUNW granule is a **pair**: one granule holds the interferogram between a
reference and a secondary acquisition, and its name carries both acquisition
times.

---

## Two minimal examples

Copy-pasteable, and both run anywhere over HTTPS.

### Example 1 -- one pair, subset to an AOI

```bash
seppo_nisar_search --product GUNW \
    --point -68.60 10.30 --track 162 \
    --https --format url -o gunw_urls.txt
```

That returns 23 chained pairs for this track and frame. Pick one by its cycle
and frame fields:

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

Output stays on the granule's native UTM grid (EPSG:32619, 80 m), which is what
lets pairs from the same track and frame stack without resampling. Add `-t_srs`
and `-tr` to reproject.

`-vars` selects layers within the group; omitted, the group's default set is
written (`unwrappedPhase`, `coherenceMagnitude`, `connectedComponents`).

### Example 2 -- a stack of pairs, with the time-series VRT

The same command over several pairs -- cycles 021 to 025, references spanning
2026-06-01 to 2026-07-19:

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

  band 1   reference 2026-06-01
  band 2   reference 2026-06-13
  band 3   reference 2026-06-25
  band 4   reference 2026-07-07
  band 5   reference 2026-07-19
```

Every band is bit-for-bit the COG of its own pair -- the VRT references them, it
does not resample or recompute. **The tool does not combine the bands**: each
one is a single pair's interferogram, and nothing is summed or differenced.

Drop `-vars unwrappedPhase` to stack the group's whole default set, one VRT per
layer.

---

## Notes

**`-lg` first.** GUNW is three independent grids at two resolutions:

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
and cannot share a raster. `-of h5` carries all three at once, each windowed on
its own axes.

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

**Pairs that do not share a window are stacked on their union.** Pairs from one
track and frame resolve to the identical window, so they stack directly. If one
only partly covers the requested box, the whole stack falls back to the union
extent and says so on stdout.

**`--no_time_series`** disables the stacking; `--no_vrt` disables the per-pair
multi-layer VRT. The stack VRT stores relative paths, so move the directory as a
whole.

**Size.** The five-pair stack above is 107 MB for one layer over a 275 x 145 km
window at 80 m. Adding `coherenceMagnitude` roughly doubles it. The 20 m
`wrappedInterferogram` grid is 16x the pixels of the other two and complex, so
it dominates an `-of h5` subset; `-groups unwrappedInterferogram pixelOffsets`
leaves it out. Measured on a smaller 688 x 695 window, that took the subset from
**98.8 MB to 22.1 MB** (and 4m32s to 3m05s over HTTPS from a laptop), with the
orbit, attitude and metadata groups unchanged.

---

## Related

- [`seppo_nisar_gunw_convert` CLI Reference](nisar_gunw_convert_cli.md)
- [`seppo_nisar_search` CLI Reference](nisar_search_cli.md)
- [`seppo_nisar_sme2_convert` examples](nisar_sme2_convert_examples.md) -- the
  same two-example shape for a geocoded L3 product
