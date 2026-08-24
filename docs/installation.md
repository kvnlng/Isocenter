# Installation

Gantry requires **Python 3.12+**.

```bash
pip install "git+https://github.com/kvnlng/Gantry.git"
```

## Optional Features

### Natural Language Processing (NLP)

To enable advanced entity detection (e.g., precise Patient Name recognition) for Redaction Zone Discovery:

```bash
pip install "git+https://github.com/kvnlng/Gantry.git#egg=gantry[nlp]"
```

Or manually:

```bash
pip install spacy
python -m spacy download en_core_web_sm
```

### Optical Character Recognition (OCR)

To detect burned-in text in pixel data, install the `ocr` extra:

```bash
pip install "git+https://github.com/kvnlng/Gantry.git#egg=gantry[ocr]"
```

This also needs the Tesseract binary itself, which is not a Python
package:

```bash
brew install tesseract        # macOS
apt-get install tesseract-ocr # Debian/Ubuntu
```

Without it, `gantry.pixel_analysis` sets `HAS_OCR = False` and the rest
of Gantry works normally — OCR-dependent features are simply skipped.

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

Gantry's parallel processing engine is designed to maximize CPU utilization. However, heavy operations like JPEG 2000 compression require significant memory per worker.

- **Memory**: Gantry is memory-intensive during specific operations (e.g., Pixel Redaction, J2K Export).
  - **Minimum**: 2GB RAM per vCPU.
  - **Recommended (Heavy Workloads)**: 8GB RAM per vCPU (e.g., for massive multi-frame J2K compression).
- **Concurrency**: By default, Gantry uses all available cores (`1:1` ratio). Use `GANTRY_MAX_WORKERS` env var to limit this if OOM occurs.
