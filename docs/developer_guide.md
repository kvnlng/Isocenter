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

The procedure -- how changes reach `main`, how a release branch is cut,
tagged and published, how patch releases work -- lives in
[`RELEASING.md`](https://github.com/kvnlng/Isocenter/blob/main/RELEASING.md)
in the repository root, next to the code it releases. In short: `main` is
the development branch; each minor line is cut to a frozen `release/X.Y`
branch; a version is the tag `vX.Y.Z` on it; and nothing publishes itself.

Publishing is a manual run of `.github/workflows/publish.yml` against the
tag. It uploads to PyPI by Trusted Publishing -- GitHub authenticates over
OIDC and PyPI matches the request against a publisher pinned to this
repository, the workflow's *filename*, and its environment name. There is
no API token in repository secrets, in CI, or on anyone's machine.
Renaming `publish.yml` or its environments breaks publishing until PyPI's
publisher configuration is updated to match.

Before anything is uploaded, the build job checks the run. The target must
be exactly `pypi` or `testpypi`. The built wheel must match
`isocenter/_version.py`, the one place the version is declared. A run to
`pypi` must be dispatched from a `v*` tag that matches both; a TestPyPI
rehearsal may run from the release branch, and a tag, if it runs from one,
must match too. It then installs the
built wheel into a clean environment *outside the source tree* and asserts
it carries its own `resources/*.json`. That gate exists because those
resources once shipped in no distribution at all and nothing failed --
every loader guards on `os.path.exists` and degrades to a default, so a
published release audited against an empty PHI policy and reported clean.

**If `test-supported` (3.13, 3.14) is red, that is a decision, not a
formality.** It only reports; it cannot block the upload. Either fix it,
or delete that classifier from `setup.py` before releasing -- `setup.py`
says outright that a classifier CI does not back is the same unbacked
promise the old `python_requires=">=3.9"` was. A red job here is *more*
likely to be a flake than a real break (v0.9.1's publish had 3.13 fail and
pass on rerun), which is exactly what makes it dangerous: "just rerun it"
is the reading that ships a real break. Rerun to learn whether it is
deterministic, not to make it go away. That is why `RELEASING.md` rehearses
on TestPyPI, from the release branch, before tagging.

### Archiving and DOIs

Once the Zenodo GitHub integration is enabled for this repository, each
published GitHub Release is archived and issued a version DOI, under a
concept DOI that always resolves to the latest. `.zenodo.json` supplies
the deposit metadata and `CITATION.cff` is what GitHub's "Cite this
repository" button reads.

Zenodo only archives releases created *after* the integration is
switched on; it does not backfill -- its own guide says "once connected,
new releases from the repository will be automatically ingested and
archived". So enabling it does nothing to the releases already published:
the first deposit comes from the next release, or from deleting and
re-creating an existing GitHub Release. Creating a GitHub Release uploads
nothing: publishing is a separate manual run (`RELEASING.md`), so the
Release can be made, or re-made for Zenodo, without touching PyPI.

The integration is enabled and the first deposit exists, from v0.8.1.
The **concept** DOI is `10.5281/zenodo.22104298` -- that is what
`CITATION.cff` and the README badge carry, and it always resolves to the
latest version. Zenodo also mints a per-version DOI for each release, one
digit away; do not substitute it, or the citation stops following the
work. `tests/test_version_contract.py` pins both the value and the
agreement between the two files.

`.zenodo.json` supplies the metadata the record is minted from. Two
things about it are easy to get wrong and are pinned by
`tests/test_version_contract.py`:

* **`license` is a Zenodo vocabulary id, not an SPDX id**, and those are
  lowercase. `apache-2.0` resolves at
  `zenodo.org/api/vocabularies/licenses/`; the SPDX spelling
  `Apache-2.0` returns 404, and would have gone into the record as a
  licence Zenodo could not match. (The same held for `agpl-3.0-or-later`
  before #348 changed the licence.)
* **No `version` field.** The GitHub integration fills it in from the
  release tag. Pinning it here would add a third place for the number to
  drift.

Record metadata can be corrected on Zenodo after publication; the DOI
itself cannot be reissued.
