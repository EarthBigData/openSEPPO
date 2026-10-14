#!/usr/bin/env python
"""
seppo_wagner7_vector -- project vector data and graticules to authalic Wagner VII
*********************************************************************************
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Overlays for seppo_nisar_gcov_convert -t_srs wagner7 rasters. QGIS and ogr2ogr
-t_srs project with geodetic latitude, up to 0.13 deg (14 km) off the rasters,
and PROJ's wag7 has no inverse, which QGIS needs for on-the-fly reprojection
and its lat/lon grid. This tool projects with the same forward pipeline as the
rasters, so the outputs overlay exactly with no reprojection in QGIS.

Usage:

# Project any OGR vector file (any CRS) to authalic Wagner VII:
seppo_wagner7_vector -i footprints.geojson -o footprints_w7.gpkg

# Write a 10-degree graticule (meridians, parallels and pole lines):
seppo_wagner7_vector --graticule 10 -o graticule_w7.gpkg

# Print the CRS as WKT, to paste into QGIS (Settings > Custom Projections):
seppo_wagner7_vector --wkt

Requires the GDAL Python bindings (osgeo).
"""

import argparse
import os
import re
import shutil
import sys
import tempfile

import numpy as np

import openseppo.nisar.wagner7 as wagner7


def _gdal():
    try:
        from osgeo import gdal, ogr, osr
    except ImportError:
        sys.exit("seppo_wagner7_vector needs the GDAL Python bindings: "
                 "conda install -c conda-forge gdal")
    gdal.UseExceptions()
    return gdal, ogr, osr


def project_vector(src, dst, fmt=None, segmentize=0.1, layer_name=None, wrap_dateline=True):
    """Project every layer of OGR dataset *src* to authalic Wagner VII in *dst*.

    Source data in any CRS is taken to WGS84 lon/lat by GDAL, split at the
    antimeridian (*wrap_dateline*; geometries become multi-part types), densified
    to a node every *segmentize* degrees (0 to skip), then projected with
    wagner7.FORWARD_PIPELINE_LATLON.
    """
    gdal, _, _ = _gdal()
    tmp = "/vsimem/wagner7_vector_ll.gpkg"
    opts = ["-t_srs", "EPSG:4326"]
    if wrap_dateline:
        opts += ["-wrapdateline", "-nlt", "PROMOTE_TO_MULTI"]
    if layer_name:
        opts += ["-nln", layer_name]
    gdal.VectorTranslate(tmp, src, options=opts + ["-f", "GPKG"])
    try:
        opts = ["-s_srs", "EPSG:4326", "-t_srs", wagner7.WKT,
                "-ct", wagner7.FORWARD_PIPELINE_LATLON]
        if segmentize:
            opts += ["-segmentize", str(segmentize)]
        if fmt:
            opts += ["-f", fmt]
        ds = gdal.VectorTranslate(dst, tmp, options=opts)
        if ds is None:
            raise RuntimeError(f"could not write {dst}")
        ds = None
    finally:
        gdal.Unlink(tmp)


def graticule(dst, step=10.0, fmt=None, segmentize=0.1):
    """Write meridians and parallels every *step* degrees, pole lines included,
    as one Wagner VII line layer 'graticule' with fields kind, value, label."""
    gdal, ogr, osr = _gdal()
    tmp = "/vsimem/wagner7_graticule_ll.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(tmp)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lyr = ds.CreateLayer("graticule", srs, ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("kind", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("value", ogr.OFTReal))
    lyr.CreateField(ogr.FieldDefn("label", ogr.OFTString))

    def add(kind, value, label, pts):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("kind", kind)
        f.SetField("value", float(value))
        f.SetField("label", label)
        line = ogr.Geometry(ogr.wkbLineString)
        for lon, lat in pts:
            line.AddPoint_2D(float(lon), float(lat))
        f.SetGeometry(line)
        lyr.CreateFeature(f)

    for lon in np.arange(-180.0, 180.0 + step / 2, step):
        lon = min(lon, 180.0)
        label = f"{abs(lon):g}°{'E' if lon > 0 else 'W' if lon < 0 else ''}"
        add("meridian", lon, label, [(lon, -90.0), (lon, 90.0)])
    lats = np.arange(-90.0, 90.0 + step / 2, step)
    lats = np.unique(np.append(np.clip(lats, -90.0, 90.0), [-90.0, 90.0]))
    for lat in lats:
        kind = "pole_line" if abs(lat) == 90 else "parallel"
        label = f"{abs(lat):g}°{'N' if lat > 0 else 'S' if lat < 0 else ''}"
        add(kind, lat, label, [(-180.0, lat), (180.0, lat)])
    ds = None
    try:
        project_vector(tmp, dst, fmt=fmt, segmentize=segmentize, wrap_dateline=False)
    finally:
        gdal.Unlink(tmp)


def qgis_wkt():
    """wagner7.WKT pretty-printed for QGIS custom projections (REMARK left out)."""
    from pyproj import CRS
    return re.sub(r',\s*REMARK\[".*?"\]', "", CRS.from_wkt(wagner7.WKT).to_wkt(pretty=True))


def _main(argv):
    ap = argparse.ArgumentParser(
        prog="seppo_wagner7_vector",
        description="Project vector data, or write a graticule, in authalic Wagner VII "
                    "(seppo_nisar_gcov_convert -t_srs wagner7), so overlays match the rasters.",
        epilog="examples:\n"
               "  seppo_wagner7_vector -i footprints.geojson -o footprints_w7.gpkg\n"
               "  seppo_wagner7_vector -i s3://bucket/scenes.parquet -o s3://bucket/scenes_w7.parquet\n"
               "  seppo_wagner7_vector --graticule 10 -o graticule_w7.gpkg\n"
               "  seppo_wagner7_vector --graticule 30 -o graticule_w7.geojson\n"
               "  seppo_wagner7_vector --wkt | xclip -selection clipboard\n\n"
               "Load the outputs and the rasters in QGIS with the project CRS set to\n"
               "Wagner VII; see doc/wagner7.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("-i", "--input", help="input vector file (local or s3://) or any OGR source, in any CRS")
    src.add_argument("--graticule", type=float, metavar="STEP",
                     help="write meridians and parallels every STEP degrees instead")
    src.add_argument("--wkt", action="store_true",
                     help="print the Wagner VII CRS as WKT, to paste into QGIS "
                          "(Settings > Custom Projections), and exit")
    ap.add_argument("-o", "--output",
                    help="output file, local or s3://; format from the extension (.gpkg, .geojson, .parquet, .shp, ...)")
    ap.add_argument("-f", "--format", default=None,
                    help="OGR driver name, if the extension is not enough (e.g. GPKG)")
    ap.add_argument("-nln", "--layer_name", default=None,
                    help="output layer name (single-layer input)")
    ap.add_argument("--segmentize", type=float, default=0.1, metavar="DEG",
                    help="add nodes every DEG degrees before projecting, so long edges "
                         "follow Wagner VII's curves (default: 0.1, 0 to skip)")
    ap.add_argument("--overwrite", action="store_true", help="replace the output if it exists")
    args = ap.parse_args(argv[1:])

    if args.wkt:
        print(qgis_wkt())
        return
    if not args.output:
        ap.error("-o/--output is required")

    if args.graticule is not None and not 0 < args.graticule <= 90:
        ap.error("--graticule STEP must be in (0, 90] degrees")
    src_path = args.input
    if src_path and src_path.startswith("s3://"):
        src_path = "/vsis3/" + src_path[len("s3://"):]

    # s3:// output: write to a local temp dir, then upload every file written
    # there (one, or several for e.g. a Shapefile). GDAL cannot write formats
    # such as GPKG straight to S3.
    fs = tmpdir = None
    out = args.output
    if args.output.startswith("s3://"):
        import s3fs
        fs = s3fs.S3FileSystem()
        if fs.exists(args.output) and not args.overwrite:
            ap.error(f"{args.output} exists; use --overwrite to replace it")
        tmpdir = tempfile.mkdtemp(prefix="seppo_wagner7_vector_")
        out = os.path.join(tmpdir, os.path.basename(args.output.rstrip("/")))
    elif os.path.exists(args.output):
        if not args.overwrite:
            ap.error(f"{args.output} exists; use --overwrite to replace it")
        gdal, _, _ = _gdal()
        drv = gdal.IdentifyDriverEx(args.output, gdal.OF_VECTOR)
        if drv is not None:
            drv.Delete(args.output)
        else:
            os.remove(args.output)

    try:
        if args.graticule is not None:
            graticule(out, args.graticule, fmt=args.format, segmentize=args.segmentize)
        else:
            project_vector(src_path, out, fmt=args.format,
                           segmentize=args.segmentize, layer_name=args.layer_name)
        if fs is not None:
            prefix = args.output.rstrip("/").rsplit("/", 1)[0]
            for name in sorted(os.listdir(tmpdir)):
                fs.put(os.path.join(tmpdir, name), f"{prefix}/{name}")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"wrote {args.output}")


def main():
    _main(sys.argv)


if __name__ == "__main__":
    main()
