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

# `<` is a contract with ingest, not an assumption about the source: the
# sidecar holds samples little-endian, because `ingest_worker` converts a
# big-endian source's Waveform Data by Waveform Bits Allocated before it
# writes them. Do not thread a byte order through here -- there is
# none left to thread, and the exporters write these same bytes back
# verbatim under a little-endian syntax.
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
    # SCP-ECG (EN 1064 / ISO 11073-91064), "Standard Communications
    # Protocol for Computer-Assisted Electrocardiography" in PS3.16 2026d
    # Table 8-1, the only spelling that table gives it. CID 3001 drew its
    # lead codes from it before MDC, and ECG carts still code their
    # measurements (QT Interval, QRS Onset) in it.
    "SCPECG",
})


def _is_known_coding_scheme(designator: str) -> bool:
    """True if `designator` names a published vocabulary.

    Compared case-insensitively. A designator beginning "99" is never
    known.
    """
    # Case-insensitive although designators are case-sensitive by
    # specification: real datasets write "sct" and "Sct", and treating those
    # as site-defined would suppress genuinely coded concepts.
    normalized = _normalized_scheme(designator)
    if normalized.startswith("99"):
        return False
    return normalized in KNOWN_CODING_SCHEMES


def _normalized_scheme(designator: str) -> str:
    """`designator` stripped and upper-cased, the one way both the
    vocabulary check and the lead-name table compare a scheme."""
    return str(designator or "").strip().upper()


# The WFDB name of each coded ECG lead, keyed (scheme, Code Value), for the
# Channel Source (003A,0208) of a lead. Every row is a row of DICOM PS3.16
# CID 3001 "ECG Lead(s)":
#   - MDC: PS3.16 2026d, CID 3001 "ECG Lead", version 20130613
#     (ISO/IEEE 11073-10101). Not an earlier edition: 2011's table listed
#     `2:3` for both Lead III and Lead V1; 2026d gives III as `2:61`.
#   - SCPECG: PS3.16 2007, CID 3001 "ECG Leads", version 20020904, scheme
#     version 1.3 -- the "prior version of this Context Group [that] used
#     codes from the SCP-ECG vocabulary", in 2026d's note. Carts still write
#     it (pydicom's bundled `waveform_ecg.dcm` does).
# Only a lead whose name is in `KNOWN_LEAD_NAMES` is named, spelled as
# PhysioNet records spell it. The rest of the context group stays the
# verbatim Code Value, deliberately: the Frank "Lead I" (`2:24`,
# `5.6.3-9-24`) and "Derived Lead I" (`2:31`) are not Einthoven's I, and
# neither is a `-cal` row; -aVR, V2R and V6R-V9R have no name in that set.
# A code in neither scheme, and any Channel Source that is not a lead
# (pressure, respiration), is unchanged.
_CID_3001_LEAD_NAMES = {
    ("MDC", "2:1"): "I",          # Lead I
    ("MDC", "2:2"): "II",         # Lead II
    ("MDC", "2:61"): "III",       # Lead III
    ("MDC", "2:62"): "aVR",       # aVR, augmented voltage, right
    ("MDC", "2:63"): "aVL",       # aVL, augmented voltage, left
    ("MDC", "2:64"): "aVF",       # aVF, augmented voltage, foot
    ("MDC", "2:3"): "V1",         # Lead V1
    ("MDC", "2:4"): "V2",         # Lead V2
    ("MDC", "2:5"): "V3",         # Lead V3
    ("MDC", "2:6"): "V4",         # Lead V4
    ("MDC", "2:7"): "V5",         # Lead V5
    ("MDC", "2:8"): "V6",         # Lead V6
    ("MDC", "2:9"): "V7",         # Lead V7
    ("MDC", "2:66"): "V8",        # Lead V8
    ("MDC", "2:67"): "V9",        # Lead V9
    ("MDC", "2:11"): "V3R",       # Lead V3R
    ("MDC", "2:12"): "V4R",       # Lead V4R
    ("MDC", "2:13"): "V5R",       # Lead V5R
    ("MDC", "2:16"): "X",         # Lead X
    ("MDC", "2:17"): "Y",         # Lead Y
    ("MDC", "2:18"): "Z",         # Lead Z
    ("MDC", "2:92"): "MCL1",      # Modified chest lead per V1 placement
    ("MDC", "2:97"): "MCL6",      # Modified chest lead per V6 placement
    ("MDC", "2:131"): "ES",       # EASI Lead ES
    ("MDC", "2:132"): "AS",       # EASI Lead AS
    ("MDC", "2:133"): "AI",       # EASI Lead AI
    ("SCPECG", "5.6.3-9-1"): "I",     # Lead I (Einthoven)
    ("SCPECG", "5.6.3-9-2"): "II",    # Lead II
    ("SCPECG", "5.6.3-9-61"): "III",  # Lead III
    ("SCPECG", "5.6.3-9-62"): "aVR",  # Lead aVR
    ("SCPECG", "5.6.3-9-63"): "aVL",  # Lead aVL
    ("SCPECG", "5.6.3-9-64"): "aVF",  # Lead aVF
    ("SCPECG", "5.6.3-9-3"): "V1",    # Lead V1
    ("SCPECG", "5.6.3-9-4"): "V2",    # Lead V2
    ("SCPECG", "5.6.3-9-5"): "V3",    # Lead V3
    ("SCPECG", "5.6.3-9-6"): "V4",    # Lead V4
    ("SCPECG", "5.6.3-9-7"): "V5",    # Lead V5
    ("SCPECG", "5.6.3-9-8"): "V6",    # Lead V6
    ("SCPECG", "5.6.3-9-9"): "V7",    # Lead V7
    ("SCPECG", "5.6.3-9-66"): "V8",   # Lead V8
    ("SCPECG", "5.6.3-9-67"): "V9",   # Lead V9
    ("SCPECG", "5.6.3-9-11"): "V3R",  # Lead V3R
    ("SCPECG", "5.6.3-9-12"): "V4R",  # Lead V4R
    ("SCPECG", "5.6.3-9-13"): "V5R",  # Lead V5R
    ("SCPECG", "5.6.3-9-16"): "X",    # Lead X
    ("SCPECG", "5.6.3-9-17"): "Y",    # Lead Y
    ("SCPECG", "5.6.3-9-18"): "Z",    # Lead Z
}


def _is_known_lead_name(label: str) -> bool:
    """True if `label` is a recognisable signal name rather than free text.

    Normalises case, collapses internal whitespace, and drops an optional
    "lead " prefix before comparing, so "I", "Lead I" and "LEAD  I" all
    match.
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
# this mark name".
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

    DICOM numbers multiplex groups from 1 (PS3.3 C.10.10.1.1: an
    annotation covering all of the first group plus channels 2 and 3 of
    the third is written `0001 0000 0003 0002 0003 0003`), so ordinal 1 is
    item 0, the group Isocenter keeps. An ordinal of 0 also reads as item
    0.

    Args:
        group_ordinal (int): The 1-based ordinal, as DICOM wrote it.

    Returns:
        int: The 0-based item index.
    """
    # Ordinal 0 is not a valid 1-based ordinal; `max` reads it as the first
    # group rather than discarding it. It cannot be confused with a group
    # that survived, and rejecting it would drop every annotation a
    # 0-counting source carries. Do not "simplify" the `max` away.
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

    Call it wherever multiplex groups beyond `kept_items` have just been
    removed from the graph: Waveform Annotation Sequence (0040,B020) lives
    at instance level, so an annotation naming a removed group's ordinal
    would point at nothing in the exported file.

    Edits `entity` in place, pair by pair: an annotation's Referenced
    Waveform Channels (0040,A0B0) loses the pairs naming removed groups,
    and the annotation item is dropped only when every pair it named is
    gone. Surviving ordinals are never renumbered. An annotation with no
    (0040,A0B0), or with none that parses as a pair, is left alone, as is
    the value of any item nothing was removed from. When every annotation
    is dropped, the sequence itself is deleted.

    Args:
        entity (Instance | DicomItem): A graph item whose `sequences`
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
    # Never renumber: the ordinal is positional, and renumbering after a
    # discard would make the file internally consistent and wrong relative
    # to the source. Kept references name kept items, whose positions did
    # not move, so the values that remain are already correct.
    #
    # An annotation with no (0040,A0B0) applies to the whole waveform and
    # cannot dangle; one with no parseable pair has no reference to judge,
    # and deleting on a guess would drop a placeable mark.
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
        UnsupportedInterpretation: For mu-law/A-law companded audio, or an
            interpretation this module has no dtype for.
        ValueError: If the payload is not a whole number of samples or
            its length does not match the geometry.
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

        The reciprocal of sensitivity times its correction factor (DICOM
        stores sensitivity as physical units per ADC unit).

        Returns:
            float: The gain; 1.0 when the effective sensitivity is zero or
                absent, since WFDB reads a zero gain as *uncalibrated*.
        """
        effective = self.sensitivity * self.correction_factor
        if not effective:
            return 1.0
        return 1.0 / effective

    def wfdb_baseline(self) -> int:
        """ADC value corresponding to zero physical units.

        DICOM: physical = adc / gain + baseline
        Setting physical = 0 gives adc = -baseline * gain.

        Returns:
            int: `-baseline * gain`, rounded.
        """
        return int(round(-self.baseline * self.gain()))

    @classmethod
    def from_dicom_item(cls, item: Any) -> "WaveformChannel":
        """Build from a Channel Definition Sequence item.

        Args:
            item (DicomItem): A Channel Definition Sequence (003A,0200) item.

        Returns:
            WaveformChannel: Label, coded source, units (default `"mV"`),
                sensitivity and correction (default 1.0) and baseline
                (default 0.0) as the item carries them.
        """
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

        The coded channel source when there is one: the lead's name
        (`I`, `aVR`, `V1` ...) when the source is an ECG lead of DICOM CID
        3001 in scheme MDC or SCPECG, and otherwise its Code Value
        verbatim. A verbatim value is not filtered, so a non-conformant
        source can put anything here, an embedded newline included;
        callers writing it into a line-based format must sanitize it.
        Otherwise the Channel Label (003A,0203),
        only when it is a recognisable lead name, since the label is
        operator-typed text. Otherwise a positional token. This applies
        whatever the privacy policy does with Channel Label.

        Args:
            index (int, optional): Zero-based channel index, used for the
                positional token. Callers without one get "signal".

        Returns:
            str: The coded lead's name, the coded source, the stripped lead
                name, `ch<index>`, or `"signal"`.
        """
        # The label check lives here rather than in the privacy profile: the
        # PHI scan is tag-gated, so a profile entry protects only a session
        # whose policy carries it, and `privacy_profile: none` or a `KEEP`
        # on Channel Label leaves the label to this check alone.
        if self.source_code:
            # The scheme is compared as `_is_known_coding_scheme` compares
            # it, case-insensitively, since carts write `sct` for `SCT`;
            # the two must not disagree about which scheme a code is in.
            # The Code Value is compared exactly: it is case-sensitive, and
            # every key is digits and punctuation. A `99...` scheme matches
            # no key, so a local code is never named.
            key = (_normalized_scheme(self.source_scheme), self.source_code)
            return _CID_3001_LEAD_NAMES.get(key, self.source_code)
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
        """Build from a Waveform Sequence item (metadata only, no samples).

        Args:
            item (DicomItem): A Waveform Sequence (5400,0100) item.

        Returns:
            Waveform: Geometry and channels as the item declares them;
                `bits_allocated` defaults to 16 and `sample_interpretation`
                to `"SS"`. `samples` is None.
        """
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
        """Decode raw Waveform Data using this item's geometry.

        Also stores the result on `self.samples`.

        Args:
            data (bytes): Raw Waveform Data (5400,1010).

        Returns:
            np.ndarray: int16, shape (num_samples, num_channels).

        Raises:
            UnsupportedInterpretation: Propagated from `decode_samples`,
                for mu-law/A-law companded audio or an interpretation it
                has no dtype for.
            ValueError: Propagated from `decode_samples`, if the payload
                is not a whole number of samples or its length does not
                match the geometry.
        """
        self.samples = decode_samples(
            data, self.sample_interpretation, self.num_samples, self.num_channels)
        return self.samples
