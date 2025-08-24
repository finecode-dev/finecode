import atexit
import shutil
import sys
import tempfile

from setuptools import setup
from setuptools.command.build import build
from setuptools.command.build_ext import build_ext
from setuptools.command.build_py import build_py
from setuptools.command.egg_info import egg_info

# Create a single temp directory for all build operations
_TEMP_BUILD_DIR = None


def get_temp_build_dir(pkg_name):
    global _TEMP_BUILD_DIR
    if _TEMP_BUILD_DIR is None:
        _TEMP_BUILD_DIR = tempfile.mkdtemp(prefix=f"{pkg_name}_build_")
        atexit.register(lambda: shutil.rmtree(_TEMP_BUILD_DIR, ignore_errors=True))
    return _TEMP_BUILD_DIR


class TempDirBuildMixin:
    def initialize_options(self):
        super().initialize_options()
        temp_dir = get_temp_build_dir(self.distribution.get_name())
        self.build_base = temp_dir


class TempDirEggInfoMixin:
    def initialize_options(self):
        super().initialize_options()
        temp_dir = get_temp_build_dir(self.distribution.get_name())
        self.egg_base = temp_dir


class CustomBuild(TempDirBuildMixin, build):
    pass


class CustomBuildPy(TempDirBuildMixin, build_py):
    pass


class CustomBuildExt(TempDirBuildMixin, build_ext):
    pass


class CustomEggInfo(TempDirEggInfoMixin, egg_info):
    def initialize_options(self):
        # Don't use temp dir for editable installs
        if "--editable" in sys.argv or "-e" in sys.argv:
            egg_info.initialize_options(self)
        else:
            super().initialize_options()


setup(
    name="fine_python_module_exports",
    cmdclass={
        "build": CustomBuild,
        "build_py": CustomBuildPy,
        "build_ext": CustomBuildExt,
        "egg_info": CustomEggInfo,
    },
)
