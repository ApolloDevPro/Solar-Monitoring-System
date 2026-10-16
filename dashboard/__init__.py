"""Static web dashboard (HTML/JS/CSS) served by the FastAPI backend.

This package marker exists so setuptools ships the static files inside the
wheel (see ``[tool.setuptools.package-data]`` in pyproject.toml); the files
are served by path, not imported.
"""
