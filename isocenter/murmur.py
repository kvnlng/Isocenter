"""Bridge DICOM Waveform Annotations to Murmur Studio's producer JSON.

Maps Waveform Annotation Sequence (0040,B020) into the
`<record>.annotations.json` format documented at
https://kvnlng.github.io/Murmur/annotation-schema

Coded concepts are passed through scheme-qualified, not normalized into a
vocabulary of Isocenter's own, so a finding says exactly what the
originating cart said.
"""
import json
import os
from typing import Any, Dict, List, Optional

from .config_manager import _vr_dummy
from .exporters.wfdb import _sanitize_description
# The (0040,A0B0) reading -- list coercion, 1-based ordinal, pair
# iteration -- lives in waveform.py, because the graph-side
# dangling-reference filter must read the pairs exactly as this bridge
# does. Import, never copy: a second parser is a second answer to
# "which group does this mark name".
from .waveform import (TAG_ANNOTATION_SEQ, TAG_REFERENCED_CHANNELS,
                       _as_list, _channel_pairs, _is_known_coding_scheme,
                       _item_index)

SCHEMA_VERSION = 1
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

# Ingest keeps Waveform Sequence (5400,0100) item 0 and discards the rest,
# so item 0 is the only multiplex group whose samples reach an
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


def _first_int(values) -> Optional[int]:
    for v in values:
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _referenced_channel(referenced_channels):
    """Classify an annotation's channel reference against the kept group.

    Args:
        referenced_channels: The Referenced Waveform Channels (0040,A0B0)
            value, or None.

    Returns:
        tuple: `(_REF_ABSENT, None)` when the attribute names no usable
        pair -- the annotation applies to the whole waveform;
        `(_REF_KEPT, channel_number)` for the first pair naming the
        ingested group, where the channel is 1-based and 0 means "all
        channels of that multiplex group" (PS3.3 C.10.10.1.1: "If the
        specified channel number is 0, the annotation applies to all
        channels in the multiplex group"), which resolves to no single
        lead; `(_REF_OTHER, group_ordinals)` when every pair names a
        group that was not ingested, carrying ALL of those distinct
        ordinals in the order the file wrote them.
    """
    # The `_REF_OTHER` list is every ordinal, not the first one: a single
    # annotation may name several groups, and reporting one of them would
    # under-report the loss. The scan stops early only on `_REF_KEPT`.
    others = []
    for group, channel in _channel_pairs(referenced_channels):
        if _item_index(group) == KEPT_WAVEFORM_ITEM_INDEX:
            return _REF_KEPT, channel
        if group not in others:
            others.append(group)
    if others:
        return _REF_OTHER, others
    return _REF_ABSENT, None


def _lead_for(waveform, referenced_channels) -> Optional[str]:
    """Resolve a Referenced Waveform Channels pair to a coded lead name.

    The attribute is a list of (multiplex group, channel) pairs, both
    1-based. `waveform.channels` is the INGESTED group's channel list, so
    a pair naming any other group resolves to None here, never to the
    ingested group's channel of that number. Callers drop such an
    annotation outright rather than emit it leadless (see
    `build_annotations`).

    Args:
        waveform (Waveform): The ingested multiplex group.
        referenced_channels: The Referenced Waveform Channels value.

    Returns:
        Optional[str]: The lead name, sanitized as the `.hea` signal line
            is, or None when the reference names no single channel of the
            ingested group.
    """
    # Sanitized with the same `_sanitize_description` the `.hea` signal
    # line gets: `wfdb_description()` returns a coded Channel Source value
    # outside its CID 3001 table verbatim (the lead-name allowlist guards
    # only the free-text Channel Label fallback), so a non-conformant
    # source can still carry an embedded newline, and annotations.json
    # must not carry a rawer value than the `.hea` file.
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

    Precondition: the annotation names the ingested group. Both branches
    are expressed on `waveform`'s sample axis, so on another group the
    result is wrong (a 1.0 s offset on a 1000 Hz group converted at the
    kept group's 500 Hz lands at half its true position).
    `build_annotations` checks first and drops the others.

    Args:
        item (DicomItem): The annotation item.
        waveform (Waveform): The ingested multiplex group.

    Returns:
        List[int]: The positions; `[]` when neither attribute yields one or
            the sampling frequency is zero.
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
    yields `UNCODED_CATEGORY` and no label, unless `include_text`.

    Args:
        item (DicomItem): The annotation item.
        include_text (bool): Release a site-defined concept's Code Meaning
            as the label and `<scheme>:<code>` (its Code Value alone when
            the scheme is empty) as the category. The same flag
            releases `note`: it means "this protocol permits free text in
            this output".

    Returns:
        tuple: `(category, label)`, the category `f"{scheme}:{code}"` (the
            Code Value alone when the scheme is empty);
            `(UNCODED_CATEGORY, None)` for a site-defined concept without
            `include_text`; `(None, None)` with
            no concept, and `(None, meaning)` for a concept with no Code
            Value.
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


def _real_note(item) -> str:
    """`item`'s Unformatted Text Value as a note, or `""` when it is the
    dummy a value-less REPLACE writes there.

    Text that really is the dummy (`ANONYMIZED`) is no note either.

    Args:
        item (DicomItem): The annotation item.

    Returns:
        str: The note, or `""`.
    """
    # The tag is D in PS3.15 Table E.1-1, so `basic` and the floor write the
    # text dummy over the annotation's text. Compared with
    # `config_manager._vr_dummy`, the one table, never a literal of its own,
    # as `exporters.wfdb._real_timing` does for the DT dummy.
    value = str(item.attributes.get(TAG_UNFORMATTED_TEXT, "") or "")
    return "" if value == _vr_dummy(TAG_UNFORMATTED_TEXT) else value


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
        dropped_groups (list, optional): Appended with one list per
            annotation dropped because it names no ingested group,
            holding the distinct multiplex group ordinals that
            annotation referenced: `len()` is how many marks were lost,
            and the union across the lists is which groups they pointed
            at. The caller writes the warning and the `DATA_LOSS` row.

    Returns:
        dict: A `schemaVersion: 1` document. `findings` is empty when the
        instance carries no annotations.
    """
    # `dropped_groups` is an out-parameter, not part of the return value:
    # the return value is the published Murmur document, so a new key there
    # is a schema change; this function has no logger or store handle; and
    # the caller aggregates, one audit row per instance, not per mark. Same
    # shape as `populate_attrs`'s `dropped_private_binary`.
    findings: List[Dict[str, Any]] = []

    seq = instance.sequences.get(TAG_ANNOTATION_SEQ)
    items = seq.items if seq is not None else []

    for item in items:
        referenced = item.attributes.get(TAG_REFERENCED_CHANNELS)
        state, referenced_groups = _referenced_channel(referenced)
        if state == _REF_OTHER:
            # Every position on this annotation is expressed on a
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
            # Nothing here presumes the multiplex discard is permanent: if
            # groups 1..n stop being discarded, this branch stops firing
            # on its own.
            if dropped_groups is not None:
                dropped_groups.append(list(referenced_groups))
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
            note = _real_note(item)
            if note:
                finding["note"] = note

        findings.append(finding)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "findings": findings,
    }


def write_annotations(path: str, document: Dict[str, Any]) -> Optional[str]:
    """Write an annotations document, skipping empty ones.

    Creates the parent directory if needed. Writes indented JSON with a
    trailing newline.

    Args:
        path (str): The file to write.
        document (Dict[str, Any]): The document `build_annotations` built.

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
