"""Validate Gantry's WFDB output against PhysioNet's own reader.

Deliberately does NOT use any Gantry code to read the output back.
"""
import os

import numpy as np
import pytest

wfdb = pytest.importorskip("wfdb", reason="conformance tests need the wfdb package")

from gantry.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture, LEADS


@pytest.fixture
def exported(tmp_path):
    """Ingest a synthetic ECG and export it as WFDB. Returns the record path."""
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=1000, baseline_uv=0.0)

    out = tmp_path / "out"
    session = DicomSession(persistence_file=str(tmp_path / "conf.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(out), format="wfdb")
        assert len(paths) == 1
        yield paths[0]
    finally:
        session.close()


def _read(hea_path):
    """Read a record with PhysioNet's wfdb, given its .hea path."""
    directory = os.path.dirname(hea_path)
    name = os.path.splitext(os.path.basename(hea_path))[0]
    return wfdb.rdrecord(os.path.join(directory, name))


def test_physionet_reader_accepts_the_record(exported):
    record = _read(exported)
    assert record.n_sig == len(LEADS)
    assert record.sig_len == 1000
    assert record.fs == 500


def test_digital_samples_survive_the_round_trip(exported):
    """Channel c was written as (i % 1000) + c*1000 by the generator.

    Reads d_signal directly (physical=False) rather than record.adc(),
    which re-derives digital values from p_signal using the same
    gain/baseline the reader just parsed -- a more independent check.
    """
    directory = os.path.dirname(exported)
    name = os.path.splitext(os.path.basename(exported))[0]
    record = wfdb.rdrecord(os.path.join(directory, name), physical=False)
    digital = record.d_signal
    assert digital.shape == (1000, len(LEADS))
    for channel in range(record.n_sig):
        expected = ((np.arange(1000) % 1000) + channel * 1000).astype(np.int16)
        np.testing.assert_array_equal(digital[:, channel], expected)


def test_channels_are_not_transposed(exported):
    directory = os.path.dirname(exported)
    name = os.path.splitext(os.path.basename(exported))[0]
    record = wfdb.rdrecord(os.path.join(directory, name), physical=False)
    digital = record.d_signal
    assert digital[0, 0] == 0
    assert digital[0, 1] == 1000
    assert digital[0, 2] == 2000


def test_units_are_reported_correctly(exported):
    record = _read(exported)
    assert set(record.units) == {"uV"}


def test_lead_descriptions_come_from_the_coded_source(exported):
    record = _read(exported)
    assert record.sig_name[0] == "MDC_ECG_LEAD_I"
    assert record.sig_name[1] == "MDC_ECG_LEAD_II"


def test_no_comment_lines_reach_the_output(exported):
    with open(exported, encoding="utf-8") as f:
        lines = f.readlines()
    assert lines, "expected header file to contain content"
    assert not any(line.startswith("#") for line in lines)
    record = _read(exported)
    assert not record.comments


def test_physical_values_match_the_declared_calibration(exported):
    """The generator uses sensitivity 1.0 uV/adu, so physical == digital."""
    record = _read(exported)
    physical = record.p_signal
    digital = record.adc()
    np.testing.assert_allclose(physical[:, 0], digital[:, 0].astype(float),
                               rtol=1e-6, atol=1e-6)


def test_nonzero_baseline_round_trips_to_the_correct_physical_zero(tmp_path):
    """Baseline is the formula most likely to be sign-inverted.

    With sensitivity 1.0 uV/adu and baseline 250 uV, physical zero must sit
    at ADC -250. If this fails, flip the sign in
    WaveformChannel.wfdb_baseline().
    """
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100, baseline_uv=250.0)

    out = tmp_path / "out"
    session = DicomSession(persistence_file=str(tmp_path / "bl.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(out), format="wfdb")
    finally:
        session.close()

    record = _read(paths[0])
    assert record.baseline[0] == -250

    # physical = (adc - baseline) / gain = (adc + 250) / 1.0
    digital = record.adc()
    expected = digital[:, 0].astype(float) + 250.0
    np.testing.assert_allclose(record.p_signal[:, 0], expected, rtol=1e-6, atol=1e-6)


def test_dat_file_sits_next_to_the_header(exported):
    directory = os.path.dirname(exported)
    name = os.path.splitext(os.path.basename(exported))[0]
    assert os.path.exists(os.path.join(directory, f"{name}.dat"))


# --- Final review Important 1/2/3, end-to-end against the reference reader ---
#
# `test_no_comment_lines_reach_the_output` and `test_units_are_reported_
# correctly` above only ever build a header from benign fixture channels;
# no input in either test can produce the defects below. These three
# reproduce the final-review findings through a real
# session.ingest() -> session.export() and read the result back with
# PhysioNet's own `wfdb.rdheader`/`rdrecord`, not Gantry's own code.


def test_channel_label_newline_cannot_manufacture_a_hea_comment(tmp_path):
    """A Channel Label containing a newline must not be surfaced by the
    reference reader as a real header comment.

    Pre-fix, `wfdb.rdheader` returned
    `comments=['Patient Jane Doe MRN9988776']` for exactly this input --
    a PHI escape route, since this module's own docstring and
    docs/waveforms.md both assert no comment lines are ever written.

    Also exercises the lead-name allowlist (#39): "Lead I\\n# Patient Jane
    Doe MRN9988776" is not a recognisable lead name, so besides not
    manufacturing a fake comment line, none of this operator text -- name,
    MRN, or the embedded newline -- should reach the header at all; it is
    replaced with a positional token.
    """
    import pydicom

    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")], num_samples=50)
    chdef = ds.WaveformSequence[0].ChannelDefinitionSequence[0]
    del chdef.ChannelSourceSequence
    chdef.ChannelLabel = "Lead I\n# Patient Jane Doe MRN9988776"

    src = tmp_path / "src"
    src.mkdir()
    pydicom.dcmwrite(str(src / "ecg.dcm"), ds, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "inject.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    assert paths, "export produced no .hea files"
    record = _read(paths[0])
    assert record.comments == [], (
        "a newline embedded in Channel Label reached the reference "
        f"reader as a real header comment: {record.comments!r}")

    with open(paths[0], encoding="utf-8") as f:
        raw_lines = f.readlines()
    assert not any(line.startswith("#") for line in raw_lines)
    assert record.sig_name == ["ch0"], (
        "expected the free-text Channel Label to be replaced with a "
        f"positional token; got sig_name={record.sig_name!r}")
    assert "Patient Jane Doe MRN9988776" not in "".join(raw_lines), (
        "operator text embedded in Channel Label reached the header; "
        "expected the lead-name allowlist to replace it with a positional "
        "token instead")


def test_coded_channel_source_newline_cannot_manufacture_a_hea_comment(tmp_path):
    """A coded Channel Source Sequence value containing a newline must not
    be surfaced by the reference reader as a real header comment.

    The lead-name allowlist (#39) only filters the free-text Channel
    Label fallback: `wfdb_description()` returns a coded `source_code`
    verbatim and unconditionally, on the assumption that "a coded value
    cannot contain an operator-typed patient name." That assumption holds
    for the DICOM standard's own SH VR, which forbids embedded control
    characters -- but pydicom does not enforce that on read or write.
    Verified directly: `pydicom.dcmwrite`/`dcmread` round-trip a CodeValue
    containing an embedded newline and a leading "#" completely unchanged,
    with no error and no warning. So a non-conformant (or malicious)
    source can still put a newline into `source_code`, and this test
    exercises the one remaining production path where
    `_sanitize_description` -- not the allowlist -- is what stops that
    from forging a `.hea` comment line.
    """
    import pydicom

    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")], num_samples=50)
    chdef = ds.WaveformSequence[0].ChannelDefinitionSequence[0]
    chdef.ChannelSourceSequence[0].CodeValue = "MDC\n# Patient Jane Doe MRN9988776"

    src = tmp_path / "src"
    src.mkdir()
    pydicom.dcmwrite(str(src / "ecg.dcm"), ds, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "inject_source.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    assert paths, "export produced no .hea files"
    record = _read(paths[0])
    assert record.comments == [], (
        "a newline embedded in a coded Channel Source value reached the "
        f"reference reader as a real header comment: {record.comments!r}")

    with open(paths[0], encoding="utf-8") as f:
        raw_lines = f.readlines()
    assert len(raw_lines) == 2, (
        "the embedded newline manufactured an extra physical line in the "
        f".hea file: {raw_lines!r}")
    assert not any(line.startswith("#") for line in raw_lines)
    # Unlike the free-text Channel Label case, a coded source value is
    # trusted content -- not run through the lead-name allowlist -- so
    # the text itself is expected to survive; only the line-breaking
    # character that could forge a comment line is replaced.
    assert "MDC # Patient Jane Doe MRN9988776" in "".join(raw_lines), (
        f"expected the newline to be replaced with a space, not stripped "
        f"or otherwise mangled: {raw_lines!r}")


def test_units_with_embedded_whitespace_does_not_shift_fields_via_reference_reader(tmp_path):
    """Embedded whitespace in `units` (field 3 of 9, NOT the last field)
    must not shift every subsequent signal-line field for a conformant
    reader.

    Pre-fix, `wfdb.rdheader` returned `units=['mV']` and
    `sig_name=['per s 16 0 0 1225 0 MDC_ECG_LEAD_I']` for CodeValue
    "mV per s" -- a silently wrong signal name, no error raised.
    """
    import pydicom

    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")],
                           num_samples=50, units="mV per s")
    src = tmp_path / "src"
    src.mkdir()
    pydicom.dcmwrite(str(src / "ecg.dcm"), ds, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "units.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    assert paths, "export produced no .hea files"
    record = _read(paths[0])
    assert record.sig_name == ["MDC_ECG_LEAD_I"], (
        "embedded whitespace in units shifted the signal-line fields: "
        f"sig_name={record.sig_name!r} units={record.units!r}")


def test_missing_channel_definitions_do_not_abort_the_export(tmp_path):
    """NumberOfWaveformChannels > 0 with an empty Channel Definition
    Sequence (non-conformant source) must not raise out of
    session.export() and abort the whole batch.

    Pre-fix: IndexError: list index out of range, propagating out of
    session.export(folder, format="wfdb") with no per-instance guard.
    """
    import datetime

    from gantry.entities import DicomItem, Instance, Patient, Series, Study
    from gantry.io_handlers import populate_attrs
    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")], num_samples=50)

    patient = Patient("MISSING01", "ANON_MISSING01")
    study = Study("1.2.3.MISSING.STUDY", datetime.date(2026, 1, 1))
    series = Series("1.2.3.MISSING.SERIES", "ECG", 1)
    instance = Instance("1.2.3.MISSING.SOP", ds.SOPClassUID, 1)

    wf_item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], wf_item)
    wf_item.sequences["003a,0200"].items = []  # non-conformant: empty
    instance.add_sequence_item("5400,0100", wf_item)
    instance.waveform_array = np.frombuffer(
        ds.WaveformSequence[0].WaveformData, dtype="<i2"
    ).reshape(50, 1).copy()

    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)

    session = DicomSession(":memory:")
    session.store.patients.append(patient)

    # Must not raise. Whatever the (fallback-calibrated) record looks
    # like, if it was written at all it must be real WFDB the reference
    # reader accepts -- not silent corruption.
    paths = session.export(str(tmp_path / "out"), format="wfdb")

    # NOT `if paths:` -- the per-instance containment this test exercises
    # swallows the IndexError and returns [], which would skip every
    # assertion below and leave the test unable to fail. Assert the record
    # was actually written, then assert it is real WFDB the reference
    # reader accepts -- not silent corruption.
    assert paths, (
        "export produced no record: the instance was skipped rather than "
        "written with fallback calibration")
    record = _read(paths[0])
    assert record.n_sig == 1
