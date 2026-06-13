"""The harness version, in ONE place. pyproject reads this via setuptools dynamic version;
``ckbbench.__version__`` re-exports it. This is the harness release version, distinct from any
per-Suite semver (which lives in each suite's manifest)."""

__version__ = "1.0.0"
