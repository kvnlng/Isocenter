"""DICOM Waveform Module object model.

Models the Waveform Sequence (5400,0100) and its Channel Definition
Sequence (003A,0200), and decodes Waveform Data (5400,1010) into a
NumPy array. Parsing only — no I/O.
"""
from collections.abc import Sequence as _SequenceABC
from dataclasses import dataclass, field
from typing import Any, List, Optional

import numpy as np

# Waveform Sequence item tags
TAG_NUM_CHANNELS = "003a,0005"
TAG_NUM_SAMPLES = "003a,0010"
TAG_SAMPLING_FREQUENCY = "003a,001a"
TAG_CHANNEL_DEFINITION_SEQ = "003a,0200"
TAG_BITS_ALLOCATED = "5400,1004"
TAG_SAMPLE_INTERPRETATION = "5400,1006"

# Channel Definition item tags
TAG_CHANNEL_SOURCE_SEQ = "003a,0208"
TAG_CHANNEL_SENSITIVITY = "003a,0210"
TAG_CHANNEL_SENSITIVITY_UNITS_SEQ = "003a,0211"
TAG_CHANNEL_SENSITIVITY_CORRECTION = "003a,0212"
TAG_CHANNEL_BASELINE = "003a,0213"
TAG_CHANNEL_LABEL = "003a,0203"

# Coded entry tags
TAG_CODE_VALUE = "0008,0100"
TAG_CODING_SCHEME = "0008,0102"
TAG_CODE_MEANING = "0008,0104"

_DTYPES = {
    "SS": "<i2",
    "US": "<u2",
    "SB": "i1",
    "UB": "u1",
}

_COMPANDED = {"MB", "AB"}

# Recognisable physiological signal names. Channel Label (003A,0203) is
# operator-typed SH text, so anything NOT on this list is treated as free
# text and replaced with a positional token rather than written into the
# .hea header or annotations.json. Compared case-insensitively after
# stripping an optional "lead " prefix, so "Lead I", "lead I" and "I" all
# match.
KNOWN_LEAD_NAMES = frozenset({
    # 12-lead ECG
    "i", "ii", "iii", "avr", "avl", "avf",
    "v1", "v2", "v3", "v4", "v5", "v6",
    # Extended / posterior / right-sided
    "v7", "v8", "v9", "v3r", "v4r", "v5r",
    # Monitoring
    "mcl1", "mcl6",
    # Frank orthogonal (vectorcardiography)
    "x", "y", "z",
    # EASI
    "es", "as", "ai",
    # Common non-ECG physiological channels
    "resp", "pleth", "spo2", "co2",
    "abp", "art", "cvp", "pap", "icp",
})


# Coding scheme designators naming a published vocabulary. A Concept Name
# drawn from one of these carries a term somebody else defined and
# maintains; anything else -- including an absent designator -- is
# site-defined, which in practice means an operator typed it.
#
# DICOM PS3.3 reserves designators beginning "99" for locally defined
# schemes, so no "99..." value can ever belong here however conformant it
# looks: a site's own "99ACME" is precisely the case this guards against.
# `_is_known_coding_scheme` re-checks that prefix rather than trusting the
# set to stay clean, because the tempting fix for "our codes are being
# suppressed" is to add the site's designator to this list.
KNOWN_CODING_SCHEMES = frozenset({
    "DCM",      # DICOM Controlled Terminology
    "SCT",      # SNOMED CT
    "SRT",      # SNOMED RT (retired, still emitted by older carts)
    "SNM3",     # SNOMED v3 (ditto)
    "LN",       # LOINC
    "MDC",      # IEEE 11073-10101 nomenclature -- the ECG lead codes
    "UCUM",     # Unified Code for Units of Measure
    "NCIT",     # NCI Thesaurus
    "RADLEX",
    "ACR",
})


def _is_known_coding_scheme(designator: str) -> bool:
    """True if `designator` names a published vocabulary.

    Compared case-insensitively. Designators are case-sensitive by
    specification, but real datasets write "sct" and "Sct", and treating
    those as site-defined would suppress genuinely coded concepts -- the
    same tolerance `_is_known_lead_name` applies for the same reason.
    """
    normalized = str(designator or "").strip().upper()
    if normalized.startswith("99"):
        return False
    return normalized in KNOWN_CODING_SCHEMES


def _is_known_lead_name(label: str) -> bool:
    """True if `label` is a recognisable signal name rather than free text.

    Normalises case, collapses internal whitespace, and drops an optional
    "lead " prefix before comparing, because DICOM sources write the same
    lead as "I", "Lead I" and "LEAD  I" interchangeably.
    """
    normalized = " ".join(str(label or "").split()).lower()
    if normalized.startswith("lead "):
        normalized = normalized[len("lead "):]
    return normalized in KNOWN_LEAD_NAMES


class UnsupportedInterpretation(ValueError):
    """Raised for Waveform Sample Interpretations Isocenter cannot decode."""


def _as_float(value, default=0.0):
    """Coerce a DICOM DS/attribute value to float, tolerating None and lists."""
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
        if value is None:
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    return int(_as_float(value, default))


# Waveform Annotation Module tags. Defined here rather than in murmur.py
# because two consumers read the same pairs and must read them the same
# way: the Murmur bridge resolves annotations against the kept group,
# and the graph-side filter below drops references to discarded ones. A
# second parser of (0040,A0B0) is a second answer to "which group does
# this mark name", which is how #159 happened.
TAG_ANNOTATION_SEQ = "0040,b020"
TAG_REFERENCED_CHANNELS = "0040,a0b0"


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


def _item_index(group_ordinal: int) -> int:
    """Convert a multiplex group ordinal to a Waveform Sequence item index.

    DICOM numbers multiplex groups from 1. PS3.3 C.10.10.1.1
    ("Referenced Channels", Waveform Annotation Module) defines the
    Attribute as pairs (M,C) where M is "the ordinal of the Item of
    Waveform Sequence (5400,0100)", and its own worked example is
    explicit about the base: an annotation covering the entire FIRST
    multiplex group plus channels 2 and 3 of the THIRD is written
    `0001 0000 0003 0002 0003 0003`. Ordinal 1 is therefore item 0 --
    the group Isocenter keeps.

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


def filter_dangling_annotation_refs(entity, kept_items: int):
    """Drop annotation references to Waveform Sequence items not kept.

    Runs wherever multiplex groups beyond `kept_items` have just been
    removed from the graph (#160's `del` at ingest, and the hydration
    heal for stores written before it). Waveform Annotation Sequence
    (0040,B020) lives at instance level, not inside the group it refers
    to, so removing an item leaves any annotation naming its ordinal
    pointing at nothing in the exported file -- exactly the kind of
    reference a strict downstream reader rejects (#177).

    It is a filter on (group, channel) PAIRS before it is a drop of
    items: an annotation may name all of group 1 plus a channel of
    group 3, and the surviving pairs keep their placeable home. An item
    goes only when every pair it named is gone.

    The ordinals of surviving references are NEVER renumbered. The
    ordinal is positional -- "the ordinal of the Item of Waveform
    Sequence" -- so after a discard, renumbering would make the file
    internally consistent and *wrong* relative to the source, with no
    way to tell afterwards. Kept references name kept items, whose
    positions did not move (only items past `kept_items` are removed),
    so the values that remain are already correct.

    Two shapes are deliberately left alone: an annotation with no
    (0040,A0B0) at all (Type 1C -- it applies to the whole waveform and
    cannot dangle), and one whose value yields no parseable pair (there
    is no reference to know is dangling, and deleting on a guess would
    drop a mark the source considered placeable). An untouched item's
    value is not rewritten either, so a compliant file round-trips
    byte-identically.

    Args:
        entity: A graph item (Instance or DicomItem) whose `sequences`
            may hold (0040,B020).
        kept_items (int): How many Waveform Sequence items survive.
            References resolving to item indexes below this are kept.

    Returns:
        Tuple[int, int, List[int]]: (annotation items dropped,
        annotation items whose reference list was rewritten to its
        surviving pairs, sorted distinct group ordinals the removed
        references named -- so the caller's loss report can say *which*
        groups, the way the WFDB bridge's does).
    """
    ann_seq = entity.sequences.get(TAG_ANNOTATION_SEQ)
    if ann_seq is None:
        return (0, 0, [])

    dropped = 0
    rewritten = 0
    removed_ordinals = set()
    surviving_items = []
    for item in ann_seq.items:
        refs = item.attributes.get(TAG_REFERENCED_CHANNELS)
        pairs = list(_channel_pairs(refs)) if refs is not None else []
        if refs is None or not pairs:
            surviving_items.append(item)
            continue

        kept_pairs = [(g, c) for g, c in pairs
                      if _item_index(g) < kept_items]
        removed_ordinals.update(g for g, _c in pairs
                                if _item_index(g) >= kept_items)
        if not kept_pairs:
            dropped += 1
            continue
        if len(kept_pairs) < len(pairs):
            item.attributes[TAG_REFERENCED_CHANNELS] = [
                v for pair in kept_pairs for v in pair]
            rewritten += 1
        surviving_items.append(item)

    if dropped:
        if surviving_items:
            ann_seq.items[:] = surviving_items
        else:
            # Waveform Annotation Sequence is Type 1 in its module:
            # present means at least one item. An empty shell would
            # trade one conformance violation for another.
            del entity.sequences[TAG_ANNOTATION_SEQ]
    return (dropped, rewritten, sorted(removed_ordinals))


def decode_samples(data: bytes,
                   interpretation: str,
                   num_samples: int,
                   num_channels: int) -> np.ndarray:
    """Decode raw Waveform Data into an int16 array.

    Args:
        data (bytes): Raw Waveform Data (5400,1010).
        interpretation (str): Waveform Sample Interpretation (5400,1006).
        num_samples (int): Samples per channel.
        num_channels (int): Channel count.

    Returns:
        np.ndarray: int16, shape (num_samples, num_channels).

    Raises:
        UnsupportedInterpretation: For mu-law/A-law companded audio.
        ValueError: If the payload length does not match the geometry.
    """
    interp = (interpretation or "SS").strip().upper()

    if interp in _COMPANDED:
        raise UnsupportedInterpretation(
            f"Sample Interpretation {interp!r} is companded audio "
            "(mu-law/A-law), which Isocenter does not decode.")

    dtype = _DTYPES.get(interp)
    if dtype is None:
        raise UnsupportedInterpretation(
            f"Unknown Waveform Sample Interpretation: {interp!r}")

    expected = num_samples * num_channels
    arr = np.frombuffer(data, dtype=dtype)
    if arr.size != expected:
        raise ValueError(
            f"Waveform payload has {arr.size} samples, expected {expected} "
            f"({num_samples} samples x {num_channels} channels).")

    arr = arr.reshape(num_samples, num_channels)

    if interp == "US":
        # Rebase unsigned onto the signed 16-bit range so one .dat format
        # (WFDB 16) serves every input interpretation.
        return (arr.astype(np.int32) - 32768).astype(np.int16)

    return arr.astype(np.int16, copy=False)


@dataclass
class WaveformChannel:
    """One channel's identity and calibration."""

    label: str = ""
    source_code: str = ""
    source_scheme: str = ""
    sensitivity: float = 1.0
    correction_factor: float = 1.0
    units: str = "mV"
    baseline: float = 0.0

    def gain(self) -> float:
        """ADC units per physical unit, as WFDB defines gain.

        DICOM stores sensitivity as physical units per ADC unit, so this is
        its reciprocal. Returns 1.0 rather than raising when sensitivity is
        absent or zero — a zero gain would be read by WFDB as *uncalibrated*.
        """
        effective = self.sensitivity * self.correction_factor
        if not effective:
            return 1.0
        return 1.0 / effective

    def wfdb_baseline(self) -> int:
        """ADC value corresponding to zero physical units.

        DICOM: physical = adc / gain + baseline
        Setting physical = 0 gives adc = -baseline * gain.
        """
        return int(round(-self.baseline * self.gain()))

    @classmethod
    def from_dicom_item(cls, item: Any) -> "WaveformChannel":
        """Build from a Channel Definition Sequence item."""
        attrs = item.attributes
        seqs = item.sequences

        source_code = ""
        source_scheme = ""
        src = seqs.get(TAG_CHANNEL_SOURCE_SEQ)
        if src is not None and src.items:
            source_code = str(src.items[0].attributes.get(TAG_CODE_VALUE, "") or "")
            source_scheme = str(src.items[0].attributes.get(TAG_CODING_SCHEME, "") or "")

        units = "mV"
        unit_seq = seqs.get(TAG_CHANNEL_SENSITIVITY_UNITS_SEQ)
        if unit_seq is not None and unit_seq.items:
            units = str(unit_seq.items[0].attributes.get(TAG_CODE_VALUE, "mV") or "mV")

        return cls(
            label=str(attrs.get(TAG_CHANNEL_LABEL, "") or ""),
            source_code=source_code,
            source_scheme=source_scheme,
            sensitivity=_as_float(attrs.get(TAG_CHANNEL_SENSITIVITY), 1.0),
            correction_factor=_as_float(attrs.get(TAG_CHANNEL_SENSITIVITY_CORRECTION), 1.0),
            units=units,
            baseline=_as_float(attrs.get(TAG_CHANNEL_BASELINE), 0.0),
        )

    def wfdb_description(self, index: Optional[int] = None) -> str:
        """Signal description for the .hea signal line and annotations `lead`.

        Prefers the coded channel source: a conformant CodeValue (SH, per
        the DICOM standard) is far less likely to carry operator-typed
        text than a free-text label. That is a likelihood argument, not a
        guarantee -- this method does NOT run the coded value through the
        lead-name allowlist below or `_sanitize_description`, and pydicom
        does not enforce SH's content restrictions on read or write, so a
        non-conformant (or malicious) source can still put anything here,
        including an embedded newline (see
        `test_coded_channel_source_newline_cannot_manufacture_a_hea_comment`
        in `tests/test_wfdb_conformance.py`, which proves exactly that).
        The `.hea` writer and the Murmur bridge both sanitize line-break
        characters out of whatever this returns as their own last line of
        defense.

        Falls back to the free-text Channel Label ONLY when that label is
        a recognisable lead name; anything else is replaced with a
        positional token, because (003A,0203) is operator-typed SH and has
        been observed carrying names, MRNs and clinical commentary.

        The check lives here rather than in the privacy profile on purpose:
        the PHI scan is tag-gated, so a profile entry protects only sessions
        that loaded a configuration. A bare Session() would still leak.

        Args:
            index (int, optional): Zero-based channel index, used for the
                positional token. Callers without one get "signal".
        """
        if self.source_code:
            return self.source_code
        if self.label and _is_known_lead_name(self.label):
            return self.label.strip()
        return f"ch{index}" if index is not None else "signal"


@dataclass
class Waveform:
    """One Waveform Sequence item: geometry, calibration, and samples."""

    sampling_frequency: float = 0.0
    num_channels: int = 0
    num_samples: int = 0
    bits_allocated: int = 16
    sample_interpretation: str = "SS"
    channels: List[WaveformChannel] = field(default_factory=list)
    samples: Optional[np.ndarray] = None

    @classmethod
    def from_dicom_item(cls, item: Any) -> "Waveform":
        """Build from a Waveform Sequence item (metadata only, no samples)."""
        attrs = item.attributes
        seqs = item.sequences

        channels = []
        chdefs = seqs.get(TAG_CHANNEL_DEFINITION_SEQ)
        if chdefs is not None:
            channels = [WaveformChannel.from_dicom_item(i) for i in chdefs.items]

        return cls(
            sampling_frequency=_as_float(attrs.get(TAG_SAMPLING_FREQUENCY)),
            num_channels=_as_int(attrs.get(TAG_NUM_CHANNELS)),
            num_samples=_as_int(attrs.get(TAG_NUM_SAMPLES)),
            bits_allocated=_as_int(attrs.get(TAG_BITS_ALLOCATED), 16) or 16,
            sample_interpretation=str(attrs.get(TAG_SAMPLE_INTERPRETATION, "SS") or "SS"),
            channels=channels,
        )

    def decode(self, data: bytes) -> np.ndarray:
        """Decode raw Waveform Data using this item's geometry."""
        self.samples = decode_samples(
            data, self.sample_interpretation, self.num_samples, self.num_channels)
        return self.samples
