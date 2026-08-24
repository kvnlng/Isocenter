"""DICOM Waveform Module object model.

Models the Waveform Sequence (5400,0100) and its Channel Definition
Sequence (003A,0200), and decodes Waveform Data (5400,1010) into a
NumPy array. Parsing only — no I/O.
"""
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
    """Raised for Waveform Sample Interpretations Gantry cannot decode."""


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
            "(mu-law/A-law), which Gantry does not decode.")

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

        Prefers the coded channel source, which cannot contain
        operator-typed text. Falls back to the free-text Channel Label
        ONLY when that label is a recognisable lead name; anything else is
        replaced with a positional token, because (003A,0203) is
        operator-typed SH and has been observed carrying names, MRNs and
        clinical commentary.

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
