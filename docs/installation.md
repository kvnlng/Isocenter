# Installation

Isocenter requires **Python 3.12+** on a POSIX system (Linux or macOS). It does not import on Windows: the storage layer's locks use `fcntl`.

```bash
pip install isocenter
```

Releases are published to PyPI from a release tag, via Trusted Publishing.
`main` is the development branch; to install unreleased work from it
instead:

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
- **A 16-bit colour image exported with `use_compression=True` (the default) is JPEG 2000 that pydicom cannot read with Pillow alone.** The file is conformant and lossless, and Isocenter reads it back exactly. pydicom's Pillow plugin, the only one of pydicom's JPEG 2000 plugins that installs with Isocenter, refuses every JPEG 2000 image with more than 8 bits and more than one sample per pixel ("Pillow cannot decode 16-bit multi-sample data correctly"), because Pillow would narrow it to 8 bits. pydicom with `pylibjpeg` and `pylibjpeg-openjpeg` reads it exactly. That was measured on CPython 3.12; `pylibjpeg-openjpeg` has no wheel for the free-threaded 3.14t build. For a recipient whose reader has only Pillow, export with `use_compression=False`. Each such instance is named at INFO in the export's log. pydicom on its own installs no JPEG 2000 decoder at all, so any compressed export assumes the recipient has one ([#670](https://github.com/kvnlng/Isocenter/issues/670)).
- **High-Throughput JPEG 2000** (`1.2.840.10008.1.2.4.201`, `.202`, `.203`) is decoded by openjpeg through `imagecodecs.jpeg2k_decode`, under JPEG 2000's rules for colour, signedness and sample width. Nothing writes it: an HTJ2K source exports uncompressed or as JPEG 2000 ([#459](https://github.com/kvnlng/Isocenter/issues/459)).
- **JPEG Baseline and Extended files Pillow cannot decode are read only when monochrome and unsigned.** 12-bit JPEG Extended decodes through `imagecodecs.jpeg_decode`, value for value as DCMTK decodes pydicom's `JPEG-lossy.dcm`. A colour one is refused, because that decoder's colour answer was measured to differ from pydicom's, and so is a signed one, because nothing here re-signs a lossy JPEG decode ([#604](https://github.com/kvnlng/Isocenter/issues/604)).
- **A JPEG 2000 codestream that is signed where PixelRepresentation 0 declares unsigned samples is refused.** No decoder here returns those samples unsigned ([#524](https://github.com/kvnlng/Isocenter/issues/524)).
- **Frames beyond NumberOfFrames are dropped with a `DATA_LOSS` row**, whether an offset table names them, pydicom's walk of the fragments finds them, or a native element's length holds them. `Instance.get_pixel_data()` on such a file refuses, naming both counts. A multi-fragment file declaring one frame (or none) with no offset table is read as one frame **unless it is RLE Lossless**, whose fragments are counted one per frame when there are more of them than declared frames and each begins an RLE frame header; for every other syntax the fragments alone cannot show an excess, and where pydicom can find no frame boundary in them at all the file is refused with both counts named ([#418](https://github.com/kvnlng/Isocenter/issues/418), [#620](https://github.com/kvnlng/Isocenter/issues/620), [#664](https://github.com/kvnlng/Isocenter/issues/664)).
- **A stream whose precision exceeds BitsStored is read by the stream, and says so where a value does not fit.** JPEG, JPEG-LS and JPEG 2000 streams carry their own sample precision, and DCMTK's true-lossless encoder writes BitsAllocated there (precision 16 under BitsStored 12). Every door reads each frame by its own stream's precision where that is wider than BitsStored (pydicom is asked not to mask a JPEG stream to BitsStored). Where every decoded sample still fits BitsStored nothing else happens; where one does not, ingest writes one `WARNING` row per instance (grading the run `REVIEW_REQUIRED`) and an export writes BitsStored from the samples: 16 for a 12-bit stream under BitsStored 8. A stream at or below BitsStored is read as before ([#622](https://github.com/kvnlng/Isocenter/issues/622)).
- **A stream whose decoded samples exceed its own declared precision is reported, where this library's own decoder read it.** A lossless JPEG's frame header, and a JPEG 2000 codestream's SIZ segment, declare a sample precision; a stream whose samples exceed it is read differently by different decoders. `imagecodecs` returns the samples the stream encodes (up to 4970 for a precision-12 stream), and pydicom with `pylibjpeg-libjpeg` **saturates** them to 4095. Ingest writes one `WARNING` row per instance (grading the run `REVIEW_REQUIRED`) where a decoded sample lies outside the stream's own precision: for unsigned samples under every codec whose precision is read, and for signed samples under JPEG Lossless (`.57`/`.70`) only, where BitsStored is wider than the stream's precision. Two limits come with it.
    - **The row cannot fire on pydicom's plugin route**, because a clamped array always fits its precision -- so what is reported depends on which decoder read the file.
    - **The signed arm is JPEG Lossless only, and that is a property of the decoders rather than a choice.** For JPEG Lossless (`.57`/`.70`; the signed arm is measured unreachable for `.50`/`.51`, whose decode is not sign-extended at all) the sign extension masks a sample back inside the precision only where BitsStored is no wider than it, so above that the routes differ visibly (a precision-12 stream under BitsStored 16 reads `0..4970` here and `0..4095` with the plugin) and the row is written; at or below it the divergence cannot be seen after the decode at all, and that half stays open. For **JPEG-LS** the extension always uses the frame's own precision, so a signed sample is always masked inside it and there is nothing to report. **JPEG 2000** is unreported for two reasons, because the family has two sub-cases: a *signed* codestream carries its own signedness in the SIZ segment, so negatives are ordinary data and say nothing about precision (`693_J2KR.dcm` from pydicom's test data reads `int16 [-2000, 2492]` identically through Pillow and through `imagecodecs`, and writes no row); an *unsigned* codestream under PixelRepresentation 1 is reinterpreted and sign-extended at the codestream's own precision, like JPEG-LS, so its samples are always inside that precision (measured at BitsStored 12, 13 and 16: `int16 [-1996, 1470]` on both routes, no row) ([#671](https://github.com/kvnlng/Isocenter/issues/671), [#682](https://github.com/kvnlng/Isocenter/issues/682)).
- **`pylibjpeg-libjpeg` does not install a working extension on a free-threaded interpreter, and re-enables the GIL when it is made to.** Version 2.4.0 has no free-threaded wheel, so `pip install` on CPython 3.14t builds from the sdist and the build either fails or produces a wheel with no extension module in it, depending on the compiler pip picks. What was measured here is the second: the install reported success and `import libjpeg` then failed with `ModuleNotFoundError: No module named '_libjpeg'`. Either way the plugin is not available. Built from the sdist by hand it imports, with `RuntimeWarning: The global interpreter lock (GIL) has been enabled to load module '_libjpeg', which has not declared that it can run safely without the GIL` -- so a 3.14t process that imports it runs with the GIL back on unless `PYTHON_GIL=0` is set. Nothing in Isocenter needs the plugin; without it, JPEG Lossless and JPEG-LS decode through `imagecodecs`, which does ship free-threaded wheels. This is a fact about the plugin, not a defect here, and it is recorded because which decoder answers is what the two limits above depend on ([#663](https://github.com/kvnlng/Isocenter/issues/663)).
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
