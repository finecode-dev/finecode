from setuptools import find_packages, setup

setup(
    name="finecode_knowledge",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.11",
)
