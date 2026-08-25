# Installation

Isocenter requires **Python 3.12+**.

```bash
pip install "git+https://github.com/kvnlng/Isocenter.git"
```

## Optional Features

### Natural Language Processing (NLP)

To enable advanced entity detection (e.g., precise Patient Name recognition) for Redaction Zone Discovery:

```bash
pip install "git+https://github.com/kvnlng/Isocenter.git#egg=isocenter[nlp]"
python -m spacy download en_core_web_sm
```

The model download is a separate step, like Tesseract below: spaCy's
`en_core_web_sm` has no PyPI release, and pinning it in the extra would
need a direct URL, which PyPI refuses in published metadata. Without the
model, `ZoneDiscoverer` falls back to its regex heuristics.

### Optical Character Recognition (OCR)

To detect burned-in text in pixel data, install the `ocr` extra:

```bash
pip install "git+https://github.com/kvnlng/Isocenter.git#egg=isocenter[ocr]"
```

This also needs the Tesseract binary itself, which is not a Python
package:

```bash
brew install tesseract        # macOS
apt-get install tesseract-ocr # Debian/Ubuntu
```

Without it, `isocenter.pixel_analysis` sets `HAS_OCR = False` and the rest
of Isocenter works normally — OCR-dependent features are simply skipped.

!!! note
    The `imagecodecs` dependency is included and strongly recommended for handling JPEG Lossless and other compressed Transfer Syntaxes.

## Dependencies

Dependencies are declared in one place, `setup.py`. There is deliberately
no `requirements.txt`: two lists drift apart, and only `install_requires`
is consulted when you `pip install`.

To set up a development environment, install the package with its test
extra:

```bash
pip install -e ".[tests]"
```

## System Requirements

Isocenter's parallel processing engine is designed to maximize CPU utilization. However, heavy operations like JPEG 2000 compression require significant memory per worker.

- **Memory**: Isocenter is memory-intensive during specific operations (e.g., Pixel Redaction, J2K Export).
  - **Minimum**: 2GB RAM per vCPU.
  - **Recommended (Heavy Workloads)**: 8GB RAM per vCPU (e.g., for massive multi-frame J2K compression).
- **Concurrency**: By default, Isocenter uses all available cores (`1:1` ratio). Use `ISOCENTER_MAX_WORKERS` env var to limit this if OOM occurs.
