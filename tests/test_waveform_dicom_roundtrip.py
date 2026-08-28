"""DICOM -> DICOM export must carry the waveform samples (#34).

#9 fixed the ingest half: samples reach the sidecar. The export half
never wrote them back, so a round-trip produced a structurally plausible
file -- correct SOP Class, correct Modality, a complete Waveform Sequence
with NumberOfWaveformSamples, SamplingFrequency and a full Channel
Definition Sequence -- carrying no signal at all.
"""
import copy
import glob
import os

import numpy as np
import pydicom
import pytest

from isocenter.session import DicomSession
from scripts.generate_waveform_test_data import build_ecg_dataset, write_fixture


def _export_roundtrip(tmp_path, src_file, db_name="rt.db"):
    """Ingest `src_file`, export as DICOM, return the re-read dataset."""
    out = tmp_path / "out"
    session = DicomSession(persistence_file=str(tmp_path / db_name))
    try:
        session.ingest(os.path.dirname(src_file))
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"
    return pydicom.dcmread(written[0])


@pytest.fixture
def ecg_src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return write_fixture(str(src / "ecg.dcm"), num_samples=400)


def test_dicom_export_writes_waveform_data_back(tmp_path, ecg_src):
    ds = _export_roundtrip(tmp_path, ecg_src)

    assert "WaveformSequence" in ds
    item = ds.WaveformSequence[0]
    assert getattr(item, "WaveformData", None), \
        "exported file carries a Waveform Sequence but no samples"


def test_the_round_tripped_waveform_is_byte_identical(tmp_path, ecg_src):
    """Samples are never mutated in memory, so a re-encode could only lose.

    Nothing in the pipeline modifies waveform samples -- unlike pixels,
    which redaction burns into -- so the sidecar's original bytes are
    still exactly right at export time.
    """
    original = pydicom.dcmread(ecg_src).WaveformSequence[0].WaveformData
    ds = _export_roundtrip(tmp_path, ecg_src)

    assert bytes(ds.WaveformSequence[0].WaveformData) == bytes(original)


def test_the_round_tripped_samples_decode_to_the_original_values(tmp_path, ecg_src):
    """Byte equality is the mechanism; equal signal is the promise."""
    from isocenter.waveform import decode_samples

    src_item = pydicom.dcmread(ecg_src).WaveformSequence[0]
    expected = decode_samples(bytes(src_item.WaveformData),
                              str(src_item.WaveformSampleInterpretation),
                              int(src_item.NumberOfWaveformSamples),
                              int(src_item.NumberOfWaveformChannels))

    out_item = _export_roundtrip(tmp_path, ecg_src).WaveformSequence[0]
    actual = decode_samples(bytes(out_item.WaveformData),
                            str(out_item.WaveformSampleInterpretation),
                            int(out_item.NumberOfWaveformSamples),
                            int(out_item.NumberOfWaveformChannels))

    np.testing.assert_array_equal(actual, expected)


def test_the_declared_interpretation_still_matches_the_bytes(tmp_path, ecg_src):
    """The corruption this fix must not introduce.

    Isocenter decodes US/SB/UB to int16 internally, rebasing US by 32768.
    Re-encoding from that array while leaving (5400,1006) saying "US"
    would shift every value by 32768 -- silently. Writing the original
    bytes back makes the mismatch structurally impossible rather than
    merely tested against.
    """
    src_item = pydicom.dcmread(ecg_src).WaveformSequence[0]
    out_item = _export_roundtrip(tmp_path, ecg_src).WaveformSequence[0]

    assert str(out_item.WaveformSampleInterpretation) == \
        str(src_item.WaveformSampleInterpretation)
    assert int(out_item.WaveformBitsAllocated) == int(src_item.WaveformBitsAllocated)


def test_a_multi_group_record_exports_samples_on_the_group_it_kept(tmp_path):
    """Ingest keeps group 0 only (#36); export must not imply otherwise."""
    src = tmp_path / "src"
    src.mkdir()
    ds = build_ecg_dataset(num_samples=200)
    second = copy.deepcopy(ds.WaveformSequence[0])
    second.SamplingFrequency = 25.0
    ds.WaveformSequence.append(second)
    path = str(src / "multi.dcm")
    pydicom.dcmwrite(path, ds, enforce_file_format=True)

    out = _export_roundtrip(tmp_path, path, db_name="multi.db")

    assert getattr(out.WaveformSequence[0], "WaveformData", None)


def test_a_waveform_sequence_with_no_samples_is_reported_on_export(tmp_path):
    """A file describing a waveform it does not contain must say so.

    This is the state #34 left every exported record in. It should now be
    reachable only from a source that itself carried no samples -- and
    when it happens the export must not pass in silence, because the
    resulting file is structurally plausible and empty, which is exactly
    the failure mode this fix exists to end.

    Asserts on the worker's return value rather than on caplog. The
    warning used to be emitted here, inside the worker, which is why this
    test had to drive `_export_instance_worker` directly: through
    `session.export()` the worker's logger lives in a child process where
    caplog cannot reach it. That is the bug #126 fixed -- the loss now
    comes back to the parent, which logs it and writes the audit entry --
    so the honest assertion is that the worker reported it, not that a
    log line appeared in whichever process happened to run.
    """
    from isocenter.io_handlers import ExportContext, _export_instance_worker
    from isocenter.entities import Instance
    from isocenter.io_handlers import populate_attrs

    ds = build_ecg_dataset(num_samples=100)
    del ds.WaveformSequence[0].WaveformData

    inst = Instance(str(ds.SOPInstanceUID), str(ds.SOPClassUID), 1)
    populate_attrs(ds, inst)

    ctx = ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / "empty.dcm"),
        patient_attributes={}, study_attributes={}, series_attributes={})

    outcome = _export_instance_worker(ctx)

    assert outcome.ok
    assert any("does not contain" in detail
               for _scope, detail in outcome.losses), outcome
