"""Build the four Cython extensions of the vendored minibt package.

Run from the directory that CONTAINS the `minibt` package (e.g. /app):
    python minibt/_build_exts.py build_ext --inplace
"""
import numpy as np
from setuptools import setup, Extension
from Cython.Build import cythonize

exts = [
    Extension("minibt.zigzag.core", ["minibt/zigzag/core.pyx"], include_dirs=[np.get_include()]),
    Extension("minibt.cython_functions.backtest_engine", ["minibt/cython_functions/backtest_engine.pyx"], include_dirs=[np.get_include()]),
    Extension("minibt.cython_functions.backtrader_from_signals", ["minibt/cython_functions/backtrader_from_signals.pyx"], include_dirs=[np.get_include()]),
    Extension("minibt.cython_functions.backtrader_pair_from_signals", ["minibt/cython_functions/backtrader_pair_from_signals.pyx"], include_dirs=[np.get_include()]),
]
setup(ext_modules=cythonize(exts, language_level=3))
