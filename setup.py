import pathlib

from setuptools import setup, find_packages

README = (pathlib.Path(__file__).parent / "README.md").read_text(encoding="utf-8")

setup(
    name="gantry",
    version="0.6.0",
    description="A Python DICOM Object Model and Redaction Toolkit",
    long_description=README,
    long_description_content_type="text/markdown",
    author="Kevin Long",
    url="https://github.com/kvnlng/Gantry",
    project_urls={
        "Documentation": "https://kvnlng.github.io/Gantry/",
        "Issues": "https://github.com/kvnlng/Gantry/issues",
        "Changelog": "https://github.com/kvnlng/Gantry/blob/main/CHANGELOG.md",
    },
    license="AGPL-3.0-or-later",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Healthcare Industry",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)",
        "Operating System :: OS Independent",
        # Only versions CI actually runs. Advertising more is the same
        # unbacked promise the old python_requires=">=3.9" was.
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Scientific/Engineering :: Medical Science Apps.",
        "Topic :: Security",
    ],
    keywords=["dicom", "de-identification", "anonymization", "phi", "wfdb",
              "ecg", "medical-imaging", "research"],
    packages=find_packages(),
    # Single source of truth for dependencies. There is deliberately no
    # requirements.txt: two lists drift, and `pip install gantry` only ever
    # reads this one -- python-dotenv once lived only in requirements.txt
    # while being imported unguarded, so a real install failed at import
    # while CI (which installed both) stayed green.
    install_requires=[
        # 2.x's pixel_data_handlers API is deprecated in 3.x and removed in
        # 4.0; gantry/__init__.py still assigns it, so cap until that is
        # migrated.
        "pydicom>=3.0.0,<4.0",
        # Floors chosen as the first release of each that supports 3.12.
        "numpy>=1.26.0",
        "pandas>=2.1.0",
        "pillow>=10.1.0",
        "imagecodecs>=2023.9.18",
        "PyYAML>=6.0.1",
        "pyarrow>=14.0.0",
        "cryptography>=41.0.0",
        "tqdm>=4.65.0",
        # Imported unguarded by gantry/config_manager.py.
        "python-dotenv>=1.0.0",
    ],
    # dataclass(slots=True) needs 3.10, and the dependency set above
    # resolves only on 3.12+. CI tests 3.12, 3.13, 3.14 and 3.14t.
    python_requires=">=3.12",
    extras_require={
        # Optional: gantry/pixel_analysis.py guards the import and sets
        # HAS_OCR=False when absent.
        "ocr": [
            "pytesseract>=0.3.10"
        ],
        "docs": [
            "mkdocs>=1.5.0",
            "mkdocs-material>=9.0.0",
            "mkdocstrings[python]>=0.20.0",
            "mkdocs-awesome-pages-plugin>=2.8.0"
        ],
        # Optional: ZoneDiscoverer imports spacy lazily and falls back to
        # regex when it is unavailable.
        "nlp": [
            "spacy>=3.7.0",
            "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl"
        ],
        "tests": [
            "pytest>=7.0.0",
            "wfdb>=4.1.0",
            "jsonschema>=4.0.0"
        ]
    }
)
