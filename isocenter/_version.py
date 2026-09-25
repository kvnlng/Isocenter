"""The single place Isocenter's version is declared.

Kept in its own module with no imports so `setup.py` can read it at build
time without importing the package -- importing `isocenter` pulls in
pydicom and numpy, which a build environment is not required to have.

Everything else derives from here: `isocenter.__version__` re-exports it,
`setup.py` parses it, and the wheel's installed metadata is generated
from it. CITATION.cff and CHANGELOG.md restate it and must be changed
with it.
"""

__version__ = "1.0.0rc3"
