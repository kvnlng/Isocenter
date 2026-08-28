"""Bridge DICOM Waveform Annotations to Murmur Studio's producer JSON.

Maps Waveform Annotation Sequence (0040,B020) into the
`<record>.annotations.json` format documented at
https://kvnlng.github.io/Murmur/annotation-schema

Isocenter transcribes; it does not interpret. Coded concepts are passed
through scheme-qualified rather than normalized into a clinical
vocabulary of Isocenter's own, so a finding says exactly what the
originating cart said.
"""
import json
import os
from collections.abc import Sequence as _SequenceABC
from typing import Any, Dict, List, Optional

from .exporters.wfdb import _sanitize_description
from .waveform import _is_known_coding_scheme

SCHEMA_VERSION = 1

TAG_ANNOTATION_SEQ = "0040,b020"
TAG_REFERENCED_CHANNELS = "0040,a0b0"
TAG_TEMPORAL_RANGE_TYPE = "0040,a130"
TAG_REFERENCED_SAMPLE_POSITIONS = "0040,a132"
TAG_REFERENCED_TIME_OFFSETS = "0040,a138"
TAG_CONCEPT_NAME_CODE_SEQ = "0040,a043"
TAG_UNFORMATTED_TEXT = "0070,0006"

TAG_CODE_VALUE = "0008,0100"
TAG_CODING_SCHEME = "0008,0102"
TAG_CODE_MEANING = "0008,0104"

_RANGE_TYPES = {"SEGMENT", "MULTISEGMENT"}

# Category stamped on a finding whose Concept Name came from a
# site-defined scheme. The mark keeps its position and lead; only the
# name and the grouping are given up.
UNCODED_CATEGORY = "uncoded"

# Ingest keeps Waveform Sequence (5400,0100) item 0 and discards the rest
# (#36), so item 0 is the only multiplex group whose samples reach an
# export. Every position in this file is therefore expressed on item 0's
# sample axis, at item 0's rate.
KEPT_WAVEFORM_ITEM_INDEX = 0

# What `_referenced_channel` found in Referenced Waveform Channels.
#
# `_REF_ABSENT` is not a failure: (0040,A0B0) is Type 1C, and an
# annotation that names no channel applies to the whole waveform. It must
# still produce a finding, without a lead -- collapsing it into
# `_REF_OTHER` would silently empty annotations.json for a common
# conformant case.
_REF_ABSENT = "absent"
_REF_KEPT = "kept"
_REF_OTHER = "other"


def _as_list(value):
    if value is None:
        return []
    # pydicom yields a MultiValue (a MutableSequence, not a list/tuple
    # subclass) for multi-valued attributes such as Referenced Sample
    # Positions or Referenced Waveform Channels. Treat any non-string
    # Sequence as iterable so those values are not mistaken for a single
    # scalar element.
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, _SequenceABC) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


def _first_int(values) -> Optional[int]:
    for v in values:
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _item_index(group_ordinal: int) -> int:
    """Convert a multiplex group ordinal to a Waveform Sequence item index.

    DICOM numbers multiplex groups from 1: PS3.3 C.10.9.1.1 gives the
    first channel of the first multiplex group as 1\\1. Ordinal 1 is
    therefore item 0 -- the group Isocenter keeps.

    A group ordinal of 0 is not a valid 1-based ordinal, and `max` reads
    it as the first group rather than discarding it. That is the only
    sane reading: 0 cannot be confused with a group that survived,
    because there is no other group it could name, whereas rejecting it
    would drop every annotation a 0-counting source carries. Isocenter's
    own fixture generator wrote 0 until #159, which is exactly how long
    the value went unread. Do not "simplify" the `max` away.
    """
    return max(0, int(group_ordinal) - 1)


def _channel_pairs(referenced_channels):
    """Yield (group ordinal, channel number) pairs, both as DICOM wrote them.

    Referenced Waveform Channels (0040,A0B0) is VM 2-2n -- one annotation
    may name several (multiplex group, channel) pairs, e.g. all of group
    1 plus channels 2 and 3 of group 3. An odd trailing value cannot be
    paired and is ignored.
    """
    values = _as_list(referenced_channels)
    for i in range(0, len(values) - 1, 2):
        try:
            yield int(values[i]), int(values[i + 1])
        except (TypeError, ValueError):
            continue


def _referenced_channel(referenced_channels):
    """Classify an annotation's channel reference against the kept group.

    Returns:
        tuple: `(_REF_ABSENT, None)` when the attribute names no usable
        pair -- the annotation applies to the whole waveform;
        `(_REF_KEPT, channel_number)` for the first pair naming the
        ingested group, where the channel is 1-based and 0 means "all
        channels of that group"; `(_REF_OTHER, group_ordinal)` when every
        pair names a group that was not ingested, carrying the first such
        ordinal for the message the caller writes.
    """
    seen = None
    for group, channel in _channel_pairs(referenced_channels):
        if _item_index(group) == KEPT_WAVEFORM_ITEM_INDEX:
            return _REF_KEPT, channel
        if seen is None:
            seen = group
    if seen is not None:
        return _REF_OTHER, seen
    return _REF_ABSENT, None


def _lead_for(waveform, referenced_channels) -> Optional[str]:
    """Resolve a Referenced Waveform Channels pair to a coded lead name.

    The attribute is a list of (multiplex group, channel) pairs, both
    1-based. `waveform.channels` is the INGESTED group's channel list, so
    a pair naming any other group resolves to nothing here -- until #159
    the group half was read for the length check and then discarded, and
    an annotation on group 2 came back wearing group 1's channel name.
    Callers drop such an annotation outright rather than emit it leadless
    (see `build_annotations`); returning None keeps this function honest
    for the case where it is asked anyway.

    Sanitized with the same `_sanitize_description` the `.hea` signal
    line gets (`isocenter.exporters.wfdb`): `wfdb_description()` returns a
    coded Channel Source value verbatim -- it is not filtered by the
    lead-name allowlist, which only guards the free-text Channel Label
    fallback -- so a non-conformant source can still carry an embedded
    newline. Without this, `annotations.json` could carry a rawer value
    than the `.hea` file for the identical input.
    """
    state, channel_number = _referenced_channel(referenced_channels)
    if state != _REF_KEPT:
        return None

    index = channel_number - 1
    if 0 <= index < len(waveform.channels):
        return _sanitize_description(waveform.channels[index].wfdb_description(index))
    return None


def _sample_positions(item, waveform) -> List[int]:
    """Return 0-based sample positions for an annotation item.

    Prefers Referenced Sample Positions (1-based in DICOM). Falls back to
    Referenced Time Offsets, converted via the sampling frequency.

    `waveform` is the INGESTED multiplex group, and both branches are
    expressed on its sample axis: a sample position is an index into it,
    and `sampling_frequency` is its rate. Callers must therefore have
    established that the annotation names that group before calling --
    `build_annotations` does, and drops the ones that do not. Applying
    this to an annotation on another group is #159's second defect: a
    1.0 s offset on a 1000 Hz group converted at the kept group's 500 Hz
    lands at half its true position, in a record it does not describe.
    """
    positions = _as_list(item.attributes.get(TAG_REFERENCED_SAMPLE_POSITIONS))
    resolved = []
    for value in positions:
        try:
            resolved.append(max(0, int(value) - 1))
        except (TypeError, ValueError):
            continue
    if resolved:
        return resolved

    offsets = _as_list(item.attributes.get(TAG_REFERENCED_TIME_OFFSETS))
    fs = waveform.sampling_frequency or 0.0
    if not fs:
        return []
    for value in offsets:
        try:
            resolved.append(max(0, int(round(float(value) * fs))))
        except (TypeError, ValueError):
            continue
    return resolved


def _concept(item, include_text: bool = False):
    """Return (category, label) from the Concept Name Code Sequence.

    A Concept Name whose Coding Scheme Designator does not name a
    published vocabulary is site-defined, and its Code Meaning is then
    operator-typed free text rather than a term from a scheme -- the same
    property that makes Unformatted Text Value opt-in. Such a concept
    yields `UNCODED_CATEGORY` and no label.

    `include_text` is the auditor's override, not a debug switch: it is
    the same flag that releases `note`, and it already means "this
    protocol permits free text in this output".
    """
    seq = item.sequences.get(TAG_CONCEPT_NAME_CODE_SEQ)
    if seq is None or not seq.items:
        return None, None

    attrs = seq.items[0].attributes
    code = str(attrs.get(TAG_CODE_VALUE, "") or "")
    scheme = str(attrs.get(TAG_CODING_SCHEME, "") or "")
    meaning = str(attrs.get(TAG_CODE_MEANING, "") or "")

    if not code:
        return None, meaning or None

    if not include_text and not _is_known_coding_scheme(scheme):
        return UNCODED_CATEGORY, None

    category = f"{scheme}:{code}" if scheme else code
    return category, (meaning or None)


def build_annotations(instance, waveform, source: str, include_text: bool = False,
                      dropped_groups: Optional[List[int]] = None) -> Dict[str, Any]:
    """Build a Murmur annotations document from an instance's annotations.

    Args:
        instance (Instance): The waveform instance, post-remediation.
        waveform (Waveform): Parsed geometry of the INGESTED multiplex
            group, used for lead and time lookup. Annotations naming any
            other group are dropped rather than resolved against it --
            see `dropped`.
        source (str): Producer identifier written to the document.
        include_text (bool): If True, write Unformatted Text Value
            (0070,0006) into each finding's `note`. Defaults to False
            because that tag routinely holds free-text clinical
            commentary, and the PHI scan is tag-gated -- so a bare
            Session() would otherwise write it out unremediated.
        dropped_groups (list, optional): Appended with the multiplex
            group ordinal of every annotation dropped because it names a
            group that was not ingested. It carries ordinals rather than
            prose, and rides an out-parameter rather than the return
            value, for three reasons: the return value is the published
            Murmur document, so a new key there is a schema change; this
            function has neither a logger nor a store handle, so the
            warning and the `DATA_LOSS` entry belong to the caller; and
            the caller aggregates -- one instance with forty marks on a
            discarded group must file one audit row, not forty. Same
            channel and same shape as `populate_attrs`'s
            `dropped_private_binary`, which likewise hands back the tag
            and lets the caller word the message (#125).

    Returns:
        dict: A `schemaVersion: 1` document. `findings` is empty when the
        instance carries no annotations.
    """
    findings: List[Dict[str, Any]] = []

    seq = instance.sequences.get(TAG_ANNOTATION_SEQ)
    items = seq.items if seq is not None else []

    for item in items:
        referenced = item.attributes.get(TAG_REFERENCED_CHANNELS)
        state, referenced_group = _referenced_channel(referenced)
        if state == _REF_OTHER:
            # #159. Every position on this annotation is expressed on a
            # sample axis that is not in this record: a different rate,
            # a different length, different channels. Resolving it
            # against the surviving group produces a well-formed finding
            # with a plausible lead name at a plausible sample position,
            # both wrong -- and a mislabelled clinical mark is worse than
            # a missing one for the same reason a wrong grade is worse
            # than a missing row: it is not visibly absent.
            #
            # Clearing only the lead would not be enough. Referenced
            # Sample Positions index the named group's samples, so the
            # mark would still land at the wrong place in the exported
            # signal.
            #
            # This drop is correct whichever way #150 goes. If multi-rate
            # support lands and groups 1..n stop being discarded, the
            # test becomes "resolve against the right group" and this
            # branch stops firing on its own; nothing here presumes the
            # discard is permanent.
            if dropped_groups is not None:
                dropped_groups.append(referenced_group)
            continue

        category, label = _concept(item, include_text)
        if not category:
            # Without a category there is nothing for Murmur to colour or
            # group by, and the schema requires it.
            continue

        positions = _sample_positions(item, waveform)
        if not positions:
            continue

        range_type = str(item.attributes.get(TAG_TEMPORAL_RANGE_TYPE, "") or "").upper()
        is_range = range_type in _RANGE_TYPES and len(positions) >= 2

        finding: Dict[str, Any] = {
            "kind": "range" if is_range else "point",
            "startSample": positions[0],
            "category": category,
        }
        if is_range:
            finding["endSample"] = positions[1]
        if label:
            finding["label"] = label

        lead = _lead_for(waveform, referenced)
        if lead:
            finding["lead"] = lead

        if include_text:
            note = item.attributes.get(TAG_UNFORMATTED_TEXT)
            if note:
                finding["note"] = str(note)

        findings.append(finding)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "findings": findings,
    }


def write_annotations(path: str, document: Dict[str, Any]) -> Optional[str]:
    """Write an annotations document, skipping empty ones.

    Returns:
        Optional[str]: The path written, or None if there were no findings.
    """
    if not document.get("findings"):
        return None

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2)
        f.write("\n")

    return path
