#!/usr/bin/env python3
"""Point recipe/meta.yaml at a released openseppo sdist on PyPI.

Run after a release is published (the publish-pypi workflow fires on a v* tag),
then copy the recipe into the conda-forge feedstock:

    python recipe/update_recipe.py            # latest release on PyPI
    python recipe/update_recipe.py 0.7.2      # a specific release

The source is the PyPI sdist rather than the GitHub tag tarball: GitHub
regenerates archives and their checksums are not stable over time, while an
uploaded sdist is immutable.

Only version, sha256 and the build number are touched -- entry points and
dependencies stay hand-maintained, since they follow pyproject.toml and not
the release number.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

PACKAGE = "openseppo"
META = Path(__file__).with_name("meta.yaml")


def pypi_sdist(version=None):
    """Return (version, sha256) for a release's sdist on PyPI."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{PACKAGE}/json", timeout=30) as r:
        data = json.load(r)
    version = version or data["info"]["version"]
    files = data["releases"].get(version)
    if not files:
        sys.exit(f"error: {PACKAGE} {version} is not on PyPI")
    for f in files:
        if f["packagetype"] == "sdist":
            return version, f["digests"]["sha256"]
    sys.exit(f"error: {PACKAGE} {version} has no sdist on PyPI")


def main():
    if len(sys.argv) > 2:
        sys.exit(f"usage: {sys.argv[0]} [version]")
    version, sha256 = pypi_sdist(sys.argv[1].lstrip("v") if len(sys.argv) == 2 else None)

    text = META.read_text()
    old_version = re.search(r'{% set version = "(.*?)" %}', text).group(1)
    text = re.sub(r'({% set version = ")(.*?)(" %})', rf"\g<1>{version}\g<3>", text, count=1)
    text = re.sub(r"^  sha256: .*$", f"  sha256: {sha256}", text, count=1, flags=re.M)
    # A new version restarts the build numbering; a re-sync of the same one bumps it.
    if version != old_version:
        text = re.sub(r"^  number: .*$", "  number: 0", text, count=1, flags=re.M)
    else:
        number = int(re.search(r"^  number: (\d+)$", text, re.M).group(1))
        text = re.sub(r"^  number: .*$", f"  number: {number + 1}", text, count=1, flags=re.M)
    META.write_text(text)

    print(f"{META}: {old_version} -> {version}\n  sha256: {sha256}")


if __name__ == "__main__":
    main()
