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


def _lead_for(waveform, referenced_channels) -> Optional[str]:
    """Resolve a Referenced Waveform Channels pair to a coded lead name.

    The attribute is a (multiplex group, channel) pair with a 1-based
    channel number.

    Sanitized with the same `_sanitize_description` the `.hea` signal
    line gets (`isocenter.exporters.wfdb`): `wfdb_description()` returns a
    coded Channel Source value verbatim -- it is not filtered by the
    lead-name allowlist, which only guards the free-text Channel Label
    fallback -- so a non-conformant source can still carry an embedded
    newline. Without this, `annotations.json` could carry a rawer value
    than the `.hea` file for the identical input.
    """
    values = _as_list(referenced_channels)
    if len(values) < 2:
        return None
    try:
        channel_number = int(values[1])
    except (TypeError, ValueError):
        return None

    index = channel_number - 1
    if 0 <= index < len(waveform.channels):
        return _sanitize_description(waveform.channels[index].wfdb_description(index))
    return None


def _sample_positions(item, waveform) -> List[int]:
    """Return 0-based sample positions for an annotation item.

    Prefers Referenced Sample Positions (1-based in DICOM). Falls back to
    Referenced Time Offsets, converted via the sampling frequency.
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


def build_annotations(instance, waveform, source: str, include_text: bool = False) -> Dict[str, Any]:
    """Build a Murmur annotations document from an instance's annotations.

    Args:
        instance (Instance): The waveform instance, post-remediation.
        waveform (Waveform): Parsed geometry, used for lead and time lookup.
        source (str): Producer identifier written to the document.
        include_text (bool): If True, write Unformatted Text Value
            (0070,0006) into each finding's `note`. Defaults to False
            because that tag routinely holds free-text clinical
            commentary, and the PHI scan is tag-gated -- so a bare
            Session() would otherwise write it out unremediated.

    Returns:
        dict: A `schemaVersion: 1` document. `findings` is empty when the
        instance carries no annotations.
    """
    findings: List[Dict[str, Any]] = []

    seq = instance.sequences.get(TAG_ANNOTATION_SEQ)
    items = seq.items if seq is not None else []

    for item in items:
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

        lead = _lead_for(waveform, item.attributes.get(TAG_REFERENCED_CHANNELS))
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
