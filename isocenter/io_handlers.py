"""
IO Handlers for Isocenter.

This module provides classes for:
- DicomStore: The central catalog of DICOM objects.
- DicomImporter: Parallel file ingestion.
- DicomExporter: Writing DICOM files to disk.
- SidecarPixelLoader: Lazy loading of pixel data.
- SidecarWaveformLoader: Lazy loading of waveform samples.

**Which log lines in this module are contract (#284).** A log line is
contract exactly when it is the only channel that carries its fact to
somebody who can act on it. Where a return value, an audit row, a report
section or a raised exception carries the same fact, the log line is a
rendering of that fact for convenience, and the suite does not pin it.

This is the rule the suite follows in the large, which is what keeps a
mixed answer from being taste. Measured: 139 `caplog`/`capsys`
occurrences across 25 test files, no `caplog.set_level` anywhere, and 30
level gates of which 27 set `logging.WARNING`.
`tests/test_export_loss_audit.py` asserts a WARNING-level line precisely
because `write_tree` can never supply a store handle, so no audit row
exists to carry it; `tests/test_legacy_waveform_hydration.py` names the
redundant half of its pair and asserts only the half nothing else
records; and `tests/test_redact_error.py` asserts the exception first
and keeps the log assertion as residue of #48, where asserting the log
INSTEAD of the exception was the defect.

**In the large, not everywhere. Three tests in this repository pin a log
line the rule calls a rendering, and they are named here rather than
left for a reader to find** -- along with why a first census missed each,
because the method is the more useful half:

- `tests/test_redaction_robustness.py` pins `services.py`'s throttle,
  both its call count and its exact suppression string. It asserts
  through a `MagicMock` logger, so a `caplog` census cannot see it.
- `tests/test_redact_reports_outcome.py` pins the WARNING string
  "1 of 3" under the assertion message "a partial redaction was not
  reported anywhere" -- and it IS reported elsewhere: the same numbers
  go into a `REDACTION` audit row from `services.record_redaction_pass`,
  surfaced in report section 2 and pinned byte for byte in
  `tests/test_redaction_audit_accounting.py`. So the assertion message
  states the opposite of the truth. It was missed because its gate is
  written `at_level("WARNING")`, the string form, which a census
  matching `logging.[A-Z]` walks straight past.
- `tests/test_redaction_attestation.py` is half an exception, on the
  same string-form gate: its "0 of 1" assertion is redundant with that
  same `REDACTION` row, but its second assertion -- that the warning
  names the reason, "no configured zone that landed inside the image" --
  is genuinely single-channel and correct under the rule.

All three are left alone. Rewriting a passing test to match a ruling
written after it is how a rule stops being evidence and starts being
enforcement, and pinning anything below *because* of them would be the
same move in reverse. The three level gates that are not WARNING are
fine under the rule: `release_memory` returns nothing and writes no
audit row, a zone that fails to apply raises but does not say which zone
on what array, and `remediation.py`'s failure arm only logs.

Five operator-facing lines in this module are therefore best-effort, and
each one has another channel that a test already pins. **That is what
guards this paragraph, and it is the answer to the question the
milestone asks of it.** These justifications are prose, and no test
reads prose -- but every alternate channel named below is itself carried
by an assertion, so deleting one turns a test red rather than quietly
evaporating the reason a log line here was left unpinned. The
justification cannot rot without something going red first:

- the "Skipping N already imported files" line in `DicomImporter` --
  `IngestSummary.skipped`, set from the same count on both of the paths
  that return it, and asserted on both in
  `tests/test_ingest_failure_audit.py`. The second assertion is newer
  than the rest: until #284 only the early-return path was pinned, while
  the log line fires on both, so this justification held on the branch
  where no work happens and nowhere else.
- the per-file superseded-source warning and its "suppressing further"
  throttle -- a per-file WARNING audit row written unconditionally
  outside the throttle (the comment there calls it the compliance
  trail) and section 4 of the report -- both pinned in
  `tests/test_reingest_after_redact.py`. `IngestSummary.declined` also
  carries the count but is asserted nowhere, so it does no work in this
  justification and is not counted as one of the pinned channels.
- the per-instance scan-gap warning -- the `SCAN_GAP` audit row written
  two lines below it, surfaced through `get_audit_scan_gaps`,
  `ComplianceReport.scan_gaps` and report section 3.2 with the tag named
  (`tests/test_data_loss_reporting.py`,
  `tests/test_private_sequence_implicit_vr.py`).
- the worker's "ERROR: Export failed" line on stderr -- the
  `ExportOutcome` returned on the very next line, which becomes
  `ExportSummary.failures`, an ERROR audit row, and a `RuntimeError` on
  the `write_tree` path (`tests/test_export_failure_audit.py`,
  `tests/test_worker_loss_is_reported.py`). That line predates both
  `ExportOutcome` and `ExportSummary`; it is a fossil from when the
  worker had no return channel, and its own comment describes the
  mechanism that replaced it.
- the ERROR-level line in `_report_export_failures` -- the `failures`
  list the same loop builds and returns, plus an ERROR audit row three
  lines below carrying a byte-identical detail string
  (`tests/test_export_failure_audit.py`).

**Whenever the probe samples one of these five, `SURVIVED` is the
correct result.** A survivor is a question, not a verdict; this
paragraph is the answer, and the reason not to re-file #284.

The wording is conditional because the probe's sample is not stable, and
this is worth knowing before reading any of its reports. It picks
mutation sites by INDEX -- `step = max(1, total // budget)` at
scripts/mutation_probe.py line 1641 and `for i in range(0, total, step):`
at scripts/mutation_probe.py line 1644 -- so removing a site anywhere in this file
renumbers every site after it and silently changes which lines get
sampled. Measured on this very change: at `b223f6a` the module had 380
sites and the sample selected all five of the lines above, which is why
#284 was filed against all five; replacing one `and` with an `isinstance`
call in the same PR took it to 378, and the sample now reaches only the
first two. Nothing about the other three changed. A line that stops
appearing in a probe report has not been fixed, and a line that starts
appearing has not regressed.

Deleting or silencing any of these lines would still be wrong -- they
are what an operator watching a terminal sees -- but a test that pinned
their wording would pin a rendering, not a fact.
"""

import concurrent.futures
import contextlib
import os
import pickle
import sys
import hashlib
import struct
from math import ceil
from typing import List, Dict, Any, Optional, Tuple, Iterable, Mapping
from datetime import datetime, date
from dataclasses import dataclass, field

import pydicom
import numpy as np
# Unguarded, and at module scope, beside the other declared dependencies.
# `imagecodecs>=2024.6.1` is in `install_requires` and already drives the
# *decode* side in `imagecodecs_handler.py`, so the project already trusts
# it with pixel fidelity in the other direction; it now drives the JPEG
# 2000 encode as well (#404). Deliberately not a `try/except ImportError`
# like the `from PIL import Image` it replaces: a guarded import whose
# absence turns into `Compression failed` is the same shape as a loader
# returning `[]` for a missing shipped resource, and this release removes
# that shape rather than adding one. `python-dotenv` is the precedent --
# a declared dependency imported plainly.
#
# The encoder is bound as a module-level name rather than reached as
# `imagecodecs.jpeg2k_encode` at each call, and that is not style.
# `imagecodecs` resolves its codecs through a module-level `__getattr__`
# that delegates back to itself, so `mock.patch` on the attribute leaves
# the module recursing (`RecursionError: maximum recursion depth
# exceeded`) once the patch is undone. A name in *this* module is patchable
# without reaching into a third-party module's lazy loader at all. The
# module import stays for `__version__`, which the refusal message quotes.
import imagecodecs
from imagecodecs import jpeg2k_encode
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.pixels import get_decoder
from pydicom.pixels.decoders.base import DecodeRunner
from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless
from pydicom.tag import Tag
from pydicom.datadict import dictionary_VR
try:
    from pydicom.encapsulate import encapsulate
except ImportError:
    from pydicom.encaps import encapsulate
from pydicom.encaps import generate_frames
from pydicom.multival import MultiValue
from pydicom.valuerep import validate_value
from pydicom.sequence import Sequence
from pydicom.dataset import Dataset
from pydicom.charset import default_encoding
from pydicom.dataelem import DataElement
from pydicom.filebase import DicomBytesIO
from pydicom.filereader import read_sequence
from pydicom.filewriter import write_sequence

from .entities import (Patient, Study, Series, Instance, Equipment, DicomItem,
                       resolve_item_path)
from .logger import (describe_exception, describe_exception_without_paths,
                     get_logger)
from .pixel_geometry import (
    FLOAT_DTYPE_BY_ELEMENT,
    SIDECAR_DTYPE_NAMES,
    GeometryEvidence,
    PIXEL_DTYPE_ATTR,
    TAG_DOUBLE_FLOAT_PIXEL_DATA,
    TAG_FLOAT_PIXEL_DATA,
    declared_int,
    resolve_photometric_interpretation,
    resolve_pixel_geometry,
)
from .blob_kind import serialize_blob_kind
from .imagecodecs_handler import (J2K_SYNTAXES, JPEGLS_SYNTAXES,
                                  T81_SYNTAXES,
                                  _j2k_irreversible,
                                  _jpeg_frame_type, _sign_extend,
                                  _jpegls_near, _stream_precision,
                                  colour_conversion, convert_colour,
                                  decode_declared_frames, extended_offsets,
                                  frame_precisions,
                                  frame_count_mismatch_words,
                                  offset_table_frame_count,
                                  signed_codestream_refusal)
from .parallel import run_parallel, _resolve_strategy
from .validation import IODValidator
from .sidecar import SidecarManager
from .waveform import filter_dangling_annotation_refs


from .store import DicomStore
from .config_manager import ConfigLoader


#: Binary elements that `populate_attrs` skips but that are *not* lost --
#: each is extracted and written to the sidecar elsewhere. They must stay
#: out of the DATA_LOSS report or every ingest files a loss that did not
#: happen (#137).
#:
#: This was decorative for (7fe0,0010) until #169: the whole-group skip
#: above the VR check meant the frozenset was never consulted for it.
#: It is load-bearing now, and so is the depth it is consulted at --
#: `_is_routed` below, not `tag in _ROUTED_BINARY_TAGS`.
_ROUTED_BINARY_TAGS = frozenset({
    Tag(0x7fe0, 0x0010),   # Pixel Data
    Tag(0x5400, 0x1010),   # Waveform Data
})

#: The Item tag (FFFE,E000) as it appears on the wire, little endian.
#: A private element that pydicom resolved to `UN` and whose value
#: starts with these four bytes is a sequence whose VR the transfer
#: syntax did not carry (#167). Four bytes is a weak signal on its own
#: -- any vendor blob may begin with them by chance -- so it only
#: selects candidates for `_sequence_from_un_bytes`, which proves or
#: refuses each one.
_ITEM_TAG_LE = b"\xfe\xff\x00\xe0"

#: Tags whose routing depends on where in the instance they sit, and the
#: depth at which they are routed. `ingest_worker` finds Pixel Data with
#: `if "PixelData" in ds` -- a top-level lookup -- so the copy inside an
#: Icon Image Sequence item is routed nowhere. Nothing puts it in the
#: blob store either: `instance_blobs` is UNIQUE(instance_uid, kind),
#: one pixel blob per instance, so a second one needs a `kind` that
#: names the sequence item. That is not a schema change -- `kind` is
#: unconstrained TEXT and only `persist_blob`'s literal tuple gates it
#: -- but it is a re-merge path on the export side and a decision about
#: what `kind` means, which #150 also has an interest in. Filed as #183,
#: whose first half (the top-level float pair) has landed and whose
#: second half -- this one -- has not.
#:
#: (5400,1010) is deliberately absent. Waveform Data is *never* at the
#: top level -- it lives inside Waveform Sequence items -- so a depth
#: rule would report the one group that is routed. Which of several
#: multiplex groups was kept is an index question, not a depth question,
#: and #160 already reports the discarded ones from the group count.
_ROOT_ONLY_ROUTED_TAGS = frozenset({
    Tag(0x7fe0, 0x0010),   # Pixel Data
})

#: The float pixel elements. Routed the same way Pixel Data is, since
#: #183's first half: `ingest_worker` extracts the array and writes it
#: to the sidecar, and `_export_instance_worker` writes it back under
#: its own tag (#170, #193). Reporting them at ingest would file a
#: `DATA_LOSS` row reading "not in the exported data" about an element
#: that is in the exported data -- the defect #194 opened against the
#: first cut of this fix, pointed at a second tag.
#:
#: Two conditions, both applied in `_is_routed`, and both still exactly
#: as load-bearing as they were. Top level only, because the extraction
#: reads the top level. And only when the instance has no Pixel Data of
#: its own: `ingest_worker`'s arms are `if "PixelData" ... elif` the
#: float pair, so on a file carrying both -- which PS3.5 Section 8.2
#: forbids, but malformed input exists -- Pixel Data is what reaches the
#: sidecar and the float half is genuinely lost. One question, one
#: answer, and it is the same answer `has_pixel_data` gave before the
#: bytes moved.
#:
#: #183's second half now carries nested (7fe0,0010) -- see
#: `_NESTED_PIXEL_DATA_TAG` and `serialize_blob_kind`. The nested *float*
#: pair is deliberately still not carried: the grammar spells it
#: (`pixels:0040,0555/0/7fe0,0008`) and the shape is unreachable from a
#: conformant file, the float elements being top-level Image Pixel Module
#: members with no macro that nests them. So `_is_routed` keeps its answer
#: for them and `test_float_pixel_data_inside_a_sequence_item_is_reported`
#: keeps its row. Do not widen the carriage by depth alone; the carriage is
#: keyed on the tag.
_FLOAT_PIXEL_TAGS = frozenset({
    Tag(0x7fe0, 0x0008),   # Float Pixel Data
    Tag(0x7fe0, 0x0009),   # Double Float Pixel Data
})

#: The one nested element whose bytes are carried (#183). Named rather than
#: written inline because three places have to agree about it: the
#: collector in `populate_attrs`, the decode in `ingest_worker`, and the
#: terminal tag of the blob kind those two produce.
_NESTED_PIXEL_DATA_TAG = Tag(0x7fe0, 0x0010)

#: The transfer syntaxes a nested icon may be decoded from (#183 Q6).
#:
#: **An allow-list, not a list of the lossy ones, and that direction is the
#: whole point.** A deny-list has to be complete to be safe, and it is
#: wrong the moment the standard adds a syntax -- silently, in the
#: direction that ships pixels. This list is wrong in the direction that
#: files a `DATA_LOSS` row, which is a reported non-carriage rather than a
#: mis-declared image. It is the same discipline `SIDECAR_DTYPE_NAMES`
#: applies to the dtype carrier: allow-list, never interpret.
#:
#: Why the lossy syntaxes were excluded, and why two of them are now in:
#: a lossy-JPEG icon decodes to RGB from a declared `YBR_FULL_422`, so the
#: exported item's Photometric Interpretation has to be rewritten to
#: match the bytes. When #183 wrote this list that was "a correctness
#: claim with no measurement behind it"; #372 measured it (a `YBR_FULL_422`
#: item under JPEG Baseline, and `YBR_ICT`/`YBR_RCT` under JPEG 2000, each
#: decoded through the borrowed `file_meta` -- RGB bytes, decoder meta
#: `RGB`, identical with Pillow alone and with the pylibjpeg plugins), and
#: `_decode_nested_pixels` now takes the colour space from the decoder's
#: meta through the same `_decode_pixels` the top level uses. The top
#: level had the same problem for every 8-bit YBR source, native ones
#: included; it is fixed in the same change. So JPEG Baseline (`.4.50`)
#: and JPEG 2000 (`.4.91`) are in.
#:
#: JPEG Extended (`.4.51`) and JPEG-LS Near-Lossless (`.4.81`) joined them
#: in #387, measured the same way through a sequence item. Under `.4.51`,
#: pydicom's Pillow plugin decodes an 8-bit baseline stream and labels it
#: RGB exactly as under `.4.50`; a true 12-bit SOF1 icon still fails to
#: decode here and keeps its loss row, because admission only stops the
#: gate refusing what the decoder can read -- it claims no decode. `.4.81`
#: decodes through the imagecodecs fallback (#416), within the stream's
#: NEAR bound, under the labels `_FALLBACK_PHOTOMETRICS` gives it.
#:
#: HTJ2K (`.4.203`) joined `.4.201` and `.4.202` in #459, measured (N6 in
#: `tests/test_nested_pixel_carriage.py`). Until then nothing here decoded
#: HTJ2K -- pydicom has no plugin for it without pylibjpeg-openjpeg -- so
#: `.4.203` stayed out, and `.4.201` and `.4.202` were listed and no better
#: off: every such icon dropped from the decode's `except` arm with the
#: generic row. The imagecodecs fallback now decodes all three through
#: `jpeg2k_decode`, a 4x4 icon exactly. An allow-list's whole point is that
#: its unmeasured side is the refusing side; this one was measured.
#:
#: Written as UID strings rather than `pydicom.uid` names on purpose: the
#: names are not stable across pydicom versions, and a draft of #183's spec
#: cited `JPEGLossyCompressedPixelTransferSyntaxes`, which does not exist.
#: `tests/test_nested_pixel_carriage.py` checks each string against
#: pydicom's own constant where a name for it exists.
_CARRIABLE_TRANSFER_SYNTAXES = frozenset({
    "1.2.840.10008.1.2",        # Implicit VR Little Endian (native)
    "1.2.840.10008.1.2.1",      # Explicit VR Little Endian (native)
    "1.2.840.10008.1.2.1.99",   # Deflated Explicit VR Little Endian
    "1.2.840.10008.1.2.2",      # Explicit VR Big Endian (native)
    "1.2.840.10008.1.2.5",      # RLE Lossless
    "1.2.840.10008.1.2.4.50",   # JPEG Baseline (Process 1), measured (#372)
    "1.2.840.10008.1.2.4.51",   # JPEG Extended (Process 2 & 4), measured (#387)
    "1.2.840.10008.1.2.4.57",   # JPEG Lossless, Non-Hierarchical
    "1.2.840.10008.1.2.4.70",   # JPEG Lossless, First-Order Prediction
    "1.2.840.10008.1.2.4.80",   # JPEG-LS Lossless
    "1.2.840.10008.1.2.4.81",   # JPEG-LS Near-Lossless, measured (#387)
    "1.2.840.10008.1.2.4.90",   # JPEG 2000 Image Compression (Lossless Only)
    "1.2.840.10008.1.2.4.91",   # JPEG 2000 Image Compression, measured (#372)
    "1.2.840.10008.1.2.4.201",  # HTJ2K Lossless, measured (#459)
    "1.2.840.10008.1.2.4.202",  # HTJ2K Lossless RPCL, measured (#459)
    "1.2.840.10008.1.2.4.203",  # HTJ2K, measured (#459)
})

#: The transfer syntaxes `_decode_pixels` decodes through `imagecodecs`
#: when pydicom cannot (#416), so that `ingest()` accepts what
#: `Instance.get_pixel_data()` reads. **This is the one place to narrow
#: that**, and it is wider than the 16-bit colour JPEG 2000 cell #416 was
#: filed for. Measured with pydicom 3.0.2, Pillow 12.3.0 and imagecodecs
#: 2026.8.16 (`get_decoder(ts).available_plugins`):
#:
#: | Transfer syntax | pydicom plugins here | in this set |
#: | --- | --- | --- |
#: | .5 RLE Lossless | `pydicom` | no |
#: | .50 / .51 JPEG Baseline / Extended | `pillow`, 12-bit refused | yes, monochrome |
#: | .57 / .70 JPEG Lossless | none | yes |
#: | .80 / .81 JPEG-LS | none | yes |
#: | .90 / .91 JPEG 2000 | `pillow`, 16-bit multi-sample refused | yes |
#: | .201 / .202 / .203 HTJ2K | none | yes |
#:
#: HTJ2K since #459 (owner ruling Q6): pydicom decodes it only with
#: pylibjpeg-openjpeg, not a dependency, and `jpeg2k_decode` reads it
#: exactly, so it takes every JPEG 2000 row below
#: (`imagecodecs_handler.J2K_SYNTAXES` says why not `htj2k_decode`).
#:
#: So every JPEG Lossless and JPEG-LS file was refused at ingest, and now
#: ingests when its decode matches its header under a colour space
#: `_FALLBACK_PHOTOMETRICS` labels for it. RLE is out because pydicom's RLE
#: decoder needs no dependency, and the handler has no RLE arm (#447):
#: pydicom's is the one that decodes.
#:
#: **JPEG Baseline and Extended joined for #604**, monochrome only. Pillow
#: refuses 12-bit JPEG Extended, and pydicom's own `JPEG-lossy.dcm` was
#: refused at ingest while the Instance door read it through the handler
#: #453 then deleted. `imagecodecs.jpeg_decode` returns that file and
#: `JPGExtended.dcm` value for value as DCMTK's `dcmdjpeg` does, at
#: imagecodecs 2024.6.1 and 2026.8.16 (review of #606, M2). Colour stays
#: out (`_FALLBACK_JPEG`). UID strings, for the reason
#: `_CARRIABLE_TRANSFER_SYNTAXES` gives.
_IMAGECODECS_FALLBACK_SYNTAXES = frozenset({
    "1.2.840.10008.1.2.4.50",   # JPEG Baseline, monochrome (#604)
    "1.2.840.10008.1.2.4.51",   # JPEG Extended, monochrome (#604)
    "1.2.840.10008.1.2.4.57",   # JPEG Lossless, Non-Hierarchical
    "1.2.840.10008.1.2.4.70",   # JPEG Lossless, First-Order Prediction
    "1.2.840.10008.1.2.4.80",   # JPEG-LS Lossless
    "1.2.840.10008.1.2.4.81",   # JPEG-LS Near-Lossless
    "1.2.840.10008.1.2.4.90",   # JPEG 2000 (Lossless Only)
    "1.2.840.10008.1.2.4.91",   # JPEG 2000
    "1.2.840.10008.1.2.4.201",  # HTJ2K Lossless
    "1.2.840.10008.1.2.4.202",  # HTJ2K Lossless RPCL
    "1.2.840.10008.1.2.4.203",  # HTJ2K
})

#: The colour space the fallback stores for each declared one, per transfer
#: syntax: ``{syntax: {declared label: stored label}}``. pydicom's decoder
#: *states* what colour space it returned (#372). `imagecodecs` does not,
#: and does not read PlanarConfiguration either, so the stored label is
#: what a decode under that syntax has been *measured* to return for that
#: declaration, and a declaration with no entry is refused. Keyed on
#: exactly `_IMAGECODECS_FALLBACK_SYNTAXES`; a syntax missing here labels
#: nothing.
#:
#: - **Monochrome and palette indices** come back as stored under every
#:   syntax here (measured: a PALETTE COLOR JPEG Lossless frame decodes to
#:   its index array, and pydicom's `as_array` applies no palette either).
#: - **RGB** maps to itself under all three families, where a colour
#:   decode is measured exact and interleaved, 8- and 16-bit. A
#:   planar/interleaved swap holds the same samples in the same shape, so
#:   no check after the decode would see one; only a measurement can.
#:   **JPEG Lossless joined in #387.** #416 left it out believing no
#:   colour JPEG Lossless stream could be built here: `ljpeg_encode`
#:   refuses three components. `jpeg8_encode(lossless=True)` does not, and
#:   its 3-component streams decode exactly, predictors 1 and 5, at
#:   imagecodecs 2024.6.1 and 2026.8.16, with every channel distinct so a
#:   plane swap would show. Measure with that encoder, not `ljpeg_encode`,
#:   before concluding a colour row is unmeasured. YBR stays out under
#:   JPEG Lossless: no YBR stream was measured.
#: - **`YBR_RCT` and `YBR_ICT` map to RGB under JPEG 2000** (#448).
#:   `jpeg2k_decode` undoes the codestream's colour transform and returns
#:   RGB, whatever the multiple-component-transform flag says (measured
#:   under both, 8- and 16-bit), and pydicom's own plugins label that
#:   output RGB too. Repeating the declared label over RGB samples is
#:   #372's defect; refusing it, as this table did first, turned away a
#:   file both doors can read.
#: - **8-bit `YBR_FULL` maps to RGB under JPEG-LS** (#448, confirmed by
#:   the owner with #464). A JPEG-LS stream has no colour transform, and
#:   `jpegls_decode` returns the YBR samples as stored, so the *handler*
#:   converts, with pydicom's `convert_color_space`. That is the function
#:   pydicom's door applies (#372), and what pydicom with pyjpegls stores
#:   for the same file. The conversion is `imagecodecs_handler.CONVERTS_TO`
#:   and not this module's since #464, so the read doors make it too. 16-bit is refused
#:   here before the decode: `convert_color_space` refuses `uint16`. Every
#:   door refuses it, since the Instance door decodes through
#:   `_decode_pixels` (#453); #461 recorded that as a limit (Q5).
#:
#: Which relabels the decoder has already done is data, not a branch on
#: syntax: `_FALLBACK_DECODER_CONVERTS`. A relabel under any other syntax
#: is a conversion the handler makes (`CONVERTS_TO`), 8-bit only.
_FALLBACK_GREY = {label: label for label in
                  ("MONOCHROME1", "MONOCHROME2", "PALETTE COLOR")}
_FALLBACK_J2K = {**_FALLBACK_GREY, "RGB": "RGB",
                 "YBR_RCT": "RGB", "YBR_ICT": "RGB"}
_FALLBACK_JPEGLS = {**_FALLBACK_GREY, "RGB": "RGB", "YBR_FULL": "RGB"}
_FALLBACK_LJPEG = {**_FALLBACK_GREY, "RGB": "RGB"}
#: JPEG Baseline and Extended (#604): monochrome, and no palette, colour
#: or YBR row. `jpeg_decode` applies a colour transform to a 3-component
#: stream with no Adobe marker that pydicom and DCMTK do not (measured on
#: `SC_jpeg_no_color_transform.dcm`, 137 apart), so a colour label here
#: would be a guess; and every 8-bit colour stream met so far is one
#: Pillow decodes before the fallback is asked.
_FALLBACK_JPEG = {label: label for label in ("MONOCHROME1", "MONOCHROME2")}
_FALLBACK_PHOTOMETRICS = {
    "1.2.840.10008.1.2.4.50": _FALLBACK_JPEG,
    "1.2.840.10008.1.2.4.51": _FALLBACK_JPEG,
    "1.2.840.10008.1.2.4.57": _FALLBACK_LJPEG,
    "1.2.840.10008.1.2.4.70": _FALLBACK_LJPEG,
    "1.2.840.10008.1.2.4.80": _FALLBACK_JPEGLS,
    "1.2.840.10008.1.2.4.81": _FALLBACK_JPEGLS,
    "1.2.840.10008.1.2.4.90": _FALLBACK_J2K,
    "1.2.840.10008.1.2.4.91": _FALLBACK_J2K,
    # HTJ2K: `jpeg2k_decode` undoes a reversible colour transform to the
    # exact RGB source, at 8 and 16 bits (#459, measured). An irreversible
    # `YBR_ICT` stream under .201 and .203 is stored as RGB within the
    # lossy transform's error, 1 at 8 bits and 2 at 16 (measured by the
    # review of #606); the row is JPEG 2000's, whose decoder it is.
    "1.2.840.10008.1.2.4.201": _FALLBACK_J2K,
    "1.2.840.10008.1.2.4.202": _FALLBACK_J2K,
    "1.2.840.10008.1.2.4.203": _FALLBACK_J2K,
}
#: The syntaxes whose decoder returns the stored label's colour space
#: itself, so a relabel there is a label change only (see above). Every
#: door reads this one table since #453, which deleted the handler's own
#: copy (`DECODER_RELABELS`).
_FALLBACK_DECODER_CONVERTS = frozenset({
    "1.2.840.10008.1.2.4.90",
    "1.2.840.10008.1.2.4.91",
    "1.2.840.10008.1.2.4.201",
    "1.2.840.10008.1.2.4.202",
    "1.2.840.10008.1.2.4.203",
})


#: The descriptors a nested payload is reshaped from, in a fixed order, with
#: the defaults `SidecarPixelLoader` applies. One tuple, read by one
#: function, so the provenance captured at ingest and the destination read
#: at export cannot be assembled differently.
_NESTED_GEOMETRY_TAGS = (
    ("0028,0010", 0),   # Rows
    ("0028,0011", 0),   # Columns
    ("0028,0002", 1),   # SamplesPerPixel
    ("0028,0008", 0),   # NumberOfFrames
    ("0028,0100", 8),   # BitsAllocated
    ("0028,0103", 0),   # PixelRepresentation
)


class _PhotometricRefusal(RuntimeError):
    """A Photometric Interpretation no output could state honestly (#502).

    A `RuntimeError` subclass, so the export worker and every caller
    handle it exactly as they handle `_J2kFrameRefusal`: the exception
    type, the `ExportError`, the `wrote 0 of N` and the empty output
    directory are all unchanged. It exists as a distinct type so a
    caller can tell "this library will not write that label" apart from
    a failed write.

    **The one refusal on this path.** The ruling for a format this
    library can read and cannot write is warn and attempt the best
    output; a multi-valued label is the exception, because there is no
    best output -- no single value can be chosen without inventing one,
    and a file carrying two values of a VM 1 attribute cannot be read
    back by this library at all: `ingest()` hashes the label while
    decompressing, before anything examines it. "What this library can
    read back" is the standard `_J2K_ENCODABLE_FRAMES` already refuses
    on.

    That measurement is of a file **with pixel data**, which is the only
    kind this exception is raised for -- the pixel arms, through
    `_write_pixel_geometry`, and a pixel element built into `attributes`
    by hand, through `_pixel_less_label_warning`. A
    pixel-less file carrying two labels has no decompression step to trip
    over and re-ingests cleanly (measured), so the writer warns about it
    and writes it (`_pixel_less_label_warning`, #534), and it is
    `_readback_label_mismatch` that refuses it, on the arity alone, for a
    caller who asked for `verify_readback=True`.

    **Raised on what the file would carry, not on what was declared.**
    At one sample the geometry resolver has already answered
    `MONOCHROME2`, so an instance whose *declaration* is multi-valued
    still writes a single-valued, re-ingestible file -- and that file is
    delivered. Refusing it on the declaration would deny the caller an
    output this library can read back, which is the one thing this
    exception is not for.
    """


#: The Photometric Interpretations this library holds against the
#: transfer syntax a file is written under (#502, #507). Two callers,
#: two independent inputs: the export writer judges the label it just
#: put on `ds` against the syntax it is about to write, and
#: `_verify_readback` judges the label in the *delivered file* against
#: that file's own syntax -- so the verifier is a second wall rather
#: than the writer checking its own belief, and a hand-built file
#: reaches it.
#:
#: A **positive** rule, per PS3.3 C.7.6.3.1.2, and a label with no cell
#: here is judged inadmissible: a label a syntax does not define is
#: exactly what this is about, and today such a file already fails the
#: readback at the decode (`ValueError: Unknown (0028,0004) ...
#: 'NONSENSE'`) while the default export writes it in silence.
#:
#: **One deliberate widening, with its reason, because a strict table
#: would fail this library's own output:**
#:
#: - `YBR_ICT` is admitted under JPEG 2000 *lossless* alongside
#:   `YBR_RCT`, though `level=0` is the reversible transform. The owner's
#:   ruling on #490 is that a source already labelled `YBR_ICT`/`YBR_RCT`
#:   is encoded with the transform and **keeps its label**, and #516
#:   ships that, so a table admitting only `YBR_RCT` would warn about,
#:   and refuse, files this exporter writes on purpose.
#:
#: **And one that is gone (#525).** `YBR_PARTIAL_422`/`YBR_PARTIAL_420`
#: stood on the J2K row while only the native half had been ruled on, so
#: the same instance warned natively and passed compressed. They are not
#: admitted under JPEG 2000: the codestream holds full-sample components
#: and PS3.5 A.4.4 gives no subsampled label to a J2K codestream. Such a
#: label is written as declared with a WARNING, because `YBR_FULL` would
#: misstate the value range (PS3.3 C.7.6.3.1.2) and there is no bare
#: `YBR_PARTIAL`. `_compress_j2k` still encodes it `mct=False` (its case
#: 3): the judgement moved, the encoder did not. The writer and the
#: readback tightened at once, which is why one table serves both.
#:
#: Retired labels (`HSV`, `ARGB`, `CMYK`) are admitted everywhere. The
#: question here is what a *syntax* can carry, not whether a label is
#: current, and this exporter writes them as declared today.
_PHOTOMETRIC_ANY_SYNTAX = frozenset({
    "MONOCHROME1", "MONOCHROME2", "PALETTE COLOR", "RGB",
    "YBR_FULL", "YBR_FULL_422", "HSV", "ARGB", "CMYK",
})
_ADMISSIBLE_PHOTOMETRICS = {
    # The three uncompressed syntaxes share a row. This exporter writes
    # only Implicit VR LE (`_create_ds`), but the readback reads a file,
    # and a hand-built one under Explicit VR LE or BE must be judged by
    # the same rule rather than falling through the table.
    "1.2.840.10008.1.2": _PHOTOMETRIC_ANY_SYNTAX,
    "1.2.840.10008.1.2.1": _PHOTOMETRIC_ANY_SYNTAX,
    "1.2.840.10008.1.2.2": _PHOTOMETRIC_ANY_SYNTAX,
    "1.2.840.10008.1.2.4.90": _PHOTOMETRIC_ANY_SYNTAX | {
        "YBR_ICT", "YBR_RCT"},
}

#: Why a label the written syntax does not admit is inadmissible, and
#: what the caller can do about it. Keyed on the label, because the
#: reason is a property of the label and not of the syntax: `YBR_ICT`
#: names a codestream transform wherever it appears, and
#: `YBR_PARTIAL_*` names a layout no syntax this exporter writes can
#: carry. The remedies differ for exactly that reason -- compressing
#: gets the transform labels a syntax that applies the transform they
#: name, and does nothing at all for the subsampled ones -- so a single
#: shared sentence would be false for two of the four.
_PHOTOMETRIC_INADMISSIBLE = {
    "YBR_ICT": (
        "these two labels name the irreversible and reversible "
        "multiple-component transforms of a JPEG 2000 codestream, and "
        "uncompressed pixel data has no codestream to carry one.",
        "Export with use_compression=True, where the transform is applied "
        "and the label is true of the codestream, or declare the label "
        "these bytes have with set_attr(\"0028,0004\", ...)."),
    "YBR_PARTIAL_422": (
        "it names a subsampled layout that a full-sample (rows, cols, 3) "
        "array does not have, and no transfer syntax this exporter writes "
        "admits it.",
        "Declare the label these bytes have -- RGB or YBR_FULL -- with "
        "set_attr(\"0028,0004\", ...)."),
    None: (
        "no value of that name is defined for it.",
        "Declare one of the labels the syntax admits with "
        "set_attr(\"0028,0004\", ...)."),
}
_PHOTOMETRIC_INADMISSIBLE["YBR_RCT"] = _PHOTOMETRIC_INADMISSIBLE["YBR_ICT"]
_PHOTOMETRIC_INADMISSIBLE["YBR_PARTIAL_420"] = \
    _PHOTOMETRIC_INADMISSIBLE["YBR_PARTIAL_422"]


#: The labels pydicom converts to RGB on a default decode -- pydicom 3.0.2
#: `_process_color_space`'s own set -- which is the decode `ingest()`
#: makes and the readback's stored-sample decode does not (#596). Its
#: conversion refuses any samples but unsigned 8-bit
#: (`convert_color_space`: `arr.dtype != np.dtype("u1")`), so a file
#: carrying one of these over 16-bit or int8 samples is conformant and
#: cannot be ingested.
_PYDICOM_CONVERTS = frozenset({"YBR_FULL", "YBR_FULL_422"})


def _pydicom_converts_samples_of(dtype) -> bool:
    """Whether pydicom's colour conversion takes samples of `dtype` (#596).

    pydicom 3.0.2 `convert_color_space` refuses any array whose dtype is
    not `u1`. A `bool` mask is written at BitsAllocated 8 and
    reads back as `uint8`, so it counts as the samples it becomes. One
    predicate for the readback's second decode and the export's note, so
    the two cannot answer differently.
    """
    dtype = np.dtype(dtype)
    return dtype == np.uint8 or dtype == np.bool_


#: The three elements a Photometric Interpretation can describe: the
#: integer one and the two float ones (PS3.3 C.7.6.24, C.7.6.25). Named
#: because the readback's reason has to say something different when the
#: file carries none of them -- the remedies in
#: `_PHOTOMETRIC_INADMISSIBLE` all talk about the pixels, and "export
#: with use_compression=True" is no help to an instance with nothing to
#: compress (#507 review).
_PIXEL_ELEMENTS = ("PixelData", "FloatPixelData", "DoubleFloatPixelData")

#: The remedy for a label on an instance that has no pixel element at
#: all. Its own sentence rather than a fourth row in
#: `_PHOTOMETRIC_INADMISSIBLE`, because that table is keyed on the label
#: and this is a property of the file.
_PHOTOMETRIC_NO_PIXELS = (
    "This file carries no pixel element at all -- no (7fe0,0010), "
    "(7fe0,0008) or (7fe0,0009) -- so the label describes nothing and no "
    "remedy involving the pixels applies. An instance with no pixels "
    "should carry no (0028,0004); the value reaching the file is the one "
    "the graph declared.")

#: The remedy for an inadmissible label on an Icon Image Sequence item
#: (#602). A property of the door, as `_PHOTOMETRIC_NO_PIXELS` is of the
#: file: the table's ICT/RCT remedy says "export with
#: use_compression=True", which is false for an icon, since
#: `_write_back_nested_pixels` writes every icon raw.
_PHOTOMETRIC_ICON = (
    "An icon is written uncompressed whatever transfer syntax the file "
    "carries (PS3.5 A.4 allows either, and this exporter never compresses "
    "one), so compressing the export does not change this. Declare the "
    "label these bytes have with set_attr(\"0028,0004\", ...) on that "
    "sequence item.")


def _written_photometric(value) -> Optional[str]:
    """One label, normalized for comparison against a syntax's row (#502).

    Stripped and upper-cased, because that is what reaches a *reader*: a
    CS is space-padded to even length in the file and a declaration is
    not normalized on the way in, so an instance declaring `' rgb '` puts
    `' rgb '` on `ds` (measured) and writes a label every conformant
    reader takes as `RGB`. Comparing it unnormalized would warn about a
    label that is perfectly admissible -- and would equally miss
    `' ybr_ict '`, which is one that is not.

    Deliberately **not** `declared_int`'s rule, which reads `[1]` as
    "not declared": that function answers what the graph declared, and
    this answers what the file will carry. Two questions, two readings.

    **No one-element unwrap here, and that is measured rather than
    assumed.** pydicom unwraps a one-element value on assignment
    (`ds.PhotometricInterpretation = ['YBR_ICT']` leaves the `str`
    `'YBR_ICT'`) and returns a `str` for any single-valued element it
    reads back, so by the time either caller has a value there is no
    one-element list left to handle. A *multi*-valued one is each
    caller's own check, spelled at the call site rather than hidden in a
    `None` from here: the writer refuses it (`_PhotometricRefusal`) and
    the readback fails the file.

    An absent or empty value returns `None`: there is no claim to judge.

    (The two normalizations are named in words rather than written as
    the dotted calls they are, on purpose:
    `tests/test_documented_api_exists.py` reads a dotted call inside any
    string in this package as a method the package promises its callers
    (#234), and `str`'s methods are not ours to promise. Read the code
    below for the spelling.)
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


#: The longest value a Code String may hold (PS3.5 6.2), and how much of a
#: declared label a correction note repeats. A label is a header value,
#: not patient-derived text, but a hand-built graph can hold any string
#: there, and a note must not become a vehicle for one.
_CS_MAX_CHARS = 16


def _cs_spelling(value):
    """`value` as a Code String is spelled, when it is text; else itself."""
    return value.strip().upper() if isinstance(value, str) else value


def _cs_quoted(value) -> str:
    """`value` quoted for a note, at most a Code String's length of it."""
    if not isinstance(value, str):
        return repr(value)
    if len(value) > _CS_MAX_CHARS:
        return f"{value[:_CS_MAX_CHARS]!r} (cut at {_CS_MAX_CHARS} characters)"
    return repr(value)


def _label_as_written(attributes) -> Tuple[Mapping, Optional[str]]:
    """The attributes the worker writes from, with (0028,0004) spelled as a
    CS value is defined, and the correction note when that changed what a
    reader sees (#532).

    A declared `' rgb '` used to reach the file as `' rgb'` -- a CS is
    right-stripped on read, not left-stripped, and never case-folded --
    and pydicom and `ingest()` refuse that file (`Unknown (0028,0004)
    'Photometric Interpretation' value ' rgb'`), so the export delivered
    a file this library could not read, with `ok=True`. PS3.5 6.2 defines
    a Code String as upper case with insignificant leading and trailing
    spaces, so writing `RGB` is the same label, spelled as defined, and
    the samples are untouched: an exact correction, #506's class, INFO
    and no row.

    **A copy, never the graph.** The returned mapping is a shallow copy
    of `attributes` holding a *new* value for the one key, and `attributes`
    itself is returned unchanged when there is nothing to respell. The
    worker runs in the caller's process under threads, so writing the
    spelling back onto `inst.attributes` would edit the live graph from an
    export (`tests/test_export_worker_graph_purity.py`).

    **Both readers of the declaration take the copy**: `_merge`, because
    pydicom emits `UserWarning: Invalid value for VR CS` on the caller's
    stream when the raw value is assigned, and `_write_pixel_geometry`,
    because it falls back to the declared value when the resolver answers
    None and would write the raw spelling back over the corrected one.
    Nothing else reads `0028,0004` from the worker's attributes.

    **The note fires only when a reader would see a difference** --
    compared against the right-stripped declaration, so `'RGB '` is
    respelled silently: its trailing pad is what the file does anyway.

    A multi-valued declaration has each text value respelled, so the
    pixel arms' refusal and the pixel-less arm's warning name clean
    values; the note is still one. Anything that is not text (`bytes`, a
    number) is left as it is: there is no CS spelling to restore.

    Only `0028,0004`, by ruling: it is the element a decoder keys on and
    the one that made this library's own file unreadable. Other Code
    String elements are written as held (#603).

    **Every sequence item takes the same copy** (#602):
    `DicomExporter._merge_sequences` respells each item's `0028,0004`
    through this before its `_merge`, for the same two reasons one depth
    down -- pydicom's `UserWarning` fires on the item's assignment, and an
    icon labelled `' rgb'` was dropped by this library's own re-ingest.
    Its note names the item. At the top level the two readers above are
    still the only ones.
    """
    value = attributes.get("0028,0004")
    if isinstance(value, str):
        declared = [value]
        respelled = value.strip().upper()
        if respelled == value:
            return attributes, None
        written_value = respelled
    elif isinstance(value, (list, tuple, MultiValue)) and any(
            isinstance(v, str) for v in value):
        declared = list(value)
        written_value = [_cs_spelling(v) for v in value]
        if written_value == declared:
            return attributes, None
    else:
        return attributes, None
    copy = dict(attributes)
    copy["0028,0004"] = written_value
    written = [written_value] if isinstance(written_value, str) \
        else written_value
    if all(not isinstance(d, str) or d.rstrip() == w
           for d, w in zip(declared, written)):
        return copy, None
    # One of the two is always true here: a value with no leading
    # whitespace and no lower case has a right-strip equal to its CS
    # spelling, and returned above.
    changes = []
    if any(isinstance(d, str) and d.upper() != d for d in declared):
        changes.append("its letters upper-cased")
    if any(isinstance(d, str) and d.lstrip() != d for d in declared):
        changes.append("its leading spaces removed")
    spelled = ", ".join(_cs_quoted(d) for d in declared)
    as_written = ", ".join(_cs_quoted(w) for w in written)
    return copy, (
        f"PhotometricInterpretation {spelled} is not a defined Code String "
        f"spelling; written as {as_written}, the same label with "
        f"{' and '.join(changes)} "
        f"(PS3.5 6.2: a CS value is upper case, and its leading and "
        f"trailing spaces are not significant). The samples are unchanged.")


def _label_inadmissibility(label, syntax_uid) -> Optional[Tuple[str, str]]:
    """The table's `(clause, remedy)` for a label `syntax_uid` does not admit.

    None when the syntax has no row (measured rows only, the discipline
    `_FALLBACK_PHOTOMETRICS` keeps), when there is no label, or when the
    label is admitted. `label` is already normalized
    (`_written_photometric`).

    **The one judgement.** The writer's sentence (`_photometric_warning`),
    the readback's (`_readback_label_mismatch`) and the icon's
    (`_icon_label_warning`, #602) each call this and keep only their own
    sentence and remedy override, so an icon is judged by the top level's
    rule and not by a copy of it.
    """
    admitted = _ADMISSIBLE_PHOTOMETRICS.get(str(syntax_uid))
    if admitted is None or label is None or label in admitted:
        return None
    return _PHOTOMETRIC_INADMISSIBLE.get(label,
                                         _PHOTOMETRIC_INADMISSIBLE[None])


def _photometric_warning(label, syntax_uid, *,
                         has_pixels: bool) -> Optional[str]:
    """One sentence for a label the written syntax does not admit (#502).

    Returns None when the syntax has no row (measured rows only, the
    discipline `_FALLBACK_PHOTOMETRICS` keeps), when there is no label,
    or when the label is admitted. The sentence names what was declared,
    what the file was written under, and which one the code used --
    everything the row in the compliance report has to carry.

    `has_pixels` is keyword-only and **has no default** (#534), the
    `float_element`/`syntax_uid` precedent: a file with no pixel element
    has no samples the label was written "over", and every remedy in
    `_PHOTOMETRIC_INADMISSIBLE` talks about the pixels -- "export with
    use_compression=True" cannot help an instance with nothing to
    compress. So a caller has to say which kind of file it is judging,
    and the pixel-less one gets `_PHOTOMETRIC_NO_PIXELS`.
    """
    found = _label_inadmissibility(label, syntax_uid)
    if found is None:
        return None
    clause, remedy = found
    if has_pixels:
        kept = ("The label was written as declared, over the samples the "
                "instance held, and neither was changed.")
    else:
        kept = "The label was written as declared."
        remedy = _PHOTOMETRIC_NO_PIXELS
    return (f"PhotometricInterpretation {_cs_quoted(label)} is not a label "
            f"the transfer syntax this file was written under admits "
            f"({syntax_uid}): {clause} {kept} {remedy}")


def _pixel_less_label_warning(ds) -> Optional[str]:
    """The label judgement for a file with no pixel element (#534).

    `_write_pixel_geometry` judges the label on the two arms that write a
    pixel element. The third arm -- an instance with none, an SR or a
    waveform-only file carrying a pixel descriptor it has no use for --
    never calls it, and `_merge` had already put whatever `0028,0004` the
    graph declared on `ds`, so the file carried it unexamined: measured,
    `YBR_ICT` and an undefined label exported `ok=True` with no warning.

    **Read off `ds` after `_finalize_dataset`, never off the worker's
    `written_syntax`.** A file with no pixel data is written natively
    even under `compression="j2k"` -- `_compress_j2k` has nothing to
    encode and leaves `file_meta` alone -- while `written_syntax` says
    JPEG 2000 for it, and the J2K row admits `YBR_ICT`. The syntax is the
    one the file will carry.

    **A multi-valued label is warned about, not refused.** The pixel arms
    refuse it (`_PhotometricRefusal`) because such a file cannot be read
    back; a pixel-less one can -- measured, `ingest()` returns
    `ingested=1` with the graph carrying both values -- so the refusal's
    reason is false here, and the write-path ruling applies: written as
    declared, with a WARNING. `verify_readback=True` still fails it on
    the arity (`_readback_label_mismatch`). The exception is a pixel
    element put into `attributes` by hand, which reaches this arm too:
    that file has pixels, so it gets the pixel sentences and, for a
    multi-valued label, the pixel arms' refusal.
    """
    label = ds.get("PhotometricInterpretation")
    if label is None:
        return None
    # A pixel element can still reach this arm: `set_attr("7fe0,0010",
    # ...)` on an instance with no pixel array is written by `_merge`,
    # and no pixel arm runs. Such a file is judged here -- nothing else
    # judges it -- but with the sentences true of a file that has pixels,
    # and a multi-valued label on it gets the pixel arms' refusal, whose
    # reason holds for it. Measured on the review of #609: the warning
    # said "which has no pixel element" over a file carrying `PixelData`.
    has_pixels = any(kw in ds for kw in _PIXEL_ELEMENTS)
    if isinstance(label, (list, tuple, MultiValue)) and len(label) > 1:
        if has_pixels:
            raise _multi_valued_refusal(label)
        return (f"PhotometricInterpretation (0028,0004) is VM 1; this "
                f"instance, which has no pixel element, declares "
                f"{len(label)} values "
                f"({', '.join(_cs_quoted(str(v)) for v in label)}). "
                f"Written as declared: a file with no pixels re-ingests "
                f"carrying all of them. {_PHOTOMETRIC_NO_PIXELS}")
    return _photometric_warning(
        _written_photometric(label),
        str(getattr(ds.file_meta, "TransferSyntaxUID", "") or ""),
        has_pixels=has_pixels)


def _multi_valued_refusal(written) -> "_PhotometricRefusal":
    """The refusal for a file with pixels declaring several labels (#502).

    One sentence for both places that raise it: the pixel arms
    (`_write_pixel_geometry`) and a pixel element built into `attributes`
    by hand, which reaches the third arm (#534).
    """
    return _PhotometricRefusal(
        f"PhotometricInterpretation (0028,0004) is a single value; "
        f"this instance declares {len(written)} "
        f"({', '.join(repr(str(v)) for v in written)}). A file "
        f"carrying more than one cannot be read back by this library "
        f"at all -- ingest refuses it before any label is examined -- "
        f"so no output here would be honest, which is why this one "
        f"case is refused where an inadmissible label is written with "
        f"a warning. Declare one label with "
        f"set_attr(\"0028,0004\", ...).")


#: The uncompressed syntax an icon's label is judged against (#602). Any
#: of the three native rows would do -- they are one set -- and this is the
#: one `_create_ds` writes.
_ICON_WRITTEN_SYNTAX = "1.2.840.10008.1.2"


def _icon_label_warning(label, at) -> Optional[str]:
    """The WARNING for an icon label uncompressed pixel data does not admit.

    `label` is normalized (`_written_photometric`); `at` is the item's
    path in `_item_path_words`' spelling. Judged against the
    **uncompressed** row whatever syntax the file carries, because an icon
    is always written raw (`_write_back_nested_pixels`): the J2K row
    admits `YBR_ICT`, and a `YBR_ICT` icon inside a `.90` file holds no
    codestream for the label to be true of. The table's clause is kept and
    its remedy is not -- `_PHOTOMETRIC_ICON` is the icon's.
    """
    found = _label_inadmissibility(label, _ICON_WRITTEN_SYNTAX)
    if found is None:
        return None
    clause, _remedy = found
    return (f"PhotometricInterpretation {_cs_quoted(label)} on the icon at "
            f"{at} is not a label uncompressed pixel data admits: {clause} "
            f"The label was written as declared, over the samples the item "
            f"held, and neither was changed. {_PHOTOMETRIC_ICON}")


def _icon_label_arity_warning(label, at) -> str:
    """The WARNING for an icon declaring several labels (#602, Q5).

    Warned about and written, not refused: the pixel arms refuse a
    multi-valued top-level label because no output would be honest, but
    an icon is not a reason to lose the instance (#433), and the user can
    fix it with one `set_attr`. Measured: this library's own ingest drops
    such an icon with the unrouted `DATA_LOSS` row.
    """
    quoted = ", ".join(_cs_quoted(str(v)) for v in label)
    return (f"PhotometricInterpretation (0028,0004) is VM 1; the icon at "
            f"{at} declares {len(label)} values ({quoted}). Written as "
            f"declared, over the samples the item held; this library's own "
            f"ingest drops an icon so labelled, with a DATA_LOSS row. "
            f"Declare one label with set_attr(\"0028,0004\", ...) on that "
            f"sequence item.")


#: PixelRepresentation (0028,0103) in the words PS3.5 6.2 uses, for the
#: one place that has to name a declared value in prose (#499). A value
#: outside the two the standard defines is named as such rather than
#: guessed at: `declared_int` will have read it as an integer, and a
#: correction note saying "PixelRepresentation 2 (unsigned)" would be a
#: second wrong claim about the same element.
_SIGNEDNESS = {0: "unsigned", 1: "signed"}
_UNDEFINED_SIGNEDNESS = "neither 0 nor 1, the only values defined"


#: The numpy dtype an integer frame decodes to, keyed on BitsAllocated
#: and indexed by PixelRepresentation: `(unsigned, signed)`.
#:
#: It replaces `uint16 if bits > 8 else uint8`, which decoded a 32-bit
#: frame two times too wide and a 64-bit one four times too wide -- so
#: `SidecarPixelLoader` raised `Integrity Error: ... holds 32 samples;
#: geometry (4, 4) needs 16` on an RTDOSE-shaped uint32 instance that had
#: ingested cleanly (#386). #373's bound was right; the dtype it was
#: checking against was not.
#:
#: **Callers must keep the legacy rule as a fallback rather than
#: subscripting this table directly.** The keys are the four widths a
#: numpy integer array can actually have, and DICOM declares widths that
#: are not among them: `BitsAllocated 1` is a real ingested population --
#: a binary Segmentation, whose packed bits pydicom unpacks to one uint8
#: per pixel -- and `BitsAllocated 12` arrives as uint16. A dict-only
#: rewrite turns both into a `KeyError` on the load path, which is a
#: regression, not a tidy-up.
_INTEGER_DTYPE_BY_BITS = {
    8:  (np.uint8,  np.int8),
    16: (np.uint16, np.int16),
    32: (np.uint32, np.int32),
    64: (np.uint64, np.int64),
}


def _integer_dtype(bits: int, pixel_representation: int):
    """The dtype for a declared width and signedness, table then fallback.

    One function because there were three copies of `uint16 if bits > 8
    else uint8` when #386 was filed and only one of them had ever been
    fixed: a signed frame rebuilt by a stale copy came back unsigned for
    exactly the reason `SidecarPixelLoader`'s did. The third copy, in
    `_compress_j2k`'s reconstruct-from-bytes branch, was deleted with that
    branch as unreachable (#404); this is what remains, with one caller.
    """
    unsigned, signed = _INTEGER_DTYPE_BY_BITS.get(
        bits, (np.uint16, np.int16) if bits > 8 else (np.uint8, np.int8))
    return signed if pixel_representation == 1 else unsigned


def nested_item_geometry(attributes) -> tuple:
    """The reshape descriptors of one sequence item, from its attributes.

    Args:
        attributes: A `DicomItem.attributes`-shaped mapping.

    Returns:
        tuple: `(rows, cols, samples, frames, bits, pixel_representation)`.
    """
    return tuple(int(attributes.get(tag, default) or default)
                 for tag, default in _NESTED_GEOMETRY_TAGS)


@dataclass
class NestedPixelRef:
    """Where one nested payload's bytes live in the sidecar (#183).

    A *reference*, deliberately not a `SidecarPixelLoader`. A loader carries
    the geometry it will reshape against, and geometry captured when the ref
    was wired is the wrong geometry to reshape against later: the path is
    recorded at ingest and resolved at export, and everything in between --
    hydration, audit, remediation, redaction -- can change the sequence's
    contents. A loader built at ingest would reshape a shifted icon against
    the numbers it was born with and succeed, writing the wrong bytes into
    the wrong item, silently.

    So the loader is built at the point of use, from the item resolved
    *then*, and the reshape it already performs is the shift guard. One
    construction site, one check, and no stored geometry to disagree with
    the graph. See `_write_back_nested_pixels`.

    Mutable because `_rewire_sidecar_loaders` repoints `offset`/`length`
    after a compaction, exactly as it does for the top-level loader.

    `geometry` is the one thing it does carry about shape, and it is
    **provenance, not a reshape target**: the descriptors the enclosing item
    declared when these bytes were wired to it. The export compares it
    against the descriptors of the item the path resolves to *then*, and a
    mismatch means an index shifted under us -- the path still resolves, to
    the wrong item, and writing there would be silent wrong bytes.

    That comparison is not belt-and-braces over the loader's reshape;
    **the loader does not raise on a mismatch.** Measured: given more
    elements than the target shape needs, `SidecarPixelLoader.__call__`
    takes its padding fallback -- `if arr.size >= target_size: arr =
    arr[:target_size]` -- and silently *truncates*; given fewer, it returns
    a 1-D array. That fallback exists for the one-byte DICOM pad on an
    odd-length frame and must not be tightened, because the top-level path
    depends on it. So the guard has to be here, and it compares descriptors
    rather than byte counts: the obvious `BitsAllocated // 8` formula is 0
    for a 1-bit icon and would refuse every one of them as a shift.
    """
    sidecar_path: str
    offset: int
    length: int
    alg: str
    blob_hash: Optional[str] = None
    geometry: Optional[tuple] = None

#: Indices into the encapsulated Pixel Data fragment stream. Not data,
#: and so not a loss -- a different question from `_is_routed`'s, which
#: is why it is a different name rather than another member of it.
#:
#: The Extended Offset Table is byte offsets and lengths relative to the
#: first fragment item tag, and (7fe0,0003) is that stream's total
#: length. Ingest decodes the pixel data and the export re-writes it
#: uncompressed, so the fragment layout these describe does not exist in
#: the exported file: they cannot be carried, and their absence loses
#: nothing recoverable. The pixels themselves round-trip exactly.
#:
#: Reporting them put two `DATA_LOSS` rows and two warnings on every
#: encapsulated instance carrying an EOT -- which is the mechanism DICOM
#: added for large multi-frame objects, so the noise landed where it was
#: least welcome (#194). It is also the failure the comment inside
#: `populate_attrs` warns about, arrived at from the other side.
#:
#: These three are the group's non-binary members (`OV`, `OV`, `UV`),
#: and `import_files`' reason clause says "binary-VR elements are not
#: held in the object graph". Exempting them makes that prose true
#: again. A future non-binary member of this group has to be added here
#: *or* given a reason clause of its own -- do not let it inherit this
#: one.
_DERIVED_PIXEL_INDEX_TAGS = frozenset({
    Tag(0x7fe0, 0x0001),   # Extended Offset Table
    Tag(0x7fe0, 0x0002),   # Extended Offset Table Lengths
    Tag(0x7fe0, 0x0003),   # Encapsulated Pixel Data Value Total Length
})

#: The retention boundary for unrouted binary values, in bytes: a value
#: at or below it is held in the object graph whatever its wire VR
#: (`OB`/`OW`/`OF`/`OD`/`OL` and `UN` alike); a value above it is
#: dropped with a `DATA_LOSS` row, whatever its wire VR (#151).
#:
#: One rule for both populations, because VR was never the property
#: anyone meant. The old gate skipped `BINARY_VRS` and kept `UN` ("for
#: safety, usually small private tags"), and under Implicit VR every
#: private element *is* `UN` -- so the identical bytes were dropped
#: from an explicit-VR source and silently retained, megabyte blobs
#: included, from an implicit-VR one. De-identification outcome and
#: memory footprint both tracked the wire format rather than the data.
#:
#: 65534 is not a round number pulled from the air; it is PS3.5's own
#: boundary. It is the largest value length a 16-bit explicit-VR length
#: field can carry (§7.1.2), and §6.2.2 Note 4 obliges every conformant
#: explicit-VR encoder to relabel anything longer as `UN` -- so at and
#: below this size the wire itself can still say what an element is in
#: either syntax, and a retention rule keyed here cannot be told two
#: stories about one value. (A leaf value's raw length is
#: syntax-independent; sequence lengths are not, which is one reason
#: recovered sequences are exempt from this rule -- see the `UN`
#: handling in `populate_attrs`.) It also bounds what retention can
#: cost: at most 64 KiB per element resident (about 87 KiB as base64 in
#: `attributes_json`, where `_split_core_and_private` keeps every
#: `bytes` value), which keeps "usually small" true by construction
#: while the megabyte vendor blobs -- the population the memory
#: guarantee on 100GB+ datasets is about -- stay out of the graph and
#: in the loss report. Pixel and waveform bytes are unaffected either
#: way: they are routed to the sidecar before this rule is consulted.
BINARY_RETENTION_MAX_BYTES = 65534


#: Private VRs whose Python value must be an integer, mapped to the
#: inclusive range each can actually encode (PS3.5 Table 6.2-1; `AT` is
#: a four-byte tag, so it takes `UL`'s range).
#:
#: They are here for two reasons and the second is not the first over
#: again. pydicom accepts a `str` at `add_new` for these -- with a
#: `UserWarning` -- and then raises `struct.error: required argument is
#: not an integer` from `filewriter.write_numbers` when the dataset is
#: written, which fails the whole export rather than the offending
#: element. Measured on pydicom 3.0.2 for `US`, `UL` and `FL`; the
#: siblings are here by the same encoding rule (all binary-encoded).
#: And being an `int` is not enough either: `add_new` accepts any Python
#: integer without complaint, so a private `US` holding `70000` writes
#: an element `_merge` reports no loss for and then raises `OSError:
#: 'H' format requires 0 <= number <= 65535` out of the export -- past
#: `_merge`'s `try`, so again the file rather than the element. A source
#: element is always in range; this is the value a later `set_attr` or a
#: remediation rule put there, and the whole point of the gate is that
#: such a value takes the fallback (#154).
_INTEGER_VR_RANGE = {
    'US': (0, 0xFFFF),
    'SS': (-0x8000, 0x7FFF),
    'UL': (0, 0xFFFFFFFF),
    'SL': (-0x80000000, 0x7FFFFFFF),
    'UV': (0, 0xFFFFFFFFFFFFFFFF),
    'SV': (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    'AT': (0, 0xFFFFFFFF),
}

#: The same, for the binary floating-point VRs, mapped to the `struct`
#: format the writer will pack with -- so the gate can ask the writer's
#: question rather than a paraphrase of it. `FD` is here for symmetry:
#: every Python `float` is a double, so it never declines, and leaving
#: it out would make the table read as though `FL` were the only float
#: VR.
_FLOAT_VR_PACK = {'FL': '<f', 'FD': '<d'}

#: The VR names alone, derived rather than restated. Two spellings of
#: "which VRs are binary integers" is exactly the drift
#: `test_every_binary_vr_the_gate_accepts_has_a_way_back_out_of_the_store`
#: exists to catch one table further out.
_INTEGER_VRS = frozenset(_INTEGER_VR_RANGE)
_FLOAT_VRS = frozenset(_FLOAT_VR_PACK)

#: Text VRs and the per-value character cap PS3.5 Table 6.2-1 gives
#: each. The cap is checked per *value*, never against a multi-valued
#: join -- the standard bounds each value, which is the same rule
#: `_fallback_multivalue` already applies.
_TEXT_VR_MAX = {
    'AE': 16, 'AS': 4, 'CS': 16, 'DA': 8, 'DS': 16, 'DT': 26,
    'IS': 12, 'LO': 64, 'LT': 10240, 'PN': 64, 'SH': 16, 'ST': 1024,
    'TM': 16, 'UI': 64,
}

#: The text VRs PS3.5 Table 6.2-1 leaves uncapped. Listed, not inferred
#: from absence in `_TEXT_VR_MAX`: "no cap recorded" is also what an
#: unknown VR and every binary VR look like, so reading absence as
#: "unbounded text" would accept a recorded VR this module knows nothing
#: about. An earlier comment claimed these three were handled by that
#: absence, and `UC` -- the only one not also in `_VM_ONE_TEXT_VRS` --
#: was in fact rejected on every path, so a private `UC` element
#: recorded its VR at ingest and still exported as `UT`.
_TEXT_VR_UNCAPPED = frozenset({'UT', 'UR', 'UC'})

#: The text VRs whose value multiplicity is fixed at 1, so a backslash
#: in the value is ordinary text rather than the value delimiter
#: (PS3.5 6.2). Every other text VR here is 1-n, where a backslash
#: re-splits the value on read -- #195, from the other side. `UC` is the
#: one uncapped VR that is 1-n, which is why the two sets are not one.
_VM_ONE_TEXT_VRS = frozenset({'ST', 'LT', 'UT', 'UR'})

#: The text VRs whose value has a *format*, not only a length: a date, a
#: time, a datetime, a UID, an age. For these a value inside the cap can
#: still be no value of the VR -- `ANONYMIZED` is 10 characters, inside
#: TM's 16, and is no time -- and `_value_fits_vr` asks pydicom's own
#: `validate_value` about it (#571). Without that, a TM, DT or UI a REPLACE
#: had emptied of meaning kept its recorded VR with an invalid value and a
#: pydicom `UserWarning`, while a DA (cap 8) happened to fall back.
_FORMAT_CHECKED_VRS = frozenset({'DA', 'DT', 'TM', 'UI', 'AS'})


def _value_fits_vr(value, vr: str) -> bool:
    """Can `value` be written under `vr` without changing what it says?

    The gate in front of a recorded private VR (#154). A recorded VR is
    a fact about the value the *source file* carried; anonymisation,
    redaction or a plain `set_attr` can replace that value with one the
    VR no longer suits, and deferring to it then would write an element
    that is conformant-looking and wrong. Answering False sends the tag
    to `_fallback_encoding`, which is exactly what happened before this
    existed -- so a False here is never worse than no recorded VR at
    all. **A wrong True is.** It is not a shrug back to the old
    behaviour; it is one of two new failures, and both were measured on
    pydicom 3.0.2 before the checks below existed:

    * A value `add_new` accepts and `Dataset` conversion then refuses --
      a non-numeric string under `IS` or `DS` -- raises inside
      `_merge`'s `try` and files a `DATA_LOSS` row for an element that
      used to export perfectly well as `LO`. Being a `str` short enough
      for the cap is not enough for these two; the text has to name a
      number.
    * A value both of those accept and `filewriter` then refuses -- an
      out-of-range integer under `US`, or a `float` too large for `FL`
      -- raises *past* `_merge`'s `try`, from `write_numbers`, and fails
      the whole file rather than the element. That is precisely the trap
      `_fallback_encoding`'s own docstring names, and a recorded VR must
      not reopen it.

    So "does the value fit" is asked in the writer's terms, not
    Python's: the type, then the range or the parse, then the cap. The
    cap applies to a number's rendered text too (`_TEXT_VR_MAX` bounds
    `IS` at 12 characters and `DS` at 16), because pydicom writes an
    over-long one without a word.

    For the VRs whose value has a format -- DA, DT, TM, UI and AS
    (`_FORMAT_CHECKED_VRS`) -- it asks pydicom's `validate_value` too
    (#571): a value inside the cap that names no date, time or UID is not
    a value of the VR, and writing it under that VR is the
    conformant-looking and wrong element this gate exists to refuse. An
    empty value passes; it is conformant under all five.

    What it deliberately does **not** ask is the character *repertoire*:
    a private `CS` whose value an anonymisation rule replaced with
    lower-case text still takes its recorded VR. pydicom writes it with
    a warning, the value is byte-faithful, and nothing is lost -- so
    this is a conformance nicety and not the class of failure above,
    where an element or a whole file goes missing. Do not read the
    absence as an oversight; adding it would need a per-VR repertoire
    table, and the fallback's `LO` is not conformant for that value
    either.

    `bool` is refused for every numeric VR before the `int` test, and
    the ordering is the mechanism: `bool` is an `int` subclass, so
    `True` under a private `US` would be written as `1`. That is the
    outcome `_fallback_encoding`'s own `bool` arm was pre-placed to
    prevent (#283), and this is the day it was pre-placed for.

    Args:
        value: The value about to be written.
        vr (str): The VR recorded for this tag at ingest.

    Returns:
        bool: True when the pairing is safe to write.
    """
    if value is None:
        # Documentation, not behaviour: `None` already reached the
        # closing `return False` by matching no `isinstance` arm. It is
        # spelled out because a reader who finds the `v is None` branch
        # in `_merge` will come here to "clean it up" by widening this
        # function, and this is where the reason not to lives. Widening
        # it makes `_value_fits_vr([None, 'B'], 'LO')` true through the
        # list arm below, `add_new` accepts the list, and `filewriter`
        # raises past `_merge`'s try -- the whole file rather than the
        # element (#344).
        #
        # Deleting this line, or flipping it to `True`, leaves every
        # test green, because the `None` branch in `_merge` runs first
        # and nothing else reaches here with a `None`. It is an
        # equivalent mutant on purpose; do not write a test around it.
        return False

    if isinstance(value, tuple):
        # A `tuple` is the one sequence shape `add_new` does not convert
        # to the element's multi-value form: it reaches `struct.pack` as
        # a tuple and fails the whole file with "required argument is
        # not an integer". Nothing in the pipeline produces one --
        # pydicom hands back `MultiValue`, the store hands back `list`
        # -- so this is a hand-`set_attr` shape, and `_fallback_multivalue`
        # has always accepted it. Declining here leaves it exporting
        # exactly as it did before #154.
        return False

    if isinstance(value, (list, MultiValue)):
        # Every value of a multi-valued element must fit, and the VR
        # must be able to express multiplicity at all: writing two of
        # three vendor values is the disguised loss `_fallback_multivalue`
        # exists to refuse.
        if vr in _VM_ONE_TEXT_VRS:
            return False
        return bool(value) and all(_value_fits_vr(a, vr) for a in value)

    if isinstance(value, (bytes, bytearray, memoryview)):
        # PS3.5 §6.2.2: raw bytes are `UN`, which is what the fallback
        # already writes.
        return False

    if isinstance(value, bool):
        return False

    if isinstance(value, int):
        if vr in _INTEGER_VR_RANGE:
            low, high = _INTEGER_VR_RANGE[vr]
            return low <= value <= high
        # `IS` values arrive from pydicom as `IS`, an `int` subclass
        # whose text form is what gets written -- so the cap that bounds
        # that text bounds this too.
        return vr == 'IS' and len(str(value)) <= _TEXT_VR_MAX['IS']

    if isinstance(value, float):
        pack = _FLOAT_VR_PACK.get(vr)
        if pack is not None:
            try:
                struct.pack(pack, value)
            except (struct.error, OverflowError, ValueError):
                return False
            return True
        # `DSfloat` is a `float` subclass, same reasoning as `IS`, and
        # pydicom renders it with `str()` -- `str(1 / 3)` is 18
        # characters, two past what `DS` may carry.
        return vr == 'DS' and len(str(value)) <= _TEXT_VR_MAX['DS']

    if isinstance(value, str):
        if vr in _INTEGER_VRS or vr in _FLOAT_VRS:
            # pydicom raises from `filewriter` rather than from
            # `add_new`, so this would fail the export, not the element.
            return False
        if vr not in _TEXT_VR_MAX and vr not in _TEXT_VR_UNCAPPED:
            return False
        if '\\' in value and vr not in _VM_ONE_TEXT_VRS:
            return False
        cap = _TEXT_VR_MAX.get(vr)
        if cap is not None and len(value) > cap:
            return False
        if vr in ('IS', 'DS') and value.strip():
            # `IS` and `DS` are text on the wire, but pydicom converts
            # them on the way in and raises for text that names no
            # number -- `ValueError: could not convert string to float`
            # -- which `_merge` catches and reports as data loss for an
            # element that exported as `LO` before #154. The empty
            # string is exempt: it is a conformant empty value and
            # pydicom writes it. `IS` goes one step further than `DS`,
            # because it takes the integer of what it parsed: `DS` of
            # `"nan"` writes, `IS` of `"nan"` raises `cannot convert
            # float NaN to integer`. All measured on pydicom 3.0.2.
            try:
                if vr == 'IS':
                    int(float(value))
                else:
                    float(value)
            except (TypeError, ValueError, OverflowError):
                return False
        if vr in _FORMAT_CHECKED_VRS:
            try:
                validate_value(vr, value, pydicom.config.RAISE)
            except ValueError:
                return False
        return True

    return False


def _is_routed(tag, is_root: bool, has_pixel_data: bool) -> bool:
    """Does something else in the pipeline carry this element's bytes?

    Args:
        tag: The pydicom `Tag` of the element being skipped.
        is_root (bool): True when the element sits directly on the
            instance, False when it sits inside a sequence item.
        has_pixel_data (bool): True when the instance also carries a
            top-level (7fe0,0010). Only the float pair reads this: the
            export prefers the sidecar array, so a float element beside
            Pixel Data is not carried by anything.

    Returns:
        bool: True if something else in the pipeline carries these bytes,
        so skipping them here loses nothing.
    """
    if tag in _FLOAT_PIXEL_TAGS:
        return is_root and not has_pixel_data
    if tag in _ROOT_ONLY_ROUTED_TAGS:
        return is_root
    return tag in _ROUTED_BINARY_TAGS


#: Modalities that must not export without pixel data. Two places
#: refuse to write a pixel-less file for one of these -- the missing
#: source-file guard and the float refusal below it -- and #193 was
#: opened because they disagreed: the float guard set `arr = None`
#: *after* the modality check had already passed, producing exactly the
#: file that check exists to prevent. One constant, consulted twice, is
#: what makes them agree by construction rather than by review.
_IMAGE_MODALITIES = frozenset({"CT", "MR", "US", "DX", "CR",
                               "MG", "NM", "PT", "XA", "RF", "SC", "OT"})

#: How a `DATA_LOSS` audit entry is graded: PRIVATE and SIGNAL take
#: `validation_status` to REVIEW_REQUIRED, STANDARD does not. Written by
#: the emitter and stored on the audit row rather than re-derived, and
#: why they differ, are argued once each -- CHANGELOG.md, #146 and #150.
#:
#: SIGNAL is acquired content that was in the source and is not in the
#: export: a discarded waveform multiplex group (#150), or an icon dropped
#: because pixel data is redacted (#542). It exists because group parity
#: was a proxy for "how much should the reader care" and those emitters
#: broke it -- both live under standard tags, and neither is an annotation
#: layer with a defined home elsewhere, which is what makes an overlay
#: routine. The discriminator is what the loss *was*: STANDARD stays the
#: scope for routine standard-group drops (widening the grading test to
#: STANDARD would take every overlay with it).
LOSS_SCOPE_PRIVATE = "PRIVATE"
LOSS_SCOPE_STANDARD = "STANDARD"
LOSS_SCOPE_SIGNAL = "SIGNAL"

#: The scopes that cost a run its PASS. `generate_report` tests
#: membership here rather than naming scopes itself, so the
#: classification stays emitter-side: adding a scope means deciding, at
#: the emitter, whether it grades -- never teaching the report to
#: re-derive the answer from prose (#146, #150).
GRADED_LOSS_SCOPES = frozenset({LOSS_SCOPE_PRIVATE, LOSS_SCOPE_SIGNAL})


def loss_scope_for_tag(tag: str) -> str:
    """Classify a lost element for grading, by the parity of its group.

    Odd group is private, even is standard -- the same split the store
    already uses to decide where an attribute is written. What each
    scope does to `validation_status`, and why they differ, is in
    CHANGELOG.md under #146.

    Args:
        tag (str): A `"gggg,eeee"` lowercase-hex tag.

    Returns:
        str: `LOSS_SCOPE_PRIVATE` or `LOSS_SCOPE_STANDARD`.

    Raises:
        ValueError: If `tag` is not in `"gggg,eeee"` form. Deliberately
            not caught: every caller holds a tag it has already parsed,
            so an unparseable one is a bug, and defaulting it to
            "standard" would silently downgrade a real loss.
    """
    group = int(tag.split(",")[0], 16)
    return LOSS_SCOPE_PRIVATE if group % 2 else LOSS_SCOPE_STANDARD


def _sequence_from_un_bytes(raw: bytes, tag, encoding) -> Optional[Sequence]:
    """Re-parse `UN` bytes as an implicit-VR sequence, or return None.

    Under Implicit VR Little Endian a private sequence has no VR on the
    wire and no dictionary entry, so pydicom resolves it to `UN` and
    hands back bytes. The structure is still in those bytes; nothing
    downstream can see it, because the PHI scan walks `sequences` and
    there is no entry there to walk (#167).

    Returns the parsed `Sequence` only when re-encoding it reproduces
    `raw` byte for byte. That is the whole safety argument: the caller
    replaces an attribute with a structure, and the equality proves the
    two are the same bytes, so nothing is lost by the substitution and
    nothing is guessed. Three adversarial inputs get past
    `read_sequence` without raising and without leaving bytes unread --
    a garbage item length, an undefined-length item with no delimiter,
    and an empty item followed by vendor payload that happens to sit
    behind the item tag -- and all three re-encode to something else.
    The last one is the reason this is not "parse and hope": it decodes
    to one empty item, and accepting it would delete the payload.

    Those three are the *shape* of what rule 4 refuses, not the set the
    tests use. `tests/test_private_sequence_implicit_vr.py` parametrizes
    a different four, chosen so each names the rule that refuses it and
    so every one can be written into a DICOM file -- an
    undefined-length item with no delimiter is a description, not a
    fixture. Kept separate on purpose: this paragraph is the refusal
    space, the test set is what is pinned (#167).

    Returns None for every failure, and the caller keeps the bytes. An
    ingest must not raise on a malformed private element: the file is
    still readable, and the value is still exportable.

    Args:
        raw (bytes): The element's value, exactly as pydicom handed it
            over.
        tag: The element's `Tag`. Used only to build the `DataElement`
            the re-encode needs; `write_sequence` writes the items, not
            the element header, so the tag never reaches the comparison.
        encoding: The enclosing dataset's character set, so text decodes
            the way it would have if pydicom had parsed the sequence
            itself.

    Returns:
        Optional[Sequence]: The parsed sequence, or None if any of the
        four rules refuses it.
    """
    if not raw.startswith(_ITEM_TAG_LE):
        return None

    fp = DicomBytesIO(raw)
    # Symmetry and insurance, not requirement -- and 0.9.0 said the
    # opposite here, so read this before deleting either line.
    # `read_sequence` takes implicitness and endianness as positional
    # arguments and threads them down itself; it consults NEITHER
    # attribute on the stream. The one place implicitness could be
    # re-derived, `_is_implicit_vr` (pydicom `filereader.py:336`),
    # short-circuits at `:368-369` on `is_sequence` before reading a
    # byte, and `read_sequence` always passes `is_sequence=True` from
    # here. Set them wrong, or not at all, and this parse is identical;
    # `tests/test_pydicom_deprecations.py` pins that, so a pydicom
    # release that STARTS reading them turns red here instead of
    # silently refusing every vendor block.
    #
    # The `_tag_packer` AttributeError the old comment cited is real,
    # but it belongs to the WRITE stream below and to
    # `is_little_endian`, whose setter builds the packers
    # (`filebase.py:121-133`); the `is_implicit_VR` setter only
    # type-checks and stores (`filebase.py:147-152`).
    fp.is_little_endian = True
    fp.is_implicit_VR = True
    try:
        parsed = read_sequence(fp, True, True, len(raw), encoding)
    except Exception:      # pylint: disable=broad-except
        # Deliberately broad. A malformed private element must not fail
        # an ingest -- the file is still readable and the bytes are
        # still exportable, so the caller keeps them and files a row.
        return None
    if fp.tell() != len(raw):
        return None

    # Before anything iterates the parsed datasets. `read_sequence`
    # returns raw elements and `write_sequence` writes their bytes back;
    # converting first would compare a re-encoding of converted values,
    # which is a different question and a weaker one.
    out = DicomBytesIO()
    # THIS is the load-bearing pair, and `is_implicit_VR` is
    # load-bearing in the way that is easiest to miss. It is not
    # required -- omit it and pydicom falls back to the datasets'
    # `original_encoding`, which the ones `read_sequence` just produced
    # happen to carry. It matters because a WRONG value is not an
    # error: explicit VR is a valid encoding, `write_sequence` succeeds,
    # the bytes differ from `raw`, the equality gate below returns None
    # for every sequence, and #167 comes back reported to users as
    # "unparseable" with no exception raised anywhere. `is_little_endian`
    # is the flag that raises outright, because `write_tag` needs the
    # `_tag_packer` its setter builds.
    #
    # Both names are public setters that emit no deprecation warning
    # under pydicom 3.0.2 and both are watched by `REMOVED_IN_V4` in
    # `tests/test_pydicom_deprecations.py`, because a
    # deprecated-to-removed setter here fails the same silent way.
    out.is_little_endian = True
    out.is_implicit_VR = True
    try:
        write_sequence(out, DataElement(tag, "SQ", parsed), encoding)
    except Exception:      # pylint: disable=broad-except
        return None
    return parsed if out.getvalue() == raw else None


def populate_attrs(ds: Any, item: "DicomItem", dropped: list = None,
                   is_root: bool = True, unscanned: list = None,
                   nested: list = None, path: tuple = ()):
    """
    Standalone function to populate attributes for pickle-compatibility in workers.

    Extracts standard DICOM elements from a pydicom Dataset and populates the
    Isocenter DicomItem. Handles Sequences recursively. Skips large binary blobs
    to keep the object graph lightweight.

    Skipping is not the same as routing, and since #151 neither is the
    same as a VR. `PixelData` and `WaveformData` are extracted and
    written to the sidecar by `ingest_worker`, so skipping them here
    loses nothing. Every other bulk value -- private vendor blocks,
    Overlay Data, the palette LUTs, and the `UN` spelling all of them
    take under Implicit VR -- is decided by size against
    `BINARY_RETENTION_MAX_BYTES`: retained on the graph at or below it,
    dropped above it and collected in `dropped` so the caller can
    report the loss (#125, #137, #151). One rule for every wire VR,
    because keying on VR made the outcome depend on the source's
    transfer syntax.

    Group `7fe0` used to be taken out above that gate, by a `continue`
    whose comment read "Skip pixels" -- so nothing in the group could
    ever reach `dropped`. The group has six assigned members and the
    skip was only right for some of them. The (7fe0,0010) inside an Icon
    Image Sequence item vanished with no warning, no audit row and no
    line in a compliance report that says it lists everything missing
    from the export (#169). It is still skipped -- keeping it is #183's
    second half, still open -- but it is reported now.

    Which member is which takes two questions, and they are deliberately
    two names. `_is_routed` asks whether something else carries the
    bytes: the sidecar, for top-level (7fe0,0010) and, since #183's
    first half, for the float pair as well (#170, #193).
    `_DERIVED_PIXEL_INDEX_TAGS` holds the three that are not data at all
    -- the Extended Offset Table pair and the encapsulated stream's
    total length -- which describe a fragment layout the exported file
    does not have and so cannot be lost with it (#194).

    Took a third `text_index` argument until #84, which collected the
    text-VR elements it saw into `Instance.text_index`. Nothing read that
    index after the PHI scan became structural, and the VR filter it
    applied was never a scan boundary -- a configured PHI tag is one
    wherever it sits and whatever its VR.

    Args:
        ds: The pydicom Dataset or Sequence Item.
        item (DicomItem): The Isocenter item to populate.
        dropped (list, optional): Collects `(tag, vr)` for every
            unrouted element that is dropped -- a bulk value over
            `BINARY_RETENTION_MAX_BYTES` (#151), or an unrouted member
            of group 7fe0 -- so the caller can report them (#125,
            #137). See `_is_routed` and `_DERIVED_PIXEL_INDEX_TAGS` for
            the exclusions and why each one exists.
        is_root (bool): True when `ds` is the instance itself, False when
            it is a sequence item. Only the exemptions read this, and
            only (7fe0,0010) needs it: the same tag is routed to the
            sidecar at the top level and routed nowhere one level down
            (#169). Defaults True so the direct callers that hand this a
            bare sequence item -- the waveform and Murmur tests -- keep
            the behaviour they had; they pass no `dropped`, so the flag
            cannot reach anything for them anyway.
        unscanned (list, optional): Collects `(tag, byte_length)` for
            every private `UN` value that begins with the item tag and
            did not verify as a sequence, so the caller can report a
            value the PHI scan could not open (#167). Distinct from
            `dropped`: nothing was lost -- the bytes stay in
            `attributes` and are exported.
        nested (list, optional): Collects `(path, tag, vr, enclosing_ds)`
            for every nested (7fe0,0010) -- an Icon Image Sequence item's
            own Pixel Data and the like -- so `ingest_worker` can decode it
            into the sidecar (#183). **When it is not None, such an element
            is routed here INSTEAD of into `dropped`**, and the caller
            appends the ones it could not decode. That is the shape rather
            than appending to both and reconciling by count, because
            reconciling two lists works only while one icon's `(tag, vr)`
            entry is indistinguishable from another's -- correct today, and
            fragile in a way a reader cannot see.

            The alternative shape -- teaching `_is_routed` to return True
            for a nested (7fe0,0010) -- is the trap, and it is #194's at a
            third site: an icon that *fails* to decode would then be
            reported as routed and its loss row would vanish, which is the
            silent drop #169 closed. Routing and reporting come from one
            place, and that place is the code that knows whether the bytes
            were carried. `_is_routed` does not change.

            Left None -- the default, and what every direct caller in the
            tests passes -- behaviour is exactly what it was.
        path (tuple): The `iter_item_tree` route from the instance to
            `ds`: a tuple of `(sequence_tag, index)` steps, empty at the
            root. Only `nested` reads it; it is what becomes the blob
            kind's path segment, and it is the same shape
            `PhiFinding.entity_path` and `resolve_item_path` already use.
    """

    # The wire VRs whose values are bulk bytes. Since #151 membership
    # routes an element to the size gate below rather than deciding its
    # fate: an unrouted value at or below BINARY_RETENTION_MAX_BYTES is
    # retained whatever its VR, and one above it is dropped with a
    # DATA_LOSS row whatever its VR. `UN` is deliberately not a member
    # -- a `UN` value may be a disguised implicit-VR sequence (#167)
    # and must get the recovery attempt first; its blob fallback takes
    # the same size gate further down. (The old comment here read "UN
    # left out for safety, usually small private tags", and "usually
    # small" was the unmeasured assumption #151 is about: under
    # Implicit VR every private element is UN, megabyte blobs
    # included.)
    BINARY_VRS = {'OB', 'OW', 'OF', 'OD', 'OL'}

    # Read once, not per element: the float pair's exemption depends on
    # whether this instance also carries Pixel Data, and `in` on a
    # Dataset is a lookup rather than a scan.
    has_pixel_data = is_root and "PixelData" in ds

    # The enclosing dataset's character set, so text inside a re-parsed
    # sequence decodes the way it would have if pydicom had parsed the
    # sequence itself. The gate compares raw bytes, so this cannot
    # change whether a value parses -- only how its text reads.
    #
    # Keep the `getattr` default. The direct callers that hand this a
    # bare sequence item do pass `Dataset`s, which carry
    # `_character_set`, but the default is what makes the line true for
    # any `ds` this function is ever handed, and `ds._character_set`
    # would not be. Both shapes the attribute returns are valid
    # `encoding` arguments -- a bare `Dataset` gives `'iso8859'`, one
    # with a Specific Character Set gives `['latin_1']` -- so no
    # normalization is needed; do not add any.
    encoding = getattr(ds, "_character_set", default_encoding)

    for elem in ds:
        if elem.tag.group == 0x7fe0:
            # Still skipped -- the group check stays because it is not
            # only about binary VRs. (7fe0,0001) and (7fe0,0002), the
            # Extended Offset Table pair, are `OV` and would otherwise
            # start landing in `attributes` as a side effect of a change
            # about reporting. What moves is that the skip is now
            # recorded unless the bytes are carried elsewhere, or are an
            # index into bytes that are (#169, #194).
            if (nested is not None and not is_root
                    and elem.tag == _NESTED_PIXEL_DATA_TAG):
                # Carried, or reported by the caller -- never both, and
                # never neither (#183). The enclosing `ds` travels rather
                # than the element because pydicom cannot decode a sequence
                # item's pixel data without the file's Transfer Syntax UID:
                # `icon.pixel_array` raises `AttributeError: Unable to
                # decode the pixel data as the dataset's 'file_meta' has no
                # (0002,0010) 'Transfer Syntax UID'`. The decode borrows it
                # from the enclosing dataset, microseconds later, in this
                # same worker -- so nothing pydicom-shaped crosses a
                # process boundary.
                nested.append(
                    (path, f"{elem.tag.group:04x},{elem.tag.element:04x}",
                     elem.VR, ds))
                continue
            if (dropped is not None
                    and elem.tag not in _DERIVED_PIXEL_INDEX_TAGS
                    and not _is_routed(elem.tag, is_root, has_pixel_data)):
                dropped.append(
                    (f"{elem.tag.group:04x},{elem.tag.element:04x}", elem.VR))
            continue
        if elem.VR in BINARY_VRS:
            # Routed first: (5400,1010) is pulled out and written to the
            # sidecar by `ingest_worker` before this runs, so it is
            # neither lost nor retainable here -- holding it in
            # `attributes` as well would put the samples in two places
            # with two answers. Group 7fe0 never gets here at all; the
            # group check above takes it. Reporting either would put a
            # DATA_LOSS entry in the record of every image and every
            # waveform ever ingested, which is how a compliance trail
            # becomes noise -- #194 is what that looks like.
            if _is_routed(elem.tag, is_root, has_pixel_data):
                continue

            # Unrouted binary keys on SIZE, not on VR (#151): at or
            # below the threshold the value is retained on the graph --
            # so `remove_private_tags=False` can finally keep a small
            # explicit-VR vendor blob, and the outcome stops depending
            # on the transfer syntax, because the implicit-VR spelling
            # of the same element (`UN`, gated below) takes the same
            # rule. Above it, the existing DATA_LOSS treatment: Overlay
            # Data and the palette LUTs (`OW`, standard, routed
            # nowhere) and the megabyte private blocks all vanish
            # loudly, whatever their group (#125, #137).
            value = elem.value
            if value is None:
                # A zero-length element. Nothing to lose and nothing to
                # weigh; retained as empty bytes so it round-trips.
                value = b""
            if isinstance(value, (bytes, bytearray, memoryview)) \
                    and len(value) <= BINARY_RETENTION_MAX_BYTES:
                item.set_attr(
                    f"{elem.tag.group:04x},{elem.tag.element:04x}",
                    bytes(value))
                continue
            if dropped is not None:
                dropped.append(
                    (f"{elem.tag.group:04x},{elem.tag.element:04x}", elem.VR))
            continue  # Skip binary blobs over the retention threshold

        tag = f"{elem.tag.group:04x},{elem.tag.element:04x}"

        # A private sequence under Implicit VR arrives here as `UN`
        # bytes, because the transfer syntax carries no VR and the
        # standard dictionary has no entry (#167). Restore the
        # structure so the PHI scan can walk it -- and only when the
        # parse is proven byte-exact; see `_sequence_from_un_bytes`.
        #
        # The tag then lives in `sequences` and *not* in `attributes`,
        # which is what the Explicit VR ingest of the same file
        # produces. Keeping both would put the same tag through
        # `_merge` and `_merge_sequences`, whose order would decide
        # whether the export carried the remediated sequence or the
        # original bytes.
        #
        # Odd group only. A standard tag resolves its VR from the
        # dictionary with no dataset present, so an even-group element
        # does not reach `UN` by this route; an even-group `UN` means a
        # writer chose it explicitly, which is a different population.
        #
        # The `startswith` here is not a duplicate of the one inside
        # `_sequence_from_un_bytes`, and folding the two together
        # silently widens the report: this one separates "not a
        # candidate" -- every ordinary vendor blob, which falls through
        # exactly as before -- from "candidate that failed
        # verification", which is the only thing that earns a row.
        if (elem.VR == 'UN' and elem.tag.group % 2 == 1
                and isinstance(elem.value, (bytes, bytearray, memoryview))):
            raw = bytes(elem.value)
            if raw.startswith(_ITEM_TAG_LE):
                parsed = _sequence_from_un_bytes(raw, elem.tag, encoding)
                if parsed is not None:
                    process_sequence(tag, parsed, item, dropped, unscanned,
                                     nested=nested, path=path)
                    continue
                if (unscanned is not None
                        and len(raw) <= BINARY_RETENTION_MAX_BYTES):
                    # Only when the bytes will actually be retained: the
                    # SCAN_GAP row says "ingested verbatim; the PHI scan
                    # could not open it", and a candidate the size gate
                    # below is about to drop gets a DATA_LOSS row
                    # instead -- one row per element, each telling the
                    # truth (#151).
                    unscanned.append((tag, len(raw)))
                # Falls through: the bytes stay in `attributes` and are
                # exported exactly as before.

        # The `UN` half of the size rule (#151). A proven sequence was
        # taken structurally above and is exempt -- structure is
        # resolved, not weighed, and a sequence's encoded length is the
        # one place the two transfer syntaxes genuinely differ. What
        # reaches here as `UN` bytes is a blob, and it takes exactly the
        # gate the `BINARY_VRS` arm applies: retained at or below
        # `BINARY_RETENTION_MAX_BYTES`, dropped with a DATA_LOSS row
        # above it. Before this, `UN` was retained unconditionally
        # ("usually small"), so the implicit-VR spelling of a megabyte
        # private blob sat resident in the graph while its explicit-VR
        # twin was dropped and reported.
        if (elem.VR == 'UN'
                and isinstance(elem.value, (bytes, bytearray, memoryview))
                and len(elem.value) > BINARY_RETENTION_MAX_BYTES):
            if dropped is not None:
                dropped.append((tag, 'UN'))
            continue

        if elem.VR == 'SQ':
            process_sequence(tag, elem, item, dropped, unscanned,
                             nested=nested, path=path)
        elif elem.VR == 'PN':
            # Sanitize PersonName for pickle safety
            item.set_attr(tag, str(elem.value))
            _record_private_vr(item, tag, elem)
        else:
            item.set_attr(tag, _process_safe(elem.value))
            _record_private_vr(item, tag, elem)


def _process_safe(value):
    """`value`, or its items as a `list` when it cannot be pickled (#651).

    **The trap.** pydicom 3.0.2 holds a LUT Descriptor -- Red, Green or
    Blue Palette Color (0028,1101-1103) or LUT Descriptor (0028,3002) --
    as a `MultiValue` whose item constructor is `_skip_conversion`, a
    closure local to `DataElement._convert_value`, and it does so only
    when the VR had to be looked up: under Implicit VR, which is the
    syntax `export(use_compression=False)` writes. Stored as it was, that
    value made the `Instance` unpicklable, and `ingest_worker`'s result is
    pickled by the pool *outside* the worker's `try` -- so one such file
    raised out of `ingest()` and lost the whole pass. Our own
    uncompressed export of a palette file could not be ingested again.

    **Why only what cannot be pickled, and not every `MultiValue`.** A
    fresh ingest keeps the `MultiValue` pydicom handed back, and
    `tests/test_private_tag_reload.py` pins that in-memory shape;
    converting them all turns that test red. A `list` is what the store
    hands back on reload, so the values this does convert read the same
    fresh and reloaded.

    **Why picklability, and not the constructor's name or pydicom's
    `_LUT_DESCRIPTOR_TAGS`.** Both are pydicom internals. Picklability is
    the property the worker boundary needs, and it catches the next local
    constructor pydicom adds, not only this one. It costs a pickle of
    each `MultiValue`, a subset of the whole-result pickle `ingest_worker`
    already measures at about 0.3% of worker time.
    """
    if not isinstance(value, MultiValue):
        return value
    try:
        pickle.dumps(value)
    except Exception:  # pylint: disable=broad-exception-caught
        return list(value)
    return value


def _record_private_vr(item, tag: str, elem) -> None:
    """Keep the VR of a private element beside its value (#154).

    Four conditions, and each of them is what keeps some other
    behaviour unchanged:

    * **Odd group only.** An even-group tag resolves its VR from the
      standard dictionary at export time, so recording one here would be
      a second answer that can drift from the dictionary's.
    * **Never `UN`.** `UN` is the absence of an answer, not an answer.
      It is also what *every* private element resolves to under Implicit
      VR Little Endian, so this condition is what makes an implicit-VR
      ingest record nothing at all and keeps
      `tests/test_private_sequence_implicit_vr.py` true by construction
      rather than by luck.
    * **Never a bytes value.** PS3.5 §6.2.2 makes `UN` the right VR for
      raw bytes and `_fallback_encoding` already writes that. There is
      also nowhere to keep it: `_split_core_and_private` routes an
      odd-group `bytes` value to `attributes_json`, not to the
      `instance_attributes` table whose `value_rep` column is this
      carrier's home on the storage side.
    * **The private creator included.** (gggg,0010) is `LO`, which is
      what the fallback already guesses, so recording it changes
      nothing -- and leaving it out would make the one tag every private
      block depends on the one tag with no recorded VR.
    """
    if elem.tag.group % 2 != 1:
        return
    if elem.VR == 'UN':
        return
    if isinstance(elem.value, (bytes, bytearray, memoryview)):
        return
    item.record_attr_vr(tag, str(elem.VR))


def process_sequence(tag, elem, parent_item, dropped: list = None,
                     unscanned: list = None, nested: list = None,
                     path: tuple = ()):
    """Recursively parses Sequence (SQ) items.

    Everything below the instance is `is_root=False`, at every depth: an
    element inside a sequence item is inside a sequence item whether it
    is one level down or four. Only the top-level element of a
    depth-sensitive tag is routed (#169).

    `dropped` and `unscanned` are both forwarded, including for a
    sequence recovered from `UN` bytes: its items go through the
    ordinary rules, so a binary-VR child inside one is reported like any
    other, and an unverifiable candidate one level further down still
    earns its row (#167).

    `nested` and `path` are forwarded the same way, and this loop is the one
    place the path grows: each item extends it by its own `(tag, index)`
    step, exactly as `iter_item_tree` does. **`index` is a position, and a
    position is the only identity a sequence item has** -- a blob keyed on
    one is only as good as the graph holding still between ingest and
    export, which is why the export re-checks geometry before it writes
    (see `_write_back_nested_pixels`).

    A sequence recovered from `UN` bytes gets a path too. It is a real
    sequence in the graph by the time anything resolves the path against
    it, so leaving it out would carry the bytes and then fail to find their
    home (#167).

    **A zero-item sequence is carried, and that is what the
    `add_sequence` call below is for.** This loop used to be the only way
    a sequence reached the graph, so a source element saying "present, no
    items" made zero calls and was gone before anything could report it --
    absent from the export with `losses == []` and an `EXPORT` row reading
    `wrote 1 of 1 planned instances` (#392). The call is unconditional
    rather than guarded by `if not len(elem)`: one statement covers both
    cases and cannot go stale. `_merge_sequences` already writes a
    zero-item `SQ` element for an empty one, so nothing is lost and no
    `DATA_LOSS` row is filed.
    """
    parent_item.add_sequence(tag)
    for index, ds_item in enumerate(elem):
        seq_item = DicomItem()
        populate_attrs(ds_item, seq_item, dropped, is_root=False,
                       unscanned=unscanned, nested=nested,
                       path=path + ((tag, index),))
        parent_item.add_sequence_item(tag, seq_item)


def _decode_pixels(ds, *, allow_excess_frames=None,
                   as_rgb=None) -> Tuple[np.ndarray, str]:
    """The array `Dataset.pixel_array` returns, and the colour space it is in.

    `pixel_array` calls exactly this -- `as_array` on `get_decoder(ts)`,
    the `pydicom.pixels` backend, which is the one `Dataset` uses
    unless `use_pdh` is set and nothing in this package sets it -- and
    then discards the meta (pydicom 3.0.2 `pixels/utils.py:1430`, the
    `Dataset` branch: `[0]` of the `as_array` pair; the path/file branch
    at `:1465` spells it `arr, _ =`). That meta is the one place pydicom states
    what colour space the returned array is in, and it is not the
    declared `PhotometricInterpretation`: with the default `as_rgb=True`
    every 8-bit YBR family comes back RGB, under any transfer syntax,
    and the dataset's own label is left as it was (#372). A caller that
    stores the array has to store this answer with it.

    The transfer syntax is read as an attribute, deliberately, rather
    than with a `dict.get()` default. A dataset read with `force=True` and
    no file meta (#281's population) has an empty `file_meta`;
    `pixel_array` raises `AttributeError` for it and so does this, into
    the same `except` and the same `Decompression Failed` row. A default
    would turn that into a decode under Explicit VR LE -- a file
    ingested with garbage geometry and no row at all.

    By default, every frame `pixel_array` returns (`index=None`); a
    helper that decoded frame 0 would carry a multi-frame source as one
    frame. That includes frames an encapsulated offset table names beyond
    NumberOfFrames, which is pydicom's default. `allow_excess_frames=False`
    keeps only the declared frames. Two callers pass it, each only when
    `offset_table_frame_count` has reported an excess, and each hands the
    excess back to `import_files` for its DATA_LOSS row: `ingest_worker`
    for the top level (#418) and `_decode_nested_pixels` for a nested
    item such as an icon (#433).

    The keyword is forwarded only when it was given. Measured on pydicom
    3.0.2: `as_array(ds, allow_excess_frames=None)` truncates exactly as
    `False` does, so passing the default through would silently truncate
    every decode, nested icons included.

    **When pydicom cannot decode, `imagecodecs` may (#416).** pydicom is
    asked first, always, so a file that decoded before decodes to the same
    bytes and the same colour-space label; `imagecodecs` first would
    change both for files already accepted. Only a `RuntimeError` falls
    through -- pydicom's "all plugins are missing dependencies" and "raised
    by all available plugins" -- and only for
    `_IMAGECODECS_FALLBACK_SYNTAXES`. Its validation failures are
    `AttributeError` and `ValueError` (pydicom 3.0.2 `_validate_options`),
    and they stay refusals in its words: imagecodecs ignores
    PlanarConfiguration, so catching those would ingest a file missing a
    Type 1 element. See `_decode_with_imagecodecs` for what it then
    refuses. Both depths get it, because both call this: one rule for
    both depths.

    **And the fallback validates first, as pydicom would have (#453).**
    pydicom validates the header only once it has found a plugin:
    `as_array` checks its plugins, raises "missing dependencies" when it
    has none, and only then validates. So for JPEG Lossless and JPEG-LS,
    which have no plugin here, "validation stays a refusal" held for
    nobody: a file missing BitsStored, PixelRepresentation or
    PlanarConfiguration, or declaring BitsStored 17 under BitsAllocated
    16, reached the fallback and was ingested, where a JPEG 2000 file with
    the same header was refused. `_validate_like_pydicom` runs the same
    check, in the same words, before the fallback is asked. On the one
    route where pydicom had already validated -- a plugin existed and
    raised, as Pillow does for 16-bit colour JPEG 2000 -- the check runs
    twice and passes twice, and pydicom's "number of bytes of compressed
    pixel data matches the expected number for uncompressed data" warning,
    which `validate()` also raises, can be emitted twice for one file.
    That is a warning, not an answer, and passing `validate=False` to
    `as_array` instead would change what pydicom decodes: its
    `_validate_options` deletes a mismatched Extended Offset Table from
    the runner it decodes with, and a separate runner cannot do that.

    **Every door calls this (#453).** `Instance.get_pixel_data()` from a
    file does too, so a file ingest refuses is refused there in the same
    words, and a file ingest reads is read there to the same array under
    the same label. It used to call pydicom and then hand any failure at
    all to `imagecodecs_handler.get_pixel_data`, which had none of the
    fallback's checks.

    **`as_rgb=False` asks for the stored samples, and only the export
    readback passes it (#449).** Forwarded only when given, like
    `allow_excess_frames`, so every ingest decode still gets pydicom's
    default and #372's relabel still sees what it was measured against.
    The readback compares the decode with the samples it wrote, and with
    the default an 8-bit YBR file comes back RGB and fails although it
    is correct (measured under both syntaxes the exporter writes). The
    imagecodecs fallback does not take the keyword, and for the readback
    it need not: the exporter writes only native syntaxes and JPEG 2000
    (`_finalize_dataset`), native syntaxes never reach the fallback
    (`_IMAGECODECS_FALLBACK_SYNTAXES`), and under JPEG 2000 the fallback
    returns the samples the encoder was given. Its one conversion, 8-bit
    `YBR_FULL` under JPEG-LS (`_FALLBACK_JPEGLS`), is not a file the
    exporter can write. **A new export syntax that the fallback converts
    would need the keyword threaded through**, or the readback would
    fail its correct files.

    **A signed JPEG 2000 codestream under PixelRepresentation 0 is
    refused first, before pydicom is asked (#524).** Pillow decodes one
    at monochrome depths and 8-bit colour and returns the samples shifted
    by 2^(bits-1), with no error, so ingest stored the shift where the
    imagecodecs handler refused. The codestream's SIZ is read instead,
    for every declared frame: `imagecodecs_handler.signed_codestream_refusal`.
    Isocenter's own exports never carry that shape -- the writer derives
    PixelRepresentation and the codestream's sign from one dtype (#499) --
    so the export readback, which decodes here too, never trips it.

    `ts` is read *outside* the `try`, so the #281 `AttributeError` above
    can never reach the fallback.
    """
    ts = ds.file_meta.TransferSyntaxUID
    refusal = signed_codestream_refusal(ds)
    if refusal is not None:
        raise RuntimeError(refusal)
    kwargs = {}
    if allow_excess_frames is not None:
        kwargs["allow_excess_frames"] = allow_excess_frames
    if as_rgb is not None:
        kwargs["as_rgb"] = as_rgb
    # A T.81 stream wider than BitsStored is read by its own precision on
    # pydicom's route too (#622). pydicom masks every T.81 decode to
    # BitsStored (`correct_unused_bits`, its default for these syntaxes),
    # where the fallback, JPEG 2000 and JPEG-LS read the stream's width:
    # an 8-bit JPEG Baseline stream under BitsStored 6 read 60 for 252.
    # The mask is also pydicom's *sign extension* for them (a left shift,
    # then an arithmetic right shift), so with it off a plugin such as
    # pylibjpeg-libjpeg returns a signed stream's unextended pattern --
    # measured, 2048 for -2048 -- and `_extend_each_frame` puts it back.
    # At a width equal to the container's, as for Pillow's `int8`, that
    # is a pure reinterpretation.
    #
    # **Every frame's precision, never frame 0's for all** (review of
    # #659, M1). The mask is one switch for the whole decode, so it is
    # off when any frame is wider; the extension is per frame, so a
    # conformant frame behind a wider frame 0 is extended at BitsStored,
    # as the fallback extends it. Extended at frame 0's width, its -2048
    # read 2048 with pylibjpeg installed, where main had read it right.
    wider = _t81_frames_wider(ds, ts)
    if wider is not None:
        kwargs["correct_unused_bits"] = False
    try:
        arr, meta = get_decoder(ts).as_array(ds, **kwargs)
        photometric = meta["photometric_interpretation"]
    except RuntimeError as exc:
        if str(ts) not in _IMAGECODECS_FALLBACK_SYNTAXES:
            raise
        # Before the fallback and outside it, so the refusal is pydicom's
        # own `AttributeError` or `ValueError`, not wrapped as a reason
        # imagecodecs could not decode, and ingest's row reads
        # `Decompression Failed: AttributeError: Missing required element:
        # ...` as a JPEG 2000 file's already did. Before the photometric
        # allow-list too: a header that fails both is refused in pydicom's
        # words, which name the element.
        _validate_like_pydicom(ds, ts)
        arr, photometric = _decode_with_imagecodecs(ds, allow_excess_frames,
                                                    exc)
    else:
        # pydicom's decode only, outside the `try`: the fallback extends
        # its own (`_decode_frame`), and a refusal here is not a reason to
        # ask it.
        if wider is not None:
            arr = _extend_each_frame(arr, ds, wider)
    # Native byte order, at the one exit every door leaves by (#648).
    # pydicom returns a big-endian source in the file's own order (`>u2`,
    # `>i2`, `>u4`) with the right *values*, and every caller that stores
    # the array stores `tobytes()` -- big-endian bytes, which the sidecar
    # loader reads as native: `[0, 100, 4000, 4095]` came back
    # `[0, 25600, 40975, 65295]`. `Instance.set_pixel_data` has always
    # normalised a caller's array the same way. The export readback cannot
    # see this class of defect, because it compares the written file with
    # the array written, not with the source; a test has to.
    if arr.dtype.byteorder not in ('=', '|'):
        arr = arr.astype(arr.dtype.newbyteorder('='))
    return np.ascontiguousarray(arr), photometric


def _t81_frames_wider(ds, ts) -> Optional[list]:
    """Every declared frame's `(stream, precision)`, when any is wider than BitsStored (#622).

    None for any syntax but T.81 (`.50/.51/.57/.70`), for a BitsStored
    that is absent or not an integer (pydicom's validation refuses it in
    its own words), and when no frame's stream is wider -- the conformant
    case, which pydicom masks and extends exactly as it did.
    """
    if str(ts) not in T81_SYNTAXES:
        return None
    try:
        bits_stored = int(ds.BitsStored)
    except (AttributeError, TypeError, ValueError):
        return None
    precisions = frame_precisions(ds)
    if any(precision is not None and precision > bits_stored
           for _stream, precision in precisions):
        return precisions
    return None


def _extend_each_frame(arr, ds, precisions):
    """pydicom's unmasked T.81 decode, each frame sign-extended as the fallback extends it (#622).

    Frame `i` is extended from its own stream's precision where that is
    wider than BitsStored, and from BitsStored otherwise -- exactly
    `imagecodecs_handler._decode_frame`'s JPEG Lossless arm, so both
    routes give one answer frame by frame. A frame past `precisions` (an
    excess the caller did not ask to drop) is extended from BitsStored.
    Unsigned, `_sign_extend` returns each frame untouched.

    The frame axis is read from the array's rank against SamplesPerPixel,
    not from NumberOfFrames: pydicom returns one frame without it.
    """
    bits_stored = int(ds.BitsStored)
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    single = arr.ndim == (2 if samples == 1 else 3)
    frames = arr[np.newaxis] if single else arr
    extended = []
    for index, frame in enumerate(frames):
        precision = (precisions[index][1] if index < len(precisions)
                     else None)
        extended.append(_sign_extend(
            frame, ds, precision if precision is not None
            and precision > bits_stored else None))
    return extended[0] if single else np.stack(extended)


def _validate_like_pydicom(ds, ts) -> None:
    """pydicom's header validation, where pydicom never got to run it (#453).

    `DecodeRunner.validate()` is the call `Decoder.as_array` makes after
    it has found a plugin (pydicom 3.0.0 through 3.0.2; the only
    difference between them is a `ceil` in the length heuristic). Raises
    exactly what `as_array` would: `AttributeError("Missing required
    element: (0028,0101) 'Bits Stored'")`, `ValueError("A (0028,0101)
    'Bits Stored' value of '17' is invalid ...")`, `ValueError("Unknown
    (0028,0004) 'Photometric Interpretation' value 'NONSENSE'")`.

    `ts` is passed rather than read again: `_decode_nested_pixels` hands
    in a sequence item whose `file_meta` it borrowed from the enclosing
    dataset, and `set_source` reads only the item's own group 0028
    (measured on an icon missing BitsStored under JPEG Lossless).
    """
    runner = DecodeRunner(ts)
    runner.set_source(ds)
    runner.validate()


def _decode_with_imagecodecs(ds, allow_excess_frames,
                             pydicom_error) -> Tuple[np.ndarray, str]:
    """`_decode_pixels`' fallback: decode, then refuse what does not fit.

    A generic fallback would store whatever the codec returned, and a
    codec's output can disagree with the header. The signed JPEG Lossless
    and JPEG-LS case #416 measured -- -800 read as 3296 -- is now decoded
    correctly by the handler, which sign-extends from BitsStored (#446),
    and a JPEG 2000 codestream whose signedness contradicts
    PixelRepresentation is refused before any decoder (#524); the dtype
    check below stays for any decode that still disagrees. So the
    decode is accepted only when it passes every check below against the
    header, and refused -- keeping pydicom's reason first, so the ingest
    row still reads `Decompression Failed: <pydicom's words>` -- when:

    - **the colour space is not one it labels under this syntax.**
      `imagecodecs` does not say what colour space it returned, or how it
      laid the samples out, so the stored label is what a decode under
      that syntax has been measured to return for the declared one; see
      `_FALLBACK_PHOTOMETRICS`. A declaration with no entry is refused.
      This is also what keeps out the one mismatch the checks after the
      decode cannot see: a planar/interleaved swap has the right dtype,
      size and shape.
    - **the offset table disagrees in a way the caller has not handled.**
      Fewer frames than declared is always refused. An excess is decoded
      to the declared frames only when the caller passed
      `allow_excess_frames=False` -- the two callers that do write the
      row (#418, #433). Otherwise it is refused: pydicom's default is to
      return every frame, and an array holding more frames than its
      header declares is one nothing can read back.
    - **the output does not match the header**: its itemsize against
      BitsAllocated, its signedness against PixelRepresentation, its
      size against Rows x Columns x SamplesPerPixel x frames, and, when
      the size agrees, its shape. A same-size decode in another shape
      (8x4 under a 4x8 header, three samples under a one-sample header)
      is a different image, and a reshape would store it without
      complaint.

    Returns:
        ``(array, photometric)`` in the shape `pixel_array` returns --
        ``(rows, cols[, samples])`` for one frame, ``(frames, ...)`` for
        more -- and the Photometric Interpretation the array is in, from
        `_FALLBACK_PHOTOMETRICS`: the declared one, or `RGB` where the
        decode converted it (#448). Both callers relabel from it, as
        they do from pydicom's meta (#372).
    """
    def refused(why):
        return RuntimeError(
            f"{pydicom_error}; imagecodecs could not decode it either: {why}")

    ts = ds.file_meta.TransferSyntaxUID
    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "")
    labels = _FALLBACK_PHOTOMETRICS.get(str(ts), {})
    if photometric not in labels:
        name = getattr(ts, "name", "")
        syntax = f"{name} ({ts})" if name and name != str(ts) else str(ts)
        raise refused(
            f"its declared colour space {photometric!r} is not one this "
            f"fallback labels under {syntax}: imagecodecs does not say "
            f"what colour space or sample layout it decoded to, so the "
            f"declared label is repeated only where a decode under that "
            f"syntax has been measured to match it") from pydicom_error
    stored_label = labels[photometric]
    convert = (stored_label != photometric
               and str(ts) not in _FALLBACK_DECODER_CONVERTS)
    bits = int(ds.BitsAllocated)
    if convert and bits != 8:
        # Every door's refusal, since #453 sent the Instance door through
        # this function too: until then it returned a 16-bit YBR_FULL
        # frame as stored, under its own label. #461 ruled (Q5) to record
        # it as a limit rather than convert ahead of pydicom, which refuses
        # the native form as well ("Invalid ndarray.dtype 'uint16' for
        # color space conversion"); `docs/installation.md` says so.
        raise refused(
            f"its declared colour space {photometric!r} is {bits}-bit, and "
            f"the conversion to {stored_label} this fallback would make, "
            f"pydicom's `convert_color_space`, takes 8-bit samples only") \
            from pydicom_error
    # The conversion itself, and the refusal of a signed 8-bit frame, are
    # the handler's (#464), so ingest and both read doors make them by one
    # rule. When this module converted for itself, ingest stored RGB and
    # the read doors returned the YBR samples. `convert` above and the
    # handler's `CONVERTS_TO` must name the same rows;
    # `test_the_handler_converts_exactly_the_relabels_ingest_leaves_to_it`
    # holds them together.
    try:
        conversion = colour_conversion(ds)
    except RuntimeError as exc:
        raise refused(str(exc)) from pydicom_error

    counted = offset_table_frame_count(ds)
    if counted is not None and counted[0] != counted[1]:
        if counted[0] < counted[1]:
            raise refused(frame_count_mismatch_words(counted)) \
                from pydicom_error
        if allow_excess_frames is not False:
            raise refused(
                f"{frame_count_mismatch_words(counted)}, and this decode "
                f"was not asked to drop the excess") from pydicom_error
    frames = (counted[1] if counted is not None
              else max(1, int(getattr(ds, "NumberOfFrames", 1) or 1)))

    try:
        arr = decode_declared_frames(ds, frames)
    except Exception as exc:  # pylint: disable=broad-except
        raise refused(describe_exception(exc)) from exc

    representation = int(ds.PixelRepresentation)
    kind = "i" if representation == 1 else "u"
    if arr.dtype.itemsize * 8 != bits or arr.dtype.kind != kind:
        raise refused(
            f"it decoded to {arr.dtype}, where BitsAllocated {bits} and "
            f"PixelRepresentation {representation} declare "
            f"{np.dtype(f'{kind}{max(1, bits // 8)}')}") from pydicom_error

    rows, cols = int(ds.Rows), int(ds.Columns)
    samples = int(ds.SamplesPerPixel)
    shape = (rows, cols) + ((samples,) if samples > 1 else ())
    if frames > 1:
        shape = (frames,) + shape
    # Before the size check, and only when the sizes agree: a same-size
    # decode in another shape reshapes without complaint, so without
    # this it is stored as a different image (8x4 under a 4x8 header, or
    # a 4x4 RGB stream under a 4x12 MONOCHROME2 one). The size check
    # below keeps the other case, where the counts differ, in its words.
    if arr.shape != shape and arr.size == int(np.prod(shape)):
        raise refused(
            f"it decoded to shape {arr.shape}, where the header declares "
            f"{shape}") from pydicom_error
    if arr.size != int(np.prod(shape)):
        raise refused(
            f"it decoded {arr.size} samples, where {frames} frame(s) of "
            f"{rows}x{cols}x{samples} need {int(np.prod(shape))}") \
            from pydicom_error
    arr = arr.reshape(shape)
    if conversion is not None:
        # After every check, on the header's shape: the conversion reads
        # the last axis as the three samples.
        arr = convert_colour(arr, conversion)
    return np.ascontiguousarray(arr), stored_label


def _high_bit_mismatch(ds) -> Optional[dict]:
    """The facts for ingest's HighBit row, or None when HighBit is BitsStored - 1.

    PS3.5 8.1.1 requires HighBit to be BitsStored - 1. No decoder here
    reads HighBit: JPEG Lossless returns right-aligned samples by
    BitsStored (or by its stream's precision where that is wider, #622),
    JPEG-LS and JPEG 2000 by the stream's own precision, and
    pydicom masks a native sample to its low BitsStored bits. So a file
    that says otherwise is read exactly as a conformant one would be, and
    the one thing that changes is that the session says so (#455, #523;
    owner rulings Q2 and Q3): `import_files` writes a `WARNING` row from
    what this returns.

    **A header rule, asked before the decode and of no decoder.** It
    reads the Image Pixel module and, for a stream that carries its own
    precision, frame 0's first bytes. So a file decoded by Pillow and one
    decoded by imagecodecs get one answer -- which is what the refusal
    this replaces could not give: it lived in `_sign_extend`, fired only
    on the imagecodecs route, and only for a signed frame.

    None, too, when either element is absent or not an integer: a header
    that cannot be compared makes no claim to compare. The caller attaches
    the facts only once the decode has succeeded; a refused file has its
    own `ERROR` row.

    An icon item is asked the same, by `_decode_nested_pixels`, with the
    file's `file_meta` borrowed (#598).

    Returns:
        ``{bits_allocated, bits_stored, high_bit, pixel_representation,
        encapsulated, stream, precision, width_read}``: `stream` is
        `_stream_precision`'s name where `precision` was read from one
        (for a T.81 stream, only when it is wider than BitsStored, #622),
        else None; `width_read` is that precision, or BitsStored.
    """
    try:
        bits_stored = int(ds.BitsStored)
        high_bit = int(ds.HighBit)
    except (AttributeError, TypeError, ValueError):
        return None
    if high_bit == bits_stored - 1:
        return None
    ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    try:
        encapsulated = bool(ts is not None and ts.is_encapsulated)
    except (AttributeError, ValueError):
        encapsulated = False
    stream = precision = None
    if encapsulated and (ts in J2K_SYNTAXES or ts in JPEGLS_SYNTAXES
                         or ts in T81_SYNTAXES):
        stream, precision = _stream_precision(ts, _first_frame(ds))
        # A T.81 stream is read by its precision only where that is wider
        # than BitsStored (#622); otherwise by BitsStored, as #446 reads
        # it, and the row says BitsStored.
        if (ts in T81_SYNTAXES and precision is not None
                and precision <= bits_stored):
            stream = precision = None
    return {
        "bits_allocated": getattr(ds, "BitsAllocated", None),
        "bits_stored": bits_stored,
        "high_bit": high_bit,
        "pixel_representation": getattr(ds, "PixelRepresentation", None),
        "encapsulated": encapsulated,
        "stream": stream,
        "precision": precision,
        "width_read": precision if precision is not None else bits_stored,
    }


def _first_frame(ds) -> bytes:
    """Frame 0 of an encapsulated `ds`, found through its offset tables.

    `b""` on any failure: a buffer the decode will refuse on its own terms,
    and a header rule asked before the decode has no business raising
    first. One read for the two rules that look inside a stream before
    decoding it -- `_high_bit_mismatch`'s precision and
    `_lossy_compression_evidence`'s frame header, NEAR and wavelet (#601)
    -- so frame 0 is the same bytes for both. Frame 0 speaks for the
    instance.
    """
    try:
        return next(generate_frames(
            ds.PixelData, number_of_frames=1,
            extended_offsets=extended_offsets(ds)))
    except Exception:  # pylint: disable=broad-except
        return b""


def _precision_mismatch(ds, arr) -> Optional[dict]:
    """The facts for ingest's precision row, or None (#622).

    A compressed stream states its own sample precision, and PS3.5 8.2.1
    directs that where the stream's characteristics contradict the Data
    Elements, the stream's control the decompression. Every door here
    reads such a stream by its precision -- JPEG 2000 and JPEG-LS always
    did; a T.81 stream does since #622, on both routes (`_decode_pixels`,
    `imagecodecs_handler._decode_frame`) -- and `import_files` writes a
    `WARNING` row from what this returns, as it does for HighBit (#455).

    **Only where the decoded samples do not fit BitsStored** (owner
    ruling on review F2 of #659). A stream wider than BitsStored is the
    ordinary output of DCMTK's true-lossless encoder, which writes the
    precision BitsAllocated for every 12-in-16 image; four of pydicom's
    fifty compressed test files have that shape, and every sample fits.
    The header disagreement alone changes no value and no export, so it
    is not a row. `arr` is the decode ingest stores: a sample of 2^BS or
    more (unsigned), or outside `[-2^(BS-1), 2^(BS-1) - 1]` (signed), is
    one BitsStored cannot hold, and then the stream's precision is why.

    None unless some sample does not fit and the widest frame's precision
    satisfies `BitsStored < precision <= BitsAllocated`. Above
    BitsAllocated no container holds the samples and the decode's dtype
    check refuses the file; at or below BitsStored a sample that does not
    fit was not put there by the stream. **Every declared frame's
    precision** (`frame_precisions`), so a wider frame behind a
    conformant frame 0 is reported; the row names the widest.

    No rewrite of BitsStored follows (the #455 precedent): the graph keeps
    the declared value, and an export writes BitsStored from the samples,
    which is the container's width for a 12-bit stream under BitsStored 8
    (`_stored_width` falls to the array's width when the declared value
    does not hold the samples).

    Returns:
        ``{bits_allocated, bits_stored, precision, stream,
        pixel_representation, sample}``, plain types, so it rides `meta`
        out of a spawned worker; `sample` is the one farthest outside
        BitsStored's range.
    """
    ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    if ts is None or not (ts in J2K_SYNTAXES or ts in JPEGLS_SYNTAXES
                          or ts in T81_SYNTAXES):
        return None
    try:
        bits_allocated = int(ds.BitsAllocated)
        bits_stored = int(ds.BitsStored)
    except (AttributeError, TypeError, ValueError):
        return None
    signed = int(getattr(ds, "PixelRepresentation", 0) or 0) == 1
    sample = _sample_beyond(arr, bits_stored, signed)
    if sample is None:
        return None
    readings = [(stream, precision) for stream, precision
                in frame_precisions(ds) if precision is not None]
    if not readings:
        return None
    stream, precision = max(readings, key=lambda reading: reading[1])
    if not bits_stored < precision <= bits_allocated:
        return None
    return {
        "bits_allocated": bits_allocated,
        "bits_stored": bits_stored,
        "precision": precision,
        "stream": stream,
        "pixel_representation": getattr(ds, "PixelRepresentation", None),
        "sample": sample,
    }


def _sample_beyond(arr, bits_stored, signed) -> Optional[int]:
    """The sample farthest outside what BitsStored holds, or None when all fit (#622).

    Unsigned, BitsStored holds `0 .. 2^BS - 1`; signed, `-2^(BS-1) ..
    2^(BS-1) - 1`. Asked of the array's minimum and maximum only, as
    Python integers, so a 16-bit container cannot overflow the bound.
    """
    if arr is None or arr.size == 0 or bits_stored < 1:
        return None
    if signed:
        low, high = -(1 << (bits_stored - 1)), (1 << (bits_stored - 1)) - 1
    else:
        low, high = 0, (1 << bits_stored) - 1
    lowest, highest = int(arr.min()), int(arr.max())
    beyond = [(highest - high, highest)] if highest > high else []
    if lowest < low:
        beyond.append((low - lowest, lowest))
    return max(beyond)[1] if beyond else None


def _precision_words(facts) -> str:
    """The precision row, from `_precision_mismatch`'s facts."""
    precision = facts["precision"]
    return (f"BitsStored {facts['bits_stored']} with BitsAllocated "
            f"{facts['bits_allocated']}, and the {facts['stream']}'s "
            f"precision is {precision}: read as right-aligned {precision}-bit "
            f"samples, as PS3.5 8.2.1 directs for a stream that contradicts "
            f"the Data Elements, and a sample reads {facts['sample']}, which "
            f"BitsStored {facts['bits_stored']} cannot hold; an export writes "
            f"BitsStored from the samples.")


#: The two transfer syntaxes whose frames are read for a DCT frame header
#: (#601). The syntax names the frame's process and does not prove it: a
#: lossless SOF3 frame under either decodes bit-exact (review J2 M1).
_DCT_SYNTAXES = frozenset({"1.2.840.10008.1.2.4.50",
                           "1.2.840.10008.1.2.4.51"})

#: SOFn for the DCT processes, each lossy by definition (ITU-T T.81 Table
#: B.1: baseline, extended and progressive, Huffman or arithmetic, and
#: their differential forms). 3, 7, 11 and 15 are the lossless processes.
_DCT_FRAME_TYPES = frozenset({0, 1, 2, 5, 6, 9, 10, 13, 14})


def _lossy_compression_evidence(ds) -> Optional[dict]:
    """The facts for ingest's LossyImageCompression row, or None (#601).

    PS3.3 C.7.6.1.1.5: (0028,2110) `01` "conveys that the Image has
    undergone lossy compression", and "once this value has been set to 01
    it shall not be reset". It is Type 3, and nothing requires it under a
    lossy transfer syntax -- but when a source omits it, the transfer
    syntax is the only record of the loss, and an export replaces the
    syntax. So ingest records `01` where **the pixel data proves it**:

    - a JPEG frame header of a DCT process (`_jpeg_frame_type`, SOF0, 1,
      2, 5, 6, 9, 10, 13 or 14), under JPEG Baseline `.50` or Extended
      `.51`;
    - a JPEG-LS scan whose NEAR is above 0 (`_jpegls_near`), under any
      JPEG-LS syntax -- a `.80` stream with NEAR 2 is lossy too;
    - a JPEG 2000 codestream using the 9-7 irreversible wavelet
      (`_j2k_irreversible`), under any JPEG 2000 or HTJ2K syntax.

    **A syntax alone is not evidence**: `.81` at NEAR 0, `.91`/`.203`
    with the reversible wavelet, and a SOF3 frame under `.50`/`.51` are
    lossless in fact (built and measured bit-exact), and a false `01` can
    never be withdrawn. The JPEG arm read the syntax alone until review J2
    (M1), and stamped that SOF3 frame. A stream with no readable frame
    header, NEAR or COD claims nothing. What that leaves
    unrecorded, stated: a reversible codestream truncated at a lossy rate,
    which its header cannot show.

    None when the source already declares `01`: that is the record, and
    this never touches it. A declared `00`, or any other value, is replaced
    -- which is not a reset of `01`.

    Returns:
        ``{declared, syntax, evidence, value}``: `declared` is the value
        the source carried (None when absent; a list for a multi-valued
        one), `evidence` is "dct", "near" or "irreversible", and `value`
        the frame header's SOF number or the NEAR. Plain types only, so it
        rides `meta` out of a spawned worker.
    """
    declared = ds.get("LossyImageCompression")
    if declared is not None and str(declared).strip() == "01":
        return None
    ts = str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "")
             or "")
    if ts in _DCT_SYNTAXES:
        frame_type = _jpeg_frame_type(_first_frame(ds))
        if frame_type not in _DCT_FRAME_TYPES:
            return None
        evidence, value = "dct", frame_type
    elif ts in JPEGLS_SYNTAXES:
        near = _jpegls_near(_first_frame(ds))
        if near is None or near <= 0:
            return None
        evidence, value = "near", near
    elif ts in J2K_SYNTAXES:
        if _j2k_irreversible(_first_frame(ds)) is not True:
            return None
        evidence, value = "irreversible", None
    else:
        return None
    if isinstance(declared, MultiValue):
        declared = [str(v) for v in declared]
    elif declared is not None:
        declared = str(declared)
    return {"declared": declared, "syntax": ts, "evidence": evidence,
            "value": value}


def _lossy_compression_words(facts) -> str:
    """The LossyImageCompression row, from `_lossy_compression_evidence`.

    Value-free: the only values quoted are the element's own code, capped
    at a Code String's length, and a NEAR integer. Names no file.
    """
    declared = facts["declared"]
    lead = ("is absent" if declared is None
            else f"declares {_cs_quoted(declared)}")
    uid = facts["syntax"]
    if facts["evidence"] == "dct":
        why = (f"its JPEG frame header is SOF{facts['value']}, a DCT process, "
               f"which is lossy by definition ({uid})")
    elif facts["evidence"] == "near":
        why = (f"its JPEG-LS scan declares NEAR {facts['value']}, where 0 is "
               f"lossless ({uid})")
    else:
        why = (f"its JPEG 2000 codestream uses the irreversible 9-7 wavelet "
               f"({uid})")
    return (f"LossyImageCompression (0028,2110) {lead}, and this file's "
            f"pixel data is lossy-compressed: {why}. Recorded as 01 at "
            f"ingest, so an export carries it (PS3.3 C.7.6.1.1.5: 01 conveys "
            f"that the image has undergone lossy compression, and once set "
            f"it shall not be reset). The samples are unchanged."
            ).replace("|", "\\|")


def _high_bit_words(facts) -> str:
    """The HighBit row, from `_high_bit_mismatch`'s facts.

    Names no file: the row's entity is the SOP Instance UID, and a source
    folder may be named for the patient.
    """
    bits_stored = facts["bits_stored"]
    head = (f"HighBit {facts['high_bit']} with BitsStored {bits_stored} "
            f"and BitsAllocated {facts['bits_allocated']}: PS3.5 8.1.1 "
            f"requires HighBit to be BitsStored - 1. ")
    if not facts["encapsulated"]:
        # Q3: pydicom's mask stays. Samples genuinely stored in bits
        # 4..15 come back wrapped; they cannot be told apart from a
        # right-aligned frame with overlay bits above BitsStored.
        read = f"Read as pydicom reads it: the low {bits_stored} bits of each sample"
        if facts["pixel_representation"] == 1:
            read += ", sign-extended"
    elif facts["stream"] is not None:
        read = (f"Read as right-aligned {facts['width_read']}-bit samples "
                f"(the {facts['stream']}'s precision {facts['precision']})")
    else:
        read = (f"Read as right-aligned {bits_stored}-bit samples "
                f"(BitsStored {bits_stored})")
    return (head + read + "; HighBit is not an input to this decode, and an "
            "export writes HighBit as BitsStored - 1.").replace("|", "\\|")


def _item_path_words(path) -> str:
    """A nested item's path as a row reads it: `0008,1140[3] > 0088,0200[0]`."""
    return " > ".join(f"{tag}[{index}]" for tag, index in path)


def _nested_row_prefix(tag, vr, path) -> str:
    """How every ingest row about a nested pixel element begins.

    One spelling for the offset-table row (#433) and the HighBit row
    (#598), so the two rows about one icon name it the same way.
    """
    return f"Standard tag {tag} ({vr}) at {_item_path_words(path)}: "


def _nested_item_syntax(transfer_syntax, item_ds, tag_str) -> str:
    """The transfer syntax a nested pixel element is encoded under (#645).

    The file's, unless the file's is encapsulated and the element has a
    defined length: then the element is native, and is read as Explicit
    VR Little Endian, which is what every encapsulated syntax's native
    encoding is. PS3.5 A.4 lets an icon be compressed or not whatever the
    file carries, and the element's own length is how a reader tells --
    an encapsulated value is always undefined-length (A.4). Measured: a
    `use_compression=True` export writes its icon native inside a
    JPEG 2000 file, and re-ingesting it borrowed the file's syntax, failed
    the decode, and dropped the icon with the unrouted `DATA_LOSS` row.

    An undefined-length element inside a native file is left under the
    file's syntax: it is not a shape a native file can carry, and its
    decode fails into the loss row it always had.
    """
    try:
        encapsulated = pydicom.uid.UID(transfer_syntax).is_encapsulated
    except (ValueError, AttributeError):
        return transfer_syntax
    if not encapsulated:
        return transfer_syntax
    group, element = (int(x, 16) for x in tag_str.split(','))
    elem = item_ds.get(Tag(group, element))
    if elem is None or getattr(elem, "is_undefined_length", True):
        return transfer_syntax
    return str(pydicom.uid.ExplicitVRLittleEndian)


def _decode_nested_pixels(ds, candidates, dropped, instance, *,
                          offset_tables, high_bits, precisions) -> list:
    """Decode every nested (7fe0,0010) `populate_attrs` collected (#183).

    Runs in `ingest_worker`, immediately after the walk that produced
    `candidates` and in the same process, because decoding needs the
    enclosing pydicom `Dataset` and nothing pydicom-shaped may cross a
    process boundary.

    **This function owns the carried/reported decision for its candidates,
    and that is the whole reason it exists.** `populate_attrs` routed them
    here *instead of* into `dropped`; whatever cannot be carried is appended
    to `dropped` below. The tidier-looking alternative -- teaching
    `_is_routed` to answer True for a nested (7fe0,0010) -- would report a
    failed decode as routed and its loss row would vanish, which is the
    silent drop #169 closed and #194 re-opened at a second tag. One
    decision, made by the code that knows the answer.

    Two things it will not do:

    - **Carry encapsulated fragments verbatim.** Measured on an
      RLE-encapsulated source: the nested icon's element value is 90 bytes
      of fragments, while the export writes Implicit VR Little Endian with
      raw bytes (16-byte top-level payload from a 104-byte encapsulated
      source). Writing fragments into that file produces an icon no reader
      can decode, under a transfer syntax that says there are no fragments.
      So it decodes here and stores raw, which is exactly what the
      top-level path does -- one rule for both depths.
    - **Decode a source whose transfer syntax is not allow-listed.** See
      `_CARRIABLE_TRANSFER_SYNTAXES` for which are, and why the rest wait.

    Args:
        ds (pydicom.Dataset): The enclosing dataset, for its `file_meta`.
        candidates (list): `(path, tag, vr, enclosing_ds)` tuples.
        dropped (list): Appended to for every candidate NOT carried, so the
            parent files the `DATA_LOSS` row it always filed.
        instance (Instance): The graph this walk just built, so a carried
            icon's PlanarConfiguration can be corrected on its own item.
        offset_tables (list): Appended to with `(path, tag, vr, counted,
            kind)` for every candidate whose offset table disagrees with
            its NumberOfFrames (#433), `kind` being "excess" (carried,
            truncated to the declared frames) or "fewer" (not carried).
            `import_files` writes the row for each; a "fewer" candidate is
            deliberately *not* also appended to `dropped`, whose row would
            give the wrong reason for the same loss.
        high_bits (list): Appended to with `(path, tag, vr, facts)` for
            every **carried** candidate whose HighBit is not BitsStored - 1,
            `facts` being `_high_bit_mismatch`'s (#598). `import_files`
            writes the top level's `WARNING` row for each. A candidate
            that is not carried appends nothing: its loss row is the one
            it is owed, and a HighBit row beside it would describe a
            decode that never reached the store.
        precisions (list): Appended to with `(path, tag, vr, facts)` for
            every **carried** candidate with a sample its BitsStored
            cannot hold from a stream wider than it, `facts` being
            `_precision_mismatch`'s (#622), on `high_bits`' terms.

    Returns:
        list: `(path, terminal_tag, vr, raw_bytes, sha256)` per carried
        payload. The VR travels because `import_files` may still have to
        report the element -- there is no sidecar on the two callers that
        pass a bare `DicomStore` -- and a loss row that guesses the VR is a
        row the reader cannot check against the file.
    """
    if not candidates:
        return []

    carried = []
    transfer_syntax = str(
        getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "") or "")

    for path, tag_str, vr, item_ds in candidates:
        item_syntax = _nested_item_syntax(transfer_syntax, item_ds, tag_str)
        if item_syntax not in _CARRIABLE_TRANSFER_SYNTAXES:
            dropped.append((tag_str, vr))
            continue

        try:
            # pydicom cannot decode a sequence item's pixel data on its own
            # -- `icon.pixel_array` raises `AttributeError: Unable to decode
            # the pixel data as the dataset's 'file_meta' has no (0002,0010)
            # 'Transfer Syntax UID'`. So the item is given one: the file's,
            # when the item is encoded as the file is, and measured to
            # decode correctly through RLE encapsulation; otherwise a
            # native one (`_nested_item_syntax`, #645). An icon does NOT
            # share the file's transfer syntax by construction -- PS3.5
            # A.4 lets it be native inside a compressed file, and this
            # library's own compressed export writes exactly that.
            if item_syntax == transfer_syntax:
                item_ds.file_meta = ds.file_meta
            else:
                item_ds.file_meta = FileMetaDataset()
                item_ds.file_meta.TransferSyntaxUID = item_syntax
            # The top level's #418 check, at this depth (#433). After the
            # borrow, because it reads the transfer syntax off
            # `file_meta`. An excess is truncated to the declared frames,
            # as at the top level; without it pydicom returned every frame
            # the table names, and the icon was carried whole under a
            # header declaring fewer -- no row at ingest, and an export
            # that then dropped it blaming an Integrity Error. Fewer than
            # declared is not carried: there is no frame for the ones the
            # table does not name. Neither refuses the file -- an icon is
            # not a reason to lose the instance.
            counted = offset_table_frame_count(item_ds)
            decode_kwargs = {}
            if counted is not None and counted[0] != counted[1]:
                if counted[0] < counted[1]:
                    offset_tables.append((path, tag_str, vr, counted,
                                          "fewer"))
                    continue
                decode_kwargs["allow_excess_frames"] = False
            # The top level's #455 header rule, at this depth (#598).
            # After the borrow, because it reads the transfer syntax off
            # `file_meta`; asked before the decode, as at the top level,
            # and kept below only once the decode has succeeded.
            facts = _high_bit_mismatch(item_ds)
            arr, decoded_pi = _decode_pixels(item_ds, **decode_kwargs)
            # Asked of the decoded samples (#622), so after the decode;
            # inside the borrow, because it walks the frames by the
            # borrowed transfer syntax.
            precision = _precision_mismatch(item_ds, arr)
            if decode_kwargs:
                offset_tables.append((path, tag_str, vr, counted, "excess"))
        except Exception:  # pylint: disable=broad-except
            # Every reason a decode can fail takes the same route, and it is
            # the route this element already took: a loss row. Not the
            # `return ... "Decompression Failed"` the top-level arm takes --
            # an icon that will not decode is not a reason to refuse the
            # file, and the instance behaves in every respect as it did
            # before #183. `test_an_undecodable_nested_icon_still_files_its
            # _loss_row` is the tripwire; its fixture declares no
            # BitsAllocated, so pydicom raises `Missing required element`.
            dropped.append((tag_str, vr))
            continue
        finally:
            # The borrow is for the decode only. `ds` is walked again below
            # for waveforms, and a sequence item left carrying a `file_meta`
            # is a shape nothing else in this codebase expects.
            if "file_meta" in item_ds.__dict__:
                del item_ds.__dict__["file_meta"]

        raw = arr.tobytes()
        carried.append(
            (path, tag_str, vr, raw, hashlib.sha256(raw).hexdigest()))
        if facts is not None:
            high_bits.append((path, tag_str, vr, facts))
        if precision is not None:
            precisions.append((path, tag_str, vr, precision))

        # The same correction the top-level arm makes just below, for the
        # same reason: pydicom de-planarises on read, so the bytes are
        # interleaved whatever the source declared. Leaving a nested
        # PlanarConfiguration of 1 in place would export interleaved bytes
        # under a planar declaration -- a colour icon read as garbage by a
        # conformant reader, which is a worse outcome than the drop this
        # change replaces. Isocenter holds and stores pixels interleaved,
        # always; see `SidecarPixelLoader.__call__`.
        target = resolve_item_path(instance, path)
        if target is not None and target.attributes.get("0028,0006") == 1:
            target.set_attr("0028,0006", 0)
        # And the colour space, for the same reason the top-level arm
        # corrects it (#372): the bytes above are whatever pydicom
        # decoded to, and the item's declared label is not consulted by
        # that decode. Only on difference, so a monochrome or RGB icon
        # bumps no revision.
        if target is not None and target.attributes.get("0028,0004") != decoded_pi:
            target.set_attr("0028,0004", decoded_pi)

    return carried


#: The reason a file is failed with when its parsed result could not be
#: pickled back to the parent (#651). A constant because tests match its
#: prefix and the CHANGELOG quotes it.
_UNCROSSABLE_RESULT = ("Its parsed result could not be returned from the "
                       "ingest worker")


def ingest_worker(fp: str) -> Tuple:
    """
    Worker function to read DICOM and construct Instance object.

    Designed for parallel execution. Reads a file, extracts metadata, constructs
    an Instance object, and optionally extracts raw pixel data and raw waveform
    data for eager sidecar loading.

    Args:
        fp (str): File path to read.

    Returns:
        tuple: (metadata_dict, instance_object, pixel_bytes, pixel_hash,
        pixel_alg, waveform_bytes, waveform_hash, error_string)
    """
    try:
        # Eager load (read pixels)
        ds = pydicom.dcmread(fp, stop_before_pixels=False, force=True)

        # Determine SOP Class UID with fallback to File Meta
        sop_class = str(ds.get("SOPClassUID", ""))
        if not sop_class and "MediaStorageSOPClassUID" in ds.file_meta:
            sop_class = str(ds.file_meta.MediaStorageSOPClassUID)

        # Extract Linking Metadata
        meta = {
            'pid': ds.get("PatientID", "UnknownPatient"),
            'pname': str(ds.get("PatientName", "Unknown")),
            'sid': ds.get("StudyInstanceUID", "UnknownStudy"),
            # Absent stays absent. This used to default to "19000101",
            # and nothing downstream could tell that from a real date --
            # SHIFT_DATE jittered it and the result was exported as
            # genuine study timing, so a study that never had a date
            # acquired one near 1900 (#60).
            'sdate': str(ds.StudyDate) if "StudyDate" in ds else None,
            'ser_id': ds.get("SeriesInstanceUID", "UnknownSeries"),
            'modality': ds.get("Modality", "OT"),
            'sop': ds.get("SOPInstanceUID", None),
            'sop_class': sop_class,
            'man': ds.get("Manufacturer", ""),
            'model': ds.get("ManufacturerModelName", ""),
            'dev_sn': ds.get("DeviceSerialNumber", ""),
            'series_num': ds.get("SeriesNumber", 0)
        }

        if not meta['sop']:
            raise ValueError("Missing SOPInstanceUID. Likely not a valid DICOM file.")

        # Construct Instance (Metadata Only)
        inst = Instance(meta['sop'], meta['sop_class'], 0, file_path=fp)
        # Rides `meta` rather than a ninth tuple slot, which is the
        # channel #36's multiplex-group loss already uses. This worker
        # may be in a subprocess with no store handle, so the loss
        # travels and the parent records it (#125, and #126 for the
        # export side of the same constraint).
        dropped = []
        unscanned = []
        nested = []
        populate_attrs(ds, inst, dropped, unscanned=unscanned, nested=nested)
        # Between the walk and `meta['dropped_private_binary']`, so the
        # candidates that failed to decode land in `dropped` before it is
        # handed over. `_decode_nested_pixels` appends them itself: it is
        # the code that knows (#183, #194).
        nested_offset_tables = []
        nested_high_bits = []
        nested_precisions = []
        meta['nested_pixels'] = _decode_nested_pixels(
            ds, nested, dropped, inst, offset_tables=nested_offset_tables,
            high_bits=nested_high_bits, precisions=nested_precisions)
        meta['nested_offset_table'] = nested_offset_tables
        # Ints, bools, strs and None only, so it pickles from a spawned
        # worker like the rest of `meta` (#598).
        meta['nested_high_bit'] = nested_high_bits
        meta['nested_precision'] = nested_precisions
        meta['dropped_private_binary'] = dropped
        # Rides `meta` for the same reason as `dropped_private_binary`
        # above: this worker may be in a subprocess with no store
        # handle, and the return arity is unpacked at every call site.
        meta['unscanned_private_sequences'] = unscanned

        # Isocenter internally manages pixels as standard contiguous arrays (Interleaved)
        # So we MUST ensure PlanarConfiguration=0 in metadata to match our converted data
        if inst.attributes.get("0028,0006") == 1:
            inst.set_attr("0028,0006", 0)

        # Extract & Process Pixel Data
        p_bytes = None
        p_hash = None
        p_alg = None

        if "PixelData" in ds:
            # The offset table against NumberOfFrames, before the decode
            # (#418). Asked here, not left to the decoder: pydicom returns
            # every frame the table names -- and, with no table it walks
            # by, every frame its walk of the fragments finds, or every
            # whole frame a native element's length holds (#620) -- so an
            # excess used to be stored whole under a header that declared
            # fewer, and the instance was accepted with no row and could
            # never be read back -- the loader refused it later with an
            # Integrity Error, against the wrong cause.
            #
            # Excess: keep the declared frames. NumberOfFrames is the
            # dataset's declared shape, and frames beyond it are not
            # addressable by any conformant reader of this object;
            # refusing would throw away a readable image. What was dropped
            # rides `meta`, like `waveform_groups` below, because this may
            # be a subprocess with no store handle -- `import_files`
            # writes the DATA_LOSS row.
            #
            # Fewer frames than declared: refused, with a reason naming
            # both counts. There is no frame to keep for the ones the
            # table does not name, and the decoder's own refusal was
            # pydicom's message-less StopIteration -- an ERROR row reading
            # `Decompression Failed: ` and nothing else.
            decode_kwargs = {}
            counted = offset_table_frame_count(ds)
            if counted is not None and counted[0] != counted[1]:
                if counted[0] < counted[1]:
                    return ({'path': fp}, None, None, None, None, None, None,
                            frame_count_mismatch_words(counted))
                decode_kwargs['allow_excess_frames'] = False
                meta['offset_table_excess'] = counted
            # Asked of the header before the decode, so both decoders'
            # files get it; attached only once the decode succeeds (#455).
            high_bit_mismatch = _high_bit_mismatch(ds)
            # The same shape for #601: asked of the header and frame 0
            # before the decode, recorded only once the decode succeeds.
            # Top level only -- 0028,2110 is the General Image Module's,
            # and an icon is not the image.
            lossy = _lossy_compression_evidence(ds)
            try:
                # Always decompress to raw bytes to ensure sidecar has consistent format (SidecarPixelLoader expects raw)
                # This handles RLE/JPEG/J2K by decoding them now.
                arr, decoded_pi = _decode_pixels(ds, **decode_kwargs)
                p_bytes = arr.tobytes()
                p_alg = 'zlib'  # Always compress the raw bytes
                # The label has to say what the bytes are, and the bytes
                # are whatever pydicom decoded to -- for every 8-bit YBR
                # family that is RGB, under any transfer syntax, native
                # included; `pixel_array` converts and never touches
                # `PhotometricInterpretation`. `populate_attrs` copied the
                # declared label above, so without this a YBR source
                # exported `YBR_FULL` over RGB bytes and a conformant
                # reader showed `(169, 255, 65)` for `(220, 40, 90)`, or
                # refused a `YBR_FULL_422` file outright (#372). AFTER the
                # decode, unlike the PlanarConfiguration write above,
                # which reads nothing from the array and must stay
                # before it. Written only on difference, the same shape
                # as `_write_str_if_changed`: an RGB, monochrome or
                # palette source has meta equal to its label and bumps
                # no revision. Not derived from the array's shape -- a
                # 3-sample array is equally RGB or YBR_FULL, which is why
                # #186 removed the `samples >= 3` relabel; the decoder's
                # meta is the one place the colour space is stated.
                if inst.attributes.get("0028,0004") != decoded_pi:
                    inst.set_attr("0028,0004", decoded_pi)
                if high_bit_mismatch is not None:
                    # Rides `meta` like `offset_table_excess`: this may be
                    # a subprocess with no store handle, so `import_files`
                    # writes the row.
                    meta['high_bit_mismatch'] = high_bit_mismatch
                # A sample BitsStored cannot hold, from a stream wider
                # than it (#622): asked of the decoded array, so here.
                precision_mismatch = _precision_mismatch(ds, arr)
                if precision_mismatch is not None:
                    meta['precision_mismatch'] = precision_mismatch
                if lossy is not None:
                    # In the worker, on an Instance nothing has linked yet,
                    # beside the relabel above; the row rides `meta`.
                    inst.set_attr("0028,2110", "01")
                    meta['lossy_compression'] = lossy
            except Exception as e:
                # If decompression fails (missing codec), we cannot ingest safely for sidecar usage.
                # The path rides the meta slot, as in the blanket except
                # below, so the parent's ERROR row can name the file (#211).
                return ({'path': fp}, None, None, None, None, None, None,
                        f"Decompression Failed: {describe_exception(e)}")

        elif any(kw in ds for kw in ("FloatPixelData", "DoubleFloatPixelData")):
            # The float pair rides the sidecar too, and until #183 it did
            # not. `populate_attrs` skips group 7fe0 and `_is_routed`
            # called these "routed" because `get_pixel_data()` re-read
            # them out of the *source file* -- so the pixels survived
            # only as long as that file stayed where it was. Move it,
            # delete it, or export from a session reopened on another
            # machine, and a float instance produced `FileNotFoundError:
            # Pixels missing and file not found` on an image modality,
            # and on a non-image modality a file with no pixel element
            # and no loss row at all.
            #
            # `elif`: PS3.5 Section 8.2 forbids an instance carrying both, and
            # the export prefers the sidecar array whenever there is a
            # loader -- so on a malformed file that carries both, Pixel
            # Data wins here and the float half is genuinely lost, which
            # is exactly what `_is_routed`'s `has_pixel_data` clause
            # still reports. One question, one answer.
            try:
                arr = np.ascontiguousarray(ds.pixel_array)
                # This arm does not pass through `_decode_pixels`, so it
                # normalises byte order itself (#648): a big-endian
                # FloatPixelData decodes to `>f4`, and its `tobytes()`
                # stored `[0.5, -1.25, 1000, 3]` as values near 1e-41.
                if arr.dtype.byteorder not in ('=', '|'):
                    arr = arr.astype(arr.dtype.newbyteorder('='))
                p_bytes = arr.tobytes()
                p_alg = 'zlib'
                # The dtype the sidecar will have to reconstruct with,
                # taken from the element rather than from `arr.dtype`
                # so that the two ends of the mapping are one table.
                # Kept as an underscore key so it rides
                # `attributes_json` untouched -- `_merge` skips
                # `t.startswith("_")`, the same channel
                # `_ISOCENTER_REDACTION_HASH` already uses -- rather
                # than as a schema change, which #183 also weighs and
                # which this does not need.
                inst.attributes[PIXEL_DTYPE_ATTR] = FLOAT_DTYPE_BY_ELEMENT[
                    TAG_FLOAT_PIXEL_DATA if "FloatPixelData" in ds
                    else TAG_DOUBLE_FLOAT_PIXEL_DATA]
            except Exception:  # pylint: disable=broad-except
                # NOT the `return ... "Decompression Failed"` the Pixel
                # Data arm above takes, and the asymmetry is deliberate.
                # This change is meant to be additive: an undecodable
                # float source ingested before #183 and failed loudly at
                # export -- `RuntimeError: Lazy load failed ...` from
                # `get_pixel_data()`, one ERROR audit row naming
                # pydicom's own words, `0 of 1` written and a
                # REVIEW_REQUIRED grade (#226). Rejecting it here
                # instead would move that failure to the door and leave
                # the report saying `0 of 0 requested`, which is a
                # weaker claim about a file that was handed to us.
                #
                # `p_bytes` stays None, so nothing reaches the sidecar,
                # no provenance is recorded, and the instance behaves in
                # every respect as it did before. The float sources that
                # *can* be decoded gain the sidecar; the ones that
                # cannot lose nothing.
                p_bytes = None
                p_alg = None

        if p_bytes:
            # Hash the RAW bytes (stable hash)
            p_hash = hashlib.sha256(p_bytes).hexdigest()

        # Extract Waveform Data
        # populate_attrs treats (5400,1010) as routed (#151 changed the
        # binary rule to a size gate, but routed elements stay out of the
        # graph regardless), so it never reaches the
        # object graph on its own. Pull it out explicitly, exactly as PixelData
        # is handled above, and offload the bytes to the sidecar.
        # Only the first Waveform Sequence item is handled; multi-item
        # sequences (e.g. multiplexed rhythm + median) keep item 0 only.
        #
        # The count is reported back in `meta` rather than kept here,
        # because this function runs in a worker process and cannot reach
        # the audit log. `import_files` warns and records the loss on the
        # far side. It rides in `meta` rather than as a tenth tuple
        # element so the return arity -- unpacked at every call site --
        # does not change.
        w_bytes = None
        w_hash = None
        meta['waveform_groups'] = len(ds.WaveformSequence) if "WaveformSequence" in ds else 0

        if "WaveformSequence" in ds and len(ds.WaveformSequence) > 0:
            wf_item = ds.WaveformSequence[0]
            raw = getattr(wf_item, "WaveformData", None)
            if raw:
                w_bytes = bytes(raw)
                w_hash = hashlib.sha256(w_bytes).hexdigest()

        # The samples of groups 1..n are discarded just above; their
        # sequence items go with them. `populate_attrs` walks the whole
        # sequence, so the graph used to hold one item per group while
        # the sidecar held one group's bytes -- and the export wrote
        # every item, producing a file that declared a multiplex group
        # and carried no Waveform Data for it. (5400,1010) is Type 1
        # (PS3.3 C.10.9): a conformant reader may reject such a file, and
        # a trusting one reads `NumberOfWaveformSamples` with nothing
        # behind it (#160).
        #
        # Dropped at ingest rather than at export because the graph is
        # what every consumer reads -- the DICOM writer, the WFDB record,
        # the annotation bridge, the PHI report. Patching the writer
        # alone would leave the rest describing a group whose samples
        # this pipeline does not have. Nothing is hidden by dropping
        # them: `import_files` warns and files the DATA_LOSS entry from
        # `meta['waveform_groups']`, which still carries the source's
        # original group count.
        #
        # This is not a position on #150. It is correct under every
        # answer there, and if multi-rate support ever lands the block
        # stops firing on its own -- the items are dropped because the
        # samples are, and then they would not be.
        wf_seq = inst.sequences.get("5400,0100")
        if wf_seq is not None and len(wf_seq.items) > 1:
            del wf_seq.items[1:]

            # And the references to what the del removed (#177).
            # Waveform Annotation Sequence (0040,B020) sits at instance
            # level and names a multiplex group by the ordinal of its
            # Waveform Sequence item (PS3.3 C.10.10.1.1), so the del
            # above turns any annotation on groups 1..n into a
            # reference to an item the exported file does not carry.
            # Filtered on (group, channel) pairs, never renumbered --
            # the ordinal is positional, and renumbering would make the
            # file internally consistent and wrong about the source.
            # Counts ride `meta` like `waveform_groups` above: this
            # worker may be in a subprocess, so `import_files` files
            # the loss on the far side.
            ann_dropped, ann_rewritten, ann_groups = \
                filter_dangling_annotation_refs(
                    inst, kept_items=len(wf_seq.items))
            meta['dropped_annotations'] = ann_dropped
            meta['rewritten_annotations'] = ann_rewritten
            meta['dropped_annotation_groups'] = ann_groups

        # The result has to cross the process boundary, and the pool pickles
        # it in its own hand-back, outside this `try` (#651). A result that
        # cannot be pickled raised there, and the parent cannot catch it
        # per file: `executor.map` re-raises at the point of iteration and
        # cancels every future queued behind it, and `run_parallel` is
        # called without a per-item arm -- so one such file lost the whole
        # pass, with no row. Asked here, it is one failed file on the
        # channel every other failure uses. `_process_safe` is the fix for
        # the value that was found; this is the guarantee for the next one.
        #
        # `(meta, inst)` and not `inst` alone: `meta` carries live objects
        # too (the nested pixel candidates, among others). The other five
        # slots are `bytes`, `str` or `None` and always pickle, and the
        # pixel bytes are the expensive part, so they are left out. On
        # every strategy, threads included, where nothing is pickled: one
        # answer per file whatever the pool, and an unpicklable instance
        # admitted on threads is a graph no process worker can later take.
        try:
            pickle.dumps((meta, inst), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:  # pylint: disable=broad-exception-caught
            return ({'path': fp}, None, None, None, None, None, None,
                    f"{_UNCROSSABLE_RESULT}: "
                    f"{describe_exception_without_paths(e)}")

        return (meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, None)
    except Exception as e:
        # `{'path': fp}` rather than None in the meta slot, so the
        # parent can name the file in its ERROR row without parsing the
        # prose. The reason travels as the error string it always was;
        # the arity -- unpacked at every call site -- does not change
        # (#211).
        return ({'path': fp}, None, None, None, None, None, None,
                describe_exception(e))


@dataclass
class IngestSummary:
    """What one ingest run did, for the caller that has to know (#211).

    `import_files` used to return nothing: a run that rejected 40 of
    500 files reported exactly like a clean one, modulo console lines
    captured nowhere, and the caller's first hint was an empty query
    result. The mirror of `ExportSummary`, which #181 introduced for
    the same hole on the export side.

    A file takes exactly one of four routes, and they are four fields
    because they answer different questions: `ingested` reached the
    graph; `failures` were rejected with a reason (and each has an
    `ERROR` audit row); `declined` were refused because the session
    already holds their SOP Instance UID -- as the redacted copy's
    pre-redaction identity (#238), or as the UID of another instance
    (#431) -- each audited as `WARNING` and never read into the store;
    `skipped` were already in the store and were not read again. A
    declined file is not recorded as imported, so offering it again
    declines it again.
    """
    ingested: int = 0
    #: `(path, reason)` per rejected file -- the same pair the `ERROR`
    #: audit row carries, so the summary and the trail cannot disagree.
    failures: List[Tuple[str, str]] = field(default_factory=list)
    declined: int = 0
    skipped: int = 0

    @property
    def failed(self) -> int:
        """How many files were rejected."""
        return len(self.failures)


class DicomImporter:
    """
    Handles scanning of folders/files and ingesting them into the Object Graph.

    Optimized for parallel processing using `run_parallel` and Eager Ingestion methods.
    """
    @staticmethod
    def import_files(file_paths: List[str], store: DicomStore, executor=None,
                     sidecar_manager=None, store_backend=None):
        """
        Parses a list of files or directories. Recurses into directories to find all files.

        Identifies new files (not already in the store), reads them in parallel,
        and links them into the provided DicomStore's hierarchy (Patient/Study/Series).

        Args:
            file_paths (List[str]): List of file or directory paths to scan.
            store (DicomStore): The active store to populate.
            executor (optional): Shared ProcessPoolExecutor.
            sidecar_manager (optional): Manager for persisting pixel data immediately.
            store_backend (optional): SqliteStore used to register sidecar
                blob references. Waveform blobs are invisible to compaction
                unless recorded here.

        Returns:
            IngestSummary: what reached the graph and what did not.
                Returned nothing until #211 -- a per-file failure was a
                console line, so a run that rejected 8% of its files
                was indistinguishable from a clean one by any caller.
        """
        all_files = []
        for path in file_paths:
            if os.path.isfile(path):
                all_files.append(path)
            elif os.path.isdir(path):
                for root, _, filenames in os.walk(path):
                    for filename in filenames:
                        if filename.startswith('.'):
                            continue
                        all_files.append(os.path.join(root, filename))
        # `os.walk` order is the filesystem's -- measured: APFS lists
        # neither sorted nor in creation order, HFS+ lists sorted -- and
        # #431 keeps the first file linked for a duplicated SOP Instance
        # UID, so without this the same folder kept a different file on a
        # different volume (#450). The key is the path string as built
        # above, the one the declined row prints: not `abspath` or
        # `realpath`, which would reorder a symlinked tree, and not
        # locale-aware, which would differ by machine.
        all_files.sort()

        known_paths = store.get_ingested_paths()
        new_files = [fp for fp in all_files
                     if os.path.abspath(fp) not in known_paths]

        logger = get_logger()
        skipped_count = len(all_files) - len(new_files)
        if skipped_count > 0:
            logger.info(f"Skipping {skipped_count} already imported files.")

        if not new_files:
            return IngestSummary(skipped=skipped_count)

        logger.info(f"Importing {len(new_files)} files (Parallel Eager Ingest)...")

        # 1. Build Fast Lookup Maps (O(1))
        patient_map = {p.patient_id: p for p in store.patients}
        study_map = {}  # Key: study_uid -> Study
        series_map = {}  # Key: series_uid -> Series

        # Every SOP Instance UID the session already holds, and the
        # instance holding it (#431). Seeded from the graph, which equals
        # the store at open (`Session.__init__` loads it all), so a file
        # duplicating a *stored* instance is caught exactly like one
        # duplicating an instance ingested a moment ago in this loop.
        held = {}

        # Populate deep maps
        for p in store.patients:
            for st in p.studies:
                study_map[st.study_instance_uid] = st
                for se in st.series:
                    series_map[se.series_instance_uid] = se
                    for held_inst in se.instances:
                        held.setdefault(held_inst.sop_instance_uid, held_inst)

        # 2. Parallel Execution
        # OPTIMIZATION: Use return_generator=True to stream results.
        # This prevents accumulating result tuples (with huge p_bytes) in a list (O(N) memory).
        # We process each result immediately and discard it (O(1) memory).
        # OPTIMIZATION: chunksize=1 to prevent buffering multiple large files in IPC queue
        #
        # The strategy is resolved here, once, and handed to
        # `run_parallel` as `strategy=`, which then ignores its own
        # resolution keywords -- so `chunksize`, `desc` and the progress
        # bar live in this call and nowhere else. Resolved here because
        # this is the one frame that can say what the strategy is worth
        # to ingest: see the warning below (#393).
        strategy = _resolve_strategy(None, 1, None, False, False, True,
                                     "Ingesting", None)
        # `ingest()` hands in the session's `ProcessPoolExecutor`, which
        # `_run_on_shared_executor` uses as given, so a threads lever the
        # strategy granted reaches nothing (#390). Say so, once per call
        # that dispatches, where the operator who set the variable will
        # look for its effect (#393, in #400's shape: it names only the
        # knob that was set, says the result is correct, and bounds
        # itself). The conditions are each load-bearing:
        #   - `threads_requested_by`, not `use_threads`: a free-threaded
        #     build resolves to threads with nothing set, and warning
        #     there would be a line on every 3.14t ingest about a
        #     variable nobody touched;
        #   - `use_threads` as well: when recycling beat the request,
        #     #185's warning -- emitted inside `run_parallel`, and
        #     already naming `ingest()` -- is the one line;
        #   - an executor that is not a thread pool: with none,
        #     `run_parallel` builds its own pool and honours the lever,
        #     and a `ThreadPoolExecutor` is already threads.
        # Here and not in `run_parallel`, because "ingest() has no threads
        # mode" is ingest's knowledge; a future caller that hands in a
        # process pool on purpose has promised nothing about the lever.
        # After the `not new_files` return, so an ingest with nothing to
        # read is silent, and before the dispatch, so a dispatch that
        # raises cannot swallow it.
        if (strategy.threads_requested_by is not None
                and strategy.use_threads
                and executor is not None
                and not isinstance(executor,
                                   concurrent.futures.ThreadPoolExecutor)):
            logger.warning(
                "%s had no effect on this ingest(). ingest() runs on the "
                "session's own process pool, so it ran in processes and "
                "its result is unaffected. The variable still applies to "
                "audit(), scan_pixel_content() and redact() in this "
                "process; ingest() has no threads mode.",
                strategy.threads_requested_by)
        # The recycling lever has the same shape one rank up (#471,
        # #393's twin). `_resolve_strategy` read
        # `ISOCENTER_MAX_TASKS_PER_CHILD` into the strategy, and the
        # session's `ProcessPoolExecutor` -- built once at `Session()`
        # with no `max_tasks_per_child`, and used as given -- never
        # recycles a worker: 24 tasks under the variable set to 2 ran on
        # no more distinct worker PIDs than the pool has workers, on
        # both gate builds.
        #
        # Honouring it here was weighed and not taken, for two reasons,
        # and the first is the trap for anyone tempted to "just pass
        # the kwarg". `ProcessPoolExecutor(max_tasks_per_child=)`
        # DEADLOCKS `map` on 3.12, the floor, the first time a worker
        # actually has to be replaced. Measured on 3.12.14, macOS spawn,
        # 2 workers x 2 tasks per child: 4 tasks complete and 12 hang,
        # 3 runs of 3; at the session's own shape 30 complete and 400
        # hang. 3.14 and 3.14t are fine (12 tasks on 6 PIDs, in order),
        # and Ubuntu was not measured. So on the one interpreter the
        # package promises as its floor, honouring the lever would have
        # hung `ingest()` rather than recycled it, and "only
        # `multiprocessing.Pool` recycles" is operationally true there
        # (#501). Second, the pool is built once at `Session()`: a
        # variable read there would be the one construction-time read of
        # a precedence lever in the package, and set after the session
        # opened it would be ignored in the same silence one step later.
        # The conditions:
        #   - `maxtasksperchild`, which this call never passes as an
        #     argument, so any value in the strategy is the variable's;
        #     the name comes from `processes_requested_by`, the
        #     attribution field, rather than being spelled here (#400);
        #   - `threads_request_overridden_by is None`: with
        #     `ISOCENTER_FORCE_THREADS` also set, #185's warning --
        #     emitted inside `run_parallel`, and already saying that
        #     `ingest()` takes no lever -- is the one line;
        #   - any caller-supplied executor. Unlike the threads case, no
        #     pool type is exempted, and that was a choice rather than a
        #     limit: `ProcessPoolExecutor._max_tasks_per_child` and
        #     `Pool._maxtasksperchild` could be read to tell a recycling
        #     pool from one that is not, but both are private, no caller
        #     hands one in, and a pool recycling at some other interval
        #     would still not be the one the variable names. With none,
        #     `run_parallel` builds the recycling pool itself.
        if (strategy.maxtasksperchild is not None
                and strategy.threads_request_overridden_by is None
                and executor is not None):
            logger.warning(
                "%s had no effect on this ingest(). ingest() runs on the "
                "session's own process pool, which never recycles its "
                "workers, so it ran without recycling and its result is "
                "unaffected. The variable still applies to audit(), "
                "scan_pixel_content(), discover_redaction_zones() and "
                "redact() in this process, and export() always recycles "
                "every 25 tasks; ingest() has no recycling.",
                strategy.processes_requested_by)
        #
        # `ordered=True` keeps the results in the sorted order above on
        # the one path that would otherwise yield by arrival: the
        # recycling pool, which a direct `import_files(executor=None)`
        # reaches under `ISOCENTER_MAX_TASKS_PER_CHILD`. The session's
        # shared executor is ordered already (#450).
        results = run_parallel(
            ingest_worker,
            new_files,
            executor=executor,
            return_generator=True,
            ordered=True,
            strategy=strategy)

        # 3. Aggregation (Streaming)
        #
        # Snapshotted once, before the loop. Nothing this loop appends to
        # the graph carries a retired identity -- only `regenerate_uid()`
        # writes one -- so a snapshot taken here cannot go stale during
        # it, and re-querying per result would walk the whole graph once
        # per file.
        superseded = store.get_superseded_uids()

        # The sidecar gate for sites 1-3 below (#368). Every frame this
        # loop appends is written under it, per result, so no append
        # can land in a file `compact_sidecar` is replacing. Callers that
        # pass a bare `DicomStore` and no backend (two test callers, and
        # the fixture generators) have no store to gate on and no
        # compaction to race, so they get a no-op.
        gate = (store_backend._hold_sidecar_gate
                if hasattr(store_backend, "_hold_sidecar_gate")
                else contextlib.nullcontext)
        # Two refusals, counted apart so the closing log line can say
        # which one happened; `IngestSummary.declined` is their sum.
        declined_superseded = 0
        declined_duplicate = 0
        high_bit_rows = 0
        lossy_rows = 0
        precision_rows = 0
        count = 0
        failures: List[Tuple[str, str]] = []

        def _record_failure(path, reason):
            """One rejected file: the log line, the summary, the trail.

            `ERROR`, not `DATA_LOSS`, and that is the scoping decision
            (#211): loss rows describe elements missing from data that
            *was* ingested, and this file never entered the store --
            nothing it holds is smaller than it claims. Same vocabulary
            #181 gave the export side's failures, and the same reader:
            `get_audit_errors()` feeds the report's Exceptions section
            and bars the PASS grade, so a cohort that lost files does
            not grade as though it did not. The path stands in the
            entity column because a file that failed to parse has no
            SOP Instance UID to be named by -- the fallback
            `_report_export_failures` already uses. Flattened and
            pipe-escaped for the same reason as there: the detail is
            rendered straight into a markdown table row.
            """
            detail = " ".join(
                f"Ingest failed for {path}: {reason}".split()
            ).replace("|", "\\|")
            logger.error(detail)
            failures.append((path, str(reason)))
            if store_backend is not None:
                store_backend.log_audit(
                    action_type="ERROR", entity_uid=path, details=detail)

        def _record_high_bit(uid, detail):
            """One HighBit row, at the top level or on a carried icon.

            One counter and one suppression line for both depths (#598):
            it is one fact about the header, and a cohort whose every
            instance carries a mismatched icon as well as a mismatched
            frame must not print twice the lines the cap promises.
            """
            nonlocal high_bit_rows
            high_bit_rows += 1
            if high_bit_rows <= 5:
                logger.warning(f"{uid}: {detail}")
            elif high_bit_rows == 6:
                logger.warning(
                    "... (suppressing further per-instance "
                    "messages for HighBit other than BitsStored "
                    "- 1) ...")
            if store_backend is not None:
                store_backend.log_audit(
                    action_type="WARNING", entity_uid=uid, details=detail)

        def _record_lossy(uid, detail):
            """One LossyImageCompression row (#601), on its own log cap.

            Its own counter, not the HighBit one: a cohort of near-lossless
            files must not suppress a header fact about another file
            before its first line is printed.
            """
            nonlocal lossy_rows
            lossy_rows += 1
            if lossy_rows <= 5:
                logger.warning(f"{uid}: {detail}")
            elif lossy_rows == 6:
                logger.warning(
                    "... (suppressing further per-instance messages for "
                    "LossyImageCompression recorded from the pixel data) "
                    "...")
            if store_backend is not None:
                store_backend.log_audit(
                    action_type="WARNING", entity_uid=uid, details=detail)

        def _record_precision(uid, detail):
            """One stream-precision row (#622), top level or icon, on its own cap.

            Its own counter, for `_record_lossy`'s reason: a cohort of
            12-bit streams under BitsStored 8 must not suppress another
            file's HighBit line before it is printed. One counter for both
            depths, for `_record_high_bit`'s.
            """
            nonlocal precision_rows
            precision_rows += 1
            if precision_rows <= 5:
                logger.warning(f"{uid}: {detail}")
            elif precision_rows == 6:
                logger.warning(
                    "... (suppressing further per-instance messages for "
                    "a stream wider than BitsStored) ...")
            if store_backend is not None:
                store_backend.log_audit(
                    action_type="WARNING", entity_uid=uid, details=detail)

        for meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, err in results:
            # Clear result components from scope as soon as possible after use to help GC
            # But the loop variable holds them. Next iteration clears them.
            # `is not None`, not truthiness. An empty reason is still a
            # failure: `str(KeyError())` is `''`, so a worker whose
            # reason was built from a message-less exception returned
            # `''` here, this test was falsy, `inst` was None too, and
            # the file was counted nowhere -- no failure, no audit row,
            # not declined, not skipped (#435). Every failing return in
            # `ingest_worker` carries a non-None reason and the success
            # return carries None; `describe_exception` makes today's
            # reasons non-empty, and this makes an empty one impossible
            # to lose.
            if err is not None:
                _record_failure((meta or {}).get('path', '<unknown>'), err)
                continue
            if inst:
                try:
                    # Above the sidecar write, deliberately. This file is
                    # the un-redacted original of an image the store
                    # already holds in redacted form -- it kept its SOP
                    # Instance UID while the redacted copy took a
                    # generated one (#228), so nothing else in the graph
                    # can tell they are the same image. Linking it back
                    # in puts the burned-in identifier into the store,
                    # the sidecar and the export (#238), and
                    # `persist_pixel_data` does not de-duplicate, so a
                    # write here would also strand the frame (#235).
                    supersedes = superseded.get(inst.sop_instance_uid)
                    if supersedes:
                        detail = (
                            f"Not importing {inst.file_path}: SOP Instance "
                            f"UID {inst.sop_instance_uid} is the "
                            f"pre-redaction identity of {supersedes}, which "
                            f"this session already holds. The file still "
                            f"carries the un-redacted original.")
                        declined_superseded += 1
                        # First five individually, as
                        # `scan_burned_in_annotations` does: a re-run over
                        # a large redacted cohort would otherwise print a
                        # line per file. The audit row is per file
                        # regardless -- it is the compliance trail, and
                        # DATA_LOSS rows are per instance for the same
                        # reason.
                        if declined_superseded <= 5:
                            logger.warning(detail)
                        elif declined_superseded == 6:
                            logger.warning(
                                "... (suppressing further per-file messages "
                                "for superseded sources) ...")
                        # Written in the parent: `import_files` runs here,
                        # so this is not the worker-audit hazard of #126.
                        # Guarded because two test callers pass a bare
                        # `DicomStore` and no backend at all.
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="WARNING",
                                entity_uid=inst.sop_instance_uid,
                                details=detail)
                        continue

                    # A second instance with an SOP Instance UID the
                    # session already holds (#431). The store is keyed on
                    # that UID and its upsert is `ON CONFLICT DO UPDATE`,
                    # so linking this one let the next `save()` overwrite
                    # the holder's row -- series, pixels and all -- with
                    # whichever was linked last, and a reload held one
                    # instance where `ingested` had counted two. Keep the
                    # first, decline the rest, and say so.
                    #
                    # Above the sidecar write, for #238's reason: nothing
                    # de-duplicates a frame, so writing and then declining
                    # would strand it. And a `WARNING`, not `DATA_LOSS`:
                    # this file never entered the store, so no element of
                    # data that was ingested is smaller than it claims
                    # (#211's scoping). `WARNING` rows are exceptions in
                    # the report and bar PASS.
                    #
                    # "First" is first in path order among the files new
                    # to this call -- `all_files` is sorted and results
                    # are consumed in submission order -- and an instance
                    # the session already held beats every new file,
                    # because `held` is seeded from the graph (#450).
                    holder = held.get(inst.sop_instance_uid)
                    if holder is not None:
                        holder_path = holder.source_path or holder.file_path
                        holder_words = (
                            f"the instance ingested from {holder_path}"
                            if holder_path else
                            "an instance in this session that has no "
                            "source file")
                        detail = " ".join((
                            f"Not importing {inst.file_path}: SOP Instance "
                            f"UID {inst.sop_instance_uid} is already held by "
                            f"{holder_words}. A "
                            f"session holds one instance per SOP Instance "
                            f"UID; the first was kept and this file was not "
                            f"read into the store.").split()
                        ).replace("|", "\\|")
                        declined_duplicate += 1
                        if declined_duplicate <= 5:
                            logger.warning(detail)
                        elif declined_duplicate == 6:
                            logger.warning(
                                "... (suppressing further per-file messages "
                                "for duplicate SOP Instance UIDs) ...")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="WARNING",
                                entity_uid=inst.sop_instance_uid,
                                details=detail)
                        continue

                    # Persist Pixels to Sidecar (Main Thread Sequential Write)
                    #
                    # Site 1 of six (#368). The gate is taken per result
                    # around the append, not around the whole loop: a
                    # 10k-file ingest must not hold it for its duration,
                    # or a background save queued behind it expires at
                    # `_SIDECAR_GATE_TIMEOUT_S` on any real dataset. This
                    # site records no blob row -- the pixel row is
                    # committed by the `save(sync=True)` that ends
                    # `ingest()`, under site 6's gate -- and the offset
                    # stays valid between the two because `ingest()`
                    # holds the pass-lock, so no compaction can run.
                    # The `except Exception` arm below runs with the
                    # gate released, so a failing result cannot hold it
                    # while `_record_failure` writes an audit row.
                    if p_bytes and sidecar_manager:
                        with gate():
                            off, leng = sidecar_manager.write_frame(p_bytes, p_alg)
                        # `pixel_hash=p_hash`, explicitly. Left out, the
                        # loader's integrity check never ran on this path:
                        # `inst._pixel_hash` was set on the next line, but a
                        # loader built with no hash has nothing to compare,
                        # and `save()` keeps this loader object -- so
                        # another frame's bytes at this offset read back as
                        # this instance's pixels (#436). `p_hash` is
                        # `sha256(p_bytes)`, the stored bytes the check
                        # reads. Passed rather than left to the loader's
                        # fallback to `inst._pixel_hash`, which would make
                        # it depend on the order of these two lines (#212).
                        inst._pixel_loader = SidecarPixelLoader(
                            sidecar_manager.filepath, off, leng, p_alg,
                            instance=inst, pixel_hash=p_hash)
                        inst._pixel_hash = p_hash

                    # HighBit other than BitsStored - 1 (#455, #523). A
                    # `WARNING`, the frozen action type (#411): the file's
                    # own header is non-conformant (PS3.5 8.1.1), which is
                    # a fact about the user's data, so it bars PASS
                    # (#479). One row per instance, as the declined rows
                    # above are; the log is capped the same way, so a
                    # 2,000-instance legacy cohort prints five lines and a
                    # suppression line rather than 2,000. After both
                    # declined `continue`s: a file not linked gets no row.
                    high_bit = meta.get('high_bit_mismatch')
                    if high_bit:
                        _record_high_bit(inst.sop_instance_uid,
                                         _high_bit_words(high_bit))

                    # A stream wider than BitsStored, read by the stream
                    # (#622, owner ruling Q2). A `WARNING` for HighBit's
                    # reason: a fact about the file's own header, which
                    # bars PASS. Beside it, on its own log cap.
                    wider = meta.get('precision_mismatch')
                    if wider:
                        _record_precision(inst.sop_instance_uid,
                                          _precision_words(wider))

                    # LossyImageCompression recorded from the pixel data
                    # (#601, owner ruling Q1). A `WARNING`: the stamp is a
                    # permanent claim this library derived, and the row is
                    # what makes it reviewable; it bars PASS. After both
                    # declined `continue`s, like the HighBit row.
                    lossy = meta.get('lossy_compression')
                    if lossy:
                        _record_lossy(inst.sop_instance_uid,
                                      _lossy_compression_words(lossy))

                    # The frames `ingest_worker` dropped because the
                    # offset table named more than NumberOfFrames
                    # declares (#418). Scoped SIGNAL, as the multiplex
                    # groups below are: what was discarded is acquired
                    # image data, so the run is reported AND graded, and
                    # an instance that silently lost frames does not
                    # PASS. SIGNAL rather than a new scope word, because
                    # the scope vocabulary is frozen
                    # (tests/test_frozen_surface.py).
                    excess = meta.get('offset_table_excess')
                    if excess:
                        table_frames, declared, _declared_raw, _table = excess
                        detail = (f"{frame_count_mismatch_words(excess)}. "
                                  f"Kept the first {declared} and discarded "
                                  f"{table_frames - declared}.")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="DATA_LOSS",
                                entity_uid=inst.sop_instance_uid,
                                details=detail,
                                loss_scope=LOSS_SCOPE_SIGNAL)

                    # Silent truncation is the defect here, not the
                    # missing multi-rate support -- that is deferred on
                    # purpose. A record whose groups were dropped without
                    # a word is indistinguishable from one that only ever
                    # had a single group (#36).
                    groups = meta.get('waveform_groups', 0)
                    if groups > 1:
                        dropped = groups - 1
                        detail = (f"WaveformSequence carried {groups} multiplex "
                                  f"groups; kept group 0 and discarded "
                                  f"{dropped}. Multi-rate records are not yet "
                                  f"supported.")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        # The log line alone is not a compliance trail: it
                        # goes to a file the user may never open. The audit
                        # entry is what puts this in the record.
                        #
                        # Scoped SIGNAL, so it is reported AND graded:
                        # the run does not PASS (#150). The tag is
                        # standard -- Waveform Sequence (5400,0100), an
                        # even group -- but what was discarded is
                        # acquired signal, and a 12-lead ECG that came
                        # out holding group 0 under a PASS grade is the
                        # case parity was wrong for. Still not PRIVATE:
                        # the scope states what the element was, and
                        # this one was neither private nor routine.
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="DATA_LOSS",
                                entity_uid=inst.sop_instance_uid,
                                details=detail,
                                loss_scope=LOSS_SCOPE_SIGNAL)

                    # Annotations whose references the group discard
                    # left dangling (#177). Dropping them without a row
                    # would re-create the silent truncation #36 closed,
                    # one element over; the WFDB bridge already reports
                    # its equivalent drop (#159), and the two paths must
                    # not differ in whether the user is told.
                    #
                    # Scoped STANDARD, not SIGNAL: an annotation is a
                    # mark *about* the signal, and the acquired-samples
                    # loss it described already costs the run its PASS
                    # via the SIGNAL row above. Grading this row too
                    # would double-charge one loss under two entries.
                    ann_dropped = meta.get('dropped_annotations', 0)
                    ann_rewritten = meta.get('rewritten_annotations', 0)
                    if ann_dropped or ann_rewritten:
                        # One row per instance, not one per mark: a cart
                        # that marks forty beats on a discarded group
                        # must not fill section 3 of the report with
                        # forty near-identical lines. The count is of
                        # annotations and the list is of distinct
                        # groups, because they answer different
                        # questions. No pipes in the prose -- the report
                        # renders this into one markdown table cell.
                        ordinals = meta.get('dropped_annotation_groups', [])
                        group_ref = (
                            f"multiplex "
                            f"{'group' if len(ordinals) == 1 else 'groups'} "
                            f"{', '.join(str(g) for g in ordinals)}")
                        parts = []
                        if ann_dropped:
                            parts.append(
                                f"Dropped {ann_dropped} waveform "
                                f"{'annotation' if ann_dropped == 1 else 'annotations'}"
                                f" whose only references named discarded "
                                f"{group_ref}")
                        if ann_rewritten:
                            parts.append(
                                f"removed references to discarded "
                                f"{group_ref} from {ann_rewritten} "
                                f"{'annotation' if ann_rewritten == 1 else 'annotations'}"
                                f" that also name the kept group")
                        detail = (
                            f"{'; '.join(parts)}. Only Waveform Sequence "
                            f"item 0 is kept (#36); a reference to a "
                            f"discarded item would name an item the "
                            f"exported file does not carry, and ordinals "
                            f"are positional so the survivors are never "
                            f"renumbered (#177).")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="DATA_LOSS",
                                entity_uid=inst.sop_instance_uid,
                                details=detail,
                                loss_scope=LOSS_SCOPE_STANDARD)

                    # Persist nested pixel payloads to the sidecar (#183).
                    #
                    # Same shape as the waveform block below, and its
                    # comment about calling `record_blob_ref` without
                    # `conn=` applies unchanged: this loop runs outside any
                    # open SqliteStore transaction.
                    #
                    # Written here, once, at ingest. Nothing in the pipeline
                    # mutates an icon -- remediation edits `attributes`,
                    # redaction touches the top-level array, anonymize
                    # touches neither -- so `save_all` re-emits the *row*
                    # (which is what makes the reference follow a
                    # `regenerate_uid()`) and never re-appends the frame.
                    nested_tables = {
                        (t_path, t_tag): (t_vr, t_counted, t_kind)
                        for t_path, t_tag, t_vr, t_counted, t_kind
                        in meta.get('nested_offset_table', ())}
                    nested_high = {
                        (h_path, h_tag): (h_vr, h_facts)
                        for h_path, h_tag, h_vr, h_facts
                        in meta.get('nested_high_bit', ())}
                    nested_precision = {
                        (p_path, p_tag): (p_vr, p_facts)
                        for p_path, p_tag, p_vr, p_facts
                        in meta.get('nested_precision', ())}
                    for n_path, n_tag, n_vr, n_raw, n_hash in meta.get(
                            'nested_pixels', ()):
                        if not sidecar_manager:
                            # No sidecar to write to, so the bytes are not
                            # carried after all and the loss row is owed.
                            # Above the reporting loop below, deliberately:
                            # that loop is what files it, and this is the
                            # last point at which the answer can still
                            # change. Reached by the callers that pass a
                            # bare DicomStore and no sidecar.
                            meta.setdefault(
                                'dropped_private_binary', []).append(
                                    (n_tag, n_vr))
                            continue
                        # The frames `_decode_nested_pixels` dropped because
                        # the item's offset table named more than it
                        # declares (#433). Here, below the no-sidecar
                        # branch, so only an icon that is actually carried
                        # claims "kept the first N". STANDARD, not the
                        # top level's SIGNAL: an icon is a derived
                        # thumbnail, every other icon loss is STANDARD, and
                        # truncating one must not grade worse than losing
                        # it whole.
                        n_table = nested_tables.get((n_path, n_tag))
                        if n_table is not None:
                            t_vr, t_counted, _t_kind = n_table
                            detail = (
                                f"{_nested_row_prefix(n_tag, t_vr, n_path)}"
                                f"{frame_count_mismatch_words(t_counted)}. "
                                f"Kept the first {t_counted[1]} and "
                                f"discarded {t_counted[0] - t_counted[1]}.")
                            logger.warning(f"{inst.sop_instance_uid}: {detail}")
                            if store_backend is not None:
                                store_backend.log_audit(
                                    action_type="DATA_LOSS",
                                    entity_uid=inst.sop_instance_uid,
                                    details=detail,
                                    loss_scope=LOSS_SCOPE_STANDARD)
                        # The top level's HighBit row, for a carried icon
                        # (#598). Below the no-sidecar branch for the
                        # offset-table row's reason: only an icon that is
                        # carried has a decode to describe. The words are
                        # the top level's, tail included, and it is true
                        # here because `_write_back_nested_pixels` writes an
                        # icon's HighBit as BitsStored - 1 too.
                        n_high = nested_high.get((n_path, n_tag))
                        if n_high is not None:
                            h_vr, h_facts = n_high
                            _record_high_bit(
                                inst.sop_instance_uid,
                                f"{_nested_row_prefix(n_tag, h_vr, n_path)}"
                                f"{_high_bit_words(h_facts)}")
                        # And the precision row (#622), on the same terms:
                        # true here because `_write_back_nested_pixels`
                        # writes an icon's BitsStored from its samples.
                        n_wider = nested_precision.get((n_path, n_tag))
                        if n_wider is not None:
                            w_vr, w_facts = n_wider
                            _record_precision(
                                inst.sop_instance_uid,
                                f"{_nested_row_prefix(n_tag, w_vr, n_path)}"
                                f"{_precision_words(w_facts)}")
                        kind = serialize_blob_kind('pixels', n_path, n_tag)
                        # Site 2 of six (#368): append and row commit under
                        # one hold, per icon, for the reason at site 1.
                        with gate():
                            n_off, n_len = sidecar_manager.write_frame(
                                n_raw, 'zlib')
                            if store_backend is not None:
                                store_backend.record_blob_ref(
                                    inst.sop_instance_uid, kind, n_off, n_len,
                                    n_hash, 'zlib')
                        # The provenance geometry, captured from the item
                        # these bytes came out of. `_write_back_nested_
                        # pixels` compares it against whatever sits at this
                        # path when the export resolves it; see
                        # `NestedPixelRef`.
                        n_item = resolve_item_path(inst, n_path)
                        inst._nested_pixel_refs[(n_path, n_tag)] = \
                            NestedPixelRef(
                                sidecar_manager.filepath, n_off, n_len,
                                'zlib', n_hash,
                                nested_item_geometry(n_item.attributes)
                                if n_item is not None else None)

                    # Private binary elements never reached the graph, so
                    # `remove_private_tags=False` could not have kept
                    # them. Same reasoning as the block above: a loss the
                    # caller cannot see is indistinguishable from a file
                    # that never carried the tag (#125).
                    #
                    # The key still says `private` because #125 found it
                    # there; since #137 the list also carries standard
                    # elements, which is why the message is chosen per
                    # tag. Saying "Private tag 6000,3000" on a row the
                    # report scopes STANDARD invites the reader to
                    # distrust whichever half they check second.
                    # A nested item whose offset table names fewer frames
                    # than it declares (#433): not carried, and not in
                    # `dropped_private_binary` either, so it gets this
                    # row and only this one -- the generic one below
                    # would call it "unrouted", which is not why.
                    for t_path, t_tag, t_vr, t_counted, t_kind in meta.get(
                            'nested_offset_table', ()):
                        if t_kind != "fewer":
                            continue
                        detail = (
                            f"Standard tag {t_tag} ({t_vr}) at "
                            f"{_item_path_words(t_path)} was not ingested: "
                            f"{frame_count_mismatch_words(t_counted)}, so "
                            f"there is no frame to keep for the ones the "
                            f"table does not name, and it is not in the "
                            f"exported file.")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="DATA_LOSS",
                                entity_uid=inst.sop_instance_uid,
                                details=detail,
                                loss_scope=LOSS_SCOPE_STANDARD)

                    for tag, vr in meta.get('dropped_private_binary', ()):
                        scope = loss_scope_for_tag(tag)
                        # Two reason clauses, because two rules drop
                        # (#151): group 7fe0 is excluded wholesale
                        # (unrouted pixel elements -- an icon's nested
                        # Pixel Data, a float element beside real Pixel
                        # Data), while everything else is dropped only
                        # for exceeding the retention threshold. One
                        # sentence covering both would be false for one
                        # of them, which is how #194's wrong-reason row
                        # happened.
                        if tag.startswith("7fe0"):
                            reason = ("unrouted pixel elements are not "
                                      "held in the object graph")
                        else:
                            reason = (f"its value exceeds the "
                                      f"{BINARY_RETENTION_MAX_BYTES}-byte "
                                      f"retention threshold, so it is not "
                                      f"held in the object graph")
                        if scope == LOSS_SCOPE_PRIVATE:
                            detail = (f"Private tag {tag} ({vr}) was not "
                                      f"ingested; {reason}, and it cannot "
                                      f"be exported even with "
                                      f"remove_private_tags=False.")
                        else:
                            detail = (f"Standard tag {tag} ({vr}) was not "
                                      f"ingested; {reason}, so it is "
                                      f"not in the exported file.")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="DATA_LOSS",
                                entity_uid=inst.sop_instance_uid,
                                details=detail,
                                loss_scope=scope)

                    # Retained, not lost -- and that is exactly why this
                    # is not a DATA_LOSS row. The bytes are whole in the
                    # object graph; what is missing is any assurance
                    # about what is inside them. Section 3.1 of the
                    # compliance report is headed "present in the source
                    # and not in the exported data", so filing this
                    # there would make that header false (#167).
                    #
                    # This row says what ingest knows and stops there.
                    # It used to end "was retained verbatim and
                    # exported", which ingest cannot know: the default
                    # `remove_private_tags=True` deletes the element
                    # during `anonymize()`, and the report then carried
                    # a REMEDIATION_REMOVE row in section 2 and the
                    # claim that the same bytes shipped in section 3.2.
                    # `generate_report` resolves that against the graph;
                    # `element_tag` is what it resolves (#167).
                    #
                    # No `loss_scope`: the column grades losses, and
                    # this is not one.
                    for tag, nbytes in meta.get(
                            'unscanned_private_sequences', ()):
                        detail = (f"Private tag {tag} holds {nbytes} bytes "
                                  f"that begin with the item tag "
                                  f"(FFFE,E000) but do not parse as an "
                                  f"implicit-VR sequence. It was ingested "
                                  f"verbatim; the PHI scan could not open "
                                  f"it.")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="SCAN_GAP",
                                entity_uid=inst.sop_instance_uid,
                                details=detail,
                                element_tag=tag)

                    # Persist Waveform Samples to Sidecar
                    #
                    # Site 3 of six (#368): append and row commit under one
                    # hold, for the reason at site 1.
                    if w_bytes and sidecar_manager:
                        with gate():
                            w_off, w_len = sidecar_manager.write_frame(
                                w_bytes, 'zlib')
                            # Unlike pixels, waveform offsets have no
                            # column on `instances`, so the blob table is
                            # their only record. Skipping this makes
                            # compaction reclaim them.
                            #
                            # Called without `conn=`: this loop runs
                            # outside any open SqliteStore transaction, so
                            # record_blob_ref is free to open (and commit)
                            # its own connection here -- under the gate,
                            # which is the one lock held across a sqlite
                            # write on purpose.
                            if store_backend is not None:
                                store_backend.record_blob_ref(
                                    inst.sop_instance_uid, 'waveform',
                                    w_off, w_len, w_hash, 'zlib')
                        inst._waveform_hash = w_hash
                        inst._waveform_loader = SidecarWaveformLoader(
                            sidecar_manager.filepath, w_off, w_len, 'zlib',
                            instance=inst, waveform_hash=w_hash)

                    # Linkage Logic
                    pid = meta['pid']
                    sid = meta['sid']
                    ser_id = meta['ser_id']

                    # Patient
                    pat = patient_map.get(pid)
                    if not pat:
                        pat = Patient(pid, meta['pname'])
                        store.patients.append(pat)
                        patient_map[pid] = pat

                    # Study
                    study = study_map.get(sid)
                    if not study:
                        # A date we cannot read is a date we do not have.
                        # Substituting one here is indistinguishable
                        # downstream from a date that was recorded (#60).
                        sdate = None
                        if meta['sdate']:
                            try:
                                sdate = datetime.strptime(
                                    meta['sdate'], "%Y%m%d").date()
                            except (ValueError, TypeError):
                                logger.warning(
                                    f"Study {sid} has an unreadable Study "
                                    f"Date ({meta['sdate']!r}); it will be "
                                    "treated as absent rather than guessed.")

                        study = Study(sid, sdate)
                        pat.studies.append(study)
                        study_map[sid] = study

                    # Series
                    series = series_map.get(ser_id)
                    if not series:
                        series = Series(ser_id, meta['modality'], meta['series_num'])
                        # The "is this equipment?" rule lives on
                        # `Equipment.from_parts`, shared with both store
                        # hydration routes and the builder (#290, #282).
                        series.equipment = Equipment.from_parts(
                            meta['man'], meta['model'], meta['dev_sn'])
                        study.series.append(series)
                        series_map[ser_id] = series

                    # Instance
                    series.instances.append(inst)
                    # Only once linked: a result whose linkage raised
                    # above is a failure, and must not hold the UID
                    # against a later file that could have been kept.
                    held[inst.sop_instance_uid] = inst
                    count += 1
                except Exception as e:
                    # A parent-side failure is the same failure to the
                    # caller as a worker-side one: the file is not in the
                    # store. It takes the same route (#211).
                    _record_failure(inst.file_path or '<unknown>',
                                    f"Linkage Failed: {describe_exception(e)}")

        logger.info(f"Successfully ingested {count} instances.")
        if failures:
            logger.warning(
                f"Rejected {len(failures)} file(s) at ingest; each has an "
                "ERROR audit row naming the file and the reason.")
        if declined_superseded:
            logger.warning(
                f"Declined {declined_superseded} file(s) whose SOP Instance "
                "UID is the pre-redaction identity of an instance already in "
                "this session; see the compliance report.")
        if declined_duplicate:
            logger.warning(
                f"Declined {declined_duplicate} file(s) whose SOP Instance "
                "UID an instance in this session already holds; each has a "
                "WARNING audit row naming both files.")

        return IngestSummary(
            ingested=count, failures=failures,
            declined=declined_superseded + declined_duplicate,
            skipped=skipped_count)


#: The refusal every door raises for a `compression` it cannot write
#: (#605). One constant because the CHANGELOG quotes it.
_COMPRESSION_REFUSAL = (
    "compression must be None (Implicit VR Little Endian) or 'j2k' "
    "(JPEG 2000 Lossless); got {!r}")


def _compresses(compression) -> bool:
    """Does this `compression` encode the pixels? The one spelling (#605).

    True for `"j2k"`, False for None, and `ValueError` for anything else.
    Every reader of `compression` asks this -- `ExportContext`'s
    construction, `write_tree`'s entry, the export worker and
    `_finalize_dataset` -- because the worker once asked three ways: two
    sites compared `== "j2k"` and the integer arm tested truthiness, so
    `"rle"` or `"J2K"` skipped both the raw write and the encoder and
    delivered an image with no Pixel Data as `ok`. A refusal rather than
    a native fallback: a caller who typed a codec name asked for a file
    this exporter does not write, and quietly writing a different one is
    the #605 outcome with its pixels put back. `""`, `False` and `0` are
    refused too; they meant "native" only by accident of the truthiness
    test.
    """
    if compression is None:
        return False
    if isinstance(compression, str) and compression == "j2k":
        return True
    raise ValueError(_COMPRESSION_REFUSAL.format(compression))


@dataclass
class ExportContext:
    """One instance to write, and how. Validates `compression` on
    construction (#605); the worker asks again, because a dataclass can
    be edited after this runs."""
    instance: Instance
    output_path: str
    patient_attributes: Dict[str, Any]
    study_attributes: Dict[str, Any]
    series_attributes: Dict[str, Any]
    pixel_array: Optional[Any] = None  # Numpy array or None
    compression: Optional[str] = None  # 'j2k' or None
    # Zero-Copy Sidecar Support
    sidecar_path: Optional[str] = None
    pixel_offset: Optional[int] = None
    pixel_length: Optional[int] = None
    pixel_alg: Optional[str] = None
    redaction_zones: List[Tuple] = field(default_factory=list)
    #: Drop every nested icon that is **not** the carrier's own depth-1
    #: Icon Image Sequence item (#183, #542). Store-wide, because such an
    #: icon may thumbnail a different, redacted SOP instance whose UID
    #: redaction regenerated (#183): computed ONCE per export run by the
    #: context builder -- see `redaction_in_effect` for why a per-instance
    #: condition fails open for it. The carrier's own icon is gated per
    #: instance in the worker instead (`_is_own_icon_path`). Both context
    #: builders set it; a builder that forgets ships thumbnails of
    #: redacted frames.
    drop_foreign_icons: bool = False
    #: Re-read the written file, compare its descriptors and decode and
    #: compare every sample before delivering it (#209, #449). Off by
    #: default: it costs a second parse and a full decode per instance
    #: (#449). Carried here because the check runs in the worker -- the
    #: file is local to it and the cost parallelizes.
    verify_readback: bool = False

    def __post_init__(self):
        _compresses(self.compression)


@dataclass
class ExportOutcome:
    """What one worker has to tell the parent about one instance (#126).

    The worker used to answer `True` or the exception, which is enough to
    count successes and no help at all for a *partial* success: a file
    that was written and is missing something the caller asked for. Data
    loss is neither an error nor nothing, so it needs its own field.

    `error` lives here rather than being returned bare so the worker has
    one return shape. Call sites still have to filter for `Exception`,
    because both export dispatches pass `yield_exceptions=True` and so
    receive a lost worker as a value (#232) -- but a site that forgets no
    longer gets an `AttributeError` on the failure path, which is the
    path that only runs when something has already gone wrong.
    """
    ok: bool
    output_path: str
    sop_instance_uid: Optional[str] = None
    #: `(scope, detail)` per lost element, where scope is one of
    #: `LOSS_SCOPE_PRIVATE` / `LOSS_SCOPE_STANDARD`. The scope travels
    #: with the message rather than being worked out by the parent
    #: because only the worker still has the tag; by the time
    #: `_report_export_losses` sees this, the tag is prose (#146).
    losses: List[Tuple[str, str]] = field(default_factory=list)
    #: One sentence per descriptor the worker corrected on the way out,
    #: for the parent to log at INFO (#468): today, a declared BitsStored
    #: the values do not fit. Carried here rather than logged by the
    #: worker, because the worker is usually a spawned process whose
    #: `isocenter` logger has no handler -- `session.export()` always
    #: spawns them, and `write_tree()` does by default on a GIL build --
    #: so a line logged there reached no one (0 of 3, measured in the
    #: review of #506). Not a loss: nothing was dropped and the file is
    #: correct, so it takes no audit row and does not move the grade.
    corrections: List[str] = field(default_factory=list)
    #: One sentence per claim in the source's own header that the file
    #: just written could not honour, for the parent to log at WARNING
    #: *and audit* (#502): today, a Photometric Interpretation the
    #: written transfer syntax does not admit. The other half of the
    #: write-path ruling's pair, and the distinction is not cosmetic --
    #: `corrections` is a fact about this library (an exact, value-
    #: preserving rewrite of our own making: INFO, no row, grade
    #: unchanged), and a warning is a fact about the user's data (a
    #: `WARNING` row, which reaches the compliance report's Exceptions &
    #: Errors section and grades the run REVIEW_REQUIRED, #411/#479).
    #: Collapsing them would make a gap in what this library can write
    #: read as a defect in the user's dataset, and a malformed source
    #: read as a shortcoming of ours.
    warnings: List[str] = field(default_factory=list)
    error: Optional[BaseException] = None


@dataclass
class ExportSummary:
    """What a batch delivered, for the parent that has to report it.

    `export_batch` used to return a bare success count, and that count
    was the only thing to survive the batch: the failures -- their UIDs
    and their exceptions -- were dropped inside it. So an instance that
    never reached disk produced no audit row, the compliance report
    graded a run in which every write failed exactly as it grades a
    clean one, and the recoverable-identity disclosure had to be written
    from the export *plan* because there was no delivered set to write
    it from (#181, #187).

    The delivered instances are kept as UIDs rather than as a number
    because the number is derivable from them and the identities are
    not: the disclosure has to say which files went out, not how many
    were meant to.
    """
    #: SOP Instance UID per instance that reached disk, or its output path
    #: when it carries no UID: ingest refuses such a file, but a hand-built
    #: graph through `write_tree()` can carry one, written as `None.dcm` (#613).
    written_uids: List[str] = field(default_factory=list)
    #: `(entity_uid, details)` per instance that did not reach disk,
    #: already audited by `_report_export_failures`.
    failures: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def written(self) -> int:
        """How many files reached disk.

        Counted over the de-duplicated UIDs, not the outcomes: the UID
        names the output file, so two instances sharing one are two
        successful write operations and *one* file -- the second
        overwrote the first. `len(self.written_uids)` here made the
        report describe that overwrite as two delivered files (#197).
        """
        return len(set(self.written_uids))

    @property
    def failed(self) -> int:
        """How many instances did not."""
        return len(self.failures)


class ExportError(RuntimeError):
    """The export delivered nothing (#191).

    Raised by `session.export()` when **zero of N planned** instances
    reached disk and at least one failed, in both formats: the DICOM path
    since #191, and `WfdbExporter.export` since #541, where N is the
    waveform records attempted. The message says "instances" in both:
    a WFDB record is written from one waveform instance, and its failure
    is named by that instance's UID, as the DICOM one is. Not on a
    partial export: two
    files out of three is a real, usable result, and raising would
    discard the summary that says which two and would have to decide the
    fate of files already written.

    Raised **last**, after every record the run produces, exactly as
    `_apply_redaction_rules` raises `RedactionError`, and for the same
    reason: a caller who catches this still holds a correct graph, a
    complete audit trail and a compliance report grading
    `REVIEW_REQUIRED`. What those records are differs by format. The
    DICOM path raises after the collision report, the
    recoverable-identity disclosure, the delivery counters, the `EXPORT`
    audit row and `Done.`. The WFDB path has none of the first three; it
    raises after each record's `ERROR` or `DATA_LOSS` row and its own
    `EXPORT` row.

    **`RuntimeError`, not `Exception`.** `write_tree` and
    `_export_instance_worker` already raise bare `RuntimeError`s on this
    same pipeline, so subclassing keeps every existing
    `except RuntimeError` catching this one, where subclassing
    `Exception` directly would turn a caught error into an escaping one.
    Raising bare would instead throw away the failure list #181 computed,
    which is the whole of what a caller can act on.

    `write_tree`'s own raise is deliberately left as a bare
    `RuntimeError`: the two describe different behaviours -- "this
    serializer could not write" and "the pipeline delivered nothing" --
    and changing it would be a second API-shape change riding on this
    one.
    """

    def __init__(self, failures, attempted, folder=None):
        self.failures = list(failures)   # [(entity_uid, details)]
        self.attempted = attempted
        first = self.failures[0] if self.failures else ("UNKNOWN", "unknown")
        where = f" to {folder}" if folder else ""
        super().__init__(
            f"Export{where} wrote 0 of {attempted} planned instances; "
            f"{len(self.failures)} failed and nothing reached disk. "
            f"First: {first[0]}: {first[1]}. See the audit log for the "
            "rest.")


def _write_pixel_geometry(ds, geom, attributes, *, float_element: bool,
                          syntax_uid: str, warnings=None) -> None:
    """Write the descriptors that describe the pixel element just written.

    Both pixel branches call this, so `Rows`, `Columns`,
    `SamplesPerPixel` and `NumberOfFrames` agree with the bytes by
    construction rather than by review. They are Type 1 in the Image
    Pixel Module and Type 1 again in the Floating Point and Double
    Floating Point Image Pixel Modules (PS3.3 C.7.6.24, C.7.6.25), so
    "the float element does not need them" was never true -- and worse,
    `_merge` had already written whatever `attributes` declared, so an
    instance whose descriptors were stale exported a file describing a
    different image (#216).

    Every descriptor here comes from one resolved geometry. Rows and
    Columns used to be recomputed from the array's shape while
    SamplesPerPixel was read straight out of `attributes`, and it is that
    *incoherence* -- not the wrong axis on its own -- that turned #186
    into a file pydicom refuses to decode: Rows=3, Columns=4 beside
    SamplesPerPixel=4. Writing all four from `geom` makes them agree by
    construction.

    `BitsAllocated` is deliberately **not** written here. Each branch
    keeps its own: the float arms set 32 or 64 because those are the
    Enumerated Values the two float modules require next to the tag they
    chose, and the integer arm derives `arr.itemsize * 8` from the bytes
    it is about to emit. They happen to agree numerically; they are not
    the same statement.

    Args:
        ds (Dataset): The dataset being written.
        geom (PixelGeometry): The one resolved geometry.
        attributes (dict): The instance's attributes, read only to decide
            whether a frame count was declared and for the photometric
            fallback -- the worker's copy with (0028,0004) spelled as a
            Code String (`_label_as_written`, #532), so the fallback
            cannot write a raw spelling back over the corrected one.
        float_element (bool): Whether the pixel element just written was
            (7fe0,0008) or (7fe0,0009). Keyword-only and **without a
            default**, so a third call site has to decide which module's
            Photometric Interpretation rules apply rather than inheriting
            the integer path's by omission (#222).
        syntax_uid (str): The transfer syntax UID the file is being
            written under, which the label is judged against (#502).
            Keyword-only and without a default, for `float_element`'s
            reason: a call site that does not say what it is writing
            cannot have its label judged, and inheriting "uncompressed"
            by omission would warn about every compressed export.
        warnings (list): Where a sentence goes for a label the syntax
            does not admit, for the parent to log and audit. `None`
            means the caller does not collect them.

    Raises:
        _PhotometricRefusal: if the *file* would carry more than one
            Photometric Interpretation (#502) -- judged on `ds` after the
            corrections below, not on the declaration, so a
            multi-valued declaration the resolver has already answered
            (at one sample it answers MONOCHROME2) still exports.
    """
    ds.Rows = geom.rows
    ds.Columns = geom.cols
    ds.SamplesPerPixel = geom.samples
    # The literal, not `pixel_geometry.TAG_NUMBER_OF_FRAMES`: this module
    # imports no tag constants and already spells every tag this way, and
    # a second spelling of a tag one file writes one way is how the two
    # answers start to disagree. Tags are lowercase-hex strings.
    if geom.frames > 1 or "0028,0008" in attributes:
        ds.NumberOfFrames = geom.frames

    # Photometric Interpretation is not derivable from an array --
    # three samples are equally RGB, YBR_FULL or YBR_RCT -- so only
    # an outright contradiction is corrected. None means the
    # declared value is coherent and `_merge` already put it on
    # `ds`; overwriting it is what relabelled every YBR instance.
    # This is *not* an `or`: the None arm is what lets YBR_FULL,
    # YBR_ICT and MONOCHROME1 survive a round trip.
    photometric = resolve_photometric_interpretation(attributes, geom.samples)

    # No float-only branch here any more, and that absence is
    # load-bearing. A `float_element` call arrives only from the worker
    # arm that just refused `geom.samples > 1` (#222), so on the float
    # path the resolver runs at `samples == 1` and can only answer None
    # or MONOCHROME2 -- both conformant under C.7.6.24/C.7.6.25. The
    # RGB pass-through guard that stood here (#224's narrow fix) is
    # unreachable in that world and came out with the refusal; do not
    # reintroduce a float correction here without re-reading #222's
    # closing decision, and note that a third call site passing
    # `float_element=True` without the worker's refusal upstream would
    # be back to writing `RGB` onto a float element.
    if photometric is None:
        photometric = attributes.get("0028,0004")
    # `YBR_FULL_422` names a layout -- two chroma samples per two
    # pixels, four bytes per pair -- that the array being written cannot
    # hold: it is `(rows, cols, 3)`, three full-resolution samples per
    # pixel, and `tobytes()` of that is a third longer than the label
    # promises. pydicom refuses the native file outright (`a third
    # larger than expected (192 vs 128 bytes)`) and decodes the JPEG 2000
    # one beneath a label the codestream contradicts (#470). So the
    # sampling half of the label is corrected, to `YBR_FULL`, which
    # names exactly these bytes; the colour-space half, YBR, is the
    # declaration's and is kept, because three samples are equally RGB
    # or YBR and a relabel to RGB would be a label without its bytes
    # (#372, #448, #482). Here and not in
    # `resolve_photometric_interpretation`, for the reason
    # `PlanarConfiguration` is written 0 here and not there: that
    # function answers what the graph should carry and is shared with
    # `set_pixel_data()`, which writes no file; this one describes the
    # element just written. Only ever reached at three or more samples:
    # at one the resolver has already answered MONOCHROME2, and two is a
    # shape no interpretation names, written as declared.
    if (geom.samples >= 3 and photometric
            and str(photometric).strip().upper() == "YBR_FULL_422"):
        photometric = "YBR_FULL"
    if photometric:
        ds.PhotometricInterpretation = photometric

    # **The label is held against the syntax being written (#502), and
    # this is the only place a label reaches a file *that has a pixel
    # element*** -- both pixel-writing worker arms, integer and float,
    # come through here, so neither can bypass a check placed here the
    # way a check inside the integer arm would be bypassed by the float
    # path.
    #
    # **The third arm does not come through here, and is judged
    # elsewhere (#534).** An instance with no pixel element never calls
    # this function, and `_merge` has already put whatever `0028,0004`
    # the graph declared onto `ds`. Until #534 that file carried the label
    # unexamined -- measured, an SR-shaped instance declaring `YBR_ICT`
    # exported `ok=True` with no warning. The worker now judges that arm
    # itself, after `_finalize_dataset`, with `_pixel_less_label_warning`:
    # the same table, a remedy true of a file with no pixels, and the
    # syntax read from `file_meta` because a pixel-less file stays native
    # under compression. Do not move that judgement in here: this
    # function is the pixel arms' contract, and its refusal of a
    # multi-valued label is true only of a file with pixels.
    #
    # It runs *after* the corrections above rather than on
    # `attributes`, so it judges what the file will carry: a declared
    # `YBR_FULL_422` has already become `YBR_FULL` and a `PALETTE
    # COLOR` at three samples has already become `RGB`, and warning
    # about either would name a label the file does not hold.
    #
    # One relabel is still ahead of this point and cannot be pulled
    # behind it: under JPEG 2000, `_compress_j2k` turns an `RGB` source
    # into `YBR_RCT` (#516 case 1), and it runs after the whole
    # geometry write. That is harmless *because both labels are on the
    # J2K row* -- the judgement does not change -- and it is why the
    # readback, not this function, is the reader that sees a compressed
    # file's final label (#507). A future encoder relabel to something
    # off that row would make this check answer about a label the file
    # no longer carries.
    #
    # Two outcomes, and which one applies is the whole of the
    # write-path ruling. A label the syntax does not admit is
    # **written as declared** and handed back on `warnings`: there are
    # formats this library can read and cannot write, and a
    # de-identified copy plus a row saying what could not be honoured
    # beats no copy at all. Relabelling is the thing that is not done
    # -- `RGB` over samples that were never inverse-transformed, or
    # `YBR_FULL` over samples PS3.3 C.7.6.3.1.2 gives a narrower range,
    # would be a new false claim rather than the source's own repeated.
    # A **multi-valued** label is refused, because no value can be
    # chosen without inventing one and the file could not be read back
    # (see `_PhotometricRefusal`).
    # Both halves read `ds`, never `attributes`, and for the refusal
    # that is not tidiness: at one sample the resolver has already
    # answered `MONOCHROME2`, so an instance whose *declaration* is
    # multi-valued still writes a single-valued, re-ingestible file.
    # Refusing it on the declaration would deny the caller an output
    # this library can read back, which is the one thing the ruling's
    # exception is not for.
    # `(list, tuple, MultiValue)` is this file's spelling and all three
    # arrive: pydicom hands back a `MultiValue` for an element it holds,
    # the store hands back a `list`, and `MultiValue` is a
    # `MutableSequence` and *not* a `list` (measured), so the two-name
    # form silently never fires. See `_fallback_multivalue`.
    written = ds.get("PhotometricInterpretation")
    if isinstance(written, (list, tuple, MultiValue)) and len(written) > 1:
        raise _multi_valued_refusal(written)
    if warnings is not None:
        warning = _photometric_warning(
            _written_photometric(ds.get("PhotometricInterpretation")),
            syntax_uid, has_pixels=True)
        if warning is not None:
            warnings.append(warning)

    # Planar Configuration describes the pixel element *just written*,
    # and that element is interleaved -- isocenter holds and stores
    # pixels interleaved, always (see `SidecarPixelLoader.__call__` for
    # the measurement). So this is not "write a default when none was
    # declared": a declared 1 is a claim about the source file, `_merge`
    # has already stamped it onto `ds` by the time this runs, and
    # leaving it there labelled interleaved bytes as planar. That is a
    # corrupt exported DICOM file with no error, no warning and no
    # DATA_LOSS row, and `_READBACK_DESCRIPTORS` does not include this
    # element, so before #449 `verify_readback=True` did not see it
    # either (#210); the pixel decode now does (a PC 1 label over
    # interleaved bytes decodes to different samples).
    #
    # The `samples < 3` half of `planar_configuration_default` is kept
    # deliberately, spelled out here: an unconditional write would add
    # (0028,0006) to every monochrome export, an element those files
    # must not carry. `planar_configuration_default` itself is
    # unchanged and still has its other caller, `set_pixel_data()`,
    # where #217's reasoning -- do not overwrite a declared value --
    # still holds, because that path writes no file.
    if geom.samples >= 3:
        ds.PlanarConfiguration = 0


def _stored_width(arr: np.ndarray, attributes) -> Tuple[int, Optional[str]]:
    """BitsStored for the integer pixel element written from `arr`.

    Returns `(bits_stored, why)`. `why` is None when the declaration was
    kept or there was none, and otherwise says what about the declaration
    the bytes could not honour, for the worker to hand back to the parent
    on `ExportOutcome.corrections` (#468). HighBit is not returned: the
    worker writes BitsStored - 1 beside whatever this answers.

    The rule: **a declared width is written when every sample fits it,
    and the array's own width is written otherwise.** Held against the
    array because BitsStored is a claim about the samples beside it that
    every conformant reader acts on -- a native sample is masked to
    BitsStored on read, so -3024 under BitsStored 12 reads back as 1072,
    and a JPEG 2000 codestream carries the value beneath a header that
    says it cannot. Before this, the worker wrote 8 or 16 when nothing
    was declared, so `int32` left under a 16-bit claim (27734 read back
    where -1103401898 was written), and wrote a declaration as it stood.
    `verify_readback=True` failed both after the fact (#449); the default
    export wrote both in silence.

    The width the values fit is `BitsAllocated`, `itemsize * 8`, and not
    the narrowest width that would hold them: the narrowest is a
    property of this frame's content and would put a different
    BitsStored on each slice of a series. A declaration the values fit
    is kept for the same reason -- 12 on a CT is the acquisition's claim,
    and the bytes cannot say it is wrong. What that still allows, stated:
    a series in which only some slices overflow their declaration comes
    out with mixed BitsStored, the declared width on the slices that fit
    and the array's on the ones that did not.

    The range is the array's **own** signedness (`dtype.kind`), not
    declared PixelRepresentation: the bytes are the array's, and a
    declared representation that disagrees with them is a different
    defect from this one. `bool` is written one byte per sample and
    ranges as `uint8`.

    Skipped when the declaration equals the array's width: nothing can
    overflow it, and the min/max pass is the one cost here (measured
    4 ms on a 52 MB int16 stack, beside 4 ms for the `tobytes()`).
    A declaration above the width, or below 1, is caught before any
    range is built: pydicom refuses to decode `BitsStored 17` over
    16-bit bytes at all, and `1 << -1` raises.

    The declaration is read by `declared_int`, the one reading of a
    declared descriptor that the geometry resolver and `set_pixel_data()`
    already use, so absent, `''`, unparseable and non-scalar all mean
    "not declared" here as they do there, and get the array's own width
    with nothing to report. That includes a list. `[12]` was exported as
    BitsStored 12 before #468, because pydicom unwraps a one-element list
    for a US element, and the first cut of this function raised
    `int() argument must be ... not 'list'` on it (review of #506). A
    one-element list is deliberately not unwrapped here: a second
    reading of the same descriptor, more lenient than the resolver's, is
    how two answers start to disagree.
    """
    allocated = arr.itemsize * 8
    declared = declared_int(attributes, "0028,0101")
    if declared is None:
        return allocated, None
    if not 1 <= declared <= allocated:
        return allocated, (
            f"BitsStored {declared} is not a width {arr.dtype} samples "
            f"can have (BitsAllocated {allocated})")
    if declared == allocated:
        return declared, None
    if arr.dtype.kind == "i":
        lo, hi = -(1 << (declared - 1)), (1 << (declared - 1)) - 1
        signed = "signed"
    else:
        lo, hi = 0, (1 << declared) - 1
        signed = "unsigned"
    seen_lo, seen_hi = int(arr.min()), int(arr.max())
    if lo <= seen_lo and seen_hi <= hi:
        return declared, None
    return allocated, (
        f"BitsStored {declared} cannot hold the pixel values: the array "
        f"holds {seen_lo}..{seen_hi} where {signed} {declared}-bit samples "
        f"span {lo}..{hi}")


def _width_notes(widened, declared_high_bit, bits_stored, high_bit) -> list:
    """The INFO notes for a BitsStored/HighBit written other than declared.

    One spelling for the top-level pixel element and an icon's (#598),
    each of which writes `_stored_width`'s BitsStored and HighBit =
    BitsStored - 1 and hands these back on `ExportOutcome.corrections`.

    `widened` is `_stored_width`'s reason, or None. A declared HighBit the
    file does not carry is said out loud too (#597). INFO, by ruling: the
    written file is conformant, the samples are unchanged, and an ingested
    file's declaration already has ingest's own row -- nothing in the
    graph marks which instances those are, so a WARNING here would write a
    second row per instance of every legacy cohort re-exported.
    `declared_high_bit` is read with `declared_int` for #506's reason, and
    gets no note after a widening, whose note already names the written
    HighBit. "Written with", because the BitsStored named may be one
    nobody declared.
    """
    if widened is not None:
        return [f"{widened}; written with BitsStored {bits_stored} "
                f"and HighBit {high_bit}, the array's own width"]
    if declared_high_bit is not None and declared_high_bit != high_bit:
        return [f"HighBit {declared_high_bit} was declared; written "
                f"with BitsStored {bits_stored} and HighBit "
                f"{high_bit}, because the samples are right-aligned "
                f"and PS3.5 8.1.1 puts their most significant bit at "
                f"BitsStored - 1. The samples are unchanged."]
    return []


#: The descriptors the readback compares first, by pydicom keyword. The
#: four geometry descriptors are the ones #186/#205 showed can describe
#: a different image than the pixels beside them; BitsAllocated is the
#: width #170/#216 showed being silently rewritten from the tag side.
#: This is the descriptor half; the pixels are decoded and compared
#: after it (#449), so a descriptor failure keeps its own message.
#:
#: **`PhotometricInterpretation` is deliberately not a sixth row, and
#: never was one (#507).** Every keyword here is compared against the
#: dataset the *worker serialized*, and the worker writes that label
#: onto that dataset itself -- so both sides would come from the same
#: assignment and agree by construction, whatever the writer got wrong.
#: A row here would be coverage that cannot fail, which reads in a diff
#: like a check and is worse than none. The label is judged instead
#: against the transfer syntax the file carries, which is not on `ds` at
#: all: see `_readback_label_mismatch`.
_READBACK_DESCRIPTORS = ("Rows", "Columns", "SamplesPerPixel",
                         "NumberOfFrames", "BitsAllocated")


def _bit_patterns(arr: np.ndarray) -> np.ndarray:
    """`arr` flattened and viewed as unsigned integers of its own width.

    The readback compares these rather than values, for floats above all:
    NaN is not equal to itself and -0.0 equals 0.0, so a value compare
    fails a correct file holding a NaN and passes one whose sign bit was
    lost (#449). `bool` is viewed as the `uint8` it is written as. No
    copy is made unless `arr` is not contiguous -- a `tobytes()` on each
    side would add two transient full copies of a large multi-frame.
    """
    flat = arr.reshape(-1)
    if flat.dtype == np.bool_:
        flat = flat.view(np.uint8)
    return flat.view(np.dtype(f"u{flat.itemsize}"))


def _readback_pixel_mismatch(decoded: np.ndarray, written: np.ndarray,
                             pixel_representation=None) -> Optional[str]:
    """Why `decoded` is not bit for bit `written`, or None if it is.

    `pixel_representation` is the file's own PixelRepresentation, named
    in the reason when the two disagree about signedness.
    """
    if written.dtype == np.bool_:
        written = written.view(np.uint8)
    # Size, not shape: the descriptors are already compared, and pydicom
    # squeezes a single frame and a single sample out of the shape.
    #
    # **The dtype comparison is not redundant with the bitwise compare
    # below.** int16 [-1, -2, -3] declared PixelRepresentation 0 reaches
    # the file with every bit intact, so the bit patterns agree -- and a
    # reader gets uint16 [65535, 65534, 65533]. Only the dtype says so.
    if decoded.dtype != written.dtype or decoded.size != written.size:
        reason = (f"the written pixel data decodes as {decoded.dtype} x "
                  f"{decoded.size} where {written.dtype} x {written.size} "
                  f"was written")
        kinds = {decoded.dtype.kind, written.dtype.kind}
        if kinds == {"i", "u"}:
            # Signed against unsigned: the element that decides it is
            # PixelRepresentation, so the reason points the reader there.
            reason += (
                f"; the file declares PixelRepresentation "
                f"{pixel_representation} "
                f"({'unsigned' if decoded.dtype.kind == 'u' else 'signed'}) "
                f"where "
                f"{'signed' if written.dtype.kind == 'i' else 'unsigned'} "
                f"samples were written")
        return reason
    differ = _bit_patterns(decoded) != _bit_patterns(written)
    count = int(np.count_nonzero(differ))
    if count == 0:
        return None
    first = int(np.argmax(differ))
    got = decoded.reshape(-1)[first].item()
    want = written.reshape(-1)[first].item()
    return (f"the written pixel data decodes to different samples: "
            f"{count} of {written.size} differ, first at flat index "
            f"{first} ({got!r} read back where {want!r} was written)")


def _readback_label_mismatch(readback) -> Optional[str]:
    """Why the delivered label is not one its syntax admits, or None (#507).

    Read entirely off `readback` -- the label from the dataset, the
    syntax from `file_meta` -- because the subject of this check is the
    *file*. Neither side can come from `ds` or from `ctx`: the syntax is
    not on `ds` at all, and under JPEG 2000 the label on `ds` is not the
    label in the file, since `_compress_j2k` relabels an `RGB` source
    `YBR_RCT` after the geometry was written (#516 case 1). This
    function is the only reader that sees a compressed file's final
    label.

    Three ways it declines to judge, each deliberate:

    - **No `PhotometricInterpretation`** -- an SR, a waveform-only
      instance, a float16 array whose arm writes no pixel element. There
      is no claim, and "absent" is not "inadmissible".
    - **A syntax with no row** in `_ADMISSIBLE_PHOTOMETRICS`. This
      library decodes eight transfer syntaxes it cannot write (#526), and
      the table holds measured rows only -- the same discipline
      `_FALLBACK_PHOTOMETRICS` keeps. A default of "admit nothing" would
      refuse hand-built files on a table nobody measured, and a default
      of `_PHOTOMETRIC_ANY_SYNTAX` would quietly assert that every
      unlisted syntax carries the uncompressed set.
    - **An admitted label**, normalized by `_written_photometric`,
      because `' rgb '` is what a conformant reader takes as `RGB`.

    What it is *not*: a colour-space check. Whether three samples are
    really YBR rather than RGB has no answer from bytes, and #372/#448/
    #482 is the standing ruling that this library does not make a claim
    the bytes cannot prove -- so `RGB` over YBR samples passes here, by
    design and not by omission. The check is structural: could a
    conformant reader take this label under this syntax at all.

    **It reads every file, whichever arm wrote it.** The writer judges
    the pixel arms in `_write_pixel_geometry` and the pixel-less arm in
    `_pixel_less_label_warning` (#534), and both write an inadmissible
    label as declared with a warning; this function reads the delivered
    file and refuses it, which is why the reason has to check for a pixel
    element before offering a remedy about the pixels. A hand-built file
    reaches it too.
    """
    if "PhotometricInterpretation" not in readback:
        return None
    label = readback.PhotometricInterpretation
    # Arity before normalization, because `_written_photometric` of a
    # `MultiValue` is the `str` of a list -- inadmissible under every
    # syntax, so the file would fail anyway, with a reason naming a
    # label no element carries.
    if isinstance(label, (list, tuple, MultiValue)) and len(label) > 1:
        # Not "this library cannot re-ingest it": measured, that is true
        # only of a file with pixel data, where `ingest()` refuses the
        # `MultiValue` while decompressing. A *pixel-less* file with two
        # labels re-ingests cleanly (`IngestSummary(ingested=1,
        # failures=[])`) and the graph comes back carrying both -- so the
        # fault named here is the arity itself, which is true of either.
        return (f"PhotometricInterpretation (0028,0004) is VM 1; the "
                f"written file reads back as {len(label)} values "
                f"({', '.join(repr(str(v)) for v in label)})")
    syntax = str(getattr(readback.file_meta, "TransferSyntaxUID", "") or "")
    normalized = _written_photometric(label)
    found = _label_inadmissibility(normalized, syntax)
    if found is None:
        return None
    clause, remedy = found
    if not any(kw in readback for kw in _PIXEL_ELEMENTS):
        remedy = _PHOTOMETRIC_NO_PIXELS
    return (f"PhotometricInterpretation reads back as '{normalized}', "
            f"which the transfer syntax the file was written under does "
            f"not admit ({syntax}): {clause} {remedy}")


def _readback_waveform_mismatch(readback, written: bytes) -> Optional[str]:
    """Why the file's `WaveformData` is not `written`, or None if it is.

    **One trailing `\\x00` on an odd-length value is not a difference.**
    `save_as` pads an odd-length OB/OW value to even length with one zero
    byte (measured: 6401 bytes written, 6402 read back), so "just compare
    the bytes" fails every correct odd-length waveform -- an 8-bit one,
    say. Exactly that pad is allowed, and only on an odd-length value: an
    even-length value is written as it is, so any tail there is a
    difference.
    """
    try:
        read = bytes(readback.WaveformSequence[0].WaveformData)
    except (AttributeError, IndexError):
        read = b""
    if read == written or (len(written) % 2 == 1
                           and read == written + b"\x00"):
        return None
    common = min(len(read), len(written))
    count = int(np.count_nonzero(
        np.frombuffer(read, np.uint8, common)
        != np.frombuffer(written, np.uint8, common)))
    count += abs(len(read) - len(written))
    return (f"the written WaveformData reads back as different bytes "
            f"({count} of {len(written)} differ)")


def _verify_readback(path: str, ds, written_pixels=None,
                     written_waveform=None) -> None:
    """Re-read a just-written file and hold it against what was meant.

    "The write did not raise" is a weaker claim than "a file exists
    that decodes to what we meant", and the compliance report presents
    the stronger one (#209). This is the opt-in check behind
    `export(verify_readback=True)`, and since #449 it checks the stronger
    claim itself. One `dcmread`, then four comparisons, in this order:

    1. **Descriptors** (`_READBACK_DESCRIPTORS`), against the dataset the
       worker serialized -- deliberately *not* against `inst.attributes`.
       The worker corrects stale declared descriptors on the way out
       (`_write_pixel_geometry` writes the resolved geometry, the integer
       branch derives `BitsAllocated` from `itemsize` -- #186, #216), so
       the raw attributes are the one baseline guaranteed to disagree
       with a correctly written file. The claim being verified is "the
       file says what the export meant", which is the claim `ok=True`
       makes to the report.
    2. **The Photometric Interpretation the file carries**, against the
       transfer syntax the file was written under
       (`_readback_label_mismatch`, #507). Before the decode, because a
       label the syntax does not admit has to be named as such rather
       than as whatever the decoder raises about the bytes underneath
       it.
    3. **Pixels**, when `written_pixels` is given: the array the pixel
       element was written from, after redaction and after geometry. The
       file is decoded through `_decode_pixels`, the door `ingest()`
       reads through -- pydicom, then the imagecodecs fallback -- so the
       check reads what a re-ingest would read. `Dataset.pixel_array`
       alone would fail every healthy 16-bit colour JPEG 2000 export,
       which pydicom with only Pillow cannot decode (#416), and
       `imagecodecs` alone would verify a reading nobody makes. It asks
       for the stored samples (`as_rgb=False`), not a colour conversion,
       because the samples are what was written. Every sample is then
       compared bit for bit (`_bit_patterns`). A native sample outside
       the declared BitsStored therefore fails: every conformant reader
       masks it, so the file does not hold what was meant (-3024 at
       BitsStored 12 reads back as 1072). A file labelled with a colour
       space pydicom converts (`_PYDICOM_CONVERTS`), over samples that
       are not unsigned 8-bit, is then decoded a second time the way
       `ingest()` decodes it, with the conversion, so a 16-bit or int8
       `YBR_FULL` file this library cannot ingest fails here too (#596).
       Unsigned 8-bit samples are the ones that conversion accepts, and
       are not decoded twice.
    4. **Waveform bytes**, when `written_waveform` is given: the file's
       `WaveformData` against the bytes written, allowing exactly the
       one pad byte `save_as` adds to an odd-length value
       (`_readback_waveform_mismatch`).

    `None` for either means "no such element was written" -- an SR, a
    float16 array (whose arm writes none), an image with no waveform --
    and the comparison is skipped, not attempted and caught: both
    decoders raise `AttributeError` on a file with no pixel element, and
    catching that would also pass a file whose pixel element vanished.
    Not compared: nested pixel payloads such as icons (#183), whose
    decoded arrays the worker does not hold.

    Raises on an unreadable or undecodable file, or any mismatch. The
    raise is the whole mechanism: it becomes `ExportOutcome(ok=False)`,
    an `ERROR` audit row and a `REVIEW_REQUIRED` grade through the same
    channel a write that raised takes (#181) -- and because it fires
    against the temporary file, before the rename that publishes it
    (#199), a file that fails here is never delivered at all.

    Each reason names the exception's type as well as its text: a
    message-less exception (`StopIteration()`) would otherwise leave a
    row reading "could not be decoded ()" (#435's class). Spelled by
    `logger.describe_exception`, the one spelling every recorded reason
    uses (#435).

    **`verify_readback=True` is the strict contract, and step 2 is the
    first descriptor on which it refuses something the export worker
    wrote on purpose (#507).** Everything this check refused before was
    a file the worker meant to be correct. Since #502 the default write
    path *deliberately* writes a Photometric Interpretation the syntax
    does not admit -- as declared, with a `WARNING` row -- because there
    are formats this library can read and cannot write, and a
    de-identified copy the caller can fix beats no copy. A caller who
    passes `verify_readback=True` has asked for the stronger claim, and
    for them that same file is a failure: `ok=False`, an `ERROR` row, a
    `REVIEW_REQUIRED` grade and **nothing delivered**, because the raise
    fires against the temporary file before the rename (#199). What the
    flag now buys that it did not before is a demand for conformant
    output. There is no third setting and no new parameter for it: #26
    freezes the public surface, and two contracts -- best effort by
    default, conformance on request -- are the two that were asked for.

    **The limit, which is documented rather than fixed (#507).** Step 2
    is *structural*: it asks whether a conformant reader could take this
    label under this syntax, not whether the samples are really in the
    colour space it names. Three samples are equally `RGB` and
    `YBR_FULL`, so the second question has no answer from the bytes, and
    #372/#448/#482 is the standing ruling that this library does not
    make -- or check -- a claim the bytes cannot prove. `RGB` over YBR
    samples therefore passes, and so does a file under a syntax with no
    row in `_ADMISSIBLE_PHOTOMETRICS`. Note also what step 2 is *not*:
    `PhotometricInterpretation` is not in `_READBACK_DESCRIPTORS` and
    was never in it, and adding it there would be a comparison that
    cannot fail -- see the comment beside that tuple.
    """
    try:
        readback = pydicom.dcmread(path)
    except Exception as exc:
        # Without paths: `path` is `<output path>.<pid>.tmp`, under
        # `Subject_<Patient ID>/`, and this message becomes the export's
        # `ERROR` row, where the outer spelling cannot strip it (bunch E).
        raise RuntimeError(
            f"Readback verification failed: the written file could not be "
            f"read back ({describe_exception_without_paths(exc)})") from exc

    mismatches = [
        f"{kw} reads back as {getattr(readback, kw, None)!r} where "
        f"{getattr(ds, kw, None)!r} was written"
        for kw in _READBACK_DESCRIPTORS
        if getattr(readback, kw, None) != getattr(ds, kw, None)]
    if mismatches:
        raise RuntimeError(
            "Readback verification failed: " + "; ".join(mismatches))

    # Before the decode, not after: a label the syntax does not admit
    # has to be named as such. An **undefined** label is what makes the
    # order observable -- pydicom raises `Unknown (0028,0004)
    # 'Photometric Interpretation' value 'NONSENSE'` from the decoder,
    # which sends the reader of a compliance report to the pixels for a
    # fault that is in one element of the header. The same is true of
    # any label pydicom does not recognise, a non-upper-case spelling
    # included. Measured, and worth knowing before this is "simplified":
    # the four labels #502 is about decode *cleanly* at
    # (rows, cols, 3) -- `pixel_array` returns `uint8 (8, 8, 3)` for
    # every one of `YBR_ICT`, `YBR_RCT` and both `YBR_PARTIAL_*`, since
    # pydicom special-cases the byte count only for `YBR_FULL_422`. So
    # for them the order changes nothing, and the undefined label is
    # the whole of the reason this check goes first.
    reason = _readback_label_mismatch(readback)
    if reason is not None:
        raise RuntimeError(f"Readback verification failed: {reason}")

    if written_pixels is not None:
        try:
            decoded, _ = _decode_pixels(readback, as_rgb=False)
        except Exception as exc:
            raise RuntimeError(
                f"Readback verification failed: the written pixel data "
                f"could not be decoded ({describe_exception(exc)})"
            ) from exc
        reason = _readback_pixel_mismatch(
            decoded, written_pixels,
            getattr(readback, "PixelRepresentation", None))
        if reason is not None:
            raise RuntimeError(f"Readback verification failed: {reason}")

        # **And the decode `ingest()` makes, where it differs (#596).**
        # The decode above asks for the stored samples, because those are
        # what was written; `ingest()` asks pydicom's default, which
        # converts a YBR_FULL family to RGB and refuses anything but
        # unsigned 8-bit samples doing it. Measured before this: a native
        # 16-bit `YBR_FULL` file passed here while `ingest()` and
        # `pixel_array` both refused it, and the JPEG 2000 one failed --
        # two answers from one contract, which is "this library can read
        # it back". After the exact compare, so a sample mismatch keeps
        # its more specific reason.
        #
        # Gated on the label **and** the decoded dtype (Q6, the review of
        # #609). Unsigned 8-bit is exactly what the conversion accepts, so
        # the second decode cannot change that verdict -- pinned against
        # pydicom by `test_unsigned_8_bit_ybr_full_converts_whenever_it_
        # decodes` -- and it cost a 100-frame JPEG 2000 file 20 s more and
        # a float32 copy of the array. **Not** on BitsAllocated: int8 is
        # BitsAllocated 8 and the conversion refuses it.
        if _written_photometric(getattr(
                readback, "PhotometricInterpretation", None)) \
                in _PYDICOM_CONVERTS \
                and not _pydicom_converts_samples_of(decoded.dtype):
            try:
                _decode_pixels(readback)
            except Exception as exc:
                raise RuntimeError(
                    f"Readback verification failed: the written file "
                    f"cannot be ingested by this library "
                    f"({describe_exception_without_paths(exc)})") from exc

    if written_waveform is not None:
        reason = _readback_waveform_mismatch(readback, written_waveform)
        if reason is not None:
            raise RuntimeError(f"Readback verification failed: {reason}")


#: The attestation `RedactionService` writes on every path that actually
#: modified pixels. Named here because the export gate reads it and
#: `services.py` writes it, and a string spelled twice is a string that can
#: be spelled differently once.
REDACTION_HASH_ATTR = "_ISOCENTER_REDACTION_HASH"


#: Icon Image Sequence, in the lowercase-hex spelling item paths use.
_ICON_IMAGE_SEQUENCE_TAG = "0088,0200"


def _is_own_icon_path(path) -> bool:
    """Is this nested payload the carrier's own icon? (#542)

    True exactly for a depth-1 Icon Image Sequence item: a path of one step
    whose sequence is (0088,0200). Such an icon is a thumbnail of the
    instance carrying it (PS3.3 C.7.6.1.1.6), so it can only show what
    *this* instance's redaction removed, and gating it per instance cannot
    fail open. Everything else -- an icon under Referenced Image Sequence,
    an icon inside an icon, pixel data under any other sequence -- is
    *foreign*: it may be a thumbnail of a different instance, and keeps the
    store-wide gate. Decided from the path alone, never by following a
    reference, because redaction's `regenerate_uid()` breaks the reference
    for exactly the instances that were redacted.
    """
    return len(path) == 1 and path[0][0] == _ICON_IMAGE_SEQUENCE_TAG


def redaction_in_effect(instances: Iterable["Instance"]) -> bool:
    """Does any instance this export can see carry a redaction attestation?

    The store-wide half of the nested-icon gate (#183 Q2/Q10, narrowed by
    #542): the answer for every nested icon that is **not** its carrier's
    own depth-1 Icon Image Sequence item (`_is_own_icon_path`).

    **Store-wide on purpose, and narrowing it for those icons is a
    de-identification regression.** An Icon Image Sequence item is a
    downsampled copy of a frame, and nothing in this pipeline scans or
    redacts one: every pixel consumer reads `instance.get_pixel_data()`,
    which is the top-level frame and only that -- the burned-in identifier
    scan, both redaction paths and the export alike. So carrying icon bytes
    out of a session that redacted can re-export a thumbnail of exactly
    what redaction zeroed, with no scan and no zones applied.

    The per-instance gate -- "was *this* instance redacted" -- is blind to
    an icon under Referenced Image Sequence (0008,1140), which is a
    thumbnail of the SOP instance being *referenced* (PS3.3 C.7.6.16), not
    of the one carrying it, so a redacted image and the untouched instance
    that thumbnails it can be different files. And it cannot be resolved by
    following the reference: redaction calls `regenerate_uid()`, so
    `ReferencedSOPInstanceUID` names a UID that is no longer in the store
    and the lookup returns nothing for precisely the instances that were
    redacted. **It fails open**, which is the worst available answer.

    **Why the carrier's own icon is safely per instance (#542).** A depth-1
    Icon Image Sequence item thumbnails its carrier (PS3.3 C.7.6.1.1.6), so
    it can only show what *this* instance's redaction removed. The worker
    drops it iff this instance carries the attestation or has zones
    applied at export; applying the store-wide answer to it as well, as
    #183 did, stripped every unredacted instance's own thumbnail out of the
    export once anything was redacted, and graded it PASS.

    This function is the **attestation** half, and the load-bearing one.
    `_redaction_zones_for` looks zones up at *export* time from the
    *current* configuration, keyed on the series' device serial number,
    while `RedactionService` redacts whatever `rois` its caller passed. So
    the zones list is empty at export while the pixels are redacted
    whenever the rule was edited, the serial changed, the service was
    driven directly, or the series has no equipment at all. The session
    adds the configuration belt itself -- a zones rule that **matches a
    series in the store**, for a redaction configured but not yet run --
    because only it can match rules to series; a rule matching no series
    redacts nothing and is no reason to drop an icon (#542). It used to be
    a `rules` parameter here that counted any rule with zones, matched or
    not. `write_tree` has no configuration to consult and uses this alone.

    Args:
        instances: Every instance this export can see. The session path
            passes the whole store; `write_tree` passes the tree it is
            about to write.

    Returns:
        bool: True when any instance carries the attestation.
    """
    return any(REDACTION_HASH_ATTR in inst.attributes for inst in instances)


def _redaction_icon_loss(path, terminal_tag, seq_tag, inst, ctx) -> Optional[str]:
    """Why the redaction gate drops this nested payload, or None (#542).

    Two tiers. The carrier's own depth-1 icon (`_is_own_icon_path`)
    thumbnails the carrier, so it goes iff *this* instance's pixels are
    redacted: the attestation on the instance the worker holds (`_merge`
    never mutates `inst.attributes`, so the `_`-prefixed key is still
    there), or zones applied by this export. Zones count whether or not
    the redaction then applies -- a declined redaction still leaves the
    icon a thumbnail of pixels the caller asked to remove, and failing
    closed is the answer. Every other nested payload may thumbnail a
    different, redacted instance and takes the store-wide
    `ctx.drop_foreign_icons`.

    Returns:
        Optional[str]: The `DATA_LOSS` detail for a drop, one text per
        tier, or None when the gate keeps the payload.
    """
    if _is_own_icon_path(path):
        if REDACTION_HASH_ATTR not in inst.attributes and not ctx.redaction_zones:
            return None
        return (f"Standard tag {terminal_tag} inside {seq_tag} was dropped "
                f"with its sequence item because this instance's pixel data "
                f"is redacted. An icon is a downsampled copy of a frame and "
                f"nothing scans or redacts one, so exporting it would ship a "
                f"thumbnail of what redaction removed.")
    if not ctx.drop_foreign_icons:
        return None
    return (f"Standard tag {terminal_tag} inside {seq_tag} was dropped with "
            f"its sequence item because this export redacts pixel data, and "
            f"an icon here may be a thumbnail of a redacted instance. Nothing "
            f"scans or redacts an icon, and a reference to a redacted "
            f"instance cannot be followed once redaction has regenerated its "
            f"UID.")


def _instances_in(patient, studies) -> Iterable["Instance"]:
    """Every instance under `studies`, for the store-wide redaction gate."""
    for study in studies or getattr(patient, "studies", ()):
        for series in study.series:
            yield from series.instances


def _resolve_ds_item(ds, path):
    """Follow an `iter_item_tree` path into a pydicom Dataset.

    The `ds`-side twin of `entities.resolve_item_path`, and it inherits that
    function's rule verbatim: **None means "this item is gone", never "use
    the root instead"**. Writing an icon's pixels onto the instance would
    fabricate a top-level element that was never in the file, which is #57's
    defect exactly.

    Returns:
        Optional[tuple]: `(item, parent_ds, seq_tag)` -- the resolved item,
        the dataset holding the sequence it sits in, and that sequence's
        pydicom `Tag`. The last two are what lets the caller *remove* the
        item, which is the conformant outcome when its bytes cannot be
        written. None if any step does not resolve.
    """
    cur, parent, seq_tag = ds, None, None
    for tag_str, index in path:
        group, element = (int(x, 16) for x in tag_str.split(','))
        tag = Tag(group, element)
        if tag not in cur:
            return None
        sequence = cur[tag].value
        if index >= len(sequence):
            return None
        parent, seq_tag, cur = cur, tag, sequence[index]
    if parent is None:
        return None
    return cur, parent, seq_tag


def _write_back_nested_pixels(ds, inst, ctx, losses, *, warnings,
                              corrections) -> None:
    """Put each carried nested payload back into its sequence item (#183).

    A post-pass over the dataset `_merge_sequences` has already built,
    rather than a change to `_merge_sequences` itself. Two reasons, and the
    second is the one that matters: the merge walks `{tag: DicomSequence}`
    and has no notion of a path, and this worker is on **both** export paths
    by construction -- `write_tree` and `DicomSession.export()` dispatch the
    same `_export_instance_worker` -- so a post-pass here cannot land on one
    path only. `tests/test_api_coherence.py` compares trees rather than
    contents, so a one-path writeback would slip past it.

    **The single rule this adds to the exporter: never descriptors without
    data.** Wherever the bytes cannot be written -- the redaction gate, an
    item that is gone, a geometry that no longer fits -- the conformant
    output is *no sequence item at all* plus a `DATA_LOSS` row. Leaving the
    descriptors behind is the Type 1 violation of the Icon Image Macro
    (PS3.3 C.7.6.1.1.6) that this whole change exists to fix, and #160
    settled the identical question for discarded multiplex groups the same
    way.

    Every path is resolved and every outcome decided **before** anything is
    removed, and that ordering is load-bearing. Two icons under one parent
    sequence: remove item 0 first and item 1 slides into index 0, so its
    path resolves to a live item that is no longer its own -- and a loop
    interleaving the two would file a "gone" row for it while leaving it in
    the file. Removal is then by object identity rather than by the index
    that was recorded, because two identical icon `Dataset`s compare equal.

    **An icon's BitsStored and HighBit follow the top level's rule (#598).**
    `_stored_width` over the array being written, and HighBit =
    BitsStored - 1, with the same INFO notes (`_width_notes`) handed back
    on `corrections`, prefixed with the item's path. Ingest's HighBit row
    says an export writes HighBit as BitsStored - 1, and that is now true
    of an icon; and a JPEG-LS icon whose samples overflowed its declared
    BitsStored is no longer masked by every conformant reader (measured:
    `40000` read back as `3136`). `corrections` is keyword-only with no
    default, the `offset_tables` precedent.

    **An icon's label is judged where it is written (#602).** This is the
    icon's pixel door, as `_write_pixel_geometry` is the top level's, so
    each item that is actually written has its Photometric Interpretation
    judged here, from the pydicom item (which carries
    `_merge_sequences`' respelling), against the uncompressed row: see
    `_icon_label_warning`. An inadmissible label is written as declared
    with a WARNING on `warnings`; a multi-valued one too
    (`_icon_label_arity_warning`). An item in `removals` writes no pixel
    element and is not judged -- it carries its own loss row.
    """
    refs = getattr(inst, "_nested_pixel_refs", None)
    if not refs:
        return

    removals, writes = [], []
    for (path, terminal_tag), ref in refs.items():
        resolved = _resolve_ds_item(ds, path)
        # The graph item, for its descriptors. `ds`'s sequences were built
        # from `inst.sequences` by `_merge_sequences` a few lines above, so
        # the two cannot disagree about what sits at this path -- and
        # reading the geometry from the graph is what lets ONE function
        # assemble it, here and at the ingest that recorded the provenance.
        graph_item = resolve_item_path(inst, path)
        if resolved is None or graph_item is None:
            # The item was removed between ingest and export. Nothing to
            # take out of the file and nothing to write; just say so.
            losses.append((LOSS_SCOPE_STANDARD, (
                f"Standard tag {terminal_tag} was carried in the store but "
                f"the sequence item it belongs to is no longer in the "
                f"object graph, so it is not in the exported file.")))
            continue

        item, parent, seq_tag = resolved

        # SIGNAL: acquired content that was in the source and is not in
        # the export, which grades (#542, Q8). #183 wrote STANDARD, so a
        # redaction that stripped every thumbnail graded PASS. The three
        # other icon losses in this loop stay STANDARD: an item gone or
        # reordered, or a failed restore, are not losses redaction caused.
        redacted = _redaction_icon_loss(path, terminal_tag, seq_tag, inst, ctx)
        if redacted is not None:
            removals.append((parent, seq_tag, item))
            losses.append((LOSS_SCOPE_SIGNAL, redacted))
            continue

        # The shifted-index guard. Position is the only identity a sequence
        # item has, and the path was recorded at ingest: remove an earlier
        # sibling in between and this path still resolves, to a *neighbour*.
        # Comparing the descriptors the bytes were taken from against the
        # ones the destination declares is what tells the two apart.
        #
        # It has to be here, explicitly, because the loader does not raise:
        # its padding fallback truncates a too-long frame into the target
        # shape and returns a 1-D array for a too-short one. See
        # `NestedPixelRef`. Descriptors rather than byte counts because
        # `BitsAllocated // 8` is 0 for a 1-bit icon.
        #
        # Not a proof of identity -- two icons of equal geometry are
        # indistinguishable by it -- but it converts the detectable half of
        # the failure from silent wrong bytes into a reported loss.
        geometry = nested_item_geometry(graph_item.attributes)
        if ref.geometry is not None and geometry != ref.geometry:
            removals.append((parent, seq_tag, item))
            losses.append((LOSS_SCOPE_STANDARD, (
                f"Standard tag {terminal_tag} inside {seq_tag} was not "
                f"restored: the sequence item at its recorded position now "
                f"declares {geometry} where the stored bytes were taken "
                f"from {ref.geometry}, so an item was removed or reordered "
                f"after ingest. Its item was dropped rather than filled "
                f"with another item's pixels.")))
            continue

        try:
            decoded = SidecarPixelLoader(
                ref.sidecar_path, ref.offset, ref.length, ref.alg,
                metadata=_nested_loader_metadata(geometry, ref, inst))()
        except Exception as exc:  # pylint: disable=broad-except
            removals.append((parent, seq_tag, item))
            losses.append((LOSS_SCOPE_STANDARD, (
                f"Standard tag {terminal_tag} inside {seq_tag} could not be "
                f"restored from the sidecar ({describe_exception(exc)}); "
                f"its sequence item was "
                f"dropped rather than exported with descriptors and no "
                f"pixel data.")))
            continue

        writes.append((path, graph_item, item, terminal_tag, decoded,
                       geometry[4]))

    for path, graph_item, item, terminal_tag, decoded, bits in writes:
        group, element = (int(x, 16) for x in terminal_tag.split(','))
        # PS3.5: `OW` above 8 bits allocated, `OB` at or below. Derived from
        # the item's own BitsAllocated rather than carried on the blob,
        # because the export writes Implicit VR Little Endian and no VR
        # reaches the file at all -- pydicom just needs one to encode with,
        # and one rule beats a stored value that can disagree with the
        # descriptor beside it.
        vr = 'OW' if bits > 8 else 'OB'
        # Unconditionally raw, and it does not consult `ctx.compression`.
        # `use_compression=True` J2K-compresses the top-level frame only;
        # "the icon wasn't compressed too" will read as an oversight, so:
        # it is deliberate. An icon is a thumbnail, the saving is nil, and
        # a second encoder call per instance is not.
        item.add_new(Tag(group, element), vr, decoded.tobytes())
        # A 1-bit icon is written as the top level writes a 1-bit image
        # (#649): BitsAllocated from the array, which the loader returns
        # one byte per sample, so BitsAllocated 8 with BitsStored 1 below.
        # Left at the declared 1, the file held 15 unpacked bytes where a
        # reader unpacks 2, and pydicom's `pixel_array` raised `TypeError`.
        # PS3.3 C.7.6.1.1.6 permits 1 or 8. Re-packing to keep the declared
        # 1 would be a second rule at a second depth. The graph keeps the
        # declared value; only the file changes.
        if bits == 1:
            item.BitsAllocated = decoded.itemsize * 8
        # BitsStored and HighBit, by the top level's rule (#598). Only
        # where the item declares a BitsStored: a hand-built item with
        # none is not given one. PixelRepresentation, and BitsAllocated
        # other than 1, are left as declared -- the loader's dtype was
        # built from them (`_nested_loader_metadata`), so the array
        # already agrees.
        if declared_int(graph_item.attributes, "0028,0101") is not None:
            item.BitsStored, widened = _stored_width(
                decoded, graph_item.attributes)
            item.HighBit = item.BitsStored - 1
            corrections.extend(
                f"At {_item_path_words(path)}: {note}"
                for note in _width_notes(
                    widened, declared_int(graph_item.attributes, "0028,0102"),
                    item.BitsStored, item.HighBit))
        # The label, judged where the pixel element is written (#602).
        label = item.get("PhotometricInterpretation")
        at = _item_path_words(path)
        if isinstance(label, (list, tuple, MultiValue)) and len(label) > 1:
            warnings.append(_icon_label_arity_warning(label, at))
        else:
            judged = _icon_label_warning(_written_photometric(label), at)
            if judged is not None:
                warnings.append(judged)

    for parent, seq_tag, item in removals:
        sequence = parent[seq_tag].value
        for index in range(len(sequence) - 1, -1, -1):
            if sequence[index] is item:
                del sequence[index]
                break
        if not len(sequence):
            # An empty sequence is not the same thing as an absent one, and
            # Icon Image Sequence is Type 3 wherever the macro is included
            # -- so absent is conformant and a zero-item sequence is just an
            # assertion about nothing.
            del parent[seq_tag]


def _nested_loader_metadata(geometry, ref, inst) -> dict:
    """The reshape metadata for one nested payload's loader.

    Built from the geometry of the item resolved at export, never from
    anything stored with the blob -- the bytes are about to be written into
    *that* item, so that is the shape they have to take.

    No `pixel_dtype`: the nested float pair is deliberately not carried
    (#183 Q5), so a nested payload is always integer and there is nothing
    for the float branch to read.
    """
    rows, cols, samples, frames, bits, pixel_representation = geometry
    return {
        "sop_instance_uid": getattr(inst, "sop_instance_uid", "Unknown"),
        "rows": rows,
        "cols": cols,
        "samples": samples,
        "frames": frames,
        "bits": bits,
        "pixel_representation": pixel_representation,
        "pixel_hash": ref.blob_hash,
    }


#: How every export line and row names an instance that carries no SOP
#: Instance UID. Never its output path: that is
#: `<folder>/Subject_<Patient ID>/...`, and these lines reach the log, the
#: audit table and the compliance report (bunch E). One spelling, because
#: the worker's failure line and the parent's three report helpers name the
#: same outcome.
_NO_SOP_UID = "an instance with no SOP Instance UID"


@dataclass(frozen=True)
class _ReVr:
    """One private element written under a VR other than its recorded one,
    or collapsed to one value (#571). Tags and VRs only: never the value,
    which after a REPLACE of user text, and before it, is patient-derived.
    """
    tag: str
    within: str
    recorded: Optional[str]
    written: str
    #: The multiplicity collapsed from, for the VM n -> one `UT` case.
    values: Optional[int] = None


#: How many elements `_re_vr_warning` names before counting the rest: a
#: vendor block re-VR'd whole would otherwise be one audit row the length
#: of the block.
_RE_VR_NAMED = 10


def _re_vr_warning(revrs) -> Optional[str]:
    """The one `WARNING` sentence for an instance's re-VR'd private
    elements, or None when there are none (#571).

    One per instance, whatever the syntax: under Implicit VR Little Endian
    no VR is on the wire, but the value is still encoded as the new VR
    (a US written LO is text, not two bytes), and an explicit-VR file
    names it and re-ingest records it permanently. The words say
    "written", never that the file names the VR.
    """
    if not revrs:
        return None
    named = []
    for r in revrs[:_RE_VR_NAMED]:
        where = f"({r.tag})" + (f" in {r.within}" if r.within else "")
        recorded = f" recorded {r.recorded}" if r.recorded else ""
        if r.values:
            named.append(f"{where}{recorded} VM {r.values}, written as one "
                         f"{r.written} value")
        else:
            named.append(f"{where}{recorded}, written {r.written}")
    rest = len(revrs) - len(named)
    listed = "; ".join(named) + (f"; and {rest} more" if rest else "")
    return (f"Private element{'s' if len(revrs) > 1 else ''} {listed}. "
            f"Each value is written under a VR that holds it, unchanged: the "
            f"VR recorded at ingest no longer does -- typically after a "
            f"REPLACE -- or a value over 64 characters cannot stay "
            f"multi-valued, and the values are joined with backslashes into "
            f"one, recoverable by splitting. A file written with an explicit "
            f"VR transfer "
            f"syntax names the new VR, and a re-ingest of it records that "
            f"VR in place of the source's.")


def _export_instance_worker(ctx: ExportContext) -> "ExportOutcome":
    """
    Worker function to export a single instance.

    Reconstructs a pydicom Dataset from the ExportContext (Instance + Attributes)
    and saves it to disk. Handles optional compression (JPEG2000).

    Args:
        ctx (ExportContext): The context/request for export.

    Returns:
        ExportOutcome: the write's result, plus any elements lost on the
            way out for the parent to log and audit (#126).
    """
    losses: List[Tuple[str, str]] = []
    corrections: List[str] = []
    warnings: List[str] = []
    uid = getattr(ctx.instance, "sop_instance_uid", None)

    try:
        # Whether the pixels are encoded, asked once and of the one
        # predicate every other reader of `compression` asks (#605).
        # Inside the `try`: `ExportContext` refuses an unknown value on
        # construction, but a dataclass can be edited afterwards, and
        # that context must fail as its own instance, not take the batch
        # down. The worker used to ask three ways -- `== "j2k"` here and
        # in `_finalize_dataset`, truthiness in the integer arm -- so
        # `compression="rle"` skipped both the raw write and the encoder
        # and delivered an image with no Pixel Data as `ok`.
        compressed = _compresses(ctx.compression)
        # The syntax the file will be written under, decided here so the
        # label check judges what is actually being written (#502) rather
        # than re-deriving it beside each call. `_create_ds` starts every
        # file at Implicit VR Little Endian and only the compressed path
        # moves it.
        written_syntax = (str(JPEG2000Lossless) if compressed
                          else str(ImplicitVRLittleEndian))

        inst = ctx.instance
        ds = DicomExporter._create_ds(inst)

        # What `verify_readback` holds the written file against (#449):
        # the array each pixel element is written from, and the waveform
        # bytes. Each is set where its element is written, so an instance
        # that writes none leaves it None and is not decoded.
        written_pixels = None
        written_waveform = None

        # 0. Base Attributes
        #
        # `attributes` is `inst.attributes` with (0028,0004) spelled as a
        # Code String is defined (#532), and it is a copy whenever that
        # changed anything. It is what `_merge` and both
        # `_write_pixel_geometry` calls read, and nothing else: every
        # other reader below stays on `inst.attributes`, the graph.
        attributes, respelled = _label_as_written(inst.attributes)
        if respelled is not None:
            corrections.append(respelled)
        #
        # `revrs` gathers every private element written under a VR other
        # than its recorded one, here and in every sequence item, for one
        # sentence per instance after the merges (#571). The three stamp
        # merges below are standard tags and pass none.
        revrs: List[_ReVr] = []
        DicomExporter._merge(ds, attributes, losses,
                             vrs=getattr(inst, 'attribute_vrs', None),
                             revrs=revrs)
        DicomExporter._merge_sequences(ds, inst.sequences, losses,
                                       revrs=revrs, corrections=corrections)
        re_vr = _re_vr_warning(revrs)
        if re_vr is not None:
            warnings.append(re_vr)

        # 0b. Nested sidecar payloads, back into the items they came out
        # of (#183). After the merge, because it needs the sequence items
        # the merge just built; here rather than inside `_merge_sequences`
        # so it cannot be reached by an `export_batch` caller separately,
        # and so both export paths get it from the one worker they share.
        _write_back_nested_pixels(ds, inst, ctx, losses, warnings=warnings,
                                  corrections=corrections)

        # 1. Patient Level
        DicomExporter._merge(ds, ctx.patient_attributes, losses)

        # 2. Study Level
        DicomExporter._merge(ds, ctx.study_attributes, losses)

        # 3. Series Level
        DicomExporter._merge(ds, ctx.series_attributes, losses)

        # Study Time is Type 2 (PS3.3 C.7.2.1): present, and empty when
        # unknown. `IODValidator` refuses it **absent**, so a study with
        # no time and an instance with none failed `session.export()`
        # outright, and `write_tree` hid the same gap by writing the
        # literal `120000` -- a fabricated clinical time (#570). Filled
        # here, after every merge, and not in `export_stamp_attributes`:
        # only here is it known whether the instance or the study
        # supplied one, and a stamp of `""` would overwrite a real value.
        if "StudyTime" not in ds:
            ds.StudyTime = ""

        # Every other Type 2 element the IOD table knows, the same way
        # (#600). Type 2 means present, and empty when unknown; an absent
        # KVP or Slice Thickness failed a CT's export with `[Type 2 Error]`
        # for a file that is conformant the moment the element is written
        # zero-length. The conditions, each of which is the trap:
        #   - after every merge, like Study Time above, so a value the
        #     instance, the study or the series supplied is already in
        #     `ds` and is never overwritten;
        #   - read from `IODValidator`'s own table, so it fills exactly
        #     what `validate` would refuse and invents nothing the
        #     validator does not know -- an OT image gains no KVP;
        #   - never Type 1: an empty Type 1 element is still a refusal,
        #     and a fabricated value would be a lie;
        #   - no row and no note, per #570's Study Time (owner ruling Q10):
        #     the written file is conformant, and absent and empty say the
        #     same thing for Type 2.
        # Study Time keeps its own fill above: it is unconditional on SOP
        # class, and the table is CT-only, so folding it in would stop
        # filling it on OT and SC files. `dictionary_VR` answers one VR for
        # every tag the table holds today; a table tag whose dictionary VR
        # is ambiguous (`'US or SS'`) would need its own choice here.
        for tag in IODValidator.absent_type2(ds):
            ds.add_new(tag, dictionary_VR(tag), None)

        # There is deliberately no `populate_attrs(ds, inst)` here, and
        # there must never be again (#184). It was the ingest reader
        # pointed at the dataset this worker just built, writing the
        # merged result back onto the live instance: `add_sequence_item`
        # appends, so every sequence item duplicated per export
        # (1 -> 2 -> 3), every patient/study/series tag landed in
        # `inst.attributes`, and both writes bump `_revision`, so the
        # next save() persisted the damage. Harmless-looking under
        # `session.export()` only because `maxtasksperchild=25` pins
        # these workers to subprocesses (#185); real through the public
        # `export_batch()`/`write_tree()` under threads, which is the
        # path a free-threaded build takes by default. Measured before
        # deletion: the exported file is byte-identical without the
        # call, across hand-built and ingested instances, pixel and
        # waveform alike -- it contributed nothing to `ds`, because it
        # only ever wrote in the wrong direction. Its one side effect
        # that mattered -- the modality checks below seeing the
        # *merged* value -- is now had by asking `ds` directly, which
        # is where the merged view already lives.

        # Handle Pixel Data
        # If we have modified pixels in memory (redaction), we MUST use them.
        # If they were unloaded, we load them.
        arr = inst.pixel_array

        if arr is None:
            try:
                arr = inst.get_pixel_data()
            except FileNotFoundError:
                # Check Modality to decide if we should fail or proceed
                # Image implementations MUST have pixels.
                # Non-image (SR, PR, KO, DOC) can proceed without.
                #
                # From `ds`, not `inst.attributes`: the modality may
                # live only at series level (hand-built graphs,
                # write_tree()), and `ds` holds the merged view. The
                # instance's own dict only appeared to hold it because
                # the deleted writeback above copied it there (#184).
                mod = str(ds.get("Modality", "OT"))

                # If it claims to be an image but has no pixels, fail hard (Safety)
                if mod in _IMAGE_MODALITIES:
                    raise RuntimeError(f"Pixels missing for Image Modality {mod}")

                # Otherwise (SR, etc.), proceed
                arr = None

        # Resolve the geometry once, here, and use the same answer for the
        # redaction axes and the descriptors written below. It has to
        # happen before the redaction block: getting the axes wrong applies
        # a zone to the wrong region, so the burned-in identifier stays in
        # the exported pixels while the pipeline reports a successful
        # redaction -- the most severe of the four sites this heuristic
        # reached, and the one neither #186 nor #205 names.
        #
        # A ValueError here (the instance declares a SamplesPerPixel no
        # axis of the array can carry) propagates to the except below and
        # becomes ExportOutcome(ok=False): audited, counted in
        # ExportSummary.failed and surfaced by the compliance report
        # (#181), which is where a contradiction belongs.
        geom = resolve_pixel_geometry(arr.shape, inst.attributes) \
            if arr is not None else None

        if arr is not None:
            # APPLY REDACTION (Fix for Export Compression Bug)
            if ctx.redaction_zones:
                # Local import to avoid circular dependency
                from .services import RedactionService

                # Always a copy, whatever the array's writeability (#469).
                # This copied only a read-only array, so a writeable one --
                # the caller's own resident array whenever the worker runs
                # in the caller's process: `export_batch()` under threads,
                # 3.14t's default -- was zeroed in place, and the export
                # redacted the live graph. A saved array then lost the
                # zones at the next `unload_pixel_data()` and an unsaved
                # one carried them into the next save, while under
                # processes the child's array was a copy and nothing
                # changed. Copied on every path, processes included (the
                # owner's ruling): which executor ran must not decide what
                # the caller's array holds afterwards. The cost is one
                # frame-set, transient, and only for an instance with
                # zones, beside the `tobytes()` copy the write already
                # makes. Readback compares against this copy.
                arr = arr.copy()

                # Apply zones
                RedactionService.apply_redaction_to_array(
                    arr, ctx.redaction_zones, geometry=geom)

        # This refusal used to live inside the integer branch below, which
        # meant the float branch -- which sits above it and ends with
        # `arr = None` -- never reached it. A float array whose geometry
        # was a guess exported a file with no Rows, no Columns and no
        # SamplesPerPixel, all three Type 1 in the Floating Point Image
        # Pixel Module (PS3.3 C.7.6.24), while the identical uint8 graph
        # was correctly refused (#216). Which pixel element carries the
        # bytes has nothing to do with whether the geometry is known, so
        # the check belongs to neither branch.
        #
        # It sits *below* the redaction block rather than above it, which
        # costs one redaction pass on an instance that is about to be
        # refused and buys a source order that still reads
        # resolve -> redact -> write. Nothing between the resolution and
        # here can change `geom`: the redaction block copies the array
        # (always, since #469), which does not change its shape.
        if arr is not None and geom.evidence is GeometryEvidence.GUESSED:
            # The instance, never `ctx.output_path`: this message is the
            # export's `ERROR` row, the report and `ExportError` whole,
            # and the output path is `Subject_<Patient ID>/...` (review
            # of #589). The same for the float refusal below.
            raise RuntimeError(
                f"Refusing to write instance {uid}: the pixel "
                f"array's shape {tuple(arr.shape)} is ambiguous -- it is "
                f"equally a multi-frame grayscale image and a "
                f"single-frame image with {arr.shape[-1]} samples per "
                f"pixel -- and the instance declares no SamplesPerPixel "
                f"(0028,0002), NumberOfFrames (0028,0008) or "
                f"Rows/Columns to resolve it. Writing it would guess "
                f"the image's geometry, and a recipient cannot tell a "
                f"guess apart from a correct answer.")

        if arr is not None and arr.dtype.kind == 'f':
            # A floating-point array is not Pixel Data, and writing it
            # under (7fe0,0010) does not make it Pixel Data -- it makes a
            # file that reads 1056964608 where the source said 0.5, with
            # BitsAllocated=32 and PixelRepresentation=0 next to it so
            # the result is internally coherent and nothing downstream
            # errors (#170). PS3.5 Section 8.2 -- not A.1, which is the
            # Implicit VR Little Endian Transfer Syntax and says nothing
            # about this -- makes Pixel Data and Float Pixel Data
            # mutually exclusive, which is why the
            # integer element is deleted below rather than merely not
            # written: `_merge` writes whatever `attributes` holds, and
            # a file carrying both is nonconformant however it got that
            # way.
            #
            # Refusing to write anything was the first cut of this fix,
            # and it traded a silent corruption for a quiet
            # nonconformance: Float Pixel Data is Type 1 in the Floating
            # Point Image Pixel Module (PS3.3 C.7.6.24), so a Parametric
            # Map exported with no pixel element at all is invalid in a
            # way #160 had just finished fixing elsewhere (#193). The
            # array is *in hand* at this point -- `get_pixel_data()`
            # re-read it from the source file and pydicom surfaces
            # (7fe0,0008)/(7fe0,0009) through `.pixel_array` -- so the
            # dtype is known and the correct tag is a lookup, not a
            # guess. float32 and float64 round-trip exactly under both
            # implicit and explicit VR.
            #
            # This sits *below* the redaction block, not above it.
            # Zeroing a zone works on a float array as well as an
            # integer one, and writing the bytes before the zones were
            # applied would export the burned-in identifiers this
            # pipeline exists to remove.
            #
            # `itemsize`, not `dtype`, is what selects the tag: it is
            # the property the two DICOM elements are defined by (32-bit
            # and 64-bit IEEE-754). float16 has no DICOM home at any
            # tag, so it is the one arm that still loses the data -- and
            # it takes the same modality decision as a missing source
            # file, because the outcome is the same file. Only a caller
            # handing `set_pixel_data` a float16 array can reach it; no
            # DICOM element decodes to one.
            #
            # Before either float element is written: a multi-sample
            # float instance has no conformant file to become, so it is
            # refused the way a GUESSED geometry is above -- the raise
            # becomes ExportOutcome(ok=False), an ERROR audit row and a
            # REVIEW_REQUIRED grade (#181, #215). The condition names
            # the two arms that write an element; the float16 arm below
            # writes none, so its samples>1 shape keeps taking the
            # DATA_LOSS route it always took rather than acquiring a
            # second failure mode as a ride-along (#222).
            #
            # Keyed on the sample count, never on the declared
            # Photometric Interpretation: every declared value is barred
            # identically here. C.7.6.24 and C.7.6.25 enumerate
            # MONOCHROME2 and nothing else, C.7.6.3.1.2 permits
            # MONOCHROME2 only at SamplesPerPixel = 1, and Planar
            # Configuration is in neither module's attribute table --
            # so passing a declared value through (#224's narrow fix,
            # which this supersedes) still wrote a file both modules
            # bar, it merely stopped inventing the value.
            if arr.itemsize in (4, 8) and geom.samples > 1:
                raise RuntimeError(
                    f"Refusing to write instance {uid}: the pixels "
                    f"are {arr.dtype} and the geometry resolves to "
                    f"{geom.samples} samples per pixel, and there is no "
                    f"conformant way to write a multi-sample float pixel "
                    f"element. The Floating Point and Double Floating "
                    f"Point Image Pixel Modules (PS3.3 C.7.6.24, "
                    f"C.7.6.25) permit only "
                    f"PhotometricInterpretation = MONOCHROME2, which "
                    f"C.7.6.3.1.2 restricts to SamplesPerPixel "
                    f"(0028,0002) = 1. Correct SamplesPerPixel if the "
                    f"declaration is wrong, or export each sample plane "
                    f"as its own single-sample instance.")

            # `written_pixels` is taken in each arm that writes an
            # element, and here rather than at the finalize below: this
            # branch ends with `arr = None`, so a capture after it would
            # leave every float export unchecked (#449). The float16 arm
            # writes no element and takes none, so it is not decoded.
            if arr.itemsize == 4:
                ds.FloatPixelData = arr.tobytes()
                ds.BitsAllocated = 32
                written_pixels = arr
            elif arr.itemsize == 8:
                ds.DoubleFloatPixelData = arr.tobytes()
                ds.BitsAllocated = 64
                written_pixels = arr
            else:
                # `ds`, not `inst.attributes` -- same reason as the
                # missing-pixels check above (#184).
                mod = str(ds.get("Modality", "OT"))
                if mod in _IMAGE_MODALITIES:
                    raise RuntimeError(
                        f"Pixels missing for Image Modality {mod}: a "
                        f"{arr.dtype} pixel array has no DICOM element "
                        f"that can carry it.")
                # Scoped STANDARD: group 7fe0 is even, the same parity
                # rule every other loss row uses (#146). Not graded
                # harder: #150 carved out SIGNAL for the multiplex
                # discard only, and widening it to this branch is its
                # own call, not a ride-along.
                losses.append((
                    LOSS_SCOPE_STANDARD,
                    f"Pixel data is {arr.dtype} and was not written: no "
                    "DICOM pixel element carries it. (7fe0,0008) and "
                    "(7fe0,0009) are 32- and 64-bit IEEE-754, and "
                    "writing the bytes as (7fe0,0010) Pixel Data would "
                    "relabel them as integers. The exported instance "
                    "has no pixel data."))

            if "PixelData" in ds:
                del ds.PixelData

            # The *other* float element, too -- the exclusion names four
            # elements and deleting (7fe0,0010) alone closes one direction
            # of it. PS3.5 Section 8.2: "It is not permitted to have
            # more than one of Pixel Data Provider URL (0028,7FE0), Pixel
            # Data (7FE0,0010), Float Pixel Data (7FE0,0008) or Double
            # Float Pixel Data (7FE0,0009) in the top level Data Set."
            # Measured: a float32 array on an instance whose `attributes`
            # carry a "7fe0,0009" exported with (7fe0,0008) *and*
            # (7fe0,0009), and `dcmread(...).pixel_array` raises the same
            # "One and only one of ..." pydicom refuses the other two
            # directions with. Same reachability class as the rest of this
            # branch: `populate_attrs` skips group 7fe0 at ingest, so it
            # arrives from a hand-built graph or a `set_attr` call.
            #
            # The float16 arm is deliberately outside this: it writes no
            # element at all, so there is nothing here for it to be
            # exclusive *with*, and stripping a "7fe0,0008" `_merge` put
            # on the dataset would be a data-loss action that owes the
            # caller a loss row rather than a conformance correction.
            #
            # The fourth member of the sentence, Pixel Data Provider URL
            # (0028,7FE0), is deleted below in the `itemsize in (4, 8)`
            # arm instead of here, and it is the one member that leaves
            # with a DATA_LOSS row. The asymmetry is *reachability*.
            # `populate_attrs` skips the whole 7fe0 group at ingest, so a
            # second pixel element can only arrive from a hand-built
            # graph -- but (0028,7FE0) has VR UR, is not binary, and
            # survives `populate_attrs`, so it comes straight through an
            # ordinary ingest of a real file that declares one (#223,
            # measured: (7fe0,0008) and the URL both present in the
            # exported file, no audit row, graded PASS). Deleting a
            # caller's URL removes information the exported file does not
            # otherwise carry, so it owes them a row; deleting a
            # duplicate pixel element is a conformance correction on a
            # file pydicom refuses to read back at all.
            other = {4: "DoubleFloatPixelData",
                     8: "FloatPixelData"}.get(arr.itemsize)
            if other is not None and other in ds:
                del ds[other]

            # "The descriptors were merged from `attributes`, which is the
            # same source file this array was read back from, so they
            # already agree" is what used to stand here instead of this
            # call, and it was true only of the ingest path. `attributes`
            # is also whatever a caller last wrote, so a stale
            # Rows/Columns exported a *decodable* file describing a
            # different image -- measured Rows=10 Columns=10 beside 16
            # floats, and Rows=99 Columns=99 beside a (2,4,8) array --
            # which is worse than the absent-descriptor case #216 filed,
            # because nothing invites the reader to go back. Rows, Columns
            # and SamplesPerPixel are Type 1 in C.7.6.24 and C.7.6.25
            # exactly as they are in the Image Pixel Module, and
            # PhotometricInterpretation is Type 1 there with Enumerated
            # Value MONOCHROME2.
            #
            # Only on the arms that actually wrote a pixel element. The
            # float16 arm below writes none, so it writes no descriptors
            # -- the same rule `BitsAllocated`'s placement above already
            # follows. BitsAllocated stays out of the helper for that
            # reason too: 32 and 64 are what the two float modules
            # enumerate beside the tag this branch chose, not a width
            # derived from the bytes.
            if arr.itemsize in (4, 8):
                # PS3.5 Section 8.2's *other* sentence, the one this
                # branch has never enforced: "Bits Stored (0028,0101),
                # High Bit (0028,0102) and Pixel Representation
                # (0028,0103) shall not be present." Deleted rather than
                # merely not written, for the same reason the pixel
                # elements above are: `_merge` has already put whatever
                # `attributes` holds onto `ds`, and `populate_attrs`
                # skips only group 7fe0, so all three arrive from an
                # ordinary ingest of a real Parametric Map (#223).
                # Measured before the fix: BitsStored 32, HighBit 31,
                # PixelRepresentation 0 beside (7fe0,0008), no loss row,
                # graded PASS. This branch writes none of the three
                # itself, so "stop writing them" was never available.
                #
                # No DATA_LOSS row, and that is the deliberate difference
                # from the Pixel Data Provider URL below. These three
                # carry nothing a recipient can want: PS3.3 C.7.6.24 says
                # they are "not used because the stored pixel values
                # always occupy the entire word" and "always signed", so
                # their content is fixed by the standard rather than by
                # the source file. The same sentence fixes BitsAllocated
                # to 32 or 64 and this branch has silently overwritten
                # *that* since #170 -- one sentence, one class of
                # element, one silent action.
                #
                # From `ds`, never from `inst.attributes`: the graph is
                # re-exportable and `SidecarPixelLoader` reads
                # "0028,0103" to reconstruct dtype. Mutating the graph
                # here would be a read-path write and a real loss.
                #
                # Inside this arm rather than at branch level, for the
                # reason the `del ds[other]` above is not: the float16
                # arm writes no pixel element, so nothing there forbids
                # the three. Not inside `_write_pixel_geometry` either --
                # that helper is shared with the integer branch, which
                # *requires* all three, and its contract is "write the
                # descriptors that describe the pixel element just
                # written", not "delete some".
                for kw in ("BitsStored", "HighBit", "PixelRepresentation"):
                    if kw in ds:
                        del ds[kw]

                # The fourth direction of the exclusion quoted above.
                # Reachable from an ordinary ingest, unlike the 7fe0
                # members, which is why this one is reported (#223).
                if "PixelDataProviderURL" in ds:
                    del ds.PixelDataProviderURL
                    losses.append((
                        LOSS_SCOPE_STANDARD,
                        "Pixel Data Provider URL (0028,7fe0) was not "
                        "exported: PS3.5 Section 8.2 permits only one of "
                        "it, Pixel Data (7FE0,0010), Float Pixel Data "
                        "(7FE0,0008) and Double Float Pixel Data "
                        "(7FE0,0009) in the top level Data Set, and this "
                        "instance's pixels were written under "
                        "(7fe0,0008)/(7fe0,0009). The URL named pixel "
                        "data held elsewhere; the exported file carries "
                        "its own."))

                _write_pixel_geometry(ds, geom, attributes,
                                      float_element=True,
                                      syntax_uid=written_syntax,
                                      warnings=warnings)

            arr = None

        if arr is not None:
            # MEMORY OPTIMIZATION:
            # If compression is requested, DO NOT convert to bytes here.
            # Pass the numpy array to _finalize_dataset -> _compress_j2k directly.
            # Only set PixelData if NOT compressing.

            if not compressed:
                ds.PixelData = arr.tobytes()

            # The second of PS3.5 Section 8.2's three reachable
            # directions -- the float branch above carries the first and
            # the third. The section is 8.2, "Native or Encapsulated
            # Format Encoding"; A.1, cited here and in #170 before it, is
            # the Implicit VR Little Endian Transfer Syntax and says
            # nothing about which pixel elements may coexist.
            #
            # The float branch has deleted (7fe0,0010)
            # since #170 for exactly this reason; the integer branch never
            # deleted its counterpart, so an instance carrying a
            # (7fe0,0008) of its own in `attributes` -- `_merge` writes
            # whatever it holds -- left with *both* pixel elements, which
            # pydicom itself refuses to decode: "One and only one of
            # 'Pixel Data', 'Float Pixel Data' or 'Double Float Pixel
            # Data' may be present". Measured reachable (#216).
            #
            # `populate_attrs` skips the whole 7fe0 group at ingest, so
            # this arrives only from a hand-built graph or a `set_attr`
            # call -- the same reachability class the float16 arm above
            # already serves, and not dead code.
            for kw in ("FloatPixelData", "DoubleFloatPixelData"):
                if kw in ds:
                    del ds[kw]

            # And the fourth member of the same sentence, which neither
            # branch deleted until #223: Pixel Data Provider URL
            # (0028,7FE0). It is the only one of the four that reaches
            # here from an ordinary ingest -- VR UR, not binary, and
            # `populate_attrs` skips only group 7fe0 -- so it is the only
            # one whose removal takes a caller's value with it, and it
            # leaves with a DATA_LOSS row rather than in silence. Scoped
            # STANDARD: group 0028 is even, the parity rule every other
            # loss row uses (#146). Per #146 a STANDARD loss does not by
            # itself move `validation_status`.
            #
            # The gate is this `arr is not None` block, not
            # `"PixelData" in ds`: when `compressed` the worker
            # never assigns `ds.PixelData` at all -- `_finalize_dataset`
            # compresses from the array -- so a membership test would let
            # the URL survive every compressed export. Measured. Do not
            # "simplify" it into one.
            if "PixelDataProviderURL" in ds:
                del ds.PixelDataProviderURL
                losses.append((
                    LOSS_SCOPE_STANDARD,
                    "Pixel Data Provider URL (0028,7fe0) was not "
                    "exported: PS3.5 Section 8.2 permits only one of it, "
                    "Pixel Data (7FE0,0010), Float Pixel Data "
                    "(7FE0,0008) and Double Float Pixel Data "
                    "(7FE0,0009) in the top level Data Set, and this "
                    "instance's pixels were written under (7fe0,0010). "
                    "The URL named pixel data held elsewhere; the "
                    "exported file carries its own."))

            _write_pixel_geometry(ds, geom, attributes,
                                  float_element=False,
                                  syntax_uid=written_syntax,
                                  warnings=warnings)

            # Derived from the array, never read from `attributes`, for the
            # same reason Rows and SamplesPerPixel now are -- and here the
            # reason is stronger, because `ds.PixelData = arr.tobytes()` is
            # three lines above. A declared width that disagrees with
            # `itemsize` cannot be honoured by the bytes being written, so
            # "the attributes win" is not one of the options (spec §3.10).
            #
            # This used to be reconciled on the *read* path: the
            # `set_pixel_data()` call that `get_pixel_data()` made ended in
            # an unconditional `set_attr("0028,0100", itemsize * 8)`.
            # Removing that call was right -- a read must not write -- but
            # it was the only thing correcting a declared width, and
            # `SidecarPixelLoader` buckets dtype as `uint16 if bits > 8
            # else uint8`, so every declared value outside {8, 16} reaches
            # here disagreeing with the array. A binary Segmentation
            # (BitsAllocated=1) exported as 1-bit beside 8-bit bytes, and
            # pydicom read a 2-frame 4x8 mask back as 16 frames: decodable,
            # internally coherent, and a different image. Reconciling it
            # where the bytes are produced is the fix that does not put a
            # write back on the load path.
            #
            # BitsStored is held against the array too, by
            # `_stored_width` (#468): a declared width every sample
            # fits is written as declared, and otherwise -- or when
            # none was declared, where 8-or-16 stood here and put
            # `int32` under a 16-bit claim -- the array's own width is.
            #
            # HighBit is BitsStored - 1, always, and never the declared
            # value. The array holds right-aligned values, so its most
            # significant stored bit is BitsStored - 1 whatever the
            # source said; a declared 12/15 written beside them claimed
            # left-aligned samples the bytes do not hold. A HighBit
            # declared with no BitsStored (16/16/11 before #468) is the
            # same claim.
            #
            # A rewrite of the declared BitsStored is handed back on
            # `corrections`, not logged here: this is usually a spawned
            # worker whose `isocenter` logger has no handler, and the
            # parent logs it (`_report_export_corrections`). `ds` only,
            # never `inst`, for the reason the float arm above gives.
            #
            # PixelRepresentation is the array's own signedness too
            # (#499). It does not constrain how many bytes `tobytes()`
            # emits, which is why it stood outside this rule until now --
            # but it decides what every reader makes of those bytes, so a
            # declaration that disagrees with them is not a descriptor
            # nit: measured, `int16 [-1, -2, -3]` declared 0 read back
            # 65535, 65534, 65533 uncompressed and 32767, 32766, 32765
            # under JPEG 2000, and `uint16 65535` declared 1 read back
            # -1. `set_pixel_data()` already writes this tag from
            # `dtype.kind` on the graph side (#386) and names this line
            # as the defect it could not reach; `_readback_pixel_mismatch`
            # already treats the array as the truth and this element as
            # the thing that is wrong about it (#449), which is why
            # `verify_readback=True` failed all four disagreements while
            # the default export wrote them in silence.
            #
            # `kind == 'i'` and not `kind != 'u'`: `bool` arrives here as
            # `'b'` -- the `view(np.uint8)` that widens a mask for the
            # encoder is inside `_compress_j2k`, below this -- and a mask
            # is unsigned.
            ds.BitsAllocated = arr.itemsize * 8
            ds.BitsStored, widened = _stored_width(arr, inst.attributes)
            ds.HighBit = ds.BitsStored - 1
            corrections.extend(_width_notes(
                widened, declared_int(inst.attributes, "0028,0102"),
                ds.BitsStored, ds.HighBit))
            ds.PixelRepresentation = 1 if arr.dtype.kind == "i" else 0
            declared_representation = declared_int(inst.attributes,
                                                   "0028,0103")
            if (declared_representation is not None
                    and declared_representation != ds.PixelRepresentation):
                # Read by `declared_int`, the one lenient-free reading of
                # a declared descriptor (#506): `[1]` is *not* unwrapped,
                # where the `.get()` this replaced handed pydicom a
                # one-element list that it unwraps for a US element. A
                # second, more lenient reading of one descriptor is how
                # two answers start to disagree.
                #
                # Nothing declared is not a correction of anything
                # (#468's rule), so the note is guarded on the
                # declaration and not on the value alone.
                corrections.append(
                    f"PixelRepresentation {declared_representation} "
                    f"({_SIGNEDNESS.get(declared_representation, _UNDEFINED_SIGNEDNESS)})"
                    f" is not the signedness of {arr.dtype} samples; written "
                    f"with PixelRepresentation {ds.PixelRepresentation} "
                    f"({_SIGNEDNESS[ds.PixelRepresentation]}), the array's "
                    f"own")

        # Waveform samples never reach `attributes` -- populate_attrs
        # routes (5400,1010) to the sidecar at every depth, whatever
        # its size (#151) -- so the rebuilt dataset carries a complete Waveform
        # Sequence (channel definitions, sampling frequency, sample count)
        # with no signal in it unless they are put back here (#34).
        #
        # The sidecar's bytes are written back verbatim rather than
        # re-encoded from the decoded array, because nothing in this
        # pipeline mutates waveform samples -- unlike pixels, which
        # redaction burns into a few lines above. A re-encode could
        # therefore only lose: it would have to undo the int16 rebasing
        # `decode_samples` applies to US, and any slip there shifts every
        # value by 32768 while (5400,1006) still says "US". Copying the
        # original bytes makes that mismatch structurally impossible
        # rather than merely tested against.
        #
        # Endianness is inherited, not assumed here: ingest never records
        # the source transfer syntax and `decode_samples` hardcodes
        # little-endian, so the whole pipeline already requires a
        # little-endian source. This adds no new assumption.
        if "WaveformSequence" in ds and len(ds.WaveformSequence) > 0:
            w_raw = inst.get_waveform_bytes()
            if w_raw:
                # Only group 0 is ingested (#36), so only group 0 can
                # be written -- and by the time the graph gets here it
                # is the only item there is, because `ingest_worker`
                # drops the items whose samples it discarded (#160).
                # Indexing [0] is therefore exhaustive, not a choice
                # among items: writing samples onto one item of several
                # is what left the rest declaring a Type 1 element they
                # did not carry.
                ds.WaveformSequence[0].WaveformData = w_raw
                written_waveform = w_raw
            else:
                # Structurally plausible and empty is the failure mode this
                # whole fix exists to end; if it is still reachable -- a
                # source that never carried samples -- say so rather than
                # writing the file in silence.
                #
                # This is the export side of the same loss #36 records at
                # ingest, so it rides the same channel (#126). Only the
                # empty-samples case: the multiplex-group loss above is
                # reported at ingest and is not re-reported here.
                #
                # Scoped STANDARD: what is missing is Waveform Data
                # (5400,1010), an even group, and unlike the ingest-side
                # multiplex loss -- scoped SIGNAL since #150 -- nothing
                # was discarded by this pipeline. This branch is
                # reachable only from a source that never carried
                # samples, so the export is not smaller than the
                # acquisition; it is the acquisition, said out loud.
                losses.append((
                    LOSS_SCOPE_STANDARD,
                    "Waveform Sequence present but no samples are available "
                    "to export; the written file will describe a waveform it "
                    "does not contain."))

        # No underscore-key cleanup here, deliberately. `_merge` drops
        # every `_`-prefixed bookkeeping key (`_ISOCENTER_REDACTION_HASH`,
        # `_ISOCENTER_SOURCE_SOP_UID`) before `ds` ever sees it, so there
        # is nothing to delete -- and asking a pydicom Dataset about a
        # string that is neither a tag nor a keyword emits a UserWarning
        # on the *caller's* stream, once per exported instance, because
        # this package installs no global filter (#144). A defensive
        # `if key in ds: del` reintroduces that noise and guards nothing;
        # measured in `test_export_redaction_hash_warning.py` (#248).

        # The integer branch's pixel element is written from `arr`, as
        # bytes above or by the encoder inside `_finalize_dataset`, so
        # this is the array the readback holds the file against (#449).
        # After the redaction block and the geometry, and a reference,
        # not a copy. `arr` is None here after the float branch, which
        # took its own capture, and for an instance with no pixels.
        if arr is not None:
            written_pixels = arr

        # Validate & Save
        ds = DicomExporter._finalize_dataset(ds, ctx.compression, pixel_array=arr)

        # A limit of ours, not a defect in the data (#596, #461): a
        # 16-bit or signed 8-bit YBR_FULL file is conformant and is
        # written, and this library cannot read it back -- pydicom's
        # colour conversion takes unsigned 8-bit samples only. Keyed on
        # the samples' dtype, the predicate pydicom itself applies and the
        # one the readback's second decode is gated on, not on
        # BitsAllocated: int8 is BitsAllocated 8, and a `> 8` key left it
        # silent. INFO on `corrections`, no row. After
        # `_finalize_dataset` and off `ds`, so the label is the written
        # one (`YBR_FULL_422` is already `YBR_FULL`) and a JPEG 2000 file,
        # which keeps `YBR_FULL` (`_compress_j2k` case 3), is covered in
        # the same words. Keyed on the integer arm having written, rather
        # than on `"PixelData" in ds`: a float arm cannot carry a colour
        # label at all (#222).
        if (written_pixels is not None and written_pixels.dtype.kind != "f"
                and _written_photometric(ds.get("PhotometricInterpretation"))
                in _PYDICOM_CONVERTS
                and not _pydicom_converts_samples_of(written_pixels.dtype)):
            corrections.append(
                f"PhotometricInterpretation "
                f"{_written_photometric(ds.PhotometricInterpretation)} at "
                f"BitsAllocated {ds.BitsAllocated} and PixelRepresentation "
                f"{ds.get('PixelRepresentation', 0)} is written as declared, "
                f"and this library cannot read such a file back (#461): "
                f"pydicom's colour conversion takes unsigned 8-bit samples "
                f"only.")

        # The third arm's label judgement (#534): a file with no pixel
        # element never reaches `_write_pixel_geometry`, which judges the
        # other two. After `_finalize_dataset`, because the syntax judged
        # has to be the one in `ds.file_meta` -- a pixel-less file stays
        # native under `compression="j2k"`, where `written_syntax` says
        # JPEG 2000 -- and before `save_as`, so the sentence rides the
        # outcome of the file it describes. An `IODValidator` refusal
        # raises first and the instance fails with its own ERROR, which is
        # the right order: a warning describes a file that was written.
        #
        # Keyed on the arm -- no pixel arm set `written_pixels`, which is
        # exactly "`_write_pixel_geometry` was not called". Since #605 that
        # is also exactly "no pixel element is in the file": only None and
        # `"j2k"` reach the integer arm, and each writes the element (the
        # raw bytes, or the encoder), so the arm that set `written_pixels`
        # always wrote one. It was not before -- `compression="rle"` set it
        # and wrote nothing, so this judgement was skipped for a file with
        # no pixels and the #596 note above described pixels the file did
        # not carry. Keep the key on the arm regardless: the arm is what
        # decides which of the two label judgements ran.
        if written_pixels is None:
            warning = _pixel_less_label_warning(ds)
            if warning is not None:
                warnings.append(warning)

        # Ensure dir exists (race safe)
        os.makedirs(os.path.dirname(ctx.output_path), exist_ok=True)

        # Write under a temporary name and rename into place only once
        # the write has finished. `save_as` creates the file first and
        # streams elements into it in ascending tag order, so a raise
        # part-way used to leave a *readable* partial under the real
        # name: `dcmread` accepts a dataset that simply stops, and
        # Pixel Data (7FE0,0010) is written last, so the element most
        # often missing was the largest and the least visible (#199).
        # The temp lives in the destination directory because that is
        # what keeps the rename atomic -- same filesystem, one
        # directory-entry swap.
        #
        # The cleanup is here, in the worker, because this is the only
        # frame that knows a write started and did not finish -- and the
        # worker is always a subprocess (`_run_export_batch` recycles
        # them every 25 tasks), so it cannot lean on parent state. The
        # pid suffix keeps recycled and concurrent workers off each
        # other's temp files. A worker killed outright can still orphan
        # one `.tmp`; that residue is what atomicity costs, and what can
        # no longer exist is a partial under a name a recipient trusts.
        tmp_path = f"{ctx.output_path}.{os.getpid()}.tmp"
        try:
            ds.save_as(tmp_path, enforce_file_format=True)
            # Before the rename, so a file that fails verification is
            # never published under its real name -- see
            # `_verify_readback` (#209).
            if ctx.verify_readback:
                _verify_readback(tmp_path, ds, written_pixels,
                                 written_waveform)
            os.replace(tmp_path, ctx.output_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass  # save_as raised before creating it
            raise
        return ExportOutcome(ok=True, output_path=ctx.output_path,
                             sop_instance_uid=uid, losses=losses,
                             corrections=corrections,
                             warnings=warnings)
    except Exception as e:
        # Do not raise, as it aborts the entire parallel batch.
        # Report the failure back for the parent to count and raise on.
        #
        # The console line names the instance, not `ctx.output_path`, and
        # spells the exception without its path: the output path is
        # `Subject_<Patient ID>/...`, and an OSError's text repeats it
        # (P8, bunch E; the WFDB half is #588). The parent's `ERROR` row,
        # `_report_export_failures`', has the same shape.
        named = f"instance {uid}" if uid else _NO_SOP_UID
        print(f"ERROR: Export failed for {named}: "
              f"{describe_exception_without_paths(e)}", file=sys.stderr)
        return ExportOutcome(ok=False, output_path=ctx.output_path,
                             sop_instance_uid=uid, losses=losses,
                             corrections=corrections,
                             warnings=warnings, error=e)


class _J2kFrameRefusal(RuntimeError):
    """A frame this project cannot compress and read back honestly.

    A `RuntimeError` subclass, so the export worker and every existing
    caller handle it exactly as they handled the old `Compression failed`
    -- the exception type, the `ExportError`, the `wrote 0 of N` and the
    empty output directory are all unchanged, and only the sentence
    improves. It is a distinct type for one reason: so `_compress_j2k`'s
    outer handler can re-raise it instead of wrapping it into
    `Compression failed: <our own sentence>`.
    """


#: The frames this project can compress **and read back**, keyed on
#: `(itemsize, multi-sample)`. Measured end to end -- ingest, export,
#: read back, compare against the literal:
#:
#: | itemsize | samples 1 | samples 3 |
#: | --- | --- | --- |
#: | 1 (`uint8`, `int8`) | exact | exact |
#: | 2 (`uint16`, `int16`) | exact | exact, read back through `imagecodecs` |
#: | 4, 8 | refused, see below | refused, see below |
#:
#: A **positive** rule, because neither the codec's refusals nor the
#: encoder's exactness marks the boundary of what is safe to write:
#: 32-bit encodes *silently* to 25 bits, so a deny-list would have had
#: to anticipate it and would not have. The standard is what this
#: library can read back, which `_refuse_unencodable_j2k_frame` states.
#:
#: **The `(2, True)` cell is where that standard moved (#416).**
#: `imagecodecs` always encoded 16-bit multi-sample frames exactly, but
#: Pillow -- the only JPEG 2000 plugin pydicom has here -- reports
#: `Pillow cannot decode 16-bit multi-sample data correctly`, so
#: `session.ingest()` on this library's own export returned `ingested=0`
#: and the cell was refused, on the same standard as 32-bit. Ingest now
#: falls back to `imagecodecs` when pydicom cannot decode
#: (`_decode_pixels`), which reads those frames bit-exactly, so the cell
#: meets the standard. Measured on imagecodecs 2026.8.16, on 3.12 and
#: 3.14t, for `uint16` and `int16`, one and two frames, and pinned by
#: `test_our_own_compressed_16_bit_colour_export_re_ingests`. It holds at
#: the install floor too: imagecodecs 2024.6.1, pydicom 3.0.2 and numpy
#: 2.2.6, measured in review of #451 (85 passed across the suites that
#: reach this cell). **Before
#: narrowing `_IMAGECODECS_FALLBACK_SYNTAXES` to exclude JPEG 2000,
#: remove this cell**, or the export writes a file ingest refuses again.
#:
#: What the cell costs: a third-party reader with pydicom and only
#: Pillow still cannot decode such a file's `pixel_array`. A caller
#: exporting for one passes `use_compression=False`, as before.
#: `verify_readback=True` decodes every written file through
#: `_decode_pixels` since #449; `(2, True)` passes it through the
#: imagecodecs fallback, where `pixel_array` with only Pillow raises.
_J2K_ENCODABLE_FRAMES = frozenset({
    (1, False),
    (1, True),
    (2, False),
    (2, True),
})

#: The 3-sample labels `_compress_j2k` encodes **with** the multiple
#: component transform (#490). `RGB` is transformed and relabelled
#: `YBR_RCT`; `YBR_RCT` and `YBR_ICT` are transformed and keep the label
#: they came with, because such an instance's samples were already
#: inverse-transformed into memory by the decode that produced them, so
#: applying the transform on re-encode is what makes the label true of
#: the codestream. PS3.5 8.2.4 is symmetric about this: a transformed
#: codestream must carry one of these labels, and an untransformed one
#: must carry components matching its Photometric Interpretation. Every
#: other label -- `YBR_FULL`, `YBR_PARTIAL_420`, `YBR_PARTIAL_422`, and
#: every 1-sample label including `PALETTE COLOR` -- encodes `mct=False`.
#:
#: **That list is this function's, not the worker's (#528).** Through the
#: export worker, a 3-sample `PALETTE COLOR` has already become `RGB`
#: (case 1) and a 3-sample `YBR_FULL_422` has become `YBR_FULL` (#470),
#: because `_write_pixel_geometry` runs before the encoder; neither
#: reaches this function under its own name. A direct caller can still
#: pass either, which is what the unit tests do.
_J2K_MCT_SOURCES = frozenset({"RGB", "YBR_RCT", "YBR_ICT"})


def _heuristic_lengths(ds, frames, samples) -> Tuple[int, ...]:
    """The encapsulated lengths pydicom mistakes for uncompressed data (#473).

    pydicom 3.0.2 `DecodeRunner._validate_buffer` warns for
    `actual in (expected, expected + expected % 2)`, where `expected` is
    `frame_length(unit="bytes") * number_of_frames` under the file's own
    transfer syntax. The runner computes it here rather than a formula
    beside it, so a pydicom that changes the arithmetic changes this
    too. `DecodeRunner` is imported from its module because `pydicom.pixels`
    does not re-export it; the `<4.0` cap in `setup.py` is what keeps the
    path stable.

    Two of `frame_length`'s branches cannot be reached from the encoder:
    BitsAllocated 1 (a bool mask is written at 8, see the `view` above
    the frame guard) and the native `YBR_FULL_422` correction (the
    runner is told the syntax is JPEG 2000, and the worker has rewritten
    that label to `YBR_FULL` before the encoder). `ds.file_meta` still
    says Implicit VR LE at this point, which is why the syntax is given,
    not read.

    The import is the module-scope one `_validate_like_pydicom` uses
    (#453): one site to fix if pydicom ever moves the class.
    """
    runner = DecodeRunner(JPEG2000Lossless)
    runner.set_options(rows=int(getattr(ds, "Rows", 0) or 0),
                       columns=int(getattr(ds, "Columns", 0) or 0),
                       samples_per_pixel=int(samples),
                       bits_allocated=int(getattr(ds, "BitsAllocated", 0) or 0),
                       number_of_frames=int(frames),
                       # Read by `frame_length` before it asks whether
                       # the syntax is encapsulated; the value cannot
                       # change the answer under JPEG 2000.
                       photometric_interpretation=str(getattr(
                           ds, "PhotometricInterpretation", "") or ""))
    expected = ceil(runner.frame_length(unit="bytes")
                    * runner.number_of_frames)
    return (expected, expected + expected % 2)


def _refuse_unencodable_j2k_frame(arr, ds, samples):
    """Raise before the encode, naming what the codec will not say.

    Called with the array in its final shape and dtype -- `bool` already
    viewed as `uint8` -- and **before any mutation of `ds`**, so a refused
    instance leaves this function exactly as it entered it.
    """
    if (arr.dtype.itemsize, samples > 1) in _J2K_ENCODABLE_FRAMES:
        return

    # Every 8- and 16-bit cell is encodable since #416, so what reaches
    # here is 32- or 64-bit, and this is the one reason that applies.
    why = (f"the encoder (imagecodecs {imagecodecs.__version__}) is "
           f"exact only to 25 bits, so a 32-bit frame would be written "
           f"wrong and read back wrong, and a 64-bit frame is refused "
           f"by the codec outright")

    raise _J2kFrameRefusal(
        f"Compression failed: JPEG 2000 lossless cannot carry "
        f"{arr.dtype} pixel data at {samples} sample(s) per pixel "
        f"(BitsAllocated "
        f"{getattr(ds, 'BitsAllocated', arr.dtype.itemsize * 8)}, "
        f"PixelRepresentation "
        f"{getattr(ds, 'PixelRepresentation', 1 if arr.dtype.kind == 'i' else 0)})"
        f". Here {why}. Export this study with use_compression=False, "
        f"which writes the same pixels uncompressed and bit-exact.")


def _compress_j2k(ds, pixel_array=None):
    """Compress the dataset's pixels as JPEG 2000 Lossless, in place.

    The encoder is `imagecodecs.jpeg2k_encode(frame, level=0,
    codecformat="J2K")`. `level=0` is lossless -- measured reversible on a
    full-range `int16` frame and identical to the explicit
    `reversible=True` -- and `codecformat="J2K"` emits a **bare
    codestream**, starting `ff4f ff51`, which is what transfer syntax
    1.2.840.10008.1.2.4.90 names. Pillow, which stood here until #404,
    wrote a JP2 *box* (`0000000c6a502020`) under that same syntax; lenient
    decoders read it, which is why it went unnoticed for every release.

    **Accepted, and measured bit-exact end to end: 8-bit and 16-bit at
    any number of samples per pixel, and `bool` (encoded as `uint8`).**
    Pillow accepted exactly `uint8` and `uint16` greyscale, so
    `session.export(folder)` -- which compresses by default -- wrote
    *nothing at all* for CT and MR, failing with `broken data stream when
    writing image file` (#404). 16-bit multi-sample was refused until
    #416: it encoded exactly, but this library could not ingest the file
    it wrote until ingest gained an `imagecodecs` fallback. See
    `_J2K_ENCODABLE_FRAMES`.

    32- and 64-bit are refused by name before any encode, and that refusal
    is the safety of this function rather than a rough edge: `imagecodecs`
    does not reject 32-bit, it encodes exactly to 25 bits and wrong above
    that, which would replace a loud failure with `wrote 1 of 1` beside a
    file written wrong.

    **The multiple-component transform and the label are decided
    together, in three cases (#490).** A 3-sample `RGB` source is
    transformed and declared `YBR_RCT`; a 3-sample source *already*
    labelled `YBR_RCT` or `YBR_ICT` is transformed and keeps that label;
    every other source -- any other 3-sample label, and every 1-sample
    one -- is encoded `mct=False` and keeps its label. PS3.5 8.2.4 binds
    the two in both directions, which is why the middle case transforms
    rather than leaving a label that would describe a transform the
    codestream does not carry. See `_J2K_MCT_SOURCES` and the note at the
    encoder.

    Updates `TransferSyntaxUID`, `PixelData` and, for the `RGB` case
    alone, `PhotometricInterpretation`; mutates nothing when it refuses.

    Args:
        ds (pydicom.Dataset): the dataset to compress, in place.
        pixel_array (np.ndarray, optional): the frame(s) to encode. When
            None there is nothing to compress and this returns having done
            nothing -- see the comment at the guard.
    """
    try:
        arr = pixel_array
        if arr is None:
            # Nothing to compress, and that is the whole meaning of the
            # branch. It used to rebuild the array from `ds.PixelData`,
            # reading those bytes as `uint16` regardless of
            # `PixelRepresentation` -- a silent-corruption sibling of #386
            # that would have compressed signed data to wrong values
            # without raising.
            #
            # Deleted rather than corrected, because it is unreachable:
            # `_compress_j2k`'s only caller is `_finalize_dataset`, whose
            # only caller is the export worker, which always passes
            # `pixel_array=arr`; and when compressing the worker
            # never assigns `ds.PixelData` at all, which the comment at
            # the `arr is not None` block above already says in those
            # words. The one path that arrives here with `arr is None` is
            # the float branch, which has deleted (7fe0,0010) and wants
            # the file written uncompressed per PS3.5 8.2.
            #
            # So: return, leaving `ds` exactly as it was. Restoring a
            # reconstruction here would reintroduce a decoder that can
            # disagree with the loader.
            return
        else:
            # Array passed explicitly.
            # Handle Flattened (1D)
            if len(arr.shape) == 1:
                frames = getattr(ds, "NumberOfFrames", 1)
                rows = getattr(ds, "Rows", 0)
                cols = getattr(ds, "Columns", 0)
                samples = getattr(ds, "SamplesPerPixel", 1)

                try:
                    target_shape = None
                    if frames > 1:
                        target_shape = (
                            frames, rows, cols, samples) if samples > 1 else (
                            frames, rows, cols)
                    else:
                        target_shape = (rows, cols, samples) if samples > 1 else (rows, cols)

                    if target_shape:
                        arr = arr.reshape(target_shape)
                except Exception as e:
                    # If reshape fails, we MUST fail export. Continuing with 1D array is dangerous.
                    # This explains the "tuple index out of range" crash when iterating 1D
                    # array as frames.
                    raise RuntimeError(
                        f"Array shape mismatch. Expected {target_shape} for {
                            arr.size} elements. Error: {e}")

            frames = getattr(ds, "NumberOfFrames", 1)
            samples = getattr(ds, "SamplesPerPixel", 1)

            # Robust Squeeze Logic for Single Sample/Single Frame Edge Cases
            # Pillow prefers (H, W) over (H, W, 1) or (1, H, W) for grayscale.
            if samples == 1:
                if frames == 1:
                    # Expect (H, W) or (1, H, W) or (H, W, 1)
                    if len(arr.shape) == 3:
                        if arr.shape[0] == 1:
                            arr = arr.squeeze(0)  # (1, H, W) -> (H, W)
                        elif arr.shape[-1] == 1:
                            arr = arr.squeeze(-1)  # (H, W, 1) -> (H, W)
                elif frames > 1:
                    # Expect (Frames, H, W) or (Frames, H, W, 1)
                    if len(arr.shape) == 4 and arr.shape[-1] == 1:
                        arr = arr.squeeze(-1)  # (F, H, W, 1) -> (F, H, W)

        # 3. Compress
        frames_data = []

        # A bool frame is stored, declared and exported as 8-bit: the
        # dtype carrier keeps `bool` because no DICOM descriptor can name
        # it (#386), but `set_pixel_data` still writes BitsAllocated 8 and
        # PixelRepresentation 0, and the uncompressed path writes
        # `arr.tobytes()` -- one byte per element. `view` rather than
        # `astype` says exactly that: the same bytes, read under the dtype
        # the file declares, with no copy and no conversion to get wrong.
        # The codec refuses kind `b` outright
        # (`ValueError: sample format not supported by codec`), so without
        # this a mask that compressed cleanly while it reloaded as `uint8`
        # would have started failing the *default* export the moment the
        # carrier began reloading it as `bool`.
        if arr.dtype.kind == 'b':
            arr = arr.view(np.uint8)

        # **The frame guard, and it runs before any encode and before any
        # mutation of `ds`.** It is a positive rule -- encode only what is
        # measured to survive a round trip -- because neither the codec's
        # refusals nor its exactness lines up with what is safe to write.
        #
        # 64-bit it does refuse, but with a different sentence on
        # different releases (`ValueError: item size not supported by
        # codec` on 2026.8.16, `Jpeg2kError: opj_encode or opj_write_tile
        # failed` on 2024.6.1), so a message a user can act on cannot come
        # from the codec.
        #
        # 32-bit it does **not** refuse, and that is the dangerous half:
        # it encodes exactly to 25 bits and wrong above that, and the
        # DICOM file built from a 32-bit codestream raises
        # `RuntimeError: Unable to decode as exceptions were raised by all
        # available plugins` on read. Without this guard the encode
        # succeeds, a file is written, and the audit log says
        # `wrote 1 of 1` beside a file that was **written wrong and would
        # be read back wrong** -- the loss is at the encoder, so a decoder
        # that *does* open the codestream (`imagecodecs.jpeg2k_decode`)
        # hands back the right dtype and shape with silently wrong values
        # rather than raising. Worse than unreadable, not milder. A
        # silence created by the fix for a silence (#404).
        #
        # 16-bit **multi-sample** was refused here too until #416, for
        # the other half of the same standard: `imagecodecs` encodes it
        # bit-exactly, but pydicom's only J2K plugin here (Pillow) cannot
        # decode it, so this library could not ingest its own export. It
        # is written now because ingest falls back to `imagecodecs`; see
        # `_J2K_ENCODABLE_FRAMES`. `int8` RGB, which Pillow also refused,
        # *is* exact and was never refused by this guard.
        _refuse_unencodable_j2k_frame(arr, ds, samples)

        # **The multiple-component transform is the label, and both are
        # decided here (#490).** `jpeg2k_encode`'s default turns MCT on
        # for every 3-component frame (measured: the `COD` segment's
        # transform byte is 1 for `uint8`, `uint16` and a YBR_FULL
        # source alike). PS3.5 8.2.4 gives that codestream exactly two
        # Photometric Interpretations -- `YBR_RCT` for the reversible
        # transform, `YBR_ICT` for the irreversible one -- so writing
        # MCT under an `RGB` label, which is what this did for every
        # colour export, is non-conformant: a reader that believes the
        # label is told no transform was applied. The codestream carries
        # its own flag, so the readers here recover the samples anyway
        # (pydicom with Pillow, `opj_decompress` and
        # `imagecodecs.jpeg2k_decode` all read maxdiff 0 under either
        # label), which is why it went unnoticed. `YBR_RCT` and not
        # `YBR_ICT`: `level=0` is the reversible transform, and it is the
        # only encode this function makes.
        #
        # **Three cases, and the rule is symmetric (#490, N2).** PS3.5
        # 8.2.4 binds the transform and the label in both directions: an
        # MCT codestream must say `YBR_RCT`/`YBR_ICT`, and with no
        # transformation applied the components shall correspond to those
        # the Photometric Interpretation specifies. So:
        #
        # 1. An `RGB` source is transformed and **relabelled** `YBR_RCT`.
        #    MCT decorrelates RGB, so it earns its place here (`mct=0` /
        #    `mct=1` size ratios 1.105-2.195 across five sources).
        # 2. A source **already** labelled `YBR_RCT`/`YBR_ICT` is
        #    transformed and **keeps its label**. Such an instance can
        #    only have come from a J2K source whose samples this library
        #    already inverse-transformed into memory, so applying the
        #    transform on re-encode is what makes the label true of this
        #    codestream. Encoding it `mct=False` under the label it keeps
        #    -- which is what #490 first shipped -- declares RCT over a
        #    `COD` transform byte of 0: the same non-conformance,
        #    mirrored. Not relabelled to `RGB`, which would discard the
        #    source's stated colour space against #482's work, and not
        #    refused, which no ingested file would reach.
        # 3. **Every other label** -- `YBR_FULL`, `YBR_PARTIAL_420`,
        #    `YBR_PARTIAL_422`, and every 1-sample label including
        #    `PALETTE COLOR` -- is encoded `mct=False` and keeps its
        #    label. Through the export worker, a 3-sample `PALETTE COLOR`
        #    has already become `RGB` (case 1) and a 3-sample
        #    `YBR_FULL_422` has become `YBR_FULL` (#470), so neither
        #    reaches this function under its own name; a direct caller
        #    can still pass either (#528). `YBR_PARTIAL_*` is a label the
        #    export also warns about (#525): the encoder does not decide
        #    admissibility. A luma/chroma source is already
        #    decorrelated, so MCT over it is both unnameable and larger:
        #    measured on a `YBR_FULL` frame, `mct=True` is 66482 bytes against
        #    `mct=False`'s 48819, a 36% loss for a file that would also
        #    be mislabelled.
        #
        # `mct` is passed in both directions rather than left default, so
        # the flag in the file is this decision and never the codec's
        # (whose default is MCT on for every 3-component frame).
        #
        # Keyed on the sample count and the declared label, before any
        # encode. The relabel is written after the last frame encodes,
        # beside the transfer syntax, because this function mutates
        # nothing when it refuses.
        photometric = str(getattr(ds, "PhotometricInterpretation", "") or "")
        mct = samples == 3 and photometric in _J2K_MCT_SOURCES

        def encode_frame(frame_arr):
            """One frame to a bare JPEG 2000 lossless codestream."""
            # `codecformat="J2K"` rather than the default: the transfer
            # syntax names a codestream, not a JP2 file. `level=0` is
            # lossless.
            return jpeg2k_encode(frame_arr, level=0, codecformat="J2K",
                                 mct=mct)

        if frames > 1:
            for i in range(frames):
                frames_data.append(encode_frame(arr[i]))
        else:
            frames_data.append(encode_frame(arr))

        # **An offset table, unless it makes the length a lie (#473).**
        # pydicom's decoder warns "the number of bytes of compressed pixel
        # data matches the expected number for uncompressed data" when an
        # encapsulated value's length falls in its window, and small
        # low-entropy frames land there by chance -- about 1 in 5 random
        # 16x16 bool masks, measured -- so `verify_readback=True`, and
        # every later reader, put a false "check the transfer syntax"
        # warning on the caller's stream about a correct file. An empty
        # Basic Offset Table is PS3.5 A.4-legal and shortens the value by
        # 4 bytes per frame; the window is `expected`, or `expected + 1`
        # when that is odd, so the two encapsulations cannot both fall in
        # it. Suppressing the warning instead was ruled out in #472: on
        # 3.12 `catch_warnings` mutates the process-global filters, and
        # this runs on threads.
        #
        # The trade, for exactly these files: no table, so the table-based
        # frame check reads nothing (`offset_table_frame_count` answers
        # None) and a reader falls back to one fragment per frame, which is
        # what this encoder writes. The readback still compares every
        # frame's samples.
        encapsulated = encapsulate(frames_data)
        if len(encapsulated) in _heuristic_lengths(ds, frames, samples):
            encapsulated = encapsulate(frames_data, has_bot=False)
        ds.PixelData = encapsulated
        # ds.TransferSyntaxUID = JPEG2000Lossless # REMOVE: Group 2 tags must be in file_meta only
        # The transfer syntax is the encoding. `is_implicit_VR` and
        # `is_little_endian` are not set alongside it: pydicom derives
        # both from the UID and removes the attributes in 4.0 (#141).
        ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
        if mct and photometric == "RGB":
            # The label for the transform the codestream now carries
            # (#490 case 1, see the note above). Only the `RGB` source is
            # relabelled: a source already labelled `YBR_RCT`/`YBR_ICT`
            # (case 2) is transformed too, and its own label is already
            # true of the codestream -- rewriting `YBR_ICT` to `YBR_RCT`
            # would name the wrong transform for a label this function
            # did not choose. Written here, after every frame encoded, so
            # a refusal leaves `ds` saying `RGB` over the pixels it still
            # has. Re-ingesting this file relabels it `RGB` again,
            # because `jpeg2k_decode` undoes the transform and
            # `DECODER_RELABELS` says so (#448, #482) -- the round trip
            # is stable in both directions.
            ds.PhotometricInterpretation = "YBR_RCT"

    except _J2kFrameRefusal:
        # Re-raised unchanged, ahead of the generic handler below. Wrapped
        # it would read `Compression failed: Compression failed: ...`, and
        # the whole point of the refusal is that the sentence the user
        # reads is ours rather than the codec's.
        raise
    except Exception as e:
        raise RuntimeError(f"Compression failed: {e}")


class SidecarPixelLoader:
    """
    Functor for lazy loading of pixel data from sidecar.

    Must be a top-level class to be picklable.
    Breaks reference cycles by storing primitive metadata (snapshot) instead of the Instance object.
    Designed to be lightweight and serializable for IPC.
    """

    def __init__(self, sidecar_path, offset, length, alg, instance=None, metadata=None, pixel_hash=None):
        self.sidecar_path = sidecar_path
        self.offset = offset
        self.length = length
        self.alg = alg

        # We need metadata to reshape safely.
        # Prefer direct metadata check, fallback to instance extraction.
        if metadata:
            self.sop_instance_uid = metadata.get("sop_instance_uid", "Unknown")
            self.rows = metadata.get("rows", 0) or 0
            self.cols = metadata.get("cols", 0) or 0
            self.samples = metadata.get("samples", 1) or 1
            self.frames = metadata.get("frames", 0) or 0
            self.bits = metadata.get("bits", 8) or 8
            self.pixel_representation = metadata.get("pixel_representation", 0) or 0
            self.pixel_hash = metadata.get("pixel_hash", None)
            self.pixel_dtype = metadata.get("pixel_dtype", None)
        elif instance:
            (self.sop_instance_uid, self.rows, self.cols, self.samples,
             self.frames, self.bits, self.pixel_representation,
             self.pixel_dtype) = self._descriptors_from(instance)
            self.pixel_hash = pixel_hash or getattr(instance, "_pixel_hash", None)
        else:
            raise ValueError("SidecarPixelLoader requires either 'instance' or 'metadata'")

    @staticmethod
    def _descriptors_from(instance) -> tuple:
        """Every descriptor `__call__` reads, as the instance holds it now.

        The one place the capture is taken, so `__init__` and `describes`
        cannot drift apart: a field added to one and not the other would
        be a descriptor the loader reads and never re-checks (#417).

        Exactly the fields `__call__` reads, and no others.
        PhotometricInterpretation, BitsStored and HighBit change no
        reading here; PlanarConfiguration went with #210. The SOP
        Instance UID is compared too, only so that after
        `regenerate_uid` an Integrity Error names the UID the caller now
        knows the instance by. That is not free: after `regenerate_uid()`
        with no save to rebind the loader, every read for the rest of the
        session rebuilds (about 0.6 us each, measured), which the pipeline
        never sees because it always persists after regenerating a UID.
        The float carrier (`PIXEL_DTYPE_ATTR`) is
        read from the instance rather than derived, because no DICOM
        descriptor says "float" (#183); `set_attr` lowercases its key
        and so cannot reach it, but a direct write can.
        """
        # One snapshot, then every field from it. Seven separate
        # `attributes.get` calls could straddle a concurrent
        # `attributes.update(...)` and read one layout's Rows with the
        # other's Columns -- a geometry neither layout declared. Measured
        # on 3.14t with the GIL off: a writer flipping between 4x4 and
        # 2x8, both valid for the stored bytes, made 10.3% of reads raise
        # an Integrity Error. `dict()` copies a dict's storage in one step
        # without calling `.get`, so the capture is one layout or the
        # other. Pinned by tests/test_descriptor_edit_with_pixels_unloaded.py::
        # test_the_descriptors_are_read_from_one_snapshot.
        return ((instance.sop_instance_uid,)
                + SidecarPixelLoader._descriptors_of(dict(instance.attributes)))

    @staticmethod
    def _descriptors_of(attrs) -> tuple:
        """`_descriptors_from` without the UID, from a mapping the caller owns.

        Takes a snapshot, not an instance: `Instance.set_attr` asks what an
        edit *would* read as before it writes it (#531), so it passes the
        attributes with the edit applied to a copy. Raises `ValueError` or
        `TypeError` for a descriptor that does not parse as an integer, as
        the constructor always has.
        """
        return (int(attrs.get("0028,0010", 0) or 0),
                int(attrs.get("0028,0011", 0) or 0),
                int(attrs.get("0028,0002", 1) or 1),
                int(attrs.get("0028,0008", 0) or 0),
                int(attrs.get("0028,0100", 8) or 8),
                int(attrs.get("0028,0103", 0) or 0),
                attrs.get(PIXEL_DTYPE_ATTR))

    @staticmethod
    def reading_of(attrs) -> tuple:
        """The `(dtype, shape)` a frame stored under `attrs` is read as.

        **The one statement of the loader's reading rule**, used by
        `__call__` and by `Instance.set_attr` (#531). A pixel-descriptor
        edit on an instance whose pixels are resident has to know whether
        a save and reload would read those bytes differently, and a second
        copy of this rule in `entities.py` would be a reading the loader
        does not make -- the drift #417 closed for the capture.

        Raises `ValueError` or `TypeError` when a descriptor does not
        parse as an integer.
        """
        return SidecarPixelLoader._reading(
            SidecarPixelLoader._descriptors_of(attrs))

    @staticmethod
    def _reading(descriptors) -> tuple:
        """`reading_of`, from descriptors already parsed.

        `descriptors` is `_descriptors_of`'s tuple, in its order.
        """
        (rows, cols, samples, frames, bits, pixel_representation,
         pixel_dtype) = descriptors
        # A recorded carrier dtype first: no DICOM descriptor says
        # "float" -- a 32-bit float frame and a 32-bit integer frame both
        # declare BitsAllocated 32 -- and none says "bool" either, since
        # numpy `bool_` and `uint8` both declare BitsAllocated 8 with
        # PixelRepresentation 0. A frame whose dtype is one of those can
        # only be rebuilt from a dtype that was carried, never from one
        # that was inferred (#183, #386). Checked against the allow-list,
        # because this string comes back out of the store and
        # `np.dtype(anything)` is not a thing a loader should do.
        if pixel_dtype in SIDECAR_DTYPE_NAMES:
            dt = np.dtype(pixel_dtype)
        else:
            # BitsAllocated crossed with PixelRepresentation, which
            # between them name every integer dtype the sidecar can hold
            # -- and the legacy bucketing as the fallback, because
            # `BitsAllocated` 1 and 12 are real ingested populations that
            # this table has no row for and must keep decoding as they do
            # (#386). Do not subscript `_INTEGER_DTYPE_BY_BITS` here.
            dt = np.dtype(_integer_dtype(bits, pixel_representation))

        # **Isocenter holds and stores pixels interleaved, always.** The
        # sidecar can hold nothing else: pydicom de-planarises on read,
        # so `ingest_worker` extracts an interleaved `ds.pixel_array`
        # from a planar source too, `set_pixel_data()` is handed an
        # interleaved-shaped array (`resolve_pixel_geometry` reads
        # `(rows, cols, samples)`), and `persist_pixel_data` writes
        # `arr.tobytes()` of that. Measured on pydicom 3.0.2: a 2-frame
        # 3x3 RGB file with PlanarConfiguration 1 and planar bytes
        # 0..53 gives `pixel_array.shape == (2, 3, 3, 3)` and
        # `ravel() == [0 9 18 1 10 19 ...]` -- interleaved.
        #
        # So there is no planar branch here and there must not be one. A
        # (0028,0006) of 1 in `attributes` describes the *source file*,
        # never the frame this reads, and reshaping as
        # `(samples, rows, cols)` plus a transpose -- which is what
        # stood here -- returned a transposed image for every
        # single-frame colour instance carrying a declared 1. The
        # multi-frame arm never had the branch, which is why it was the
        # correct one; #210's issue text has that inverted and its
        # Option 1 would have made both arms wrong. `self.planar_conf`
        # went with the branch: a field nothing reads is a second
        # answer waiting to disagree with this one. (#210)
        if frames > 1:
            target_shape = (frames, rows, cols, samples)
            if samples == 1:
                target_shape = (frames, rows, cols)
        elif samples > 1:
            target_shape = (rows, cols, samples)
        else:
            target_shape = (rows, cols)
        return dt, target_shape

    def describes(self, instance) -> bool:
        """Whether this loader's capture still matches `instance` (#417).

        The capture is taken once, at construction, and `__call__`
        rebuilds every frame from it rather than from the instance. A
        descriptor written since -- by `set_attr`, or by any of the
        writers that go straight to `attributes` -- leaves the capture
        describing an instance that no longer exists, and the live
        session then read the stored bytes differently from the same
        store reopened. `Instance.get_pixel_data` asks this on every
        read and, on False, reads through `for_instance` instead.
        """
        return (self.sop_instance_uid, self.rows, self.cols, self.samples,
                self.frames, self.bits, self.pixel_representation,
                self.pixel_dtype) == self._descriptors_from(instance)

    def for_instance(self, instance) -> "SidecarPixelLoader":
        """The same stored bytes, read under `instance`'s descriptors now.

        The hash is **copied, not re-derived** -- including a None. The
        bytes at this offset did not move, so the integrity question is
        the one this loader was already answering. It is assigned after
        construction rather than passed as `pixel_hash=`, because the
        constructor treats a falsy hash as missing and falls back to
        `instance._pixel_hash`, which can drift from the bytes at this
        offset; that fallback produced #212 once already.

        Not stored on the instance by anything: see the loader arm of
        `Instance.get_pixel_data` for why a read must not write the slot.
        """
        fresh = SidecarPixelLoader(self.sidecar_path, self.offset,
                                   self.length, self.alg, instance=instance)
        fresh.pixel_hash = self.pixel_hash
        return fresh

    def __call__(self):
        mgr = SidecarManager(self.sidecar_path)

        try:
            raw = mgr.read_frame(self.offset, self.length, self.alg)
        except Exception as e:
            raise RuntimeError(
                f"Integrity Error: Failed to read/decompress frame for "
                f"{self.sop_instance_uid}: {describe_exception(e)}")

        # Integrity Check
        if self.pixel_hash:
            curr_hash = hashlib.sha256(raw).hexdigest()
            if curr_hash != self.pixel_hash:
                raise RuntimeError(
                    f"Integrity Error: Pixel data hash mismatch for {self.sop_instance_uid}. "
                    f"Expected {self.pixel_hash}, got {curr_hash}. "
                    f"Loader(offset={self.offset}, length={self.length}, alg={self.alg})"
                )

        # Reconstruct based on the capture, by the one reading rule
        # (`_reading`): the dtype and the shape it names.
        dt, target_shape = self._reading((
            self.rows, self.cols, self.samples, self.frames, self.bits,
            self.pixel_representation, self.pixel_dtype))

        # Before `np.frombuffer`, which raises a bare `ValueError: buffer
        # size must be a multiple of element size` for a byte count that
        # is not whole samples. That is the right refusal in the wrong
        # channel: only `RuntimeError("Integrity Error: ...")` rides the
        # export worker's `Pixel Loader failed` path into an ERROR row,
        # and `entities.get_pixel_data` wraps it as `Pixel Loader failed
        # for <uid>`. A 16-bit frame's byte length is even by
        # construction, so an odd one is not a DICOM pad; it is the wrong
        # bytes (#373).
        itemsize = np.dtype(dt).itemsize
        if len(raw) % itemsize:
            raise RuntimeError(
                f"Integrity Error: frame for {self.sop_instance_uid} holds "
                f"{len(raw)} bytes, which is not a whole number of "
                f"{itemsize}-byte samples (dtype {np.dtype(dt).name})")

        arr = np.frombuffer(raw, dtype=dt)

        rows = self.rows
        cols = self.cols
        frames = self.frames

        # The element count the bound below compares `arr.size` against.
        target_size = 1
        for d in target_shape:
            target_size *= d

        # A declared geometry of nothing is an integrity failure, not a
        # shape to reshape or pad towards. Without this, a one-byte frame
        # took the old padding fallback -- `arr.size >= 0` is always
        # true, `arr[:0]` is empty -- and a `(0, 0)` array went back to
        # the caller with the integrity hash *passing*, because the hash
        # is over the raw bytes. The export worker then failed with
        # `Compression failed: cannot write empty image`; a caller who
        # never exports got an empty image that looked like data. The
        # reachable shape is an instance whose `pixel_array` was assigned
        # directly, so no Rows/Columns were ever written (#343).
        #
        # Kept ahead of the bound rather than folded into it: an *empty*
        # frame satisfies `0 <= 0 <= 1`, and `np.frombuffer(b"")
        # .reshape((0, 0))` succeeds, so without this the empty array
        # comes back exactly as before. `target_size == 0` names exactly
        # that case: `frames` enters the shape only when > 1 and
        # `samples` is normalised to at least 1, so it is zero exactly
        # when Rows or Columns is (Rows=2, Columns=2, Frames=0 loads as
        # `(2, 2)`). Same prefix as the hash mismatch so it rides the
        # export worker's `Pixel Loader failed` channel into an ERROR
        # row. Here in `__call__` and not in a wrapper: this loader
        # pickles into spawned export workers, and a guard installed on
        # the parent would not be in the child.
        if target_size == 0:
            raise RuntimeError(
                f"Integrity Error: {self.sop_instance_uid} declares no "
                f"pixel geometry (Rows={rows}, Columns={cols}, "
                f"Frames={frames}); a stored frame of {len(raw)} bytes "
                f"cannot be reshaped to nothing")

        # The tolerance is one trailing sample, in elements, and nothing
        # wider in either direction (#373). Why one: DICOM pads an
        # odd-length OB value to even, which for 8-bit data with an odd
        # sample count is exactly one byte, and that is the *only*
        # surplus with a DICOM reason -- ingest itself never writes a
        # pad (`np.ascontiguousarray(ds.pixel_array).tobytes()`). Why
        # elements and not bytes: the byte check above has already
        # refused a partial sample, so here every unit is a whole one.
        #
        # This replaced `try: reshape / except ValueError: truncate or
        # return 1-D`. That fallback took *any* surplus (`arr.size >=
        # target_size`) and silently truncated -- a 16-byte frame loaded
        # as a 2x2 image -- and returned a short frame as a 1-D array
        # that every caller then treated as an image. Both are the right
        # bytes with the wrong geometry: the hash passes and the reshape
        # is what lies, which is why this is independent of #368's
        # hash-beside-the-frame and cannot be produced by any ordering
        # the sidecar gate closes. The message names the UID, both
        # sizes and the shape so that an export failing on one frame in
        # ten thousand is diagnosed from that line alone.
        if not target_size <= arr.size <= target_size + 1:
            raise RuntimeError(
                f"Integrity Error: frame for {self.sop_instance_uid} holds "
                f"{arr.size} samples; geometry {target_shape} needs "
                f"{target_size} (one trailing pad byte is tolerated, "
                f"nothing else)")

        # Cannot fail after the bound: the slice is exactly `target_size`
        # elements, which is the product of `target_shape`.
        return arr[:target_size].reshape(target_shape)


class SidecarWaveformLoader:
    """Functor for lazy loading of waveform samples from the sidecar.

    Top-level class so it stays picklable across process boundaries.
    Stores primitive geometry rather than an Instance reference, which
    avoids a reference cycle and keeps IPC payloads small.
    """

    def __init__(self, sidecar_path, offset, length, alg,
                 instance=None, metadata=None, waveform_hash=None):
        self.sidecar_path = sidecar_path
        self.offset = offset
        self.length = length
        self.alg = alg

        if metadata:
            self.num_samples = metadata.get("num_samples", 0)
            self.num_channels = metadata.get("num_channels", 0)
            self.interpretation = metadata.get("interpretation", "SS")
            self.waveform_hash = metadata.get("waveform_hash")
        elif instance is not None:
            from .waveform import Waveform
            seq = instance.sequences.get("5400,0100")
            if seq is None or not seq.items:
                raise ValueError(
                    "SidecarWaveformLoader requires a Waveform Sequence on the instance")
            wf = Waveform.from_dicom_item(seq.items[0])
            self.num_samples = wf.num_samples
            self.num_channels = wf.num_channels
            self.interpretation = wf.sample_interpretation
            self.waveform_hash = waveform_hash or getattr(instance, "_waveform_hash", None)
        else:
            raise ValueError(
                "SidecarWaveformLoader requires either 'instance' or 'metadata'")

    def read_raw(self) -> bytes:
        """Return the original Waveform Data bytes, integrity-checked.

        Split out from `__call__` so DICOM export can write the source
        bytes back without a decode/re-encode round trip (#34). Callers
        get the sha256 verification for free, which is the reason to come
        through here rather than reading the frame directly.
        """
        mgr = SidecarManager(self.sidecar_path)
        raw = mgr.read_frame(self.offset, self.length, self.alg)

        if self.waveform_hash:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != self.waveform_hash:
                raise ValueError(
                    f"Waveform integrity check failed: expected "
                    f"{self.waveform_hash}, got {actual}")

        return raw

    def __call__(self):
        from .waveform import decode_samples

        return decode_samples(self.read_raw(), self.interpretation,
                              self.num_samples, self.num_channels)


def format_study_date(study_date) -> str:
    """Render a Study's date as "YYYYMMDD" for use in exported DICOM
    attributes.

    Args:
        study_date: `Study.study_date` -- a `date`/`datetime`-like object,
            a preformatted string, or falsy/None.

    Returns:
        str: "YYYYMMDD" when `study_date` supports `strftime`, else
        `str(study_date)`, else "".
    """
    if not study_date:
        return ""
    if hasattr(study_date, 'strftime'):
        return study_date.strftime("%Y%m%d")
    return str(study_date)


def _get_attr_case_insensitive(attributes: dict, tag: str, default):
    """Look up a DICOM attribute tag tolerating either hex-letter casing.

    Real ingested attribute keys are always lowercased
    (`populate_attrs`'s `f"{elem.tag.group:04x},{elem.tag.element:04x}"`),
    but object graphs built directly by a caller -- test fixtures,
    `scripts/generate_test_dataset.py`'s `inst_builder.set_attribute(
    "0008,103E", ...)` -- are free to spell a tag with uppercase hex
    letters. Checking only one casing silently drops values set under the
    other; this is the same trap `privacy.py`'s
    `PHIRedactor._normalize_tag_keys` normalizes away for PHI-tag config
    keys (see its comment naming this exact tag, "0008,103E"). Callers of
    this function should look up a tag through it rather than re-adding a
    `str.lower()`/`str.upper()` at their own call site.

    Args:
        attributes (dict): A `DicomItem.attributes`-shaped dict.
        tag (str): The tag to look up, e.g. `"0008,103e"`.
        default: Returned if `tag` is absent under every casing.

    Returns:
        The attribute value, or `default`.
    """
    if tag in attributes:
        return attributes[tag]
    tag_lower = tag.lower()
    for key, value in attributes.items():
        if isinstance(key, str) and key.lower() == tag_lower:
            return value
    return default


def export_folder_names(patient, study, series):
    """Build the Subject/Study/Series folder names for the exported file
    tree, reproducing `DicomSession._export_dicom`'s "Hybrid Naming"
    scheme -- the naming every user actually gets from
    `session.export(folder)` / `session.export(folder, format="dicom")`
    via the registered `"dicom"` exporter.

    This is the single source of truth for that naming so every export
    format lands in the same `Patient/Study/Series` tree -- callers must
    not reimplement this logic locally, or the trees will drift apart on
    the next edit to either one.

    Uses `ConfigLoader.clean_filename`, the single sanitizer for folder
    names -- NOT the even-stricter per-format record-*name* sanitizers
    such as `isocenter.exporters.wfdb._sanitize` (which forbids spaces,
    appropriate for a bare record-name token but not for a folder name
    that must match `_export_dicom`'s output character-for-character).

    Args:
        patient (Patient): Patient root.
        study (Study): Study whose folder name is being built.
        series (Series): Series whose folder name is being built.

    Returns:
        tuple[str, str, str]: (subject_folder, study_folder, series_folder)
    """
    subj_name = "Subject_" + ConfigLoader.clean_filename(patient.patient_id or "UnknownPatient")

    # Study/Series descriptions are read from the FIRST series' FIRST
    # instance -- not from whichever instance a caller happens to be
    # iterating -- matching `_export_dicom`'s "peek" exactly, so every
    # instance in a series lands under the same folder name.
    st_desc = "Study"
    try:
        if study.series and study.series[0].instances:
            st_desc = _get_attr_case_insensitive(
                study.series[0].instances[0].attributes, "0008,1030", "Study")
    except (AttributeError, IndexError, KeyError):
        # No instances, or no description tag: the "Study" default above
        # stands. Narrow on purpose -- BaseException here also swallowed
        # Ctrl-C during a long export.
        pass
    st_date = str(study.study_date or "NoDate")
    # The suffix disambiguates two studies sharing a date and description.
    # With no UID there is nothing to disambiguate *with*, so say so --
    # slicing a placeholder produced `"Unknown"[-5:]` == "nknow", a word
    # from nowhere that looks like real data and sorts among real
    # suffixes. Take the last 5 only when there is a UID to take them
    # from. (#53, #78)
    st_uid_suffix = (study.study_instance_uid[-5:]
                     if study.study_instance_uid else "NoUID")
    study_folder = ConfigLoader.clean_filename(f"Study_{st_date}_{st_desc}_{st_uid_suffix}")

    se_desc = "Series"
    try:
        if series.instances:
            se_desc = _get_attr_case_insensitive(
                series.instances[0].attributes, "0008,103e", "Series")
    except (AttributeError, IndexError, KeyError):
        # As above: fall back to the "Series" default.
        pass
    # `str(None)` is "None", which reads as a series *numbered* None
    # rather than one whose number was never recorded -- the same defect
    # as the sliced placeholder, one line up.
    se_num = ("NoNumber" if series.series_number is None
              else str(series.series_number))
    se_mod = series.modality or "OT"
    se_uid_suffix = (series.series_instance_uid[-5:]
                     if series.series_instance_uid else "NoUID")
    series_folder = ConfigLoader.clean_filename(
        f"Series_{se_num}_{se_mod}_{se_desc}_{se_uid_suffix}")

    return subj_name, study_folder, series_folder


def export_stamp_attributes(patient, study, series):
    """The patient, study and series tags stamped onto every exported
    instance, for both write doors (#570).

    The one answer to "what does the export write over the instance's own
    attributes", as `export_folder_names` is the one answer to "where".
    `session.export()` and `DicomExporter.write_tree()` each built their
    own set until #570, and they disagreed: `write_tree` wrote the literal
    Study Time `120000` over a real one, and re-stamped Manufacturer, Model
    Name and Device Serial Number from `Series.equipment` -- which, after
    `anonymize()` had emptied the instance's `(0018,1000)`, put the
    scanner's real serial back into a de-identified file. Do not add
    either back here:

    * **No Study Time unless the study has one.** The worker writes a
      zero-length Study Time when nothing supplied one (Type 2
      "unknown"); a literal is a fabricated clinical time, and a `""`
      here would overwrite the instance's real value.
    * **No equipment.** It comes from the instance, which is what
      `anonymize()` edits; `Series.equipment` keeps the source serial on
      purpose, because `redact()` matches rules on it. A hand-built graph
      gets its equipment onto the instances from `SeriesBuilder`.

    Args:
        patient (Patient): The patient root.
        study (Study): The study the instance belongs to.
        series (Series): The series the instance belongs to.

    Returns:
        Tuple[dict, dict, dict]: `(patient_attributes, study_attributes,
            series_attributes)`, keyed by `"gggg,eeee"`.
    """
    patient_attributes = {
        "0010,0010": patient.patient_name,
        "0010,0020": patient.patient_id,
    }
    if getattr(patient, 'birth_date', None):
        patient_attributes["0010,0030"] = patient.birth_date
    if getattr(patient, 'sex', None):
        patient_attributes["0010,0040"] = patient.sex

    study_attributes = {
        "0020,000d": study.study_instance_uid,
        # Formatted, so one string reaches `_merge` whether the entity
        # holds a `date`, a string or None.
        "0008,0020": format_study_date(study.study_date),
    }
    if getattr(study, 'study_time', None):
        study_attributes["0008,0030"] = study.study_time
    if getattr(study, 'accession_number', None):
        study_attributes["0008,0050"] = study.accession_number

    series_attributes = {
        "0020,000e": series.series_instance_uid,
        "0008,0060": series.modality,
        # None stays None, a zero-length Series Number (Type 2). The
        # session stamped `str(None)`, which IS refuses, so the element
        # was dropped with a DATA_LOSS row (#570).
        "0020,0011": (None if series.series_number is None
                      else str(series.series_number)),
    }
    if getattr(series, 'series_description', None):
        series_attributes["0008,103e"] = series.series_description
    return patient_attributes, study_attributes, series_attributes


class DicomExporter:
    """
    Handles writing the Object Graph back to standard DICOM files.

    Provides static methods for saving Patients, Studies, or creating export batches from Validated/Curated data.
    """
    @staticmethod
    def _generate_export_contexts(
            patient: Patient,
            studies: List[Study],
            out_dir: str,
            compression: str = None,
            drop_foreign_icons: bool = False) -> List[ExportContext]:
        """
        Generates ExportContext objects for the given studies.

        Calculates output paths and metadata overrides for each instance in the
        provided studies.

        Args:
            patient (Patient): The patient object.
            studies (List[Study]): List of studies to export.
            out_dir (str): Output directory.
            compression (str, optional): Compression format (e.g. 'j2k').
            drop_foreign_icons (bool): Copied onto every context (#183,
                #542). Passed in rather than computed here because it is a
                **store-wide** answer and this method sees one patient: a
                per-patient computation would carry foreign icons for the
                patients that happen not to have been redacted, which is
                exactly the narrowing `redaction_in_effect` documents as
                failing open. Each carrier's own icon needs nothing from
                here: the worker reads the attestation off the instance.

        Returns:
            List[ExportContext]: List of prepared export contexts.
        """
        contexts = []
        for st in studies:
            for se in st.series:
                for inst in se.instances:
                    # The session's stamps, from the one helper both
                    # doors call (#570). This used to be its own set, with
                    # a literal Study Time and equipment re-stamped from
                    # the series -- see `export_stamp_attributes`.
                    pat_attrs, study_attrs, series_attrs = \
                        export_stamp_attributes(patient, st, se)

                    # Calculate Output Path
                    # 1-3. Subject/Study/Series folders, via the shared
                    # hybrid naming used by every other export format --
                    # see `export_folder_names` for the scheme.
                    subj_name, study_folder, series_folder = export_folder_names(
                        patient, st, se)

                    # 4. Filename -- the SOP Instance UID, matching
                    # `DicomSession._export_dicom`. InstanceNumber
                    # (0020,0013) used to win here when it parsed as an
                    # integer, which meant the same instance landed under
                    # two different names depending on which export path
                    # wrote it, and a tree built by one could not be
                    # diffed against a tree built by the other.
                    #
                    # The UID is also the only correct choice on its own
                    # terms: InstanceNumber is not unique and collides
                    # silently within a series, so `0001.dcm` could be
                    # overwritten by a second instance claiming the same
                    # number. Do not reintroduce a "friendlier" name
                    # here without making it unique. (#50, #78)
                    fname = f"{inst.sop_instance_uid}.dcm"

                    full_out_path = os.path.join(
                        out_dir, subj_name, study_folder, series_folder, fname)

                    # Handle In-Memory Pixels (e.g. Remediated/Detached instances)
                    # If file_path is None, worker cannot load pixels. send them.
                    p_array = None
                    if inst.pixel_array is not None:
                        p_array = inst.pixel_array

                    # Extract Sidecar Info if available (Zero-Copy)
                    sc_path, sc_offset, sc_length, sc_alg = None, None, None, None
                    if hasattr(inst, '_pixel_loader') and inst._pixel_loader:
                        # A type test, not the duck test this used to
                        # be: the block reads four attributes and the
                        # `hasattr` pair checked two, so it never
                        # actually guarded `.length` or `.alg` and was
                        # not the guard it looked like. Every
                        # `_pixel_loader` the package assigns is a
                        # `SidecarPixelLoader`; the only other values
                        # that reach here are two test doubles
                        # (`tests/test_redaction_optimization.py`'s bare
                        # lambda and `tests/test_redaction_parallel.py`'s
                        # `ConstantPixelLoader`), and neither carries any
                        # of the four. Behaviour-identical, and it also
                        # removes a boolean operator the mutation probe
                        # had to keep re-reporting as an equivalent
                        # mutant (#285).
                        pl = inst._pixel_loader
                        if isinstance(pl, SidecarPixelLoader):
                            sc_path = pl.sidecar_path
                            sc_offset = pl.offset
                            sc_length = pl.length
                            sc_alg = pl.alg

                    # Add to queue
                    ctx = ExportContext(
                        instance=inst,
                        output_path=full_out_path,
                        patient_attributes=pat_attrs,
                        study_attributes=study_attrs,
                        series_attributes=series_attrs,
                        pixel_array=p_array,
                        compression=compression,
                        sidecar_path=sc_path,
                        pixel_offset=sc_offset,
                        pixel_length=sc_length,
                        pixel_alg=sc_alg,
                        drop_foreign_icons=drop_foreign_icons,
                    )
                    contexts.append(ctx)
        return contexts

    @staticmethod
    def _report_export_losses(results, store_backend=None) -> int:
        """Log every loss the workers reported, and audit it if we can.

        Warning and auditing are deliberately not the same condition. The
        warning is unconditional because `write_tree` can never supply a
        backend -- it is the serializer path, with no session behind it --
        and gating the report on one would make the fixture generators in
        `scripts/` lose elements in total silence. The audit entry is what
        turns a log line into a compliance record (#36), and needs a store.

        Returns the number of losses reported.
        """
        logger = get_logger()
        count = 0
        for r in results:
            # A worker can append a loss and *then* fail, so a loss does
            # not imply a file. The observation is still true -- the
            # element was dropped from the in-memory copy -- and
            # suppressing it would make the compliance record quietly
            # incomplete, so the row is kept and the statement corrected
            # instead: without the annotation, "was not exported" reads
            # as one element missing from a written file, next to the
            # `ERROR` row saying the file does not exist (#240).
            failed = not getattr(r, "ok", False)
            for scope, loss in getattr(r, "losses", ()):  # Exceptions have none
                # Never `r.output_path`: it is `Subject_<Patient ID>/...`
                # (D10). The row's key is `UNKNOWN`, as
                # `_report_export_failures` keys it.
                uid = r.sop_instance_uid or "UNKNOWN"
                if failed:
                    loss = (f"{loss} The file itself was not written: this "
                            "instance's export failed after the element was "
                            "dropped.")
                logger.warning(f"{r.sop_instance_uid or _NO_SOP_UID}: {loss}")
                count += 1
                if store_backend is not None:
                    # `log_audit`, not `log_audit_batch`: the batch method
                    # writes straight to the database while the audit
                    # writer thread is live, and swallows `sqlite3.Error`
                    # into a log line -- so contention would lose the very
                    # entry that exists because a log line was not enough.
                    # The queue is the path #36 uses, and `close()` drains
                    # it.
                    store_backend.log_audit(
                        action_type="DATA_LOSS", entity_uid=uid, details=loss,
                        loss_scope=scope)
        return count

    @staticmethod
    def _report_export_corrections(results) -> int:
        """Log, at INFO, every descriptor a worker corrected on the way out (#468).

        The parent's half of `ExportOutcome.corrections`, called beside
        `_report_export_losses` on both public write paths. Here because
        the parent is the process whose `isocenter` logger has a handler;
        the worker is usually spawned, and a line it logged was measured
        reaching no one. INFO, not WARNING: the file is correct, nothing
        was lost, and the grade must not move. No audit row, by the
        ruling on #468 -- this line is the record, which is why where it
        is emitted matters.

        Only for written files. A correction to a file that was never
        written describes nothing, and the failure has its own ERROR row.

        Returns the number of lines logged.
        """
        logger = get_logger()
        count = 0
        for r in results:
            if not getattr(r, "ok", False):
                continue  # A lost worker or a failed write: no file.
            for note in r.corrections:
                # The instance, never `r.output_path` (D10).
                logger.info("%s: %s", r.sop_instance_uid or _NO_SOP_UID,
                            note)
                count += 1
        return count

    @staticmethod
    def _report_export_warnings(results, store_backend=None) -> int:
        """Log and audit every claim a written file could not honour (#502).

        The parent's half of `ExportOutcome.warnings`, called beside
        `_report_export_losses` and `_report_export_corrections` on both
        public write paths, and in the parent for the reason all three
        are: the worker is usually a spawned process, with no store
        handle and an `isocenter` logger that has no handler (#126).

        **`WARNING`, and not a new action_type.** The audit vocabulary
        is frozen (13 words), and `WARNING` is the one that means "recorded, not
        fatal": `get_audit_errors()` selects `ERROR` and `WARNING`, the
        report renders both under "Exceptions & Errors", and a row there
        costs the run its `PASS` (#479). That grade movement is the
        point -- it is what makes a preserved false label a reported
        fact rather than a silent one.

        **Not `DATA_LOSS`.** Nothing was dropped: the label and the
        samples are both written exactly as the instance held them.
        `import_files` draws the same line for a declined duplicate.

        Only for written files. A warning describes a file the caller
        now has; when the write failed there is no file and the failure
        has its own `ERROR` row. A lost worker arrives as a bare
        exception with no `warnings` at all (#232).

        Returns the number of warnings reported.
        """
        logger = get_logger()
        count = 0
        for r in results:
            if not getattr(r, "ok", False):
                continue  # A lost worker or a failed write: no file.
            # Never `r.output_path` (D10): the key is `UNKNOWN`, as
            # `_report_export_failures` keys it, and the line names the
            # instance in the worker's own words.
            uid = r.sop_instance_uid or "UNKNOWN"
            for warning in r.warnings:
                logger.warning("%s: %s", r.sop_instance_uid or _NO_SOP_UID,
                               warning)
                count += 1
                if store_backend is not None:
                    # `log_audit`, not `log_audit_batch` -- see the note
                    # in `_report_export_losses`.
                    store_backend.log_audit(
                        action_type="WARNING", entity_uid=uid,
                        details=warning)
        return count

    @staticmethod
    def _report_export_failures(results, store_backend=None):
        """Log every instance the workers could not write, and audit it.

        The mirror of `_report_export_losses`, for the same reason
        (#126): the code that hits the exception is in a subprocess with
        no store handle, so the failure travels back in the
        `ExportOutcome` and is recorded here, in the parent.

        `ERROR` is the existing vocabulary, not a new one. The reader has
        always been there -- `get_audit_errors()` selects `ERROR` and
        `WARNING`, and the report renders them under "Exceptions &
        Errors" -- and nothing in the package had ever written a row it
        could return. That is why a failed export graded `PASS` and said
        "No exceptions or errors were recorded" (#181).

        The detail is flattened to one line and its pipes escaped
        because it is rendered straight into a markdown table row; a
        validator error is a repr'd list and arrives with both. It is
        not truncated: a compliance record that drops the end of the
        reason is its own small lie.

        Returns:
            List[Tuple[str, str]]: `(entity_uid, details)` per failure.
        """
        logger = get_logger()
        failures = []
        for r in results:
            if isinstance(r, ExportOutcome):
                if r.ok:
                    continue
                # The row names the instance and never `r.output_path`,
                # in the key or the text: that path is
                # `<folder>/Subject_<Patient ID>/...`, and this row is
                # persisted, rendered into the compliance report and
                # carried by `ExportError.failures`. It was
                # `Export failed for <output path>: <exception>` until
                # bunch E, and the exception repeated the path -- an
                # `OSError`'s `str()` appends its filename -- which is
                # why the reason goes through
                # `describe_exception_without_paths`. The same shape as
                # the WFDB row (#588) and the worker's console line (P8).
                # The caller already holds the folder; the `EXPORT` row
                # records it, and nothing below it is the report's.
                uid = r.sop_instance_uid or "UNKNOWN"
                named = (f"instance {r.sop_instance_uid}" if r.sop_instance_uid
                         else _NO_SOP_UID)
                # `error` is the exception the worker caught, not prose,
                # so it is described here; `str()` of a message-less one
                # was `''` and the row ended in a colon (#435). A string
                # is kept as given: no worker writes one, and prose
                # cannot be told apart from a path inside it.
                if isinstance(r.error, BaseException):
                    reason = describe_exception_without_paths(r.error)
                else:
                    reason = r.error if r.error is not None else "unknown error"
                detail = f"Export failed for {named}: {reason}"
            else:
                # `run_parallel` returns its own exception when a worker
                # dies before it can answer. There is no outcome to name
                # the instance with, and the row still has to exist.
                uid = "UNKNOWN"
                died = (describe_exception_without_paths(r)
                        if isinstance(r, BaseException) else r)
                detail = f"Export worker failed: {died}"

            detail = " ".join(str(detail).split()).replace("|", "\\|")
            logger.error("%s: %s", uid, detail)
            failures.append((uid, detail))
            if store_backend is not None:
                # `log_audit`, not `log_audit_batch` -- see the note in
                # `_report_export_losses`.
                store_backend.log_audit(action_type="ERROR", entity_uid=uid,
                                        details=detail)
        return failures

    @staticmethod
    def write_tree(
            patient: Patient,
            out_dir: str,
            studies: List[Study] = None,
            compression: str = None,
            show_progress: bool = True,
            executor=None,
            store_backend=None):
        """Write an object graph to disk as DICOM, exactly as it stands.

        **This applies no de-identification.** It is the serializer, not
        the pipeline: it runs no PHI scan, honours no subset filter,
        applies no redaction zones, and reports no partial failure beyond
        raising on the first one. Whatever is in `patient` is what lands
        on disk.

        `DicomSession.export()` is the pipeline, and is what a caller
        de-identifying a cohort wants. It performs the same write, after
        the burned-in identifier scan (`check_burned_in`), the subset
        filter, the recoverable-identity disclosure (`check_reversibility`)
        and the configured redaction rules.

        This exists as a public API because building an object graph by
        hand and writing it out is a real need with no session behind it
        -- it is how `scripts/generate_test_dataset.py` and the other
        fixture generators work, and how the test suite produces DICOM
        without standing up a database. It was previously called
        `save_patient`/`save_studies`, which named it as though it were
        the export path rather than half of one (#54, #78).

        Args:
            patient (Patient): The patient root object.
            out_dir (str): Destination directory.
            studies (List[Study], optional): Write only these studies.
                Defaults to every study under `patient`.
            compression (str, optional): `'j2k'` for JPEG 2000 Lossless, or
                None for Implicit VR Little Endian. Nothing else (#605).
            show_progress (bool): If True, shows a progress bar.
            executor (ProcessPoolExecutor, optional): Shared executor for parallelism.
            store_backend (SqliteStore, optional): Where to write a
                `DATA_LOSS` audit entry for each element that could not be
                written. Callers of this path usually have no session and
                so pass nothing; the losses are logged either way (#126).

        Raises:
            ValueError: If `compression` is neither None nor `'j2k'`,
                before anything is written (#605).
            RuntimeError: If any instance failed to write.
        """
        # First, so an empty tree refuses too: the value is wrong whether
        # or not there is anything to write with it (#605).
        _compresses(compression)
        if studies is None:
            studies = patient.studies
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        logger = get_logger()

        # Planning Phase: Generate Contexts
        #
        # The foreign-icon gate is computed here, once, over the whole tree
        # about to be written -- not inside `_generate_export_contexts`,
        # which sees one patient at a time (#183). Attestation only: there
        # is no session and so no configuration to consult; this is the
        # serializer, and the configuration half of the condition is
        # structurally unavailable to it. The attestation half is not, and
        # applies -- dropping an icon out of a graph that carries a
        # redaction attestation is a property of carrying icon bytes at
        # all, not one of the pipeline gates `write_tree` deliberately
        # skips. Each carrier's own icon needs nothing extra here (#542):
        # the worker reads the attestation off that instance, and the
        # contexts carry no zones on this path.
        drop_foreign = redaction_in_effect(_instances_in(patient, studies))
        export_tasks = DicomExporter._generate_export_contexts(
            patient, studies, out_dir, compression,
            drop_foreign_icons=drop_foreign)

        # Execution Phase
        if not export_tasks:
            logger.warning("No instances found to export.")
            return

        # Log only if progress is shown, or at least one summary line if hidden?
        # If hidden, the caller (batch export) is logging.
        if show_progress:
            logger.info(f"Starting parallel export of {len(export_tasks)} instances...")

        results = run_parallel(
            _export_instance_worker,
            export_tasks,
            desc="Exporting",
            chunksize=10,
            show_progress=show_progress,
            executor=executor,
            yield_exceptions=True)

        # An ExportOutcome per task -- or an Exception, if `run_parallel`
        # itself lost a worker. Both shapes have to survive this, and the
        # second only exists because `yield_exceptions=True` above asks
        # for it: without the flag a lost worker raises out of the
        # iteration and discards every result queued behind it (#232).
        #
        # Materialized because what follows walks it twice, and
        # `run_parallel` returns a generator when asked to. Neither export
        # site asks today; if one ever does, the loss report would consume
        # the results and every success count would silently read zero.
        results = list(results)
        DicomExporter._report_export_losses(results, store_backend)
        DicomExporter._report_export_warnings(results, store_backend)
        DicomExporter._report_export_corrections(results)
        success_count = sum(1 for r in results if getattr(r, "ok", False))
        failures = [r.error if isinstance(r, ExportOutcome) else r
                    for r in results
                    if isinstance(r, Exception) or (
                        isinstance(r, ExportOutcome) and not r.ok)]

        logger.info(f"Export Complete. Success: {success_count}/{len(export_tasks)}")

        if failures:
            # Raise the first failure to satisfy strict tests
            raise RuntimeError(
                f"Export incomplete. {
                    len(failures)} failed. First error: {
                    failures[0]}")

    @staticmethod
    def export_batch(
            export_tasks: Iterable[ExportContext],
            show_progress: bool = True,
            total: int = None,
            executor=None,
            maxtasksperchild: int = None,
            disable_gc: bool = False,
            store_backend=None):
        """
        Exports a flat list of ExportContexts using parallel workers.

        Args:
            export_tasks (Iterable[ExportContext]): Iterator/List of tasks.
            show_progress (bool): If True, shows progress bar.
            total (int, optional): Total count for progress bar.
            executor (optional): Shared executor.
            maxtasksperchild (int, optional): Worker recycle rate (for memory management).
            disable_gc (bool): If True, disables GC in workers for throughput.
            store_backend (SqliteStore, optional): Where to write a
                `DATA_LOSS` audit entry for each element the workers could
                not write. Without it the losses are still logged, but
                only logged (#126).

        Returns:
            ExportSummary: what reached disk and what did not. An
                instance that was written but lost an element counts as
                written; the loss is reported separately. Returned an
                `int` until #181 -- the count was all the parent could
                see, so a failed instance was invisible to the audit log
                and to the compliance report.
        """
        logger = get_logger()
        # if not export_tasks: return # Cannot easily check empty iterator without consuming

        if show_progress:
            count_str = str(total) if total else "?"
            logger.info(f"Starting global parallel export of {count_str} instances...")

        # Run parallel
        results = run_parallel(
            _export_instance_worker,
            export_tasks,
            desc="Exporting",
            chunksize=1,
            show_progress=show_progress,
            total=total,
            executor=executor,
            maxtasksperchild=maxtasksperchild,
            disable_gc=disable_gc,
            # Same reason as `write_tree`: `_report_export_failures`'
            # Exception arm is unreachable without it, and a lost worker
            # would take the whole batch's accounting with it (#232).
            yield_exceptions=True)

        # Two passes -- see the note in `write_tree`.
        results = list(results)
        DicomExporter._report_export_losses(results, store_backend)
        DicomExporter._report_export_warnings(results, store_backend)
        DicomExporter._report_export_corrections(results)
        # We don't raise here by default (batch mode). The failures are
        # audited rather than raised, and the summary is what lets the
        # caller say how many of the requested instances exist (#181).
        failures = DicomExporter._report_export_failures(results, store_backend)
        summary = ExportSummary(
            # The path fallback stays here, unlike the report helpers
            # above (D10): `written_uids` is a frozen public field that is
            # counted (`written` de-duplicates it) and matched against the
            # plan's UIDs, and no line or row is built from it. A shared
            # placeholder would count every UID-less instance as one file
            # -- reachable through `write_tree()` on a hand-built graph,
            # since ingest refuses a file with no SOP Instance UID (#613).
            written_uids=[r.sop_instance_uid or r.output_path
                          for r in results
                          if isinstance(r, ExportOutcome) and r.ok],
            failures=failures)

        logger.info(f"Export Complete. Success: {summary.written}/{total or '?'}")
        return summary

    @staticmethod
    def _finalize_dataset(ds, compression=None, pixel_array=None):
        """
        Finalizes the dataset before saving.

        Applies compression if requested and validates the IOD against DICOM standards.

        Args:
            ds (pydicom.Dataset): The dataset to process.
            compression (str, optional): 'j2k' or None.
            pixel_array (np.ndarray, optional): Pixel data to compress.

        Returns:
            pydicom.Dataset: The finalized dataset.

        Raises:
            ValueError: If validation fails.
        """
        # `_compresses`, the one predicate (#605): anything but None or
        # `"j2k"` raises rather than writing natively in silence.
        if _compresses(compression):
            _compress_j2k(ds, pixel_array)

        errs = IODValidator.validate(ds)
        if errs:
            # We log but might want to raise? logic in worker returns None on error.
            # But worker expects exception to be raised for error?
            # In previous logic: "if not errs: save else return None"
            # So here we should probably return None or raise.
            # Let's raise to be clearer in worker catch
            raise ValueError(f"Validation Errors: {errs}")

        return ds

    @staticmethod
    def _create_ds(inst):
        """Helper to create a fresh FileDataset from an Instance."""
        meta = FileMetaDataset()
        # Fallback to attributes if sop_class_uid property is missing/empty
        sop_class = inst.sop_class_uid
        if not sop_class and "0008,0016" in inst.attributes:
            sop_class = inst.attributes["0008,0016"]

        meta.MediaStorageSOPClassUID = sop_class
        meta.MediaStorageSOPInstanceUID = inst.sop_instance_uid
        meta.TransferSyntaxUID = ImplicitVRLittleEndian
        # Encoding comes from meta.TransferSyntaxUID above; see the
        # note on the JPEG 2000 branch in `_compress_j2k` (#141).
        ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
        return ds

    @staticmethod
    def _merge(ds, attrs, losses=None, vrs=None, *, revrs=None, within=""):
        """Merges a dictionary of attributes into a pydicom Dataset.

        `losses` is an optional list that collects `(scope, detail)` for
        every element that could not be written -- the description, plus
        which side of the private/standard line the tag fell on, which
        is what grades the run (#146). It is an accumulator rather than
        a return value because `_merge` is called five times per instance
        and the loss belongs to the instance, not the call.

        `vrs` is the parallel `{tag: vr}` map an item carries for its
        private tags (`DicomItem.attribute_vrs`, #154). Parallel rather
        than attached to the values: `attributes` holds what the element
        *is*, and pairing every value with a VR would change that shape
        for every reader of it. Only the patient/study/series merges
        pass nothing, because those mappings are standard tags whose VRs
        the dictionary already knows.

        `revrs` is the third accumulator (#571): one `_ReVr` per private
        element written under a VR other than the one recorded for it, or
        collapsed from several values to one. An accumulator for the
        reason `losses` is -- the change belongs to the instance, and the
        worker turns the whole list into **one** `WARNING` sentence
        (`_re_vr_warning`), which a sentence per call would not be:
        `_merge` also runs once per sequence item. `within` names the
        sequence an item's element sits in, for that sentence. With no
        accumulator the collapse is logged here instead, as a loss is.
        """
        for t, v in attrs.items():
            # Explicit VRs for the `gantry` v0.4.1 encrypted-identity
            # tags. They are private, so `dictionary_VR` below raises for
            # them and the fallback would only log a warning -- the tags
            # would silently not be written. Nothing has *written* these
            # since v0.5.0 (47278f8) migrated to (0400,0500); this is the
            # read-back path for stores from that one release, and pairs
            # with the WHITELIST_TAGS exemption in `privacy.py`. Remove
            # one without the other and the sweep strips what this
            # preserves.
            if t == "0099,0010":
                ds.add_new(0x00990010, 'LO', v)
                continue
            if t == "0099,1001":
                ds.add_new(0x00991001, 'OB', v)
                continue

            # Explicit handling for Encrypted Attributes to fix potential dictionary mismatches
            if t == "0400,0510":  # Encrypted Content
                ds.add_new(0x04000510, 'OB', v)
                continue
            if t == "0400,0520":  # Encrypted Content Transfer Syntax UID
                ds.add_new(0x04000520, 'UI', v)
                continue

            if t.startswith("_") or "," not in t:
                continue

            g, e = map(lambda x: int(x, 16), t.split(','))

            # Skip Command Set elements (Group 0000) which are illegal for file persistence
            if g == 0x0000:
                continue

            vr, encoded, re_vr = None, None, None
            try:
                vr = dictionary_VR(Tag(g, e))
            except Exception:
                # Not a standard tag. Almost always a private (odd-group)
                # one, which is the whole point of `remove_private_tags=
                # False`: the caller asked to keep the vendor block, and
                # until #118 this arm only logged, so the tags reached
                # the object graph and the index and then never reached
                # the file (#118).
                #
                # The source element's own VR first, when one was
                # recorded at ingest and the value STILL CONFORMS TO IT.
                # The conformance test is the whole design: a recorded
                # VR is a fact about the value the source file carried,
                # and anonymisation, redaction or a plain `set_attr` can
                # replace that value with one the VR no longer fits.
                # Deferring to it unconditionally would put #195's
                # backslash and #190's over-long value back through a
                # VR that mangles them, which is the defect this fix
                # would otherwise reintroduce one layer up. When it does
                # not fit, the fallback runs exactly as before (#154).
                recorded = (vrs or {}).get(t)
                if v is None or (isinstance(v, (list, tuple, MultiValue))
                                 and len(v) == 0):
                    # A zero-length element: the source asserted the
                    # tag's presence and gave it no value, and DICOM has
                    # an encoding for exactly that. Dropping it is a
                    # claim about the source too, and a different one
                    # from what the source made -- #60 forbade inventing
                    # a value *and* discarding one, and this arm is the
                    # second half (#344). The recorded VR is the
                    # source's own answer; `UN` is what an element whose
                    # VR was never known is (PS3.5 6.2.2), which is
                    # every private element of an Implicit VR source,
                    # because `_record_private_vr` refuses to record
                    # `UN`.
                    #
                    # An empty container is the same assertion as `None`
                    # -- present, no value; PS3.5 7.4 makes no
                    # distinction on the wire -- and until #367 it was
                    # not treated as one: `_value_fits_vr([], vr)` is
                    # False under every VR (it recurses over the
                    # elements, and an empty list has none to check), so
                    # `[]` fell through to the fallback and exported as
                    # `LO` whatever the source recorded. The value is
                    # normalised to `None` because pydicom's three empty
                    # spellings are not interchangeable: `add_new(tag,
                    # 'DS', ())` raises `TypeError` and `PN` raises
                    # `AttributeError`, while `None` writes a zero-length
                    # element under every VR tried (eleven, measured).
                    #
                    # Handled HERE and not by widening `_value_fits_vr`,
                    # deliberately. That function recurses over a list,
                    # so admitting `None` would make `[None, 'B']` "fit"
                    # LO -- `add_new` accepts it and `filewriter` then
                    # raises `TypeError: sequence item 0: expected a
                    # bytes-like object, NoneType found` past `_merge`'s
                    # try, failing the whole file rather than the
                    # element. That is the second failure class
                    # `_value_fits_vr`'s own docstring exists to
                    # prevent. See
                    # `test_a_none_among_siblings_is_the_same_loud_loss_on_both_paths`.
                    vr = recorded if recorded is not None else 'UN'
                    v = None
                elif recorded is not None and _value_fits_vr(v, recorded):
                    vr = recorded
                else:
                    encoded = DicomExporter._fallback_encoding(v)

            try:
                if vr is None:
                    if encoded is None:
                        raise ValueError(
                            f"no VR fits a {type(v).__name__} value")
                    # The one silent shape change left in the fallback:
                    # a multi-valued element with an over-long value
                    # collapses to a single `UT` join -- #165's trade,
                    # and still the right one, because the values are
                    # all present and recoverable, so a DATA_LOSS row
                    # would overstate it. But VM n -> 1 must not be
                    # discovered by reading the file (#190), so it is
                    # said, where the tag is still a tag: onto `revrs`
                    # for the instance's one sentence (#571), or to the
                    # log when there is nowhere to put it. The
                    # backslash-bearing case never reaches this: the
                    # encoder returns None for it before the collapse.
                    collapsed = (encoded[0] == 'UT'
                                 and isinstance(v, (list, tuple, MultiValue))
                                 and len(v) > 1)
                    if collapsed and revrs is None:
                        get_logger().warning(
                            "Tag %s written as a single UT value: one of "
                            "its %d values exceeds LO's 64-character cap, "
                            "so the multiplicity collapses from %d to 1. "
                            "The values are backslash-joined and "
                            "recoverable by splitting.", t, len(v), len(v))
                    # A VR other than the recorded one, or a collapse:
                    # what re-ingest of an explicit-VR file records
                    # changes, so it is reported under any syntax (#571).
                    # Kept until `add_new` has accepted the element; a
                    # refusal there is a loss, not a re-VR.
                    if collapsed or (recorded is not None
                                     and encoded[0] != recorded):
                        re_vr = _ReVr(tag=t, within=within,
                                      recorded=recorded, written=encoded[0],
                                      values=len(v) if collapsed else None)
                    vr, v = encoded
                ds.add_new(Tag(g, e), vr, v)
                if re_vr is not None and revrs is not None:
                    revrs.append(re_vr)
            except Exception as exc:
                # Say "not exported". "Failed to merge" reads like an
                # internal hiccup; this is an element the caller asked
                # for that will not be in the output.
                #
                # Reported by handing it back rather than logging it
                # here: `_merge` runs inside `_export_instance_worker`,
                # which may be in a subprocess with no store handle and
                # -- as the #126 tests show -- no logger the caller can
                # see either. The parent logs it and writes the audit
                # entry (#126).
                loss = (f"Tag {t} not exported (data loss): "
                        f"{describe_exception(exc)}")
                if losses is None:
                    get_logger().warning(loss)
                    continue
                # The scope is attached here, where `t` is still a tag,
                # not in the parent where it is only a substring of a
                # sentence (#146).
                #
                # The dedupe stays, with a narrower reason than it had.
                # Until #179 the worker merged `inst.attributes` twice,
                # so *every* loss arrived in duplicate and this was the
                # only thing keeping section 3 of the compliance report
                # from double-counting. That is gone. What remains is
                # that the four surviving merges overlap by tag --
                # (0010,0010), (0008,0020), (0020,000d), (0020,000e),
                # (0008,0060) are all in `inst.attributes` *and* in the
                # patient/study/series mapping stamped over it -- and
                # the message is deterministic, so one malformed value
                # present at two levels still lands here twice. Keys
                # within a single `attrs` are unique, so the duplicate
                # can only ever come from a second call.
                entry = (loss_scope_for_tag(t), loss)
                if entry not in losses:
                    losses.append(entry)

    # PS3.5 6.2: `LO` is a Long String, 64 characters maximum.
    _LO_MAX = 64

    @staticmethod
    def _fallback_encoding(value) -> Optional[Tuple[str, Any]]:
        """How to write a tag the standard dictionary does not know.

        Returns `(vr, value)` -- picking the VR and encoding the value are
        one decision, not two, because pydicom will accept almost anything
        at `add_new` and only raise when the dataset is written. A wrong
        pairing here does not fail on the offending element; it fails the
        whole export, thousands of instances later, with a `TypeError`
        from `filewriter`.

        PS3.5 §6.2.2 ("Unknown (UN) Value Representation") makes `UN`
        the VR for an unknown value -- not A.1, which is the Implicit VR
        Little Endian Transfer Syntax and says nothing about it -- and
        for raw bytes that is right. It is wrong for everything else:
        `UN` is an OB-family VR and rejects `str` at write time. Text
        needs a text VR, and `LO` caps at 64 characters, so longer
        values go to `UT`, which is unbounded. The same section's
        second clause -- a known VR whose value exceeds what a 16-bit
        length field can carry is relabelled `UN` -- is the rule behind
        `BINARY_RETENTION_MAX_BYTES` (#151), so this escape hatch and
        that threshold rest on one paragraph of the standard rather
        than two unrelated ones. Numbers are stringified, which is what the
        EAV table (`instance_attributes.value_text`) would have done to
        them anyway -- without it, whether a private tag exported would
        depend on whether a save had happened yet.

        The `UT` branch narrows one thing: `UT` has a value multiplicity
        of 1, where `LO` is 1-n. A backslash-delimited value past 64
        characters therefore round-trips as one string containing literal
        backslashes rather than a list. Widening `LO` to cover it would
        be worse -- an over-long `LO` is non-conformant. The same VM-1
        property is why `UT` also takes any string containing `\\`,
        whatever its length: under `LO` the backslash reads as the value
        delimiter and the value comes back split (#195).

        That paragraph used to end "and nothing downstream reads these as
        lists anyway, because they arrive from `value_text` already
        flattened to a single string". That describes the EAV round-trip
        and *only* it. `populate_attrs` calls `set_attr(tag, elem.value)`
        with whatever pydicom produced, and for VM > 1 that is a
        `MultiValue` -- on the in-memory path, which is the one
        `session.export()` takes. The reasoning was the gap: no arm
        matched, so every multi-valued private element was reported as
        data loss and written nowhere (#165). See `_fallback_multivalue`.

        Returns None when nothing fits, which the caller reports as data
        loss rather than encoding something it would have to guess at.
        """
        if isinstance(value, (bytes, bytearray)):
            return 'UN', bytes(value)
        if isinstance(value, memoryview):
            return 'UN', value.tobytes()
        if isinstance(value, bool):
            # This arm returns exactly what the `int` arm below would:
            # `str(True)` is already "True", so today deleting it is an
            # equivalent mutant no test can kill (#283). It is kept as a
            # pre-placed guard for #154, which is about private tags
            # ceasing to collapse to `LO`. The day the numeric arm emits
            # a numeric VR, a `bool` falling through to it becomes `1` --
            # and ordering above `int` is the entire mechanism that
            # prevents that, since `bool` is an `int` subclass.
            return 'LO', str(value)
        if isinstance(value, (int, float)):
            return 'LO', str(value)
        if isinstance(value, str):
            # `\` is the value delimiter of every 1-n VR (PS3.5 6.2).
            # Under `LO`, pydicom writes it as a separator and re-splits
            # on it at read time, so a value that legitimately contains
            # one -- a source `LT`/`ST`/`UT` element, where backslash is
            # ordinary text -- came back as two values, silently, from
            # conformant input (#195). `UT` is VM 1: no separating in
            # either direction, and the value round-trips byte-faithfully.
            fits_lo = (len(value) <= DicomExporter._LO_MAX
                       and '\\' not in value)
            return ('LO' if fits_lo else 'UT'), value
        # Last, because `str`, `bytes` and `bytearray` are sequences too
        # and each has its own answer above.
        if isinstance(value, (list, tuple, MultiValue)):
            return DicomExporter._fallback_multivalue(value)
        return None

    @staticmethod
    def _fallback_multivalue(values) -> Optional[Tuple[str, Any]]:
        """The same decision for a value with more than one value (#165).

        Two shapes arrive here and both must work. In memory, pydicom
        hands back a `MultiValue`, which is a `MutableSequence` and *not*
        a `list` subclass -- `isinstance(value, list)` misses it, which
        is the same trap `save_vertical_attributes` documents on the
        storage side. After a save/close/reopen,
        `load_vertical_attributes` reassembles the EAV rows as a plain
        list of strings (#158), so the reloaded path arrives as a `list`.
        The two converge: the EAV stores `str(atom)`, and each atom here
        is encoded by the same scalar rules that produced that string, so
        an in-memory `US [1, 2, 3]` and its reloaded `['1', '2', '3']`
        export to the identical element.

        **No VR is restored and no type is inferred.** `[1, 2, 3]` is not
        worked back to `US`; the values are encoded elementwise by the
        scalar arms above and written as a multi-valued *string* element,
        which is how DICOM expresses multiplicity natively -- one element,
        backslash-separated on the wire, and pydicom does the separating.
        What VR a private tag should be written under is #154 and the
        repo owner's call; this only stops the values from vanishing.
        A multi-valued `AT` therefore stringifies to `(0010,0010)` per
        value exactly as the VM = 1 case already does, rather than being
        forked here into a second answer.

        **The 64-character cap is checked per value, not against the
        join.** PS3.5 6.2 bounds each *value* of a multi-valued element,
        so two 50-character values under `LO` are conformant even though
        their encoding is 101 bytes; verified by writing and reading one
        back under both implicit and explicit VR with no warning. When a
        single value *does* exceed 64 there is no text VR that holds both
        the length and the multiplicity -- `LO` is 1-n and capped, `UT` is
        unbounded and VM 1 -- so the element collapses to one `UT` string
        with literal backslashes, which is the trade the single-value `UT`
        branch above already documents.

        An empty sequence is a zero-length `LO`: a legal element saying
        the tag was present with no value, where before it reached the
        "nothing fits" arm and was reported as loss.

        Returns None if any one value has no text encoding -- an `object`,
        or `bytes`, whose `UN` is an OB-family VR with no multiplicity to
        put a list into. The whole element is then reported as data loss,
        siblings included: there is no half-written element in DICOM, and
        two of three vendor values written silently is the disguised loss
        this is meant to avoid, not a smaller version of it.
        """
        atoms = []
        for atom in values:
            encoded = DicomExporter._fallback_encoding(atom)
            if encoded is None:
                return None
            text = encoded[1]
            # `bytes` (from the `UN` arm) and `list` (from a nested
            # sequence) both land here and neither can be a value of a
            # multi-valued text element.
            if not isinstance(text, str):
                return None
            atoms.append(text)

        if not atoms:
            # `_merge` never reaches this with an empty container: it
            # decides before calling (the recorded VR, or `UN`, #367).
            # This is the answer a *direct* caller gets, and it is PS3.5
            # 6.2.2's -- a zero-length element whose VR was never known
            # is `UN`, not `LO`. `None` rather than `[]` because `None`
            # is the one empty spelling `add_new` accepts under every
            # VR. Stated rather than deleted: with this arm gone the
            # join below returns `('LO', [])` for an empty list anyway
            # (`all(...)` over nothing is True), so the function needs
            # an answer of its own and it has to agree with `_merge`'s.
            return 'UN', None

        # An atom containing `\` cannot be a value of any 1-n VR: the
        # backslash *is* the multiplicity on the wire (PS3.5 6.2), so a
        # reader cannot tell the atom's content from the element's
        # arity -- `LO` re-splits it and the VM inflates, silently
        # (#190, #195). Joining to `UT` is ambiguous in the same way,
        # so the whole element is reported as data loss instead: a loud
        # loss beats a silently wrong element, the same call the #165
        # entry makes for a partial one. This check must run BEFORE the
        # over-long collapse below -- once collapsed to `UT`, the join
        # and the atom's own backslash are indistinguishable and the
        # value is unrecoverable, so the combination of an over-long
        # sibling and a backslash-bearing atom takes this arm, not the
        # join (#190).
        if any('\\' in atom for atom in atoms):
            return None

        # A new list every time, never the input: `_merge` rebinds the
        # value it is handed, and the mapping it read belongs to the live
        # object graph.
        if all(len(atom) <= DicomExporter._LO_MAX for atom in atoms):
            return 'LO', atoms
        return 'UT', '\\'.join(atoms)

    @staticmethod
    def _merge_sequences(ds, sequences: Dict[str, Any], losses=None, *,
                         revrs=None, within="", corrections=None):
        """
        Recursively populates sequences into the dataset.

        Args:
            ds (pydicom.Dataset): The dataset to modify.
            sequences (Dict[str, DicomSequence]): Dictionary mapping tags to Sequence objects.
            losses (list, optional): `_merge`'s loss accumulator.
            revrs (list, optional): `_merge`'s re-VR accumulator (#571),
                threaded to every item so a nested element joins its
                instance's one sentence.
            within (str): The enclosing sequence path, for that sentence.
            corrections (list, optional): Appended to with
                `_label_as_written`'s note for every item whose
                (0028,0004) was respelled (#602), prefixed with the item.
        """
        for tag_str, dicom_seq in sequences.items():
            g, e = map(lambda x: int(x, 16), tag_str.split(','))
            tag = Tag(g, e)

            pydicom_seq = Sequence()
            for index, item in enumerate(dicom_seq.items):
                # A sequence item is never encoded on its own: pydicom
                # writes it with the enclosing file's encoding, so these
                # flags were read by nothing even before 4.0 drops them.
                ds_item = Dataset()

                # Recursively merge item attributes and sub-sequences
                path = (f"{within} > ({tag_str})" if within
                        else f"({tag_str})")
                # #532's respelling, per item and before its `_merge`
                # (#602): pydicom warns as the raw value is assigned, so
                # respelling afterwards would be too late. A copy, never
                # `item.attributes` -- under threads that is the live graph.
                attributes, respelled = _label_as_written(item.attributes)
                if respelled is not None and corrections is not None:
                    corrections.append(f"{path} item {index}: {respelled}")
                DicomExporter._merge(ds_item, attributes, losses,
                                     vrs=getattr(item, 'attribute_vrs', None),
                                     revrs=revrs, within=path)
                DicomExporter._merge_sequences(ds_item, item.sequences, losses,
                                               revrs=revrs, within=path,
                                               corrections=corrections)

                pydicom_seq.append(ds_item)

            ds.add_new(tag, 'SQ', pydicom_seq)
