"""Normalize a semver prerelease (0.1.1-alpha.1) to PEP 440 (0.1.1a1) for packaging.

The canonical version lives in VERSION as semver, and `luna_doctor/cli.py --version`
reports it verbatim. Python packaging requires PEP 440, which uses no hyphen
for prereleases. This shim bridges the two so `pip install .` keeps working
during the alpha without duplicating the version number.

Once the project hits 1.0 this shim is still harmless. Remove it only if
there is a reason to stop normalizing.
"""

import re
from pathlib import Path

from setuptools import setup


def pep440_version() -> str:
    raw = Path("VERSION").read_text(encoding="utf-8").strip()
    # 0.1.1-alpha.1 -> 0.1.1a1, 0.1.1-beta.2 -> 0.1.1b2, 0.2.0-rc.1 -> 0.1.1rc1
    m = re.match(r"^(\d+\.\d+\.\d+)-([a-zA-Z]+)\.(\d+)$", raw)
    if not m:
        return raw
    prefix, num = {"alpha": "a", "beta": "b"}.get(m.group(2).lower(), m.group(2).lower()), m.group(3)
    return f"{m.group(1)}{prefix}{num}"


setup(version=pep440_version())
