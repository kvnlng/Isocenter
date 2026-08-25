# Developer Guide

Welcome to the Isocenter development documentation. This guide covers how to set up your environment, maintain code quality, and run tests.

## 1. Environment Setup

Isocenter requires Python 3.12+. The floor is not arbitrary: `entities.py`
and `privacy.py` use `@dataclass(slots=True)` (3.10+), and the declared
dependency set resolves only on 3.12 and later.

```bash
# Clone the repository
git clone https://github.com/kvnlng/Isocenter.git
cd Isocenter

# Install the library, the test suite's dependencies, and pylint
pip install -e ".[dev]"
```

The `dev` extra is contributor tooling only -- it pulls in `tests` plus
`pylint`. Somebody installing Isocenter to *use* it gets none of it;
`pip install isocenter` never installs a linter or a test runner.

## 2. Code Quality

We enforce strict code quality standards to insure reliability and maintainability.

### Pylint

We use `pylint` to lint our codebase. The configuration is strict (`pylintrc.toml`) and we aim to keep the score above 8.5/10 for the main package.

**Run Pylint:**

```bash
# Lint the main package
pylint isocenter

# Lint tests (slightly more lenient)
pylint tests
```

**Common Rules:**

* **Imports**: All imports must be at the top-level (except for strictly necessary circular dependency breaking or rare optional heavy dependencies).
* **Docstrings**: All public modules, classes, and methods must have docstrings.
* **Encodings**: All `open()` calls must specify `encoding='utf-8'` to prevent cross-platform issues.

### Formatting

(Optional) We recommend using `black` for formatting, though it is not currently enforced by CI.

## 3. Testing

We use `pytest` for our test suite.

**Run All Tests:**

```bash
pytest
```

**Run Specific Tests:**

```bash
pytest tests/test_session.py
```

### Benchmarks

We have a dedicated benchmark suite in `tests/benchmarks/`.

```bash
# Run benchmark stress test
python -m tests.benchmarks.run_stress_test
```

## 4. Release Process

Releases publish themselves. `.github/workflows/publish.yml` uploads to
PyPI by Trusted Publishing -- GitHub authenticates over OIDC and PyPI
matches the request against a publisher pinned to this repository, the
workflow's *filename*, and its environment name. There is no API token
in repository secrets, in CI, or on anyone's machine. Renaming
`publish.yml` or its environments breaks publishing until PyPI's
publisher configuration is updated to match.

1. Move `[Unreleased]` in `CHANGELOG.md` to a new `[x.y.z] - YYYY-MM-DD`
   section.
2. Bump `__version__` in `isocenter/_version.py`. That is the only
   place the number is declared; `setup.py` parses it and
   `isocenter.__version__` re-exports it.
3. Update `version` and `date-released` in `CITATION.cff`.
4. Merge to `main` with CI green.
5. Tag `vx.y.z`. **The tag must match the declared version exactly** --
   the build job reads the version out of the built wheel and refuses to
   publish a mismatch. Publishing
   `v0.7.1` from a tree that still says `0.7.0` would produce a release
   nobody can install under the name they were given, and permanently
   spend the version it did claim, since PyPI never allows reuse.
6. **Publish a GitHub Release.** This is the trigger; pushing a tag alone
   does nothing. That is deliberate -- a release is a decision someone
   makes, whereas a mistyped `git push --tags` should not be able to burn
   a version number.

The build job then runs two gates before anything is uploaded: the tag
check above, and an installation of the built wheel into a clean
environment *outside the source tree*, asserting it carries its own
`resources/*.json`. That second gate exists because those resources once
shipped in no distribution at all and nothing failed -- every loader
guards on `os.path.exists` and degrades to a default, so a published
release audited against an empty PHI policy and reported clean.

To rehearse against TestPyPI, run the workflow manually
(`workflow_dispatch`) with `target: testpypi`. Note that neither index
allows re-uploading a version, so a rehearsal consumes that version
number on TestPyPI.

### Archiving and DOIs

Once the Zenodo GitHub integration is enabled for this repository, each
published GitHub Release is archived and issued a version DOI, under a
concept DOI that always resolves to the latest. `.zenodo.json` supplies
the deposit metadata and `CITATION.cff` is what GitHub's "Cite this
repository" button reads.

Zenodo only archives releases created *after* the integration is
switched on; it does not backfill. After the first deposit, add the
concept DOI to `CITATION.cff` and the badge to `README.md`.
