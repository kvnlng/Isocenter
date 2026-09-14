# Installation

Isocenter requires **Python 3.12+** on a POSIX system (Linux or macOS). It does not import on Windows: the storage layer's locks use `fcntl`.

```bash
pip install isocenter
```

Releases are published to PyPI from a tagged GitHub Release, via Trusted
Publishing. To install unreleased work from `main` instead:

```bash
pip install "git+https://github.com/kvnlng/Isocenter.git"
```

## Optional Features

### Natural Language Processing (NLP)

To enable advanced entity detection (e.g., precise Patient Name recognition) for Redaction Zone Discovery:

```bash
pip install "isocenter[nlp]"
python -m spacy download en_core_web_sm
```

The model download is a separate step, like Tesseract below: spaCy's
`en_core_web_sm` has no PyPI release, and pinning it in the extra would
need a direct URL, which PyPI refuses in published metadata. Without the
model, `ZoneDiscoverer` falls back to its regex heuristics.

### Optical Character Recognition (OCR)

To detect burned-in text in pixel data, install the `ocr` extra:

```bash
pip install "isocenter[ocr]"
```

This also needs the Tesseract binary itself, which is not a Python
package:

```bash
brew install tesseract        # macOS
apt-get install tesseract-ocr # Debian/Ubuntu
```

Without the extra or the binary the rest of Isocenter works normally, but
the two methods that read burned-in text — `scan_pixel_content()` and
`discover_redaction_zones()` — raise `OcrUnavailableError`, a
`RuntimeError`, naming what is missing, before they scan anything. The
check is made in the calling process, before the scan starts. After it
passes, `scan_pixel_content()` lists each instance whose pixels could not
be loaded, or whose OCR failed on any frame — for example in a worker
process that cannot find a binary the caller could — in
`report.failures` and warns with the count, and
`discover_redaction_zones()` warns the same way and counts only the
instances it read. If either could read none of the instances it tried,
it raises `PixelScanError`, also a `RuntimeError` (#423). `isocenter.pixel_analysis.HAS_OCR` is `False` when `pytesseract`
did not import; it does not check the binary.

!!! note
    `imagecodecs` is a required dependency and installs with Isocenter. It is the JPEG 2000 encoder the default compressed export uses, and it decodes JPEG Lossless, JPEG-LS, JPEG 2000 and High-Throughput JPEG 2000 files that pydicom's installed plugins cannot, and monochrome JPEG Baseline and Extended files that Pillow cannot, such as 12-bit JPEG Extended.

### Decode limits

Every read of a compressed frame -- `ingest()`, `Instance.get_pixel_data()`, an icon, and the export's readback -- goes through one decode, so a file gets one answer everywhere. That answer has limits:

- **A stream corrupted mid-stream can decode to plausible wrong values with no error.** openjpeg, which decodes JPEG 2000 through Pillow and through `imagecodecs`, and lj92, which decodes JPEG Lossless, report nothing for a stream with bytes damaged in the middle, and the second decoder a cross-check would use returns the same wrong array. Every door reads such a file without an error. libjpeg-turbo, which decodes JPEG Baseline and Extended, reads a stream that lost data from its middle and still ends in its EOI marker with the missing rows filled with mid-grey. A truncated stream is refused, a JPEG one because it does not end in EOI, and JPEG-LS (CharLS) refuses mid-stream damage too ([#452](https://github.com/kvnlng/Isocenter/issues/452)).
- **16-bit `YBR_FULL` JPEG-LS and JPEG 2000 files are refused at every door.** The conversion to RGB that 8-bit `YBR_FULL` gets takes 8-bit samples only ([#461](https://github.com/kvnlng/Isocenter/issues/461)).
- **High-Throughput JPEG 2000** (`1.2.840.10008.1.2.4.201`, `.202`, `.203`) is decoded by openjpeg through `imagecodecs.jpeg2k_decode`, under JPEG 2000's rules for colour, signedness and sample width. Nothing writes it: an HTJ2K source exports uncompressed or as JPEG 2000 ([#459](https://github.com/kvnlng/Isocenter/issues/459)).
- **JPEG Baseline and Extended files Pillow cannot decode are read only when monochrome and unsigned.** 12-bit JPEG Extended decodes through `imagecodecs.jpeg_decode`, value for value as DCMTK decodes pydicom's `JPEG-lossy.dcm`. A colour one is refused, because that decoder's colour answer was measured to differ from pydicom's, and so is a signed one, because nothing here re-signs a lossy JPEG decode ([#604](https://github.com/kvnlng/Isocenter/issues/604)).
- **A JPEG 2000 codestream that is signed where PixelRepresentation 0 declares unsigned samples is refused.** No decoder here returns those samples unsigned ([#524](https://github.com/kvnlng/Isocenter/issues/524)).
- **HighBit other than BitsStored − 1 is read, and says so.** No decoder reads HighBit: samples are read right-aligned, by BitsStored or by the stream's own precision. Ingest writes one `WARNING` row per such instance, which grades the run `REVIEW_REQUIRED`, and an export writes HighBit as BitsStored − 1 ([#455](https://github.com/kvnlng/Isocenter/issues/455)).

## Dependencies

Dependencies are declared in one place, `setup.py`. There is deliberately
no `requirements.txt`: two lists drift apart, and only `install_requires`
is consulted when you `pip install`.

To set up a development environment, install the package with its
contributor extra (tests, pylint and coverage); `.[tests]` alone is enough
to run the suite:

```bash
pip install -e ".[dev]"
```

## System Requirements

Isocenter's parallel processing engine is designed to maximize CPU utilization. However, heavy operations like JPEG 2000 compression require significant memory per worker.

- **Memory**: Isocenter is memory-intensive during specific operations (e.g., Pixel Redaction, J2K Export).
  - **Minimum**: 2GB RAM per vCPU.
  - **Recommended (Heavy Workloads)**: 8GB RAM per vCPU (e.g., for massive multi-frame J2K compression).
- **Concurrency**: By default, ingest, audit and export use one worker per CPU; `redact()`, which holds a decoded frame per worker, uses half the CPUs and at most eight. Use the `ISOCENTER_MAX_WORKERS` env var to limit both if OOM occurs.
