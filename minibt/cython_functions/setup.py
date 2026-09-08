from setuptools import setup
from Cython.Build import cythonize
import numpy as np

setup(
    ext_modules=cythonize([
        "backtrader_from_signals.pyx",
        "backtrader_pair_from_signals.pyx",
        "backtest_engine.pyx",
    ]),
    include_dirs=[np.get_include()]
)