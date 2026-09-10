# coding: utf-8
"""Version stamp and deployment report.

This lives in a submodule rather than ``__init__.py`` on purpose. The QMT
sandbox loader never executes the root package -- it builds an empty module and
returns it, because the package's eager exports trip QMT's import allowlist:

    # QMT native allowlist rejects the root package eager exports.
    if name == "bigqmt_signal_trader":
        return module

So anything defined in ``__init__.py`` simply does not exist inside QMT. A
version stamp that is invisible in the one environment it exists to describe
would be worse than none at all.

Kept in step with pyproject.toml by tests/test_version_stamp.py. It sat at
0.2.0 for fifteen releases, which made it useless for its one job: telling a
deployed tree apart from the package it came from. Deploying into QMT is a file
copy, and QMT keeps modules in sys.modules across strategy re-runs, so "the
copy never happened" and "the copy landed but was not picked up" look identical
from the outside.
"""

__version__ = "0.3.31"


def deployment_report(package_dir=None):
    """Return ``(version, package_dir)`` -- which build, loaded from where.

    Never raises: this runs during strategy startup, where an exception would
    take the bridge down for the sake of a log line.
    """
    import os

    try:
        directory = package_dir or os.path.dirname(os.path.abspath(__file__))
    except Exception:
        directory = "?"
    return __version__, directory
