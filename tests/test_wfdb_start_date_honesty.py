"""The WFDB header must not assert timing or provenance it does not have.

Two claims are made in the header when a study date exists but no
time-of-day does. Both were untrue in some path:

* `_instance_only_datetime` substituted `000000`, so the record line
  carried `00:00:00` as acquisition timing that Isocenter invented (#59);
* the surviving date is written as `# de-identified start date: ...`
  unconditionally, so an export where `anonymize()` never ran labelled
  the patient's real study date as de-identified.

The second is the more serious of the two. A downstream consumer reading
that comment would believe the date had been shifted when it is the date
of care, and the header is exactly what they would check.
"""
import pathlib
from datetime import datetime

import pydicom
import pytest

from isocenter.exporters.wfdb import WfdbExporter
from isocenter.session import DicomSession


def _ecg_without_time(tmp_path, study_date="20230417", study_time=None):
    """An ECG carrying a Study Date, and a time only if asked for."""
    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")],
                           patient_id="STARTDATE001",
                           patient_name="Waveform^Test")
    ds.StudyDate = study_date
    for tag in ("StudyTime", "AcquisitionDateTime", "ContentTime",
                "AcquisitionTime"):
        if tag in ds:
            delattr(ds, tag)
    if study_time is not None:
        ds.StudyTime = study_time

    path = tmp_path / "wf.dcm"
    pydicom.dcmwrite(str(path), ds, write_like_original=False)
    return path


def _export(tmp_path, anonymize):
    """Ingests the ECG and exports WFDB, optionally de-identifying first."""
    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        session.ingest(str(tmp_path))
        if anonymize:
            config = tmp_path / "config.yaml"
            session.create_config(str(config))
            session.load_config(str(config))
            session.anonymize(session.audit())
        shifted = session.store.patients[0].studies[0].date_shifted
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()
    return pathlib.Path(paths[0]).read_text(encoding="utf-8"), shifted


def test_an_unshifted_date_is_not_labelled_de_identified(tmp_path):
    """No anonymize() ran, so nothing about this date is de-identified."""
    _ecg_without_time(tmp_path)

    header, shifted = _export(tmp_path, anonymize=False)

    assert shifted is False, "test setup: the study should not be shifted"
    assert "de-identified start date" not in header, (
        "the header claims a real, unshifted study date was de-identified:\n"
        + header)
    assert "start date: 17/04/2023" in header, (
        "the real date should still be reported, just not as de-identified")


def test_a_shifted_date_is_still_labelled_de_identified(tmp_path):
    """The claim is correct on the anonymized path and must survive."""
    _ecg_without_time(tmp_path)

    header, shifted = _export(tmp_path, anonymize=True)

    assert shifted is True, "test setup: anonymize() did not shift the date"
    assert "de-identified start date" in header


def test_the_instance_only_fallback_does_not_invent_midnight():
    """`000000` was appended when Study Time was absent (#59).

    The record line then carried `00:00:00`, which a reader cannot
    distinguish from an acquisition that really happened at midnight.
    """
    from isocenter.entities import Instance

    instance = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    instance.attributes["0008,0020"] = "20230417"

    start, note = WfdbExporter._instance_only_datetime(instance)

    assert start is None, f"midnight was invented: {start!r}"
    assert note == "start date: 17/04/2023", (
        "the date is real and should be reported, without a fabricated time")


def test_the_instance_only_fallback_keeps_a_real_time(tmp_path):
    """The control: a recorded time must still reach the record line."""
    from isocenter.entities import Instance

    instance = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    instance.attributes["0008,0020"] = "20230417"
    instance.attributes["0008,0030"] = "143005"

    start, note = WfdbExporter._instance_only_datetime(instance)

    assert start == datetime(2023, 4, 17, 14, 30, 5)
    assert note is None


def test_an_hhmm_time_is_not_discarded():
    """Study Time is a TM, and `1430` is a legal value.

    The combined stamp was only ever parsed as `%Y%m%d%H%M%S`, so a
    four-digit time failed to parse and both the time *and* the date were
    dropped -- while a study with no time at all kept its date. Real
    recorded timing, discarded.
    """
    from isocenter.entities import Instance

    instance = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    instance.attributes["0008,0020"] = "20230417"
    instance.attributes["0008,0030"] = "1430"

    start, note = WfdbExporter._instance_only_datetime(instance)

    assert start == datetime(2023, 4, 17, 14, 30), (
        f"a recorded HH:MM time was discarded: {start!r}, note={note!r}")


@pytest.mark.parametrize("stamp,expected", [
    ("143005", (14, 30, 5)),
    ("1430", (14, 30, 0)),     # was 14:03:00
    ("14", (14, 0, 0)),        # was 01:04:00
])
def test_a_dicom_time_is_read_at_the_precision_it_was_written(stamp, expected):
    """`strptime` accepts 1-2 digit fields, so the format order lied.

    Study Time is a TM: `HH`, `HHMM`, and `HHMMSS` are all legal. The
    formats were tried longest-first, and `%H%M%S` *matches* `"1430"` --
    as 14:03:00, not 14:30:00. `"14"` matched `%H%M` as 01:04:00.

    Nothing raised, so nothing fell through to the correct format. This
    is on the ordinary anonymized path: an ECG recorded at half past two
    exported claiming three minutes past.
    """
    from isocenter.entities import Instance

    instance = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    instance.attributes["0008,0030"] = stamp

    got = WfdbExporter._instance_time_of_day(instance)

    assert (got.hour, got.minute, got.second) == expected, (
        f"Study Time {stamp!r} read as {got!r}")


@pytest.mark.parametrize("stamp,expected", [
    ("20230417143005", (14, 30, 5)),
    ("202304171430", (14, 30, 0)),   # was 14:03:00
])
def test_an_acquisition_datetime_is_read_at_its_own_precision(stamp, expected):
    """Same trap, same function, on the DT rather than the TM."""
    from isocenter.entities import Instance

    instance = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    instance.attributes["0008,002a"] = stamp

    got = WfdbExporter._instance_time_of_day(instance)

    assert (got.hour, got.minute, got.second) == expected, (
        f"Acquisition DateTime {stamp!r} read as {got!r}")
