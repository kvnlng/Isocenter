# Installation

Isocenter needs **Python 3.12 or later** on **Linux or macOS**. The storage layer's locks use `fcntl`, so Isocenter does not import on Windows. On Windows, use WSL or a Linux container.

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

To find burned-in text in pixel data, install the `ocr` extra and the Tesseract binary. The binary is not a Python package.

```bash
pip install "isocenter[ocr]"
brew install tesseract          # macOS
apt-get install tesseract-ocr   # Debian/Ubuntu
```

Two methods read burned-in text: `scan_pixel_content()` and `discover_redaction_zones()`. Without the extra or the binary, they raise `OcrUnavailableError` before they scan anything. `OcrUnavailableError` is a `RuntimeError`, and it names what is missing.

When the check passes:

- `scan_pixel_content()` lists in `report.failures` each instance whose pixels could not be loaded or read. It warns with the count.
- `discover_redaction_zones()` warns the same way. It counts only the instances it read.
- Either method raises `PixelScanError`, also a `RuntimeError`, when it could read none of the instances it tried.

`isocenter.pixel_analysis.HAS_OCR` is `False` when `pytesseract` did not import. It does not check the binary.

### Text detection

The `db` detector finds burned-in text with a text-detection model instead of tesseract (`burned_in_text: {detector: db}`). To use it, install the `textdetect` extra. The extra brings OpenCV. The model itself ships inside the package.

- With `target: any`, the detector needs no tesseract.
- With a narrowed target, tesseract also reads each detected region. So you also need the `ocr` extra and the binary above.

```bash
pip install "isocenter[textdetect]"
```

OpenCV publishes no free-threaded wheel. So you cannot install this extra on Python 3.14t, and on 3.14t `detector: db` raises `TextDetectorUnavailableError` ([OCR](ocr.md#detecting-text-with-a-model-detector-db)).

### Natural language processing (NLP)

`discover_redaction_zones()` can use spaCy to classify the text it finds as names. Install the `nlp` extra and the English model:

```bash
pip install "isocenter[nlp]"
python -m spacy download en_core_web_sm
```

The model download is a separate step because the model is not published on PyPI. Without the model, discovery classifies text with regular expressions.

A [`burned_in_text`](configuration.md#burned-in-text) `target` that names `names` also needs the extra and the model. It has no fallback. Without them, `redact()` and `export(check_burned_in=True)` raise `NlpUnavailableError`. spaCy publishes no free-threaded wheel. So on the free-threaded build (3.14t), you cannot use discovery's spaCy arm or the `names` kind. No CI job installs the model.

## Codecs

The decoders for every compressed transfer syntax that Isocenter reads install with it:

- `imagecodecs` and Pillow for JPEG Baseline and Extended, JPEG Lossless, JPEG-LS, JPEG 2000 and High-Throughput JPEG 2000.
- pydicom itself for RLE.

If one of pydicom's own decoding plugins is installed for a syntax, that plugin is tried first. A decode cannot catch every damaged stream. [Codec support](codecs.md) lists what reads each syntax, and the limits.

## System requirements

Heavy operations such as pixel redaction and JPEG 2000 export need memory per worker.

- **Memory**: at least 2 GB RAM per vCPU. For large multi-frame JPEG 2000 export, 8 GB per vCPU.
- **Concurrency**: ingest, audit and export use one worker per CPU by default. `redact()` holds a decoded frame per worker. It uses half the CPUs and at most eight. If a worker is killed for memory, set `ISOCENTER_MAX_WORKERS` to limit both ([Environment Variables](environment.md)).

## Next

The [Quick Start](quickstart.md) walks the pipeline once. To work on Isocenter itself, see [Contributing](developer_guide.md).
