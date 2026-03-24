from setuptools import setup, find_packages

setup(
    name="safe_synth",
    version="0.1",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
    ],
    python_requires=">=3.8",
)
