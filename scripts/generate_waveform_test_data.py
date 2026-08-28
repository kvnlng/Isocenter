"""Generate synthetic 12-lead ECG DICOM files for Isocenter's waveform tests.

Signals are deterministic and analytically known, so a round-trip through
WFDB export can be asserted against exact expected physical values.
"""
import os

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

# 12-Lead ECG Waveform Storage
ECG_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.9.1.1"

# (CodeValue, CodeMeaning) from the MDC coding scheme.
LEADS = [
    ("MDC_ECG_LEAD_I", "Lead I"),
    ("MDC_ECG_LEAD_II", "Lead II"),
    ("MDC_ECG_LEAD_V1", "Lead V1"),
    ("MDC_ECG_LEAD_V2", "Lead V2"),
    ("MDC_ECG_LEAD_V3", "Lead V3"),
    ("MDC_ECG_LEAD_V4", "Lead V4"),
    ("MDC_ECG_LEAD_V5", "Lead V5"),
    ("MDC_ECG_LEAD_V6", "Lead V6"),
]


def _code(value, meaning, scheme):
    """Build a single-item coded sequence Dataset."""
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = scheme
    item.CodeMeaning = meaning
    return item


def _synthetic_signal(num_samples, num_channels):
    """Deterministic ramp-plus-offset, distinct per channel.

    Channel c holds sample values ``(i % 1000) + c * 1000`` so any
    channel mix-up or transposition during export is immediately visible.
    """
    idx = np.arange(num_samples, dtype=np.int32)
    out = np.empty((num_samples, num_channels), dtype=np.int16)
    for c in range(num_channels):
        out[:, c] = ((idx % 1000) + c * 1000).astype(np.int16)
    return out


def build_ecg_dataset(num_samples=5000,
                      sampling_frequency=500.0,
                      channels=None,
                      baseline_uv=0.0,
                      units="uV",
                      patient_id="WFTEST001",
                      patient_name="Waveform^Test"):
    """Build an in-memory 12-Lead ECG Waveform Storage Dataset.

    Args:
        num_samples (int): Samples per channel.
        sampling_frequency (float): Hz.
        channels (list, optional): (code, meaning) pairs. Defaults to LEADS.
        baseline_uv (float): Channel Baseline, in `units`.
        units (str): UCUM unit code for Channel Sensitivity Units.
        patient_id (str): PatientID value.
        patient_name (str): PatientName value.

    Returns:
        pydicom.Dataset: A complete, writable ECG dataset.
    """
    channels = channels or LEADS
    n_ch = len(channels)

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ECG_SOP_CLASS_UID
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    ds.SOPClassUID = ECG_SOP_CLASS_UID
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = "ECG"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1

    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.PatientBirthDate = "19570314"
    ds.StudyDate = "20260101"
    ds.StudyTime = "101530"
    ds.AcquisitionDateTime = "20260101101530.000000"
    ds.Manufacturer = "IsocenterTest"
    ds.ManufacturerModelName = "SyntheticCart"
    ds.DeviceSerialNumber = "SN-ECG-001"

    samples = _synthetic_signal(num_samples, n_ch)

    chdefs = []
    for channel_number, (code_value, code_meaning) in enumerate(channels, start=1):
        chdef = Dataset()
        chdef.ChannelNumber = channel_number
        chdef.ChannelSensitivity = "1.0"
        chdef.ChannelSensitivityCorrectionFactor = "1.0"
        chdef.ChannelBaseline = str(baseline_uv)
        chdef.ChannelSensitivityUnitsSequence = [_code(units, units, "UCUM")]
        chdef.ChannelSourceSequence = [_code(code_value, code_meaning, "MDC")]
        chdef.ChannelLabel = code_meaning
        chdef.WaveformBitsStored = 16
        chdefs.append(chdef)

    wf = Dataset()
    wf.WaveformOriginality = "ORIGINAL"
    wf.NumberOfWaveformChannels = n_ch
    wf.NumberOfWaveformSamples = num_samples
    wf.SamplingFrequency = str(sampling_frequency)
    wf.ChannelDefinitionSequence = chdefs
    wf.WaveformBitsAllocated = 16
    wf.WaveformSampleInterpretation = "SS"
    wf.WaveformData = samples.tobytes()

    ds.WaveformSequence = [wf]
    return ds


def add_annotation(ds, start_sample=100, end_sample=None,
                   code_value="164889003", code_meaning="Atrial fibrillation",
                   scheme="SCT", text=None, channel=1, group=1,
                   time_offsets=None):
    """Attach a Waveform Annotation Sequence item to `ds`.

    Args:
        ds (Dataset): Dataset to modify in place.
        start_sample (int): 1-based Referenced Sample Position, per DICOM.
        end_sample (int, optional): Second position, making this a SEGMENT.
        code_value (str): Concept Name code value.
        code_meaning (str): Concept Name code meaning.
        scheme (str): Coding scheme designator.
        text (str, optional): Unformatted Text Value.
        channel (int): 1-based Referenced Waveform Channel.
        group (int): 1-based multiplex group ordinal -- the ordinal of
            the Waveform Sequence item, where the first group is 1
            (PS3.3 C.10.10.1.1 "Referenced Channels"; its worked example
            writes the first multiplex group as `0001`). This used to be
            hardcoded to 0, which is not a valid ordinal; it went
            unnoticed because the annotation bridge ignored the value
            entirely (#159). Pass 2 for the second group -- the one
            ingest discards.
        time_offsets (list, optional): Referenced Time Offsets in
            seconds. When given, they are written INSTEAD of Referenced
            Sample Positions, which is the only way to exercise the
            bridge's seconds-to-samples fallback: DICOM prefers sample
            positions and so does the bridge.

    Returns:
        Dataset: The same dataset, for chaining.
    """
    ann = Dataset()
    ann.ReferencedWaveformChannels = [group, channel]
    ann.ConceptNameCodeSequence = [_code(code_value, code_meaning, scheme)]

    if time_offsets is not None:
        offsets = list(time_offsets)
        ann.TemporalRangeType = "POINT" if len(offsets) < 2 else "SEGMENT"
        ann.ReferencedTimeOffsets = offsets
    elif end_sample is None:
        ann.TemporalRangeType = "POINT"
        ann.ReferencedSamplePositions = [start_sample]
    else:
        ann.TemporalRangeType = "SEGMENT"
        ann.ReferencedSamplePositions = [start_sample, end_sample]

    if text is not None:
        ann.UnformattedTextValue = text

    existing = list(getattr(ds, "WaveformAnnotationSequence", []))
    existing.append(ann)
    ds.WaveformAnnotationSequence = existing
    return ds


def write_fixture(path, **kwargs):
    """Write a generated ECG dataset to `path` and return the path."""
    ds = build_ecg_dataset(**kwargs)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    pydicom.dcmwrite(path, ds, enforce_file_format=True)
    return path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="Output .dcm path")
    parser.add_argument("--samples", type=int, default=5000)
    parser.add_argument("--fs", type=float, default=500.0)
    parser.add_argument("--baseline", type=float, default=0.0)
    args = parser.parse_args()

    written = write_fixture(args.output,
                            num_samples=args.samples,
                            sampling_frequency=args.fs,
                            baseline_uv=args.baseline)
    print(f"Wrote {written}")
