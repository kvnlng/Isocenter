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


def _rhythm_plus_median(path, rhythm_samples=5000, median_samples=1200):
    """A two-group ECG: an 8ch/500 Hz rhythm strip plus a 1000 Hz median beat.

    The shape real 12-lead carts emit, and the one that made the defect
    visible: the second group is at a *different* sampling frequency, so
    it cannot be folded into the first.
    """
    ds = build_ecg_dataset(num_samples=rhythm_samples, sampling_frequency=500.0)
    median = copy.deepcopy(ds.WaveformSequence[0])
    median.SamplingFrequency = 1000.0
    median.NumberOfWaveformSamples = median_samples
    median.WaveformData = np.zeros(
        median_samples * int(median.NumberOfWaveformChannels),
        dtype="<i2").tobytes()
    ds.WaveformSequence.append(median)
    pydicom.dcmwrite(str(path), ds, enforce_file_format=True)
    return str(path)


def test_no_exported_multiplex_group_declares_samples_it_does_not_carry(tmp_path):
    """Waveform Data (5400,1010) is Type 1: required, never absent (#160).

    Ingest keeps group 0's samples and discards the rest (#36), but the
    sequence *metadata* came in by a different path -- `populate_attrs`
    walks the whole sequence -- so every group's item reached the graph
    and was written out. The exported file declared a multiplex group of
    8 channels at 1000 Hz over 1200 samples and carried none of them: a
    conformant reader may reject it, and one that trusts Type 1 without
    checking reads a sample count with nothing behind it.
    """
    src = tmp_path / "src"
    src.mkdir()
    path = _rhythm_plus_median(src / "multi.dcm")

    out = _export_roundtrip(tmp_path, path, db_name="orphan.db")

    hollow = [i for i, item in enumerate(out.WaveformSequence)
              if not getattr(item, "WaveformData", None)]
    assert not hollow, (
        f"exported WaveformSequence items {hollow} declare a multiplex "
        f"group with no Waveform Data")


# --- Annotations referencing a discarded group (#177) -----------------
#
# Waveform Annotation Sequence (0040,B020) lives at instance level, not
# inside the multiplex group it refers to; Referenced Waveform Channels
# (0040,A0B0) names a group by ordinal -- PS3.3 C.10.10.1.1, "the
# ordinal of the Item of Waveform Sequence (5400,0100)", 1-based. #160
# removed the sequence items whose samples ingest discarded, so an
# annotation naming one of them referenced an item no longer in the
# exported file. These tests pin the filter that goes with the del.


def _annotated_two_group_file(path, channels_per_annotation):
    """A two-group ECG with one annotation per entry.

    Each entry of `channels_per_annotation` is either None -- an
    annotation with no Referenced Waveform Channels at all (Type 1C:
    it applies to the whole waveform) -- or a flat list of
    (group, channel) pairs written verbatim into (0040,A0B0).
    """
    from scripts.generate_waveform_test_data import add_annotation

    ds = build_ecg_dataset(num_samples=200)
    second = copy.deepcopy(ds.WaveformSequence[0])
    second.SamplingFrequency = 25.0
    ds.WaveformSequence.append(second)

    for refs in channels_per_annotation:
        add_annotation(ds, start_sample=10)
        ann = ds.WaveformAnnotationSequence[-1]
        if refs is None:
            del ann.ReferencedWaveformChannels
        else:
            ann.ReferencedWaveformChannels = list(refs)

    pydicom.dcmwrite(str(path), ds, enforce_file_format=True)
    return str(path)


def test_an_annotation_on_a_discarded_group_does_not_reach_the_export(tmp_path):
    """The annotation survived #160; its referent did not.

    A strict downstream reader is entitled to reject a file whose
    annotation names a Waveform Sequence item the file does not carry
    (#177).
    """
    src = tmp_path / "src"
    src.mkdir()
    _annotated_two_group_file(src / "ann.dcm",
                              [[1, 1], [2, 1]])

    out = _export_roundtrip(tmp_path, str(src / "ann.dcm"), db_name="ann.db")

    assert len(out.WaveformSequence) == 1
    anns = list(getattr(out, "WaveformAnnotationSequence", []))
    assert len(anns) == 1, anns
    assert list(anns[0].ReferencedWaveformChannels) == [1, 1]


def test_surviving_pairs_are_kept_when_only_some_references_dangle(tmp_path):
    """It is a filter on (group, channel) pairs before it is a drop.

    An annotation may name several pairs -- all of group 1 plus a
    channel of group 2 -- and dropping the whole item because one pair
    dangles would lose a mark that still has a placeable home (#177).
    """
    src = tmp_path / "src"
    src.mkdir()
    _annotated_two_group_file(src / "ann.dcm",
                              [[1, 2, 2, 3]])

    out = _export_roundtrip(tmp_path, str(src / "ann.dcm"), db_name="pairs.db")

    anns = list(getattr(out, "WaveformAnnotationSequence", []))
    assert len(anns) == 1, anns
    assert list(anns[0].ReferencedWaveformChannels) == [1, 2]


def test_a_channelless_annotation_is_not_touched(tmp_path):
    """(0040,A0B0) is Type 1C; absent means the whole waveform.

    An annotation with no channel reference cannot dangle, and dropping
    it would silently empty the sequence for a conformant common case.
    """
    src = tmp_path / "src"
    src.mkdir()
    _annotated_two_group_file(src / "ann.dcm", [None, [2, 1]])

    out = _export_roundtrip(tmp_path, str(src / "ann.dcm"), db_name="nochan.db")

    anns = list(getattr(out, "WaveformAnnotationSequence", []))
    assert len(anns) == 1, anns
    assert "ReferencedWaveformChannels" not in anns[0]


def test_a_single_group_record_keeps_its_annotations_verbatim(tmp_path):
    """No groups discarded, nothing to filter -- including ordinal 0.

    A 0-counting source (Isocenter's own fixtures wrote 0 until #159)
    reads as the first group, not as a dangling reference.
    """
    from scripts.generate_waveform_test_data import add_annotation

    src = tmp_path / "src"
    src.mkdir()
    ds = build_ecg_dataset(num_samples=200)
    add_annotation(ds, start_sample=10, group=1, channel=2)
    pydicom.dcmwrite(str(src / "one.dcm"), ds, enforce_file_format=True)

    out = _export_roundtrip(tmp_path, str(src / "one.dcm"), db_name="one.db")

    anns = list(getattr(out, "WaveformAnnotationSequence", []))
    assert len(anns) == 1, anns
    assert list(anns[0].ReferencedWaveformChannels) == [1, 2]
