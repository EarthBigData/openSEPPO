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

For polar regions, the polar Lambert Azimuthal Equal Area grids EPSG:6931 (north) and EPSG:6932 (south), or
EPSG:3571–3576 (north, with different central meridians), are an equal-area alternative that GDAL warps directly,
without these restrictions; see [Equal-area projections](nisar_gcov_convert_examples.md#equal-area-projections).

> **Viewing the outputs in QGIS:** QGIS shows these files as *Unknown CRS* until the CRS is added once as a custom
> projection; see [Adding the Wagner VII CRS to QGIS](#adding-the-wagner-vii-crs-to-qgis) for the WKT to paste
> (`seppo_wagner7_vector --wkt` prints it too). For coastlines, footprints or lat/lon grid lines, see
> [Vector overlays and grid lines](#vector-overlays-and-grid-lines-seppo_wagner7_vector).

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
`-tr`, so each output cell still collects about 16 pixels. Toward the poles Wagner VII squeezes cells along one axis by
the Tissot scale factor *a*, and a cell only gets a value if a source pixel centre lands in it, so the pre-downscale
stops at `-tr / (2 a)` where that is finer, *a* being the largest over the scene:

| Latitude | *a* (max) | Pre-downscale stops at |
|---|---|---|
| up to 75° | ≤ 2 | `-tr` / 4 |
| 80° | 2.7 | `-tr` / 5.4 |
| 85° | 5.2 | `-tr` / 10.4 |
| 87° | 8.6 | `-tr` / 17 |

This cannot go finer than the native posting. With pixel alignment on (the default), output pixel edges sit
at multiples of `-tr` from projected (0, 0), so separately processed scenes mosaic without another resampling.

### Output CRS

There is no EPSG or ESRI code for authalic Wagner VII, so files carry a named WKT2 CRS, `WGS 84 authalic sphere /
Wagner VII (authalic latitude)`: Wagner VII, `lon_0` 0, on a sphere of radius 6,371,007.181 m. GeoTIFF has no geokey
for Wagner VII, so GDAL stores it as an ESRI PE string and reads the names back as
`WGS_84_authalic_sphere_Wagner_VII_authalic_latitude`, `GCS_WGS_84_authalic_sphere`, and so on. Files written before
this carry `+proj=wag7 +lon_0=0 +R=6371007.181`, the same CRS without names, which `gdalinfo` lists as `unknown`.
`is_wagner7()` recognises both forms by their content. Reprojecting a file with this CRS alone treats authalic latitude as geodetic latitude, up to 0.13° (about 14 km) off. The
`WAGNER7_AUTHALIC` tag records the convention and the forward PROJ pipeline, and `openseppo.nisar.wagner7.inverse()`
converts Wagner VII metres back to WGS84 longitude and latitude.

### Adding the Wagner VII CRS to QGIS

QGIS names a CRS only when it has an EPSG or ESRI code or is in the user's own CRS list, so Wagner VII outputs show as
`Unknown CRS: WGS_84_authalic_sphere_Wagner_VII_authalic_latitude`. The layers are still drawn in the right place. To
give the CRS a name, add it once as a custom projection:

1. **Settings → Custom Projections…**, then **+** (Add new CRS).
2. **Name**: `Wagner VII authalic (WGS84)`.
3. **Format**: `WKT (Recommended)`.
4. Paste the WKT below into **Parameters**, click **Validate**, then **OK**.

```
PROJCRS["WGS 84 authalic sphere / Wagner VII (authalic latitude)",
    BASEGEOGCRS["WGS 84 authalic sphere",
        DATUM["WGS 84 authalic sphere",
            ELLIPSOID["WGS 84 authalic sphere",6371007.181,0,
                LENGTHUNIT["metre",1]]],
        PRIMEM["Greenwich",0,
            ANGLEUNIT["degree",0.0174532925199433]]],
    CONVERSION["Wagner VII",
        METHOD["Wagner VII"],
        PARAMETER["Longitude of natural origin",0,
            ANGLEUNIT["degree",0.0174532925199433],
            ID["EPSG",8802]],
        PARAMETER["False easting",0,
            LENGTHUNIT["metre",1],
            ID["EPSG",8806]],
        PARAMETER["False northing",0,
            LENGTHUNIT["metre",1],
            ID["EPSG",8807]]],
    CS[Cartesian,2],
        AXIS["easting (X)",east,
            ORDER[1],
            LENGTHUNIT["metre",1]],
        AXIS["northing (Y)",north,
            ORDER[2],
            LENGTHUNIT["metre",1]]]
```

The same WKT is printed by `seppo_wagner7_vector --wkt`, e.g. straight to the clipboard with
`seppo_wagner7_vector --wkt | xclip -selection clipboard` (Linux) or `seppo_wagner7_vector --wkt | pbcopy` (macOS).

QGIS then lists matching layers as `USER:100000` (or the next free number), and the CRS can be picked as a project CRS
to show other layers in Wagner VII. Some notes:

- An error such as `unhandled keyword: ROJCRS` means the first character was lost while copying; the text must start
  with `PROJCRS[`.
- A `.vrt` or `.aux.xml` written by an older openSEPPO version can still carry the unnamed CRS and override the
  GeoTIFF's own; rebuild or delete it.
- Do not let QGIS reproject vector layers or draw a lat/lon grid in Wagner VII; see the next section.

### Vector overlays and grid lines (`seppo_wagner7_vector`)

QGIS's on-the-fly reprojection does not work for this CRS, for two reasons:

- It projects with geodetic latitude, so overlays such as coastlines or scene footprints sit up to 0.13° (about
  14 km) poleward of the rasters, most at 45° latitude.
- PROJ's `wag7` has no inverse. QGIS needs one to find the features in view, show cursor coordinates and draw its
  lat/lon grid (map canvas grid and layout map grids in degrees), so these fail or are wrong in Wagner VII.

`seppo_wagner7_vector` projects vector data with the same forward pipeline as the rasters and writes it in the
Wagner VII CRS, so it overlays exactly and QGIS does not reproject anything:

```bash
# Any OGR vector source in any CRS (GeoJSON, GeoPackage, Shapefile, ...):
seppo_wagner7_vector -i footprints.geojson -o footprints_w7.gpkg

# GeoParquet straight from and to S3:
seppo_wagner7_vector -i s3://bucket/scenes.parquet -o s3://bucket/scenes_w7.parquet

# Meridians, parallels and the two pole lines every 10 degrees, for grid lines:
seppo_wagner7_vector --graticule 10 -o graticule_w7.gpkg
```

| Option | Meaning |
|---|---|
| `-i INPUT` | Vector file (local or `s3://`) or other OGR source, any CRS; all layers are projected. A Wagner VII file cannot be read back in: PROJ has no inverse for `wag7`. |
| `--graticule STEP` | Instead of `-i`: write a `graticule` line layer every STEP degrees, with fields `kind` (`meridian`, `parallel`, `pole_line`), `value` (degrees) and `label` (e.g. `30°N`) for labelling in QGIS. |
| `--wkt` | Instead of `-i`: print the CRS as WKT for [QGIS](#adding-the-wagner-vii-crs-to-qgis) and exit; no `-o` needed. |
| `-o OUTPUT` | Output file, local or `s3://`; the format follows the extension (`.gpkg`, `.geojson`, `.parquet`, `.shp`, ...). An `s3://` output is written to a local temp directory and uploaded with s3fs (default AWS credentials, `AWS_PROFILE` honoured), every file of it, e.g. a Shapefile's `.shp`/`.shx`/`.dbf`/`.prj`. GeoParquet needs GDAL's Parquet driver (`libgdal-arrow-parquet` on conda-forge). |
| `-f FORMAT` | OGR driver name, when the extension is not enough. |
| `-nln NAME` | Output layer name, for single-layer input. |
| `--segmentize DEG` | Add a node every DEG degrees before projecting, so long edges follow Wagner VII's curves (default 0.1; 0 to skip). |
| `--overwrite` | Replace an existing output, local or `s3://`. |

It needs the GDAL Python bindings (`osgeo`). In QGIS, set the project CRS to the Wagner VII custom CRS above and load
the rasters and these outputs; all layers then share one CRS. A layout grid in map units (metres) works; for a
lat/lon grid, use the graticule layer instead of QGIS's grid.

The same projection with `ogr2ogr`, from EPSG:4326 input (`-ct` takes latitude first, hence the `axisswap`; the
pipeline is `wagner7.FORWARD_PIPELINE_LATLON`):

```bash
PIPE=$(python -c "import openseppo.nisar.wagner7 as w; print(w.FORWARD_PIPELINE_LATLON)")
WKT=$(python -c "import openseppo.nisar.wagner7 as w; print(w.WKT)")
ogr2ogr -f GPKG out_w7.gpkg in.geojson -s_srs EPSG:4326 -t_srs "$WKT" -ct "$PIPE" -segmentize 0.1
```

`seppo_wagner7_vector` splits features at the antimeridian first (`ogr2ogr -wrapdateline` to EPSG:4326), so its outputs
have multi-part geometry types (`MultiPolygon`, `MultiLineString`, `MultiPoint`); with plain
`ogr2ogr`, a feature crossing ±180° is otherwise drawn across the whole map.
A segment running from one antimeridian to the other (longitude −180 to +180, e.g. a parallel drawn as two points)
gets a point every degree before that split; otherwise the split cuts it down to its two end points and it draws nothing.
