"""`release_memory()` must free what it says it freed (#35).

Waveforms decode to int16 of shape (num_samples, num_channels) and are
cached on `Instance.waveform_array` -- ~80 KB for a 10-second 12-lead,
~104 MB for a 24-hour 3-channel Holter. The one operation Isocenter
offers for reclaiming RAM never touched them.
"""
import logging
import os

import pytest

from isocenter.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture


@pytest.fixture
def ecg_session(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=400)
    session = DicomSession(persistence_file=str(tmp_path / "rm.db"))
    session.ingest(str(src))
    yield session
    session.close()


def _only_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def test_release_memory_frees_waveform_samples(ecg_session):
    inst = _only_instance(ecg_session)
    assert inst.get_waveform_data() is not None
    assert inst.waveform_array is not None

    ecg_session.release_memory()

    assert inst.waveform_array is None


def test_released_waveform_samples_are_still_recoverable(ecg_session):
    """Freeing must not be discarding."""
    inst = _only_instance(ecg_session)
    before = inst.get_waveform_data().copy()

    ecg_session.release_memory()

    import numpy as np
    np.testing.assert_array_equal(inst.get_waveform_data(), before)


def test_release_memory_reports_waveforms_distinctly_from_pixels(ecg_session, caplog):
    """"Released N images" is wrong for a waveform: it is not an image."""
    _only_instance(ecg_session).get_waveform_data()

    with caplog.at_level(logging.INFO):
        ecg_session.release_memory()

    report = " ".join(r.getMessage() for r in caplog.records)
    assert "waveform" in report.lower(), report


def test_release_memory_does_not_count_instances_that_had_nothing_resident(
        ecg_session, caplog):
    """`unload_pixel_data()` returns True when nothing was cached.

    So the old count reported every instance as freed even when the
    session held nothing in RAM -- telling the user memory was reclaimed
    when none had been, which is the failure this issue is about.
    """
    inst = _only_instance(ecg_session)
    inst.unload_pixel_data()
    inst.unload_waveform_data()
    assert inst.pixel_array is None and inst.waveform_array is None

    with caplog.at_level(logging.INFO):
        ecg_session.release_memory()

    report = " ".join(r.getMessage() for r in caplog.records)
    assert "0/1" in report or " 0 " in report, report


def test_a_waveform_with_no_loader_is_left_resident(ecg_session, caplog):
    """Unloading what cannot be reloaded is a silent discard, not a free."""
    inst = _only_instance(ecg_session)
    inst.get_waveform_data()
    inst._waveform_loader = None

    ecg_session.release_memory()

    assert inst.waveform_array is not None
