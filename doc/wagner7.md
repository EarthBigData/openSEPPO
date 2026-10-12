# seppo_nisar_gcov_convert

## Authalic Wagner VII (`-t_srs wagner7`)

Wagner VII applied to (longitude, **authalic latitude**) on the WGS84 **authalic sphere**, the sphere with the same
surface area as the WGS84 ellipsoid (R = 6,371,007.181 m). The result is equal-area with respect to WGS84, on the
same area reference as the EASE-Grid 2.0 polar grids (EPSG:6931 / 6932).

```bash
seppo_nisar_gcov_convert -i file.h5 -o out/ -dB -dpratio --no_single_bands \
    -t_srs wagner7 -tr 1000 1000 --resample average
```

| Requirement | Why |
|---|---|
| `-tr XRES YRES` in metres | There is no natural output pixel size to fall back on. |
| `--resample average` or `nearest` | `average` for `-tr` coarser than the native posting, `nearest` otherwise. |
| No `-projwin` | Use `-srcwin`. |
| Scene does not cross the antimeridian | Its bounding box would span the whole map; such scenes are rejected. |

### Why not ESRI:54076

ESRI:54076 (`World_Wagner_VII`) applies Wagner VII to geodetic latitude on a sphere of radius a. It is equal-area on
that sphere but not on WGS84. Authalic latitude on the authalic sphere (Snyder 1987, eqs. 3-11, 3-12) is how
EPSG:6931 / 6932 / 6933 are equal-area on the ellipsoid.

| Latitude band | Area vs WGS84, authalic Wagner VII | Area vs WGS84, ESRI:54076 |
|---|---|---|
| 60°–90° | +0.0002 % | −0.498 % |
| 30°–60° | +0.0002 % | +0.031 % |
| 0°–30° | +0.0002 % | +0.562 % |

The uniform +0.0002 % (1.6 ppm) is in PROJ's `wag7` itself, from the rounded constants in its formulas.

### How it is warped

GDAL cannot warp to this projection: its warper maps each output pixel back to the source, and PROJ's `wag7` has no
inverse. `openseppo.nisar.wagner7` warps the same linear-power arrays GDAL would receive, before dB, AMP, DN or ratio
conversion:

| `--resample` | Method |
|---|---|
| `average` | Every valid source pixel centre is projected and averaged into the output cell it falls in. |
| `nearest` | Each output pixel centre is taken back to the source pixel it lands in. |

Ancillary grids keep their rules (`mask` nearest, `numberOfLooks` summed, `rtcGammaToSigmaFactor` averaged). The
[automatic pre-downscale](nisar_gcov_convert_examples.md#-tr-and-automatic-pre-downscaling) stops at a quarter of
`-tr`, so each output cell still collects about 16 pixels. With pixel alignment on (the default), output pixel edges sit
at multiples of `-tr` from projected (0, 0), so separately processed scenes mosaic without another resampling.

### Output CRS

Files carry `+proj=wag7 +lon_0=0 +R=6371007.181`; there is no EPSG or ESRI code for authalic Wagner VII. Reprojecting a
file with that CRS alone treats authalic latitude as geodetic latitude, up to 0.13° (about 14 km) off. The
`WAGNER7_AUTHALIC` tag records the convention and the forward PROJ pipeline, and `openseppo.nisar.wagner7.inverse()`
converts Wagner VII metres back to WGS84 longitude and latitude.
