import pathlib
import re

from setuptools import setup, find_packages

HERE = pathlib.Path(__file__).parent
README = (HERE / "README.md").read_text(encoding="utf-8")


def read_version():
    """The version from isocenter/_version.py, without importing it.

    Parsed rather than imported because importing the package pulls in
    pydicom and numpy, and a build environment is not required to have
    them installed. This is what makes _version.py the single source:
    setup.py restating the number is how the two came to disagree.
    """
    source = (HERE / "isocenter" / "_version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                      source, re.MULTILINE)
    if not match:
        raise RuntimeError("No __version__ found in isocenter/_version.py")
    return match.group(1)

setup(
    name="isocenter",
    version=read_version(),
    description="A Python DICOM Object Model and Redaction Toolkit",
    long_description=README,
    long_description_content_type="text/markdown",
    author="Kevin Long",
    url="https://github.com/kvnlng/Isocenter",
    project_urls={
        "Documentation": "https://kvnlng.github.io/Isocenter/",
        "Issues": "https://github.com/kvnlng/Isocenter/issues",
        "Security policy": "https://github.com/kvnlng/Isocenter/security/policy",
        "Changelog": "https://github.com/kvnlng/Isocenter/blob/main/CHANGELOG.md",
    },
    license="Apache-2.0",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Healthcare Industry",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: Apache Software License",
        # POSIX, not "OS Independent": every sidecar write takes
        # `fcntl.flock`, and the gate (`<sidecar>.lock`) and pass-lock
        # (`<sidecar>.pass.lock`) beside it are flock files too (#368).
        # `fcntl` does not exist on Windows, so the old classifier
        # promised a platform on which `pip install` succeeded and the
        # first sidecar write raised `ModuleNotFoundError` (#376).
        # `tests/test_packaging_contract.py` checks this line against
        # the module-scope `import fcntl` in `isocenter/sidecar.py`, in
        # both directions.
        "Operating System :: POSIX",
        # Only versions CI actually runs. Advertising more is the same
        # unbacked promise the old python_requires=">=3.9" was.
        #
        # All four are run by the release matrix in publish.yml. 3.12 and
        # 3.14t additionally block the upload; 3.13 and 3.14 only report.
        # So if a release goes out with one of those red, delete its line
        # here in the same release -- these are checked at release time,
        # which is the last moment they can still be made true.
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        # `python_requires` cannot express this: 3.14t *is* 3.14, the `t`
        # is the build variant, and the wheel is py3-none-any so the ABI
        # tag does not carry it either. This classifier is the only place
        # the promise can be stated -- and `run_parallel()` does take a
        # different path when there is no GIL (isocenter/parallel.py), so
        # it is a real promise, not an absence of one. Backed by 3.14t
        # being in publish.yml's test-floor, which blocks the upload.
        "Programming Language :: Python :: Free Threading :: 3 - Stable",
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
    # wheel nor the sdist. It once failed silently (a pip-installed
    # Isocenter audited against an empty PHI tag list and reported clean);
    # since #388 a missing resource raises `RuntimeError` at first use,
    # and since #495 the default PHI policy is Python (`FLOOR_POLICY`),
    # so what is at stake is the machine redaction knowledge base and the
    # CTP rules. The .yaml glob covers the ctp_rules.yaml that session.py
    # prefers over the .json when present.
    #
    # The .onnx and .NOTICE globs (#1121): the PP-OCRv3 text-detection
    # model `burned_in_text: {detector: db}` reads, sha256-pinned in
    # isocenter/text_detection.py, and its provenance and licence beside
    # it. Shipped, never downloaded (owner ruling Q-C3 (a)).
    package_data={"isocenter": ["resources/*.json", "resources/*.yaml",
                                "resources/*.onnx", "resources/*.NOTICE"]},
    # Single source of truth for dependencies. There is deliberately no
    # requirements.txt: two lists drift, and `pip install isocenter` only ever
    # reads this one -- python-dotenv once lived only in requirements.txt
    # while being imported unguarded, so a real install failed at import
    # while CI (which installed both) stayed green.
    install_requires=[
        # The `<4.0` cap is precautionary, not a pending migration. As
        # of #141 nothing here calls an API 4.0 removes: the
        # `pixel_data_handlers` import moved to `pydicom.pixels`, the
        # `is_little_endian`/`is_implicit_VR` assignments are gone (the
        # transfer syntax already determined both), and `save_as` takes
        # `enforce_file_format`. The one remaining reference,
        # `entities.py`'s read of `pydicom.config.pixel_data_handlers`,
        # is inside `except AttributeError` and only decorates an error
        # message.
        #
        # It stays capped because 4.0 does not exist to test against, and
        # this file is the project's shipping promise -- a bound we
        # cannot back with a passing matrix is a bound we should not
        # widen. Lift it when 4.0 ships and the suite is green on it,
        # not before. `tests/test_pydicom_deprecations.py` is what keeps
        # the first half of this comment true.
        "pydicom>=3.0.0,<4.0",
        # Floors chosen as the first release of each that supports 3.12.
        "numpy>=1.26.0",
        "pandas>=2.1.0",
        "pillow>=10.1.0",
        # Raised from >=2023.9.18 in #404, and the old floor was unbackable
        # in two separate ways. It publishes no cp312 wheel, so on this
        # project's own Python floor `pip install imagecodecs==2023.9.18`
        # fails outright -- measured: "No matching distribution found" with
        # --only-binary, and a failed source build without it. And
        # `io_handlers._compress_j2k` now encodes JPEG 2000 with it, so the
        # floor has to be a release where `jpeg2k_encode(arr, level=0,
        # codecformat="J2K")` is bit-exact for uint8/int8/uint16/int16;
        # 2024.6.1 is the first release after the old floor and was
        # measured exact for all four, and for uint8 RGB.
        "imagecodecs>=2024.6.1",
        "PyYAML>=6.0.1",
        "pyarrow>=14.0.0",
        "cryptography>=41.0.0",
        "tqdm>=4.65.0",
    ],
    # dataclass(slots=True) needs 3.10, and the dependency set above
    # resolves only on 3.12+. publish.yml runs all four at release time,
    # and this floor and 3.14t are the two that block the upload.
    python_requires=">=3.12",
    extras_require={
        # Optional: isocenter/pixel_analysis.py guards the import and sets
        # HAS_OCR=False when absent.
        "ocr": [
            "pytesseract>=0.3.10"
        ],
        "docs": [
            # The site is built with Sphinx, `dirhtml`, and
            # pydata-sphinx-theme (docs/conf.py). <10: myst-parser 5.1
            # and pydata-sphinx-theme 0.23 both cap Sphinx below 10, and
            # 9.1 is the release the site was built and measured on.
            "sphinx>=9.1,<10",
            # Pre-1.0: theme options, template names and CSS variables
            # move between minors, and docs/conf.py names several.
            "pydata-sphinx-theme>=0.23,<0.24",
            # The pages are MyST Markdown. <6: a major may change the
            # directive syntax or `myst_heading_slug_func`, which the
            # anchor-keeping hook in docs/_ext/slugs.py relies on.
            "myst-parser>=5.1,<6",
            # `tab-set`/`tab-item` (docs/ocr.md). Pre-1.0, so capped at
            # the minor.
            "sphinx-design>=0.7,<0.8",
            # The two ```{mermaid} diagrams.
            "sphinxcontrib-mermaid>=2.1",
            # The copy button on code blocks, with the `>>> ` prompt
            # stripped (docs/conf.py).
            "sphinx-copybutton>=0.5.2",
            # docs/_ext/slugs.py gives every heading Python-Markdown's id,
            # so no anchor the MkDocs site published changes (and
            # tests/test_doc_anchors.py reads ids with the same function).
            "markdown>=3.4",
            # docs.yml still deploys with mike until the deploy step is
            # rewritten (docs/sphinx C3), which removes this line in the
            # same commit that takes mike out of the workflow.
            "mike>=2.2,<3"
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
        #
        # Capped below 4.0 (owner ruling R19, 2026-10-16), as pydicom is:
        # `SCRUB` target `orgs` and burned-in `names` read `nlp(text).ents`,
        # `label_`, `start_char`/`end_char` and `meta["version"]`, and a
        # major release is installed before a model is built for it or
        # anyone has checked those still hold. The model itself is not
        # pinned and bumps freely within 3.x (R18).
        "nlp": [
            "spacy>=3.7.0,<4.0"
        ],
        # Optional: isocenter/text_detection.py guards `import cv2` and
        # sets HAS_DETECTOR=False when absent; `burned_in_text: {detector:
        # db}` then raises TextDetectorUnavailableError (#1121, owner
        # ruling Q-C1 (a)). Never in install_requires: +35-61 MB for
        # every user, and OpenCV publishes no free-threaded wheel, so
        # 3.14t could not install the package at all.
        #
        # Not in `tests` or `dev` either, for the same reason: `pip
        # install -e ".[dev]"` must keep working on the 3.14t venv the
        # release gate uses. tests.yml adds this extra on the jobs that
        # are not free-threaded.
        #
        # >=5.0.0 because 5.0.0 is the release the detector spike measured
        # (35 of 35 answer-key boxes, 0 of 82 false frames); nothing older
        # was tried, and 4.x's DB post-processing may give other boxes.
        # <6 as pydicom's <4.0 is capped: a major we cannot test against
        # is not one we promise. Owner ruling of 2026-10-15.
        "textdetect": [
            "opencv-python-headless>=5.0.0,<6"
        ],
        "tests": [
            "pytest>=7.0.0",
            "wfdb>=4.1.0",
            "jsonschema>=4.0.0",
            # tests/test_discovery_integration.py builds its fixtures with
            # scripts/generate_redaction_example.py, which fakes patient and
            # institution names. Undeclared until 0.8.2, which meant that
            # test skipped unconditionally and had never once run -- dead
            # coverage reporting as a skip. It is a declared test dependency
            # now, so its absence is an error rather than a quiet gap.
            "faker>=19.0.0",
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
            "setuptools>=70.1",
            # tests/test_doc_anchors.py renders the docs and checks that
            # every `#fragment` link lands on an anchor a heading really
            # produces -- `mkdocs build --strict` validates file
            # references but not fragments, which is why five links in
            # docs/configuration.md were dead for months (#152). The
            # renderer has to be the real one: a stdlib slugifier written
            # during that review disagreed with Python-Markdown's on
            # underscores, so it would have graded links against slugs
            # the site never emits. The `docs` extra declares it too,
            # because the site's slug hook uses the same function;
            # declared here because the test runs in the `tests`
            # environment, which does not include that extra.
            "markdown>=3.4"
        ],
        # Contributor tooling, deliberately kept out of `tests` and out of
        # install_requires: pylint keeps this codebase readable, and none
        # of that is any business of somebody who just wants to use the
        # library. `pip install isocenter` must never drag a linter in.
        # This is also the extra `docs/developer_guide.md` documents, so
        # the command it gives contributors has to keep working.
        "dev": [
            "isocenter[tests]",
            "pylint>=3.0",
            # coverage: .coveragerc's spawned-worker measurement (#380);
            # contributor tooling like pylint, never in tests or
            # install_requires. >=7.10 because `[run] core`,
            # load-bearing there, exists by then (read in 7.10.0's
            # coverage/config.py; measured on 7.15.4 and 7.16.0; nothing
            # between was tried). `[run] sigterm` was the other reason
            # until #886 dropped it.
            "coverage>=7.10"
        ]
    }
)
