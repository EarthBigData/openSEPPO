"""
openSEPPO -- Open SEPPO Tools
Supporting Geospatial and Remote Sensing Data Processing

(c) 2026 Earth Big Data LLC  |  https://earthbigdata.com
Licensed under the Apache License, Version 2.0
https://github.com/EarthBigData/openSEPPO

Geospatial and SAR processing utilities. The tools are designed to scale
readily with the SEPPO (Scalable EO Processing Platform) software by
Earth Big Data (https://earthbigdata.com/seppo).

Subpackages
-----------
openseppo.nisar   -- NISAR GCOV data search, download, and COG conversion
openseppo.cli     -- Command-line entry points
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("openseppo")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"

import os as _os, glob as _glob
from datetime import datetime as _dt
_pkg_dir = _os.path.dirname(__file__)
_py_files = _glob.glob(_os.path.join(_pkg_dir, "**", "*.py"), recursive=True)
__date__ = _dt.fromtimestamp(
    max(_os.path.getmtime(f) for f in _py_files)
).strftime("%Y-%m-%d") if _py_files else "unknown"

__all__ = ["__version__", "__date__", "banner"]


def banner(progname):
    """Print version banner when CLI is invoked."""
    print(f"*** openSEPPO {progname} {__version__} ({__date__}) ***")
