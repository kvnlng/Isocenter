# Installation

Isocenter needs **Python 3.12 or later** on **Linux or macOS**. The storage layer's locks use `fcntl`, so it does not import on Windows; there, use WSL or a Linux container.

Install it into a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install isocenter
```

Release candidates: `pip install --pre isocenter`.

Check which version you have:

```bash
python -c "import isocenter; print(isocenter.__version__)"
```

To install unreleased work from `main` instead:

```bash
pip install "git+https://github.com/kvnlng/Isocenter.git"
```

## Optional features

Everything below is optional. Without it, the rest of Isocenter works as normal.

### Optical character recognition (OCR)

To find burned-in text in pixel data, install the `ocr` extra and the Tesseract binary, which is not a Python package:

```bash
pip install "isocenter[ocr]"
brew install tesseract          # macOS
apt-get install tesseract-ocr   # Debian/Ubuntu
```

Without the extra or the binary, the two methods that read burned-in text, `scan_pixel_content()` and `discover_redaction_zones()`, raise `OcrUnavailableError`, a `RuntimeError`, naming what is missing, before they scan anything. Once the check passes, `scan_pixel_content()` lists each instance whose pixels could not be loaded or read in `report.failures` and warns with the count; `discover_redaction_zones()` warns the same way and counts only the instances it read. Either one raises `PixelScanError`, also a `RuntimeError`, when it could read none of the instances it tried.

`isocenter.pixel_analysis.HAS_OCR` is `False` when `pytesseract` did not import. It does not check the binary.

### Natural language processing (NLP)

`discover_redaction_zones()` can use spaCy to classify the text it finds as names. Install the `nlp` extra and the English model:

```bash
pip install "isocenter[nlp]"
python -m spacy download en_core_web_sm
```

The model download is a separate step because the model is not published on PyPI. Without the model, discovery classifies text with regular expressions.

## Codecs

The decoders for every compressed transfer syntax Isocenter reads install with it: `imagecodecs` and Pillow for JPEG Baseline and Extended, JPEG Lossless, JPEG-LS, JPEG 2000 and High-Throughput JPEG 2000, and pydicom itself for RLE. Where one of pydicom's own decoding plugins is installed for a syntax, it is tried first. A decode cannot catch every damaged stream; [Codec support](codecs.md) lists what reads each syntax and the limits.

## System requirements

Heavy operations such as pixel redaction and JPEG 2000 export need memory per worker.

- **Memory**: at least 2 GB RAM per vCPU; 8 GB per vCPU for large multi-frame JPEG 2000 export.
- **Concurrency**: ingest, audit and export use one worker per CPU by default. `redact()`, which holds a decoded frame per worker, uses half the CPUs and at most eight. If a worker is killed for memory, set `ISOCENTER_MAX_WORKERS` to limit both ([Environment Variables](environment.md)).

## Next

The [Quick Start](quickstart.md) walks the pipeline once. To work on Isocenter itself, see [Contributing](developer_guide.md).
