import pathlib

from setuptools import setup, find_packages

README = (pathlib.Path(__file__).parent / "README.md").read_text(encoding="utf-8")

setup(
    name="isocenter",
    version="0.7.0",
    description="A Python DICOM Object Model and Redaction Toolkit",
    long_description=README,
    long_description_content_type="text/markdown",
    author="Kevin Long",
    url="https://github.com/kvnlng/Isocenter",
    project_urls={
        "Documentation": "https://kvnlng.github.io/Isocenter/",
        "Issues": "https://github.com/kvnlng/Isocenter/issues",
        "Changelog": "https://github.com/kvnlng/Isocenter/blob/main/CHANGELOG.md",
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
    # `include`, not a bare find_packages(): scripts/ carries an
    # __init__.py so the benchmarks can import it, and a bare call swept
    # it into the wheel. Installing Isocenter then dropped a top-level
    # module called `scripts` into site-packages -- a name we do not own
    # and half of PyPI also wants.
    packages=find_packages(include=["isocenter", "isocenter.*"]),
    # Without this the JSON under isocenter/resources/ ships in neither the
    # wheel nor the sdist, and nothing fails loudly: every loader guards
    # on os.path.exists, so `ConfigLoader.load_phi_config()` returns {}
    # and a pip-installed Isocenter audits against an empty PHI tag list and
    # reports clean. The .yaml glob covers the ctp_rules.yaml that
    # session.py prefers over the .json when present.
    package_data={"isocenter": ["resources/*.json", "resources/*.yaml"]},
    # Single source of truth for dependencies. There is deliberately no
    # requirements.txt: two lists drift, and `pip install isocenter` only ever
    # reads this one -- python-dotenv once lived only in requirements.txt
    # while being imported unguarded, so a real install failed at import
    # while CI (which installed both) stayed green.
    install_requires=[
        # 2.x's pixel_data_handlers API is deprecated in 3.x and removed in
        # 4.0; isocenter/__init__.py still assigns it, so cap until that is
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
        # Imported unguarded by isocenter/config_manager.py.
        "python-dotenv>=1.0.0",
    ],
    # dataclass(slots=True) needs 3.10, and the dependency set above
    # resolves only on 3.12+. CI tests 3.12, 3.13, 3.14 and 3.14t.
    python_requires=">=3.12",
    extras_require={
        # Optional: isocenter/pixel_analysis.py guards the import and sets
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
        # The en_core_web_sm model is deliberately NOT listed here. It
        # has no PyPI release, so pinning it needs a direct URL
        # (`en_core_web_sm @ https://github.com/explosion/...whl`), and
        # PyPI refuses any distribution whose metadata contains one --
        # `twine upload` fails outright with "Can't have direct
        # dependency". ZoneDiscoverer falls back to regex when the model
        # is absent, so the extra is still useful without it; install the
        # model with `python -m spacy download en_core_web_sm`.
        "nlp": [
            "spacy>=3.7.0"
        ],
        "tests": [
            "pytest>=7.0.0",
            "wfdb>=4.1.0",
            "jsonschema>=4.0.0",
            # tests/test_packaging_contract.py builds a real wheel and sdist
            # and inspects them, which needs a build backend in the *test*
            # environment -- not just in pip's isolated build env. Python
            # 3.12 dropped setuptools from new venvs, so `pip install -e
            # ".[tests]"` leaves none behind and those tests fail with
            # ModuleNotFoundError. Invisible locally to anyone whose venv
            # predates 3.12 or who installed setuptools by hand; CI, which
            # builds its venv fresh every run, fails every time. 70.1 is the
            # first release with bdist_wheel built in, so no separate
            # `wheel` dependency is needed.
            "setuptools>=70.1"
        ],
        # Contributor tooling, deliberately kept out of `tests` and out of
        # install_requires: pylint keeps this codebase readable, and none
        # of that is any business of somebody who just wants to use the
        # library. `pip install isocenter` must never drag a linter in.
        # This is also the extra `docs/developer_guide.md` documents, so
        # the command it gives contributors has to keep working.
        "dev": [
            "isocenter[tests]",
            "pylint>=3.0"
        ]
    }
)
