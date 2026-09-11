"""Sphinx configuration for the kf-boot documentation."""

from __future__ import annotations

import os
import sys
from importlib.metadata import version as pkg_version

ROOT = os.path.abspath("..")
SRC = os.path.join(ROOT, "src")

for path in (SRC, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    import sphinx_rtd_theme  # noqa: F401
except ImportError:
    sphinx_rtd_theme = None

# Project information

project = "kf-boot"
author = "KERI Foundation"
copyright = "2026, KERI Foundation and contributors"

try:
    release = pkg_version("kf-boot")
except Exception:
    release = "0.0.1"
version = release

# General configuration

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

autosummary_generate = True
autodoc_member_order = "bysource"
autodoc_mock_imports = [
    "falcon",
    "hio",
    "keri",
    "ordered_set",
    "requests",
]
napoleon_include_init_with_doc = True

# HTML output

if sphinx_rtd_theme:
    html_theme = "sphinx_rtd_theme"
