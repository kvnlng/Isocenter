"""A Study Date must be the same type before and after a store round trip.

`ingest()` parses (0008,0020) into a `datetime.date` (#60: an unreadable
date must be absent, not guessed). The store serialises that with
`isoformat()` -- correct for SQLite, whose default date adapter Python
3.12 deprecated -- but hydration had no inverse, so `Study.study_date`
came back as the ISO *string* `'2024-01-15'` where a `date` went in.

Nothing downstream checks the type, and two exporters read it:

- `session.export()` stamps `study.study_date` into (0008,0020)
  unconverted. pydicom renders a `date` as `YYYYMMDD` and writes a `str`
  verbatim, so the reloaded path emitted `'2024-01-15'` -- not a legal DA
  value (PS3.5 Table 6.2-1 fixes DA at eight digits). The export
  succeeded, nothing was audited, and the compliance report said PASS.
- `WfdbExporter._start_datetime` parses the date with
  `strptime(..., "%Y%m%d")` inside a `try`. The ISO string does not
  match, the `ValueError` is swallowed, and the function falls through to
  `_instance_only_datetime` -- the instance's own, never-shifted date
  tags. That fallback is exactly what `_start_datetime`'s docstring
  exists to prevent, and the guard did not fire: it was built against a
  *missing* study, and a reloaded session supplies a perfectly present
  one whose `study_date` is merely the wrong type.

Fixed by restoring the type at hydration, so `study_date` is one type
everywhere and both exporters are correct without either of them
learning about the store. (#171)

Not covered here, and still open: `Study.date_shifted` has no column, so
a reloaded session cannot tell a shifted date from an original one, and
the WFDB header comment loses its "de-identified" qualifier. That is a
schema change, filed separately.
"""
import pathlib
from datetime import date, datetime

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter.entities import Instance, Study
from isocenter.persistence import _as_loaded_date, _as_stored_date
from isocenter.exporters.wfdb import WfdbExporter
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"


def _write(directory, name="a.dcm", study_date="20240115"):
    """A minimal exportable Secondary Capture carrying a Study Date."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = "1.2.3.1"
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = Dataset()
    ds.file_meta = meta
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.SOPInstanceUID = "1.2.3.1"
    ds.SOPClassUID = SC
    ds.StudyInstanceUID = "1.2.4.1"
    ds.SeriesInstanceUID = "1.2.5.1"
    ds.PatientID = "P1"
    ds.PatientName = "Test^Patient"
    ds.Modality = "OT"
    ds.SeriesNumber = 1
    ds.ConversionType = "WSD"
    ds.StudyDate = study_date
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    path = pathlib.Path(directory) / name
    ds.save_as(str(path), enforce_file_format=True)
    return path


@pytest.fixture
def reloaded(tmp_path):
    """Ingests one dated instance, saves, and reopens the same store.

    Yields `(fresh_export_dir, reload_export_dir, reloaded_session)` --
    the round trip every assertion below needs, run once.
    """
    source = tmp_path / "src"
    source.mkdir()
    _write(source)

    db = tmp_path / "roundtrip.db"
    fresh_out = tmp_path / "out_fresh"
    reload_out = tmp_path / "out_reload"

    session = DicomSession(str(db))
    try:
        session.ingest(str(source))
        # The pre-condition every assertion below is measured against:
        # ingest really did produce a `date`, so a string on the far side
        # is the round trip's doing and not the fixture's.
        assert session.store.patients[0].studies[0].study_date == date(2024, 1, 15)
        session.export(str(fresh_out))
        session.save()
    finally:
        session.close()

    reopened = DicomSession(str(db))
    try:
        reopened.export(str(reload_out))
        yield fresh_out, reload_out, reopened
    finally:
        reopened.close()


def _exported_study_dates(out):
    return [pydicom.dcmread(str(f)).get("StudyDate")
            for f in sorted(pathlib.Path(out).rglob("*.dcm"))]


def test_a_study_date_is_still_a_date_after_a_reload(reloaded):
    """The type ingest produced is the type the store must return."""
    _fresh, _reload, session = reloaded

    study = session.store.patients[0].studies[0]
    assert study.study_date == date(2024, 1, 15)
    assert isinstance(study.study_date, date), (
        f"the store returned {study.study_date!r} "
        f"({type(study.study_date).__name__}) where ingest produced a date")


def test_a_reloaded_export_writes_a_legal_da_value(reloaded):
    """(0008,0020) is DA: eight digits, on both paths.

    `'2024-01-15'` is what the reloaded path used to write. pydicom warns
    ("Invalid value for VR DA") and writes it anyway, so the file leaves
    with a value no conformant reader is required to parse.
    """
    fresh_out, reload_out, _session = reloaded

    fresh = _exported_study_dates(fresh_out)
    reloaded_dates = _exported_study_dates(reload_out)

    assert fresh == ["20240115"], f"fresh export wrote {fresh!r}"
    assert reloaded_dates == ["20240115"], (
        f"reloaded export wrote {reloaded_dates!r}; DA is fixed-length "
        "YYYYMMDD (PS3.5 Table 6.2-1)")


def test_the_wfdb_start_time_still_comes_from_the_study_after_a_reload(reloaded):
    """A hydrated study must not be silently demoted to the fallback.

    `_start_datetime` prefers `study.study_date` precisely so that a
    SHIFT_DATE result wins over the instance's own un-shifted tags. When
    the study date arrived as a string the `strptime` raised, the
    `except ValueError` swallowed it, and the instance's date won
    instead -- with the study present the whole time, so the "study is
    REQUIRED" guard never fired.

    Asserted here against a study that really came out of the store,
    rather than a hand-built string, because the string is the store's
    doing and hand-building one would pin the symptom to the wrong layer.
    """
    _fresh, _reload, session = reloaded
    study = session.store.patients[0].studies[0]

    # An instance whose own timing disagrees with the study, so which of
    # the two branches ran is visible in the answer.
    instance = Instance("1.2.9.9", SC, 1)
    instance.attributes["0008,002a"] = "20260101101530.000000"

    start, _comment = WfdbExporter._start_datetime(instance, study)

    assert start is not None
    assert start.date() == date(2024, 1, 15), (
        f"_start_datetime returned {start!r}: it fell through to the "
        "instance's own date tags while a study date was available")
    # The time-of-day is always the instance's -- SHIFT_DATE never moves
    # one -- so this half staying put is the control, not a second bug.
    assert start == datetime(2024, 1, 15, 10, 15, 30)


# --- The pair as a pair -----------------------------------------------
#
# The three tests above all drive one input (`"20240115"` at ingest, i.e.
# a `date` in the store) end to end. That leaves the two rules
# `_as_loaded_date`'s docstring calls deliberate untested, and a mutation
# probe confirms it: rewriting the parse as a strict
# `strptime(value, "%Y-%m-%d")` -- the exact "tightening" the docstring
# forbids -- and rewriting the `except` to return `None` instead of the
# stored value both leave every date-related test in the suite green.
#
# Asserted against the pair rather than `_as_loaded_date` alone, because
# the claim being pinned is that it is `_as_stored_date`'s inverse. Two
# rows are deliberately *not* round-trip-equal and say so in the third
# column; those are the normalisations the docstring blesses.

@pytest.mark.parametrize("value, stored, loaded", [
    # NULL stays None: a date we do not have is not one we invent (#60).
    (None, None, None),
    # The case ingest actually produces: exact round trip.
    (date(2024, 1, 15), "2024-01-15", date(2024, 1, 15)),
    # Pre-1900 and year 1 -- `strftime` territory, but `isoformat` is
    # unbothered and neither may be silently dropped.
    (date(1880, 5, 3), "1880-05-03", date(1880, 5, 3)),
    (date(1, 1, 1), "0001-01-01", date(1, 1, 1)),
    # Unparseable values are returned as they were stored, never
    # replaced with None and never guessed at (#60).
    ("junk", "junk", "junk"),
    ("2024-13-45", "2024-13-45", "2024-13-45"),
    ("", "", ""),
    # Deliberate normalisation, not a round trip: a DA-spelled string set
    # by hand (rather than parsed at ingest) loads as a `date`, because
    # one type everywhere is the whole point. Do not "fix" this row by
    # tightening the parse.
    ("20240115", "20240115", date(2024, 1, 15)),
    ("2024-01-15", "2024-01-15", date(2024, 1, 15)),
])
def test_the_stored_and_loaded_date_helpers_are_inverses(value, stored, loaded):
    assert _as_stored_date(value) == stored
    assert _as_loaded_date(stored) == loaded
    assert type(_as_loaded_date(stored)) is type(loaded)


def test_a_datetime_study_date_is_refused_at_the_boundary():
    """The one input the inverse could not restore is now never stored.

    This flips the pin that used to stand here (#188). A `datetime` in
    `study_date` round-tripped as the ISO string
    `'2024-01-15T10:30:00'` -- `_as_stored_date` calls `isoformat()`,
    `date.fromisoformat` rejects the result -- and from there it was
    #171's original defect again: exported unconverted into (0008,0020),
    a VR PS3.5 Table 6.2-1 fixes at eight digits.

    Rejection, not truncation, was the decision: silently dropping a
    time-of-day the caller supplied is the same quiet lossy
    normalisation #60 forbids in the other direction, and Study Time
    (0008,0030) is where that half belongs. Nothing in the library
    produces a `datetime` here -- ingest parses (0008,0020) with
    `.date()` -- so the only writes this refuses are hand-set ones,
    which were exactly the ones that broke.
    """
    # The boundary is the field, so the constructor and a later
    # assignment refuse identically -- the dataclass __init__ assigns
    # through the same __setattr__.
    with pytest.raises(TypeError, match=r"\.date\(\)"):
        Study("1.2.9.1", datetime(2024, 1, 15, 10, 30))

    study = Study("1.2.9.1", date(2024, 1, 15))
    with pytest.raises(TypeError, match=r"0008,0030"):
        study.study_date = datetime(2024, 1, 15, 10, 30)

    # A refused write leaves the field as it was, and the values the
    # library itself produces still assign: a date, and None (#60).
    assert study.study_date == date(2024, 1, 15)
    study.study_date = date(2024, 2, 1)
    assert study.study_date == date(2024, 2, 1)
    study.study_date = None
    assert study.study_date is None
