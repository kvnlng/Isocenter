"""
IO Handlers for Isocenter.

This module provides classes for:
- DicomStore: The central catalog of DICOM objects.
- DicomImporter: Parallel file ingestion.
- DicomExporter: Writing DICOM files to disk.
- SidecarPixelLoader: Lazy loading of pixel data.
- SidecarWaveformLoader: Lazy loading of waveform samples.
"""

import os
import sys
import hashlib
import io
from typing import List, Dict, Any, Optional, Tuple, Iterable
from datetime import datetime, date
from dataclasses import dataclass, field

import pydicom
import numpy as np
try:
    from PIL import Image
except ImportError:
    Image = None
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless
from pydicom.tag import Tag
from pydicom.datadict import dictionary_VR
try:
    from pydicom.encapsulate import encapsulate
except ImportError:
    from pydicom.encaps import encapsulate
from pydicom.multival import MultiValue
from pydicom.sequence import Sequence
from pydicom.dataset import Dataset
from pydicom.charset import default_encoding
from pydicom.dataelem import DataElement
from pydicom.filebase import DicomBytesIO
from pydicom.filereader import read_sequence
from pydicom.filewriter import write_sequence

from .entities import Patient, Study, Series, Instance, Equipment, DicomItem
from .logger import get_logger
from .pixel_geometry import (
    GeometryEvidence,
    planar_configuration_default,
    resolve_photometric_interpretation,
    resolve_pixel_geometry,
)
from .parallel import run_parallel
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
#: what `kind` means, which #150 also has an interest in. Filed as #183.
#:
#: (5400,1010) is deliberately absent. Waveform Data is *never* at the
#: top level -- it lives inside Waveform Sequence items -- so a depth
#: rule would report the one group that is routed. Which of several
#: multiplex groups was kept is an index question, not a depth question,
#: and #160 already reports the discarded ones from the group count.
_ROOT_ONLY_ROUTED_TAGS = frozenset({
    Tag(0x7fe0, 0x0010),   # Pixel Data
})

#: The float pixel elements. Also routed, and by a different mechanism
#: from everything else here: nothing extracts them at ingest, and
#: `_export_instance_worker` writes them back from the array
#: `get_pixel_data()` re-reads out of the source file, under their own
#: tag (#170, #193). That still satisfies `_is_routed`'s question --
#: something else in the pipeline carries these bytes -- and reporting
#: them at ingest would file a `DATA_LOSS` row reading "not in the
#: exported data" about an element that is in the exported data. Which
#: is the defect #194 opened against the first cut of this fix, pointed
#: at a second tag.
#:
#: Two conditions, both applied in `_is_routed`. Top level only, because
#: `get_pixel_data()` reads the top level. And only when the instance
#: has no Pixel Data of its own: the export takes the sidecar array
#: whenever there is a loader, so in a file carrying both -- which
#: PS3.5 Section 8.2 forbids, but malformed input exists -- the float half is
#: genuinely lost and must still be reported.
#:
#: The sidecar is still the missing half, and is still #183: an ingest
#: that could carry these bytes would survive the source file being
#: moved, which this cannot.
_FLOAT_PIXEL_TAGS = frozenset({
    Tag(0x7fe0, 0x0008),   # Float Pixel Data
    Tag(0x7fe0, 0x0009),   # Double Float Pixel Data
})

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


def _is_routed(tag, is_root: bool, has_pixel_data: bool = False) -> bool:
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
#: SIGNAL exists because group parity turned out to be a proxy for "how
#: much should the reader care", and one emitter broke the proxy: a
#: discarded waveform multiplex group lives under a standard tag, and it
#: is acquired signal that was in the source and is not in the export --
#: not an annotation layer with a defined home elsewhere, which is what
#: makes an overlay routine (#150). The discriminator is what the loss
#: *was*, not how bad it felt: STANDARD stays the scope for routine
#: standard-group drops, and widening the report's grading test to
#: STANDARD would take every overlay with it.
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
    # Required, not decoration: without them `read_sequence` raises
    # `AttributeError: 'DicomBytesIO' object has no attribute
    # '_tag_packer'`. They are the public setters and emit no
    # deprecation warning under pydicom 3.0.2 --
    # `tests/test_pydicom_deprecations.py` is what notices if that
    # changes, because a deprecated-to-removed setter would make this
    # function return None for *every* sequence and #167 would come
    # back reported as "unparseable".
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
    out.is_little_endian = True
    out.is_implicit_VR = True
    try:
        write_sequence(out, DataElement(tag, "SQ", parsed), encoding)
    except Exception:      # pylint: disable=broad-except
        return None
    return parsed if out.getvalue() == raw else None


def populate_attrs(ds: Any, item: "DicomItem", dropped: list = None,
                   is_root: bool = True, unscanned: list = None):
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
    from the export (#169). It is still skipped -- keeping it is #183 --
    but it is reported now.

    Which member is which takes two questions, and they are deliberately
    two names. `_is_routed` asks whether something else carries the
    bytes: the sidecar for top-level (7fe0,0010), and the export's
    source re-read for the float pair (#170, #193).
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
                    process_sequence(tag, parsed, item, dropped, unscanned)
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
            process_sequence(tag, elem, item, dropped, unscanned)
        elif elem.VR == 'PN':
            # Sanitize PersonName for pickle safety
            item.set_attr(tag, str(elem.value))
        else:
            item.set_attr(tag, elem.value)


def process_sequence(tag, elem, parent_item, dropped: list = None,
                     unscanned: list = None):
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
    """
    for ds_item in elem:
        seq_item = DicomItem()
        populate_attrs(ds_item, seq_item, dropped, is_root=False,
                       unscanned=unscanned)
        parent_item.add_sequence_item(tag, seq_item)


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
        populate_attrs(ds, inst, dropped, unscanned=unscanned)
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
            try:
                # Always decompress to raw bytes to ensure sidecar has consistent format (SidecarPixelLoader expects raw)
                # This handles RLE/JPEG/J2K by decoding them now.
                arr = np.ascontiguousarray(ds.pixel_array)
                p_bytes = arr.tobytes()
                p_alg = 'zlib'  # Always compress the raw bytes
            except Exception as e:
                # If decompression fails (missing codec), we cannot ingest safely for sidecar usage.
                # The path rides the meta slot, as in the blanket except
                # below, so the parent's ERROR row can name the file (#211).
                return ({'path': fp}, None, None, None, None, None, None,
                        f"Decompression Failed: {e}")

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

        return (meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, None)
    except Exception as e:
        # `{'path': fp}` rather than None in the meta slot, so the
        # parent can name the file in its ERROR row without parsing the
        # prose. The reason travels as the error string it always was;
        # the arity -- unpacked at every call site -- does not change
        # (#211).
        return ({'path': fp}, None, None, None, None, None, None, str(e))


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
    `ERROR` audit row); `declined` were refused as the un-redacted
    originals of instances the session already holds (#238, audited as
    `WARNING`); `skipped` were already in the store and were not read
    again.
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

        # Populate deep maps
        for p in store.patients:
            for st in p.studies:
                study_map[st.study_instance_uid] = st
                for se in st.series:
                    series_map[se.series_instance_uid] = se

        # 2. Parallel Execution
        # OPTIMIZATION: Use return_generator=True to stream results.
        # This prevents accumulating result tuples (with huge p_bytes) in a list (O(N) memory).
        # We process each result immediately and discard it (O(1) memory).
        # OPTIMIZATION: chunksize=1 to prevent buffering multiple large files in IPC queue
        results = run_parallel(
            ingest_worker,
            new_files,
            desc="Ingesting",
            chunksize=1,
            executor=executor,
            return_generator=True)

        # 3. Aggregation (Streaming)
        #
        # Snapshotted once, before the loop. Nothing this loop appends to
        # the graph carries a retired identity -- only `regenerate_uid()`
        # writes one -- so a snapshot taken here cannot go stale during
        # it, and re-querying per result would walk the whole graph once
        # per file.
        superseded = store.get_superseded_uids()
        declined = 0
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

        for meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, err in results:
            # Clear result components from scope as soon as possible after use to help GC
            # But the loop variable holds them. Next iteration clears them.
            if err:
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
                        declined += 1
                        # First five individually, as
                        # `scan_burned_in_annotations` does: a re-run over
                        # a large redacted cohort would otherwise print a
                        # line per file. The audit row is per file
                        # regardless -- it is the compliance trail, and
                        # DATA_LOSS rows are per instance for the same
                        # reason.
                        if declined <= 5:
                            logger.warning(detail)
                        elif declined == 6:
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

                    # Persist Pixels to Sidecar (Main Thread Sequential Write)
                    if p_bytes and sidecar_manager:
                        off, leng = sidecar_manager.write_frame(p_bytes, p_alg)
                        inst._pixel_loader = SidecarPixelLoader(
                            sidecar_manager.filepath, off, leng, p_alg, instance=inst)
                        inst._pixel_hash = p_hash

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
                    if w_bytes and sidecar_manager:
                        w_off, w_len = sidecar_manager.write_frame(w_bytes, 'zlib')
                        inst._waveform_hash = w_hash
                        inst._waveform_loader = SidecarWaveformLoader(
                            sidecar_manager.filepath, w_off, w_len, 'zlib',
                            instance=inst, waveform_hash=w_hash)

                        # Unlike pixels, waveform offsets have no column on
                        # `instances`, so the blob table is their only record.
                        # Skipping this makes compaction reclaim them.
                        #
                        # Called without `conn=`: this loop runs outside any
                        # open SqliteStore transaction, so record_blob_ref is
                        # free to open (and commit) its own connection here.
                        if store_backend is not None:
                            store_backend.record_blob_ref(
                                inst.sop_instance_uid, 'waveform',
                                w_off, w_len, w_hash, 'zlib')

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
                        if meta['man'] or meta['model']:
                            series.equipment = Equipment(meta['man'], meta['model'], meta['dev_sn'])
                        study.series.append(series)
                        series_map[ser_id] = series

                    # Instance
                    series.instances.append(inst)
                    count += 1
                except Exception as e:
                    # A parent-side failure is the same failure to the
                    # caller as a worker-side one: the file is not in the
                    # store. It takes the same route (#211).
                    _record_failure(inst.file_path or '<unknown>',
                                    f"Linkage Failed: {e}")

        logger.info(f"Successfully ingested {count} instances.")
        if failures:
            logger.warning(
                f"Rejected {len(failures)} file(s) at ingest; each has an "
                "ERROR audit row naming the file and the reason.")
        if declined:
            logger.warning(
                f"Declined {declined} file(s) whose SOP Instance UID is the "
                "pre-redaction identity of an instance already in this "
                "session; see the compliance report.")

        return IngestSummary(ingested=count, failures=failures,
                             declined=declined, skipped=skipped_count)


@dataclass
class ExportContext:
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
    #: Re-read the written file and compare its descriptors before
    #: delivering it (#209). Off by default: it costs a second parse
    #: per instance. Carried here because the check runs in the worker
    #: -- the file is local to it and the cost parallelizes.
    verify_readback: bool = False


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
    #: SOP Instance UID per instance that reached disk. Falls back to
    #: the output path for an instance carrying no UID, which the export
    #: plan cannot produce -- it names the file after that UID.
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


def _write_pixel_geometry(ds, geom, attributes, *, float_element: bool) -> None:
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
            fallback.
        float_element (bool): Whether the pixel element just written was
            (7fe0,0008) or (7fe0,0009). Keyword-only and **without a
            default**, so a third call site has to decide which module's
            Photometric Interpretation rules apply rather than inheriting
            the integer path's by omission (#222).
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
    if photometric:
        ds.PhotometricInterpretation = photometric

    if planar_configuration_default(attributes, geom.samples):
        ds.PlanarConfiguration = 0


#: The descriptors the readback compares, by pydicom keyword. The four
#: geometry descriptors are the ones #186/#205 showed can describe a
#: different image than the pixels beside them; BitsAllocated is the
#: width #170/#216 showed being silently rewritten from the tag side.
_READBACK_DESCRIPTORS = ("Rows", "Columns", "SamplesPerPixel",
                         "NumberOfFrames", "BitsAllocated")


def _verify_readback(path: str, ds) -> None:
    """Re-read a just-written file and hold it against what was meant.

    "The write did not raise" is a weaker claim than "a file exists
    that decodes to what we meant", and the compliance report presents
    the stronger one (#209). This is the opt-in check behind
    `export(verify_readback=True)`.

    Compared against the dataset the worker serialized, deliberately
    *not* against `inst.attributes`: the worker corrects stale declared
    descriptors on the way out (`_write_pixel_geometry` writes the
    resolved geometry, the integer branch derives `BitsAllocated` from
    `itemsize` -- #186, #216), so the raw attributes are the one
    baseline guaranteed to disagree with a correctly written file. The
    claim being verified is "the file says what the export meant",
    which is the claim `ok=True` makes to the report.

    Raises on an unreadable file or any descriptor mismatch. The raise
    is the whole mechanism: it becomes `ExportOutcome(ok=False)`, an
    `ERROR` audit row and a `REVIEW_REQUIRED` grade through the same
    channel a write that raised takes (#181) -- and because it fires
    against the temporary file, before the rename that publishes it
    (#199), a file that fails here is never delivered at all.
    """
    try:
        readback = pydicom.dcmread(path)
    except Exception as exc:
        raise RuntimeError(
            f"Readback verification failed: the written file could not be "
            f"read back ({exc})") from exc

    mismatches = [
        f"{kw} reads back as {getattr(readback, kw, None)!r} where "
        f"{getattr(ds, kw, None)!r} was written"
        for kw in _READBACK_DESCRIPTORS
        if getattr(readback, kw, None) != getattr(ds, kw, None)]
    if mismatches:
        raise RuntimeError(
            "Readback verification failed: " + "; ".join(mismatches))


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
    uid = getattr(ctx.instance, "sop_instance_uid", None)

    try:
        inst = ctx.instance
        ds = DicomExporter._create_ds(inst)

        # 0. Base Attributes
        DicomExporter._merge(ds, inst.attributes, losses)
        DicomExporter._merge_sequences(ds, inst.sequences, losses)

        # 1. Patient Level
        DicomExporter._merge(ds, ctx.patient_attributes, losses)

        # 2. Study Level
        DicomExporter._merge(ds, ctx.study_attributes, losses)

        # 3. Series Level
        DicomExporter._merge(ds, ctx.series_attributes, losses)

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

                # Check writeability
                if not arr.flags.writeable:
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
        # when it is not writeable, which does not change its shape.
        if arr is not None and geom.evidence is GeometryEvidence.GUESSED:
            raise RuntimeError(
                f"Refusing to write {ctx.output_path}: the pixel "
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
                    f"Refusing to write {ctx.output_path}: the pixels "
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

            if arr.itemsize == 4:
                ds.FloatPixelData = arr.tobytes()
                ds.BitsAllocated = 32
            elif arr.itemsize == 8:
                ds.DoubleFloatPixelData = arr.tobytes()
                ds.BitsAllocated = 64
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

                _write_pixel_geometry(ds, geom, inst.attributes,
                                      float_element=True)

            arr = None

        if arr is not None:
            # MEMORY OPTIMIZATION:
            # If compression is requested, DO NOT convert to bytes here.
            # Pass the numpy array to _finalize_dataset -> _compress_j2k directly.
            # Only set PixelData if NOT compressing.

            if not ctx.compression:
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
            # `"PixelData" in ds`: with `ctx.compression` set the worker
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

            _write_pixel_geometry(ds, geom, inst.attributes,
                                  float_element=False)

            if arr.itemsize == 1:
                default_bits = 8
            else:
                default_bits = 16

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
            # BitsStored, HighBit and PixelRepresentation stay declared:
            # they do not constrain how many bytes `tobytes()` emits, and
            # their coherence is out of scope (spec §8).
            ds.BitsAllocated = arr.itemsize * 8
            ds.BitsStored = inst.attributes.get("0028,0101", default_bits)
            ds.HighBit = inst.attributes.get("0028,0102", default_bits - 1)
            ds.PixelRepresentation = inst.attributes.get("0028,0103", 0)

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

        # Validate & Save
        ds = DicomExporter._finalize_dataset(ds, ctx.compression, pixel_array=arr)

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
                _verify_readback(tmp_path, ds)
            os.replace(tmp_path, ctx.output_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass  # save_as raised before creating it
            raise
        return ExportOutcome(ok=True, output_path=ctx.output_path,
                             sop_instance_uid=uid, losses=losses)
    except Exception as e:
        # Do not raise, as it aborts the entire parallel batch.
        # Report the failure back for the parent to count and raise on.
        print(f"ERROR: Export failed for {ctx.output_path}: {e}", file=sys.stderr)
        return ExportOutcome(ok=False, output_path=ctx.output_path,
                             sop_instance_uid=uid, losses=losses, error=e)


def _compress_j2k(ds, pixel_array=None):
    """
    Compresses the pixel data of the dataset using JPEG 2000 Lossless (Pillow).
    Updates TransferSyntaxUID and PixelData.
    """
    try:
        arr = pixel_array
        if arr is None:
            # Fallback to reconstructing from PixelData bytes if array not passed
            if not hasattr(ds, 'PixelData'):
                return

            # 1. Get metadata
            rows = ds.Rows
            cols = ds.Columns
            samples = ds.SamplesPerPixel
            bits = ds.BitsAllocated

            # 2. Reconstruct Numpy Array from bytes (since we just set it in worker)
            # Assuming Little Endian input for now (as set in _create_ds)
            dt = np.uint16 if bits > 8 else np.uint8
            arr = np.frombuffer(ds.PixelData, dtype=dt)

            # Reshape
            # Correctly handle frames
            frames = getattr(ds, "NumberOfFrames", 1)

            # Shape logic matching export worker
            if frames > 1:
                if samples > 1:
                    arr = arr.reshape((frames, rows, cols, samples))
                else:
                    arr = arr.reshape((frames, rows, cols))
            else:
                if samples > 1:
                    arr = arr.reshape((rows, cols, samples))
                else:
                    arr = arr.reshape((rows, cols))
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

        # Helper to compress single frame
        def encode_frame(frame_arr):
            # Pillow expects [H, W] or [H, W, C]
            if Image is None:
                raise ImportError("Pillow not installed.")
            img = Image.fromarray(frame_arr)
            bio = io.BytesIO()
            img.save(bio, format='JPEG2000', compression='lossless')
            return bio.getvalue()

        if frames > 1:
            for i in range(frames):
                frames_data.append(encode_frame(arr[i]))
        else:
            frames_data.append(encode_frame(arr))

        ds.PixelData = encapsulate(frames_data)
        # ds.TransferSyntaxUID = JPEG2000Lossless # REMOVE: Group 2 tags must be in file_meta only
        # The transfer syntax is the encoding. `is_implicit_VR` and
        # `is_little_endian` are not set alongside it: pydicom derives
        # both from the UID and removes the attributes in 4.0 (#141).
        ds.file_meta.TransferSyntaxUID = JPEG2000Lossless

    except ImportError:
        # Fallback or Log?
        raise RuntimeError("Pillow or pydicom not installed/configured for JPEG 2000.")
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
            self.planar_conf = metadata.get("planar_configuration", 0) or 0
            self.pixel_hash = metadata.get("pixel_hash", None)
        elif instance:
            self.sop_instance_uid = instance.sop_instance_uid
            # Extract attributes safely
            self.rows = int(instance.attributes.get("0028,0010", 0) or 0)
            self.cols = int(instance.attributes.get("0028,0011", 0) or 0)
            self.samples = int(instance.attributes.get("0028,0002", 1) or 1)
            self.frames = int(instance.attributes.get("0028,0008", 0) or 0)
            self.bits = int(instance.attributes.get("0028,0100", 8) or 8)
            self.pixel_representation = int(instance.attributes.get("0028,0103", 0) or 0)
            self.planar_conf = int(instance.attributes.get("0028,0006", 0) or 0)
            self.pixel_hash = pixel_hash or getattr(instance, "_pixel_hash", None)
        else:
            raise ValueError("SidecarPixelLoader requires either 'instance' or 'metadata'")

    def __call__(self):
        mgr = SidecarManager(self.sidecar_path)

        try:
            raw = mgr.read_frame(self.offset, self.length, self.alg)
        except Exception as e:
            raise RuntimeError(
                f"Integrity Error: Failed to read/decompress frame for {self.sop_instance_uid}: {e}")

        # Integrity Check
        if self.pixel_hash:
            curr_hash = hashlib.sha256(raw).hexdigest()
            if curr_hash != self.pixel_hash:
                raise RuntimeError(
                    f"Integrity Error: Pixel data hash mismatch for {self.sop_instance_uid}. "
                    f"Expected {self.pixel_hash}, got {curr_hash}. "
                    f"Loader(offset={self.offset}, length={self.length}, alg={self.alg})"
                )

        # Reconstruct based on attributes
        dt = np.uint16 if self.bits > 8 else np.uint8
        # Handle signed?
        if self.pixel_representation == 1:
            dt = np.int16 if self.bits > 8 else np.int8

        arr = np.frombuffer(raw, dtype=dt)

        rows = self.rows
        cols = self.cols
        samples = self.samples
        frames = self.frames
        planar_conf = self.planar_conf

        target_shape = None
        if frames > 1:
            target_shape = (frames, rows, cols, samples)
            if samples == 1:
                target_shape = (frames, rows, cols)
        elif samples > 1:
            if planar_conf == 0:
                target_shape = (rows, cols, samples)
            else:
                # Planar Configuration 1: (Samples, Rows, Cols)
                target_shape = (samples, rows, cols)
        else:
            target_shape = (rows, cols)

        try:
            arr_reshaped = arr.reshape(target_shape)
        except ValueError:
            # Handle padding
            target_size = 1
            for d in target_shape:
                target_size *= d
            if arr.size >= target_size:
                arr = arr[:target_size]
                arr_reshaped = arr.reshape(target_shape)
            else:
                return arr  # Fallback to 1D

        # If Planar=1, transpose to (Rows, Cols, Samples) for consistency
        if samples > 1 and frames <= 1 and planar_conf == 1:
            arr_reshaped = arr_reshaped.transpose(1, 2, 0)
        return arr_reshaped


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
    `.lower()`/`.upper()` at their own call site.

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
            compression: str = None) -> List[ExportContext]:
        """
        Generates ExportContext objects for the given studies.

        Calculates output paths and metadata overrides for each instance in the
        provided studies.

        Args:
            patient (Patient): The patient object.
            studies (List[Study]): List of studies to export.
            out_dir (str): Output directory.
            compression (str, optional): Compression format (e.g. 'j2k').

        Returns:
            List[ExportContext]: List of prepared export contexts.
        """
        contexts = []
        for st in studies:
            for se in st.series:
                for inst in se.instances:
                    # Prepare Metadata used for directory structure AND overrides

                    # Patient Attributes
                    pat_attrs = {
                        "0010,0010": patient.patient_name,
                        "0010,0020": patient.patient_id
                    }

                    # Study Attributes
                    s_date_str = format_study_date(st.study_date)

                    study_attrs = {
                        "0020,000d": st.study_instance_uid,
                        "0008,0020": s_date_str,
                        "0008,0030": "120000"
                    }

                    # Series Attributes
                    series_attrs = {
                        "0020,000e": se.series_instance_uid,
                        "0008,0060": se.modality,
                        "0020,0011": se.series_number
                    }
                    if se.equipment:
                        series_attrs["0008,0070"] = se.equipment.manufacturer
                        series_attrs["0008,1090"] = se.equipment.model_name
                        series_attrs["0018,1000"] = se.equipment.device_serial_number

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
                        # Check if it's a SidecarPixelLoader
                        # We duck-type check for attributes
                        pl = inst._pixel_loader
                        if hasattr(pl, 'sidecar_path') and hasattr(pl, 'offset'):
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
                        pixel_alg=sc_alg
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
                uid = r.sop_instance_uid or r.output_path
                if failed:
                    loss = (f"{loss} The file itself was not written: this "
                            "instance's export failed after the element was "
                            "dropped.")
                logger.warning(f"{uid}: {loss}")
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
                uid = r.sop_instance_uid or r.output_path
                reason = r.error if r.error is not None else "unknown error"
                detail = f"Export failed for {r.output_path}: {reason}"
            else:
                # `run_parallel` returns its own exception when a worker
                # dies before it can answer. There is no outcome to name
                # the instance with, and the row still has to exist.
                uid = "UNKNOWN"
                detail = f"Export worker failed: {r}"

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
            compression (str, optional): Compression format ('j2k' or None).
            show_progress (bool): If True, shows a progress bar.
            executor (ProcessPoolExecutor, optional): Shared executor for parallelism.
            store_backend (SqliteStore, optional): Where to write a
                `DATA_LOSS` audit entry for each element that could not be
                written. Callers of this path usually have no session and
                so pass nothing; the losses are logged either way (#126).

        Raises:
            RuntimeError: If any instance failed to write.
        """
        if studies is None:
            studies = patient.studies
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        logger = get_logger()

        # Planning Phase: Generate Contexts
        export_tasks = DicomExporter._generate_export_contexts(
            patient, studies, out_dir, compression)

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
        # We don't raise here by default (batch mode). The failures are
        # audited rather than raised, and the summary is what lets the
        # caller say how many of the requested instances exist (#181).
        failures = DicomExporter._report_export_failures(results, store_backend)
        summary = ExportSummary(
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
        if compression == 'j2k':
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
    def _merge(ds, attrs, losses=None):
        """Merges a dictionary of attributes into a pydicom Dataset.

        `losses` is an optional list that collects `(scope, detail)` for
        every element that could not be written -- the description, plus
        which side of the private/standard line the tag fell on, which
        is what grades the run (#146). It is an accumulator rather than
        a return value because `_merge` is called six times per instance
        and the loss belongs to the instance, not the call.
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

            vr, encoded = None, None
            try:
                vr = dictionary_VR(Tag(g, e))
            except Exception:
                # Not a standard tag. Almost always a private (odd-group)
                # one, which is the whole point of `remove_private_tags=
                # False`: the caller asked to keep the vendor block, and
                # until #118 this arm only logged, so the tags reached
                # the object graph and the index and then never reached
                # the file (#118).
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
                    # said here, where the tag is still a tag. The
                    # backslash-bearing case never reaches this: the
                    # encoder returns None for it before the collapse.
                    if (encoded[0] == 'UT'
                            and isinstance(v, (list, tuple, MultiValue))
                            and len(v) > 1):
                        get_logger().warning(
                            "Tag %s written as a single UT value: one of "
                            "its %d values exceeds LO's 64-character cap, "
                            "so the multiplicity collapses from %d to 1. "
                            "The values are backslash-joined and "
                            "recoverable by splitting.", t, len(v), len(v))
                    vr, v = encoded
                ds.add_new(Tag(g, e), vr, v)
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
                loss = f"Tag {t} not exported (data loss): {exc}"
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

        PS3.5 A.1 makes `UN` the VR for an unknown value, and for raw
        bytes that is right. It is wrong for everything else: `UN` is an
        OB-family VR and rejects `str` at write time. Text needs a text
        VR, and `LO` caps at 64 characters, so longer values go to `UT`,
        which is unbounded. Numbers are stringified, which is what the
        EAV table (`instance_attributes.value_text`) would have done to
        them anyway -- without it, whether a private tag exported would
        depend on whether a save had happened yet.

        The `UT` branch narrows one thing: `UT` has a value multiplicity
        of 1, where `LO` is 1-n. A backslash-delimited value past 64
        characters therefore round-trips as one string containing literal
        backslashes rather than a list. Widening `LO` to cover it would
        be worse -- an over-long `LO` is non-conformant. The same VM-1
        property is why `UT` also takes any string containing `\`,
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
            #
            # The comment here used to claim '"True" is a better
            # round-trip than "1"', a distinction the code does not
            # make: nothing currently produces "1" for a bool. Under
            # this project's one-spelling rule a future reader is
            # entitled to delete a redundant arm, and that sentence was
            # the justification they would have used.
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
            return 'LO', []

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
    def _merge_sequences(ds, sequences: Dict[str, Any], losses=None):
        """
        Recursively populates sequences into the dataset.

        Args:
            ds (pydicom.Dataset): The dataset to modify.
            sequences (Dict[str, DicomSequence]): Dictionary mapping tags to Sequence objects.
        """
        for tag_str, dicom_seq in sequences.items():
            g, e = map(lambda x: int(x, 16), tag_str.split(','))
            tag = Tag(g, e)

            pydicom_seq = Sequence()
            for item in dicom_seq.items:
                # A sequence item is never encoded on its own: pydicom
                # writes it with the enclosing file's encoding, so these
                # flags were read by nothing even before 4.0 drops them.
                ds_item = Dataset()

                # Recursively merge item attributes and sub-sequences
                DicomExporter._merge(ds_item, item.attributes, losses)
                DicomExporter._merge_sequences(ds_item, item.sequences, losses)

                pydicom_seq.append(ds_item)

            ds.add_new(tag, 'SQ', pydicom_seq)
