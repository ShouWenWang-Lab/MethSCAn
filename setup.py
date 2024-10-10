import os
import sys
from pathlib import Path

from setuptools import find_packages, setup

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

setup(
    name="methscan",
    version="1.1.0",
    python_requires=">=3.8",
    packages=find_packages()
)
