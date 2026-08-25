# Developer Guide

Welcome to the Isocenter development documentation. This guide covers how to set up your environment, maintain code quality, and run tests.

## 1. Environment Setup

Isocenter requires Python 3.9+.

```bash
# Clone the repository
git clone https://github.com/kvnlng/Isocenter.git
cd Isocenter

# Install dependencies (including dev tools)
pip install -e ".[dev]"
```

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

1. Update `CHANGELOG.md`.
2. Bump version in `setup.py`.
3. Tag the release in git.
