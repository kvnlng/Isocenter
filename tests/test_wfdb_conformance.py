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
