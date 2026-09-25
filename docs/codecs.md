# Codec support

Isocenter reads compressed pixel data at ingest and writes one of two transfer syntaxes on export. This page lists which decoder reads each syntax, what the export writes, and the limits a decode cannot catch.

## What reads what

Every read of a compressed frame goes through one decode: `ingest()`, `Instance.get_pixel_data()`, an icon, and the export's readback. A file gets the same answer everywhere.

| Transfer syntax | UID | Decoded by |
| :--- | :--- | :--- |
| JPEG Baseline, JPEG Extended | `1.2.840.10008.1.2.4.50`, `.51` | Pillow. Monochrome, unsigned files Pillow cannot read (12-bit JPEG Extended, for example) go through `imagecodecs`. |
| JPEG Lossless | `1.2.840.10008.1.2.4.57`, `.70` | pydicom's plugins where one is installed, otherwise `imagecodecs` |
| JPEG-LS | `1.2.840.10008.1.2.4.80`, `.81` | pydicom's plugins where one is installed, otherwise `imagecodecs` |
| JPEG 2000 | `1.2.840.10008.1.2.4.90`, `.91` | pydicom's plugins where one is installed, otherwise `imagecodecs` |
| High-Throughput JPEG 2000 | `1.2.840.10008.1.2.4.201`, `.202`, `.203` | `imagecodecs` (openjpeg) |
| RLE Lossless | `1.2.840.10008.1.2.5` | pydicom's own decoder |

`imagecodecs` and Pillow are required dependencies and install with Isocenter. `imagecodecs` is also the JPEG 2000 encoder the default compressed export uses.

## What the export writes

The export writes two transfer syntaxes and no others:

- Implicit VR Little Endian, with `use_compression=False`, and for every file with no pixel data.
- JPEG 2000 Lossless (`1.2.840.10008.1.2.4.90`), with `use_compression=True`, the default.

A source in any other syntax is decoded at ingest and re-encoded into one of those two. Nothing writes High-Throughput JPEG 2000: an HTJ2K source exports uncompressed or as JPEG 2000. [What the export writes](export-output.md#compression) covers what compression does to colour images and lossy sources.

## Decode limits

- **A stream corrupted mid-stream can decode to plausible wrong values with no error.** openjpeg (JPEG 2000, through Pillow and through `imagecodecs`) and lj92 (JPEG Lossless) report nothing for a stream with bytes damaged in the middle, and a second decoder returns the same wrong array. libjpeg-turbo (JPEG Baseline and Extended) reads a stream that lost data from its middle and still ends in its EOI marker, and fills the missing rows with mid-grey. A truncated stream is refused: a JPEG one because it does not end in EOI. JPEG-LS (CharLS) refuses mid-stream damage too.
- **16-bit `YBR_FULL` JPEG-LS and JPEG 2000 files are refused.** The conversion to RGB takes 8-bit samples only.
- **A 16-bit colour image exported with `use_compression=True` is JPEG 2000 that pydicom cannot read with Pillow alone.** The file is conformant and lossless, and Isocenter reads it back exactly. pydicom's Pillow plugin refuses every JPEG 2000 image with more than 8 bits and more than one sample per pixel. pydicom with `pylibjpeg` and `pylibjpeg-openjpeg` reads it exactly; `pylibjpeg-openjpeg` has no wheel for the free-threaded 3.14t build. For a recipient whose reader has only Pillow, export with `use_compression=False`. The export names each such instance at INFO. pydicom on its own installs no JPEG 2000 decoder, so any compressed export assumes the recipient has one.
- **High-Throughput JPEG 2000** is decoded under JPEG 2000's rules for colour, signedness and sample width.
- **JPEG Baseline and Extended files Pillow cannot decode are read only when monochrome and unsigned.** A colour one is refused, because the fallback decoder's colour output differs from pydicom's. A signed one is refused, because nothing here re-signs a lossy JPEG decode.
- **A JPEG 2000 codestream that is signed where PixelRepresentation 0 declares unsigned samples is refused.** No decoder here returns those samples unsigned.
- **Frames beyond NumberOfFrames are dropped with a `DATA_LOSS` row**, whether an offset table names them, pydicom's walk of the fragments finds them, or a native element's length holds them. `Instance.get_pixel_data()` on such a file refuses and names both counts. A multi-fragment file declaring one frame (or none) with no offset table is read as one frame, unless it is RLE Lossless: RLE fragments are counted one per frame when there are more of them than declared frames and each begins an RLE frame header. Where pydicom finds no frame boundary in the fragments, the file is refused and both counts are named.
- **A stream whose precision exceeds BitsStored is read by the stream's precision.** JPEG, JPEG-LS and JPEG 2000 streams carry their own sample precision; some encoders write BitsAllocated there (precision 16 under BitsStored 12). Where every decoded sample still fits BitsStored nothing else happens. Where one does not, ingest writes one `WARNING` row per instance, which grades the run `REVIEW_REQUIRED`, and an export writes BitsStored from the samples.
- **A stream whose decoded samples exceed its own declared precision is reported when `imagecodecs` read it.** `imagecodecs` returns the samples the stream encodes (up to 4970 for a precision-12 stream), and pydicom with `pylibjpeg-libjpeg` saturates them to 4095. Ingest writes one `WARNING` row per instance where a decoded sample lies outside the stream's own precision: for unsigned samples under every codec whose precision is read, and for signed samples under JPEG Lossless (`.57`/`.70`) where BitsStored is wider than the stream's precision. Two limits come with it:
    - The row cannot fire when pydicom's plugin read the file, because a clamped array always fits its precision. What is reported depends on which decoder read the file.
    - A signed JPEG Lossless stream beyond its own precision, under a BitsStored no wider than that precision, reads differently on the two routes and writes no row ([#682](https://github.com/kvnlng/Isocenter/issues/682), open). JPEG-LS and JPEG 2000 have nothing to report here: their sign extension keeps every sample inside the stream's precision.
- **HighBit other than BitsStored − 1 is read, and says so.** Samples are read right-aligned, by BitsStored or by the stream's own precision. Ingest writes one `WARNING` row per such instance, which grades the run `REVIEW_REQUIRED`, and an export writes HighBit as BitsStored − 1.

## Free-threaded Python and `pylibjpeg-libjpeg`

`pylibjpeg-libjpeg` has no free-threaded wheel. On CPython 3.14t, `pip install` builds it from source and either fails or installs a package whose extension does not import. Built by hand, it imports and turns the GIL back on unless `PYTHON_GIL=0` is set. Isocenter does not need it: without it, JPEG Lossless and JPEG-LS decode through `imagecodecs`, which ships free-threaded wheels. Which decoder reads a file is what the two precision limits above depend on.
