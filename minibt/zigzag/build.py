import os
import shutil
import sys
from setuptools.command.build_ext import build_ext
from setuptools import Distribution, Extension

from Cython.Build import cythonize
import numpy as np

compile_args = ["-O3"]
link_args = []
include_dirs = [np.get_include()]
libraries = ["m"]


def build():
    debug_mode_on = '1' if 'debug_mode_on' in os.environ else '0'
    extensions = [
        Extension(
            "zigzag.core",
            ["zigzag/core.pyx"],
            extra_compile_args=compile_args,
            extra_link_args=link_args,
            include_dirs=include_dirs,
            libraries=libraries if os.name != 'nt' else [],
            define_macros=[('CYTHON_TRACE', debug_mode_on),
                           ('CYTHON_TRACE_NOGIL', debug_mode_on),
                           ('CYTHON_BINDING', debug_mode_on),
                           ('CYTHON_FAST_PYCCALL', '1')],
        )
    ]
    ext_modules = cythonize(
        extensions,
        include_path=include_dirs,
        compiler_directives={"binding": True, "language_level": sys.version_info.major},
    )

    distribution = Distribution({"name": "zigzag", "ext_modules": ext_modules})
    distribution.package_dir = {"": "."}

    cmd = build_ext(distribution)
    cmd.ensure_finalized()
    cmd.run()

    # Copy built extensions back to the project
    for output in cmd.get_outputs():
        if output.endswith('.pyd') or output.endswith('.so'):
            relative_extension = os.path.relpath(output, cmd.build_lib)
            shutil.copyfile(output, relative_extension)
            mode = os.stat(relative_extension).st_mode
            mode |= (mode & 0o444) >> 2
            os.chmod(relative_extension, mode)


if __name__ == "__main__":
    build()
