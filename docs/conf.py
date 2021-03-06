"""
    Homonim: Correction of aerial and satellite imagery to surface reflectance
    Copyright (C) 2021 Dugal Harris
    Email: dugalh@gmail.com

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

import os
import sys
sys.path.insert(0, os.path.abspath('..'))
from homonim.version import __version__


# -- Project information -----------------------------------------------------

project = 'homonim'
copyright = '2022, Dugal Harris'
author = 'Dugal Harris'

# The full version, including alpha/beta/rc tags
release = __version__


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'sphinx_click',
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.napoleon',
    'nbsphinx'
] # yapf: disable

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'sphinx_rtd_theme'

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']

# -- Options for autodoc -----------------------------------------------------
# see https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html
# autodoc_mock_imports = ['rasterio', 'click']
autosummary_generate = True
autoclass_content = 'class'
autodoc_class_signature = 'separated'
autodoc_member_order = 'bysource'
autodoc_typehints = 'both'
# autodoc_typehints_format = 'short'

# -- Options for nbsphinx ----------------------------------------------------
# env.docname will be e.g. examples/l7_composite.ipynb.  The `../` is to
# reference it from itself. preferable to link to actual version of the file
# at the time of the doc build, than a hyperlink to github.
# see https://github.com/aazuspan/wxee/blob/main/docs/conf.py for other
# examples
nbsphinx_prolog = """
.. note::

   This page was generated from a Jupyter notebook. If you want to run and 
   interact with it, you can download it 
   :download:`here <../{{ env.docname }}.ipynb>`.
"""

# -- Workaround for cloup arguments ------------------------------------------
from sphinx_click import ext

def _format_cloup_argument(arg):
    """Format the output of a `click.Argument` or `cloup.Argument`."""
    if hasattr(arg, 'help'):
        # return the argument's help if this is a cloup argument
        yield '.. option:: {}'.format(arg.human_readable_name)
        yield ''
        yield ext._indent(arg.help)
    else:
        return ext._format_argument(arg)

# overwrite sphinx_click's _format_argument with the one above
ext._format_argument = _format_cloup_argument
